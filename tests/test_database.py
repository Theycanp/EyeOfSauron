from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from signalwatch.database import Database
from signalwatch.models import FeedFetchResult
from signalwatch.rules import RuleSet

from helpers import observation, production_config


NOW = 1788363000


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = production_config(self.root)
        self.database = Database(self.config.service.database_path)
        self.rules = RuleSet.from_config(self.config.rules, self.config.ntfy.default_topic)

    def tearDown(self) -> None:
        self.database.close()
        self.temp.cleanup()

    def _success(self, source_id: str, *items):  # type: ignore[no-untyped-def]
        return self.database.record_source_success(
            source_id,
            FeedFetchResult(tuple(items), 'W/"etag"', None),
            self.rules,
            NOW,
            self.config.ntfy.default_topic,
        )

    def test_first_collection_is_baseline_even_when_item_matches(self) -> None:
        report = self._success(
            "bloomberg_markets",
            observation("initial-breaking", "Breaking: Prime Minister Resigns"),
        )
        self.assertTrue(report.baseline_created)
        self.assertEqual(0, report.queued_alerts)
        self.assertEqual({}, self.database.status()["outbox"])

    def test_new_matching_item_is_queued_once(self) -> None:
        self._success("bloomberg_markets", observation("baseline", "Ordinary market article"))
        first = self._success(
            "bloomberg_markets",
            observation("new-breaking", "Breaking: Prime Minister Resigns"),
        )
        second = self._success(
            "bloomberg_markets",
            observation("new-breaking", "Breaking: Prime Minister Resigns"),
        )
        self.assertEqual(1, first.queued_alerts)
        self.assertEqual(0, second.queued_alerts)
        self.assertEqual(1, self.database.status()["outbox"]["pending"])

    def test_guid_is_deduplicated_across_sections(self) -> None:
        self._success("bloomberg_markets")
        self._success("bloomberg_politics")
        first = self._success(
            "bloomberg_markets",
            observation("shared-guid", "Breaking: Prime Minister Resigns"),
        )
        second = self._success(
            "bloomberg_politics",
            observation(
                "shared-guid",
                "Breaking: Prime Minister Resigns",
                source_id="bloomberg_politics",
            ),
        )
        self.assertEqual(1, first.queued_alerts)
        self.assertEqual(0, second.inserted_observations)
        self.assertEqual(1, self.database.status()["outbox"]["pending"])

    def test_outbox_lease_retry_and_delivery(self) -> None:
        self.assertTrue(self.database.enqueue_test_alert("signalwatch", NOW))
        claimed = self.database.claim_due_alert(NOW, lease_seconds=60)
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(1, claimed.attempts)
        self.database.mark_retry(claimed.id, NOW + 10, "temporary failure")
        self.assertIsNone(self.database.claim_due_alert(NOW + 9, lease_seconds=60))
        retried = self.database.claim_due_alert(NOW + 10, lease_seconds=60)
        self.assertIsNotNone(retried)
        assert retried is not None
        self.assertEqual(2, retried.attempts)
        self.database.mark_delivered(retried.id, NOW + 11)
        self.assertEqual(1, self.database.status()["outbox"]["delivered"])

    def test_expired_sending_lease_is_reclaimed(self) -> None:
        self.database.enqueue_test_alert("signalwatch", NOW)
        first = self.database.claim_due_alert(NOW, lease_seconds=20)
        self.assertIsNotNone(first)
        self.assertIsNone(self.database.claim_due_alert(NOW + 19, lease_seconds=20))
        second = self.database.claim_due_alert(NOW + 20, lease_seconds=20)
        self.assertIsNotNone(second)
        assert second is not None
        self.assertEqual(2, second.attempts)

    def test_source_outage_and_recovery_are_each_queued_once(self) -> None:
        for attempt in range(1, 6):
            queued = self.database.record_source_failure(
                "bloomberg_markets",
                "network unavailable",
                threshold=3,
                default_topic="signalwatch",
                now=NOW + attempt,
            )
            self.assertEqual(attempt == 3, queued)
        report = self.database.record_source_success(
            "bloomberg_markets",
            FeedFetchResult((), None, None, not_modified=True),
            self.rules,
            NOW + 10,
            "signalwatch",
        )
        self.assertTrue(report.recovery_queued)
        self.assertEqual(2, self.database.status()["outbox"]["pending"])


if __name__ == "__main__":
    unittest.main()
