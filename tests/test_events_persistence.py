from __future__ import annotations

import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
