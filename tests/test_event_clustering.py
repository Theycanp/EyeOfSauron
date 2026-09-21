from __future__ import annotations

import unittest

from argus.event_clustering import cluster_events, event_match_score, explain_event_match
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
    def test_fed_decision_reports_share_one_event_and_keep_market_context(self) -> None:
        titles = (
            "Federal Reserve issues FOMC statement",
            "Federal Reserve Board and Federal Open Market Committee release economic projections from the September 15-16 FOMC meeting",
            "Fed Raises Rates as Warsh Bucks Trump to Contain Inflation",
            "Federal Reserve raises rates for first time since 2023",
            "Key Takeaways From Fed Decision to Raise Interest Rates",
            "Treasuries Hold Gains as Fed Hikes Rates, Signaling More Ahead",
            "Fed Unanimously Raises Rates by a Quarter Point",
            "Dollar Jumps After Fed Raises Rates, Sends Hawkish Signal",
            "Federal Reserve raises fed funds rate with likely more to come",
        )
        rows = [
            {
                **_row(
                    index,
                    title,
                    source_id=f"source-{index}",
                    published_at=1_789_581_600 + index * 120,
                ),
                "topic": "general",
                "region": "GLOBAL",
            }
            for index, title in enumerate(titles, 1)
        ]
        events = cluster_events(rows)
        self.assertEqual(1, len(events))
        self.assertEqual(9, len(events[0].reports))
        relations = {report.title: report.relation for report in events[0].reports}
        self.assertEqual("context", relations[titles[5]])
        self.assertEqual("context", relations[titles[7]])

    def test_distinct_central_bank_decisions_do_not_merge(self) -> None:
        events = cluster_events([
            _row(1, "Federal Reserve raises interest rates", source_id="fed"),
            _row(2, "European Central Bank raises interest rates", source_id="ecb"),
        ])
        self.assertEqual(2, len(events))

    def test_reviewed_multilingual_aliases_merge_parallel_reports(self) -> None:
        events = cluster_events([
            {
                **_row(1, "Federal Reserve raises interest rates by 25 basis points", source_id="fed", tier="primary", published_at=1_800_000_000),
                "region": "US",
                "attributes": {"entities": ("Federal Reserve",)},
            },
            {
                **_row(2, "美联储加息25个基点", source_id="cn-wire", tier="secondary", published_at=1_800_000_060),
                "region": "US",
                "attributes": {"entities": ("美联储",)},
            },
        ])
        self.assertEqual(1, len(events))
        self.assertEqual({"primary", "secondary"}, {report.source_tier for report in events[0].reports})
        self.assertEqual("corroborates", next(report.relation for report in events[0].reports if report.source_id == "cn-wire"))

    def test_multilingual_opposite_rate_directions_remain_separate(self) -> None:
        events = cluster_events([
            {
                **_row(1, "Federal Reserve raises interest rates", source_id="fed", tier="primary"),
                "region": "US",
                "attributes": {"entities": ("Federal Reserve",)},
            },
            {
                **_row(2, "美联储降息", source_id="cn-wire", tier="secondary"),
                "region": "US",
                "attributes": {"entities": ("美联储",)},
            },
        ])
        self.assertEqual(2, len(events))

    def test_reviewed_japanese_aliases_merge_with_english_report(self) -> None:
        events = cluster_events([
            {
                **_row(1, "Bank of Japan raises interest rates by 25 basis points", source_id="boj", tier="primary"),
                "region": "JP",
                "attributes": {"entities": ("Bank of Japan",)},
            },
            {
                **_row(2, "日本銀行が利上げ、25ベーシスポイント", source_id="jp-wire", tier="secondary", published_at=1_800_000_060),
                "region": "JP",
                "attributes": {"entities": ("日本銀行",)},
            },
        ])
        self.assertEqual(1, len(events))
        self.assertEqual({"primary", "secondary"}, {report.source_tier for report in events[0].reports})

    def test_explanation_exposes_reviewed_signals_and_action_boundary(self) -> None:
        left = _row(1, "Federal Reserve raises interest rates", source_id="fed")
        right = _row(2, "美联储降息", source_id="cn-wire")
        evidence = explain_event_match(left, right)
        self.assertEqual(0.0, event_match_score(left, right))
        self.assertIn("entity_federal_reserve", evidence["shared_canonical_signals"])
        self.assertEqual(["action_rate_hike"], evidence["left_canonical_actions"])
        self.assertEqual(["action_rate_cut"], evidence["right_canonical_actions"])

    def test_same_day_decisions_outside_window_have_unique_cluster_keys(self) -> None:
        instant = 1_789_581_600
        events = cluster_events([
            _row(1, "Federal Reserve raises interest rates", source_id="fed", published_at=instant),
            _row(2, "Federal Reserve raises interest rates", source_id="fed", published_at=instant + 7 * 3600),
        ])
        self.assertEqual(2, len(events))
        self.assertEqual(2, len({event.event_id for event in events}))

    def test_confirmed_identity_bridges_source_topic_and_region_taxonomies(self) -> None:
        events = cluster_events([
            {**_row(1, "Federal Reserve issues FOMC statement", source_id="fed"), "topic": "policy", "region": "US"},
            {**_row(2, "Fed raises interest rates", source_id="media"), "topic": "markets", "region": "GLOBAL"},
        ])
        self.assertEqual(1, len(events))

    def test_speculative_rate_story_has_no_semantic_override(self) -> None:
        events = cluster_events([
            _row(1, "Fed could cut rates later this year", source_id="a"),
            _row(2, "Fed may raise rates if inflation returns", source_id="b"),
        ])
        self.assertEqual(2, len(events))

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
