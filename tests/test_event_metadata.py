import unittest

from argus.news_rollout import plan_disaster_signal_policy, plan_event_metadata


class EventMetadataTests(unittest.TestCase):
    def test_disaster_policy_demotes_jma_without_changing_global_earthquakes(self) -> None:
        original = {
            "sources": [
                {"id": "japan_meteorological_agency_high_frequency", "enabled": True,
                 "poll_interval_seconds": 300, "default_importance": 4},
                {"id": "usgs_earthquakes_significant_month", "enabled": True,
                 "poll_interval_seconds": 300, "default_importance": 5},
            ],
            "rules": [
                {"id": "official_news_critical", "source_ids": [
                    "japan_meteorological_agency_high_frequency",
                    "usgs_earthquakes_significant_month",
                ]},
                {"id": "other", "source_ids": ["japan_meteorological_agency_high_frequency"]},
            ],
        }
        planned, probes = plan_disaster_signal_policy(original)
        self.assertEqual(
            ["japan_meteorological_agency_high_frequency"],
            [source["id"] for source in probes],
        )
        self.assertEqual(300, original["sources"][0]["poll_interval_seconds"])
        self.assertEqual(21600, planned["sources"][0]["poll_interval_seconds"])
        self.assertEqual(1, planned["sources"][0]["default_importance"])
        self.assertEqual(
            "jma_exceptional_hazards",
            planned["sources"][0]["settings"]["entry_filter_profile"],
        )
        self.assertFalse(
            planned["sources"][0]["settings"]["notification_eligible"]
        )
        self.assertTrue(planned["sources"][0]["enabled"])
        self.assertEqual(original["sources"][1], planned["sources"][1])
        self.assertEqual(
            ["usgs_earthquakes_significant_month"], planned["rules"][0]["source_ids"]
        )
        self.assertEqual([], planned["rules"][1]["source_ids"])
        self.assertEqual(planned, plan_disaster_signal_policy(planned)[0])

    def test_fills_missing_verified_fed_fields_without_enabling_source(self) -> None:
        original = {"sources": [{"id": "fed_monetary", "url": "https://www.federalreserve.gov/feeds/press_monetary.xml", "enabled": False}]}
        planned, probes = plan_event_metadata(original)
        self.assertEqual("primary", planned["sources"][0]["source_tier"])
        self.assertFalse(planned["sources"][0]["enabled"])
        self.assertEqual([], probes)
        self.assertNotIn("source_tier", original["sources"][0])

    def test_preserves_explicit_choices_and_rejects_unverified_host(self) -> None:
        source = {"id": "fed_monetary", "url": "https://www.federalreserve.gov/feed", "source_tier": "secondary", "region": "GLOBAL", "default_importance": 2}
        original = {"sources": [source]}
        self.assertEqual((original, []), plan_event_metadata(original))
        source["url"] = "https://example.test/feed"
        del source["source_tier"]
        self.assertEqual((original, []), plan_event_metadata(original))
