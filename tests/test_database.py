from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from argus.database import Database
from argus.models import AlertCandidate, FeedFetchResult
from argus.rules import RuleSet

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
        incidents = self.database.list_incidents()
        self.assertEqual(1, len(incidents))
        self.assertEqual("event", incidents[0]["kind"])
        self.assertEqual("recorded", incidents[0]["status"])
        self.assertEqual(["bloomberg_markets"], incidents[0]["source_ids"])
        self.assertEqual(0, self.database.status()["incidents"].get("open", 0))

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

    def test_observation_analysis_is_persisted_behind_repository(self) -> None:
        item = observation("primary-policy", "官方紧急公告", timestamp=NOW)
        item = item.__class__(
            **{
                **{field: getattr(item, field) for field in item.__dataclass_fields__},
                "attributes": {
                    "source_tier": "primary",
                    "region": "JP",
                    "importance": 4,
                    "urgency": 4,
                    "topic": "policy",
                },
            }
        )
        self._success("bloomberg_markets", item)
        rows = self.database.list_observations(NOW - 1, NOW + 1, handling="immediate", region="jp")
        self.assertEqual(1, len(rows))
        self.assertEqual("JP", rows[0]["region"])
        self.assertEqual("primary", rows[0]["source_tier"])
        self.assertEqual("policy", rows[0]["topic"])

    def test_prompt_versions_are_stored_and_only_one_is_active(self) -> None:
        self.assertEqual(11, self.database.status()["database_schema"])
        self.assertEqual(1, len(self.database.list_prompts("triage")))
        self.database.save_prompt("triage", 2, "Return JSON only.", "test", NOW)
        self.assertEqual("Return JSON only.", self.database.get_prompt("triage")["system_text"])
        rows = self.database.list_prompts("triage")
        self.assertEqual([2, 1], [row["version"] for row in rows])
        self.assertEqual([2], [row["version"] for row in rows if row["active"]])
        # A failed template is rejected before it can change the active version.
        with self.assertRaises(ValueError):
            self.database.save_prompt("triage", 3, "", "test", NOW)
        self.assertEqual(2, self.database.get_prompt("triage")["version"])

    def test_outbox_lease_retry_and_delivery(self) -> None:
        self.assertTrue(self.database.enqueue_test_alert("eos", NOW))
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
        self.database.enqueue_test_alert("eos", NOW)
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
                default_topic="eos",
                now=NOW + attempt,
            )
            self.assertEqual(attempt == 3, queued)
        incident = self.database.list_incidents()[0]
        self.assertEqual("stateful", incident["kind"])
        self.assertEqual("open", incident["status"])
        self.assertEqual("Argus 数据源异常", incident["latest_title"])
        report = self.database.record_source_success(
            "bloomberg_markets",
            FeedFetchResult((), None, None, not_modified=True),
            self.rules,
            NOW + 10,
            "eos",
        )
        self.assertTrue(report.recovery_queued)
        self.assertEqual(2, self.database.status()["outbox"]["pending"])
        incident = self.database.list_incidents()[0]
        self.assertEqual("recovered", incident["status"])
        self.assertEqual(NOW + 10, incident["recovered_at"])
        self.assertEqual("Argus 数据源已恢复", incident["latest_title"])

    def test_stateful_incident_can_reopen_after_recovery(self) -> None:
        def candidate(dedupe_key: str, recovery: bool = False) -> AlertCandidate:
            return AlertCandidate(
                rule_id="host.health",
                dedupe_key=dedupe_key,
                title="Host recovered" if recovery else "Host unhealthy",
                message="state transition",
                priority=4,
                tags=("warning",),
                click_url="",
                topic="eos",
                incident_key="host:disk:/",
                incident_kind="stateful",
                recovery=recovery,
            )

        with self.database.connection:
            self.assertTrue(self.database._insert_alert(candidate("host-open-1"), None, NOW))
        self.assertEqual("open", self.database.list_incidents()[0]["status"])
        with self.database.connection:
            self.assertTrue(self.database._insert_alert(candidate("host-recovered", True), None, NOW + 1))
        self.assertEqual("recovered", self.database.list_incidents()[0]["status"])
        with self.database.connection:
            self.assertTrue(self.database._insert_alert(candidate("host-open-2"), None, NOW + 1900))
        incident = self.database.list_incidents()[0]
        self.assertEqual("open", incident["status"])
        self.assertIsNone(incident["recovered_at"])

    def test_config_revision_history_and_activation(self) -> None:
        payload = {"sources": [], "rules": []}
        self.database.record_config_revision(1, payload, "tester", "initial")
        self.database.record_config_revision(2, payload, "tester", "change")
        revisions = self.database.list_config_revisions()
        self.assertEqual([2, 1], [item["revision"] for item in revisions])
        self.assertEqual(1, revisions[0]["active"])
        self.assertTrue(self.database.activate_config_revision(1, "tester"))
        self.assertEqual(1, self.database.status()["config_revision"]["revision"])

    def test_prometheus_metrics_include_source_and_incident_counts(self) -> None:
        self._success("bloomberg_markets", observation("baseline", "Ordinary market article"))
        metrics = self.database.metrics_prometheus()
        self.assertIn("argus_observations_total 1", metrics)
        self.assertIn('argus_source_consecutive_failures{source="bloomberg_markets"} 0', metrics)


if __name__ == "__main__":
    unittest.main()
