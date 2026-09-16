from __future__ import annotations

import unittest

from argus.config import ConfigError, parse_source_config
from argus.digest import _observation_score
from argus.regions import normalize_region, region_family, region_weight


class RegionTaxonomyTests(unittest.TestCase):
    def test_legacy_codes_remain_valid_and_map_to_macro_families(self) -> None:
        self.assertEqual("CN", normalize_region("cn"))
        self.assertEqual("EAST_ASIA", region_family("CN"))
        self.assertEqual("EAST_ASIA", region_family("JP"))
        self.assertEqual("NORTH_AMERICA", region_family("US"))

    def test_macro_aliases_are_canonicalized(self) -> None:
        self.assertEqual("AUSTRALIA_OCEANIA", normalize_region("australia"))
        self.assertEqual("SOUTHEAST_ASIA", normalize_region("south-east-asia"))

    def test_configured_legacy_weight_overrides_macro_default(self) -> None:
        self.assertEqual(5, region_weight({"CN": 5}, "CN"))
        self.assertEqual(5, region_weight({"CN": 5}, "EAST_ASIA"))
        self.assertEqual(4, region_weight({"EUROPE": 4}, "EUROPE"))

    def test_macro_source_config_is_accepted(self) -> None:
        raw = {
            "id": "europe_source",
            "kind": "rss",
            "publisher": "European source",
            "section": "World",
            "dedupe_scope": "europe_source",
            "url": "https://example.test/feed.xml",
            "allowed_hosts": ["example.test"],
            "poll_interval_seconds": 300,
            "request_timeout_seconds": 20,
            "request_attempts": 2,
            "retry_base_seconds": 2,
            "max_response_bytes": 2097152,
            "region": "EUROPE",
        }
        source = parse_source_config(raw)
        self.assertEqual("EUROPE", source.region)

    def test_unknown_region_is_rejected(self) -> None:
        raw = {
            "id": "bad_region",
            "kind": "rss",
            "publisher": "Example",
            "section": "World",
            "dedupe_scope": "bad_region",
            "url": "https://example.test/feed.xml",
            "allowed_hosts": ["example.test"],
            "poll_interval_seconds": 300,
            "request_timeout_seconds": 20,
            "request_attempts": 2,
            "retry_base_seconds": 2,
            "max_response_bytes": 2097152,
            "region": "ATLANTIS",
        }
        with self.assertRaisesRegex(ConfigError, "region is invalid"):
            parse_source_config(raw)

    def test_digest_score_uses_macro_family_for_legacy_country(self) -> None:
        item = {
            "importance": 3,
            "urgency": 2,
            "relevance": 3,
            "confidence": 0.5,
            "region": "JP",
            "source_tier": "secondary",
        }
        east_asia = _observation_score(item, {"EAST_ASIA": 5})
        europe = _observation_score({**item, "region": "EUROPE"}, {"EUROPE": 4})
        self.assertGreater(east_asia, europe)


if __name__ == "__main__":
    unittest.main()
