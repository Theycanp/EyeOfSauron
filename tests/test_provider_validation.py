from __future__ import annotations

import unittest

from argus.provider_validation import validate_provider_configuration
from argus.providers import ProviderConfigError


class ProviderValidationTests(unittest.TestCase):
    def validate(self, kind, settings, **kwargs):
        options = dict(kind=kind, settings=settings, url=None, allowed_hosts=(),
                       enabled=True, max_response_bytes=2097152, location="sources[0].settings")
        options.update(kwargs)
        return validate_provider_configuration(**options)

    def market(self):
        return {"symbols": [" aapl ", "MSFT", "AAPL"],
                "api_base_url": "https://data.alpaca.markets",
                "api_key_env": "ALPACA_API_KEY", "api_secret_env": "ALPACA_API_SECRET"}

    def x(self):
        return {"user_id": "12345", "bearer_token_env": "X_BEARER_TOKEN"}

    def imap(self):
        return {"host": "imap.example.com", "username_env": "MAIL_USERNAME",
                "password_env": "MAIL_PASSWORD"}

    def test_market_normalizes_symbols_and_defaults_without_reading_credentials(self) -> None:
        result = self.validate("market", self.market())
        self.assertEqual(["AAPL", "MSFT"], result.settings["symbols"])
        self.assertEqual(1800, result.settings["cooldown_seconds"])
        self.assertEqual("/v2/stocks/{symbol}/snapshot", result.settings["path_template"])
        self.assertEqual("ALPACA_API_SECRET", result.credential_refs["api_secret_env"])

    def test_api_origins_cannot_leak_credentials_to_unapproved_hosts(self) -> None:
        for kind, settings in (("market", self.market()), ("x", self.x())):
            for invalid in ("http://api.x.com", "https://user:pass@api.x.com", "https://api.x.com?token=secret",
                            "https://api.x.com#secret", "https://unapproved.example", 123):
                with self.subTest(kind=kind, invalid=invalid), self.assertRaises(ProviderConfigError):
                    self.validate(kind, {**settings, "api_base_url": invalid})
        approved = self.validate("market", {**self.market(), "api_base_url": "https://feed.example"},
                                 allowed_hosts=("feed.example",))
        self.assertEqual("https://feed.example", approved.settings["api_base_url"])

    def test_market_rejects_invalid_symbols_templates_thresholds_and_flags(self) -> None:
        invalid_values = {
            "symbols": [[], [""], ["AAPL"] * 501, [123]],
            "path_template": ["https://data.alpaca.markets/{symbol}", "/{symbol}/{symbol}",
                              "/stocks", "/{symbol}?token=secret", "/{symbol}#fragment"],
            "price_change_threshold": [True, "5", float("nan"), float("inf"), 0, 1001],
            "volume_multiplier": [0], "gap_threshold": [-1],
            "cooldown_seconds": [True, 1, 86401],
            "max_quote_age_seconds": [0, 604801], "require_quote_timestamp": [1, "true"],
            "api_key_env": ["literal-secret", ""], "api_secret_env": [123, "has spaces"],
        }
        for field, invalid in invalid_values.items():
            for value in invalid:
                with self.subTest(field=field, value=value), self.assertRaises(ProviderConfigError):
                    self.validate("market", {**self.market(), field: value})

    def test_x_paging_defaults_and_valid_explicit_options(self) -> None:
        default = self.validate("x", self.x())
        self.assertEqual("https://api.x.com/2", default.settings["api_base_url"])
        self.assertEqual(5, default.settings["max_pages_per_poll"])
        self.assertTrue(default.settings["exclude_replies"])
        custom = self.validate("x", {**self.x(), "api_base_url": "https://api.twitter.com/2",
                                    "max_pages_per_poll": 100, "exclude_replies": False})
        self.assertFalse(custom.settings["exclude_replies"])
        for field, value in (("user_id", "someone"), ("user_id", 123), ("max_pages_per_poll", 0),
                             ("max_pages_per_poll", 101), ("exclude_replies", "false")):
            with self.subTest(field=field, value=value), self.assertRaises(ProviderConfigError):
                self.validate("x", {**self.x(), field: value})

    def test_mailbox_validation_bounds_memory_and_batch_size(self) -> None:
        result = self.validate("imap", self.imap())
        self.assertEqual(993, result.settings["port"])
        self.assertEqual(2097152, result.settings["max_message_bytes"])
        self.assertEqual(100, result.settings["batch_size"])
        self.assertEqual("MAIL_PASSWORD", result.credential_refs["password_env"])
        for field, value in (("host", "bad host"), ("host", ""), ("port", 65536),
                             ("batch_size", 501), ("batch_size", False), ("max_message_bytes", 512),
                             ("max_message_bytes", 10485761), ("mailbox", 3)):
            with self.subTest(field=field, value=value), self.assertRaises(ProviderConfigError):
                self.validate("imap", {**self.imap(), field: value})

    def test_host_probes_reject_malformed_endpoints_and_unbounded_thresholds(self) -> None:
        result = self.validate("host", {"paths": ["/"], "units": ["argus.service"],
                                        "required_listen_ports": [22, "tcp:18080"]})
        self.assertEqual(90, result.settings["disk_used_percent"])
        self.assertGreaterEqual(result.settings["load1"], 1)
        for field, value in (("paths", "/"), ("units", [1]), ("required_listen_ports", [True]),
                             ("allowed_listen_ports", [{}]), ("disk_used_percent", 0),
                             ("inode_used_percent", 101), ("memory_used_percent", float("nan")),
                             ("load1", float("inf"))):
            with self.subTest(field=field, value=value), self.assertRaises(ProviderConfigError):
                self.validate("host", {field: value})

    def test_feed_allowlist_and_unconfigured_experimental_sources_fail_closed(self) -> None:
        for url, allowed in ((None, ("example.com",)), ("https://example.com/feed", ())):
            with self.subTest(url=url), self.assertRaises(ProviderConfigError):
                self.validate("rss", {}, url=url, allowed_hosts=allowed)
        self.validate("rss", {}, url="https://example.com/feed", allowed_hosts=("example.com",))
        self.validate("youtube", {"channel_id": "UC123"})
        with self.assertRaises(ProviderConfigError):
            self.validate("youtube", {"channel_id": "   "})
        for kind in ("mqtt", "heartbeat"):
            with self.subTest(kind=kind), self.assertRaises(ProviderConfigError):
                self.validate(kind, {})
            self.validate(kind, {}, enabled=False)
        self.assertEqual({}, self.validate("market", {}, enabled=False).credential_refs)

    def test_optional_rss_content_age_requires_bounded_integer(self) -> None:
        options = dict(url="https://example.com/feed", allowed_hosts=("example.com",))
        self.assertEqual(0, self.validate("rss", {}, **options).settings["max_content_age_seconds"])
        for value in (0, 86400, 31536000):
            self.assertEqual(value, self.validate("rss", {"max_content_age_seconds": value}, **options).settings["max_content_age_seconds"])
        for value in (-1, True, "86400", 31536001):
            for enabled in (False, True):
                with self.subTest(value=value, enabled=enabled), self.assertRaises(ProviderConfigError):
                    self.validate("rss", {"max_content_age_seconds": value}, enabled=enabled, **options)
