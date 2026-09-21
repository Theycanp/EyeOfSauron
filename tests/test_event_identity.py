from __future__ import annotations

import unittest

from argus.event_identity import identify_semantic_event, identify_weather_event


class EventIdentityTests(unittest.TestCase):
    def test_jma_bulletin_variants_share_warning_identity(self) -> None:
        first = identify_weather_event(
            "【東京都土砂災害警報・注意報】【特別警報（土砂災害）】大島に特別警報を発表しています。",
            source_id="japan_meteorological_agency_high_frequency",
        )
        second = identify_weather_event(
            "【東京都気象特別警報報知】【特別警報（土砂災害）】東京都に特別警報を発表しました。",
            source_id="japan_meteorological_agency_high_frequency",
        )
        assert first is not None and second is not None
        self.assertEqual(first.notification_key(), second.notification_key())

    def test_jma_escalation_or_area_expansion_gets_new_identity(self) -> None:
        warning = identify_weather_event(
            "【東京都土砂災害警報・注意報】大島に土砂災害警報を発表しています。",
            source_id="japan_meteorological_agency_high_frequency",
        )
        special = identify_weather_event(
            "【東京都気象特別警報報知】【特別警報（土砂災害）】東京都に特別警報を発表しました。",
            source_id="japan_meteorological_agency_high_frequency",
        )
        assert warning is not None and special is not None
        self.assertNotEqual(warning.notification_key(), special.notification_key())

    def test_weather_identity_is_limited_to_jma_source(self) -> None:
        self.assertIsNone(identify_weather_event("大島に特別警報", source_id="other"))

    def test_jma_clearance_is_a_new_notification_state(self) -> None:
        active = identify_weather_event(
            "【東京都土砂災害警報・注意報】【特別警報（土砂災害）】大島に特別警報を発表しています。",
            source_id="japan_meteorological_agency_high_frequency",
        )
        cleared = identify_weather_event(
            "【東京都土砂災害警報・注意報】大島の土砂災害特別警報を解除しました。",
            source_id="japan_meteorological_agency_high_frequency",
        )
        assert active is not None and cleared is not None
        self.assertNotEqual(active.notification_key(), cleared.notification_key())

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
