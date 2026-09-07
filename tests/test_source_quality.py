import unittest

from argus.source_quality import SourceQualityPolicy, calculate_quality


class SourceQualityTests(unittest.TestCase):
    def test_short_term_feedback_does_not_change_weight(self):
        profile = calculate_quality(
            "source",
            [{"signal": -1, "created_at": 100}],
            now=100,
        )
        self.assertEqual(1.0, profile.weight)
        self.assertFalse(profile.eligible)

    def test_evidence_requires_samples_and_span(self):
        rows = [{"signal": -1, "created_at": i * 86400} for i in range(91)]
        profile = calculate_quality("source", rows[:30], now=90 * 86400)
        self.assertFalse(profile.eligible)
        profile = calculate_quality("source", rows, now=90 * 86400)
        self.assertTrue(profile.eligible)
        self.assertLess(profile.weight, 1.0)

    def test_refreshes_do_not_consume_rate_limit(self):
        policy = SourceQualityPolicy()
        rows = [{"signal": -1, "created_at": i * 86400} for i in range(91)]
        first = calculate_quality("source", rows, now=90 * 86400, policy=policy)
        second = calculate_quality(
            "source", rows, now=90 * 86400 + 1, policy=policy,
            current_weight=first.weight, updated_at=90 * 86400,
        )
        self.assertEqual(first.weight, second.weight)

    def test_manual_override_is_bounded(self):
        profile = calculate_quality("source", [], now=100, manual_override=2.0)
        self.assertEqual(1.15, profile.weight)
        self.assertTrue(profile.eligible)


if __name__ == "__main__":
    unittest.main()
