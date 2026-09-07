from __future__ import annotations

import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

from argus.database import Database
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
            def process_once(self, now: int):  # type: ignore[no-untyped-def]
                self.thread_id = threading.get_ident()
                service.request_stop()
                return None

            thread_id = None

        scheduler = Scheduler()
        service.digest_scheduler = scheduler  # type: ignore[assignment]
        await service._digest_loop()
        self.assertEqual(owner_thread, scheduler.thread_id)


if __name__ == "__main__":
    unittest.main()
