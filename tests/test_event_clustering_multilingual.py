from __future__ import annotations

import itertools
import unittest

from argus.event_clustering import cluster_events, event_match_score, explain_event_match


def row(identifier: int, title: str, *, source: str, tier: str = "secondary",
        published_at: int = 1_800_000_000, summary: str = "", region: str = "JP",
        topic: str = "policy") -> dict[str, object]:
    return {
        "id": identifier,
        "source_id": source,
        "publisher": source,
        "external_id": f"{source}:{identifier}",
        "title": title,
        "summary": summary or title,
        "url": f"https://example.test/{source}/{identifier}",
        "published_at": published_at,
        "topic": topic,
        "region": region,
        "source_tier": tier,
        "importance": 4,
        "urgency": 3,
        "relevance": 4,
        "confidence": 0.8,
    }


class MultilingualEventReplayTests(unittest.TestCase):
    def test_boj_chinese_english_and_social_context_share_one_event(self) -> None:
        observations = [
            row(1, "日本銀行、政策金利を0.25％に据え置き", source="boj", tier="primary",
                summary="日銀は金融政策決定会合で政策金利を維持した"),
            row(2, "BOJ keeps its benchmark rate at 0.25%", source="wire", tier="secondary",
                summary="The Bank of Japan left the benchmark rate unchanged"),
            row(3, "Yen rises after BOJ decision", source="social", tier="social",
                summary="Japanese yen moves higher after the Bank of Japan policy decision",
                topic="markets"),
        ]
        events = cluster_events(observations)
        self.assertEqual(1, len(events))
        self.assertEqual(3, len(events[0].reports))
        relations = {report.source_id: report.relation for report in events[0].reports}
        self.assertEqual("primary", relations["boj"])
        self.assertEqual("corroborates", relations["wire"])
        self.assertEqual("context", relations["social"])

    def test_same_institution_different_decisions_do_not_merge(self) -> None:
        observations = [
            row(1, "日本銀行、政策金利を0.25％に据え置き", source="boj", tier="primary"),
            row(2, "BOJ cuts policy rate to 0.10%", source="wire", tier="secondary"),
        ]
        self.assertEqual(2, len(cluster_events(observations)))

    def test_federal_reserve_chinese_and_english_rate_headlines_merge(self) -> None:
        observations = [
            row(1, "Federal Reserve raises rates", source="fed", tier="primary", region="US",
                summary="The Fed raises interest rates"),
            row(2, "美联储加息25个基点", source="wire", tier="secondary", region="US",
                summary="美联储宣布加息25个基点"),
        ]
        events = cluster_events(observations)
        self.assertEqual(1, len(events))
        self.assertGreaterEqual(event_match_score(observations[0], observations[1]), 0.57)

    def test_cross_language_amounts_are_equal_but_conflicting_amounts_are_marked(self) -> None:
        same = [
            row(1, "财政部向工商银行增资1000亿元", source="mof", tier="primary", region="CN",
                summary="财政部将向中国工商银行注资1000亿元"),
            row(2, "China plans a CNY 100 billion capital injection into ICBC", source="wire",
                region="CN", summary="The Finance Ministry plans a 100 billion yuan injection into ICBC"),
        ]
        events = cluster_events(same)
        self.assertEqual(1, len(events))
        self.assertFalse(events[0].has_contradictions)

        conflict = [
            same[0],
            row(3, "工商银行获财政部增资2000亿元", source="social", tier="social", region="CN",
                summary="社交平台称注资金额为2000亿元"),
        ]
        events = cluster_events(conflict)
        self.assertEqual(1, len(events))
        self.assertTrue(events[0].has_contradictions)

    def test_shared_finance_ministry_does_not_merge_different_action_types(self) -> None:
        observations = [
            row(1, "Finance Ministry releases annual budget", source="mof", tier="primary",
                region="CN", topic="policy"),
            row(2, "Finance Ministry injects capital into ICBC", source="wire",
                region="CN", topic="policy"),
        ]
        self.assertEqual(2, len(cluster_events(observations)))

    def test_order_independent_replay_and_match_explanation(self) -> None:
        observations = [
            row(1, "日本銀行、政策金利を0.25％に据え置き", source="boj", tier="primary"),
            row(2, "BOJ keeps its benchmark rate at 0.25%", source="wire"),
            row(3, "Yen rises after BOJ decision", source="social", tier="social", topic="markets"),
        ]
        baseline = cluster_events(observations)
        for permutation in itertools.permutations(observations):
            replay = cluster_events(list(permutation))
            self.assertEqual(
                [(event.event_id, [report.report_id for report in event.reports]) for event in baseline],
                [(event.event_id, [report.report_id for report in event.reports]) for event in replay],
            )
        evidence = explain_event_match(observations[0], observations[1])
        self.assertGreaterEqual(event_match_score(observations[0], observations[1]), 0.57)
        self.assertTrue(evidence["shared_entities"] or evidence.get("shared_canonical_signals"))


if __name__ == "__main__":
    unittest.main()
