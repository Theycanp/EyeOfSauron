from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from argus.database import Database, SCHEMA_VERSION
from argus.events import (
    PersistedEvent,
    PersistedEventClaim,
    PersistedEventClaimEvidence,
    PersistedEventReport,
    PersistedEventTimelineItem,
)
from argus.models import FeedFetchResult
from argus.digest import DigestBuilder
from argus.event_pool import EventPoolProjector
from argus.manual_events import ManualEventSpec
from argus.rules import RuleSet

from helpers import observation, production_config


class EventPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = production_config(self.root)
        self.database = Database(self.config.service.database_path)
        self.rules = RuleSet.from_config(self.config.rules, self.config.ntfy.default_topic)
        self.database.record_source_success(
            "source-a",
            FeedFetchResult((observation("obs-1", "Official event", source_id="source-a"),), None, None),
            self.rules,
            100,
            self.config.ntfy.default_topic,
        )

    def tearDown(self) -> None:
        self.database.close()
        self.temp.cleanup()

    def test_event_report_claim_and_timeline_round_trip(self) -> None:
        event = self.database.save_event(PersistedEvent(
            event_key="event-1", fingerprint="fp-1", title="An event", summary="Summary",
            score=4.2, importance=4, urgency=3, relevance=4, confidence=0.8,
            first_seen_at=100, last_seen_at=100, regions=("EAST_ASIA",), topics=("policy",),
            created_at=100, updated_at=100,
        ))
        self.assertEqual("event-1", event.event_key)
        report = self.database.save_event_report(PersistedEventReport(
            event_key="event-1", observation_id=1, source_id="source-a", source_tier="primary",
            publisher="Source A",
            relation="primary", match_score=1.0, is_representative=True, published_at=100,
            title="Official event", summary="Summary", url="https://example.test/1", created_at=100,
        ))
        self.assertIsNotNone(report.report_id)
        claim = self.database.save_event_claim(PersistedEventClaim(
            event_key="event-1", claim_key="claim-1", text="The event happened", status="active",
            confidence=0.9, first_seen_at=100, last_seen_at=100, created_at=100, updated_at=100,
        ))
        evidence = self.database.save_claim_evidence(PersistedEventClaimEvidence(
            claim_key="claim-1", report_id=report.report_id or 0, stance="supports", created_at=100,
        ))
        timeline = self.database.save_event_timeline(PersistedEventTimelineItem(
            event_key="event-1", occurred_at=100, kind="reported", text="Initial report",
            confidence=0.8, report_id=report.report_id, created_at=100,
        ))
        self.assertEqual("event-1", self.database.get_event("event-1").event_key)  # type: ignore[union-attr]
        self.assertEqual([1], [item.observation_id for item in self.database.list_event_reports("event-1")])
        self.assertEqual([claim.claim_key], [item.claim_key for item in self.database.list_event_claims("event-1")])
        self.assertEqual([evidence.stance], [item.stance for item in self.database.list_claim_evidence("claim-1")])
        self.assertEqual([timeline.text], [item.text for item in self.database.list_event_timeline("event-1")])

    def _event_report(self, event_key: str, source_id: str, observation_id: int) -> PersistedEventReport:
        self.database.save_event(PersistedEvent(
            event_key=event_key, fingerprint=event_key, title="An event", summary="Summary",
            score=4.2, importance=4, urgency=3, relevance=4, confidence=0.8,
            first_seen_at=100, last_seen_at=100,
        ))
        return self.database.save_event_report(PersistedEventReport(
            event_key=event_key, observation_id=observation_id, source_id=source_id,
            publisher=source_id, source_tier="primary", relation="primary",
            match_score=1.0, is_representative=True, published_at=100,
            title="An event", summary="Summary", url="https://example.test/report",
        ))

    def _second_event_report(self) -> PersistedEventReport:
        self.database.record_source_success(
            "source-b", FeedFetchResult((observation("obs-2", "Another event", source_id="source-b"),), None, None),
            self.rules, 100, self.config.ntfy.default_topic,
        )
        return self._event_report("event-2", "source-b", 2)

    def test_claim_evidence_resolves_same_event_and_rejects_cross_event_report(self) -> None:
        first_report = self._event_report("event-1", "source-a", 1)
        second_report = self._second_event_report()
        first_claim = self.database.save_event_claim(PersistedEventClaim(
            event_key="event-1", claim_key="shared", text="First claim", status="active",
            confidence=0.8, first_seen_at=100, last_seen_at=100,
        ))
        with self.assertRaisesRegex(ValueError, "same event"):
            self.database.save_claim_evidence(PersistedEventClaimEvidence(
                claim_key="shared", report_id=second_report.report_id or 0, stance="supports",
            ))
        second_claim = self.database.save_event_claim(PersistedEventClaim(
            event_key="event-2", claim_key="shared", text="Second claim", status="active",
            confidence=0.8, first_seen_at=100, last_seen_at=100,
        ))
        first = self.database.save_claim_evidence(PersistedEventClaimEvidence(
            claim_key="shared", report_id=first_report.report_id or 0, stance="supports",
        ))
        second = self.database.save_claim_evidence(PersistedEventClaimEvidence(
            claim_key="shared", report_id=second_report.report_id or 0, stance="refutes",
        ))
        self.assertEqual(
            [first_claim.claim_id, second_claim.claim_id],
            [row["claim_id"] for row in self.database.connection.execute(
                "SELECT claim_id FROM event_claim_evidence ORDER BY id"
            )],
        )
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.database.list_claim_evidence("shared")
        self.assertEqual([first.evidence_id], [item.evidence_id for item in self.database.list_claim_evidence(
            "shared", event_key="event-1"
        )])
        self.assertEqual([second.evidence_id], [item.evidence_id for item in self.database.list_claim_evidence(
            "shared", event_key="event-2"
        )])

    def test_timeline_report_must_belong_to_event(self) -> None:
        self._event_report("event-1", "source-a", 1)
        other_report = self._second_event_report()
        with self.assertRaisesRegex(ValueError, "same event"):
            self.database.save_event_timeline(PersistedEventTimelineItem(
                event_key="event-1", occurred_at=100, kind="reported", text="Wrong evidence",
                confidence=0.8, report_id=other_report.report_id,
            ))
        self.assertEqual([], self.database.list_event_timeline("event-1"))

    def test_claim_supersedes_requires_existing_acyclic_predecessor(self) -> None:
        self._event_report("event-1", "source-a", 1)
        original = self.database.save_event_claim(PersistedEventClaim(
            event_key="event-1", claim_key="original", text="Two people", status="active",
            confidence=0.9, first_seen_at=100, last_seen_at=100,
        ))
        with self.assertRaisesRegex(ValueError, "does not exist"):
            self.database.save_event_claim(replace(
                original, claim_key="corrected", text="Three people", supersedes_claim_key="missing",
            ))
        with self.assertRaisesRegex(ValueError, "itself"):
            self.database.save_event_claim(replace(original, supersedes_claim_key="original"))
        corrected = self.database.save_event_claim(replace(
            original, claim_key="corrected", text="Three people", supersedes_claim_key="original",
        ))
        self.assertEqual("original", corrected.supersedes_claim_key)
        self.assertEqual("superseded", next(
            claim.status for claim in self.database.list_event_claims("event-1")
            if claim.claim_key == "original"
        ))
        with self.assertRaisesRegex(ValueError, "cycle"):
            self.database.save_event_claim(replace(original, supersedes_claim_key="corrected"))
        self.assertEqual(2, len(self.database.list_event_claims("event-1")))

    def test_event_schema_migrates_additively_from_version_16(self) -> None:
        path = self.config.service.database_path
        self.database.connection.execute("DROP TABLE event_claim_evidence")
        self.database.connection.execute("DROP TABLE event_timeline")
        self.database.connection.execute("DROP TABLE event_claims")
        self.database.connection.execute("DROP TABLE event_reports")
        self.database.connection.execute("DROP TABLE events")
        self.database.connection.execute("PRAGMA user_version=16")
        self.database.connection.commit()
        self.database.close()
        migrated = Database(path)
        self.database = migrated
        self.assertEqual(SCHEMA_VERSION, migrated.status()["database_schema"])
        tables = {
            str(row["name"])
            for row in migrated.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertTrue({"events", "event_reports", "event_claims", "event_claim_evidence", "event_timeline"} <= tables)

    def _legacy_decision_events(self) -> list[str]:
        instant = 1_789_581_600
        titles = ("Federal Reserve issues FOMC statement", "Fed raises interest rates",
                  "Dollar Jumps After Fed Raises Rates, Sends Hawkish Signal")
        keys = []
        for index, title in enumerate(titles):
            source = f"decision-{index}"
            self.database.record_source_success(source, FeedFetchResult((observation(
                f"decision-{index}", title, source_id=source, timestamp=instant + index * 60,
            ),), None, None), self.rules, instant + 180, "eos")
            identifier = self.database.list_observations(instant, instant + 180)[0]["id"]
            key = f"legacy-decision-{index}"
            keys.append(key)
            self.database.save_event(PersistedEvent(
                event_key=key, fingerprint=key, title=title, summary=title,
                score=4, importance=4, urgency=3, relevance=4, confidence=0.8,
                first_seen_at=instant + index * 60, last_seen_at=instant + index * 60,
            ))
            self.database.save_event_report(PersistedEventReport(
                event_key=key, observation_id=identifier, source_id=source, publisher=source,
                source_tier="primary" if index == 0 else "secondary", relation="primary",
                match_score=1, is_representative=True, published_at=instant + index * 60,
                title=title, summary=title, url="https://example.test/decision",
            ))
        return keys

    def test_guarded_repair_preserves_evidence_and_leaves_audit(self) -> None:
        keys = self._legacy_decision_events()
        ids = [report.observation_id for key in keys for report in self.database.list_event_reports(key)]
        with self.assertRaisesRegex(ValueError, "evidence changed"):
            self.database.merge_semantic_event_group(keys, expected_observation_ids=ids[:-1], actor="test", now=1_789_582_000)
        self.assertEqual(1, len(self.database.list_event_reports(keys[1])))
        target = self.database.merge_semantic_event_group(keys, expected_observation_ids=ids, actor="test", now=1_789_582_000)
        self.assertEqual(keys[0], target)
        reports = self.database.list_event_reports(target)
        self.assertEqual(set(ids), {report.observation_id for report in reports})
        self.assertEqual("context", next(report.relation for report in reports if report.source_id == "decision-2"))
        self.assertEqual("repair_merge", self.database.list_event_timeline(target)[0].kind)
        self.assertEqual("closed", self.database.get_event(keys[1]).status)  # type: ignore[union-attr]
        self.assertEqual(4, self.database.connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0])

    def test_guarded_repair_refuses_existing_claim_graph(self) -> None:
        keys = self._legacy_decision_events()
        ids = [report.observation_id for key in keys for report in self.database.list_event_reports(key)]
        self.database.save_event_claim(PersistedEventClaim(
            event_key=keys[0], claim_key="decision", text="confirmed", status="active",
            confidence=1, first_seen_at=100, last_seen_at=100,
        ))
        with self.assertRaisesRegex(ValueError, "claims or timeline"):
            self.database.merge_semantic_event_group(keys, expected_observation_ids=ids, actor="test", now=1_789_582_000)

    def test_guarded_repair_refuses_historical_digest_references(self) -> None:
        keys = self._legacy_decision_events()
        ids = [report.observation_id for key in keys for report in self.database.list_event_reports(key)]
        instant = 1_789_581_600
        saved = self.database.save_digest(DigestBuilder(self.database).build(
            digest_key="daily:repair-history", period_start=instant - 1, period_end=instant + 180,
            timezone="Asia/Shanghai", created_at=instant + 200,
            source_ids=tuple(f"decision-{index}" for index in range(3)),
        ))
        with self.assertRaisesRegex(ValueError, "historical digests"):
            self.database.merge_semantic_event_group(keys, expected_observation_ids=ids, actor="test", now=instant + 300)
        self.assertEqual(saved, self.database.get_digest(saved.digest_key, saved.version))

    def test_projector_does_not_join_opposite_directions_through_announcement(self) -> None:
        instant = 1_789_581_600
        for index, title in enumerate(("Federal Reserve issues FOMC statement", "Fed raises interest rates", "Fed cuts interest rates")):
            self.database.record_source_success(f"fed-{index}", FeedFetchResult((observation(
                f"fed-{index}", title, source_id=f"fed-{index}", timestamp=instant + index * 60,
            ),), None, None), self.rules, instant + 180, "eos")
        EventPoolProjector(self.database).project_pending(now=instant + 200, since=instant, until=instant + 180)
        events = self.database.list_event_page(since=instant, until=instant + 180)
        self.assertEqual(2, len(events.items))
        for item in events.items:
            titles = {report.title for report in item.reports}
            self.assertFalse({"Fed raises interest rates", "Fed cuts interest rates"} <= titles)

    def test_projector_preserves_distinct_same_day_decisions_outside_window(self) -> None:
        instant = 1_789_581_600
        for index in range(2):
            published = instant + index * 7 * 3600
            self.database.record_source_success(f"rate-{index}", FeedFetchResult((observation(
                f"rate-{index}", "Fed raises interest rates", source_id=f"rate-{index}", timestamp=published,
            ),), None, None), self.rules, published, "eos")
            EventPoolProjector(self.database).project_pending(now=published, since=instant, until=published + 1)
        items = self.database.list_event_page(since=instant, until=instant + 8 * 3600).items
        self.assertEqual(2, len(items))
        self.assertTrue(all(item.report_count == 1 for item in items))

    def test_digest_builder_projects_event_and_reports(self) -> None:
        digest = DigestBuilder(self.database).build(
            digest_key="daily:2026-09-06", period_start=0, period_end=2_000_000_000,
            timezone="Asia/Shanghai", created_at=200, source_ids=("source-a",),
        )
        self.assertEqual(1, len(digest.items))
        self.assertIsNotNone(digest.items[0].event_id)
        self.assertEqual(1, len(self.database.list_events()))
        saved = self.database.save_digest(digest)
        loaded = self.database.get_digest(saved.digest_key, saved.version)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(digest.items[0].event_id, loaded.items[0].event_id)
        self.assertEqual(1, len(loaded.items[0].reports))

    def test_related_report_on_next_day_reuses_stable_event_key(self) -> None:
        first_time = 1_788_361_200
        first = DigestBuilder(self.database).build(
            digest_key="daily:first", period_start=first_time - 10,
            period_end=first_time + 10, timezone="Asia/Shanghai",
            created_at=first_time + 10, source_ids=("source-a",),
        )
        self.database.record_source_success(
            "source-a",
            FeedFetchResult((observation(
                "obs-2", "Official event update", source_id="source-a",
                timestamp=first_time + 3600,
            ),), None, None),
            self.rules, first_time + 3600, self.config.ntfy.default_topic,
        )
        second = DigestBuilder(self.database).build(
            digest_key="daily:second", period_start=first_time + 10,
            period_end=first_time + 7200, timezone="Asia/Shanghai",
            created_at=first_time + 7200, source_ids=("source-a",),
        )
        self.assertEqual(first.items[0].event_id, second.items[0].event_id)
        event = self.database.get_event(first.items[0].event_id or "")
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(first_time, event.first_seen_at)
        self.assertEqual(first_time + 3600, event.last_seen_at)

    def test_continuous_projector_and_cursor_page_are_read_only(self) -> None:
        projector = EventPoolProjector(self.database)
        self.assertEqual(1, projector.project_pending(now=200, limit=50))
        page = self.database.list_event_page(
            since=0, until=2_000_000_000, sort="latest", limit=1
        )
        self.assertEqual(1, len(page.items))
        self.assertEqual("source-a", page.items[0].reports[0].source_id)
        self.assertEqual("Bloomberg", page.items[0].reports[0].publisher)
        before = self.database.connection.total_changes
        repeated = self.database.list_event_page(
            since=0, until=2_000_000_000, sort="latest", limit=1
        )
        self.assertEqual(before, self.database.connection.total_changes)
        self.assertEqual(page.items, repeated.items)

    def test_event_projection_failure_rolls_back_event(self) -> None:
        EventPoolProjector(self.database).project_pending(now=200, limit=50)
        existing = self.database.list_events()[0]
        report = self.database.list_event_reports(existing.event_key)[0]
        event = replace(existing, event_key="failed-projection")
        invalid = replace(report, event_key=event.event_key, observation_id=999999)
        with self.assertRaises(KeyError):
            self.database.save_event_projection(event, invalid)
        self.assertIsNone(self.database.get_event(event.event_key))

    def test_event_relation_update_failure_rolls_back_new_report(self) -> None:
        EventPoolProjector(self.database).project_pending(now=200, limit=50)
        existing = self.database.list_events()[0]
        prior = self.database.list_event_reports(existing.event_key)[0]
        self.database.record_source_success(
            "source-a",
            FeedFetchResult((observation(
                "new-report", "Official event update", source_id="source-a", timestamp=300,
            ),), None, None),
            self.rules, 300, self.config.ntfy.default_topic,
        )
        pending = self.database.list_unassigned_event_observations(limit=1)
        self.assertEqual(1, len(pending))
        incoming = replace(
            prior, report_id=None, observation_id=int(pending[0]["id"]),
            published_at=300, created_at=300, relation="updates",
        )
        invalid_update = replace(prior, report_id=999999, relation="corroborates")
        with self.assertRaises(ValueError):
            self.database.save_event_projection(
                replace(existing, last_seen_at=300, updated_at=300), incoming,
                relation_updates=(invalid_update,),
            )
        self.assertEqual(existing.last_seen_at, self.database.get_event(existing.event_key).last_seen_at)
        self.assertEqual(1, len(self.database.list_event_reports(existing.event_key)))
        self.assertEqual(1, len(self.database.list_unassigned_event_observations(limit=1)))

    def test_event_projection_repeated_assignment_does_not_create_orphan(self) -> None:
        projector = EventPoolProjector(self.database)
        self.assertEqual(1, projector.project_pending(now=200, limit=50))
        existing = self.database.list_events()[0]
        report = self.database.list_event_reports(existing.event_key)[0]
        competing = replace(existing, event_key="competing-projection")
        saved = self.database.save_event_projection(
            competing, replace(report, event_key=competing.event_key),
        )
        self.assertEqual(existing.event_key, saved.event_key)
        self.assertIsNone(self.database.get_event(competing.event_key))
        self.assertEqual(0, projector.project_pending(now=201, limit=50))

    def test_multiple_feeds_from_one_publisher_are_one_independent_source(self) -> None:
        now = 1_788_361_200
        self.database.record_source_success(
            "source-b", FeedFetchResult((observation(
                "other-feed", "Official event", source_id="source-b", timestamp=now,
            ),), None, None), self.rules, now, self.config.ntfy.default_topic,
        )
        EventPoolProjector(self.database).project_pending(now=now, limit=50)
        events = self.database.list_events()
        self.assertEqual(1, len(events))
        self.assertEqual(1, events[0].independent_source_count)
        self.assertEqual(2, len(self.database.list_event_reports(events[0].event_key)))

    def test_recurring_title_outside_match_window_gets_new_event_key(self) -> None:
        projector = EventPoolProjector(self.database)
        projector.project_pending(now=200, limit=50)
        old = self.database.list_events()[0]
        later = old.last_seen_at + 10 * 86400
        self.database.record_source_success(
            "source-a",
            FeedFetchResult((observation(
                "obs-later", "Official event", source_id="source-a", timestamp=later,
            ),), None, None),
            self.rules, later, self.config.ntfy.default_topic,
        )
        projector.project_pending(now=later, limit=50)
        self.assertEqual(2, len(self.database.list_events()))
        self.assertEqual(2, len({item.event_key for item in self.database.list_events()}))

    def test_event_page_reports_are_scoped_to_requested_window(self) -> None:
        first_time = 1_788_361_200
        self.database.record_source_success(
            "source-a",
            FeedFetchResult((observation(
                "window-first", "Official policy decision", source_id="source-a",
                timestamp=first_time,
            ),), None, None),
            self.rules, first_time, self.config.ntfy.default_topic,
        )
        EventPoolProjector(self.database).project_pending(
            now=first_time, since=first_time - 1, until=first_time + 1, limit=50
        )
        future = first_time + 3600
        self.database.record_source_success(
            "source-a",
            FeedFetchResult((observation(
                "window-future", "Official policy decision update", source_id="source-a",
                timestamp=future,
            ),), None, None),
            self.rules, future, self.config.ntfy.default_topic,
        )
        EventPoolProjector(self.database).project_pending(now=future, limit=50)
        digest = DigestBuilder(self.database).build(
            digest_key="daily:scoped", period_start=first_time - 1,
            period_end=first_time + 1, timezone="Asia/Shanghai",
            created_at=future + 1,
        )
        scoped_items = [
            item for item in digest.items if 2 in item.observation_ids
        ]
        self.assertEqual(1, len(scoped_items))
        self.assertEqual((2,), scoped_items[0].observation_ids)
        page = self.database.list_event_page(
            since=first_time - 1, until=first_time + 1, limit=30
        )
        matching = [
            item for item in page.items
            if any(report.observation_id == 2 for report in item.reports)
        ]
        self.assertEqual(1, len(matching))
        self.assertEqual([2], [report.observation_id for report in matching[0].reports])

    def test_published_digest_reports_do_not_grow_with_current_event(self) -> None:
        first_time = 1_788_361_200
        first = DigestBuilder(self.database).build_and_save(
            digest_key="daily:frozen", period_start=first_time - 10,
            period_end=first_time + 10, timezone="Asia/Shanghai",
            created_at=first_time + 10,
        )
        published = self.database.publish_digest(first.digest_key, first.version, first_time + 10)
        original_ids = tuple(report.observation_id for report in published.items[0].reports)
        self.database.record_source_success(
            "source-a", FeedFetchResult((observation(
                "future-report", "Official event update", source_id="source-a",
                timestamp=first_time + 3600,
            ),), None, None), self.rules, first_time + 3600, self.config.ntfy.default_topic,
        )
        EventPoolProjector(self.database).project_pending(now=first_time + 3600, limit=50)
        loaded = self.database.get_digest(first.digest_key, published_only=True)
        assert loaded is not None
        self.assertEqual(original_ids, tuple(report.observation_id for report in loaded.items[0].reports))
        self.assertTrue(loaded.items[0].reports[0].is_representative)
        self.assertEqual(2, len(self.database.list_event_reports(loaded.items[0].event_id or "")))

    def test_digest_pool_keeps_low_score_immediate_event(self) -> None:
        now = 1_788_361_200
        detail = self.database.create_manual_event(
            ManualEventSpec(
                title="Low score but notified", summary="Operator notification",
                importance=1, region="GLOBAL", topic="general",
            ),
            "test", now, self.config.ntfy.default_topic,
        )
        EventPoolProjector(self.database).project_pending(now=now, limit=50)
        digest = DigestBuilder(self.database, item_limit=1).build(
            digest_key="daily:immediate", period_start=0, period_end=2_000_000_000,
            timezone="Asia/Shanghai", created_at=now + 1,
        )
        self.assertEqual(1, len(digest.items))
        self.assertEqual("Low score but notified", digest.items[0].title)
        self.assertEqual("immediate", digest.items[0].handling)
        self.assertEqual(detail["observation"]["id"], digest.items[0].observation_ids[0])

    def test_delayed_notification_enters_current_event_and_digest_window(self) -> None:
        old = 1_700_000_000
        notified_at = old + 86400
        detail = self.database.create_manual_event(
            ManualEventSpec(
                title="Delayed official confirmation", summary="Evidence",
                importance=4, region="GLOBAL", topic="general",
            ),
            "test", old, self.config.ntfy.default_topic,
        )
        with self.database.connection:
            self.database.connection.execute(
                "UPDATE alerts SET created_at=? WHERE observation_id=?",
                (notified_at, detail["observation"]["id"]),
            )
        EventPoolProjector(self.database).project_pending(now=notified_at, limit=50)
        page = self.database.list_event_page(
            since=notified_at - 1, until=notified_at + 1,
        )
        self.assertEqual(1, len(page.items))
        self.assertEqual("immediate", page.items[0].handling)
        digest = DigestBuilder(self.database).build(
            digest_key="daily:delayed", period_start=notified_at - 1,
            period_end=notified_at + 1, timezone="Asia/Shanghai",
            created_at=notified_at + 1,
        )
        self.assertEqual((detail["observation"]["id"],), digest.items[0].observation_ids)

    def test_unassigned_backfill_prioritizes_latest_observations(self) -> None:
        old = 1_700_000_000
        recent = 1_800_000_000
        self.database.record_source_success(
            "source-a",
            FeedFetchResult((
                observation("old-backfill", "Old event", source_id="source-a", timestamp=old),
                observation("recent-backfill", "Recent event", source_id="source-a", timestamp=recent),
            ), None, None),
            self.rules, recent, self.config.ntfy.default_topic,
        )
        rows = self.database.list_unassigned_event_observations(limit=1)
        self.assertEqual("recent-backfill", rows[0]["external_id"])

    def test_importance_cursor_paginates_without_duplicates(self) -> None:
        EventPoolProjector(self.database).project_pending(now=200, limit=50)
        self.database.record_source_success(
            "source-a",
            FeedFetchResult((observation(
                "another-page", "Unrelated technology event", source_id="source-a",
                timestamp=1_800_000_000,
            ),), None, None),
            self.rules, 1_800_000_000, self.config.ntfy.default_topic,
        )
        EventPoolProjector(self.database).project_pending(now=1_800_000_000, limit=50)
        first = self.database.list_event_page(
            since=0, until=2_000_000_000, sort="importance", limit=1
        )
        self.assertIsNotNone(first.next_cursor)
        second = self.database.list_event_page(
            since=0, until=2_000_000_000, sort="importance", limit=1,
            cursor=first.next_cursor,
        )
        self.assertEqual(1, len(second.items))
        self.assertNotEqual(first.items[0].event.event_key, second.items[0].event.event_key)


if __name__ == "__main__":
    unittest.main()
