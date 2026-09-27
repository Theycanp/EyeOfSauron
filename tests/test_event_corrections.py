from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from argus.database import Database
from argus.event_fact_projection import EventFactProjector
from argus.events import EventWorkspaceConflict
from argus.sqlite_event_corrections import SQLiteEventCorrections

import test_event_page_consistency as fixtures


class EventCorrectionTests(unittest.TestCase):
    add_report = fixtures.EventPageConsistencyTests.add_report

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "corrections.db")
        self.sequence = 0
        self.adapter = SQLiteEventCorrections(self.database)
        self.projector = EventFactProjector(self.database)

    def tearDown(self) -> None:
        self.database.close()
        self.temp.cleanup()

    def pair(self, *, new_tier="primary"):
        self.add_report("decision", 1_790_000_000, title="Fed raises interest rates by 25 bps", source_tier="primary")
        new_report = self.add_report("decision", 1_790_000_100, title="Fed raises interest rates by 50 bps", source_tier=new_tier)
        self.projector.process_once(1_790_000_200)
        self.projector.process_once(1_790_000_201)
        claims = self.database.list_event_claims("decision")
        old = next(claim for claim in claims if "25" in claim.text)
        new = next(claim for claim in claims if "50" in claim.text)
        return old.claim_key, new.claim_key, new_report.report_id

    def apply(self, old, new, report, revision):
        return self.adapter.apply("decision", old, new, report, expected_revision=revision,
                                  actor="operator", reason="Read the explicit official correction",
                                  topic="test-topic", now=1_790_000_300)

    def test_explicit_primary_correction_preserves_old_proof_and_queues_once(self):
        old, new, report = self.pair()
        preview = self.adapter.preview("decision", old, new, report)
        result = self.apply(old, new, report, preview["revision"])
        self.assertEqual({"applied": True, "notification_queued": True, "event_key": "decision"}, result)
        claims = {claim.claim_key: claim for claim in self.database.list_event_claims("decision")}
        self.assertEqual("superseded", claims[old].status)
        self.assertEqual("active", claims[new].status)
        self.assertEqual(old, claims[new].supersedes_claim_key)
        self.assertEqual(1, len(self.database.list_claim_evidence(old, event_key="decision")))
        self.assertFalse(self.apply(old, new, report, preview["revision"])["applied"])
        self.assertEqual(1, self.database.connection.execute("SELECT COUNT(*) FROM alerts").fetchone()[0])
        self.assertEqual("fact_correction", self.database.list_event_audit("decision")[0]["action"])
        self.add_report("decision", 1_790_000_400, title="Fed raises interest rates by 50 bps", source_tier="secondary")
        self.projector.process_once(1_790_000_500)
        claims = {claim.claim_key: claim for claim in self.database.list_event_claims("decision")}
        self.assertEqual("active", claims[new].status)
        self.assertEqual("superseded", claims[old].status)
        self.add_report("decision", 1_790_000_600, title="Fed raises interest rates by 25 bps", source_tier="secondary")
        self.projector.process_once(1_790_000_700)
        claims = {claim.claim_key: claim for claim in self.database.list_event_claims("decision")}
        self.assertEqual("active", claims[new].status)
        self.assertEqual("superseded", claims[old].status)

    def test_secondary_proof_cannot_overwrite_primary(self):
        old, new, report = self.pair(new_tier="secondary")
        with self.assertRaisesRegex(ValueError, "primary report"):
            self.adapter.preview("decision", old, new, report)
        self.assertEqual({"disputed"}, {claim.status for claim in self.database.list_event_claims("decision")})
        self.assertEqual([], self.database.list_alerts())

    def test_stale_preview_and_outbox_failure_leave_claims_unchanged(self):
        old, new, report = self.pair()
        preview = self.adapter.preview("decision", old, new, report)
        with self.assertRaises(EventWorkspaceConflict):
            self.apply(old, new, report, "0" * 64)
        with patch.object(self.database, "_insert_alert", side_effect=RuntimeError("injected outbox failure")):
            with self.assertRaisesRegex(RuntimeError, "outbox"):
                self.apply(old, new, report, preview["revision"])
        self.assertEqual({"disputed"}, {claim.status for claim in self.database.list_event_claims("decision")})
        self.assertEqual(0, self.database.connection.execute("SELECT COUNT(*) FROM event_fact_corrections").fetchone()[0])
        self.assertEqual([], self.database.list_event_audit("decision"))
        self.assertEqual([], self.database.list_alerts())
