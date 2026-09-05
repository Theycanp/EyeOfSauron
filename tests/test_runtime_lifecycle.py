from __future__ import annotations

import asyncio
import contextlib
import io
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from argus.cli import _build_service, main
from argus.database import Database
from argus.models import FeedFetchResult
from argus.notifier import NotifyError
from argus.rules import RuleSet
from argus.service import AlreadyRunningError, ArgusService, ProcessLock, _sd_notify
from helpers import production_config


class RuntimeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        config = production_config(self.root)
        self.config = replace(config, sources=(), rules=(), ntfy=replace(config.ntfy, enabled=False),
                              service=replace(config.service, source_start_jitter_seconds=0, managed_sources_path=self.root / "managed.json"))
        self.db = Database(self.config.service.database_path)
        self.service = ArgusService(self.config, self.db, {}, RuleSet(()), None)

    async def asyncTearDown(self):
        self.db.close()
        self.temp.cleanup()

    async def test_daemon_heartbeat_and_clean_shutdown(self):
        task = asyncio.create_task(self.service.run_forever())
        await asyncio.sleep(0.03)
        self.assertEqual("running", self.db.status()["engine"]["state"])
        self.service.request_stop()
        self.assertFalse(await asyncio.wait_for(task, 2))
        self.assertEqual("offline", self.db.status()["engine"]["state"])

    async def test_new_revision_requests_restart_and_preserves_outbox(self):
        self.db.save_managed_config({"sources": [], "rules": []}, "test", "change", expected_revision=0)
        self.db.enqueue_test_alert("eos", 100)
        self.assertTrue(await asyncio.wait_for(self.service.run_forever(), 2))
        self.assertEqual(1, self.db.status()["outbox"]["pending"])

    async def test_worker_failure_marks_engine_failed(self):
        async def broken():
            raise RuntimeError("worker failed")
        with patch.object(self.service, "_delivery_loop", broken):
            with self.assertRaises(ExceptionGroup):
                await self.service.run_forever()
        self.assertEqual("failed", self.db.status()["engine"]["state"])

    async def test_source_test_is_read_only_and_finishes_job(self):
        raw = {"id": "probe_feed", "kind": "rss", "publisher": "Test", "section": "News",
               "dedupe_scope": "test", "url": "https://example.com/feed", "allowed_hosts": ["example.com"],
               "poll_interval_seconds": 60, "request_timeout_seconds": 20,
               "request_attempts": 1, "retry_base_seconds": 1, "max_response_bytes": 4096}
        job = self.db.create_admin_job("test_source", {"source": raw}, "test", 2000000000)
        collector = Mock()
        collector.fetch.return_value = FeedFetchResult((), None, None)
        original = self.db.finish_admin_job
        def finish(*args, **kwargs):
            value = original(*args, **kwargs)
            self.service.request_stop()
            return value
        with patch("argus.service.build_collector", return_value=collector) as factory, patch.object(self.db, "finish_admin_job", side_effect=finish):
            await asyncio.wait_for(self.service._admin_job_loop(), 2)
        self.assertEqual("succeeded", self.db.get_admin_job(job["id"])["status"])
        self.assertEqual(15, factory.call_args.args[0].request_timeout_seconds)
        self.assertEqual(0, self.db.status()["observations"])

    async def test_invalid_job_finishes_failed_without_killing_worker(self):
        job = self.db.create_admin_job("test_source", {}, "test", 2000000000)
        original = self.db.finish_admin_job
        def finish(*args, **kwargs):
            value = original(*args, **kwargs)
            self.service.request_stop()
            return value
        with patch.object(self.db, "finish_admin_job", side_effect=finish):
            await asyncio.wait_for(self.service._admin_job_loop(), 2)
        self.assertEqual("failed", self.db.get_admin_job(job["id"])["status"])

    async def test_permanent_delivery_failure_is_visible_as_dead(self):
        self.db.enqueue_test_alert("eos", 2000000000)
        notifier = Mock()
        notifier.publish.side_effect = NotifyError("rejected", failure_kind="remote_rejected")
        self.service.notifier = notifier
        with patch("argus.service.now_epoch", return_value=2000000001):
            self.assertFalse(await self.service.deliver_one())
        self.assertEqual(1, self.db.status()["outbox"]["dead"])

    def test_process_lock_prevents_second_engine(self):
        with ProcessLock(self.root / "lock"):
            with self.assertRaises(AlreadyRunningError):
                with ProcessLock(self.root / "lock"):
                    self.fail("second engine acquired lock")
        with ProcessLock(self.root / "lock"):
            pass

    def test_missing_source_credentials_does_not_prevent_other_sources(self):
        config = production_config(self.root)
        config = replace(config, ntfy=replace(config.ntfy, enabled=False))
        from argus.adapters import AdapterError
        with patch("argus.cli.build_collector", side_effect=[AdapterError("missing credential"), Mock(), Mock(), Mock()]):
            service = _build_service(config, self.db)
        self.assertEqual(3, len(service.collectors))

    def test_cli_config_status_once_and_admin_composition(self):
        args = ["--config", "test.toml"]
        with patch("argus.cli.load_config", return_value=self.config), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, main(args + ["check-config"]))
            self.assertEqual(0, main(args + ["status", "--json"]))
            self.assertEqual(0, main(args + ["status"]))
            self.assertEqual(0, main(args + ["once"]))
            self.assertEqual(0, main(args + ["enqueue-test"]))
            with patch("argus.cli.serve") as serve:
                self.assertEqual(0, main(args + ["admin"]))
                serve.assert_called_once()

    def test_systemd_notify_failure_is_best_effort(self):
        with patch.dict("os.environ", {"NOTIFY_SOCKET": "@missing-eos-test"}):
            _sd_notify("READY=1")

    def test_check_config_uses_database_revision_without_opening_writer(self):
        payload = {"sources": [], "rules": []}
        with patch("argus.cli.load_config", return_value=self.config) as load, \
             patch("argus.cli.read_active_config", return_value=payload), \
             patch("argus.cli.Database") as database, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, main(["--config", "test.toml", "check-config"]))
        database.assert_not_called()
        self.assertEqual(payload, load.call_args.kwargs["managed_override"])
