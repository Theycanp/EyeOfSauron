from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import test_event_page_consistency as fixtures
from argus.database import Database
from argus.digest import DigestBuilder
from argus.event_clustering import cluster_events, event_match_score
from argus.event_pool import EventPoolProjector
from argus.event_review import event_quality
from argus.events import EVENT_CLOSED_SECONDS, EVENT_QUIET_SECONDS, EventWorkspaceConflict, PersistedEventClaim, event_lifecycle


class EventWorkspaceTests(unittest.TestCase):
    add_report = fixtures.EventPageConsistencyTests.add_report

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "events.db"
        self.database = Database(self.path)
        self.sequence = 0

    def tearDown(self) -> None:
        self.database.close()
        self.temp.cleanup()

    def apply(self, preview):
        return self.database.apply_event_repair(preview["action"], preview["event_keys"],
                                               observation_ids=preview["observation_ids"], expected_revision=preview["revision"],
                                               actor="operator", reason="Verified source evidence", now=2000)

    def save_digest(self):
        draft = DigestBuilder(self.database).build(digest_key="daily:test", period_start=0,
                                                  period_end=1500, timezone="UTC", created_at=1500)
        stored = self.database.save_digest(draft)
        return self.database.publish_digest(stored.digest_key, stored.version, 1500)

    def test_merge_preserves_published_digest_reports_aliases_and_observations(self):
        self.add_report("A", 100, source_tier="primary", title="NASA launches lunar satellite")
        self.add_report("B", 110, title="Lunar satellite launches successfully")
        digest = self.save_digest()
        observations = [tuple(row) for row in self.database.connection.execute("SELECT * FROM observations ORDER BY id")]
        preview = self.database.preview_event_repair("merge", ["A", "B"])
        self.assertEqual("A", self.apply(preview))
        self.assertEqual(digest, self.database.get_digest(digest.digest_key, digest.version))
        self.assertEqual(observations, [tuple(row) for row in self.database.connection.execute("SELECT * FROM observations ORDER BY id")])
        self.assertEqual("A", self.database.canonical_event_key("B"))
        self.assertEqual(2, len(self.database.list_event_reports("A")))
        self.assertEqual("closed", self.database.get_event("B").status)
        self.assertEqual(["A"], [item.event_key for item in self.database.list_event_candidates(0, 2000)])
        self.assertEqual("merge", self.database.list_event_audit("B")[0]["action"])

    def test_split_preserves_digest_snapshots_and_shrinks_remaining_time_range(self):
        first = self.add_report("A", 100, title="NASA launches lunar satellite")
        second = self.add_report("A", 1000, title="Flood forces evacuation in Tokyo", region="JP")
        digest = self.save_digest()
        preview = self.database.preview_event_repair("split", ["A"], observation_ids=[second.observation_id])
        target = self.apply(preview)
        self.assertNotEqual("A", target)
        self.assertEqual([first.observation_id], [report.observation_id for report in self.database.list_event_reports("A")])
        self.assertEqual(100, self.database.get_event("A").last_seen_at)
        self.assertEqual(("JP",), self.database.get_event(target).regions)
        self.assertEqual(digest, self.database.get_digest(digest.digest_key, digest.version))
        self.assertEqual("split", self.database.list_event_audit(target)[0]["action"])

    def test_concurrent_new_evidence_invalidates_preview_without_partial_mutation(self):
        self.add_report("A", 100)
        self.add_report("B", 110)
        preview = self.database.preview_event_repair("merge", ["A", "B"])
        self.add_report("B", 120)
        with self.assertRaises(EventWorkspaceConflict):
            self.apply(preview)
        self.assertEqual(2, len(self.database.list_event_reports("B")))
        self.assertEqual([], self.database.list_event_audit("A"))

    def test_repair_failure_rolls_back_moves_snapshots_and_aliases(self):
        self.add_report("A", 100)
        self.add_report("B", 110)
        digest = self.save_digest()
        preview = self.database.preview_event_repair("merge", ["A", "B"])
        with patch("argus.sqlite_event_workspace.SQLiteEventWorkspace._audit", side_effect=RuntimeError("disk full")):
            with self.assertRaisesRegex(RuntimeError, "disk full"):
                self.apply(preview)
        self.assertEqual(1, len(self.database.list_event_reports("B")))
        self.assertEqual("B", self.database.canonical_event_key("B"))
        self.assertEqual(digest, self.database.get_digest(digest.digest_key, digest.version))
        self.assertEqual(0, self.database.connection.execute("SELECT COUNT(*) FROM digest_items WHERE event_reports_json IS NOT NULL").fetchone()[0])

    def test_split_rejects_all_evidence_and_claims_require_specialized_repair(self):
        report = self.add_report("A", 100)
        self.add_report("B", 110)
        with self.assertRaisesRegex(ValueError, "some, but not all"):
            self.database.preview_event_repair("split", ["A"], observation_ids=[report.observation_id])
        self.database.save_event_claim(PersistedEventClaim(event_key="A", claim_key="fact", text="verified fact", status="active", confidence=1, first_seen_at=100, last_seen_at=100))
        with self.assertRaisesRegex(ValueError, "claims"):
            self.database.preview_event_repair("merge", ["A", "B"])

    def test_cursor_is_rejected_after_membership_changes(self):
        self.add_report("A", 100)
        self.add_report("B", 110)
        page = self.database.list_event_page(since=0, until=1500, limit=1)
        self.apply(self.database.preview_event_repair("merge", ["A", "B"]))
        with self.assertRaisesRegex(ValueError, "刷新"):
            self.database.list_event_page(since=0, until=1500, cursor=page.next_cursor)

    def test_personal_preferences_do_not_affect_global_digest_choices(self):
        self.add_report("A", 100)
        self.database.set_event_preference("A", "reader", read=True, followed=False, ignored=True, now=200)
        self.assertTrue(self.database.event_workspace_state(["A"], "reader")["A"]["ignored"])
        self.assertFalse(self.database.event_workspace_state(["A"], "other")["A"]["ignored"])
        self.assertEqual({}, self.database.get_event_digest_choices(["A"]))
        self.database.set_event_digest_choice("A", "exclude", actor="operator", reason="Unrelated report", now=210)
        self.assertEqual({"A": "exclude"}, self.database.get_event_digest_choices(["A"]))
        self.database.set_event_digest_choice("A", "auto", actor="operator", reason="Restored", now=220)
        self.assertEqual({}, self.database.get_event_digest_choices(["A"]))
        self.assertEqual(2, len(self.database.list_event_audit("A")))

    def test_editorial_conflict_blocks_merge_and_preferences_follow_merged_event(self):
        self.add_report("A", 100)
        self.add_report("B", 110)
        self.database.set_event_digest_choice("A", "include", actor="operator", reason="Essential", now=200)
        self.database.set_event_digest_choice("B", "exclude", actor="operator", reason="Unrelated", now=200)
        with self.assertRaisesRegex(ValueError, "conflicting"):
            self.database.preview_event_repair("merge", ["A", "B"])
        self.database.set_event_digest_choice("B", "auto", actor="operator", reason="Reconsider", now=200)
        self.database.set_event_preference("B", "reader", read=True, followed=True, ignored=False, now=200)
        target = self.apply(self.database.preview_event_repair("merge", ["A", "B"]))
        state = self.database.event_workspace_state([target], "reader")[target]
        self.assertTrue(state["followed"])
        self.assertFalse(state["read"])

    def test_lifecycle_uses_publication_time_and_is_bounded(self):
        self.add_report("A", 100)
        self.add_report("B", 110)
        self.assertEqual(1, self.database.maintain_event_lifecycle(now=EVENT_QUIET_SECONDS+200, limit=1))
        self.assertEqual("quiet", self.database.get_event("A").status)
        self.assertEqual("active", self.database.get_event("B").status)
        self.database.maintain_event_lifecycle(now=EVENT_CLOSED_SECONDS+200)
        self.assertTrue(all(event.status == "closed" for event in self.database.list_events()))
        self.assertEqual("active", event_lifecycle(100, 100+EVENT_QUIET_SECONDS-1))

    def test_backfilled_generic_titles_remain_separate_and_inactive(self):
        for key in ("A", "B"):
            report = self.add_report(key, 100, title="Politics")
            with self.database.unit_of_work():
                self.database.connection.execute("DELETE FROM event_reports WHERE id=?", (report.report_id,))
                self.database.connection.execute("DELETE FROM events WHERE event_key=?", (key,))
        EventPoolProjector(self.database).project_pending(now=EVENT_CLOSED_SECONDS+200)
        events = self.database.list_events()
        self.assertEqual(2, len(events))
        self.assertTrue(all(event.status == "closed" for event in events))
        self.assertEqual(2, len(cluster_events([{"id": 1, "title": "Politics", "published_at": 100}, {"id": 2, "title": "Politics", "published_at": 110}])))

    def test_late_fresh_compatible_evidence_reactivates_quiet_event(self):
        self.add_report("A", 100, title="NASA launches lunar satellite")
        self.database.maintain_event_lifecycle(now=100+EVENT_QUIET_SECONDS)
        report = self.add_report("B", 100+EVENT_QUIET_SECONDS, title="NASA launches lunar satellite")
        with self.database.unit_of_work():
            self.database.connection.execute("DELETE FROM event_reports WHERE id=?", (report.report_id,))
            self.database.connection.execute("DELETE FROM events WHERE event_key='B'")
        EventPoolProjector(self.database).project_pending(now=200+EVENT_QUIET_SECONDS)
        self.assertEqual("active", self.database.get_event("A").status)
        self.assertEqual(2, len(self.database.list_event_reports("A")))

    def test_time_window_is_a_hard_boundary_and_quality_is_not_accuracy(self):
        left = {"title": "NASA launches lunar satellite", "published_at": 100}
        right = {**left, "published_at": 100+8*86400}
        self.assertEqual(0, event_match_score(left, right))
        self.assertEqual(2, len(cluster_events([left, right])))
        self.add_report("A", 100, publisher="publisher")
        self.add_report("B", 110, publisher="publisher")
        result = event_quality(self.database, since=0, until=1500)
        self.assertEqual(2, result["single_publisher_events"])
        self.assertIn("不代表聚类错误率", result["interpretation"])

    def test_stale_projector_cannot_resurrect_merged_alias(self):
        self.add_report("A", 100, source_tier="primary")
        self.add_report("B", 110)
        stale = self.database.get_event("B")
        spare = self.add_report("C", 120)
        with self.database.unit_of_work():
            self.database.connection.execute("DELETE FROM event_reports WHERE id=?", (spare.report_id,))
        self.apply(self.database.preview_event_repair("merge", ["A", "B"]))
        with self.assertRaisesRegex(ValueError, "merged during projection"):
            self.database.save_event_projection(stale, replace(spare, event_key="B", report_id=None))
        self.assertEqual([], self.database.list_event_reports("B"))
