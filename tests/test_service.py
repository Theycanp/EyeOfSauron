from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from signalwatch.database import Database
from signalwatch.models import FeedFetchResult
from signalwatch.rules import RuleSet
from signalwatch.service import SignalWatchService, retry_delay

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
        return SignalWatchService(
            self.config,
            self.database,
            {self.source.id: collector},
            self.rules,
            notifier,
        )

    async def test_poll_baseline_then_deliver_new_alert(self) -> None:
        collector = _Collector(FeedFetchResult((observation("baseline", "Ordinary news"),), None, None))
        notifier = _Notifier()
        service = self._service(collector, notifier)
        self.assertTrue(await service.poll_source_once(self.source.id))
        collector.result = FeedFetchResult(
            (observation("new", "Breaking: Prime Minister Resigns"),), None, None
        )
        self.assertTrue(await service.poll_source_once(self.source.id))
        self.assertTrue(await service.deliver_one())
        self.assertEqual(1, len(notifier.sent))
        self.assertEqual("signalwatch", notifier.sent[0].topic)

    async def test_notification_failure_returns_alert_to_pending(self) -> None:
        self.database.enqueue_test_alert("signalwatch", 1)
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

    def test_retry_delay_is_bounded(self) -> None:
        self.assertEqual(5, retry_delay(1, 3600))
        self.assertEqual(10, retry_delay(2, 3600))
        self.assertEqual(3600, retry_delay(100, 3600))


if __name__ == "__main__":
    unittest.main()
