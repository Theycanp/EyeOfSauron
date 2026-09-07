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
from argus.auth import AdminAuth
from argus.database import Database


class AdminAuthHttpTests(unittest.TestCase):
    def test_login_cookie_csrf_and_rbac(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ready: queue.Queue[tuple[HTTPServer, int]] = queue.Queue()

            def run_server() -> None:
                database = Database(root / "state.db")
                auth = AdminAuth(database)
                database.create_admin_user(
                    "owner", "Owner", auth.hash_password("correct-horse-battery"),
                    "admin", "bootstrap", 100,
                )
                database.create_admin_user(
                    "reader", "Reader", auth.hash_password("reader-password-value"),
                    "viewer", "bootstrap", 100,
                )
                store = ManagedConfigStore(root / "managed.json", database=database)
                server = HTTPServer(("127.0.0.1", 0), make_handler(store, database, "emergency"))
                ready.put((server, server.server_port))
                try:
                    server.serve_forever()
                finally:
                    server.server_close()
                    database.close()

            thread = threading.Thread(target=run_server, daemon=True)
            thread.start()
            server, port = ready.get(timeout=5)
            base_url = f"http://127.0.0.1:{port}"
            try:
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
                users = urllib.request.Request(
                    f"{base_url}/api/users", headers={"Cookie": cookie_values}
                )
                with urllib.request.urlopen(users) as response:
                    self.assertEqual(2, len(json.loads(response.read())["users"]))
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
