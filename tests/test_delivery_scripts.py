from __future__ import annotations

import importlib.util
import io
import json
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from argus.database import SCHEMA_VERSION
from argus import __version__


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "check_status", ROOT / "scripts/operations/check_status.py"
)
assert SPEC and SPEC.loader
check_status = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_status)
UNPACK_SPEC = importlib.util.spec_from_file_location(
    "unpack_release", ROOT / "scripts/release/unpack-release.py"
)
assert UNPACK_SPEC and UNPACK_SPEC.loader
unpack_release = importlib.util.module_from_spec(UNPACK_SPEC)
UNPACK_SPEC.loader.exec_module(unpack_release)
DAILY_SPEC = importlib.util.spec_from_file_location(
    "daily_backup", ROOT / "scripts/operations/daily_backup.py"
)
assert DAILY_SPEC and DAILY_SPEC.loader
daily_backup = importlib.util.module_from_spec(DAILY_SPEC)
DAILY_SPEC.loader.exec_module(daily_backup)


class DailyBackupTests(unittest.TestCase):
    def test_watchdog_start_limit_allows_its_normal_timer_cadence(self):
        import configparser
        unit = configparser.ConfigParser(interpolation=None)
        timer = configparser.ConfigParser(interpolation=None)
        unit.read(ROOT / 'deploy/argus-watchdog.service')
        timer.read(ROOT / 'deploy/argus-watchdog.timer')
        interval = int(unit['Unit']['StartLimitIntervalSec'].removesuffix('min'))
        cadence = int(timer['Timer']['OnUnitActiveSec'].removesuffix('min'))
        self.assertLessEqual(interval, cadence)

    def test_health_status_can_arrive_on_inherited_stdin(self):
        with patch("sys.stdin", io.StringIO('{"engine": {}}')), patch("sys.stdout", io.StringIO()), \
             patch.object(check_status, "evaluate_status", return_value={"ok": True}) as evaluate:
            result = check_status.main(["--status-file", "-", "--config", "unused.toml"])
        self.assertEqual(0, result)
        self.assertEqual({"engine": {}}, evaluate.call_args.args[0])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "state.db"
        with sqlite3.connect(self.database) as connection:
            connection.execute("CREATE TABLE observations (value TEXT)")
            connection.execute("INSERT INTO observations VALUES ('preserved')")
        self.daily = self.root / "eyeofsauron/daily"

    def test_keeps_fourteen_verified_daily_bundles_only(self):
        self.daily.mkdir(parents=True)
        manual = self.daily / "manual-backup"
        manual.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        linked = self.daily / "daily-20200101T000000Z-00000000"
        linked.symlink_to(outside, target_is_directory=True)
        for _ in range(16):
            latest = daily_backup.daily_backup(self.database, self.daily, None)
        bundles = [path for path in self.daily.iterdir() if path.is_dir() and not path.is_symlink() and daily_backup.BUNDLE_NAME.fullmatch(path.name)]
        self.assertEqual(14, len(bundles))
        self.assertIn(latest, bundles)
        self.assertTrue(manual.exists())
        self.assertTrue(linked.is_symlink())
        self.assertTrue(outside.exists())
        self.assertEqual(0o700, self.daily.stat().st_mode & 0o777)

    def test_failed_new_backup_preserves_existing_snapshot(self):
        previous = daily_backup.daily_backup(self.database, self.daily, None)
        with patch.object(daily_backup, "create_backup", side_effect=daily_backup.BackupError("failure")):
            with self.assertRaises(daily_backup.BackupError):
                daily_backup.daily_backup(self.database, self.daily, None, keep=1)
        self.assertTrue(previous.exists())

    def test_rejects_broad_or_symlink_roots(self):
        with self.assertRaises(ValueError):
            daily_backup.daily_backup(self.database, self.root, None)
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / "eyeofsauron").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            daily_backup.daily_backup(self.database, self.daily, None)
        self.assertFalse((outside / "daily").exists())


class ArchiveSafetyTests(unittest.TestCase):
    def test_regular_archive_extracts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "release.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                item = tarfile.TarInfo("eyeofsauron/RELEASE.json")
                item.size = 2
                bundle.addfile(item, io.BytesIO(b"{}"))
            unpack_release.unpack_release(archive, root / "output")
            self.assertEqual("{}", (root / "output/eyeofsauron/RELEASE.json").read_text())

    def test_traversal_links_duplicates_and_empty_archive_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for case in ("traversal", "absolute", "symlink", "hardlink", "duplicate", "empty"):
                with self.subTest(case=case):
                    archive = root / f"{case}.tar.gz"
                    with tarfile.open(archive, "w:gz") as bundle:
                        if case != "empty":
                            name = {
                                "traversal": "eyeofsauron/../../escaped",
                                "absolute": "/escaped",
                            }.get(case, "eyeofsauron/file")
                            item = tarfile.TarInfo(name)
                            if case in {"symlink", "hardlink"}:
                                item.type = tarfile.SYMTYPE if case == "symlink" else tarfile.LNKTYPE
                                item.linkname = "../../escaped"
                            bundle.addfile(item)
                            if case == "duplicate":
                                bundle.addfile(item)
                    with self.assertRaises(ValueError):
                        unpack_release.unpack_release(archive, root / case)
            self.assertFalse((root / "escaped").exists())

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd tooling is required")
    def test_preflight_does_not_mutate_prepared_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for directory in ("src/argus", "scripts", "deploy"):
                shutil.copytree(
                    ROOT / directory, root / directory,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
            shutil.copyfile(ROOT / "pyproject.toml", root / "pyproject.toml")
            config = root / "test.toml"
            config.write_text(
                (ROOT / "config/argus.example.toml").read_text()
                .replace("/tmp/argus-managed-sources.json", str(root / "export.json"))
            )
            (root / "RELEASE.json").write_text(json.dumps({
                "format_version": 1, "artifact_kind": "server-release",
                "product": "EyeOfSauron", "component": "Argus", "version": __version__,
                "database_schema": SCHEMA_VERSION, "commit": "a" * 40,
            }))
            before = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
            for _ in range(2):
                result = subprocess.run(
                    ["bash", str(root / "scripts/release/preflight.sh"), str(root), str(config)],
                    text=True, capture_output=True, timeout=30,
                )
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            after = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
            self.assertEqual(before, after)


class HealthGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / "config.toml"
        self.database = self.root / "state.db"
        self.config.write_text(
            (ROOT / "config/argus.example.toml").read_text()
            .replace("/tmp/argus-development.db", str(self.database))
            .replace("/tmp/argus-managed-sources.json", str(self.root / "export.json"))
        )
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "CREATE TABLE config_revisions (revision INTEGER, payload_json TEXT, active INTEGER)"
            )
            connection.execute(
                "INSERT INTO config_revisions VALUES (1, ?, 1)",
                (json.dumps({"revision": 1, "sources": [], "rules": []}),),
            )
        self.status = {
            "database_schema": SCHEMA_VERSION,
            "desired_revision": 1,
            "engine": {"state": "running", "heartbeat_age_seconds": 0, "applied_revision": 1},
            "sources": [{
                "source_id": "bloomberg_markets", "runtime_status": "active",
                "last_success_at": 990, "consecutive_failures": 0,
                "last_attempt_at": 990,
            }],
            "outbox": {},
        }

    def evaluate(self):
        return check_status.evaluate_status(
            self.status, self.config, now=1000,
            source_age_multiplier=4, minimum_source_age=300, max_pending=10,
        )

    def test_healthy_engine_ignores_broken_compatibility_export(self):
        (self.root / "export.json").write_text("broken export")
        self.assertTrue(self.evaluate()["ok"])

    def test_stale_heartbeat_or_unapplied_revision_fails(self):
        for key, value in [("heartbeat_age_seconds", 1000), ("applied_revision", 0)]:
            with self.subTest(key=key):
                previous = self.status["engine"][key]
                self.status["engine"][key] = value
                self.assertFalse(self.evaluate()["ok"])
                self.status["engine"][key] = previous

    def test_revision_change_during_probe_requests_retry(self):
        self.status["desired_revision"] = 2
        with self.assertRaisesRegex(ValueError, "changed during"):
            self.evaluate()

    def test_external_outage_warns_but_stopped_worker_fails(self):
        self.status["sources"][0].update(
            last_success_at=1, runtime_status="degraded", outage_alerted=True,
        )
        result = self.evaluate()
        self.assertTrue(result["ok"])
        self.assertTrue(result["warnings"])
        self.status["sources"][0]["runtime_status"] = "stopped"
        self.assertFalse(self.evaluate()["ok"])

    def test_backlog_and_missing_runtime_fail(self):
        self.status["outbox"] = {"pending": 10, "sending": 1}
        self.assertFalse(self.evaluate()["ok"])
        self.status["outbox"] = {}
        self.status["sources"] = []
        self.assertFalse(self.evaluate()["ok"])

    def test_worker_stall_fails_even_when_engine_heartbeat_is_fresh(self):
        self.status["sources"][0]["last_attempt_at"] = 1
        self.assertFalse(self.evaluate()["ok"])
        self.status["sources"][0]["registered_at"] = 990
        self.assertTrue(self.evaluate()["ok"])

    def test_numeric_shell_options_cannot_evaluate_arithmetic_expressions(self):
        result = subprocess.run(
            ["bash", str(ROOT / "scripts/operations/health-gate.sh"), "--wait-seconds", "1+2"],
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("non-negative integers", result.stderr)


if __name__ == "__main__":
    unittest.main()
