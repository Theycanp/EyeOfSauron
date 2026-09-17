from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path
from typing import Any

from argus.database import Database
from argus.event_clustering import event_aggregate_score, event_evidence_score
from argus.events import PersistedEvent, PersistedEventReport
from argus.models import AlertCandidate


class EventPageConsistencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "events.db"
        self.database = Database(self.path)
        self.sequence = 0

    def tearDown(self) -> None:
        self.database.close()
        self.temp.cleanup()

    def add_report(
        self,
        event_key: str,
        published_at: int,
        *,
        database: Database | None = None,
        source_id: str | None = None,
        publisher: str | None = None,
        title: str | None = None,
        importance: int = 3,
        urgency: int = 2,
        relevance: int = 3,
        confidence: float = 0.8,
        region: str = "US",
        topic: str = "policy",
        source_tier: str = "secondary",
    ) -> PersistedEventReport:
        database = database or self.database
        self.sequence += 1
        source_id = source_id or f"source-{self.sequence}"
        publisher = source_id if publisher is None else publisher
        title = title or event_key
        identity = f"report-{self.sequence}"
        with database.unit_of_work():
            cursor = database.connection.execute(
                """INSERT INTO observations(
                    source_id, publisher, dedupe_scope, external_id, published_at,
                    fetched_at, title, summary, url, attributes_json, importance,
                    urgency, relevance, confidence, region, topic, source_tier,
                    information_type, handling, processing_state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?, ?, ?, ?, ?, ?,
                          'report', 'digest', 'new')""",
                (
                    source_id, publisher, source_id, identity, published_at,
                    published_at, title, f"{title} summary", f"https://example.test/{identity}",
                    importance, urgency, relevance, confidence, region, topic, source_tier,
                ),
            )
            observation_id = int(cursor.lastrowid)
        event = PersistedEvent(
            event_key=event_key, fingerprint=event_key, title=title,
            summary=f"{title} summary", score=event_evidence_score(
                importance, urgency, relevance, confidence
            ), importance=importance, urgency=urgency, relevance=relevance,
            confidence=confidence, first_seen_at=published_at, last_seen_at=published_at,
            regions=(region,), topics=(topic,), independent_source_count=1,
            created_at=published_at, updated_at=published_at,
        )
        report = PersistedEventReport(
            event_key=event_key, observation_id=observation_id, source_id=source_id,
            publisher=publisher, source_tier=source_tier, relation="context",
            match_score=1.0, is_representative=True, published_at=published_at,
            title=title, summary=f"{title} summary", url=f"https://example.test/{identity}",
            created_at=published_at,
        )
        return database.save_event_projection(event, report)

    def notify(self, report: PersistedEventReport, created_at: int) -> None:
        candidate = AlertCandidate(
            rule_id="test.notification", dedupe_key=f"notify:{report.observation_id}:{created_at}",
            title="Notification", message="Notification", priority=4, tags=("warning",),
            click_url="", topic="events",
        )
        with self.database.unit_of_work():
            self.database._insert_alert(candidate, report.observation_id, created_at)

    def test_page_queries_share_one_read_snapshot_during_concurrent_projection(self) -> None:
        self.add_report("event", 100)
        writer = Database(self.path)
        committed = False

        def interleave(sql: str) -> None:
            nonlocal committed
            if "source_reports AS" in sql and not committed:
                committed = True
                self.add_report("event", 200, database=writer)

        self.database.connection.set_trace_callback(interleave)
        before = self.database.connection.total_changes
        try:
            page = self.database.list_event_page(since=0, until=1000)
        finally:
            self.database.connection.set_trace_callback(None)
            writer.close()
        self.assertTrue(committed)
        self.assertEqual(before, self.database.connection.total_changes)
        self.assertEqual(1, page.items[0].report_count)
        self.assertEqual(1, len(page.items[0].reports))
        self.assertEqual(2, self.database.list_event_page(since=0, until=1000).items[0].report_count)
        self.assertFalse(self.database.connection.in_transaction)

    def test_page_reuses_an_existing_transaction_without_committing_it(self) -> None:
        self.add_report("event", 100)
        self.database.connection.execute("BEGIN")
        try:
            self.assertEqual(1, len(self.database.list_event_page(since=0, until=1000).items))
            self.assertTrue(self.database.connection.in_transaction)
        finally:
            self.database.connection.rollback()

    def test_future_updates_do_not_move_events_across_latest_cursor(self) -> None:
        for key, published_at in (("A", 300), ("B", 200), ("C", 100)):
            self.add_report(key, published_at)
        first = self.database.list_event_page(since=0, until=1000, limit=1)
        self.add_report("B", 1100, importance=5, source_tier="primary", title="Future B")
        second = self.database.list_event_page(
            since=150, until=1200, limit=10, cursor=first.next_cursor
        )
        self.assertEqual(["A"], [item.event.event_key for item in first.items])
        self.assertEqual(["B", "C"], [item.event.event_key for item in second.items])
        self.assertEqual((0, 1000), (second.window_since, second.window_until))
        self.assertEqual(200, second.items[0].event.last_seen_at)
        self.assertEqual(3, second.items[0].event.importance)

    def test_future_scores_do_not_move_events_across_importance_cursor(self) -> None:
        for key, importance in (("A", 4), ("B", 3), ("C", 2)):
            self.add_report(key, 100, importance=importance)
        first = self.database.list_event_page(since=0, until=1000, sort="importance", limit=1)
        self.add_report("B", 1100, importance=5, urgency=5, relevance=5, confidence=1.0)
        second = self.database.list_event_page(
            since=0, until=1200, sort="importance", limit=10, cursor=first.next_cursor
        )
        self.assertEqual(["B", "C"], [item.event.event_key for item in second.items])
        self.assertEqual(event_aggregate_score(event_evidence_score(3, 2, 3, 0.8), 1), second.items[0].event.score)

    def test_late_arriving_backdated_reports_do_not_change_cursor_membership(self) -> None:
        for key, published_at in (("A", 300), ("B", 200), ("C", 100)):
            self.add_report(key, published_at)
        first = self.database.list_event_page(since=0, until=1000, limit=1)
        self.add_report("B", 400, importance=5, title="Backdated B")
        self.add_report("new", 500)
        second = self.database.list_event_page(
            since=0, until=1000, cursor=first.next_cursor, limit=10
        )
        self.assertEqual(["B", "C"], [item.event.event_key for item in second.items])
        self.assertEqual(200, second.items[0].event.last_seen_at)
        refreshed = self.database.list_event_page(since=0, until=1000)
        self.assertEqual(["new", "B", "A", "C"], [item.event.event_key for item in refreshed.items])

    def test_late_arriving_alerts_do_not_change_cursor_membership(self) -> None:
        for key, published_at in (("A", 1300), ("B", 1200), ("C", 100)):
            report = self.add_report(key, published_at)
        first = self.database.list_event_page(since=1000, until=2000, limit=1)
        self.notify(report, 1500)
        second = self.database.list_event_page(
            since=1000, until=2000, cursor=first.next_cursor, limit=10
        )
        self.assertEqual(["B"], [item.event.event_key for item in second.items])
        refreshed = self.database.list_event_page(since=1000, until=2000)
        self.assertEqual(["A", "B", "C"], [item.event.event_key for item in refreshed.items])

    def test_historical_fields_and_initial_order_ignore_future_reports(self) -> None:
        self.add_report("old", 100, importance=1, urgency=1, relevance=1,
                        confidence=0.2, title="Old title", region="US", topic="policy")
        self.add_report("newer", 200, importance=2)
        self.add_report("old", 2000, importance=5, urgency=5, relevance=5,
                        confidence=1.0, title="Future title", region="CN", topic="technology")
        page = self.database.list_event_page(since=0, until=1000)
        self.assertEqual(["newer", "old"], [item.event.event_key for item in page.items])
        event = page.items[1].event
        self.assertEqual("Old title", event.title)
        self.assertEqual("Old title summary", event.summary)
        self.assertEqual((1, 1, 1, 0.2), (event.importance, event.urgency, event.relevance, event.confidence))
        self.assertEqual(event_aggregate_score(event_evidence_score(1, 1, 1, 0.2), 1), event.score)
        self.assertEqual((100, 100), (event.first_seen_at, event.last_seen_at))
        self.assertEqual(("US",), event.regions)
        self.assertEqual(("policy",), event.topics)
        self.assertEqual(1, event.independent_source_count)
        important = self.database.list_event_page(since=0, until=1000, sort="importance")
        self.assertEqual(["newer", "old"], [item.event.event_key for item in important.items])

    def test_report_display_limit_does_not_change_window_aggregate(self) -> None:
        for timestamp, importance, region in ((100, 5, "US"), (200, 1, "CN"), (300, 1, "JP")):
            self.add_report("event", timestamp, importance=importance, region=region)
        complete = self.database.list_event_page(since=0, until=1000, reports_per_event=100).items[0]
        limited = self.database.list_event_page(since=0, until=1000, reports_per_event=1).items[0]
        self.assertEqual(complete.event, limited.event)
        self.assertEqual(3, limited.report_count)
        self.assertEqual(1, len(limited.reports))
        self.assertTrue(limited.reports_truncated)
        self.assertEqual((100, 300), (limited.event.first_seen_at, limited.event.last_seen_at))
        self.assertEqual(event_aggregate_score(event_evidence_score(5, 2, 3, 0.8), 3), limited.event.score)

    def test_representatives_are_computed_inside_the_frozen_window(self) -> None:
        first = self.add_report("event", 100, source_id="same")
        second = self.add_report("event", 200, source_id="same")
        self.add_report("event", 1100, source_id="same")
        page = self.database.list_event_page(since=0, until=1000, reports_per_event=1)
        self.assertEqual(second.observation_id, page.items[0].reports[0].observation_id)
        self.assertTrue(page.items[0].reports[0].is_representative)
        complete = self.database.list_event_page(since=0, until=1000).items[0]
        self.assertEqual([second.observation_id, first.observation_id], [report.observation_id for report in complete.reports])
        self.assertEqual([True, False], [report.is_representative for report in complete.reports])

    def test_independent_sources_count_publishers_not_feeds(self) -> None:
        self.add_report("event", 100, source_id="markets", publisher="Bloomberg")
        self.add_report("event", 200, source_id="economics", publisher=" bloomberg ")
        self.add_report("event", 300, source_id="other", publisher="")
        item = self.database.list_event_page(since=0, until=1000).items[0]
        self.assertEqual(3, item.report_count)
        self.assertEqual(2, item.event.independent_source_count)
        self.assertEqual(event_aggregate_score(event_evidence_score(3, 2, 3, 0.8), 2), item.event.score)

    def test_delayed_alert_enters_window_once_and_cancelled_alert_does_not(self) -> None:
        delayed = self.add_report("delayed", 100)
        cancelled = self.add_report("cancelled", 200)
        self.notify(delayed, 1100)
        self.notify(delayed, 1200)
        self.notify(cancelled, 1100)
        with self.database.unit_of_work():
            self.database.connection.execute(
                "UPDATE alerts SET status='cancelled' WHERE observation_id=?",
                (cancelled.observation_id,),
            )
        page = self.database.list_event_page(since=1000, until=2000)
        self.assertEqual(["delayed"], [item.event.event_key for item in page.items])
        self.assertEqual(1, page.items[0].report_count)
        self.assertEqual("immediate", page.items[0].handling)

    def test_cursor_rejects_noninteger_out_of_range_and_nonfinite_values(self) -> None:
        valid: dict[str, Any] = {
            "sort": "importance", "since": 0, "until": 1000, "last": 100,
            "report_max": 1, "alert_max": 0,
            "id": 1, "importance": 3, "score": 2.5,
        }
        for overrides in (
            {"since": 0.0}, {"until": True}, {"last": 2**63}, {"id": 0},
            {"id": 1.1}, {"since": 1000}, {"importance": 6},
            {"report_max": False}, {"report_max": 2**63}, {"alert_max": -1},
            {"score": math.inf}, {"score": math.nan}, {"score": True}, {"score": 5.1},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.database.list_event_page(
                    since=0, until=1000, sort="importance",
                    cursor=self.database._event_cursor(valid | overrides),
                )

    def test_shared_score_helpers_preserve_defaults_and_confidence_bounds(self) -> None:
        self.assertEqual(event_evidence_score(), event_evidence_score(True, "bad", None, math.nan))
        self.assertEqual(event_evidence_score(confidence=1), event_evidence_score(confidence=2))
        self.assertEqual(event_evidence_score(confidence=0), event_evidence_score(confidence=-1))
        self.assertEqual(0.75, event_aggregate_score(0.0, 8))


if __name__ == "__main__":
    unittest.main()
