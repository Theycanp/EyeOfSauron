from __future__ import annotations

import unittest

from argus.event_identity import identify_semantic_event


class EventIdentityTests(unittest.TestCase):
    def test_predictions_comparisons_and_generic_policy_are_not_decisions(self) -> None:
        for title in (
            "Fed could cut rates later this year", "Fed expected to raise rates at FOMC meeting",
            "Fed will raise rates tomorrow", "Fed monetary policy faces scrutiny",
            "ECB raises rates while Fed cuts rates", "央行宣布加息", "Fed cut rates last year",
            "Preview: Fed issues FOMC statement tomorrow",
            "Fed raises concerns as unemployment rates spike",
        ):
            with self.subTest(title=title):
                self.assertIsNone(identify_semantic_event(title))

    def test_background_does_not_manufacture_a_decision_identity(self) -> None:
        self.assertIsNone(identify_semantic_event("Oil prices fall", "The Federal Reserve raised rates."))

    def test_market_reaction_uses_actual_rate_direction_not_market_verb(self) -> None:
        identity = identify_semantic_event("Treasuries Hold Gains as Fed Hikes Rates, Signaling More Ahead")
        assert identity is not None
        self.assertEqual("raise", identity.action)
        self.assertEqual("context", identity.relation_hint)

    def test_date_window_and_opposite_directions_are_hard_boundaries(self) -> None:
        raised = identify_semantic_event("Fed raises interest rates")
        cut = identify_semantic_event("Fed cuts interest rates")
        official = identify_semantic_event("Federal Reserve issues FOMC statement")
        assert raised is not None and cut is not None and official is not None
        instant = 1_789_581_600
        self.assertTrue(raised.compatible(official, instant, instant + 60))
        self.assertFalse(raised.compatible(cut, instant, instant + 60))
        self.assertFalse(raised.compatible(raised, instant, instant + 21601))
        self.assertFalse(raised.compatible(raised, instant, instant + 86400))
        self.assertNotEqual(raised.notification_key(instant), official.notification_key(instant))
        self.assertNotEqual(raised.notification_key(instant), cut.notification_key(instant))


if __name__ == "__main__":
    unittest.main()
