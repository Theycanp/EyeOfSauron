from __future__ import annotations

import unittest

from argus.event_clustering import cluster_events
from argus.digest import cluster_observations_event_centric


def _row(
    identifier: int,
    title: str,
    *,
    source_id: str,
    tier: str = "secondary",
    entities: tuple[str, ...] = (),
    published_at: int = 1_800_000_000,
    number: str | None = None,
) -> dict[str, object]:
    return {
        "id": identifier,
        "source_id": source_id,
        "publisher": source_id,
        "external_id": f"{source_id}:{identifier}",
        "title": title,
        "summary": title,
        "url": f"https://example.test/{source_id}/{identifier}",
        "published_at": published_at,
        "topic": "policy",
        "region": "CN",
        "source_tier": tier,
        "importance": 4,
        "urgency": 3,
        "relevance": 4,
        "confidence": 0.8,
        "attributes": {"entities": entities} if entities else {},
        **({"summary": f"{title} {number}"} if number else {}),
    }


class EventClusteringTests(unittest.TestCase):
    def test_order_independent_and_stable_without_observation_ids(self) -> None:
        first = _row(1, "财政部向金融机构增资", source_id="official", tier="primary", entities=("财政部", "金融机构"))
        second = _row(2, "金融机构获财政部增资", source_id="media", entities=("财政部", "金融机构"), published_at=1_800_000_060)
        a = cluster_events([first, second])
        b = cluster_events([{**second, "id": 99}, {**first, "id": 88}])
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0].event_id, b[0].event_id)
        self.assertEqual({report.source_id for report in a[0].reports}, {"official", "media"})

    def test_primary_secondary_reports_remain_parallel(self) -> None:
        event = cluster_events(
            [
                _row(1, "央行宣布维持政策利率", source_id="central-bank", tier="primary", entities=("央行",)),
                _row(2, "央行维持政策利率不变", source_id="wire", tier="secondary", entities=("央行",)),
                _row(3, "央行维持利率引发讨论", source_id="social", tier="social", entities=("央行",)),
            ]
        )[0]
        relations = {report.source_id: report.relation for report in event.reports}
        self.assertEqual(relations["central-bank"], "primary")
        self.assertEqual(relations["wire"], "corroborates")
        self.assertEqual(relations["social"], "context")

    def test_conflicting_numbers_are_retained_and_marked(self) -> None:
        event = cluster_events(
            [
                _row(1, "工厂爆炸造成伤亡", source_id="official", tier="primary", entities=("工厂",), number="2人"),
                _row(2, "工厂爆炸造成伤亡", source_id="media", entities=("工厂",), number="5人"),
            ]
        )[0]
        self.assertTrue(event.has_contradictions)
        self.assertEqual(2, len(event.reports))
        self.assertTrue(all(report.contradicts for report in event.reports))

    def test_complete_linkage_guard_blocks_chain_merge(self) -> None:
        a = _row(1, "甲公司发布芯片计划", source_id="a", entities=("甲公司", "芯片"))
        b = _row(2, "甲公司发布芯片与电池计划", source_id="b", entities=("甲公司", "芯片", "电池"))
        c = _row(3, "乙公司发布电池计划", source_id="c", entities=("乙公司", "电池"))
        events = cluster_events([c, a, b])
        self.assertEqual(2, len(events))
        self.assertEqual({"a", "b"}, {report.source_id for report in events[0].reports})

    def test_digest_adapter_uses_stable_event_key_and_parallel_tiers(self) -> None:
        rows = [
            _row(1, "央行宣布维持政策利率", source_id="central-bank", tier="primary", entities=("央行",)),
            _row(2, "央行维持政策利率不变", source_id="wire", tier="secondary", entities=("央行",)),
        ]
        item = cluster_observations_event_centric(rows)[0]
        self.assertEqual(24, len(item.cluster_key))
        self.assertEqual(("central-bank", "wire"), item.source_ids)
        self.assertEqual(("primary", "secondary"), item.source_tiers)
        self.assertEqual((1, 2), item.observation_ids)


if __name__ == "__main__":
    unittest.main()
