from __future__ import annotations

import unittest
from dataclasses import replace

from argus.config import PatternConfig, load_config
from argus.rules import RuleSet

from helpers import PROJECT_ROOT, observation


NOW = 1788363000


class RuleTests(unittest.TestCase):
    def setUp(self) -> None:
        config = load_config(PROJECT_ROOT / "config" / "argus.production.toml")
        self.rules = RuleSet.from_config(config.rules, config.ntfy.default_topic)

    def test_major_central_bank_action_matches(self) -> None:
        item = observation(
            "fed-cut",
            "Federal Reserve Makes Emergency Rate Cut as Markets Plunge 8%",
            "Officials acted after an unexpected market shock.",
        )
        alerts = self.rules.evaluate(item, NOW)
        self.assertEqual(1, len(alerts))
        self.assertEqual(5, alerts[0].priority)
        self.assertEqual("eos", alerts[0].topic)

    def test_routine_market_wrap_does_not_match(self) -> None:
        item = observation("wrap", "Stocks Rise as Buyers Return: Markets Wrap")
        self.assertEqual((), self.rules.evaluate(item, NOW))

    def test_podcast_hypothesis_is_suppressed(self) -> None:
        item = observation("podcast", "Podcast: Why Markets Could Crash 10% This Year")
        self.assertEqual((), self.rules.evaluate(item, NOW))

    def test_explicit_breaking_marker_matches(self) -> None:
        item = observation("breaking", "Breaking: Prime Minister Resigns")
        self.assertEqual(1, len(self.rules.evaluate(item, NOW)))

    def test_old_item_does_not_match(self) -> None:
        item = observation("old", "Breaking: Prime Minister Resigns", timestamp=NOW - 86400)
        self.assertEqual((), self.rules.evaluate(item, NOW))

    def test_dedupe_key_is_stable_across_sections(self) -> None:
        first = observation("same-guid", "Breaking: Prime Minister Resigns")
        second = observation(
            "same-guid",
            "Breaking: Prime Minister Resigns",
            source_id="bloomberg_politics",
        )
        first_alert = self.rules.evaluate(first, NOW)[0]
        second_alert = self.rules.evaluate(second, NOW)[0]
        self.assertEqual(first_alert.dedupe_key, second_alert.dedupe_key)

    def test_unicode_titles_keep_distinct_incident_identity(self) -> None:
        first = self.rules.evaluate(observation("cn-1", "Breaking: 中国央行紧急行动"), NOW)[0]
        second = self.rules.evaluate(observation("cn-2", "Breaking: 日本央行紧急行动"), NOW)[0]
        self.assertNotEqual(first.incident_key, second.incident_key)

    def test_central_bank_event_identity_is_shared_across_rules_and_headlines(self) -> None:
        official_rule = replace(
            self.rules.rules[0].config, id="fed_announcements", source_ids=("fed_monetary",),
            patterns=(PatternConfig("official", ".", 10, 10),), priority=4,
        )
        rules = RuleSet.from_config((official_rule, self.rules.rules[0].config), "eos")
        official = rules.evaluate(
            observation(
                "fed-statement",
                "Federal Reserve issues FOMC statement",
                source_id="fed_monetary",
            ),
            NOW,
        )[0]
        media = self.rules.evaluate(
            observation(
                "fed-report",
                "Fed Raises Rates as Warsh Bucks Trump to Contain Inflation",
                "The Federal Reserve raised interest rates by a quarter percentage point.",
            ),
            NOW,
        )[0]
        self.assertNotEqual(official.rule_id, media.rule_id)
        # A concrete direction is a useful update to a generic announcement.
        self.assertNotEqual(official.incident_key, media.incident_key)
        other_media = self.rules.evaluate(observation("ft", "Federal Reserve raises interest rates"), NOW)[0]
        self.assertEqual(media.incident_key, other_media.incident_key)

    def test_distinct_central_banks_keep_distinct_incident_identity(self) -> None:
        fed = self.rules.evaluate(
            observation("fed", "Federal Reserve raises interest rates"), NOW
        )[0]
        ecb = self.rules.evaluate(
            observation("ecb", "European Central Bank raises interest rates"), NOW
        )[0]
        self.assertNotEqual(fed.incident_key, ecb.incident_key)

    def test_explicit_domain_identity_has_priority_over_semantic_news(self) -> None:
        first = replace(observation("explicit-1", "Federal Reserve raises interest rates"), attributes={"incident_key": "check-a", "stateful": True})
        second = replace(first, external_id="explicit-2", attributes={"incident_key": "check-b", "stateful": True})
        left = self.rules.evaluate(first, NOW)[0]
        right = self.rules.evaluate(second, NOW)[0]
        self.assertNotEqual(left.incident_key, right.incident_key)
        self.assertFalse(str(left.incident_key).startswith("news-event:"))

    def test_jma_republication_uses_persistent_weather_identity(self) -> None:
        jma_rule = replace(
            self.rules.rules[0].config,
            id="jma_critical",
            source_ids=("japan_meteorological_agency_high_frequency",),
            threshold=8,
            patterns=(PatternConfig("重大灾害", r"特別警報", 8, 4),),
        )
        rules = RuleSet.from_config((jma_rule,), "eos")
        first = rules.evaluate(
            observation(
                "jma-1",
                "【東京都土砂災害警報・注意報】【特別警報（土砂災害）】大島に特別警報を発表しています。",
                source_id="japan_meteorological_agency_high_frequency",
            ),
            NOW,
        )[0]
        second = rules.evaluate(
            observation(
                "jma-2",
                "【東京都気象特別警報報知】【特別警報（土砂災害）】東京都に特別警報を発表しました。",
                source_id="japan_meteorological_agency_high_frequency",
            ),
            NOW + 3600,
        )[0]
        self.assertEqual(first.incident_key, second.incident_key)
        self.assertTrue(first.incident_key.startswith("weather-event:"))


if __name__ == "__main__":
    unittest.main()
