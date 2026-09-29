from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from argus.database import SCHEMA_VERSION, Database
from argus.event_facts import FACT_EXTRACTOR_VERSION


class Schema28ControlsTests(unittest.TestCase):
    def test_concurrent_schema27_upgrade_serializes_and_seeds_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            database = Database(path)
            database.close()
            with sqlite3.connect(path) as connection:
                connection.execute("ALTER TABLE digest_items DROP COLUMN event_facts_json")
                for table in ("event_fact_corrections", "weather_provider_usage", "weather_provider_policy",
                              "event_quality_labels", "event_fact_versions"):
                    connection.execute(f"DROP TABLE {table}")
                connection.execute("PRAGMA user_version=27")

            def open_database(_: int) -> tuple[int, int]:
                current = Database(path)
                try:
                    return (current.connection.execute("PRAGMA user_version").fetchone()[0],
                            len(current.list_weather_provider_policies()))
                finally:
                    current.close()

            with ThreadPoolExecutor(max_workers=6) as workers:
                results = list(workers.map(open_database, range(12)))
            self.assertEqual([(SCHEMA_VERSION, 6)] * 12, results)
            database = Database(path)
            try:
                self.assertEqual("ok", database.connection.execute("PRAGMA integrity_check").fetchone()[0])
                self.assertEqual([], database.connection.execute("PRAGMA foreign_key_check").fetchall())
                self.assertEqual(1, database.connection.execute(
                    "SELECT COUNT(*) FROM event_fact_versions WHERE extractor_version=?",
                    (FACT_EXTRACTOR_VERSION,),
                ).fetchone()[0])
                self.assertEqual(1, len([row for row in database.connection.execute("PRAGMA table_info(digest_items)")
                                        if row[1] == "event_facts_json"]))
            finally:
                database.close()
