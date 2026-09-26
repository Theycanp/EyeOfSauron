from __future__ import annotations

import asyncio
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

from argus.database import Database
from argus.content import ContentDocumentDraft, ContentFetchError, ContentFetchRequest, ContentLevel
from argus.models import FeedFetchResult
from argus.rules import RuleSet
from argus.reminders import parse_reminder
from argus.service import ArgusService, retry_delay
from unittest.mock import patch

from helpers import observation, production_config


class _Collector:
    def __init__(self, result: FeedFetchResult) -> None:
        self.result = result

    def fetch(self, state):  # type: ignore[no-untyped-def]
        return self.result


class _Notifier:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent = []

    def publish(self, alert):  # type: ignore[no-untyped-def]
        if self.fail:
            raise RuntimeError("temporary")
        self.sent.append(alert)


class _FlakyCollector:
    def __init__(self, failures: int, result: FeedFetchResult) -> None:
        self.failures = failures
        self.result = result
        self.calls = 0

    def fetch(self, state):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("transient TLS failure")
        return self.result


class _ContentFetcher:
    def __init__(self, error=None):  # type: ignore[no-untyped-def]
        self.error = error

    def fetch(self, item):  # type: ignore[no-untyped-def]
        if self.error is not None:
            raise self.error
        return ContentDocumentDraft(
            ContentLevel.DOCUMENT,
            "public_text",
            "Official public document content. " * 5,
            canonical_url=item.request.url,
            rights_policy="public_official_document",
        )


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = production_config(self.root)
        self.source = self.config.sources[0]
        self.config = self.config.__class__(
            schema_version=self.config.schema_version,
            service=self.config.service,
            ntfy=self.config.ntfy,
            sources=(self.source,),
            rules=self.config.rules,
        )
        self.database = Database(self.config.service.database_path)
        self.rules = RuleSet.from_config(self.config.rules, self.config.ntfy.default_topic)

    async def asyncTearDown(self) -> None:
        self.database.close()
        self.temp.cleanup()

    def _service(self, collector, notifier):  # type: ignore[no-untyped-def]
        return ArgusService(
            self.config,
            self.database,
            {self.source.id: collector},
            self.rules,
            notifier,
        )

    async def test_weather_invalid_forecast_records_failure_without_setting_baseline(self) -> None:
        from unittest.mock import Mock
        from argus.weather import WeatherForecast
        service = self._service(None, None)
        service.weather_provider = Mock()
        service.weather_provider.fetch.return_value = WeatherForecast(0, 20, 2, ())
        self.assertFalse(await service.process_weather_once())
        status = self.database.get_weather_status()
        self.assertEqual(1, status["consecutive_failures"])
        self.assertIsNone(status["rain_expected"])

    async def test_weather_location_revision_wakes_hourly_worker(self) -> None:
        from dataclasses import asdict
        from unittest.mock import AsyncMock, Mock

        service = self._service(None, None)
        service.weather_provider = Mock()
        current = self.database.get_weather_subscription()
        calls = 0

        async def poll() -> bool:
            nonlocal calls
            calls += 1
            if calls == 1:
                editable = {key: value for key, value in asdict(current).items() if key != "id"}
                self.database.update_weather_subscription(
                    {**editable, "label": "new location", "latitude": 39.9},
                    actor="tester", now=int(time.time()),
                )
            else:
                service.stop_event.set()
            return True

        service.process_weather_once = AsyncMock(side_effect=poll)
        await asyncio.wait_for(service._weather_loop(), timeout=3)
        self.assertEqual(2, calls)

    async def test_local_weather_revision_starts_all_three_channels(self) -> None:
        from unittest.mock import Mock
        from argus.weather import WeatherAstronomy

        service = self._service(None, None)
        provider = Mock()
        provider.fetch_minutely.return_value = Mock()
        provider.fetch_alerts.return_value = ()
        provider.fetch_astronomy.return_value = WeatherAstronomy("20260926", None, None, None, None, None, None)
        service.local_weather_provider = provider
        with (patch.object(self.database, "record_weather_nowcast", return_value=0),
              patch.object(self.database, "record_official_weather_alerts", return_value=0),
              patch.object(self.database, "record_weather_astronomy", side_effect=lambda *_args, **_kwargs: service.stop_event.set()),
              patch("argus.service.nowcast_signal", return_value=None)):
            await asyncio.wait_for(service._local_weather_loop(), timeout=3)
        provider.fetch_minutely.assert_called_once()
        provider.fetch_alerts.assert_called_once()
        provider.fetch_astronomy.assert_called_once()

    def _queue_content_fetch(self) -> int:
        baseline = FeedFetchResult((), None, None)
        self.database.record_source_success(
            self.source.id, baseline, self.rules, int(time.time()) - 2, self.config.ntfy.default_topic
        )
        item = observation("content", "Breaking: official release", "Feed excerpt", timestamp=int(time.time()))
        item = replace(
            item,
            content_fetch=ContentFetchRequest(
                item.url, ("www.bloomberg.com",), max_response_bytes=4096, timeout_seconds=10
            ),
        )
        self.database.record_source_success(
            self.source.id,
            FeedFetchResult((item,), None, None),
            self.rules,
            int(time.time()),
            self.config.ntfy.default_topic,
        )
        return int(self.database.list_alerts()[0]["id"])

    async def test_content_enrichment_completes_without_changing_source_health(self) -> None:
        alert_id = self._queue_content_fetch()
        service = self._service(_Collector(FeedFetchResult((), None, None)), _Notifier())
        service.content_fetcher = _ContentFetcher()
        self.assertTrue(await service.process_content_fetch_once())
        detail = self.database.get_alert_detail(alert_id)
        assert detail is not None
        self.assertEqual("document", detail["documents"][0]["level"])
        self.assertEqual(0, self.database.get_source_state(self.source.id).consecutive_failures)

    async def test_content_enrichment_failure_is_isolated_and_terminal(self) -> None:
        alert_id = self._queue_content_fetch()
        service = self._service(_Collector(FeedFetchResult((), None, None)), _Notifier())
        service.content_fetcher = _ContentFetcher(
            ContentFetchError("not public", retryable=False, kind="url_policy")
        )
        self.assertTrue(await service.process_content_fetch_once())
        detail = self.database.get_alert_detail(alert_id)
        assert detail is not None
        self.assertEqual("dead", detail["content_fetch"]["status"])
        self.assertEqual(0, self.database.get_source_state(self.source.id).consecutive_failures)

    async def test_poll_baseline_then_deliver_new_alert(self) -> None:
        recent = int(time.time()) - 60
        collector = _Collector(FeedFetchResult((observation("baseline", "Ordinary news", timestamp=recent),), None, None))
        notifier = _Notifier()
        service = self._service(collector, notifier)
        self.assertTrue(await service.poll_source_once(self.source.id))
        collector.result = FeedFetchResult(
            (observation("new", "Breaking: Prime Minister Resigns", timestamp=recent),), None, None
        )
        self.assertTrue(await service.poll_source_once(self.source.id))
        self.assertTrue(await service.deliver_one())
        self.assertEqual(1, len(notifier.sent))
        self.assertEqual("eos", notifier.sent[0].topic)

    async def test_poll_attaches_configured_information_metadata(self) -> None:
        self.source = replace(
            self.source,
            region="JP",
            source_tier="primary",
            default_importance=4,
            settings={**self.source.settings, "topic": "policy"},
        )
        self.config = replace(self.config, sources=(self.source,))
        item = observation("metadata", "Official policy update", timestamp=int(time.time()))
        service = self._service(_Collector(FeedFetchResult((item,), None, None)), _Notifier())
        self.assertTrue(await service.poll_source_once(self.source.id))
        rows = self.database.list_observations(0, int(time.time()) + 1)
        self.assertEqual(1, len(rows))
        self.assertEqual("JP", rows[0]["region"])
        self.assertEqual("primary", rows[0]["source_tier"])
        self.assertEqual("policy", rows[0]["topic"])
        self.assertEqual(4, rows[0]["importance"])

    async def test_notification_failure_returns_alert_to_pending(self) -> None:
        self.database.enqueue_test_alert("eos", int(time.time()))
        service = self._service(_Collector(FeedFetchResult((), None, None)), _Notifier(fail=True))
        self.assertFalse(await service.deliver_one())
        self.assertEqual(1, self.database.status()["outbox"]["pending"])

    async def test_source_retries_inside_one_poll_cycle(self) -> None:
        self.source = replace(self.source, retry_base_seconds=0)
        self.config = replace(self.config, sources=(self.source,))
        collector = _FlakyCollector(
            failures=2,
            result=FeedFetchResult((observation("baseline", "Ordinary news"),), None, None),
        )
        service = self._service(collector, _Notifier())
        self.assertTrue(await service.poll_source_once(self.source.id))
        self.assertEqual(3, collector.calls)
        state = self.database.get_source_state(self.source.id)
        self.assertEqual(0, state.consecutive_failures)

    async def test_due_reminder_uses_existing_delivery_worker(self) -> None:
        due = int(time.time()) + 60
        reminder = parse_reminder(
            {
                "title": "测试提醒",
                "message": "这是调度器生成的消息。",
                "schedule_kind": "once",
                "run_at": due,
                "timezone": "UTC",
                "enabled": True,
                "priority": 3,
                "tags": ["alarm_clock"],
            },
            due - 60,
        )
        self.database.upsert_reminder(reminder, "tester", due - 60)
        notifier = _Notifier()
        service = self._service(_Collector(FeedFetchResult((), None, None)), notifier)
        with patch("argus.service.now_epoch", return_value=due):
            self.assertEqual(1, service.process_reminders_once())
            self.assertTrue(await service.deliver_one())
        self.assertEqual("测试提醒", notifier.sent[0].title)

    def test_retry_delay_is_bounded(self) -> None:
        self.assertEqual(5, retry_delay(1, 3600))
        self.assertEqual(10, retry_delay(2, 3600))
        self.assertEqual(3600, retry_delay(100, 3600))

    async def test_source_supervisor_contains_one_source_exception(self) -> None:
        service = self._service(_Collector(FeedFetchResult((), None, None)), _Notifier())
        calls = 0

        async def poll(source_id: str) -> bool:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("rule bug")
            service.request_stop()
            return True

        service.poll_source_once = poll  # type: ignore[method-assign]
        await service._source_loop(self.source.id, 0)
        self.assertEqual(2, calls)

    async def test_normal_poll_does_not_mark_source_as_starting(self) -> None:
        collector = _Collector(FeedFetchResult((), None, None))
        service = self._service(collector, _Notifier())
        with patch.object(self.database, "mark_source_runtime", wraps=self.database.mark_source_runtime) as mark:
            await service.poll_source_once(self.source.id)
        self.assertEqual("active", mark.call_args_list[0].args[1])
        self.assertNotIn("starting", [call.args[1] for call in mark.call_args_list])

    async def test_digest_loop_keeps_database_work_on_event_loop_thread(self) -> None:
        owner_thread = threading.get_ident()
        service = self._service(_Collector(FeedFetchResult((), None, None)), _Notifier())

        class Scheduler:
            async def process_once_async(self, now: int):  # type: ignore[no-untyped-def]
                self.thread_id = threading.get_ident()
                service.request_stop()
                return None

            thread_id = None

        scheduler = Scheduler()
        service.digest_scheduler = scheduler  # type: ignore[assignment]
        await service._digest_loop()
        self.assertEqual(owner_thread, scheduler.thread_id)

    async def test_event_pool_yields_after_every_observation(self) -> None:
        service = self._service(_Collector(FeedFetchResult((), None, None)), _Notifier())
        calls: list[int] = []
        peer_ran = asyncio.Event()

        def project_once(*, limit: int = 50) -> int:
            calls.append(limit)
            if len(calls) == 2:
                self.assertTrue(peer_ran.is_set())
                service.request_stop()
            return 1

        async def peer() -> None:
            await asyncio.sleep(0)
            peer_ran.set()

        service.process_event_pool_once = project_once  # type: ignore[method-assign]
        peer_task = asyncio.create_task(peer())
        await asyncio.wait_for(service._event_pool_loop(), 2)
        await peer_task
        self.assertEqual([1, 1], calls)


if __name__ == "__main__":
    unittest.main()
