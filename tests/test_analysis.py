from __future__ import annotations

import unittest

from argus.analysis import HANDLING_ARCHIVE, HANDLING_DIGEST, HANDLING_IMMEDIATE, analyze_observation, merge_advisory_analysis
from tests.helpers import observation


class AnalysisTests(unittest.TestCase):
    def test_primary_high_impact_urgent_item_is_immediate(self):
        item = observation("primary", "紧急政策公告")
        item = item.__class__(**{
            **{field: getattr(item, field) for field in item.__dataclass_fields__},
            "attributes": {
                "source_tier": "primary", "importance": 5, "urgency": 5,
                "region": "JP", "topic": "policy",
            },
        })
        result = analyze_observation(item)
        self.assertEqual(HANDLING_IMMEDIATE, result.handling)
        self.assertEqual("JP", result.region)
        self.assertGreaterEqual(result.confidence, 0.8)

    def test_default_news_is_digest_and_low_signal_is_archive(self):
        self.assertEqual(HANDLING_DIGEST, analyze_observation(observation("digest", "常规报道")).handling)
        item = observation("archive", "普通消息")
        item = item.__class__(**{
            **{field: getattr(item, field) for field in item.__dataclass_fields__},
            "importance": 1, "relevance": 1,
        })
        self.assertEqual(HANDLING_ARCHIVE, analyze_observation(item).handling)

    def test_advisory_model_cannot_escalate_to_immediate(self):
        baseline = analyze_observation(observation("model", "常规报道"))
        merged = merge_advisory_analysis(baseline, {
            "importance": 5, "urgency": 5, "confidence": 1.0,
            "handling": "immediate", "rationale": "模型建议",
        })
        self.assertNotEqual(HANDLING_IMMEDIATE, merged.handling)
        self.assertLessEqual(merged.importance, baseline.importance + 1)
