from __future__ import annotations

import json
import ast
import inspect
import queue
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import patch

from argus.admin import AdminError, ManagedConfigStore, _public, make_handler
from argus.database import Database, read_active_config
from argus.digest import DigestCluster, DigestDocument, SourceCoverage
from argus.persistence import ControlPlaneRepository, RevisionConflictError, RuntimeRepository
from argus.service import ArgusService


def source(identifier: str = "test_feed") -> dict:
    return {
        "id": identifier,
        "kind": "rss",
        "publisher": "Example",
        "section": "News",
        "dedupe_scope": identifier,
        "url": "https://example.com/feed.xml",
        "allowed_hosts": ["example.com"],
        "enabled": False,
        "poll_interval_seconds": 300,
        "request_timeout_seconds": 20,
        "request_attempts": 3,
        "retry_base_seconds": 2,
        "max_response_bytes": 2097152,
    }


class ControlPlaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = Database(self.root / "state.db")
        self.store = ManagedConfigStore(self.root / "managed.json", database=self.database)

    def tearDown(self) -> None:
        self.database.close()
        self.temp.cleanup()

    def test_committed_notifications_use_full_wal_durability(self) -> None:
        self.assertEqual("wal", self.database.connection.execute("PRAGMA journal_mode").fetchone()[0])
        self.assertEqual(2, self.database.connection.execute("PRAGMA synchronous").fetchone()[0])

    def test_engine_repository_declares_every_persistence_dependency(self) -> None:
        tree = ast.parse(inspect.getsource(ArgusService))
        calls = {
            node.attr for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "self" and node.value.attr == "database"
        }
        self.assertTrue(calls)
        self.assertEqual(set(), calls - set(dir(RuntimeRepository)))
        self.assertIsInstance(self.database, RuntimeRepository)
        self.assertIsInstance(self.database, ControlPlaneRepository)
        self.assertFalse(hasattr(ControlPlaneRepository, "claim_due_alert"))
        self.assertFalse(hasattr(ControlPlaneRepository, "save_digest"))
        self.assertTrue(hasattr(ControlPlaneRepository, "list_digests"))

    def test_readonly_config_lookup_does_not_create_or_migrate_database(self) -> None:
        missing = self.root / "missing" / "state.db"
        self.assertIsNone(read_active_config(missing))
        self.assertFalse(missing.parent.exists())
        self.assertIsNone(read_active_config(self.database.path))
        self.store.upsert("source", source())
        self.assertEqual(self.store.read(), read_active_config(self.database.path))
        with self.database.connection:
            self.database.connection.execute("UPDATE config_revisions SET payload_json = 'broken'")
        with self.assertRaises(RuntimeError):
            read_active_config(self.database.path)

    def test_config_cas_rejects_stale_writer_without_changing_history(self) -> None:
        self.store.upsert("source", source(), expected_revision=0)
        with self.assertRaises(RevisionConflictError):
            self.store.upsert("source", source("stale"), expected_revision=0)
        self.assertEqual(["test_feed"], [row["id"] for row in self.store.read()["sources"]])
        self.assertEqual(1, len(self.database.list_config_revisions()))
        self.store.upsert("source", source("second"), expected_revision=1)
        self.assertEqual(3, self.store.rollback(1, expected_revision=2))
        self.assertEqual(1, len(self.store.read()["sources"]))
        self.assertEqual(2, len(self.database.get_config_revision(2)["payload"]["sources"]))

    def test_snapshot_revision_and_payload_cannot_cross_concurrent_commit(self) -> None:
        self.store.upsert("source", source(), expected_revision=0)
        second = Database(self.root / "state.db")
        self.addCleanup(second.close)
        other = ManagedConfigStore(self.root / "managed.json", database=second)
        original = self.store._snapshot

        def interleaved_snapshot():
            snapshot = original()
            other.upsert("source", source("concurrent"))
            return snapshot

        with patch.object(self.store, "_snapshot", side_effect=interleaved_snapshot):
            with self.assertRaises(RevisionConflictError):
                self.store.upsert("source", source("stale"))
        self.assertEqual({"test_feed", "concurrent"}, {row["id"] for row in self.store.read()["sources"]})

    def test_legacy_import_once_preserves_revision_and_database_authority(self) -> None:
        self.store.path.write_text(json.dumps({"revision": 5, "sources": [source()], "rules": []}))
        imported = ManagedConfigStore(self.store.path, database=self.database)
        self.assertEqual(5, imported.revision)
        imported.upsert("source", source("second"))
        imported.path.write_text("invalid stale file")
        reopened = ManagedConfigStore(imported.path, database=self.database)
        self.assertEqual(6, reopened.revision)
        self.assertEqual(2, len(reopened.read()["sources"]))
        self.database.record_config_revision(1, {}, only_if_empty=True)
        self.assertEqual(6, reopened.revision)

    def test_direct_config_write_and_legacy_import_reject_secrets(self) -> None:
        raw = source()
        raw["settings"] = {"api_key": "must-not-persist"}
        with self.assertRaises(AdminError):
            self.store.write({"sources": [raw], "rules": []})
        self.assertIsNone(self.database.get_active_config_revision())
        self.store.path.write_text(json.dumps({"sources": [raw], "rules": []}))
        with self.assertRaises(AdminError):
            ManagedConfigStore(self.store.path, database=self.database)

    def test_model_token_limit_is_not_mistaken_for_a_credential(self) -> None:
        payload = {"sources": [], "rules": [], "analysis": {"max_tokens": 300}}
        self.assertEqual(1, self.store.write(payload))
        self.assertEqual(300, self.store.read()["analysis"]["max_tokens"])
        self.assertEqual(
            {"max_tokens": 300, "api_key_env": "API_KEY"},
            _public({"max_tokens": 300, "api_key": "secret", "api_key_env": "API_KEY"}),
        )

    def test_invalid_stored_config_fails_closed_instead_of_erasing_sources(self) -> None:
        self.store.upsert("source", source())
        with self.database.connection:
            self.database.connection.execute("UPDATE config_revisions SET payload_json = 'broken'")
        with self.assertRaises(RuntimeError):
            self.store.read()
        with self.assertRaises(RuntimeError):
            self.database.get_config_revision(1)

    def _claimed_alert(self, now: int = 1000):
        self.database.enqueue_test_alert("eos", now)
        claimed = self.database.claim_due_alert(now, lease_seconds=30)
        self.assertIsNotNone(claimed)
        return claimed

    def _failure(self, alert_id: int, now: int, **overrides) -> str:
        options = dict(retryable=True, failure_kind="http_503", max_attempts=3, max_age_seconds=100)
        options.update(overrides)
        return self.database.mark_delivery_failure(alert_id, now, now + 5, "temporary", **options)

    def test_permanent_delivery_failure_and_terminal_state_actions(self) -> None:
        alert = self._claimed_alert()
        self.assertFalse(self.database.cancel_alert(alert.id, 1001))
        self.assertFalse(self.database.discard_alert(alert.id))
        self.assertEqual("dead", self._failure(alert.id, 1001, retryable=False))
        self.assertTrue(self.database.retry_alert(alert.id, 1002))
        self.assertFalse(self.database.retry_alert(alert.id, 1003))
        self.assertTrue(self.database.cancel_alert(alert.id, 1004))
        self.assertTrue(self.database.discard_alert(alert.id))
        self.assertIsNone(self.database.get_alert(alert.id))

    def test_transient_delivery_retries_have_attempt_and_age_limits(self) -> None:
        alert = self._claimed_alert()
        self.assertEqual("pending", self._failure(alert.id, 1001))
        alert = self.database.claim_due_alert(1006, lease_seconds=30)
        self.assertEqual("dead", self._failure(alert.id, 1007, max_attempts=2))
        self.database.retry_alert(alert.id, 2000)
        self.database.claim_due_alert(2000, lease_seconds=30)
        self.assertEqual("dead", self._failure(alert.id, 2100))

    def test_manual_retry_restarts_age_budget_and_preserves_original_time(self) -> None:
        alert = self._claimed_alert()
        self.assertEqual("dead", self._failure(alert.id, 1100))
        self.database.retry_alert(alert.id, 5000)
        retried = self.database.claim_due_alert(5000, lease_seconds=30)
        self.assertEqual(1000, retried.created_at)
        self.assertEqual(1, retried.attempts)
        self.assertEqual("pending", self._failure(alert.id, 5001))

    def test_jobs_expire_and_interrupted_jobs_are_terminal(self) -> None:
        expired = self.database.create_admin_job("test_source", {}, "admin", 1000, ttl_seconds=60)
        self.assertIsNone(self.database.claim_admin_job("test_source", 1060))
        self.assertEqual("failed", self.database.get_admin_job(expired["id"])["status"])
        job = self.database.create_admin_job("test_source", {}, "admin", 2000)
        self.database.claim_admin_job("test_source", 2001)
        self.assertFalse(self.database.cancel_admin_job(job["id"], 2002))
        self.assertIsNone(self.database.claim_admin_job("test_source", 2003))
        self.assertEqual("worker interrupted", self.database.get_admin_job(job["id"])["error"])
        self.assertFalse(self.database.finish_admin_job(job["id"], 2004, result={"late": True}))

    def test_jobs_complete_once_and_queued_jobs_can_be_cancelled(self) -> None:
        job = self.database.create_admin_job("test_source", {}, "admin", 1000)
        self.assertEqual(job["id"], self.database.claim_admin_job("test_source", 1001)["id"])
        self.assertTrue(self.database.finish_admin_job(job["id"], 1002, result={"count": 3}))
        self.assertFalse(self.database.finish_admin_job(job["id"], 1003, error="late"))
        self.assertEqual({"count": 3}, self.database.get_admin_job(job["id"])["result"])
        pending = self.database.create_admin_job("test_source", {}, "admin", 2000)
        self.assertTrue(self.database.cancel_admin_job(pending["id"], 2001))
        self.assertIsNone(self.database.claim_admin_job("test_source", 2002))

    def test_old_engine_cannot_modify_new_instance_sources(self) -> None:
        self.database.register_engine("new", started_at=1000, applied_revision=None,
                                      code_version="test", configured_sources=1, active_sources=1)
        self.database.sync_source_runtime([("feed", "rss", True, 120)], {"feed"}, config_revision=None, now=1000)
        self.assertFalse(self.database.heartbeat_engine("old", 2000))
        self.database.stop_engine("old", 2000)
        status = self.database.status()
        self.assertEqual("running", status["engine"]["state"])
        self.assertEqual("active", status["sources"][0]["runtime_status"])
        self.assertEqual(1000, status["sources"][0]["heartbeat_at"])


class ControlPlaneHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        ready: queue.Queue = queue.Queue()

        def run_server():
            database = Database(self.root / "state.db")
            store = ManagedConfigStore(self.root / "managed.json", database=database)
            server = HTTPServer(("127.0.0.1", 0), make_handler(store, database, "test-token", heartbeat_timeout_seconds=75))
            ready.put(server)
            try:
                server.serve_forever(poll_interval=0.01)
            finally:
                server.server_close()
                database.close()

        self.thread = threading.Thread(target=run_server, daemon=True)
        self.thread.start()
        self.server = ready.get(timeout=5)
        self.database = Database(self.root / "state.db")

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.database.close()
        self.temp.cleanup()

    def request(self, path, data=None, *, headers=None, method=None):
        auth = {"Authorization": "Bearer test-token"}
        auth.update(headers or {})
        request = urllib.request.Request(f"http://127.0.0.1:{self.server.server_port}{path}",
                                         data=json.dumps(data).encode() if data is not None else None,
                                         headers=auth, method=method)
        try:
            response = urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    def _save_digest(self, *, title: str = "每日情报摘要") -> DigestDocument:
        return self.database.save_digest(
            DigestDocument(
                digest_key="daily:2026-09-06",
                version=0,
                period_start=1_788_307_200,
                period_end=1_788_393_600,
                timezone="Asia/Shanghai",
                title=title,
                summary="三条重点信息，其中一条需要持续关注。",
                generation_kind="algorithm",
                items=(
                    DigestCluster(
                        cluster_key="policy-update",
                        title="重要政策公告",
                        summary="政策公告摘要",
                        score=4.75,
                        importance=5,
                        urgency=3,
                        relevance=5,
                        confidence=0.9,
                        published_at=1_788_307_300,
                        regions=("CN",),
                        topics=("policy",),
                        source_ids=("mof_cn",),
                        observation_ids=(42,),
                        links=("https://example.test/42",),
                    ),
                ),
                coverage=(
                    SourceCoverage(
                        "mof_cn", "covered", 1, 1_788_393_500, 1_788_393_500, 0
                    ),
                ),
                created_at=1_788_393_600,
            )
        )

    def test_readiness_requires_daemon_and_current_revision(self) -> None:
        self.assertEqual(503, self.request("/ready")[0])
        self.database.register_engine("test", started_at=int(time.time()), applied_revision=0,
                                      code_version="test", configured_sources=0, active_sources=0)
        self.assertEqual(200, self.request("/ready")[0])
        _, body = self.request("/api/config")
        self.assertEqual(75, body["status"]["engine"]["heartbeat_timeout_seconds"])
        status, _ = self.request("/api/sources", source(), headers={"If-Match": '"0"'})
        self.assertEqual(200, status)
        self.assertEqual(503, self.request("/ready")[0])
        status, _ = self.request("/api/sources", source("stale"), headers={"If-Match": '"0"'})
        self.assertEqual(409, status)

    def test_source_test_is_queued_without_network_and_requires_auth(self) -> None:
        status, payload = self.request("/api/test-source", {"source": source()})
        self.assertEqual(202, status)
        self.assertEqual("queued", payload["job"]["status"])
        self.assertTrue(payload["job"]["request"]["source"]["enabled"])
        status, _ = self.request("/api/jobs/" + payload["job"]["id"], headers={"Authorization": "invalid"})
        self.assertEqual(401, status)
        self.assertEqual(400, self.request("/api/outbox?limit=invalid")[0])

    def test_prompt_catalog_is_authenticated_and_versioned(self) -> None:
        status, payload = self.request("/api/prompts")
        self.assertEqual(200, status)
        self.assertEqual(
            {("digest", 1), ("triage", 1)},
            {(item["prompt_id"], item["version"]) for item in payload["prompts"]},
        )
        status, payload = self.request(
            "/api/prompts",
            {"prompt_id": "triage", "version": 2, "system_text": "Return JSON only."},
            method="POST",
        )
        self.assertEqual(200, status)
        self.assertEqual(2, payload["saved"]["version"])
        status, _ = self.request("/api/prompts", headers={"Authorization": "invalid"})
        self.assertEqual(401, status)

    def test_analysis_policy_has_dedicated_revisioned_endpoint(self) -> None:
        policy = {
            "enabled": False,
            "local_enabled": False,
            "api_enabled": False,
            "send_full_text": False,
            "region_weights": {"CN": 5, "JP": 4, "US": 5, "GLOBAL": 3, "OTHER": 3},
        }
        status, payload = self.request(
            "/api/analysis", policy, headers={"If-Match": '"0"'}, method="POST"
        )
        self.assertEqual(200, status)
        self.assertEqual(1, payload["revision"])
        self.assertEqual(4, payload["saved"]["region_weights"]["JP"])
        _, config = self.request("/api/config")
        self.assertEqual(policy, config["managed"]["analysis"])
        self.assertEqual(
            409,
            self.request(
                "/api/analysis", policy, headers={"If-Match": '"0"'}, method="POST"
            )[0],
        )

    def test_digest_policy_has_dedicated_revisioned_endpoint(self) -> None:
        policy = {
            "enabled": False,
            "timezone": "Asia/Shanghai",
            "daily_time": "20:00",
            "item_limit": 30,
            "observation_limit": 5000,
            "notify": True,
            "public_base_url": "https://eos.example.test",
        }
        status, payload = self.request(
            "/api/digest-config", policy, headers={"If-Match": '"0"'}, method="POST"
        )
        self.assertEqual(200, status)
        self.assertEqual(1, payload["revision"])
        _, config = self.request("/api/config")
        self.assertEqual(policy, config["managed"]["digest"])

    def test_digest_reader_lists_only_published_and_returns_full_detail(self) -> None:
        draft = self._save_digest()
        status, payload = self.request("/api/digests")
        self.assertEqual(200, status)
        self.assertEqual([], payload["digests"])
        self.assertEqual(
            404,
            self.request("/api/digests/daily%3A2026-09-06")[0],
        )

        self.database.publish_digest(draft.digest_key, draft.version, 1_788_393_700)
        status, payload = self.request("/api/digests")
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["digests"]))
        row = payload["digests"][0]
        self.assertEqual("daily:2026-09-06", row["digest_key"])
        self.assertEqual("/digests/daily%3A2026-09-06", row["web_path"])
        self.assertEqual(1, row["item_count"])
        self.assertNotIn("items", row)

        status, payload = self.request("/api/digests/daily%3A2026-09-06")
        self.assertEqual(200, status)
        detail = payload["digest"]
        self.assertEqual([42], detail["items"][0]["observation_ids"])
        self.assertEqual(["mof_cn"], detail["items"][0]["source_ids"])
        self.assertEqual("covered", detail["coverage"][0]["status"])

    def test_digest_reader_supports_explicit_versions_and_validates_queries(self) -> None:
        first = self._save_digest(title="第一版")
        self.database.publish_digest(first.digest_key, first.version, 1_788_393_700)
        second = self._save_digest(title="第二版草稿")

        status, payload = self.request("/api/digests?status=all&limit=1")
        self.assertEqual(200, status)
        self.assertTrue(payload["pagination"]["truncated"])
        self.assertEqual("第二版草稿", payload["digests"][0]["title"])
        status, payload = self.request(
            "/api/digests/daily%3A2026-09-06?version=2"
        )
        self.assertEqual(200, status)
        self.assertEqual(second.version, payload["digest"]["version"])
        self.assertEqual("draft", payload["digest"]["status"])

        self.assertEqual(400, self.request("/api/digests?status=unknown")[0])
        self.assertEqual(400, self.request("/api/digests?limit=101")[0])
        self.assertEqual(
            400,
            self.request("/api/digests/daily%3A2026-09-06?version=zero")[0],
        )
        self.assertEqual(
            401,
            self.request("/api/digests", headers={"Authorization": "invalid"})[0],
        )

    def test_digest_spa_routes_serve_only_the_application_shell(self) -> None:
        self._save_digest(title="must not leak into static shell")
        for path in ("/digests", "/digests/daily%3A2026-09-06"):
            with urllib.request.urlopen(
                f"http://127.0.0.1:{self.server.server_port}{path}", timeout=3
            ) as response:
                body = response.read().decode("utf-8")
                self.assertEqual(200, response.status)
                self.assertIn("text/html", response.headers["Content-Type"])
                self.assertNotIn("must not leak into static shell", body)

    def test_readiness_detects_stale_poll_despite_healthy_daemon(self) -> None:
        now = int(time.time())
        self.database.register_engine("test", started_at=now, applied_revision=0,
                                      code_version="test", configured_sources=1, active_sources=1)
        self.database.sync_source_runtime([("feed", "rss", True, 120)], {"feed"}, config_revision=0, now=now - 400)
        self.database.heartbeat_engine("test", now)
        status, payload = self.request("/ready")
        self.assertEqual(503, status)
        self.assertEqual(["feed"], payload["stale_sources"])
        self.database.sync_source_runtime([("feed", "rss", True, 120)], {"feed"}, config_revision=0, now=now)
        self.assertEqual(200, self.request("/ready")[0])

    def test_discard_rejects_active_notification_and_only_removes_terminal_row(self) -> None:
        now = int(time.time())
        self.database.enqueue_test_alert("eos", now)
        alert = self.database.list_alerts()[0]
        route = f"/api/outbox/{alert['id']}"
        status, body = self.request(route, method="DELETE")
        self.assertEqual(409, status)
        self.assertFalse(body["removed"])
        self.assertEqual("invalid_state", body["code"])
        self.assertIsNotNone(self.database.get_alert(alert["id"]))
        self.database.cancel_alert(alert["id"], now)
        status, body = self.request(route, method="DELETE")
        self.assertEqual(200, status)
        self.assertTrue(body["removed"])
        self.assertIsNone(self.database.get_alert(alert["id"]))
