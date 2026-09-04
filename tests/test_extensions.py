from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import urllib.request
import threading
from http.server import HTTPServer
from pathlib import Path

from signalwatch.adapters import build_collector
from signalwatch.admin import AdminError, ManagedConfigStore, make_handler
from signalwatch.database import Database
from signalwatch.market import MarketCollector
from signalwatch.host import HostHealthCollector
from unittest.mock import patch
from signalwatch.models import SourceState
from signalwatch.mqtt import CommandPolicy, CommandRequest, MqttError, SensorNormalizer

from helpers import PROJECT_ROOT
from signalwatch.config import load_config


class _Provider:
    def __init__(self, values):
        self.values = values

    def latest(self, symbol):
        return self.values[symbol]


class ExtensionTests(unittest.TestCase):
    def test_market_collector_baselines_then_reports_move(self) -> None:
        config = load_config(PROJECT_ROOT / "config" / "signalwatch.example.toml")
        source = config.sources[0]
        source = source.__class__(
            id="stocks", kind="market", publisher="Test", section="Watchlist", dedupe_scope="market",
            poll_interval_seconds=300, request_timeout_seconds=5, request_attempts=1,
            retry_base_seconds=1, max_response_bytes=1024, enabled=True,
            settings={"symbols": ["ABC"], "price_change_threshold": 5, "cooldown_seconds": 60},
        )
        collector = MarketCollector(source, _Provider({"ABC": {"price": 100, "volume": 10, "timestamp": "1"}}))
        first = collector.fetch(SourceState("stocks", True, None, None, None, None, 0, False, None))
        self.assertTrue(first.not_modified)
        collector.provider = _Provider({"ABC": {"price": 110, "volume": 10, "timestamp": "2"}})
        second = collector.fetch(SourceState("stocks", True, None, None, None, None, 0, False, None, first.cursor))
        self.assertEqual(1, len(second.observations))
        self.assertIn("ABC", second.observations[0].title)

    def test_managed_store_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ManagedConfigStore(Path(directory) / "managed.json")
            source = {
                "id": "stocks", "kind": "market", "publisher": "Test", "section": "Watchlist",
                "dedupe_scope": "market", "enabled": False, "poll_interval_seconds": 300,
                "request_timeout_seconds": 5, "request_attempts": 1, "retry_base_seconds": 1,
                "max_response_bytes": 1024, "settings": {"symbols": ["ABC"], "api_key_env": "KEY"},
            }
            store.upsert("source", source)
            loaded = store.read()
            self.assertEqual("KEY", loaded["sources"][0]["settings"]["api_key_env"])

    def test_managed_store_rejects_embedded_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ManagedConfigStore(Path(directory) / "managed.json")
            source = {
                "id": "mail", "kind": "imap", "publisher": "Mail", "section": "Inbox",
                "dedupe_scope": "mail", "poll_interval_seconds": 300, "request_timeout_seconds": 5,
                "request_attempts": 1, "retry_base_seconds": 1, "max_response_bytes": 1024,
                "settings": {"host": "mail.example", "username_env": "MAIL_USER", "password": "leak"},
            }
            with self.assertRaises(AdminError):
                store.upsert("source", source)

    def test_admin_revision_and_metrics_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = load_config(PROJECT_ROOT / "config" / "signalwatch.example.toml")
            database = Database(root / "state.db")
            store = ManagedConfigStore(root / "managed.json", {source.id for source in config.sources}, {rule.id for rule in config.rules})
            server = HTTPServer(("127.0.0.1", 0), make_handler(store, database, "token"))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/api/health"
                request = urllib.request.Request(url, headers={"Authorization": "Bearer token"})
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(200, response.status)
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/api/validate",
                    data=json.dumps({"sources": [], "rules": []}).encode(),
                    headers={"Authorization": "Bearer token", "Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request) as response:
                    self.assertTrue(json.loads(response.read())["valid"])
            finally:
                server.shutdown()
                server.server_close()
                database.close()

    def test_sensor_normalizer_and_command_policy(self) -> None:
        event = SensorNormalizer().parse("home/cat_feeder/state", b'{"value":"ok","unit":"state"}', observed_at=10)
        self.assertEqual("cat_feeder", event.device_id)
        policy = CommandPolicy({"cat_feeder": {"dispense": {"required": ["grams"]}}})
        request = CommandRequest("cat_feeder", "dispense", {"grams": 20}, "once", 100)
        self.assertTrue(policy.validate(request, now=10))
        with self.assertRaises(MqttError):
            policy.validate(CommandRequest("cat_feeder", "power", {}, "bad", 100), now=10)

    def test_host_collector_has_cursor_and_recovery_contract(self) -> None:
        source = load_config(PROJECT_ROOT / "config" / "signalwatch.example.toml").sources[0]
        source = source.__class__(
            id="host_health", kind="host", publisher="bk", section="Host", dedupe_scope="host",
            poll_interval_seconds=300, request_timeout_seconds=5, request_attempts=1,
            retry_base_seconds=1, max_response_bytes=1024, enabled=True,
            settings={"paths": ["/tmp"], "units": [], "disk_used_percent": 0},
        )
        result = HostHealthCollector(source).fetch(SourceState("host_health", True, None, None, None, None, 0, False, None))
        self.assertTrue(result.cursor)
        self.assertTrue(result.observations)

    def test_host_collector_detects_unexpected_and_missing_listen_ports(self) -> None:
        source = load_config(PROJECT_ROOT / "config" / "signalwatch.example.toml").sources[0]
        source = source.__class__(
            id="host_ports", kind="host", publisher="bk", section="Host", dedupe_scope="host",
            poll_interval_seconds=300, request_timeout_seconds=5, request_attempts=1,
            retry_base_seconds=1, max_response_bytes=1024, enabled=True,
            settings={"paths": ["/tmp"], "units": [], "allowed_listen_ports": [22], "required_listen_ports": [22, 443]},
        )
        with patch("signalwatch.host._listen_ports", return_value={("tcp", 22), ("tcp", 8080)}):
            result = HostHealthCollector(source).fetch(SourceState("host_ports", True, None, None, None, None, 0, False, None))
        titles = {item.title for item in result.observations}
        self.assertTrue(any("8080" in title for title in titles))
        self.assertTrue(any("443" in title for title in titles))

    def test_disabled_source_factory_returns_none(self) -> None:
        config = load_config(PROJECT_ROOT / "config" / "signalwatch.example.toml")
        source = config.sources[0]
        source = source.__class__(
            id=source.id, kind=source.kind, publisher=source.publisher, section=source.section,
            dedupe_scope=source.dedupe_scope, poll_interval_seconds=source.poll_interval_seconds,
            request_timeout_seconds=source.request_timeout_seconds, request_attempts=source.request_attempts,
            retry_base_seconds=source.retry_base_seconds, max_response_bytes=source.max_response_bytes,
            url=source.url, allowed_hosts=source.allowed_hosts, enabled=False, settings=source.settings,
        )
        self.assertIsNone(build_collector(source))

    def test_schema_one_database_migrates_without_losing_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            connection = sqlite3.connect(path)
            connection.executescript("""
                CREATE TABLE observations (id INTEGER PRIMARY KEY);
                CREATE TABLE alerts (
                    id INTEGER PRIMARY KEY, observation_id INTEGER, rule_id TEXT, dedupe_key TEXT,
                    topic TEXT, title TEXT, message TEXT, priority INTEGER, tags_json TEXT,
                    click_url TEXT, status TEXT, attempts INTEGER DEFAULT 0, next_attempt_at INTEGER,
                    lease_until INTEGER, last_error TEXT, created_at INTEGER, delivered_at INTEGER
                );
                CREATE TABLE collector_state (
                    source_id TEXT PRIMARY KEY, initialized INTEGER DEFAULT 0, etag TEXT,
                    last_modified TEXT, last_attempt_at INTEGER, last_success_at INTEGER,
                    consecutive_failures INTEGER DEFAULT 0, outage_alerted INTEGER DEFAULT 0,
                    outage_started_at INTEGER, last_error TEXT
                );
                INSERT INTO collector_state(source_id, initialized) VALUES ('legacy', 1);
                PRAGMA user_version=1;
            """)
            connection.close()
            database = Database(path)
            self.assertEqual(3, database.status()["database_schema"])
            self.assertTrue(database.get_source_state("legacy").initialized)
            columns = {row[1] for row in database.connection.execute("PRAGMA table_info(alerts)")}
            self.assertIn("confidence", columns)
            database.close()


if __name__ == "__main__":
    unittest.main()
