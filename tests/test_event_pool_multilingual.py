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
    ) -> None:
        item = observation(external_id, title, source_id=source_id, timestamp=timestamp)
        item = replace(
            item,
            publisher=publisher,
            source_tier=tier,
            topic=topic,
            region=region,
            summary=title,
            attributes={"entities": []},
        )
        self.database.record_source_success(
            source_id,
            FeedFetchResult((item,), None, None),
            self.rules,
            timestamp,
            self.config.ntfy.default_topic,
        )

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
