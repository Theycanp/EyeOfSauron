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
from types import SimpleNamespace

from argus.admin import ManagedConfigStore, make_handler
from argus.auth import AdminAuth
from argus.content import ContentFetchRequest
from argus.database import Database
from argus.event_fact_projection import EventFactProjector

import test_event_page_consistency as fixtures


class AdminFactContentHttpTests(unittest.TestCase):
    def test_correction_and_content_retry_require_session_csrf_and_write_role(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ready: queue.Queue = queue.Queue()

            def run_server() -> None:
                database = Database(root / "state.db")
                auth = AdminAuth(database)
                for username, role in (("operator", "operator"), ("reader", "viewer")):
                    database.create_admin_user(
                        username, username, auth.hash_password("http-test-password"), role, "bootstrap", 100,
                    )
                surface = SimpleNamespace(database=database, sequence=0)
                add_report = fixtures.EventPageConsistencyTests.add_report
                add_report(surface, "decision", 1_790_000_000,
                           title="Fed raises interest rates by 25 bps", source_tier="primary")
                new_report = add_report(surface, "decision", 1_790_000_100,
                                        title="Fed raises interest rates by 50 bps", source_tier="primary")
                projector = EventFactProjector(database)
                projector.process_once(1_790_000_200)
                projector.process_once(1_790_000_201)
                claims = database.list_event_claims("decision")
                selected = {
                    "event_key": "decision", "report_id": new_report.report_id,
                    "old_claim_key": next(claim.claim_key for claim in claims if "25" in claim.text),
                    "new_claim_key": next(claim.claim_key for claim in claims if "50" in claim.text),
                }
                with database.unit_of_work():
                    database._enqueue_content_fetch(new_report.observation_id, ContentFetchRequest(
                        new_report.url, ("example.test",),
                    ), 100)
                job = database.claim_content_fetch(100)
                assert job is not None
                database.fail_content_fetch(job, "parser failed", 101, 101,
                                            retryable=False, failure_kind="empty_content")
                handler = make_handler(ManagedConfigStore(root / "managed.json", database=database), database, "")
                server = HTTPServer(("127.0.0.1", 0), handler)
                ready.put((server, selected, job.id))
                try:
                    server.serve_forever()
                finally:
                    server.server_close()
                    database.close()

            thread = threading.Thread(target=run_server, daemon=True)
            thread.start()
            server, selected, job_id = ready.get(timeout=30)
            base = f"http://127.0.0.1:{server.server_port}"

            def post(path, data, credentials=None, *, csrf=True, origin=None):
                headers = {"Content-Type": "application/json", "Origin": origin or base}
                if credentials is not None:
                    cookie, token = credentials
                    headers["Cookie"] = cookie
                    if csrf:
                        headers["X-CSRF-Token"] = token
                request = urllib.request.Request(base + path, data=json.dumps(data).encode(),
                                                 headers=headers, method="POST")
                with urllib.request.urlopen(request, timeout=10) as response:
                    return json.loads(response.read())

            def login(username):
                request = urllib.request.Request(
                    base + "/api/auth/login",
                    data=json.dumps({"username": username, "password": "http-test-password"}).encode(),
                    headers={"Content-Type": "application/json", "Origin": base}, method="POST",
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    cookie = "; ".join(value.split(";", 1)[0] for value in response.headers.get_all("Set-Cookie"))
                    token = next(value.split("=", 1)[1] for value in cookie.split("; ")
                                 if value.startswith("__Host-eos_csrf="))
                    return cookie, token

            try:
                operator, reader = login("operator"), login("reader")
                retry = {"ids": [job_id], "reason": "Reviewed parser repair"}
                for path, payload in (("/api/event-facts/correction/preview", selected),
                                      ("/api/event-facts/correction/apply", {**selected, "reason": "reviewed", "expected_revision": "0"*64}),
                                      ("/api/content-jobs/retry", retry)):
                    with self.subTest(path=path, denial="anonymous"):
                        with self.assertRaises(urllib.error.HTTPError) as failure:
                            post(path, payload)
                        self.assertEqual(401, failure.exception.code)
                    with self.subTest(path=path, denial="viewer"):
                        with self.assertRaises(urllib.error.HTTPError) as failure:
                            post(path, payload, reader)
                        self.assertEqual(403, failure.exception.code)
                    with self.subTest(path=path, denial="csrf"):
                        with self.assertRaises(urllib.error.HTTPError) as failure:
                            post(path, payload, operator, csrf=False)
                        self.assertEqual(403, failure.exception.code)
                    with self.subTest(path=path, denial="origin"):
                        with self.assertRaises(urllib.error.HTTPError) as failure:
                            post(path, payload, operator, origin="https://evil.example")
                        self.assertEqual(403, failure.exception.code)
                preview = post("/api/event-facts/correction/preview", selected, operator)
                apply_payload = {**selected, "reason": "Read explicit primary correction",
                                 "expected_revision": preview["revision"], "confirmed_primary_correction": True}
                for confirmation in (None, False, 1, "true"):
                    with self.subTest(confirmation=confirmation):
                        with self.assertRaises(urllib.error.HTTPError) as failure:
                            post("/api/event-facts/correction/apply",
                                 {**apply_payload, "confirmed_primary_correction": confirmation}, operator)
                        self.assertEqual(400, failure.exception.code)
                result = post("/api/event-facts/correction/apply", apply_payload, operator)
                self.assertTrue(result["applied"])
                self.assertTrue(result["notification_queued"])
                self.assertFalse(post("/api/event-facts/correction/apply", apply_payload, operator)["applied"])
                self.assertEqual(1, post("/api/content-jobs/retry", retry, operator)["changed"])
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    post("/api/content-jobs/retry", retry, operator)
                self.assertEqual(400, failure.exception.code)
            finally:
                server.shutdown()
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
