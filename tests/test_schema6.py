from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from argus.database import Database


class SchemaSixMigrationTests(unittest.TestCase):
    def test_schema_five_classifies_events_and_recoverable_incidents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "schema-five.db"
            current = Database(path)
            current.close()
            connection = sqlite3.connect(path)
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.executescript("""
                DROP TABLE incidents;
                CREATE TABLE incidents (
                    id INTEGER PRIMARY KEY,
                    incident_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL CHECK (status IN ('open', 'recovered')),
                    first_seen_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL,
                    recovered_at INTEGER,
                    confidence REAL NOT NULL DEFAULT 0.5,
                    evidence_json TEXT NOT NULL DEFAULT '[]',
                    source_ids_json TEXT NOT NULL DEFAULT '[]',
                    observation_count INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL
                );
            """)
            connection.executemany(
                """INSERT INTO observations(
                    id, source_id, publisher, dedupe_scope, external_id,
                    published_at, fetched_at, title, summary, url, attributes_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (1, "bloomberg_markets", "Bloomberg", "bloomberg", "news", 10, 10, "News", "", "https://example.com/news", '{"section":"Markets"}'),
                    (2, "host_health", "bk", "host", "disk", 11, 11, "Disk", "", "", '{"check":"disk:/"}'),
                    (3, "stocks", "Market", "market", "stock", 12, 12, "Stock", "", "", '{"symbol":"ABC"}'),
                ],
            )
            connection.executemany(
                """INSERT INTO incidents(
                    id, incident_key, status, first_seen_at, last_seen_at,
                    recovered_at, confidence, evidence_json, source_ids_json,
                    observation_count, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0.8, '[]', '[]', ?, ?)""",
                [
                    (1, "news", "open", 10, 10, None, 1, 10),
                    (2, "host", "open", 11, 11, None, 1, 11),
                    (3, "market", "recovered", 12, 13, 13, 1, 13),
                    (4, "source", "open", 14, 14, None, 0, 14),
                ],
            )
            connection.executemany(
                """INSERT INTO alerts(
                    observation_id, incident_id, rule_id, dedupe_key, topic, title,
                    message, priority, confidence, evidence_json, incident_key,
                    tags_json, click_url, status, attempts, next_attempt_at, created_at
                ) VALUES (?, ?, ?, ?, 'eos', ?, '', 4, 0.8, '[]', ?, '[]', '', 'delivered', 1, ?, ?)""",
                [
                    (1, 1, "bloomberg_breaking", "news-alert", "News", "news", 10, 10),
                    (2, 2, "host.health", "host-alert", "Host", "host", 11, 11),
                    (3, 3, "market.notify", "market-alert", "Market", "market", 12, 12),
                    (None, 4, "system.source_failure", "source-alert", "Source", "source", 14, 14),
                ],
            )
            connection.execute("PRAGMA user_version=5")
            connection.commit()
            connection.close()

            database = Database(path)
            migrated = {
                row["incident_key"]: (row["kind"], row["status"], row["recovered_at"])
                for row in database.connection.execute(
                    "SELECT incident_key, kind, status, recovered_at FROM incidents"
                )
            }
            self.assertEqual(("event", "recorded", None), migrated["news"])
            self.assertEqual(("stateful", "open", None), migrated["host"])
            self.assertEqual(("stateful", "recovered", 13), migrated["market"])
            self.assertEqual(("stateful", "open", None), migrated["source"])
            self.assertEqual("ok", database.connection.execute("PRAGMA integrity_check").fetchone()[0])
            self.assertEqual([], list(database.connection.execute("PRAGMA foreign_key_check")))
            database.close()


if __name__ == "__main__":
    unittest.main()
