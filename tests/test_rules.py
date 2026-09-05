from __future__ import annotations

import unittest

from argus.config import load_config
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


if __name__ == "__main__":
    unittest.main()
