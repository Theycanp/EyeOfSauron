from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from argus.backup import BackupError, create_backup, restore_backup, verify_backup


class BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "live" / "state.db"
        self.database.parent.mkdir()
        self.writer = sqlite3.connect(self.database)
        self.writer.execute("PRAGMA journal_mode=WAL")
        self.writer.executescript(
            """
            CREATE TABLE observations (id INTEGER PRIMARY KEY, title TEXT NOT NULL);
            CREATE TABLE alerts (id INTEGER PRIMARY KEY, status TEXT NOT NULL);
            PRAGMA user_version=6;
            """
        )
        self.writer.execute("INSERT INTO observations(title) VALUES ('baseline')")
        self.writer.commit()

    def tearDown(self) -> None:
        self.writer.close()
        self.temporary.cleanup()

    def test_online_backup_includes_committed_wal_rows_and_manifest(self) -> None:
        self.writer.execute("INSERT INTO observations(title) VALUES ('in wal')")
        self.writer.execute("INSERT INTO alerts(status) VALUES ('pending')")
        self.writer.commit()
        managed = self.root / "managed.json"
        managed.write_text(
            json.dumps({"revision": 4, "sources": [], "rules": []}),
            encoding="utf-8",
        )
        bundle = self.root / "backups" / "snapshot"

        manifest = create_backup(
            self.database,
            bundle,
            managed_config=managed,
            source_revision="test-revision",
            expected_schema=6,
        )

        self.assertEqual(2, manifest["database"]["table_counts"]["observations"])
        self.assertEqual(1, manifest["database"]["table_counts"]["alerts"])
        self.assertEqual("test-revision", verify_backup(bundle)["source_revision"])

    def test_checksum_tampering_is_detected(self) -> None:
        bundle = self.root / "snapshot"
        create_backup(self.database, bundle, expected_schema=6)
        with (bundle / "state.db").open("ab") as handle:
            handle.write(b"tamper")

        with self.assertRaisesRegex(BackupError, "size|checksum"):
            verify_backup(bundle)

    def test_restore_is_verified_and_preserves_existing_database(self) -> None:
        bundle = self.root / "snapshot"
        create_backup(self.database, bundle, expected_schema=6)
        restored = self.root / "restore" / "state.db"

        result = restore_backup(bundle, restored)

        self.assertIsNone(result["rollback_database"])
        with sqlite3.connect(restored) as connection:
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0])
        with self.assertRaisesRegex(BackupError, "already exists"):
            restore_backup(bundle, restored)

    def test_restore_refuses_destination_with_wal_companion(self) -> None:
        bundle = self.root / "snapshot"
        create_backup(self.database, bundle, expected_schema=6)
        restored = self.root / "restore" / "state.db"
        restored.parent.mkdir()
        Path(f"{restored}-wal").write_bytes(b"stale")

        with self.assertRaisesRegex(BackupError, "WAL/SHM"):
            restore_backup(bundle, restored)

    def test_replace_preserves_previous_database(self) -> None:
        bundle = self.root / "snapshot"
        create_backup(self.database, bundle, expected_schema=6)
        restored = self.root / "restore" / "state.db"
        restored.parent.mkdir()
        with sqlite3.connect(restored) as connection:
            connection.execute("CREATE TABLE old_state (value TEXT NOT NULL)")
            connection.execute("INSERT INTO old_state(value) VALUES ('preserved')")

        result = restore_backup(bundle, restored, replace=True)

        rollback = Path(str(result["rollback_database"]))
        self.assertTrue(rollback.is_file())
        with sqlite3.connect(rollback) as connection:
            self.assertEqual(
                "preserved",
                connection.execute("SELECT value FROM old_state").fetchone()[0],
            )

    def test_restore_refuses_non_file_destination(self) -> None:
        bundle = self.root / "snapshot"
        create_backup(self.database, bundle, expected_schema=6)
        restored = self.root / "restore" / "state.db"
        restored.mkdir(parents=True)

        with self.assertRaisesRegex(BackupError, "not a regular file"):
            restore_backup(bundle, restored, replace=True)


if __name__ == "__main__":
    unittest.main()
