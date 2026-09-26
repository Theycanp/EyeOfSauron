from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from argus.database import Database, SCHEMA_VERSION
from argus.event_fact_projection import EventFactProjector, SQLiteEventFacts
from argus.event_facts import FACT_EXTRACTOR_VERSION, FACT_PRODUCER, extract_event_facts

import test_event_page_consistency as fixtures


class EventFactProjectionTests(unittest.TestCase):
    add_report = fixtures.EventPageConsistencyTests.add_report

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "facts.db")
        self.sequence = 0
        self.projector = EventFactProjector(self.database)

    def tearDown(self) -> None:
        self.database.close()
        self.temp.cleanup()

    def test_extracts_durable_claim_without_inventing_event_time(self) -> None:
        report = self.add_report(
            "decision", 1_790_000_000, title="Fed raises interest rates by 25 bps",
            source_tier="primary",
        )
        self.assertTrue(self.projector.process_once(1_790_000_100))
        graph = self.database.read_event_evidence("decision")
        assert graph is not None
        self.assertEqual(1, len(graph.claims))
        self.assertEqual(FACT_PRODUCER, graph.claims[0].producer)
        self.assertEqual(FACT_EXTRACTOR_VERSION, graph.claims[0].extractor_version)
        self.assertIsNone(graph.claims[0].fact["effective_at"] if graph.claims[0].fact else None)
        self.assertEqual(report.report_id, graph.evidence[0].report_id)
        self.assertEqual(FACT_PRODUCER, graph.evidence[0].producer)
        self.assertEqual((), graph.timeline)
        self.assertFalse(self.projector.process_once(1_790_000_101))

    def test_same_fact_supports_once_and_different_value_is_disputed(self) -> None:
        self.add_report("decision", 1_790_000_000, title="Fed raises interest rates by 25 bps")
        self.add_report("decision", 1_790_000_100, title="Fed raises interest rates by 25 bps")
        self.assertTrue(self.projector.process_once(1_790_000_200))
        self.assertTrue(self.projector.process_once(1_790_000_201))
        graph = self.database.read_event_evidence("decision")
        assert graph is not None
        self.assertEqual(1, len(graph.claims))
        self.assertEqual(2, len(graph.evidence))
        self.add_report("decision", 1_790_000_300, title="Fed raises interest rates by 50 bps")
        self.assertTrue(self.projector.process_once(1_790_000_400))
        graph = self.database.read_event_evidence("decision")
        assert graph is not None
        self.assertEqual({"disputed"}, {claim.status for claim in graph.claims})
        self.assertEqual(2, len(graph.claims))

    def test_expired_lease_rejects_old_worker_and_retries(self) -> None:
        self.add_report("decision", 1_790_000_000, title="Fed raises interest rates by 25 bps")
        old = self.database.claim_event_fact_job(1_790_000_100, version=FACT_EXTRACTOR_VERSION, lease_seconds=30)
        assert old is not None
        new = self.database.claim_event_fact_job(1_790_000_131, version=FACT_EXTRACTOR_VERSION, lease_seconds=30)
        assert new is not None
        facts = extract_event_facts(old.extraction_input())
        self.assertFalse(self.database.complete_event_fact_job(old, facts, 1_790_000_132))
        self.assertTrue(self.database.complete_event_fact_job(new, facts, 1_790_000_132))
        self.assertEqual(1, len(self.database.list_event_claims("decision")))

    def test_apply_failure_rolls_back_claim_and_job_completion(self) -> None:
        self.add_report("decision", 1_790_000_000, title="Fed raises interest rates by 25 bps")
        work = self.database.claim_event_fact_job(1_790_000_100, version=FACT_EXTRACTOR_VERSION)
        assert work is not None
        facts = extract_event_facts(work.extraction_input())
        with patch.object(SQLiteEventFacts, "_reconcile_slot", side_effect=RuntimeError("injected failure")):
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                self.database.complete_event_fact_job(work, facts, 1_790_000_101)
        self.assertEqual([], self.database.list_event_claims("decision"))
        self.assertEqual("leased", self.database.connection.execute(
            "SELECT status FROM event_fact_jobs WHERE report_id=?", (work.report_id,)
        ).fetchone()[0])
        self.assertTrue(self.database.fail_event_fact_job(work, "injected failure", 1_790_000_101))

    def test_schema_twenty_upgrade_creates_fact_and_occurrence_indexes(self) -> None:
        path = Path(self.temp.name) / "facts.db"
        self.database.close()
        connection = sqlite3.connect(path)
        connection.execute("DROP TABLE event_report_occurrences")
        connection.execute("DROP TABLE event_occurrences")
        connection.execute("DROP TABLE event_fact_jobs")
        connection.execute("DROP INDEX event_timeline_key_idx")
        connection.execute("DROP INDEX event_claims_slot_idx")
        connection.execute("PRAGMA user_version=20")
        connection.commit()
        connection.close()
        self.database = Database(path)
        self.assertEqual(SCHEMA_VERSION, self.database.connection.execute("PRAGMA user_version").fetchone()[0])
        for table in ("event_fact_jobs", "event_occurrences", "event_report_occurrences"):
            self.assertIsNotNone(self.database.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone())

    def test_manual_merge_rehomes_automatic_fact_and_keeps_old_container(self) -> None:
        self.add_report("A", 1_790_000_000, title="Fed raises interest rates by 25 bps", source_tier="primary")
        moved = self.add_report("B", 1_790_000_100, title="Fed raises interest rates by 25 bps")
        self.assertTrue(self.projector.process_once(1_790_000_200))
        self.assertTrue(self.projector.process_once(1_790_000_201))
        self.assertEqual(1, len(self.database.list_event_claims("B")))
        preview = self.database.preview_event_repair("merge", ["A", "B"])
        target = self.database.apply_event_repair(
            "merge", ["A", "B"], observation_ids=[], expected_revision=preview["revision"],
            actor="operator", reason="same official decision", now=1_790_000_300,
        )
        self.assertEqual("A", target)
        self.assertEqual([], self.database.list_event_claims("B"))
        self.assertEqual(1, len(self.database.list_event_claims("A")))
        self.assertEqual("A", self.database.canonical_event_key("B"))
        self.assertEqual("pending", self.database.connection.execute(
            "SELECT status FROM event_fact_jobs WHERE report_id=?", (moved.report_id,)
        ).fetchone()[0])
        self.assertTrue(self.projector.process_once(1_790_000_301))
        self.assertEqual(2, len(self.database.list_claim_evidence(
            self.database.list_event_claims("A")[0].claim_key, event_key="A"
        )))


if __name__ == "__main__":
    unittest.main()
