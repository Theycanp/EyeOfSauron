from __future__ import annotations

import unittest
from typing import Any

from argus.event_identity import (
    event_occurrences_compatible, identify_event_occurrence, identify_semantic_event,
    identify_weather_event,
)


class EventIdentityTests(unittest.TestCase):
    def test_usgs_official_id_survives_publication_and_title_changes(self) -> None:
        first = {
            "source_id": "usgs_earthquakes_significant_month",
            "external_id": "urn:earthquake-usgs-gov:us:6000txpi",
            "published_at": 1_800_000_000, "title": "M 6.2 - First report",
        }
        updated = {**first, "published_at": 1_800_500_000, "title": "M 6.3 - Revised report"}
        first_identity = identify_event_occurrence(first)
        updated_identity = identify_event_occurrence(updated)
        assert first_identity is not None and updated_identity is not None
        self.assertEqual("official_id", first_identity.basis)
        self.assertEqual(first_identity.key, updated_identity.key)
        self.assertTrue(event_occurrences_compatible(first_identity, updated_identity))
        self.assertFalse(event_occurrences_compatible(first_identity, identify_event_occurrence({
            **updated, "external_id": "urn:earthquake-usgs-gov:us:6000txpj",
        })))
        self.assertIsNone(event_occurrences_compatible(first_identity, None))

    def test_feed_guid_is_not_an_occurrence_id(self) -> None:
        self.assertIsNone(identify_event_occurrence({
            "source_id": "generic_rss", "source_tier": "secondary",
            "external_id": "urn:earthquake-usgs-gov:us:6000txpi",
            "published_at": 1_800_000_000,
        }))

    def test_usgs_network_is_part_of_official_occurrence_id(self) -> None:
        report = {
            "source_id": "usgs_earthquakes_significant_month",
            "external_id": "urn:earthquake-usgs-gov:ci:6000txpi",
        }
        identity = identify_event_occurrence(report)
        other_network = identify_event_occurrence({
            **report, "external_id": "urn:earthquake-usgs-gov:us:6000txpi",
        })
        assert identity is not None and other_network is not None
        self.assertNotEqual(identity.key, other_network.key)
        self.assertFalse(event_occurrences_compatible(identity, other_network))

    def test_structured_occurrence_requires_explicit_primary_evidence(self) -> None:
        report: dict[str, Any] = {
            "source_id": "official-one", "source_tier": "primary", "published_at": 1_800_500_000,
            "attributes": {"event_identity": {
                "kind": "earthquake", "entity_ids": ["fault:one"], "location_id": "jp:tohoku",
                "occurred_at": 1_800_000_000, "time_precision": "second",
            }},
        }
        identity = identify_event_occurrence(report)
        assert identity is not None
        self.assertEqual(1_800_000_000, identity.occurred_at)
        self.assertEqual("structured", identity.basis)
        later_identity = identify_event_occurrence({
            **report, "published_at": 1_801_000_000, "source_id": "official-two",
        })
        assert later_identity is not None
        self.assertEqual(identity.key, later_identity.key)
        self.assertIsNone(identify_event_occurrence({**report, "source_tier": "secondary"}))
        self.assertIsNone(identify_event_occurrence({
            **report, "attributes": {"event_identity": {
                **report["attributes"]["event_identity"], "occurred_at": None,
            }},
        }))
        self.assertIsNone(identify_event_occurrence({
            **report, "attributes": {"event_identity": {
                **report["attributes"]["event_identity"], "time_precision": [],
            }},
        }))

    def test_explicit_official_id_requires_matching_primary_namespace(self) -> None:
        report = {
            "source_id": "official-one", "source_tier": "primary",
            "attributes": {"event_identity": {
                "kind": "policy-decision", "namespace": "official-one", "external_id": "meeting-7",
            }},
        }
        self.assertIsNotNone(identify_event_occurrence(report))
        self.assertIsNone(identify_event_occurrence({**report, "source_id": "generic-rss"}))
        self.assertIsNone(identify_event_occurrence({**report, "source_tier": "secondary"}))

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
