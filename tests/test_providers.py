from __future__ import annotations

import json
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from dataclasses import replace

from argus.adapters import AdapterError, build_collector
from argus.database import Database
from argus.models import FeedFetchResult, SourceState
from argus.rules import RuleSet
from helpers import observation
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
            {"rss", "official_list", "youtube", "x", "imap", "market", "host", "mqtt", "heartbeat"},
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
        class ReplayCollector:
            def fetch(self, state: SourceState) -> FeedFetchResult:
                item = replace(
                    observation("first", "Ordinary provider observation"),
                    source_id=source.id, dedupe_scope=source.dedupe_scope,
                )
                return FeedFetchResult((item,), None, None, cursor="first")

        marker = ReplayCollector()
        collector = build_collector(
            source,
            provider_registry=registry,
            factories={"custom": lambda _source: marker},
        )
        self.assertIs(marker, collector)
        self.assertNotIn("custom", DEFAULT_PROVIDER_REGISTRY.kinds)
        with TemporaryDirectory() as directory:
            database = Database(Path(directory) / "state.db")
            try:
                rules = RuleSet.from_config((), "eos")
                state = database.get_source_state(source.id)
                result = collector.fetch(state)
                self.assertIsNone(database.get_source_state(source.id).cursor)
                first = database.record_source_success(source.id, result, rules, 100, "eos")
                self.assertTrue(first.baseline_created)
                self.assertEqual(1, first.inserted_observations)
                self.assertEqual(0, first.queued_alerts)
                restored = database.get_source_state(source.id)
                self.assertEqual("first", restored.cursor)
                second = database.record_source_success(
                    source.id, collector.fetch(restored), rules, 200, "eos"
                )
                self.assertEqual(0, second.inserted_observations)
            finally:
                database.close()
        with self.assertRaisesRegex(AdapterError, "implement fetch"):
            build_collector(source, provider_registry=registry, factories={"custom": lambda _: object()})


class NewsCatalogTests(unittest.TestCase):
    def test_every_entry_is_disabled_confirmation_gated_and_has_known_policy(self) -> None:
        rows = NEWS_SOURCE_CATALOG.describe()
        self.assertGreaterEqual(len(rows), 9)
        for row in rows:
            self.assertFalse(row["default_enabled"])
            self.assertTrue(row["requires_user_confirmation"])
            self.assertIn(
                row["content_policy"],
                {"feed_metadata_and_original_link_only", "public_document_full_text"},
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
            "bank_of_japan",
            "japan_meteorological_agency",
            "china_ndrc",
            "world_health_organization",
            "nasa",
            "usgs_earthquakes",
            "nvidia_newsroom",
            "us_embassy_china",
        ):
            self.assertIsNotNone(NEWS_SOURCE_CATALOG.get(entry_id))
        self.assertEqual((), NEWS_SOURCE_CATALOG.require("reuters").feeds)
        self.assertEqual((), NEWS_SOURCE_CATALOG.require("associated_press").feeds)
        self.assertEqual(
            IntegrationMode.LICENSED_PROVIDER,
            NEWS_SOURCE_CATALOG.require("reuters").integration_mode,
        )
        self.assertEqual("JP", NEWS_SOURCE_CATALOG.require("bank_of_japan").region)
        self.assertEqual(
            1,
            NEWS_SOURCE_CATALOG.require("japan_meteorological_agency").default_importance,
        )
        self.assertEqual("CN", NEWS_SOURCE_CATALOG.require("china_ndrc").region)
        self.assertEqual("primary", NEWS_SOURCE_CATALOG.require("world_health_organization").source_tier)
        nvidia = NEWS_SOURCE_CATALOG.require("nvidia_newsroom")
        self.assertEqual("US", nvidia.region)
        self.assertEqual("technology", nvidia.topic)
        self.assertEqual("https://nvidianews.nvidia.com/rss.xml", nvidia.feeds[0].url)
        embassy = NEWS_SOURCE_CATALOG.require("us_embassy_china")
        self.assertEqual(IntegrationMode.VERIFIED_RSS, embassy.integration_mode)
        self.assertEqual("https://china.usembassy-china.org.cn/category/alert/feed/", embassy.feeds[0].url)
        self.assertEqual(5, embassy.default_importance)

    def test_catalog_template_carries_information_policy(self) -> None:
        source = NEWS_SOURCE_CATALOG.source_template(
            "bank_of_japan", "whats_new", "boj_updates"
        )
        parsed = parse_source_config(source)
        self.assertEqual("JP", parsed.region)
        self.assertEqual("primary", parsed.source_tier)
        self.assertEqual(4, parsed.default_importance)
        self.assertEqual("policy", parsed.settings["topic"])

    def test_nvidia_newsroom_template_is_bounded_and_primary(self) -> None:
        raw = NEWS_SOURCE_CATALOG.source_template("nvidia_newsroom", "news", "nvidia_news")
        parsed = parse_source_config(raw)
        self.assertEqual("rss", parsed.kind)
        self.assertEqual(("nvidianews.nvidia.com", "blogs.nvidia.com"), parsed.allowed_hosts)
        self.assertEqual("US", parsed.region)
        self.assertEqual("primary", parsed.source_tier)
        self.assertFalse(parsed.enabled)

    def test_embassy_alert_template_is_bounded_and_primary(self) -> None:
        entry = NEWS_SOURCE_CATALOG.require("us_embassy_china")
        raw = NEWS_SOURCE_CATALOG.source_template(
            "us_embassy_china", "alerts", "us_embassy_china_alerts",
        )
        parsed = parse_source_config(raw)
        self.assertFalse(parsed.enabled)
        self.assertEqual(("china.usembassy-china.org.cn",), parsed.allowed_hosts)
        self.assertEqual("CN", parsed.region)
        self.assertEqual("primary", parsed.source_tier)
        self.assertEqual(5, entry.default_importance)

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
