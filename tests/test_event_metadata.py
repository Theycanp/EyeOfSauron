import unittest

from argus.news_rollout import plan_event_metadata


class EventMetadataTests(unittest.TestCase):
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
