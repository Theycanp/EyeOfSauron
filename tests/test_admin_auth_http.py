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
from argus.reminders import parse_reminder


class AdminAuthHttpTests(unittest.TestCase):
    def test_login_cookie_csrf_and_rbac(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ready: queue.Queue[tuple[HTTPServer, int, int]] = queue.Queue()

            def run_server() -> None:
                database = Database(root / "state.db")
                database.sync_source_runtime(
                    [("diagnostic_feed", "rss", True, 300)], {"diagnostic_feed"},
                    config_revision=1, now=100,
                )
                auth = AdminAuth(database)
                database.create_admin_user(
                    "owner", "Owner", auth.hash_password("correct-horse-battery"),
                    "admin", "bootstrap", 100,
                )
                database.create_admin_user(
                    "reader", "Reader", auth.hash_password("reader-password-value"),
                    "viewer", "bootstrap", 100,
                )
                reminder = parse_reminder({
                    "title": "确认测试", "message": "测试提醒内容", "schedule_kind": "once",
                    "run_at": 160, "timezone": "UTC", "ack_enabled": True,
                    "repeat_interval_seconds": 60,
                }, 100)
                database.upsert_reminder(reminder, "bootstrap", 100)
                database.enqueue_due_reminders(160, "eos")
                occurrence_id = int(database.connection.execute(
                    "SELECT id FROM reminder_occurrences WHERE reminder_id = ?",
                    (reminder.id,),
                ).fetchone()[0])
                store = ManagedConfigStore(root / "managed.json", database=database)
                server = HTTPServer(("127.0.0.1", 0), make_handler(store, database, "emergency"))
                ready.put((server, server.server_port, occurrence_id))
                try:
                    server.serve_forever()
                finally:
                    server.server_close()
                    database.close()

            thread = threading.Thread(target=run_server, daemon=True)
            thread.start()
            # Argon2id initialization intentionally performs a memory-hard hash
            # before the server is ready. Shared CI runners can take longer
            # than five seconds without indicating a functional failure.
            server, port, occurrence_id = ready.get(timeout=30)
            base_url = f"http://127.0.0.1:{port}"
            try:
                with urllib.request.urlopen(base_url) as response:
                    csp = response.headers["Content-Security-Policy"]
                    self.assertIn("https://a.tile.openstreetmap.org", csp)
                    self.assertIn("https://b.tile.openstreetmap.org", csp)
                    self.assertIn("https://c.tile.openstreetmap.org", csp)
                    self.assertNotIn("img-src *", csp)
                    content_length = response.headers["Content-Length"]
                with urllib.request.urlopen(urllib.request.Request(base_url, method="HEAD")) as response:
                    self.assertEqual(content_length, response.headers["Content-Length"])
                    self.assertEqual(b"", response.read())
                for icon_path in ("favicon.svg", "favicon.ico"):
                    with urllib.request.urlopen(f"{base_url}/{icon_path}") as response:
                        self.assertEqual("image/svg+xml", response.headers["Content-Type"])
                        self.assertIn(b"<svg", response.read())
                with self.assertRaises(urllib.error.HTTPError) as head_failure:
                    urllib.request.urlopen(urllib.request.Request(f"{base_url}/api/weather", method="HEAD"))
                self.assertEqual(401, head_failure.exception.code)
                self.assertEqual(b"", head_failure.exception.read())
                occurrence_url = f"{base_url}/api/reminders/occurrences/{occurrence_id}"
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(occurrence_url)
                self.assertEqual(401, failure.exception.code)
                anonymous_ack = urllib.request.Request(
                    f"{occurrence_url}/acknowledge", data=b'{}',
                    headers={"Content-Type": "application/json", "Origin": base_url}, method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(anonymous_ack)
                self.assertEqual(401, failure.exception.code)
                with self.assertRaises(urllib.error.HTTPError) as unauthenticated:
                    urllib.request.urlopen(f"{base_url}/api/source-health/diagnostic_feed")
                self.assertEqual(401, unauthenticated.exception.code)
                bad_login = urllib.request.Request(
                    f"{base_url}/api/auth/login",
                    data=json.dumps({"username": "owner", "password": "wrong-password-value"}).encode(),
                    headers={"Content-Type": "application/json", "Origin": base_url},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(bad_login)
                self.assertEqual(401, failure.exception.code)

                login = urllib.request.Request(
                    f"{base_url}/api/auth/login",
                    data=json.dumps({
                        "username": "owner", "password": "correct-horse-battery"
                    }).encode(),
                    headers={"Content-Type": "application/json", "Origin": base_url},
                    method="POST",
                )
                with urllib.request.urlopen(login) as response:
                    self.assertEqual("owner", json.loads(response.read())["user"]["username"])
                    cookies = response.headers.get_all("Set-Cookie")
                cookie_values = "; ".join(item.split(";", 1)[0] for item in cookies)
                csrf = next(
                    item.split("=", 1)[1]
                    for item in cookie_values.split("; ") if item.startswith("__Host-eos_csrf=")
                )
                with urllib.request.urlopen(urllib.request.Request(
                    occurrence_url, headers={"Cookie": cookie_values},
                )) as response:
                    self.assertEqual("确认测试", json.loads(response.read())["occurrence"]["title"])
                missing_ack_csrf = urllib.request.Request(
                    f"{occurrence_url}/acknowledge", data=b'{}',
                    headers={"Cookie": cookie_values, "Content-Type": "application/json", "Origin": base_url},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(missing_ack_csrf)
                self.assertEqual(403, failure.exception.code)
                valid_ack = urllib.request.Request(
                    f"{occurrence_url}/acknowledge", data=b'{}',
                    headers={"Cookie": cookie_values, "Content-Type": "application/json",
                             "Origin": base_url, "X-CSRF-Token": csrf}, method="POST",
                )
                with urllib.request.urlopen(valid_ack) as response:
                    self.assertIsNotNone(json.loads(response.read())["occurrence"]["acknowledged_at"])
                users = urllib.request.Request(
                    f"{base_url}/api/users", headers={"Cookie": cookie_values}
                )
                with urllib.request.urlopen(users) as response:
                    self.assertEqual(2, len(json.loads(response.read())["users"]))
                weather_request = urllib.request.Request(
                    f"{base_url}/api/weather", headers={"Cookie": cookie_values}
                )
                with urllib.request.urlopen(weather_request) as response:
                    weather = json.loads(response.read())
                self.assertEqual("北京邮电大学沙河校区", weather["subscription"]["label"])
                with patch("argus.admin.OpenMeteoProvider.resolve_timezone", return_value="Asia/Shanghai") as resolve:
                    with urllib.request.urlopen(urllib.request.Request(
                        f"{base_url}/api/weather/place-timezone?lat=40.156116&lon=116.283563",
                        headers={"Cookie": cookie_values},
                    )) as response:
                        self.assertEqual("Asia/Shanghai", json.loads(response.read())["timezone"])
                    resolve.assert_called_once_with(40.156116, 116.283563)
                for bad_query in ("lat=nan&lon=116", "lat=91&lon=116", "lat=40&lon=181",
                                  "lat=40&lat=41&lon=116", "lat=40", "lat=40&lon=116&extra=1"):
                    with self.assertRaises(urllib.error.HTTPError) as invalid_timezone:
                        urllib.request.urlopen(urllib.request.Request(
                            f"{base_url}/api/weather/place-timezone?{bad_query}",
                            headers={"Cookie": cookie_values},
                        ))
                    self.assertEqual(400, invalid_timezone.exception.code)
                with self.assertRaises(urllib.error.HTTPError) as denied_timezone:
                    urllib.request.urlopen(f"{base_url}/api/weather/place-timezone?lat=40&lon=116")
                self.assertEqual(401, denied_timezone.exception.code)
                weather_payload = {key: value for key, value in weather["subscription"].items() if key != "id"}
                weather_payload["daily_time"] = "08:00"
                weather_write = urllib.request.Request(
                    f"{base_url}/api/weather", data=json.dumps(weather_payload).encode(),
                    headers={"Cookie": cookie_values, "Content-Type": "application/json",
                             "Origin": base_url, "X-CSRF-Token": csrf}, method="POST",
                )
                with urllib.request.urlopen(weather_write) as response:
                    self.assertEqual("08:00", json.loads(response.read())["subscription"]["daily_time"])
                diagnostics = urllib.request.Request(
                    f"{base_url}/api/source-health/diagnostic_feed", headers={"Cookie": cookie_values}
                )
                with urllib.request.urlopen(diagnostics) as response:
                    health = json.loads(response.read())["health"]
                self.assertEqual("diagnostic_feed", health["source_id"])
                self.assertIsNone(health["polling"]["success_rate"])
                invalid_audit = urllib.request.Request(
                    f"{base_url}/api/source-quality/%20/audit", headers={"Cookie": cookie_values}
                )
                with self.assertRaises(urllib.error.HTTPError) as invalid:
                    urllib.request.urlopen(invalid_audit)
                self.assertEqual(400, invalid.exception.code)
                unknown = urllib.request.Request(
                    f"{base_url}/api/source-health/unknown", headers={"Cookie": cookie_values}
                )
                with self.assertRaises(urllib.error.HTTPError) as missing:
                    urllib.request.urlopen(unknown)
                self.assertEqual(404, missing.exception.code)
                missing_csrf = urllib.request.Request(
                    f"{base_url}/api/users",
                    data=b'{}', headers={
                        "Cookie": cookie_values,
                        "Content-Type": "application/json",
                        "Origin": base_url,
                    },
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(missing_csrf)
                self.assertEqual(403, failure.exception.code)
                weather_missing_csrf = urllib.request.Request(
                    f"{base_url}/api/weather", data=json.dumps(weather_payload).encode(),
                    headers={"Cookie": cookie_values, "Content-Type": "application/json", "Origin": base_url},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(weather_missing_csrf)
                self.assertEqual(403, failure.exception.code)
                valid_write = urllib.request.Request(
                    f"{base_url}/api/users",
                    data=json.dumps({
                        "username": "operator",
                        "display_name": "Operator",
                        "password": "operator-password-value",
                        "role": "operator",
                    }).encode(),
                    headers={
                        "Cookie": cookie_values,
                        "Content-Type": "application/json",
                        "Origin": base_url,
                        "X-CSRF-Token": csrf,
                    },
                    method="POST",
                )
                with urllib.request.urlopen(valid_write) as response:
                    self.assertEqual(201, response.status)
                cross_origin = urllib.request.Request(
                    f"{base_url}/api/users/2/revoke-sessions",
                    data=b'{}',
                    headers={
                        "Cookie": cookie_values,
                        "Content-Type": "application/json",
                        "Origin": "https://attacker.example",
                        "X-CSRF-Token": csrf,
                    },
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(cross_origin)
                self.assertEqual(403, failure.exception.code)
                session = urllib.request.Request(
                    f"{base_url}/api/auth/session",
                    headers={"Cookie": cookie_values, "X-CSRF-Token": csrf},
                )
                with urllib.request.urlopen(session) as response:
                    self.assertIn("users:manage", json.loads(response.read())["user"]["permissions"])

                logout = urllib.request.Request(
                    f"{base_url}/api/auth/logout",
                    data=b'{}',
                    headers={
                        "Cookie": cookie_values,
                        "Content-Type": "application/json",
                        "Origin": base_url,
                        "X-CSRF-Token": csrf,
                    },
                    method="POST",
                )
                with urllib.request.urlopen(logout) as response:
                    self.assertEqual(2, len(response.headers.get_all("Set-Cookie")))
                    self.assertTrue(all("Max-Age=0" in item for item in response.headers.get_all("Set-Cookie")))
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(session)
                self.assertEqual(401, failure.exception.code)

                reader_login = urllib.request.Request(
                    f"{base_url}/api/auth/login",
                    data=json.dumps({
                        "username": "reader", "password": "reader-password-value"
                    }).encode(),
                    headers={"Content-Type": "application/json", "Origin": base_url},
                    method="POST",
                )
                with urllib.request.urlopen(reader_login) as response:
                    reader_cookies = "; ".join(
                        item.split(";", 1)[0] for item in response.headers.get_all("Set-Cookie")
                    )
                with urllib.request.urlopen(urllib.request.Request(
                    f"{base_url}/api/config", headers={"Cookie": reader_cookies}
                )) as response:
                    self.assertEqual(200, response.status)
                with urllib.request.urlopen(urllib.request.Request(
                    f"{base_url}/api/source-health/diagnostic_feed", headers={"Cookie": reader_cookies}
                )) as response:
                    self.assertEqual(200, response.status)
                with urllib.request.urlopen(urllib.request.Request(
                    f"{base_url}/api/weather", headers={"Cookie": reader_cookies}
                )) as response:
                    self.assertEqual(200, response.status)
                reader_weather_write = urllib.request.Request(
                    f"{base_url}/api/weather", data=json.dumps(weather_payload).encode(),
                    headers={"Cookie": reader_cookies, "Content-Type": "application/json", "Origin": base_url},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(reader_weather_write)
                self.assertEqual(403, failure.exception.code)
                reader_ack = urllib.request.Request(
                    f"{occurrence_url}/acknowledge", data=b'{}',
                    headers={"Cookie": reader_cookies, "Content-Type": "application/json", "Origin": base_url},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(reader_ack)
                self.assertEqual(403, failure.exception.code)
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(urllib.request.Request(
                        f"{base_url}/api/users", headers={"Cookie": reader_cookies}
                    ))
                self.assertEqual(403, failure.exception.code)

                proxied_emergency = urllib.request.Request(
                    f"{base_url}/api/config",
                    headers={"Authorization": "Bearer emergency", "X-Real-IP": "203.0.113.10"},
                )
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(proxied_emergency)
                self.assertEqual(401, failure.exception.code)
                with urllib.request.urlopen(urllib.request.Request(
                    f"{base_url}/api/config", headers={"Authorization": "Bearer emergency"}
                )) as response:
                    self.assertEqual(200, response.status)
            finally:
                server.shutdown()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
