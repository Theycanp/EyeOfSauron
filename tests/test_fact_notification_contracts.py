from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from argus.database import Database
from argus.event_facts import FACT_EXTRACTOR_VERSION
from argus.event_identity import identify_semantic_event
from argus.models import AlertCandidate

import test_event_page_consistency as fixtures


NOW = 1_790_000_000


class FactNotificationContractTests(unittest.TestCase):
    add_report = fixtures.EventPageConsistencyTests.add_report

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "contracts.db"
        self.database = Database(self.path)
        self.sequence = 0

    def tearDown(self):
        self.database.close()
        self.temp.cleanup()

    def test_delayed_new_report_reusing_old_id_is_found_after_restart(self):
        old = self.add_report("old", NOW - 100)
        version = FACT_EXTRACTOR_VERSION + 1
        self.assertIsNone(self.database.claim_event_fact_job(NOW, version=version))
        with self.database.unit_of_work():
            self.database.connection.execute("DELETE FROM observations WHERE id=?", (old.observation_id,))
        delayed = self.add_report("late", NOW - 200)
        self.assertEqual(old.report_id, delayed.report_id)
        # The projector uses processing time as created_at independently of a
        # report's old publication timestamp. This matters after ID reuse.
        with self.database.unit_of_work():
            self.database.connection.execute("UPDATE event_reports SET created_at=? WHERE id=?", (NOW + 1, delayed.report_id))
        self.database.close()
        self.database = Database(self.path)
        work = self.database.claim_event_fact_job(NOW + 2, version=version)
        self.assertIsNotNone(work)
        self.assertEqual(("late", NOW - 200), (work.event_key, work.published_at))
        self.assertTrue(self.database.complete_event_fact_job(work, (), NOW + 3))
        self.assertIsNone(self.database.claim_event_fact_job(NOW + 4, version=version))

    def candidate(self, identity, key, *, topic="eos", priority=4):
        return AlertCandidate(rule_id="contract", dedupe_key=key, title="Event update",
                              message="Saved evidence", priority=priority, tags=(), click_url="",
                              topic=topic, incident_key=identity)

    def test_notification_contract_keeps_phase_upgrade_and_topic_independent(self):
        announcement = identify_semantic_event("Federal Reserve issues FOMC statement")
        raised = identify_semantic_event("Fed raises interest rates")
        unrelated = identify_semantic_event("European Central Bank raises interest rates")
        self.assertIsNotNone(announcement)
        self.assertIsNotNone(raised)
        self.assertIsNotNone(unrelated)
        announcement_key, raised_key = announcement.notification_key(NOW), raised.notification_key(NOW)
        with self.database.unit_of_work():
            self.assertTrue(self.database._insert_alert(self.candidate(announcement_key, "announcement"), None, NOW))
            # A concrete direction after the generic first announcement is a
            # distinct, useful phase even at the same priority.
            self.assertTrue(self.database._insert_alert(self.candidate(raised_key, "direction"), None, NOW + 1))
            self.assertFalse(self.database._insert_alert(self.candidate(raised_key, "repost"), None, NOW + 2))
            self.assertTrue(self.database._insert_alert(self.candidate(raised_key, "upgrade", priority=5), None, NOW + 3))
            self.assertTrue(self.database._insert_alert(self.candidate(raised_key, "another-topic", topic="other"), None, NOW + 4))
            self.assertTrue(self.database._insert_alert(self.candidate(unrelated.notification_key(NOW), "unrelated"), None, NOW + 5))
        self.assertEqual(5, len(self.database.list_alerts()))

    def test_cancelled_notification_does_not_block_future_support_but_delivered_does(self):
        candidate = self.candidate("news-event:cancelled-contract", "cancelled")
        with self.database.unit_of_work():
            self.assertTrue(self.database._insert_alert(candidate, None, NOW))
            self.database.connection.execute("UPDATE alerts SET status='cancelled' WHERE dedupe_key='cancelled'")
            self.assertTrue(self.database._insert_alert(replace(candidate, dedupe_key="fresh-support"), None, NOW + 1))
            self.database.connection.execute("UPDATE alerts SET status='delivered',delivered_at=? WHERE dedupe_key='fresh-support'", (NOW + 2,))
            self.assertFalse(self.database._insert_alert(replace(candidate, dedupe_key="duplicate-support"), None, NOW + 3))
