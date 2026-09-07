from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from argus.analysis import AnalysisAttempt, analyze_observation
from argus.database import Database
from argus.models import FeedFetchResult
from argus.rules import RuleSet
from tests.helpers import observation


NOW = 1788652800


class AnalysisPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "state.db")
        self.rules = RuleSet(())
        self.database.record_source_success(
            "bloomberg_markets",
            FeedFetchResult((observation("leased", "Policy update", timestamp=NOW),), None, None),
            self.rules,
            NOW,
            "eos",
        )

    def tearDown(self):
        self.database.close()
        self.temporary.cleanup()

    def test_claim_lease_and_persist_audited_result(self):
        work = self.database.claim_analysis_observations(NOW, limit=10, lease_seconds=300)
        self.assertEqual(1, len(work))
        analysis = analyze_observation(work[0].observation)
        self.database.save_analysis_result(
            work[0].observation_id,
            work[0].lease_token,
            analysis,
            (AnalysisAttempt("deterministic", "succeeded", {"importance": 3}),),
            processing_state="analyzed",
            now=NOW + 1,
        )
        self.assertEqual([], self.database.claim_analysis_observations(
            NOW + 2, limit=10, lease_seconds=300
        ))
        row = self.database.connection.execute(
            "SELECT processing_state, analyzed_at FROM observations"
        ).fetchone()
        self.assertEqual(("analyzed", NOW + 1), tuple(row))
        audit = self.database.connection.execute(
            "SELECT analyzer, outcome, advisory_json FROM analysis_runs"
        ).fetchone()
        self.assertEqual("deterministic", audit["analyzer"])
        self.assertEqual("succeeded", audit["outcome"])
        self.assertIn('"importance": 3', audit["advisory_json"])

    def test_expired_lease_can_be_reclaimed_but_stale_token_cannot_write(self):
        first = self.database.claim_analysis_observations(NOW, limit=1, lease_seconds=10)[0]
        second = self.database.claim_analysis_observations(NOW + 11, limit=1, lease_seconds=10)[0]
        self.assertNotEqual(first.lease_token, second.lease_token)
        with self.assertRaises(RuntimeError):
            self.database.save_analysis_result(
                first.observation_id,
                first.lease_token,
                analyze_observation(first.observation),
                (),
                processing_state="analyzed",
                now=NOW + 12,
            )

    def test_api_budget_is_durable_and_atomic(self):
        self.assertTrue(self.database.reserve_analysis_api_call("2026-09-06", limit=2, now=NOW))
        self.assertTrue(self.database.reserve_analysis_api_call("2026-09-06", limit=2, now=NOW))
        self.assertFalse(self.database.reserve_analysis_api_call("2026-09-06", limit=2, now=NOW))
        calls = self.database.connection.execute(
            "SELECT calls FROM analysis_api_usage WHERE budget_day = '2026-09-06'"
        ).fetchone()[0]
        self.assertEqual(2, calls)

    def test_schema_10_database_migrates_to_11(self):
        # Re-run the migration after downgrading the version marker. The migration
        # is intentionally idempotent because two application units may open it.
        source = self.database.path
        copied = Path(self.temporary.name) / "schema10.db"
        source_connection = sqlite3.connect(source)
        target_connection = sqlite3.connect(copied)
        source_connection.backup(target_connection)
        target_connection.execute("PRAGMA user_version=10")
        target_connection.commit()
        source_connection.close()
        target_connection.close()
        migrated = Database(copied)
        try:
            self.assertEqual(12, migrated.status()["database_schema"])
        finally:
            migrated.close()


if __name__ == "__main__":
    unittest.main()
