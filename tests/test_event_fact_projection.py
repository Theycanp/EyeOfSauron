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

    def test_fact_diagnostics_explains_empty_completion_and_retries_dead_jobs_by_version(self) -> None:
        report = self.add_report("diagnostic", 1_790_000_000, title="No deterministic fact here")
        work = self.database.claim_event_fact_job(1_790_000_100, version=FACT_EXTRACTOR_VERSION)
        assert work is not None
        self.assertTrue(self.database.complete_event_fact_job(work, (), 1_790_000_101))
        diagnostics = self.database.event_fact_diagnostics(version=FACT_EXTRACTOR_VERSION, now=1_790_000_102)
        self.assertEqual(1, diagnostics["completed_without_claim"])
        self.assertEqual(1, diagnostics["completed_count"])
        self.assertEqual(0, diagnostics["completed_with_claim"])
        self.assertEqual(FACT_EXTRACTOR_VERSION, diagnostics["active_worker_version"])
        self.assertTrue(diagnostics["selected_version_processable"])
        self.assertEqual([FACT_EXTRACTOR_VERSION], [row["extractor_version"] for row in diagnostics["versions"]])
        self.assertIn("not a quality verdict", diagnostics["explanation"])
        self.database.connection.execute(
            "UPDATE event_fact_jobs SET status='dead',attempts=5,updated_at=? "
            "WHERE report_id=? AND extractor_version=?",
            (1_790_000_103, report.report_id, FACT_EXTRACTOR_VERSION),
        )
        self.database.connection.commit()
        retried = self.database.retry_event_fact_jobs(
            version=FACT_EXTRACTOR_VERSION, limit=1, actor="operator", now=1_790_000_104,
        )
        self.assertEqual(1, retried)
        row = self.database.connection.execute(
            "SELECT status,attempts,next_attempt_at FROM event_fact_jobs WHERE report_id=?",
            (report.report_id,),
        ).fetchone()
        self.assertEqual("pending", row["status"])
        self.assertEqual(0, row["attempts"])
        self.assertEqual(1_790_000_104, row["next_attempt_at"])
        audit = self.database.connection.execute(
            "SELECT action,actor FROM admin_auth_audit WHERE action='event_fact_retry'"
        ).fetchone()
        self.assertEqual(("event_fact_retry", "operator"), (audit["action"], audit["actor"]))

    def test_new_extractor_does_not_reprocess_history_without_bounded_backfill(self) -> None:
        old = self.add_report("old", 1_790_000_000, title="Old report")
        next_version = FACT_EXTRACTOR_VERSION + 1
        self.assertIsNone(self.database.claim_event_fact_job(1_790_000_100, version=next_version))
        fresh = self.add_report("new", 1_790_000_200, title="New report")
        work = self.database.claim_event_fact_job(1_790_000_201, version=next_version)
        assert work is not None
        self.assertEqual(fresh.report_id, work.report_id)
        with patch("argus.event_fact_projection.FACT_EXTRACTOR_VERSION", next_version):
            self.assertEqual(1, self.database.backfill_event_fact_jobs(
                version=next_version, limit=1, actor="operator", now=1_790_000_202,
            ))
        backfilled = self.database.claim_event_fact_job(1_790_000_203, version=next_version)
        assert backfilled is not None
        self.assertEqual(old.report_id, backfilled.report_id)
        with patch("argus.event_fact_projection.FACT_EXTRACTOR_VERSION", next_version):
            self.assertEqual(0, self.database.backfill_event_fact_jobs(
                version=next_version, limit=1, actor="operator", now=1_790_000_204,
            ))

    def test_fact_admin_rejects_inert_versions_without_enqueuing_or_auditing(self) -> None:
        self.add_report("old", 1_790_000_000, title="Old report")
        for operation in (self.database.backfill_event_fact_jobs, self.database.retry_event_fact_jobs):
            for version in (0, FACT_EXTRACTOR_VERSION + 1, True):
                with self.subTest(operation=operation.__name__, version=version):
                    with self.assertRaises(ValueError):
                        operation(version=version, limit=1, actor="operator", now=1_790_000_100)
        self.assertEqual(0, self.database.connection.execute("SELECT COUNT(*) FROM event_fact_jobs").fetchone()[0])
        self.assertEqual(0, self.database.connection.execute(
            "SELECT COUNT(*) FROM admin_auth_audit WHERE action IN ('event_fact_retry','event_fact_backfill')"
        ).fetchone()[0])
        diagnostics = self.database.event_fact_diagnostics(version=FACT_EXTRACTOR_VERSION + 1, now=1_790_000_101)
        self.assertFalse(diagnostics["selected_version_processable"])
        self.assertEqual([], diagnostics["versions"])

    def test_completed_denominator_and_claim_match_are_version_scoped(self) -> None:
        self.add_report("fact", 1_790_000_000, title="Fed raises interest rates by 25 bps")
        self.assertTrue(self.projector.process_once(1_790_000_100))
        report = self.database.connection.execute("SELECT report_id FROM event_fact_jobs").fetchone()[0]
        next_version = FACT_EXTRACTOR_VERSION + 1
        with self.database.unit_of_work():
            self.database.connection.execute(
                "INSERT INTO event_fact_versions VALUES(?,0,?)", (next_version, 1_790_000_101),
            )
            self.database.connection.execute(
                "INSERT INTO event_fact_jobs(report_id,extractor_version,status,updated_at) VALUES(?,?,'completed',?)",
                (report, next_version, 1_790_000_101),
            )
        current = self.database.event_fact_diagnostics(version=FACT_EXTRACTOR_VERSION)
        future = self.database.event_fact_diagnostics(version=next_version)
        self.assertEqual((1, 1, 0), (current["completed_count"], current["completed_with_claim"], current["completed_without_claim"]))
        self.assertEqual((1, 0, 1), (future["completed_count"], future["completed_with_claim"], future["completed_without_claim"]))
        self.assertEqual([next_version], [row["extractor_version"] for row in future["versions"]])

    def test_new_report_is_enqueued_when_retention_reuses_history_report_id(self) -> None:
        old = self.add_report("old", 1_790_000_000)
        next_version = FACT_EXTRACTOR_VERSION + 1
        self.assertIsNone(self.database.claim_event_fact_job(1_790_000_100, version=next_version))
        with self.database.unit_of_work():
            self.database.connection.execute("DELETE FROM observations WHERE id=?", (old.observation_id,))
        fresh = self.add_report("new", 1_790_000_200)
        self.assertEqual(old.report_id, fresh.report_id)
        work = self.database.claim_event_fact_job(1_790_000_201, version=next_version)
        assert work is not None
        self.assertEqual("new", work.event_key)

    def test_same_second_report_projection_is_included_without_history_backfill(self) -> None:
        older = self.add_report("older", 1_790_000_000)
        overlap = self.add_report("overlap", 1_790_000_100)
        next_version = FACT_EXTRACTOR_VERSION + 1
        work = self.database.claim_event_fact_job(1_790_000_100, version=next_version)
        assert work is not None
        self.assertEqual(overlap.report_id, work.report_id)
        self.assertNotEqual(older.report_id, work.report_id)
        self.assertTrue(self.database.complete_event_fact_job(work, (), 1_790_000_101))
        self.assertIsNone(self.database.claim_event_fact_job(1_790_000_102, version=next_version))

    def test_diagnostic_counts_use_one_snapshot_during_concurrent_completion(self) -> None:
        self.add_report("diagnostic", 1_790_000_000)
        work = self.database.claim_event_fact_job(1_790_000_100, version=FACT_EXTRACTOR_VERSION)
        assert work is not None
        writer = Database(Path(self.temp.name) / "facts.db")
        completed = False
        def interleave(sql: str) -> None:
            nonlocal completed
            if "SELECT COUNT(*) FROM event_fact_jobs j" in sql and not completed:
                completed = True
                writer.complete_event_fact_job(work, (), 1_790_000_101)
        self.database.connection.set_trace_callback(interleave)
        try:
            diagnostics = self.database.event_fact_diagnostics(version=FACT_EXTRACTOR_VERSION)
        finally:
            self.database.connection.set_trace_callback(None)
            writer.close()
        self.assertTrue(completed)
        self.assertEqual((0, 0, 0), (diagnostics["completed_count"], diagnostics["completed_with_claim"], diagnostics["completed_without_claim"]))
        refreshed = self.database.event_fact_diagnostics(version=FACT_EXTRACTOR_VERSION)
        self.assertEqual((1, 0, 1), (refreshed["completed_count"], refreshed["completed_with_claim"], refreshed["completed_without_claim"]))


if __name__ == "__main__":
    unittest.main()
