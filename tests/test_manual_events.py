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
from unittest.mock import patch

from argus.admin import ManagedConfigStore, make_handler
from argus.auth import AdminAuth
from argus.database import Database
from argus.digest import DigestBuilder
from argus.manual_events import ManualEventError, ManualEventSpec, parse_manual_event


NOW = 1_788_363_000


class ManualEventValidationTests(unittest.TestCase):
    def test_parser_normalizes_a_valid_event(self) -> None:
        event = parse_manual_event(
            {
                "title": "  Policy update  ",
                "summary": " Full details.\nSecond line. ",
                "importance": 4,
                "region": "jp",
                "topic": "Politics",
                "source_url": "https://example.com/report?edition=1",
            }
        )
        self.assertEqual("Policy update", event.title)
        self.assertEqual("Full details.\nSecond line.", event.summary)
        self.assertEqual("JP", event.region)
        self.assertEqual("politics", event.topic)

    def test_parser_rejects_invalid_fields_and_unsafe_links(self) -> None:
        base = {"title": "Event", "summary": "Details"}
        invalid = (
            ({**base, "importance": True}, "importance"),
            ({**base, "importance": 6}, "importance"),
            ({**base, "title": "x" * 181}, "title"),
            ({**base, "summary": "x" * 5001}, "summary"),
            ({**base, "region": "x"}, "region"),
            ({**base, "topic": "not a topic"}, "topic"),
            ({**base, "source_url": "http://example.com"}, "HTTPS"),
            ({**base, "source_url": "https://user:pass@example.com"}, "credentials"),
            ({**base, "source_url": "https://example.com/report#secret"}, "fragment"),
        )
        for payload, message in invalid:
            with self.subTest(payload=payload), self.assertRaisesRegex(ManualEventError, message):
                parse_manual_event(payload)


class ManualEventPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "state.db")

    def tearDown(self) -> None:
        self.database.close()
        self.temp.cleanup()

    def test_creation_atomically_persists_observation_incident_and_alert(self) -> None:
        event = ManualEventSpec(
            "Policy update",
            "The complete event summary.",
            4,
            "JP",
            "politics",
            "https://example.com/report",
        )
        detail = self.database.create_manual_event(event, "operator", NOW, "eos")

        self.assertEqual("pending", detail["alert"]["status"])
        self.assertEqual(4, detail["alert"]["priority"])
        self.assertEqual("https://example.com/report", detail["alert"]["source_url"])
        self.assertEqual("recorded", detail["incident"]["status"])
        self.assertEqual(["manual"], detail["incident"]["source_ids"])
        observation = detail["observation"]
        self.assertEqual("manual", observation["source_id"])
        self.assertEqual("manual_entry", detail["documents"][0]["source_method"])
        self.assertEqual(event.summary, detail["documents"][0]["body"])
        self.assertEqual("analyzed", self.database.list_observations(NOW, NOW + 1)[0]["processing_state"])
        self.assertEqual("immediate", observation["handling"])
        self.assertEqual("operator", observation["attributes"]["actor"])

        digest = DigestBuilder(self.database).build(
            digest_key="daily:2026-09-10",
            period_start=NOW,
            period_end=NOW + 1,
            timezone="UTC",
            created_at=NOW + 1,
        )
        self.assertEqual(1, len(digest.items))
        self.assertEqual("Policy update", digest.items[0].title)

    def test_failed_notification_insert_rolls_back_the_observation(self) -> None:
        event = ManualEventSpec("Event", "Details", 3, "GLOBAL", "general")
        with patch.object(self.database, "_insert_alert", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "could not be queued"):
                self.database.create_manual_event(event, "operator", NOW, "eos")
        self.assertEqual([], self.database.list_observations(NOW, NOW + 1))
        self.assertEqual([], self.database.list_incidents())
        self.assertEqual([], self.database.list_alerts())


class ManualEventHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        ready: queue.Queue[HTTPServer] = queue.Queue()

        def run_server() -> None:
            database = Database(self.root / "state.db")
            auth = AdminAuth(database)
            for username, role in (("owner", "admin"), ("operator", "operator"), ("reader", "viewer")):
                database.create_admin_user(
                    username,
                    username.title(),
                    auth.hash_password(f"{username}-password-value"),
                    role,
                    "bootstrap",
                    NOW,
                )
            store = ManagedConfigStore(self.root / "managed.json", database=database)
            server = HTTPServer(
                ("127.0.0.1", 0),
                make_handler(store, database, None, notification_topic="custom-topic"),
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
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def login(self, username: str) -> tuple[str, str]:
        request = urllib.request.Request(
            f"{self.base_url}/api/auth/login",
            data=json.dumps(
                {"username": username, "password": f"{username}-password-value"}
            ).encode(),
            headers={"Content-Type": "application/json", "Origin": self.base_url},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            cookies = response.headers.get_all("Set-Cookie")
        values = "; ".join(item.split(";", 1)[0] for item in cookies)
        csrf = next(
            item.split("=", 1)[1]
            for item in values.split("; ")
            if item.startswith("__Host-eos_csrf=")
        )
        return values, csrf

    def post(self, payload: dict[str, object], cookies: str, csrf: str) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"{self.base_url}/api/events",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Cookie": cookies,
                "Origin": self.base_url,
                "X-CSRF-Token": csrf,
            },
            method="POST",
        )
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    def test_operator_can_create_but_viewer_cannot(self) -> None:
        payload = {
            "title": "Manual test",
            "summary": "This exercises the complete notification path.",
            "importance": 5,
            "region": "US",
            "topic": "markets",
            "source_url": "https://example.com/test",
        }
        operator = self.login("operator")
        status, detail = self.post(payload, *operator)
        self.assertEqual(201, status)
        self.assertEqual("manual", detail["observation"]["source_id"])
        self.assertEqual("pending", detail["alert"]["status"])

        database = Database(self.root / "state.db")
        try:
            self.assertEqual("custom-topic", database.list_alerts()[0]["topic"])
        finally:
            database.close()

        reader = self.login("reader")
        self.assertEqual(403, self.post(payload, *reader)[0])

    def test_validation_and_csrf_still_protect_the_endpoint(self) -> None:
        owner = self.login("owner")
        payload = {"title": "Bad link", "summary": "Details", "source_url": "http://example.com"}
        self.assertEqual(400, self.post(payload, *owner)[0])

        request = urllib.request.Request(
            f"{self.base_url}/api/events",
            data=json.dumps({"title": "No CSRF", "summary": "Details"}).encode(),
            headers={"Content-Type": "application/json", "Cookie": owner[0], "Origin": self.base_url},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as failure:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(403, failure.exception.code)


if __name__ == "__main__":
    unittest.main()
