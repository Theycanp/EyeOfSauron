from __future__ import annotations

import json
import unittest

from argus.adapters import build_collector
from argus.config import parse_source_config
from argus.news_catalog import (
    IntegrationMode,
    NEWS_SOURCE_CATALOG,
    NewsCatalogError,
)
from argus.providers import (
    DEFAULT_PROVIDER_REGISTRY,
    ProviderSpec,
    ProviderTestStrategy,
    SettingField,
    SourceCapability,
    TestMode as ProviderTestMode,
    UrlMode,
)


class ProviderRegistryTests(unittest.TestCase):
    def test_registry_describes_capabilities_credentials_and_test_strategy(self) -> None:
        kinds = set(DEFAULT_PROVIDER_REGISTRY.kinds)
        self.assertEqual(
            {"rss", "youtube", "x", "imap", "market", "host", "mqtt", "heartbeat"},
            kinds,
        )
        market = DEFAULT_PROVIDER_REGISTRY.require("market")
        self.assertIn(SourceCapability.MARKET_QUOTES, market.capabilities)
        self.assertEqual(
            {"api_key_env", "api_secret_env"},
            {credential.setting_name for credential in market.credentials},
        )
        self.assertEqual(ProviderTestMode.READ_ONLY_API, market.test_strategy.mode)
        self.assertTrue(market.test_strategy.side_effect_free)
        mqtt = DEFAULT_PROVIDER_REGISTRY.require("mqtt")
        self.assertFalse(mqtt.runtime_collector)
        self.assertEqual(ProviderTestMode.CONFIG_ONLY, mqtt.test_strategy.mode)

    def test_provider_description_is_json_safe_and_marks_secret_references(self) -> None:
        raw_descriptions = DEFAULT_PROVIDER_REGISTRY.describe()
        json.dumps(raw_descriptions)
        descriptions = {item["kind"]: item for item in raw_descriptions}
        x_fields = {field["name"]: field for field in descriptions["x"]["settings"]}
        self.assertTrue(x_fields["bearer_token_env"]["secret_reference"])
        self.assertNotIn("value", descriptions["x"]["credentials"][0])
        self.assertEqual("derived", descriptions["youtube"]["url_mode"])

    def test_registry_can_be_extended_without_modifying_core_dispatch(self) -> None:
        custom = ProviderSpec(
            "custom",
            "Custom provider",
            frozenset({SourceCapability.FEED_ITEMS}),
            UrlMode.NONE,
            (SettingField("endpoint", "string", required_when_enabled=True),),
            (),
            ProviderTestStrategy(
                ProviderTestMode.CONFIG_ONLY,
                side_effect_free=True,
                requires_credentials=False,
                description="test provider",
            ),
        )
        registry = DEFAULT_PROVIDER_REGISTRY.extend(custom)
        raw_source = {
            "id": "custom_source",
            "kind": "custom",
            "publisher": "Test",
            "section": "Test",
            "dedupe_scope": "custom",
            "enabled": True,
            "poll_interval_seconds": 60,
            "request_timeout_seconds": 5,
            "request_attempts": 1,
            "retry_base_seconds": 1,
            "max_response_bytes": 4096,
            "settings": {"endpoint": "read-only"},
        }
        source = parse_source_config(raw_source, provider_registry=registry)
        with self.assertRaisesRegex(ValueError, "endpoint is required"):
            parse_source_config(
                {**raw_source, "settings": {}},
                provider_registry=registry,
            )
        marker = object()
        collector = build_collector(
            source,
            provider_registry=registry,
            factories={"custom": lambda _source: marker},
        )
        self.assertIs(marker, collector)
        self.assertNotIn("custom", DEFAULT_PROVIDER_REGISTRY.kinds)


class NewsCatalogTests(unittest.TestCase):
    def test_every_entry_is_disabled_confirmation_gated_and_metadata_only(self) -> None:
        rows = NEWS_SOURCE_CATALOG.describe()
        self.assertGreaterEqual(len(rows), 9)
        for row in rows:
            self.assertFalse(row["default_enabled"])
            self.assertTrue(row["requires_user_confirmation"])
            self.assertEqual(
                "feed_metadata_and_original_link_only", row["content_policy"]
            )
            for feed in row["feeds"]:
                self.assertTrue(feed["url"].startswith("https://"))
                self.assertIn(feed["url"].split("/", 3)[2], feed["allowed_hosts"])
                source_id = f"catalog_{row['id']}_{feed['id']}"
                parsed = parse_source_config(
                    NEWS_SOURCE_CATALOG.source_template(
                        row["id"], feed["id"], source_id
                    )
                )
                self.assertFalse(parsed.enabled)

    def test_catalog_covers_requested_publishers_without_inventing_public_feeds(self) -> None:
        for entry_id in (
            "bloomberg",
            "wall_street_journal",
            "economist",
            "financial_times",
            "reuters",
            "associated_press",
            "federal_reserve",
            "sec",
            "ecb",
        ):
            self.assertIsNotNone(NEWS_SOURCE_CATALOG.get(entry_id))
        self.assertEqual((), NEWS_SOURCE_CATALOG.require("reuters").feeds)
        self.assertEqual((), NEWS_SOURCE_CATALOG.require("associated_press").feeds)
        self.assertEqual(
            IntegrationMode.LICENSED_PROVIDER,
            NEWS_SOURCE_CATALOG.require("reuters").integration_mode,
        )

    def test_verified_template_requires_confirmation_only_when_enabling(self) -> None:
        disabled = NEWS_SOURCE_CATALOG.source_template(
            "wall_street_journal", "world", "wsj_world"
        )
        parsed = parse_source_config(disabled)
        self.assertFalse(parsed.enabled)
        self.assertEqual("feeds.a.dj.com", parsed.allowed_hosts[0])
        self.assertEqual(7 * 86400, parsed.settings["max_content_age_seconds"])
        self.assertIn("2025-01-27", NEWS_SOURCE_CATALOG.require("wall_street_journal").notes)
        with self.assertRaisesRegex(NewsCatalogError, "explicit user confirmation"):
            NEWS_SOURCE_CATALOG.source_template(
                "wall_street_journal",
                "world",
                "wsj_world",
                enabled=True,
            )
        enabled = NEWS_SOURCE_CATALOG.source_template(
            "wall_street_journal",
            "world",
            "wsj_world",
            enabled=True,
            user_confirmed=True,
        )
        self.assertTrue(parse_source_config(enabled).enabled)

    def test_licensed_source_cannot_be_misrepresented_as_rss(self) -> None:
        with self.assertRaisesRegex(NewsCatalogError, "dedicated authorized adapter"):
            NEWS_SOURCE_CATALOG.user_url_template(
                "reuters",
                "reuters_licensed",
                "https://example.reuters.com/feed.xml",
                ("example.reuters.com",),
                user_confirmed=True,
            )


if __name__ == "__main__":
    unittest.main()
