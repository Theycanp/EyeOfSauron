from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from signalwatch.config import ConfigError, load_config

from helpers import PROJECT_ROOT


class ConfigTests(unittest.TestCase):
    def test_production_configuration_is_valid(self) -> None:
        config = load_config(PROJECT_ROOT / "config" / "signalwatch.production.toml")
        self.assertEqual(4, len(config.sources))
        self.assertEqual("bloomberg_breaking", config.rules[0].id)
        self.assertEqual(3, config.sources[0].request_attempts)
        self.assertTrue(config.ntfy.enabled)

    def test_unknown_key_is_rejected(self) -> None:
        source = (PROJECT_ROOT / "config" / "signalwatch.example.toml").read_text()
        mutated = source.replace('log_level = "INFO"', 'log_level = "INFO"\nmagic = true')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.toml"
            path.write_text(mutated)
            with self.assertRaisesRegex(ConfigError, "unknown service keys"):
                load_config(path)

    def test_feed_host_must_be_allowlisted(self) -> None:
        source = (PROJECT_ROOT / "config" / "signalwatch.example.toml").read_text()
        mutated = source.replace(
            'url = "https://feeds.bloomberg.com/markets/news.rss"',
            'url = "https://example.net/feed.xml"',
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.toml"
            path.write_text(mutated)
            with self.assertRaisesRegex(ConfigError, "host must be present"):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
