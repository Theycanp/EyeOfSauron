from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from argus.config import ConfigError, load_config

from helpers import PROJECT_ROOT


class ConfigTests(unittest.TestCase):
    def test_production_configuration_is_valid(self) -> None:
        config = load_config(PROJECT_ROOT / "config" / "argus.production.toml")
        self.assertEqual(4, len(config.sources))
        self.assertEqual("bloomberg_breaking", config.rules[0].id)
        self.assertEqual(3, config.sources[0].request_attempts)
        self.assertTrue(config.ntfy.enabled)
        self.assertFalse(config.digest.enabled)

    def test_unknown_key_is_rejected(self) -> None:
        source = (PROJECT_ROOT / "config" / "argus.example.toml").read_text()
        mutated = source.replace('log_level = "INFO"', 'log_level = "INFO"\nmagic = true')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.toml"
            path.write_text(mutated)
            with self.assertRaisesRegex(ConfigError, "unknown service keys"):
                load_config(path)

    def test_feed_host_must_be_allowlisted(self) -> None:
        source = (PROJECT_ROOT / "config" / "argus.example.toml").read_text()
        mutated = source.replace(
            'url = "https://feeds.bloomberg.com/markets/news.rss"',
            'url = "https://example.net/feed.xml"',
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.toml"
            path.write_text(mutated)
            with self.assertRaisesRegex(ConfigError, "host must be present"):
                load_config(path)

    def test_source_provenance_defaults_and_validation(self) -> None:
        config = load_config(PROJECT_ROOT / "config" / "argus.example.toml")
        source = config.sources[0]
        self.assertEqual("US", source.region)
        self.assertEqual("secondary", source.source_tier)
        self.assertEqual(3, source.default_importance)
        source_text = (PROJECT_ROOT / "config" / "argus.example.toml").read_text()
        mutated = source_text.replace('region = "US"', 'region = "JP"').replace(
            'source_tier = "secondary"', 'source_tier = "unknown"'
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.toml"
            path.write_text(mutated)
            with self.assertRaisesRegex(ConfigError, "source_tier is invalid"):
                load_config(path)

    def test_digest_policy_is_disabled_by_default_and_validates_public_url(self) -> None:
        config = load_config(PROJECT_ROOT / "config" / "argus.example.toml")
        self.assertFalse(config.digest.enabled)
        self.assertEqual("Asia/Shanghai", config.digest.timezone)
        source = (PROJECT_ROOT / "config" / "argus.example.toml").read_text()
        mutated = source.replace('[digest]\nenabled = false',
                                 '[digest]\nenabled = true').replace(
            'public_base_url = "https://eos.example.com"',
            'public_base_url = "http://eos.example.com"',
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.toml"
            path.write_text(mutated)
            with self.assertRaisesRegex(ConfigError, "HTTPS"):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
