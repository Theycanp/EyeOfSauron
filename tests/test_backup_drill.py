from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from argus.backup import BackupError, create_backup
from scripts.operations.restore_drill import main, restore_drill


class RestoreDrillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        database = self.root / "live.db"
        with sqlite3.connect(database) as connection:
            connection.executescript(
                "CREATE TABLE parent(id INTEGER PRIMARY KEY);"
                "CREATE TABLE child(id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id));"
                "INSERT INTO parent VALUES (1); INSERT INTO child VALUES (1, 1);"
                "PRAGMA user_version=19;"
            )
        self.bundle = self.root / "backup"
        create_backup(database, self.bundle, expected_schema=19, source_revision="drill-test")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_restore_validates_isolated_database_and_records_objectives(self) -> None:
        scratch = self.root / "scratch"
        scratch.mkdir()
        result = restore_drill(self.bundle, scratch_parent=scratch)

        self.assertTrue(result["passed"])
        self.assertTrue(result["rpo_met"])
        self.assertTrue(result["rto_met"])
        self.assertEqual("ok", result["integrity_check"])
        self.assertEqual(0, result["foreign_key_violations"])
        self.assertEqual(19, result["schema_version"])
        self.assertEqual("drill-test", result["source_revision"])
        self.assertGreaterEqual(result["rpo_seconds"], 0)
        self.assertGreaterEqual(result["rto_database_seconds"], 0)
        self.assertEqual([], list(scratch.iterdir()))
        self.assertTrue((self.bundle / "state.db").is_file())

    def test_old_backup_records_rpo_breach_without_skipping_restore(self) -> None:
        manifest_path = self.bundle / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["created_at"] = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        result = restore_drill(self.bundle)

        self.assertFalse(result["passed"])
        self.assertFalse(result["rpo_met"])
        self.assertTrue(result["rto_met"])
        self.assertGreater(result["rpo_seconds"], 24 * 3600)

    def test_rejects_corrupt_bundle_and_future_timestamp(self) -> None:
        with (self.bundle / "state.db").open("ab") as handle:
            handle.write(b"corrupt")
        with self.assertRaises(BackupError):
            restore_drill(self.bundle)

        self.bundle = self.root / "fresh-backup"
        create_backup(self.root / "live.db", self.bundle, expected_schema=19)
        manifest_path = self.bundle / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["created_at"] = (datetime.now(UTC) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(BackupError, "future"):
            restore_drill(self.bundle)

    def test_cli_prints_json_evidence_and_fails_objective_breach(self) -> None:
        output = StringIO()
        report = self.root / "drill-report.json"
        with redirect_stdout(output):
            status = main(["--bundle", str(self.bundle), "--report", str(report)])
        self.assertEqual(0, status)
        self.assertTrue(json.loads(output.getvalue())["passed"])
        self.assertEqual(json.loads(output.getvalue()), json.loads(report.read_text(encoding="utf-8")))
        self.assertEqual(0o600, report.stat().st_mode & 0o777)

        output = StringIO()
        errors = StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            status = main(["--bundle", str(self.bundle), "--report", str(report)])
        self.assertEqual(1, status)
        self.assertEqual("", output.getvalue())
        self.assertIn("File exists", errors.getvalue())

        old_manifest = self.bundle / "manifest.json"
        manifest = json.loads(old_manifest.read_text(encoding="utf-8"))
        manifest["created_at"] = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        old_manifest.write_text(json.dumps(manifest), encoding="utf-8")
        output = StringIO()
        breached_report = self.root / "breached.json"
        with redirect_stdout(output):
            status = main(["--bundle", str(self.bundle), "--report", str(breached_report)])
        self.assertEqual(1, status)
        self.assertFalse(json.loads(output.getvalue())["rpo_met"])
        self.assertFalse(json.loads(breached_report.read_text(encoding="utf-8"))["passed"])

    def test_rto_breach_is_recorded(self) -> None:
        with patch("scripts.operations.restore_drill.time.monotonic", side_effect=[0.0, 1801.0]):
            result = restore_drill(self.bundle)
        self.assertFalse(result["passed"])
        self.assertTrue(result["rpo_met"])
        self.assertFalse(result["rto_met"])


if __name__ == "__main__":
    unittest.main()
