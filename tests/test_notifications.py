from __future__ import annotations

import json
import queue
import runpy
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from dataclasses import asdict, replace
from http.server import HTTPServer
from pathlib import Path

from argus.admin import AdminError, ManagedConfigStore, make_handler
from argus.auth import AdminAuth
from argus.config import ConfigError, load_config
from argus.database import Database
from argus.models import AlertCandidate
from argus.notifications import (
    CATEGORIES, NotificationRoutingService, parse_notification_policy, public_subscription_base_url,
    standard_notification_policy,
)
from argus.persistence import RevisionConflictError
from argus.reminders import parse_reminder
from argus.weather import ForecastHour, WeatherForecast


TOPICS = tuple(f"eos-{category}" for category in CATEGORIES)


def candidate(category: str, **kwargs: object) -> AlertCandidate:
    value = AlertCandidate("test.rule", f"test:{category}", "Test", "Message", 3, (), "", category=category, topic="eos-news")
    return replace(value, **kwargs)


class NotificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database = Database(self.root / "state.db")
        self.store = ManagedConfigStore(self.root / "managed.json", database=self.database, notification_topics=TOPICS)
        self.policy = asdict(standard_notification_policy())
        self.policy["destinations"] = list(self.policy["destinations"])
        self.store.set_notifications(self.policy, "owner", expected_revision=0)
        self.router = NotificationRoutingService(self.database, allowed_topics=TOPICS, default_topic="eos-news")
        self.database.configure_notification_router(self.router)

    def tearDown(self) -> None:
        self.database.close()
        self.directory.cleanup()

    def test_categories_and_unknown_fallback(self) -> None:
        for category in CATEGORIES:
            with self.subTest(category=category):
                route = self.router.resolve(candidate(category))
                self.assertEqual(f"eos-{category}", route.topic)
                self.assertEqual(1, route.revision)
        self.assertEqual("eos-news", self.router.resolve(candidate("future-kind")).topic)

    def test_explicit_rule_override_including_default_topic(self) -> None:
        self.policy["routes"]["news"] = "system"
        self.store.set_notifications(self.policy, "owner", expected_revision=1)
        self.assertEqual("eos-system", self.router.resolve(candidate("news")).topic)
        self.assertEqual("eos-news", self.router.resolve(candidate("news", topic_override=True)).topic)
        with self.assertRaises(ValueError):
            self.router.resolve(candidate("news", topic="private-other-topic", topic_override=True))

    def test_mute_retains_incident_and_does_not_enqueue(self) -> None:
        self.policy["routes"]["system"] = None
        self.store.set_notifications(self.policy, "owner", expected_revision=1)
        value = candidate("system", incident_key="source:test", incident_kind="stateful")
        with self.database.unit_of_work():
            self.assertFalse(self.database._insert_alert(value, None, 100))
        self.assertEqual([], self.database.list_alerts())
        self.assertEqual("open", self.database.list_incidents()[0]["status"])
        # Deliberate admin tests are independent of normal-category muting.
        test_id = self.database.enqueue_notification_test("eos-weather", "owner", 100)
        self.assertEqual("eos-weather", self.database.get_alert_detail(test_id)["alert"]["topic"])

    def test_outbox_destination_and_revision_are_frozen(self) -> None:
        with self.database.unit_of_work():
            self.database._insert_alert(candidate("weather"), None, 100)
        self.policy["routes"]["weather"] = "system"
        self.store.set_notifications(self.policy, "owner", expected_revision=1)
        message = self.database.claim_due_alert(101, 60)
        assert message is not None
        self.assertEqual(("eos-weather", "weather", 1), (message.topic, message.category, message.routing_revision))
        self.database.mark_retry(message.id, 102, "temporary")
        retry = self.database.claim_due_alert(102, 60)
        assert retry is not None
        self.assertEqual("eos-weather", retry.topic)
        self.assertEqual("eos-system", self.router.resolve(candidate("weather")).topic)

    def test_business_entrypoints_use_the_same_router(self) -> None:
        reminder = parse_reminder({"title": "Reminder", "message": "Remember", "schedule_kind": "once",
                                   "run_at": 200, "timezone": "UTC"}, 100)
        self.database.upsert_reminder(reminder, "owner", 100)
        self.database.enqueue_due_reminders(200, "eos-news")
        subscription = self.database.get_weather_subscription()
        at = 1790679600
        forecast = WeatherForecast(at, 20, 2, tuple(ForecastHour(at + index * 3600, 20, 0, 0, 10) for index in range(30)))
        self.database.record_weather_forecast(subscription, forecast, now=at, topic="eos-news", click_url="")
        self.database.enqueue_weather_test(topic="eos-news", click_url="", now=at + 1)
        for now in (202, 203, 204):
            self.database.record_weather_failure(subscription, "temporary", now, topic="eos-news", click_url="")
        self.database.enqueue_digest_failure_notification(digest_key="daily:2026-09-29", streak=3,
                                                         error="temporary", topic="eos-news", click_url="", now=205)
        topics = {row["rule_id"]: row["topic"] for row in self.database.list_alerts()}
        self.assertEqual("eos-reminders", topics["reminder.manual"])
        self.assertEqual("eos-weather", topics["weather.test"])
        self.assertEqual("eos-system", topics["weather.provider_outage"])
        self.assertEqual("eos-system", topics["digest.ai_failure"])

    def test_file_config_round_trip_includes_routing_and_analysis(self) -> None:
        store = ManagedConfigStore(self.root / "file-managed.json", notification_topics=TOPICS)
        store.set_notifications(self.policy, "owner", expected_revision=0)
        store.set_analysis({"enabled": False}, "owner", expected_revision=1)
        store.set_digest({"enabled": False}, "owner", expected_revision=2)
        loaded = ManagedConfigStore(self.root / "file-managed.json", notification_topics=TOPICS)
        self.assertEqual(self.policy, loaded.read()["notifications"])
        loaded.rollback(2, "owner", expected_revision=3)
        self.assertEqual({"enabled": False}, loaded.read()["analysis"])
        self.assertEqual(self.policy, loaded.read()["notifications"])

    def test_permissions_references_and_revision_conflict(self) -> None:
        bad = json.loads(json.dumps(self.policy))
        bad["destinations"][0]["topic"] = "another-app"
        with self.assertRaises(AdminError):
            self.store.set_notifications(bad, "owner", expected_revision=1)
        bad = json.loads(json.dumps(self.policy))
        bad["destinations"] = bad["destinations"][1:]
        with self.assertRaises(AdminError):
            self.store.set_notifications(bad, "owner", expected_revision=1)
        self.store.set_notifications(self.policy, "owner", expected_revision=1)
        with self.assertRaises(RevisionConflictError):
            self.store.set_notifications(self.policy, "stale-editor", expected_revision=1)

    def test_other_config_edits_preserve_policy_and_rollback(self) -> None:
        self.store.set_analysis({"enabled": False}, "owner", expected_revision=1)
        self.assertEqual(self.policy, self.store.read()["notifications"])
        self.policy["routes"]["weather"] = None
        self.store.set_notifications(self.policy, "owner", expected_revision=2)
        self.store.rollback(1, "owner", expected_revision=3)
        self.assertEqual("eos-weather", self.router.resolve(candidate("weather")).topic)

    def test_invalid_policy_shapes(self) -> None:
        for changes in ({"routes": {}}, {"fallback": "missing"}, {"destinations": []}, {"destinations": self.policy["destinations"] * 2}, {"extra": True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                parse_notification_policy({**self.policy, **changes})

    def test_subscription_urls_never_export_embedded_credentials(self) -> None:
        for value in ("https://user:password@example.com", "https://example.com?token=fake", "http://127.0.0.1:10009", "https://example.com:invalid"):
            self.assertEqual("", public_subscription_base_url(value))
        self.assertEqual("https://ntfy.example.com:10008", public_subscription_base_url("https://ntfy.example.com:10008/"))

    def test_configuration_validates_authorized_topics(self) -> None:
        example = Path(__file__).resolve().parents[1] / "config/argus.example.toml"
        config = load_config(example)
        self.assertEqual("eos-news", config.ntfy.default_topic)
        self.assertIsNotNone(config.notifications)
        bad = json.loads(json.dumps(self.policy))
        bad["destinations"][0]["topic"] = "another-app"
        with self.assertRaises(ConfigError):
            load_config(example, managed_override={"notifications": bad})

    def test_migration_preserves_modules_and_refuses_overwriting_policy(self) -> None:
        path = Path(__file__).resolve().parents[1] / "scripts/operations/activate_notification_routing.py"
        plan = runpy.run_path(str(path))["plan_migration"]
        current = {"sources": [], "rules": [{"id": "old", "topic": "eos"}, {"id": "custom", "topic": "eos-system"}],
                   "analysis": {"enabled": True}, "digest": {"enabled": True}}
        planned = plan(current, "eos")
        self.assertEqual("eos", current["rules"][0]["topic"])
        self.assertNotIn("topic", planned["rules"][0])
        self.assertEqual("eos-system", planned["rules"][1]["topic"])
        self.assertEqual(current["analysis"], planned["analysis"])
        self.assertEqual(current["digest"], planned["digest"])
        with self.assertRaises(ValueError):
            plan(planned, "eos")

    def test_schema28_upgrade_preserves_old_messages(self) -> None:
        self.database.notification_router = None
        self.database.enqueue_test_alert("eos", 100)
        self.database.close()
        path = self.root / "state.db"
        with sqlite3.connect(path) as connection:
            connection.execute("ALTER TABLE alerts DROP COLUMN notification_category")
            connection.execute("ALTER TABLE alerts DROP COLUMN routing_revision")
            connection.execute("DROP INDEX alerts_topic_id_idx")
            connection.execute("PRAGMA user_version=28")
        self.database = Database(path)
        row = self.database.list_alerts()[0]
        self.assertEqual(("eos", "pending", "system", None), (row["topic"], row["status"], row["notification_category"], row["routing_revision"]))
        self.assertEqual("ok", self.database.connection.execute("PRAGMA quick_check").fetchone()[0])


class NotificationHttpTests(unittest.TestCase):
    def test_cookie_csrf_permissions_and_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ready: queue.Queue[HTTPServer] = queue.Queue()

            def run_server() -> None:
                database = Database(root / "state.db")
                auth = AdminAuth(database)
                for name, role in (("owner", "admin"), ("operator", "operator"), ("reader", "viewer")):
                    database.create_admin_user(name, name, auth.hash_password("test-password-value"), role, "bootstrap", 100)
                store = ManagedConfigStore(root / "managed.json", database=database, notification_topics=TOPICS)
                routing = NotificationRoutingService(database, allowed_topics=TOPICS, default_topic="eos-news", base_policy=standard_notification_policy())
                database.configure_notification_router(routing)
                server = HTTPServer(("127.0.0.1", 0), make_handler(store, database, None, notification_routing=routing, notification_base_url="https://ntfy.example.com"))
                ready.put(server)
                try:
                    server.serve_forever()
                finally:
                    server.server_close(); database.close()

            thread = threading.Thread(target=run_server, daemon=True)
            thread.start()
            server = ready.get(timeout=30)
            base_url = f"http://127.0.0.1:{server.server_port}"

            def request(path: str, body: object | None = None, headers: dict[str, str] | None = None) -> tuple[int, dict]:
                req = urllib.request.Request(base_url + path, data=json.dumps(body).encode() if body is not None else None,
                                             headers={"Origin": base_url, "Content-Type": "application/json", **(headers or {})})
                try:
                    with urllib.request.urlopen(req) as response:
                        return response.status, json.loads(response.read())
                except urllib.error.HTTPError as exc:
                    return exc.code, json.loads(exc.read())

            def login(name: str) -> dict[str, str]:
                req = urllib.request.Request(base_url + "/api/auth/login", data=json.dumps({"username": name, "password": "test-password-value"}).encode(), headers={"Origin": base_url, "Content-Type": "application/json"})
                with urllib.request.urlopen(req) as response:
                    cookies = [value.split(";", 1)[0] for value in response.headers.get_all("Set-Cookie")]
                    csrf = next(value.split("=", 1)[1] for value in cookies if value.startswith("__Host-eos_csrf="))
                return {"Cookie": "; ".join(cookies), "X-CSRF-Token": csrf}

            try:
                self.assertEqual(401, request("/api/notifications")[0])
                policy = asdict(standard_notification_policy())
                for name in ("operator", "reader"):
                    headers = login(name)
                    self.assertEqual(200, request("/api/notifications", headers=headers)[0])
                    self.assertEqual(403, request("/api/notifications", policy, {**headers, "If-Match": '"0"'})[0])
                    self.assertEqual(403, request("/api/notifications/test", {"topic": "eos-news"}, headers)[0])
                owner = login("owner")
                self.assertEqual(403, request("/api/notifications", policy, {"Cookie": owner["Cookie"], "If-Match": '"0"'})[0])
                self.assertEqual(400, request("/api/notifications", policy, owner)[0])
                self.assertEqual(200, request("/api/notifications", policy, {**owner, "If-Match": '"0"'})[0])
                self.assertEqual(409, request("/api/notifications", policy, {**owner, "If-Match": '"0"'})[0])
                self.assertEqual(400, request("/api/notifications/test", {"topic": "another-app"}, owner)[0])
                status, result = request("/api/notifications/test", {"topic": "eos-news"}, owner)
                self.assertEqual(202, status)
                self.assertEqual("eos-news", request(f"/api/alerts/{result['alert_id']}", headers=owner)[1]["alert"]["topic"])
            finally:
                server.shutdown(); thread.join(timeout=5)
