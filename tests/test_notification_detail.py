from __future__ import annotations

import json
import queue
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path

from argus.admin import ManagedConfigStore, make_handler
from argus.config import ConfigError, load_config
from argus.database import Database
from argus.models import FeedFetchResult, OutboxMessage
from argus.notifier import NtfyNotifier, NotifyError
from argus.rules import RuleSet

from helpers import PROJECT_ROOT, observation, production_config


NOW = 1_788_363_000


class _Response:
    status = 200

    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, exc_type, exc, traceback):  # type: ignore[no-untyped-def]
        return None

    def read(self, amount: int) -> bytes:
        return b"{}"


class _Opener:
    def __init__(self) -> None:
        self.request = None

    def open(self, request, timeout):  # type: ignore[no-untyped-def]
        self.request = request
        return _Response()


class NotificationLinkTests(unittest.TestCase):
    def test_ntfy_routes_main_click_to_eos_and_keeps_original_article_action(self) -> None:
        alert = OutboxMessage(
            id=17,
            topic="eos",
            title="Bloomberg breaking",
            message="Saved RSS summary",
            priority=5,
            tags=("warning",),
            click_url="https://www.bloomberg.com/news/articles/test",
            attempts=1,
            observation_id=42,
            incident_id=7,
        )
        notifier = NtfyNotifier(
            "https://ntfy.example", "private-token", 10, "https://eos.example:10008"
        )
        opener = _Opener()
        notifier._opener = opener
        notifier.publish(alert)
        assert opener.request is not None
        payload = json.loads(opener.request.data)
        self.assertEqual("Saved RSS summary", payload["message"])
        self.assertEqual("https://eos.example:10008/events/17", payload["click"])
        self.assertEqual("查看原文", payload["actions"][0]["label"])
        self.assertEqual(alert.click_url, payload["actions"][0]["url"])

    def test_notification_without_event_context_keeps_its_existing_click(self) -> None:
        alert = OutboxMessage(
            id=18,
            topic="eos",
            title="Digest",
            message="Digest summary",
            priority=3,
            tags=("newspaper",),
            click_url="https://eos.example:10008/digests/today",
            attempts=1,
        )
        notifier = NtfyNotifier(
            "https://ntfy.example", "private-token", 10, "https://eos.example:10008"
        )
        opener = _Opener()
        notifier._opener = opener
        notifier.publish(alert)
        payload = json.loads(opener.request.data)
        self.assertEqual(alert.click_url, payload["click"])
        self.assertNotIn("actions", payload)

    def test_unsafe_notification_detail_base_is_rejected(self) -> None:
        with self.assertRaisesRegex(NotifyError, "detail base URL"):
            NtfyNotifier("https://ntfy.example", "token", 10, "http://eos.example")

    def test_admin_public_base_is_validated_independently(self) -> None:
        config = load_config(PROJECT_ROOT / "config" / "argus.production.toml")
        self.assertEqual("https://eos.juggler.cc:10008", config.admin.public_base_url)
        source = (PROJECT_ROOT / "config" / "argus.example.toml").read_text()
        for invalid, message in (
            ("http://eos.example.com", "HTTPS"),
            ("https://eos.example.com/admin", "origin"),
            ("https://eos.example.com?next=publisher", "origin"),
        ):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as directory:
                mutated = source.replace(
                    'public_base_url = "https://eos.example.com"',
                    f'public_base_url = "{invalid}"',
                    1,
                )
                path = Path(directory) / "bad.toml"
                path.write_text(mutated)
                with self.assertRaisesRegex(ConfigError, message):
                    load_config(path)


class NotificationDetailPersistenceTests(unittest.TestCase):
    def test_detail_joins_the_exact_observation_and_incident(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = production_config(Path(directory))
            database = Database(config.service.database_path)
            try:
                rules = RuleSet.from_config(config.rules, config.ntfy.default_topic)
                database.record_source_success(
                    "bloomberg_markets",
                    FeedFetchResult(
                        (observation("baseline", "Ordinary market article"),), None, None
                    ),
                    rules,
                    NOW,
                    config.ntfy.default_topic,
                )
                database.record_source_success(
                    "bloomberg_markets",
                    FeedFetchResult(
                        (observation(
                            "new-breaking",
                            "Breaking: Prime Minister Resigns",
                            "Government officials confirmed the change.",
                        ),),
                        None,
                        None,
                    ),
                    rules,
                    NOW + 1,
                    config.ntfy.default_topic,
                )
                alert_id = int(database.list_alerts(status="pending")[0]["id"])
                detail = database.get_alert_detail(alert_id)
                self.assertIsNotNone(detail)
                assert detail is not None
                self.assertNotIn("dedupe_key", detail["alert"])
                self.assertEqual(
                    "Government officials confirmed the change.",
                    detail["observation"]["summary"],
                )
                self.assertEqual(
                    "https://www.bloomberg.com/news/articles/new-breaking",
                    detail["alert"]["source_url"],
                )
                self.assertEqual("recorded", detail["incident"]["status"])
                self.assertEqual(alert_id, database.list_incidents()[0]["latest_alert_id"])
            finally:
                database.close()


class NotificationDetailHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        ready: queue.Queue = queue.Queue()

        def run_server() -> None:
            database = Database(self.root / "state.db")
            database.enqueue_test_alert("eos", NOW)
            store = ManagedConfigStore(self.root / "managed.json", database=database)
            server = HTTPServer(
                ("127.0.0.1", 0), make_handler(store, database, "test-token")
            )
            ready.put(server)
            try:
                server.serve_forever(poll_interval=0.01)
            finally:
                server.server_close()
                database.close()

        self.thread = threading.Thread(target=run_server, daemon=True)
        self.thread.start()
        self.server = ready.get(timeout=30)
        self.database = Database(self.root / "state.db")
        self.alert_id = int(self.database.list_alerts()[0]["id"])

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.database.close()
        self.temp.cleanup()

    def request(self, path: str, token: str = "test-token") -> tuple[int, dict]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}{path}",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            response = urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    def test_detail_endpoint_requires_auth_and_handles_missing_ids(self) -> None:
        status, payload = self.request(f"/api/alerts/{self.alert_id}")
        self.assertEqual(200, status)
        self.assertEqual(self.alert_id, payload["alert"]["id"])
        self.assertIsNone(payload["observation"])
        self.assertIsNone(payload["incident"])
        self.assertEqual(400, self.request("/api/alerts/not-a-number")[0])
        self.assertEqual(404, self.request("/api/alerts/999999")[0])
        self.assertEqual(401, self.request(f"/api/alerts/{self.alert_id}", "wrong")[0])

    def test_event_deep_link_serves_the_spa_before_login(self) -> None:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.server.server_port}/events/{self.alert_id}",
            timeout=3,
        ) as response:
            self.assertEqual(200, response.status)
            self.assertIn(b'<div id="app"></div>', response.read())
