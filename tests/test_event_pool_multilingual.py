from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from argus.database import Database
from argus.event_pool import EventPoolProjector
from argus.models import FeedFetchResult
from argus.rules import RuleSet

from helpers import observation, production_config


class EventPoolMultilingualReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = production_config(self.root)
        self.database = Database(self.config.service.database_path)
        self.rules = RuleSet.from_config(self.config.rules, self.config.ntfy.default_topic)
        self.projector = EventPoolProjector(self.database)

    def tearDown(self) -> None:
        self.database.close()
        self.temp.cleanup()

    def add(
        self,
        source_id: str,
        external_id: str,
        title: str,
        *,
        publisher: str,
        tier: str,
        topic: str,
        region: str,
        timestamp: int,
        attributes: dict[str, object] | None = None,
    ) -> None:
        item = observation(external_id, title, source_id=source_id, timestamp=timestamp)
        item = replace(
            item,
            publisher=publisher,
            source_tier=tier,
            topic=topic,
            region=region,
            summary=title,
            attributes=attributes if attributes is not None else {"entities": []},
        )
        self.database.record_source_success(
            source_id,
            FeedFetchResult((item,), None, None),
            self.rules,
            timestamp,
            self.config.ntfy.default_topic,
        )

    def test_verified_occurrence_joins_cross_batch_and_publication_window(self) -> None:
        first_time = 1_800_000_000
        identity = {
            "kind": "earthquake", "entity_ids": ["fault:one"],
            "location_id": "jp:tohoku", "occurred_at": first_time,
            "time_precision": "second",
        }
        self.add(
            "official-one", "one", "Major earthquake reported in Tohoku",
            publisher="Agency One", tier="primary", topic="disaster", region="JP",
            timestamp=first_time, attributes={"event_identity": identity},
        )
        self.assertEqual(1, self.projector.project_pending(now=first_time + 10))
        key = self.database.list_events()[0].event_key
        self.add(
            "official-two", "two", "Revised event data",
            publisher="Agency Two", tier="primary", topic="science", region="GLOBAL",
            timestamp=first_time + 10 * 86400, attributes={"event_identity": identity},
        )
        self.assertEqual(1, self.projector.project_pending(now=first_time + 10 * 86400 + 10))
        self.assertEqual(2, len(self.database.list_event_reports(key)))
        self.assertEqual(key, self.database.find_event_by_occurrence(
            self.database.list_event_occurrences(key)[0].key
        ).event_key)
        self.assertEqual(1, len(self.database.list_events()))
        second = next(report for report in self.database.list_event_reports(key)
                      if report.source_id == "official-two")
        with self.assertRaisesRegex(ValueError, "verified occurrence"):
            self.database.preview_event_repair(
                "split", [key], observation_ids=[second.observation_id]
            )

    def test_distinct_verified_occurrences_with_same_headline_remain_separate(self) -> None:
        first_time = 1_800_000_000
        base = {
            "kind": "earthquake", "entity_ids": ["fault:one"],
            "location_id": "jp:tohoku", "occurred_at": first_time,
            "time_precision": "second",
        }
        for suffix, occurred_at in (("one", first_time), ("two", first_time + 3600)):
            self.add(
                f"official-{suffix}", suffix, "Major earthquake reported in Tohoku",
                publisher=f"Agency {suffix}", tier="primary", topic="disaster", region="JP",
                timestamp=first_time + (0 if suffix == "one" else 60),
                attributes={"event_identity": {**base, "occurred_at": occurred_at}},
            )
            self.assertEqual(1, self.projector.project_pending(
                now=first_time + (10 if suffix == "one" else 70)
            ))
        self.assertEqual(2, len(self.database.list_events()))
        self.assertEqual({1}, {len(self.database.list_event_reports(e.event_key)) for e in self.database.list_events()})
        with self.assertRaisesRegex(ValueError, "distinct verified occurrences"):
            self.database.preview_event_repair(
                "merge", [event.event_key for event in self.database.list_events()]
            )

    def test_split_moves_verified_occurrence_binding_with_its_report(self) -> None:
        now = 1_800_000_000
        self.add("unverified", "intro", "Major earthquake reported in Tohoku",
                 publisher="News", tier="secondary", topic="disaster", region="JP", timestamp=now)
        self.assertEqual(1, self.projector.project_pending(now=now + 1))
        original = self.database.list_events()[0].event_key
        self.add("official", "quake", "Major earthquake reported in Tohoku",
                 publisher="Agency", tier="primary", topic="disaster", region="JP", timestamp=now + 30,
                 attributes={"event_identity": {
                     "kind": "earthquake", "entity_ids": ["fault:one"],
                     "location_id": "jp:tohoku", "occurred_at": now,
                     "time_precision": "second",
                 }})
        self.assertEqual(1, self.projector.project_pending(now=now + 31))
        official = next(report for report in self.database.list_event_reports(original)
                        if report.source_id == "official")
        occurrence = self.database.list_event_occurrences(original)[0]
        preview = self.database.preview_event_repair(
            "split", [original], observation_ids=[official.observation_id]
        )
        target = self.database.apply_event_repair(
            "split", [original], observation_ids=[official.observation_id],
            expected_revision=preview["revision"], actor="operator",
            reason="Distinct occurrence verified", now=now + 40,
        )
        self.assertEqual((), self.database.list_event_occurrences(original))
        self.assertEqual(target, self.database.find_event_by_occurrence(occurrence.key).event_key)
        self.assertEqual(occurrence.key, self.database.list_event_occurrences(target)[0].key)

    def test_projection_reuses_event_for_cross_language_and_keeps_tiers(self) -> None:
        now = 1_800_000_000
        self.add(
            "boj", "boj-1", "日本銀行、政策金利を0.25％に据え置き",
            publisher="BOJ", tier="primary", topic="policy", region="JP", timestamp=now,
        )
        self.assertEqual(1, self.projector.project_pending(now=now + 30))
        first = self.database.list_events()[0]
        self.add(
            "wire", "wire-1", "BOJ keeps its benchmark rate at 0.25%",
            publisher="Reuters", tier="secondary", topic="policy", region="JP", timestamp=now + 60,
        )
        self.assertEqual(1, self.projector.project_pending(now=now + 90))
        reports = self.database.list_event_reports(first.event_key)
        self.assertEqual(1, len(self.database.list_events()))
        self.assertEqual(2, len(reports))
        self.assertEqual({"primary", "corroborates"}, {report.relation for report in reports})

    def test_projection_keeps_opposite_decision_as_distinct_event(self) -> None:
        now = 1_800_000_000
        self.add(
            "boj", "boj-1", "日本銀行、政策金利を0.25％に据え置き",
            publisher="BOJ", tier="primary", topic="policy", region="JP", timestamp=now,
        )
        self.projector.project_pending(now=now + 30)
        self.add(
            "wire", "wire-1", "BOJ cuts policy rate to 0.10%",
            publisher="Reuters", tier="secondary", topic="policy", region="JP", timestamp=now + 60,
        )
        self.projector.project_pending(now=now + 90)
        self.assertEqual(2, len(self.database.list_events()))

    def test_late_primary_reclassifies_secondary_but_preserves_market_context(self) -> None:
        now = 1_800_000_000
        self.add(
            "wire", "wire-1", "Fed raises interest rates",
            publisher="Reuters", tier="secondary", topic="policy", region="US", timestamp=now,
        )
        self.assertEqual(1, self.projector.project_pending(now=now + 10))
        event_key = self.database.list_events()[0].event_key
        self.assertEqual("context", self.database.list_event_reports(event_key)[0].relation)

        self.add(
            "market", "market-1", "Stocks rise after Fed raises interest rates",
            publisher="Market Wire", tier="secondary", topic="policy", region="US", timestamp=now + 20,
        )
        self.assertEqual(1, self.projector.project_pending(now=now + 30))
        self.add(
            "fed", "fed-1", "Federal Reserve raises interest rates",
            publisher="Federal Reserve", tier="primary", topic="policy", region="US", timestamp=now + 40,
        )
        self.assertEqual(1, self.projector.project_pending(now=now + 50))
        self.assertEqual(1, len(self.database.list_events()))
        reports = {report.source_id: report for report in self.database.list_event_reports(event_key)}
        self.assertEqual("corroborates", reports["wire"].relation)
        self.assertEqual("context", reports["market"].relation)
        self.assertEqual("primary", reports["fed"].relation)
        self.assertEqual(0, self.projector.project_pending(now=now + 60))
        self.assertEqual(reports, {report.source_id: report for report in self.database.list_event_reports(event_key)})

    def test_late_primary_from_same_source_remains_primary(self) -> None:
        now = 1_800_000_000
        self.add(
            "fed", "fed-wire", "Fed raises interest rates",
            publisher="Federal Reserve", tier="secondary", topic="policy", region="US", timestamp=now,
        )
        self.projector.project_pending(now=now + 10)
        event_key = self.database.list_events()[0].event_key
        self.add(
            "fed", "fed-official", "Federal Reserve raises interest rates",
            publisher="Federal Reserve", tier="primary", topic="policy", region="US", timestamp=now + 30,
        )
        self.assertEqual(1, self.projector.project_pending(now=now + 40))
        reports = {report.observation_id: report for report in self.database.list_event_reports(event_key)}
        self.assertEqual(1, len(self.database.list_events()))
        self.assertEqual({"primary", "corroborates"}, {report.relation for report in reports.values()})

    def test_nonsemantic_market_context_stays_context_after_late_primary(self) -> None:
        now = 1_800_000_000
        self.add(
            "wire", "wire-1", "BOJ keeps its benchmark rate at 0.25%",
            publisher="Reuters", tier="secondary", topic="policy", region="JP", timestamp=now,
        )
        self.projector.project_pending(now=now + 10)
        event_key = self.database.list_events()[0].event_key
        self.add(
            "market", "market-1", "Yen rises after BOJ decision",
            publisher="Market Wire", tier="secondary", topic="policy", region="JP", timestamp=now + 20,
        )
        self.assertEqual(1, self.projector.project_pending(now=now + 30))
        self.add(
            "boj", "boj-1", "日本銀行、政策金利を0.25％に据え置き",
            publisher="BOJ", tier="primary", topic="policy", region="JP", timestamp=now + 40,
        )
        self.assertEqual(1, self.projector.project_pending(now=now + 50))
        self.assertEqual(1, len(self.database.list_events()))
        reports = {report.source_id: report for report in self.database.list_event_reports(event_key)}
        self.assertEqual("corroborates", reports["wire"].relation)
        self.assertEqual("context", reports["market"].relation)
        self.assertEqual("primary", reports["boj"].relation)


if __name__ == "__main__":
    unittest.main()
