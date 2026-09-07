from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from .analysis import AnalysisAttempt, InformationAnalysis, analyze_observation
from .digest import DigestCluster, DigestDocument, SourceCoverage
from .models import (
    AnalysisWorkItem,
    AlertCandidate,
    FeedFetchResult,
    IngestReport,
    Observation,
    OutboxMessage,
    SourceState,
)
from .rules import RuleSet
from .reminders import ReminderError, ReminderSpec, next_daily_occurrence
from .source_quality import SourceQualityPolicy, calculate_quality
from .util import sanitize_error, to_epoch
from .persistence import RevisionConflictError, SQLiteUnitOfWork
from .prompts import PromptTemplate, TRIAGE_V1

SCHEMA_VERSION = 12


def read_active_config(path: Path) -> dict[str, Any] | None:
    """Read the desired configuration without creating or migrating a database."""
    if not path.exists():
        return None
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'config_revisions'"
        ).fetchone()
        if exists is None:
            return None
        row = connection.execute(
            "SELECT payload_json FROM config_revisions WHERE active = 1 ORDER BY revision DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row[0])
        except (TypeError, ValueError) as exc:
            raise RuntimeError("active configuration revision is invalid") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("active configuration revision must be an object")
        return payload
    finally:
        connection.close()


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        # Serialize the version read and the complete migration sequence. SQLite
        # serializes individual DDL writes, but without this lock two processes
        # can both observe user_version=0 and race the initial CREATE TABLE script.
        lock_fd = os.open(f"{path}.migrate.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            self.connection = sqlite3.connect(path, timeout=5.0)
            try:
                self.connection.row_factory = sqlite3.Row
                self.connection.execute("PRAGMA journal_mode=WAL")
                self.connection.execute("PRAGMA synchronous=FULL")
                self.connection.execute("PRAGMA foreign_keys=ON")
                self.connection.execute("PRAGMA busy_timeout=5000")
                self._migrate()
            except Exception:
                self.connection.close()
                raise
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def _migrate(self) -> None:
        version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"database schema {version} is newer than supported {SCHEMA_VERSION}")
        if version == 0:
            self.connection.executescript(
                """
                BEGIN;
                CREATE TABLE observations (
                    id INTEGER PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    publisher TEXT NOT NULL,
                    dedupe_scope TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    published_at INTEGER NOT NULL,
                    fetched_at INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    url TEXT NOT NULL,
                    attributes_json TEXT NOT NULL,
                    importance INTEGER NOT NULL DEFAULT 3,
                    urgency INTEGER NOT NULL DEFAULT 2,
                    relevance INTEGER NOT NULL DEFAULT 3,
                    confidence REAL NOT NULL DEFAULT 0.5,
                    region TEXT NOT NULL DEFAULT 'GLOBAL',
                    topic TEXT NOT NULL DEFAULT 'general',
                    source_tier TEXT NOT NULL DEFAULT 'secondary',
                    information_type TEXT NOT NULL DEFAULT 'report',
                    handling TEXT NOT NULL DEFAULT 'digest',
                    processing_state TEXT NOT NULL DEFAULT 'new',
                    UNIQUE (dedupe_scope, external_id)
                );

                CREATE TABLE incidents (
                    id INTEGER PRIMARY KEY,
                    incident_key TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL CHECK (kind IN ('event', 'stateful')),
                    status TEXT NOT NULL CHECK (status IN ('recorded', 'open', 'recovered')),
                    first_seen_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL,
                    recovered_at INTEGER,
                    confidence REAL NOT NULL DEFAULT 0.5,
                    evidence_json TEXT NOT NULL DEFAULT '[]',
                    source_ids_json TEXT NOT NULL DEFAULT '[]',
                    observation_count INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE reminders (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    schedule_kind TEXT NOT NULL CHECK (schedule_kind IN ('once', 'daily')),
                    run_at INTEGER,
                    daily_time TEXT,
                    timezone TEXT NOT NULL,
                    next_run_at INTEGER,
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                    priority INTEGER NOT NULL CHECK (priority BETWEEN 1 AND 5),
                    tags_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    last_enqueued_at INTEGER,
                    last_delivered_at INTEGER,
                    completed_at INTEGER,
                    CHECK (
                        (schedule_kind = 'once' AND run_at IS NOT NULL AND daily_time IS NULL)
                        OR (schedule_kind = 'daily' AND run_at IS NULL AND daily_time IS NOT NULL)
                    )
                );

                CREATE INDEX reminders_due_idx ON reminders(enabled, next_run_at);

                CREATE TABLE reminder_audit (
                    id INTEGER PRIMARY KEY,
                    reminder_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL
                );

                CREATE INDEX reminder_audit_time_idx ON reminder_audit(created_at, id);

                CREATE TABLE alerts (
                    id INTEGER PRIMARY KEY,
                    observation_id INTEGER REFERENCES observations(id),
                    incident_id INTEGER REFERENCES incidents(id),
                    reminder_id TEXT REFERENCES reminders(id) ON DELETE SET NULL,
                    rule_id TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    topic TEXT NOT NULL,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    priority INTEGER NOT NULL CHECK (priority BETWEEN 1 AND 5),
                    confidence REAL NOT NULL DEFAULT 0.5,
                    evidence_json TEXT NOT NULL DEFAULT '[]',
                    incident_key TEXT,
                    tags_json TEXT NOT NULL,
                    click_url TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'sending', 'delivered')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at INTEGER NOT NULL,
                    lease_until INTEGER,
                    last_error TEXT,
                    created_at INTEGER NOT NULL,
                    delivered_at INTEGER
                );

                CREATE INDEX alerts_due_idx
                    ON alerts(status, next_attempt_at, lease_until, priority, created_at);
                CREATE INDEX alerts_reminder_idx ON alerts(reminder_id, created_at);

                CREATE INDEX incidents_status_idx ON incidents(status, last_seen_at);

                CREATE TABLE collector_state (
                    source_id TEXT PRIMARY KEY,
                    initialized INTEGER NOT NULL DEFAULT 0,
                    etag TEXT,
                    last_modified TEXT,
                    last_attempt_at INTEGER,
                    last_success_at INTEGER,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    outage_alerted INTEGER NOT NULL DEFAULT 0,
                    outage_started_at INTEGER,
                    last_error TEXT,
                    cursor TEXT,
                    last_duration_ms INTEGER,
                    last_error_kind TEXT,
                    last_http_status INTEGER
                );

                CREATE TABLE config_revisions (
                    id INTEGER PRIMARY KEY,
                    revision INTEGER NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    active INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1))
                );

                CREATE TABLE config_audit (
                    id INTEGER PRIMARY KEY,
                    revision_id INTEGER REFERENCES config_revisions(id),
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL
                );

                CREATE INDEX config_audit_time_idx ON config_audit(created_at, id);

                CREATE TABLE prompts (
                    id INTEGER PRIMARY KEY,
                    prompt_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    system_text TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                    actor TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    UNIQUE (prompt_id, version)
                );

                CREATE INDEX prompts_active_idx ON prompts(prompt_id, active, version DESC);
                PRAGMA user_version=6;
                COMMIT;
                """
            )
            version = 6
        if version < 2:
            # Multiple systemd units may open the database during an upgrade.
            # Take the writer lock and re-read the version before altering.
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                locked_version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
                if locked_version == 1:
                    self.connection.execute("ALTER TABLE alerts ADD COLUMN confidence REAL NOT NULL DEFAULT 0.5")
                    self.connection.execute("ALTER TABLE alerts ADD COLUMN evidence_json TEXT NOT NULL DEFAULT '[]'")
                    self.connection.execute("ALTER TABLE alerts ADD COLUMN incident_key TEXT")
                    self.connection.execute("ALTER TABLE collector_state ADD COLUMN cursor TEXT")
                    self.connection.execute("PRAGMA user_version=2")
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
            version = 2
        if version < 3:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                locked_version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
                if locked_version < 3:
                    self.connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS incidents (
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
                        )
                        """
                    )
                    self.connection.execute("CREATE INDEX IF NOT EXISTS incidents_status_idx ON incidents(status, last_seen_at)")
                    alert_columns = {str(row[1]) for row in self.connection.execute("PRAGMA table_info(alerts)")}
                    if "incident_id" not in alert_columns:
                        self.connection.execute("ALTER TABLE alerts ADD COLUMN incident_id INTEGER REFERENCES incidents(id)")
                    self.connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS config_revisions (
                            id INTEGER PRIMARY KEY,
                            revision INTEGER NOT NULL UNIQUE,
                            payload_json TEXT NOT NULL,
                            actor TEXT NOT NULL,
                            reason TEXT NOT NULL,
                            created_at INTEGER NOT NULL,
                            active INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1))
                        )
                        """
                    )
                    self.connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS config_audit (
                            id INTEGER PRIMARY KEY,
                            revision_id INTEGER REFERENCES config_revisions(id),
                            action TEXT NOT NULL,
                            actor TEXT NOT NULL,
                            details_json TEXT NOT NULL DEFAULT '{}',
                            created_at INTEGER NOT NULL
                        )
                        """
                    )
                    self.connection.execute("CREATE INDEX IF NOT EXISTS config_audit_time_idx ON config_audit(created_at, id)")
                    collector_columns = {str(row[1]) for row in self.connection.execute("PRAGMA table_info(collector_state)")}
                    for name, definition in (
                        ("last_duration_ms", "INTEGER"),
                        ("last_error_kind", "TEXT"),
                        ("last_http_status", "INTEGER"),
                    ):
                        if name not in collector_columns:
                            self.connection.execute(f"ALTER TABLE collector_state ADD COLUMN {name} {definition}")
                    self.connection.execute("PRAGMA user_version=3")
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
            version = 3
        if version < 4:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                locked_version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
                if locked_version < 4:
                    self.connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS reminders (
                            id TEXT PRIMARY KEY,
                            title TEXT NOT NULL,
                            message TEXT NOT NULL,
                            schedule_kind TEXT NOT NULL CHECK (schedule_kind IN ('once', 'daily')),
                            run_at INTEGER,
                            daily_time TEXT,
                            timezone TEXT NOT NULL,
                            next_run_at INTEGER,
                            enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                            priority INTEGER NOT NULL CHECK (priority BETWEEN 1 AND 5),
                            tags_json TEXT NOT NULL,
                            created_at INTEGER NOT NULL,
                            updated_at INTEGER NOT NULL,
                            last_enqueued_at INTEGER,
                            last_delivered_at INTEGER,
                            completed_at INTEGER,
                            CHECK (
                                (schedule_kind = 'once' AND run_at IS NOT NULL AND daily_time IS NULL)
                                OR (schedule_kind = 'daily' AND run_at IS NULL AND daily_time IS NOT NULL)
                            )
                        )
                        """
                    )
                    self.connection.execute(
                        "CREATE INDEX IF NOT EXISTS reminders_due_idx ON reminders(enabled, next_run_at)"
                    )
                    self.connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS reminder_audit (
                            id INTEGER PRIMARY KEY,
                            reminder_id TEXT NOT NULL,
                            action TEXT NOT NULL,
                            actor TEXT NOT NULL,
                            details_json TEXT NOT NULL DEFAULT '{}',
                            created_at INTEGER NOT NULL
                        )
                        """
                    )
                    self.connection.execute(
                        "CREATE INDEX IF NOT EXISTS reminder_audit_time_idx ON reminder_audit(created_at, id)"
                    )
                    alert_columns = {str(row[1]) for row in self.connection.execute("PRAGMA table_info(alerts)")}
                    if "reminder_id" not in alert_columns:
                        self.connection.execute(
                            "ALTER TABLE alerts ADD COLUMN reminder_id TEXT REFERENCES reminders(id) ON DELETE SET NULL"
                        )
                    self.connection.execute(
                        "CREATE INDEX IF NOT EXISTS alerts_reminder_idx ON alerts(reminder_id, created_at)"
                    )
                    self.connection.execute("PRAGMA user_version=4")
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
            version = 4
        if version < 5:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                locked_version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
                if locked_version < 5:
                    # Rows created before schema 2 can return ALTER TABLE defaults
                    # logically while still missing the fields in their record body.
                    # Rewriting materializes those defaults so integrity_check is clean.
                    self.connection.execute(
                        """
                        UPDATE alerts
                        SET confidence = COALESCE(confidence, 0.5),
                            evidence_json = COALESCE(evidence_json, '[]')
                        """
                    )
                    self.connection.execute("PRAGMA user_version=5")
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
            version = 5
        if version < 6:
            # SQLite cannot widen an existing CHECK constraint in place. Rebuild
            # only the incidents table while preserving IDs referenced by alerts.
            self.connection.commit()
            self.connection.execute("PRAGMA foreign_keys=OFF")
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                locked_version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
                if locked_version < 6:
                    self.connection.execute(
                        """
                        CREATE TABLE incidents_v6 (
                            id INTEGER PRIMARY KEY,
                            incident_key TEXT NOT NULL UNIQUE,
                            kind TEXT NOT NULL CHECK (kind IN ('event', 'stateful')),
                            status TEXT NOT NULL CHECK (status IN ('recorded', 'open', 'recovered')),
                            first_seen_at INTEGER NOT NULL,
                            last_seen_at INTEGER NOT NULL,
                            recovered_at INTEGER,
                            confidence REAL NOT NULL DEFAULT 0.5,
                            evidence_json TEXT NOT NULL DEFAULT '[]',
                            source_ids_json TEXT NOT NULL DEFAULT '[]',
                            observation_count INTEGER NOT NULL DEFAULT 0,
                            updated_at INTEGER NOT NULL
                        )
                        """
                    )
                    self.connection.execute(
                        """
                        WITH classified AS (
                            SELECT i.*,
                                CASE WHEN EXISTS (
                                    SELECT 1
                                    FROM alerts AS a
                                    LEFT JOIN observations AS o ON o.id = a.observation_id
                                    WHERE a.incident_id = i.id
                                      AND (
                                          a.rule_id IN ('system.source_failure', 'system.source_recovery')
                                          OR
                                          instr(COALESCE(o.attributes_json, ''), '"check"') > 0
                                          OR instr(COALESCE(o.attributes_json, ''), '"symbol"') > 0
                                          OR instr(COALESCE(o.attributes_json, ''), '"stateful"') > 0
                                      )
                                ) THEN 'stateful' ELSE 'event' END AS migrated_kind
                            FROM incidents AS i
                        )
                        INSERT INTO incidents_v6(
                            id, incident_key, kind, status, first_seen_at, last_seen_at,
                            recovered_at, confidence, evidence_json, source_ids_json,
                            observation_count, updated_at
                        )
                        SELECT id, incident_key, migrated_kind,
                            CASE WHEN migrated_kind = 'event' THEN 'recorded' ELSE status END,
                            first_seen_at, last_seen_at,
                            CASE WHEN migrated_kind = 'event' THEN NULL ELSE recovered_at END,
                            confidence, evidence_json, source_ids_json,
                            observation_count, updated_at
                        FROM classified
                        """
                    )
                    self.connection.execute("DROP TABLE incidents")
                    self.connection.execute("ALTER TABLE incidents_v6 RENAME TO incidents")
                    self.connection.execute(
                        "CREATE INDEX incidents_status_idx ON incidents(status, last_seen_at)"
                    )
                    self.connection.execute("PRAGMA user_version=6")
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
            finally:
                self.connection.execute("PRAGMA foreign_keys=ON")
            if self.connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError("database foreign key check failed after schema 6 migration")
        if version < 7:
            self.connection.commit()
            self.connection.execute("PRAGMA foreign_keys=OFF")
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                locked_version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
                if locked_version < 7:
                    self.connection.execute(
                        """
                        CREATE TABLE alerts_v7 (
                            id INTEGER PRIMARY KEY,
                            observation_id INTEGER REFERENCES observations(id),
                            incident_id INTEGER REFERENCES incidents(id),
                            reminder_id TEXT REFERENCES reminders(id) ON DELETE SET NULL,
                            rule_id TEXT NOT NULL,
                            dedupe_key TEXT NOT NULL UNIQUE,
                            topic TEXT NOT NULL,
                            title TEXT NOT NULL,
                            message TEXT NOT NULL,
                            priority INTEGER NOT NULL CHECK (priority BETWEEN 1 AND 5),
                            confidence REAL NOT NULL DEFAULT 0.5,
                            evidence_json TEXT NOT NULL DEFAULT '[]',
                            incident_key TEXT,
                            tags_json TEXT NOT NULL,
                            click_url TEXT NOT NULL,
                            status TEXT NOT NULL CHECK (
                                status IN ('pending', 'sending', 'delivered', 'dead', 'cancelled')
                            ),
                            attempts INTEGER NOT NULL DEFAULT 0,
                            next_attempt_at INTEGER NOT NULL,
                            lease_until INTEGER,
                            last_error TEXT,
                            failure_kind TEXT,
                            created_at INTEGER NOT NULL,
                            delivered_at INTEGER,
                            dead_at INTEGER,
                            retry_started_at INTEGER,
                            config_revision INTEGER,
                            rule_hash TEXT,
                            collector_version TEXT
                        )
                        """
                    )
                    self.connection.execute(
                        """
                        INSERT INTO alerts_v7(
                            id, observation_id, incident_id, reminder_id, rule_id, dedupe_key,
                            topic, title, message, priority, confidence, evidence_json,
                            incident_key, tags_json, click_url, status, attempts,
                            next_attempt_at, lease_until, last_error, created_at, delivered_at
                        )
                        SELECT id, observation_id, incident_id, reminder_id, rule_id, dedupe_key,
                            topic, title, message, priority, confidence, evidence_json,
                            incident_key, tags_json, click_url, status, attempts,
                            next_attempt_at, lease_until, last_error, created_at, delivered_at
                        FROM alerts
                        """
                    )
                    self.connection.execute("DROP TABLE alerts")
                    self.connection.execute("ALTER TABLE alerts_v7 RENAME TO alerts")
                    self.connection.execute(
                        "CREATE INDEX alerts_due_idx ON alerts(status, next_attempt_at, lease_until, priority, created_at)"
                    )
                    self.connection.execute(
                        "CREATE INDEX alerts_reminder_idx ON alerts(reminder_id, created_at)"
                    )
                    self.connection.execute(
                        "CREATE INDEX alerts_observation_idx ON alerts(observation_id)"
                    )
                    self.connection.execute(
                        "CREATE INDEX alerts_incident_idx ON alerts(incident_id, created_at DESC, id DESC)"
                    )
                    self.connection.execute(
                        "CREATE INDEX IF NOT EXISTS observations_fetched_idx ON observations(fetched_at)"
                    )
                    collector_columns = {
                        str(row[1]) for row in self.connection.execute("PRAGMA table_info(collector_state)")
                    }
                    for name, definition in (
                        ("poll_count", "INTEGER NOT NULL DEFAULT 0"),
                        ("success_count", "INTEGER NOT NULL DEFAULT 0"),
                        ("failure_count", "INTEGER NOT NULL DEFAULT 0"),
                        ("last_observation_count", "INTEGER"),
                        ("last_alert_count", "INTEGER"),
                        ("last_warning", "TEXT"),
                    ):
                        if name not in collector_columns:
                            self.connection.execute(
                                f"ALTER TABLE collector_state ADD COLUMN {name} {definition}"
                            )
                    self.connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS source_runtime (
                            source_id TEXT PRIMARY KEY,
                            kind TEXT NOT NULL,
                            configured_enabled INTEGER NOT NULL CHECK (configured_enabled IN (0, 1)),
                            runtime_status TEXT NOT NULL CHECK (
                                runtime_status IN (
                                    'disabled', 'configured', 'starting', 'active',
                                    'degraded', 'invalid', 'stale'
                                )
                            ),
                            expected_interval_seconds INTEGER NOT NULL,
                            config_revision INTEGER,
                            registered_at INTEGER,
                            heartbeat_at INTEGER,
                            last_error TEXT,
                            updated_at INTEGER NOT NULL
                        )
                        """
                    )
                    self.connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS engine_runtime (
                            id INTEGER PRIMARY KEY CHECK (id = 1),
                            instance_id TEXT,
                            state TEXT NOT NULL CHECK (
                                state IN ('offline', 'starting', 'running', 'degraded', 'stopping', 'failed')
                            ),
                            started_at INTEGER,
                            heartbeat_at INTEGER,
                            applied_revision INTEGER,
                            applied_at INTEGER,
                            code_version TEXT,
                            configured_sources INTEGER NOT NULL DEFAULT 0,
                            active_sources INTEGER NOT NULL DEFAULT 0,
                            last_error TEXT
                        )
                        """
                    )
                    self.connection.execute(
                        "INSERT OR IGNORE INTO engine_runtime(id, state) VALUES (1, 'offline')"
                    )
                    self.connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS admin_jobs (
                            id TEXT PRIMARY KEY,
                            kind TEXT NOT NULL,
                            status TEXT NOT NULL CHECK (
                                status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')
                            ),
                            request_json TEXT NOT NULL,
                            result_json TEXT,
                            error TEXT,
                            actor TEXT NOT NULL,
                            created_at INTEGER NOT NULL,
                            started_at INTEGER,
                            completed_at INTEGER,
                            expires_at INTEGER NOT NULL
                        )
                        """
                    )
                    self.connection.execute(
                        "CREATE INDEX IF NOT EXISTS admin_jobs_status_idx ON admin_jobs(status, created_at)"
                    )

                    self.connection.execute("PRAGMA user_version=7")
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
            finally:
                self.connection.execute("PRAGMA foreign_keys=ON")
            if self.connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError("database foreign key check failed after schema 7 migration")

        if version < 8:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                locked_version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
                if locked_version < 8:
                    observation_columns = {
                        str(row[1]) for row in self.connection.execute("PRAGMA table_info(observations)")
                    }
                    columns = (
                        ("importance", "INTEGER NOT NULL DEFAULT 3"),
                        ("urgency", "INTEGER NOT NULL DEFAULT 2"),
                        ("relevance", "INTEGER NOT NULL DEFAULT 3"),
                        ("confidence", "REAL NOT NULL DEFAULT 0.5"),
                        ("region", "TEXT NOT NULL DEFAULT 'GLOBAL'"),
                        ("topic", "TEXT NOT NULL DEFAULT 'general'"),
                        ("source_tier", "TEXT NOT NULL DEFAULT 'secondary'"),
                        ("information_type", "TEXT NOT NULL DEFAULT 'report'"),
                        ("handling", "TEXT NOT NULL DEFAULT 'digest'"),
                        ("processing_state", "TEXT NOT NULL DEFAULT 'new'"),
                    )
                    for name, definition in columns:
                        if name not in observation_columns:
                            self.connection.execute(
                                f"ALTER TABLE observations ADD COLUMN {name} {definition}"
                            )
                    self.connection.execute(
                        "CREATE INDEX IF NOT EXISTS observations_handling_idx "
                        "ON observations(handling, fetched_at DESC, id DESC)"
                    )
                    self.connection.execute("PRAGMA user_version=8")
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
            version = 8

        if version < 9:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                locked_version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
                if locked_version < 9:
                    self.connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS prompts (
                            id INTEGER PRIMARY KEY,
                            prompt_id TEXT NOT NULL,
                            version INTEGER NOT NULL,
                            system_text TEXT NOT NULL,
                            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                            actor TEXT NOT NULL,
                            created_at INTEGER NOT NULL,
                            UNIQUE (prompt_id, version)
                        )
                        """
                    )
                    self.connection.execute(
                        "CREATE INDEX IF NOT EXISTS prompts_active_idx "
                        "ON prompts(prompt_id, active, version DESC)"
                    )
                    self.connection.execute(
                        """
                        INSERT OR IGNORE INTO prompts(prompt_id, version, system_text, active, actor, created_at)
                        VALUES (?, ?, ?, 1, ?, ?)
                        """,
                        (TRIAGE_V1.prompt_id, TRIAGE_V1.version, TRIAGE_V1.system_text, "system", int(time.time())),
                    )
                    self.connection.execute("PRAGMA user_version=9")
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
            version = 9

        if version < 10:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                locked_version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
                if locked_version < 10:
                    self.connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS digests (
                            id INTEGER PRIMARY KEY,
                            digest_key TEXT NOT NULL,
                            version INTEGER NOT NULL,
                            period_start INTEGER NOT NULL,
                            period_end INTEGER NOT NULL,
                            timezone TEXT NOT NULL,
                            title TEXT NOT NULL,
                            summary TEXT NOT NULL,
                            generation_kind TEXT NOT NULL CHECK (
                                generation_kind IN ('algorithm', 'api')
                            ),
                            status TEXT NOT NULL CHECK (
                                status IN ('draft', 'published', 'superseded')
                            ),
                            created_at INTEGER NOT NULL,
                            published_at INTEGER,
                            UNIQUE (digest_key, version),
                            CHECK (period_end > period_start)
                        )
                        """
                    )
                    self.connection.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS digests_one_published_idx "
                        "ON digests(digest_key) WHERE status = 'published'"
                    )
                    self.connection.execute(
                        "CREATE INDEX IF NOT EXISTS digests_period_idx "
                        "ON digests(period_start DESC, digest_key, version DESC)"
                    )
                    self.connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS digest_items (
                            id INTEGER PRIMARY KEY,
                            digest_id INTEGER NOT NULL REFERENCES digests(id) ON DELETE CASCADE,
                            position INTEGER NOT NULL,
                            cluster_key TEXT NOT NULL,
                            title TEXT NOT NULL,
                            summary TEXT NOT NULL,
                            score REAL NOT NULL,
                            importance INTEGER NOT NULL CHECK (importance BETWEEN 1 AND 5),
                            urgency INTEGER NOT NULL CHECK (urgency BETWEEN 1 AND 5),
                            relevance INTEGER NOT NULL CHECK (relevance BETWEEN 1 AND 5),
                            confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
                            published_at INTEGER NOT NULL,
                            regions_json TEXT NOT NULL,
                            topics_json TEXT NOT NULL,
                            source_ids_json TEXT NOT NULL,
                            observation_ids_json TEXT NOT NULL,
                            links_json TEXT NOT NULL,
                            UNIQUE (digest_id, position),
                            UNIQUE (digest_id, cluster_key)
                        )
                        """
                    )
                    self.connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS digest_source_coverage (
                            digest_id INTEGER NOT NULL REFERENCES digests(id) ON DELETE CASCADE,
                            source_id TEXT NOT NULL,
                            status TEXT NOT NULL CHECK (
                                status IN (
                                    'covered', 'quiet', 'degraded',
                                    'stale', 'disabled', 'unknown'
                                )
                            ),
                            observation_count INTEGER NOT NULL CHECK (observation_count >= 0),
                            last_attempt_at INTEGER,
                            last_success_at INTEGER,
                            consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK (
                                consecutive_failures >= 0
                            ),
                            PRIMARY KEY (digest_id, source_id)
                        )
                        """
                    )
                    self.connection.execute("PRAGMA user_version=10")
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
            version = 10

        if version < 11:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                locked_version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
                if locked_version < 11:
                    observation_columns = {
                        str(row[1])
                        for row in self.connection.execute("PRAGMA table_info(observations)")
                    }
                    for name, definition in (
                        ("analysis_lease_token", "TEXT"),
                        ("analysis_lease_until", "INTEGER"),
                        ("analysis_attempts", "INTEGER NOT NULL DEFAULT 0"),
                        ("analysis_error", "TEXT"),
                        ("analyzed_at", "INTEGER"),
                    ):
                        if name not in observation_columns:
                            self.connection.execute(
                                f"ALTER TABLE observations ADD COLUMN {name} {definition}"
                            )
                    self.connection.execute(
                        "CREATE INDEX IF NOT EXISTS observations_analysis_due_idx "
                        "ON observations(processing_state, analysis_lease_until, fetched_at, id)"
                    )
                    self.connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS analysis_runs (
                            id INTEGER PRIMARY KEY,
                            observation_id INTEGER NOT NULL REFERENCES observations(id)
                                ON DELETE CASCADE,
                            analyzer TEXT NOT NULL,
                            outcome TEXT NOT NULL CHECK (
                                outcome IN ('succeeded', 'failed', 'budget_exhausted')
                            ),
                            shadow INTEGER NOT NULL CHECK (shadow IN (0, 1)),
                            advisory_json TEXT,
                            error TEXT,
                            duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
                            created_at INTEGER NOT NULL
                        )
                        """
                    )
                    self.connection.execute(
                        "CREATE INDEX IF NOT EXISTS analysis_runs_observation_idx "
                        "ON analysis_runs(observation_id, created_at, id)"
                    )
                    self.connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS analysis_api_usage (
                            budget_day TEXT PRIMARY KEY,
                            calls INTEGER NOT NULL CHECK (calls >= 0),
                            updated_at INTEGER NOT NULL
                        )
                        """
                    )
                    self.connection.execute("PRAGMA user_version=11")
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
            version = 11

        if version < 12:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                locked_version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
                if locked_version < 12:
                    digest_columns = {
                        str(row[1])
                        for row in self.connection.execute("PRAGMA table_info(digest_items)")
                    }
                    if "handling" not in digest_columns:
                        self.connection.execute(
                            "ALTER TABLE digest_items ADD COLUMN handling TEXT NOT NULL DEFAULT 'digest' "
                            "CHECK (handling IN ('digest', 'immediate'))"
                        )
                    self.connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS source_quality_feedback (
                            id INTEGER PRIMARY KEY,
                            source_id TEXT NOT NULL,
                            observation_id INTEGER REFERENCES observations(id) ON DELETE SET NULL,
                            signal INTEGER NOT NULL CHECK (signal IN (-1, 0, 1)),
                            reason TEXT NOT NULL,
                            actor TEXT NOT NULL,
                            created_at INTEGER NOT NULL
                        )
                        """
                    )
                    self.connection.execute(
                        "CREATE INDEX IF NOT EXISTS source_quality_feedback_source_time_idx "
                        "ON source_quality_feedback(source_id, created_at, id)"
                    )
                    self.connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS source_quality_overrides (
                            source_id TEXT PRIMARY KEY,
                            weight REAL NOT NULL CHECK (weight BETWEEN 0.70 AND 1.15),
                            reason TEXT NOT NULL,
                            actor TEXT NOT NULL,
                            created_at INTEGER NOT NULL,
                            updated_at INTEGER NOT NULL
                        )
                        """
                    )
                    self.connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS source_quality_state (
                            source_id TEXT PRIMARY KEY,
                            automatic_weight REAL NOT NULL CHECK (
                                automatic_weight BETWEEN 0.70 AND 1.15
                            ),
                            calculated_at INTEGER NOT NULL
                        )
                        """
                    )
                    self.connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS source_quality_audit (
                            id INTEGER PRIMARY KEY,
                            source_id TEXT NOT NULL,
                            action TEXT NOT NULL CHECK (
                                action IN ('feedback', 'override_set', 'override_cleared')
                            ),
                            actor TEXT NOT NULL,
                            details_json TEXT NOT NULL,
                            created_at INTEGER NOT NULL
                        )
                        """
                    )
                    self.connection.execute(
                        "CREATE INDEX IF NOT EXISTS source_quality_audit_source_time_idx "
                        "ON source_quality_audit(source_id, created_at DESC, id DESC)"
                    )
                    self.connection.execute("PRAGMA user_version=12")
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
            version = 12

    def close(self) -> None:
        self.connection.close()

    def unit_of_work(self, *, immediate: bool = True) -> SQLiteUnitOfWork:
        return SQLiteUnitOfWork(self.connection, immediate=immediate)
    def register_engine(
        self,
        instance_id: str,
        *,
        started_at: int,
        applied_revision: int | None,
        code_version: str,
        configured_sources: int,
        active_sources: int,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE engine_runtime
                SET instance_id = ?, state = ?, started_at = ?, heartbeat_at = ?,
                    applied_revision = ?, applied_at = ?, code_version = ?,
                    configured_sources = ?, active_sources = ?, last_error = NULL
                WHERE id = 1
                """,
                (
                    instance_id,
                    "running" if configured_sources == active_sources else "degraded",
                    started_at,
                    started_at,
                    applied_revision,
                    started_at,
                    code_version,
                    configured_sources,
                    active_sources,
                ),
            )

    def heartbeat_engine(self, instance_id: str, now: int, *, state: str = "running") -> bool:
        with self.connection:
            updated = self.connection.execute(
                """
                UPDATE engine_runtime SET heartbeat_at = ?, state = ?
                WHERE id = 1 AND instance_id = ?
                """,
                (now, state, instance_id),
            )
            if updated.rowcount == 1:
                self.connection.execute(
                    "UPDATE source_runtime SET heartbeat_at = ?, updated_at = ? WHERE runtime_status IN ('active', 'degraded')",
                    (now, now),
                )
        return updated.rowcount == 1

    def stop_engine(self, instance_id: str, now: int, *, error: str | None = None) -> None:
        state = "failed" if error else "offline"
        with self.connection:
            updated = self.connection.execute(
                """
                UPDATE engine_runtime SET state = ?, heartbeat_at = ?, last_error = ?
                WHERE id = 1 AND instance_id = ?
                """,
                (state, now, sanitize_error(error) if error else None, instance_id),
            )
            if updated.rowcount != 1:
                return
            self.connection.execute(
                """
                UPDATE source_runtime SET runtime_status = CASE
                    WHEN configured_enabled = 1 THEN 'stale' ELSE 'disabled' END,
                    updated_at = ?
                """,
                (now,),
            )

    def sync_source_runtime(
        self,
        sources: list[tuple[str, str, bool, int]],
        active_source_ids: set[str],
        *,
        config_revision: int | None,
        now: int,
    ) -> None:
        identifiers = {source_id for source_id, _, _, _ in sources}
        with self.unit_of_work():
            if identifiers:
                placeholders = ",".join("?" for _ in identifiers)
                self.connection.execute(
                    f"DELETE FROM source_runtime WHERE source_id NOT IN ({placeholders})",
                    tuple(sorted(identifiers)),
                )
            else:
                self.connection.execute("DELETE FROM source_runtime")
            for source_id, kind, enabled, interval in sources:
                self.connection.execute(
                    "INSERT OR IGNORE INTO collector_state(source_id) VALUES (?)",
                    (source_id,),
                )
                status = "active" if source_id in active_source_ids else "disabled" if not enabled else "invalid"
                self.connection.execute(
                    """
                    INSERT INTO source_runtime(
                        source_id, kind, configured_enabled, runtime_status,
                        expected_interval_seconds, config_revision, registered_at,
                        heartbeat_at, last_error, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                    ON CONFLICT(source_id) DO UPDATE SET
                        kind = excluded.kind,
                        configured_enabled = excluded.configured_enabled,
                        runtime_status = excluded.runtime_status,
                        expected_interval_seconds = excluded.expected_interval_seconds,
                        config_revision = excluded.config_revision,
                        registered_at = excluded.registered_at,
                        heartbeat_at = excluded.heartbeat_at,
                        last_error = NULL,
                        updated_at = excluded.updated_at
                    """,
                    (
                        source_id,
                        kind,
                        int(enabled),
                        status,
                        interval,
                        config_revision,
                        now if source_id in active_source_ids else None,
                        now if source_id in active_source_ids else None,
                        now,
                    ),
                )

    def mark_source_runtime(
        self,
        source_id: str,
        status: str,
        now: int,
        error: BaseException | str | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE source_runtime
                SET runtime_status = ?, heartbeat_at = ?, last_error = ?, updated_at = ?
                WHERE source_id = ?
                """,
                (
                    status,
                    now,
                    sanitize_error(error) if error is not None else None,
                    now,
                    source_id,
                ),
            )

    def create_admin_job(
        self,
        kind: str,
        request: Mapping[str, Any],
        actor: str,
        now: int,
        *,
        ttl_seconds: int = 900,
    ) -> dict[str, Any]:
        job_id = uuid.uuid4().hex
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO admin_jobs(
                    id, kind, status, request_json, actor, created_at, expires_at
                ) VALUES (?, ?, 'queued', ?, ?, ?, ?)
                """,
                (
                    job_id,
                    kind[:64],
                    json.dumps(dict(request), ensure_ascii=False, sort_keys=True),
                    actor[:128],
                    now,
                    now + max(60, min(ttl_seconds, 86400)),
                ),
            )
        result = self.get_admin_job(job_id)
        assert result is not None
        return result

    def claim_admin_job(self, kind: str, now: int) -> dict[str, Any] | None:
        with self.unit_of_work():
            self.connection.execute(
                "UPDATE admin_jobs SET status = 'failed', error = 'job expired', completed_at = ? WHERE status IN ('queued', 'running') AND expires_at <= ?",
                (now, now),
            )
            # Jobs are processed serially by the singleton daemon. A running row
            # seen while claiming the next job therefore belongs to an interrupted
            # worker and must become terminal instead of remaining stuck forever.
            self.connection.execute(
                "UPDATE admin_jobs SET status = 'failed', error = 'worker interrupted', completed_at = ? WHERE status = 'running'",
                (now,),
            )
            row = self.connection.execute(
                "SELECT id FROM admin_jobs WHERE kind = ? AND status = 'queued' ORDER BY created_at, id LIMIT 1",
                (kind,),
            ).fetchone()
            if row is None:
                return None
            job_id = str(row["id"])
            self.connection.execute(
                "UPDATE admin_jobs SET status = 'running', started_at = ? WHERE id = ? AND status = 'queued'",
                (now, job_id),
            )
        return self.get_admin_job(job_id)

    def finish_admin_job(
        self,
        job_id: str,
        now: int,
        *,
        result: Mapping[str, Any] | None = None,
        error: BaseException | str | None = None,
    ) -> bool:
        status = "failed" if error is not None else "succeeded"
        with self.connection:
            updated = self.connection.execute(
                """
                UPDATE admin_jobs
                SET status = ?, result_json = ?, error = ?, completed_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (
                    status,
                    json.dumps(dict(result or {}), ensure_ascii=False, sort_keys=True),
                    sanitize_error(error) if error is not None else None,
                    now,
                    job_id,
                ),
            )
        return updated.rowcount == 1

    def cancel_admin_job(self, job_id: str, now: int) -> bool:
        with self.connection:
            updated = self.connection.execute(
                "UPDATE admin_jobs SET status = 'cancelled', completed_at = ? WHERE id = ? AND status = 'queued'",
                (now, job_id),
            )
        return updated.rowcount == 1

    def get_admin_job(self, job_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM admin_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        try:
            item["request"] = json.loads(item.pop("request_json"))
        except (TypeError, ValueError):
            item["request"] = {}
        try:
            item["result"] = json.loads(item.pop("result_json")) if item.get("result_json") else None
        except (TypeError, ValueError):
            item["result"] = None
        return item

    def get_source_state(self, source_id: str) -> SourceState:
        self.connection.execute(
            "INSERT OR IGNORE INTO collector_state(source_id) VALUES (?)", (source_id,)
        )
        self.connection.commit()
        row = self.connection.execute(
            "SELECT * FROM collector_state WHERE source_id = ?", (source_id,)
        ).fetchone()
        assert row is not None
        return SourceState(
            source_id=source_id,
            initialized=bool(row["initialized"]),
            etag=row["etag"],
            last_modified=row["last_modified"],
            last_attempt_at=row["last_attempt_at"],
            last_success_at=row["last_success_at"],
            consecutive_failures=int(row["consecutive_failures"]),
            outage_alerted=bool(row["outage_alerted"]),
            outage_started_at=row["outage_started_at"],
            cursor=row["cursor"],
        )

    def _insert_alert(
        self,
        candidate: AlertCandidate,
        observation_id: int | None,
        now: int,
        reminder_id: str | None = None,
    ) -> bool:
        incident_id: int | None = None
        if candidate.incident_key:
            incident_id = self._upsert_incident(candidate, observation_id, now)
            existing = self.connection.execute(
                """
                SELECT 1 FROM alerts
                WHERE rule_id = ? AND incident_key = ? AND created_at >= ?
                LIMIT 1
                """,
                (candidate.rule_id, candidate.incident_key, now - 1800),
            ).fetchone()
            if existing is not None and not candidate.recovery:
                return False
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO alerts(
                observation_id, incident_id, reminder_id, rule_id, dedupe_key, topic, title, message,
                priority, confidence, evidence_json, incident_key, tags_json, click_url,
                status, next_attempt_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                observation_id,
                incident_id,
                reminder_id,
                candidate.rule_id,
                candidate.dedupe_key,
                candidate.topic,
                candidate.title,
                candidate.message,
                candidate.priority,
                candidate.confidence,
                json.dumps(candidate.evidence, ensure_ascii=False),
                candidate.incident_key,
                json.dumps(candidate.tags, ensure_ascii=False),
                candidate.click_url,
                now,
                now,
            ),
        )
        return cursor.rowcount == 1

    def _upsert_incident(
        self,
        candidate: AlertCandidate,
        observation_id: int | None,
        now: int,
    ) -> int:
        """Create or update the durable event or stateful incident."""
        assert candidate.incident_key
        if candidate.incident_kind not in {"event", "stateful"}:
            raise ValueError(f"unsupported incident kind: {candidate.incident_kind}")
        status = (
            "recorded"
            if candidate.incident_kind == "event"
            else "recovered" if candidate.recovery else "open"
        )
        row = self.connection.execute(
            "SELECT * FROM incidents WHERE incident_key = ?",
            (candidate.incident_key,),
        ).fetchone()
        if row is None:
            source_ids: list[str] = []
            if observation_id is not None:
                source_row = self.connection.execute(
                    "SELECT source_id FROM observations WHERE id = ?", (observation_id,)
                ).fetchone()
                if source_row:
                    source_ids.append(str(source_row["source_id"]))
            self.connection.execute(
                """
                INSERT INTO incidents(
                    incident_key, kind, status, first_seen_at, last_seen_at, recovered_at,
                    confidence, evidence_json, source_ids_json, observation_count, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.incident_key,
                    candidate.incident_kind,
                    status,
                    now,
                    now,
                    now if status == "recovered" else None,
                    candidate.confidence,
                    json.dumps(list(candidate.evidence), ensure_ascii=False),
                    json.dumps(source_ids, ensure_ascii=False),
                    1 if observation_id is not None else 0,
                    now,
                ),
            )
            return int(self.connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        try:
            evidence = set(json.loads(row["evidence_json"]))
        except (TypeError, ValueError):
            evidence = set()
        evidence.update(candidate.evidence)
        try:
            source_ids = set(json.loads(row["source_ids_json"]))
        except (TypeError, ValueError):
            source_ids = set()
        if observation_id is not None:
            source_row = self.connection.execute(
                "SELECT source_id FROM observations WHERE id = ?", (observation_id,)
            ).fetchone()
            if source_row:
                source_ids.add(str(source_row["source_id"]))
        recovered_at = now if status == "recovered" else None
        self.connection.execute(
            """
            UPDATE incidents SET kind = ?, status = ?, last_seen_at = ?, recovered_at = ?,
                confidence = MAX(confidence, ?), evidence_json = ?, source_ids_json = ?,
                observation_count = observation_count + ?, updated_at = ?
            WHERE id = ?
            """,
            (
                candidate.incident_kind,
                status,
                now,
                recovered_at,
                candidate.confidence,
                json.dumps(sorted(evidence), ensure_ascii=False),
                json.dumps(sorted(source_ids), ensure_ascii=False),
                1 if observation_id is not None else 0,
                now,
                int(row["id"]),
            ),
        )
        return int(row["id"])

    def record_source_success(
        self,
        source_id: str,
        result: FeedFetchResult,
        rules: RuleSet,
        now: int,
        default_topic: str,
        duration_ms: int | None = None,
    ) -> IngestReport:
        state = self.get_source_state(source_id)
        inserted = 0
        queued = 0
        recovery_queued = False
        with self.unit_of_work():
            for observation in result.observations:
                analysis = analyze_observation(observation)
                cursor = self.connection.execute(
                    """
                    INSERT OR IGNORE INTO observations(
                        source_id, publisher, dedupe_scope, external_id,
                        published_at, fetched_at, title, summary, url, attributes_json,
                        importance, urgency, relevance, confidence, region, topic,
                        source_tier, information_type, handling, processing_state
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        observation.source_id,
                        observation.publisher,
                        observation.dedupe_scope,
                        observation.external_id,
                        to_epoch(observation.published_at),
                        now,
                        observation.title,
                        observation.summary,
                        observation.url,
                        json.dumps(observation.attributes, ensure_ascii=False, sort_keys=True),
                        analysis.importance,
                        analysis.urgency,
                        analysis.relevance,
                        analysis.confidence,
                        analysis.region,
                        analysis.topic,
                        analysis.source_tier,
                        analysis.information_type,
                        analysis.handling,
                        observation.processing_state,
                    ),
                )
                if cursor.rowcount != 1:
                    continue
                inserted += 1
                observation_id = int(cursor.lastrowid)
                if state.initialized:
                    for candidate in rules.evaluate(observation, now):
                        if self._insert_alert(candidate, observation_id, now):
                            queued += 1

            if state.outage_alerted:
                outage_identity = state.outage_started_at or now
                recovery = AlertCandidate(
                    rule_id="system.source_recovery",
                    dedupe_key=f"source-recovery:{source_id}:{outage_identity}",
                    title="Argus 数据源已恢复",
                    message=f"数据源 {source_id} 已恢复正常采集。",
                    priority=2,
                    tags=("white_check_mark",),
                    click_url="",
                    topic=default_topic,
                    incident_key=f"system:source:{source_id}",
                    incident_kind="stateful",
                    recovery=True,
                )
                recovery_queued = self._insert_alert(recovery, None, now)
                queued += int(recovery_queued)

            self.connection.execute(
                """
                UPDATE collector_state
                SET initialized = 1,
                    etag = ?,
                    last_modified = ?,
                    last_attempt_at = ?,
                    last_success_at = ?,
                    consecutive_failures = 0,
                    outage_alerted = 0,
                    outage_started_at = NULL,
                    last_error = NULL,
                    cursor = ?,
                    last_duration_ms = ?,
                    last_error_kind = NULL,
                    last_http_status = NULL,
                    poll_count = poll_count + 1,
                    success_count = success_count + 1,
                    last_observation_count = ?,
                    last_alert_count = ?,
                    last_warning = ?
                WHERE source_id = ?
                """,
                (
                    result.etag,
                    result.last_modified,
                    now,
                    now,
                    result.cursor,
                    duration_ms,
                    inserted,
                    queued,
                    "; ".join(result.warnings)[:1024] if result.warnings else None,
                    source_id,
                ),
            )
        return IngestReport(
            inserted_observations=inserted,
            queued_alerts=queued,
            baseline_created=not state.initialized,
            recovery_queued=recovery_queued,
        )

    def record_source_failure(
        self,
        source_id: str,
        error: BaseException | str,
        threshold: int,
        default_topic: str,
        now: int,
        duration_ms: int | None = None,
        error_kind: str | None = None,
        http_status: int | None = None,
    ) -> bool:
        state = self.get_source_state(source_id)
        failures = state.consecutive_failures + 1
        outage_started = state.outage_started_at or now
        should_alert = failures >= threshold and not state.outage_alerted
        error_text = sanitize_error(error)
        with self.unit_of_work():
            alert_inserted = False
            if should_alert:
                candidate = AlertCandidate(
                    rule_id="system.source_failure",
                    dedupe_key=f"source-failure:{source_id}:{outage_started}",
                    title="Argus 数据源异常",
                    message=(
                        f"数据源 {source_id} 已连续采集失败 {failures} 次。"
                        "服务会继续自动重试，详细原因请查看本机日志。"
                    ),
                    priority=4,
                    tags=("warning",),
                    click_url="",
                    topic=default_topic,
                    incident_key=f"system:source:{source_id}",
                    incident_kind="stateful",
                )
                alert_inserted = self._insert_alert(candidate, None, now)
            self.connection.execute(
                """
                UPDATE collector_state
                SET last_attempt_at = ?,
                    consecutive_failures = ?,
                    outage_alerted = ?,
                    outage_started_at = ?,
                    last_error = ?,
                    last_duration_ms = ?,
                    last_error_kind = ?,
                    last_http_status = ?,
                    poll_count = poll_count + 1,
                    failure_count = failure_count + 1,
                    last_observation_count = NULL,
                    last_alert_count = NULL,
                    last_warning = NULL
                WHERE source_id = ?
                """,
                (
                    now,
                    failures,
                    int(state.outage_alerted or should_alert),
                    outage_started,
                    error_text,
                    duration_ms,
                    error_kind,
                    http_status,
                    source_id,
                ),
            )
        return alert_inserted

    def enqueue_test_alert(self, topic: str, now: int) -> bool:
        candidate = AlertCandidate(
            rule_id="system.test",
            dedupe_key=f"system-test:{now}",
            title="Argus 测试通知",
            message="采集、规则、SQLite outbox 与 ntfy 通知链路已就绪。",
            priority=3,
            tags=("test_tube", "white_check_mark"),
            click_url="",
            topic=topic,
        )
        with self.connection:
            return self._insert_alert(candidate, None, now)

    def _record_reminder_audit(
        self,
        reminder_id: str,
        action: str,
        actor: str,
        now: int,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO reminder_audit(reminder_id, action, actor, details_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                reminder_id,
                action[:64],
                actor[:128],
                json.dumps(dict(details or {}), ensure_ascii=False, sort_keys=True),
                now,
            ),
        )

    @staticmethod
    def _decode_reminder(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        try:
            item["tags"] = json.loads(item.pop("tags_json"))
        except (TypeError, ValueError):
            item["tags"] = []
        item["enabled"] = bool(item["enabled"])
        return item

    def get_reminder(self, reminder_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT r.*,
                (SELECT status FROM alerts a WHERE a.reminder_id = r.id ORDER BY a.id DESC LIMIT 1)
                    AS last_delivery_status,
                (SELECT last_error FROM alerts a WHERE a.reminder_id = r.id ORDER BY a.id DESC LIMIT 1)
                    AS last_delivery_error
            FROM reminders r WHERE r.id = ?
            """,
            (reminder_id,),
        ).fetchone()
        return self._decode_reminder(row) if row is not None else None

    def list_reminders(self, limit: int = 500) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        rows = self.connection.execute(
            """
            SELECT r.*,
                (SELECT status FROM alerts a WHERE a.reminder_id = r.id ORDER BY a.id DESC LIMIT 1)
                    AS last_delivery_status,
                (SELECT last_error FROM alerts a WHERE a.reminder_id = r.id ORDER BY a.id DESC LIMIT 1)
                    AS last_delivery_error
            FROM reminders r
            ORDER BY (r.next_run_at IS NULL), r.next_run_at, r.updated_at DESC, r.id
            LIMIT ?
            """,
            (limit,),
        )
        return [self._decode_reminder(row) for row in rows]

    def upsert_reminder(self, reminder: ReminderSpec, actor: str, now: int) -> dict[str, Any]:
        next_run_at: int | None = None
        if reminder.enabled:
            if reminder.schedule_kind == "once":
                next_run_at = reminder.run_at
            else:
                assert reminder.daily_time is not None
                next_run_at = next_daily_occurrence(reminder.daily_time, reminder.timezone, now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.connection.execute(
                "SELECT id FROM reminders WHERE id = ?", (reminder.id,)
            ).fetchone()
            cancelled = 0
            if existing is not None:
                cancelled = self.connection.execute(
                    "DELETE FROM alerts WHERE reminder_id = ? AND status = 'pending'",
                    (reminder.id,),
                ).rowcount
                self.connection.execute(
                    """
                    UPDATE reminders SET title = ?, message = ?, schedule_kind = ?,
                        run_at = ?, daily_time = ?, timezone = ?, next_run_at = ?, enabled = ?,
                        priority = ?, tags_json = ?, updated_at = ?, completed_at = NULL
                    WHERE id = ?
                    """,
                    (
                        reminder.title,
                        reminder.message,
                        reminder.schedule_kind,
                        reminder.run_at,
                        reminder.daily_time,
                        reminder.timezone,
                        next_run_at,
                        int(reminder.enabled),
                        reminder.priority,
                        json.dumps(reminder.tags, ensure_ascii=False),
                        now,
                        reminder.id,
                    ),
                )
                action = "update"
            else:
                self.connection.execute(
                    """
                    INSERT INTO reminders(
                        id, title, message, schedule_kind, run_at, daily_time, timezone,
                        next_run_at, enabled, priority, tags_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        reminder.id,
                        reminder.title,
                        reminder.message,
                        reminder.schedule_kind,
                        reminder.run_at,
                        reminder.daily_time,
                        reminder.timezone,
                        next_run_at,
                        int(reminder.enabled),
                        reminder.priority,
                        json.dumps(reminder.tags, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                action = "create"
            self._record_reminder_audit(
                reminder.id,
                action,
                actor,
                now,
                {"cancelled_pending": int(cancelled)},
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        result = self.get_reminder(reminder.id)
        assert result is not None
        return result

    def set_reminder_enabled(
        self,
        reminder_id: str,
        enabled: bool,
        actor: str,
        now: int,
    ) -> dict[str, Any]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
            ).fetchone()
            if row is None:
                raise ReminderError("reminder not found")
            next_run_at: int | None = None
            if enabled:
                if row["schedule_kind"] == "once":
                    if int(row["run_at"]) <= now:
                        raise ReminderError("one-time reminder time has passed; edit it before enabling")
                    next_run_at = int(row["run_at"])
                else:
                    next_run_at = next_daily_occurrence(
                        str(row["daily_time"]), str(row["timezone"]), now
                    )
            cancelled = 0
            if not enabled:
                cancelled = self.connection.execute(
                    "DELETE FROM alerts WHERE reminder_id = ? AND status = 'pending'",
                    (reminder_id,),
                ).rowcount
            self.connection.execute(
                """
                UPDATE reminders SET enabled = ?, next_run_at = ?, updated_at = ?,
                    completed_at = CASE WHEN ? THEN NULL ELSE completed_at END
                WHERE id = ?
                """,
                (int(enabled), next_run_at, now, int(enabled), reminder_id),
            )
            self._record_reminder_audit(
                reminder_id,
                "enable" if enabled else "disable",
                actor,
                now,
                {"cancelled_pending": int(cancelled)},
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        result = self.get_reminder(reminder_id)
        assert result is not None
        return result

    def delete_reminder(self, reminder_id: str, actor: str, now: int) -> bool:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            exists = self.connection.execute(
                "SELECT 1 FROM reminders WHERE id = ?", (reminder_id,)
            ).fetchone()
            if exists is None:
                self.connection.commit()
                return False
            cancelled = self.connection.execute(
                "DELETE FROM alerts WHERE reminder_id = ? AND status = 'pending'",
                (reminder_id,),
            ).rowcount
            self._record_reminder_audit(
                reminder_id,
                "delete",
                actor,
                now,
                {"cancelled_pending": int(cancelled)},
            )
            self.connection.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
            self.connection.commit()
            return True
        except Exception:
            self.connection.rollback()
            raise

    def enqueue_due_reminders(self, now: int, topic: str, limit: int = 100) -> int:
        limit = max(1, min(int(limit), 1000))
        queued = 0
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            rows = list(self.connection.execute(
                """
                SELECT * FROM reminders
                WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?
                ORDER BY next_run_at, id
                LIMIT ?
                """,
                (now, limit),
            ))
            for row in rows:
                scheduled_for = int(row["next_run_at"])
                candidate = AlertCandidate(
                    rule_id="reminder.manual",
                    dedupe_key=f"reminder:{row['id']}:{scheduled_for}",
                    title=str(row["title"]),
                    message=str(row["message"]),
                    priority=int(row["priority"]),
                    tags=tuple(json.loads(row["tags_json"])),
                    click_url="",
                    topic=topic,
                    confidence=1.0,
                    evidence=(f"scheduled_for:{scheduled_for}",),
                )
                queued += int(self._insert_alert(candidate, None, now, reminder_id=str(row["id"])))
                if row["schedule_kind"] == "once":
                    self.connection.execute(
                        """
                        UPDATE reminders SET enabled = 0, next_run_at = NULL,
                            last_enqueued_at = ?, completed_at = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (now, now, now, row["id"]),
                    )
                else:
                    next_run_at = next_daily_occurrence(
                        str(row["daily_time"]), str(row["timezone"]), now
                    )
                    self.connection.execute(
                        """
                        UPDATE reminders SET next_run_at = ?, last_enqueued_at = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (next_run_at, now, now, row["id"]),
                    )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return queued

    def claim_due_alert(self, now: int, lease_seconds: int) -> OutboxMessage | None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """
                SELECT * FROM alerts
                WHERE (status = 'pending' AND next_attempt_at <= ?)
                   OR (status = 'sending' AND lease_until <= ?)
                ORDER BY priority DESC, created_at ASC, id ASC
                LIMIT 1
                """,
                (now, now),
            ).fetchone()
            if row is None:
                self.connection.commit()
                return None
            attempts = int(row["attempts"]) + 1
            self.connection.execute(
                """
                UPDATE alerts
                SET status = 'sending', attempts = ?, lease_until = ?, last_error = NULL
                WHERE id = ?
                """,
                (attempts, now + lease_seconds, row["id"]),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        try:
            tags = tuple(json.loads(row["tags_json"]))
        except (TypeError, ValueError, KeyError):
            tags = ()
        try:
            evidence = tuple(json.loads(row["evidence_json"]))
        except (TypeError, ValueError, KeyError):
            evidence = ()
        return OutboxMessage(
            id=int(row["id"]),
            topic=str(row["topic"]),
            title=str(row["title"]),
            message=str(row["message"]),
            priority=int(row["priority"]),
            tags=tags,
            click_url=str(row["click_url"]),
            attempts=attempts,
            confidence=float(row["confidence"]),
            evidence=evidence,
            created_at=int(row["created_at"]),
        )

    def mark_delivered(self, alert_id: int, now: int) -> None:
        with self.connection:
            row = self.connection.execute(
                "SELECT reminder_id FROM alerts WHERE id = ? AND status = 'sending'",
                (alert_id,),
            ).fetchone()
            updated = self.connection.execute(
                """
                UPDATE alerts
                SET status = 'delivered', delivered_at = ?, lease_until = NULL,
                    last_error = NULL, failure_kind = NULL, dead_at = NULL
                WHERE id = ? AND status = 'sending'
                """,
                (now, alert_id),
            )
            if updated.rowcount == 1 and row is not None and row["reminder_id"]:
                self.connection.execute(
                    "UPDATE reminders SET last_delivered_at = ? WHERE id = ?",
                    (now, row["reminder_id"]),
                )

    def mark_retry(self, alert_id: int, next_attempt_at: int, error: BaseException | str) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE alerts
                SET status = 'pending', next_attempt_at = ?, lease_until = NULL,
                    last_error = ?, failure_kind = 'transient_unknown', dead_at = NULL
                WHERE id = ? AND status = 'sending'
                """,
                (next_attempt_at, sanitize_error(error), alert_id),
            )


    def mark_delivery_failure(
        self,
        alert_id: int,
        now: int,
        next_attempt_at: int,
        error: BaseException | str,
        *,
        retryable: bool,
        failure_kind: str,
        max_attempts: int,
        max_age_seconds: int,
    ) -> str:
        """Return the durable state selected for a failed delivery."""
        with self.unit_of_work():
            row = self.connection.execute(
                "SELECT attempts, COALESCE(retry_started_at, created_at) AS retry_started_at FROM alerts WHERE id = ? AND status = 'sending'",
                (alert_id,),
            ).fetchone()
            if row is None:
                return "missing"
            expired = now - int(row["retry_started_at"]) >= max_age_seconds
            exhausted = int(row["attempts"]) >= max_attempts
            status = "pending" if retryable and not expired and not exhausted else "dead"
            self.connection.execute(
                """
                UPDATE alerts
                SET status = ?, next_attempt_at = ?, lease_until = NULL,
                    last_error = ?, failure_kind = ?, dead_at = ?
                WHERE id = ? AND status = 'sending'
                """,
                (
                    status,
                    next_attempt_at,
                    sanitize_error(error),
                    failure_kind[:64],
                    now if status == "dead" else None,
                    alert_id,
                ),
            )
        return status

    @staticmethod
    def _decode_alert(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        for field in ("tags_json", "evidence_json"):
            try:
                item[field[:-5]] = json.loads(item.pop(field))
            except (TypeError, ValueError):
                item[field[:-5]] = []
        return item

    def get_alert(self, alert_id: int) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
        return self._decode_alert(row) if row is not None else None

    def list_alerts(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
        before_id: int | None = None,
    ) -> list[dict[str, Any]]:
        valid = {"pending", "sending", "delivered", "dead", "cancelled"}
        if status is not None and status not in valid:
            raise ValueError("invalid alert status")
        limit = max(1, min(int(limit), 500))
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if before_id is not None:
            clauses.append("id < ?")
            params.append(int(before_id))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self.connection.execute(
            f"SELECT * FROM alerts {where} ORDER BY id DESC LIMIT ?", params
        )
        return [self._decode_alert(row) for row in rows]

    def retry_alert(self, alert_id: int, now: int) -> bool:
        with self.connection:
            updated = self.connection.execute(
                """
                UPDATE alerts
                SET status = 'pending', attempts = 0, next_attempt_at = ?, retry_started_at = ?,
                    lease_until = NULL, last_error = NULL, failure_kind = NULL, dead_at = NULL
                WHERE id = ? AND status IN ('dead', 'cancelled')
                """,
                (now, now, alert_id),
            )
        return updated.rowcount == 1

    def cancel_alert(self, alert_id: int, now: int) -> bool:
        with self.connection:
            updated = self.connection.execute(
                """
                UPDATE alerts
                SET status = 'cancelled', lease_until = NULL, dead_at = ?,
                    failure_kind = 'cancelled', last_error = 'cancelled by administrator'
                WHERE id = ? AND status = 'pending'
                """,
                (now, alert_id),
            )
        return updated.rowcount == 1

    def discard_alert(self, alert_id: int) -> bool:
        with self.connection:
            deleted = self.connection.execute(
                "DELETE FROM alerts WHERE id = ? AND status IN ('dead', 'cancelled')",
                (alert_id,),
            )
        return deleted.rowcount == 1
    def list_incidents(self, limit: int = 100, status: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        query = """
            SELECT i.*,
                (SELECT a.title FROM alerts AS a WHERE a.incident_id = i.id
                 ORDER BY a.created_at DESC, a.id DESC LIMIT 1) AS latest_title,
                (SELECT a.message FROM alerts AS a WHERE a.incident_id = i.id
                 ORDER BY a.created_at DESC, a.id DESC LIMIT 1) AS latest_message,
                (SELECT a.click_url FROM alerts AS a WHERE a.incident_id = i.id
                 ORDER BY a.created_at DESC, a.id DESC LIMIT 1) AS latest_click_url
            FROM incidents AS i
        """
        params: list[Any] = []
        if status:
            query += " WHERE i.status = ?"
            params.append(status)
        query += " ORDER BY i.last_seen_at DESC, i.id DESC LIMIT ?"
        params.append(limit)
        rows = []
        for row in self.connection.execute(query, params):
            item = dict(row)
            for field in ("evidence_json", "source_ids_json"):
                try:
                    item[field[:-5]] = json.loads(item.pop(field))
                except (TypeError, ValueError):
                    item[field[:-5]] = []
            rows.append(item)
        return rows

    def record_config_revision(
        self,
        revision: int,
        payload: Mapping[str, Any],
        actor: str = "admin",
        reason: str = "configuration update",
        active: bool = True,
        *,
        only_if_empty: bool = False,
    ) -> int:
        now = int(time.time())
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self.unit_of_work():
            if only_if_empty:
                existing = self.connection.execute(
                    "SELECT id FROM config_revisions WHERE active = 1 LIMIT 1"
                ).fetchone()
                if existing is not None:
                    return int(existing["id"])
            if active:
                self.connection.execute("UPDATE config_revisions SET active = 0")
            self.connection.execute(
                """
                INSERT INTO config_revisions(revision, payload_json, actor, reason, created_at, active)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(revision) DO UPDATE SET payload_json=excluded.payload_json,
                    actor=excluded.actor, reason=excluded.reason, active=excluded.active
                """,
                (int(revision), encoded, actor[:128], reason[:512], now, int(active)),
            )
            row = self.connection.execute(
                "SELECT id FROM config_revisions WHERE revision = ?", (int(revision),)
            ).fetchone()
            assert row is not None
            revision_id = int(row["id"])
            self.connection.execute(
                "INSERT INTO config_audit(revision_id, action, actor, details_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (revision_id, "activate" if active else "save", actor[:128], "{}", now),
            )
        return revision_id

    def activate_config_revision(self, revision: int, actor: str = "admin", reason: str = "rollback") -> bool:
        now = int(time.time())
        with self.connection:
            row = self.connection.execute(
                "SELECT id FROM config_revisions WHERE revision = ?", (int(revision),)
            ).fetchone()
            if row is None:
                return False
            self.connection.execute("UPDATE config_revisions SET active = 0")
            self.connection.execute(
                "UPDATE config_revisions SET active = 1 WHERE revision = ?", (int(revision),)
            )
            self.connection.execute(
                "INSERT INTO config_audit(revision_id, action, actor, details_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (int(row["id"]), "rollback", actor[:128], json.dumps({"reason": reason[:512]}), now),
            )
        return True

    def list_config_revisions(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        return [dict(row) for row in self.connection.execute(
            "SELECT id, revision, actor, reason, created_at, active FROM config_revisions ORDER BY revision DESC LIMIT ?",
            (limit,),
        )]

    def get_config_revision(self, revision: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM config_revisions WHERE revision = ?", (int(revision),)
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        try:
            result["payload"] = json.loads(result.pop("payload_json"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("stored configuration revision is invalid") from exc
        if not isinstance(result["payload"], dict):
            raise RuntimeError("stored configuration revision must be an object")
        return result

    def get_prompt(self, prompt_id: str, version: int | None = None) -> dict[str, Any] | None:
        if not prompt_id or len(prompt_id) > 64:
            raise ValueError("prompt ID is invalid")
        if version is not None and version < 1:
            raise ValueError("prompt version is invalid")
        if version is None:
            row = self.connection.execute(
                "SELECT * FROM prompts WHERE prompt_id = ? AND active = 1 "
                "ORDER BY version DESC LIMIT 1", (prompt_id,)
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT * FROM prompts WHERE prompt_id = ? AND version = ?",
                (prompt_id, version),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_prompts(self, prompt_id: str | None = None) -> list[dict[str, Any]]:
        if prompt_id is None:
            rows = self.connection.execute(
                "SELECT * FROM prompts ORDER BY prompt_id, version DESC"
            )
        else:
            rows = self.connection.execute(
                "SELECT * FROM prompts WHERE prompt_id = ? ORDER BY version DESC",
                (prompt_id,),
            )
        return [dict(row) for row in rows]

    def save_prompt(
        self,
        prompt_id: str,
        version: int,
        system_text: str,
        actor: str,
        now: int,
        *,
        active: bool = True,
    ) -> None:
        template = PromptTemplate(prompt_id, version, system_text)
        if len(actor.strip()) > 128:
            raise ValueError("prompt actor is too long")
        with self.connection:
            if active:
                self.connection.execute(
                    "UPDATE prompts SET active = 0 WHERE prompt_id = ?", (prompt_id,)
                )
            self.connection.execute(
                """
                INSERT INTO prompts(prompt_id, version, system_text, active, actor, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(prompt_id, version) DO UPDATE SET
                    system_text = excluded.system_text,
                    active = excluded.active,
                    actor = excluded.actor,
                    created_at = excluded.created_at
                """,
                (template.prompt_id, template.version, template.system_text,
                 int(active), actor.strip()[:128], int(now)),
            )

    def list_observations(
        self,
        since: int,
        until: int,
        *,
        handling: str | None = None,
        region: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Read normalized observations through the repository boundary."""
        if since < 0 or until < since:
            raise ValueError("observation time range is invalid")
        if not 1 <= limit <= 5000:
            raise ValueError("observation limit is out of range")
        clauses = ["published_at >= ?", "published_at < ?"]
        params: list[Any] = [since, until]
        if handling is not None:
            if handling not in {"immediate", "digest", "archive"}:
                raise ValueError("observation handling is invalid")
            clauses.append("handling = ?")
            params.append(handling)
        if region is not None:
            if not region or len(region) > 16:
                raise ValueError("observation region is invalid")
            clauses.append("region = ?")
            params.append(region.upper())
        params.append(limit)
        rows = self.connection.execute(
            "SELECT id, source_id, publisher, published_at, fetched_at, title, summary, "
            "url, attributes_json, importance, urgency, relevance, confidence, region, "
            "topic, source_tier, information_type, handling, processing_state "
            f"FROM observations WHERE {' AND '.join(clauses)} "
            "ORDER BY published_at DESC, id DESC LIMIT ?",
            params,
        )
        observations: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["attributes"] = json.loads(item.pop("attributes_json"))
            except (TypeError, ValueError):
                item["attributes"] = {}
            observations.append(item)
        return observations

    def claim_analysis_observations(
        self, now: int, *, limit: int, lease_seconds: int
    ) -> list[AnalysisWorkItem]:
        """Lease unprocessed observations without holding a transaction during inference."""
        if now < 0:
            raise ValueError("analysis claim time is invalid")
        if not 1 <= limit <= 500:
            raise ValueError("analysis claim limit is out of range")
        if not 10 <= lease_seconds <= 3600:
            raise ValueError("analysis lease is out of range")
        token = uuid.uuid4().hex
        with self.unit_of_work():
            rows = list(self.connection.execute(
                """
                SELECT id FROM observations
                WHERE processing_state = 'new'
                   OR (
                       processing_state = 'processing'
                       AND COALESCE(analysis_lease_until, 0) <= ?
                   )
                ORDER BY fetched_at, id
                LIMIT ?
                """,
                (now, limit),
            ))
            ids = [int(row["id"]) for row in rows]
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            self.connection.execute(
                f"""
                UPDATE observations
                SET processing_state = 'processing', analysis_lease_token = ?,
                    analysis_lease_until = ?, analysis_attempts = analysis_attempts + 1,
                    analysis_error = NULL
                WHERE id IN ({placeholders})
                """,
                (token, now + lease_seconds, *ids),
            )
            leased = list(self.connection.execute(
                f"""
                SELECT id, source_id, publisher, dedupe_scope, external_id,
                       published_at, fetched_at, title, summary, url, attributes_json,
                       importance, urgency, relevance, confidence, region, topic,
                       source_tier, information_type, handling, processing_state,
                       analysis_lease_token, analysis_attempts
                FROM observations
                WHERE id IN ({placeholders}) AND analysis_lease_token = ?
                ORDER BY fetched_at, id
                """,
                (*ids, token),
            ))

        items: list[AnalysisWorkItem] = []
        for row in leased:
            try:
                attributes = json.loads(row["attributes_json"])
            except (TypeError, ValueError):
                attributes = {}
            if not isinstance(attributes, dict):
                attributes = {}
            observation = Observation(
                source_id=str(row["source_id"]),
                publisher=str(row["publisher"]),
                dedupe_scope=str(row["dedupe_scope"]),
                external_id=str(row["external_id"]),
                published_at=datetime.fromtimestamp(int(row["published_at"]), UTC),
                title=str(row["title"]),
                summary=str(row["summary"]),
                url=str(row["url"]),
                attributes=attributes,
                importance=int(row["importance"]),
                urgency=int(row["urgency"]),
                relevance=int(row["relevance"]),
                confidence=float(row["confidence"]),
                region=str(row["region"]),
                topic=str(row["topic"]),
                source_tier=str(row["source_tier"]),
                information_type=str(row["information_type"]),
                handling=str(row["handling"]),
                processing_state=str(row["processing_state"]),
            )
            items.append(AnalysisWorkItem(
                observation_id=int(row["id"]),
                observation=observation,
                lease_token=str(row["analysis_lease_token"]),
                fetched_at=int(row["fetched_at"]),
                attempts=int(row["analysis_attempts"]),
            ))
        return items

    def reserve_analysis_api_call(
        self, budget_day: str, *, limit: int, now: int
    ) -> bool:
        """Atomically reserve one remote analysis call against a UTC-day budget."""
        try:
            parsed_day = datetime.strptime(budget_day, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError("analysis budget day is invalid") from exc
        if parsed_day.isoformat() != budget_day:
            raise ValueError("analysis budget day is invalid")
        if not 0 <= limit <= 1000:
            raise ValueError("analysis API budget is out of range")
        if limit == 0:
            return False
        with self.unit_of_work():
            row = self.connection.execute(
                "SELECT calls FROM analysis_api_usage WHERE budget_day = ?",
                (budget_day,),
            ).fetchone()
            used = int(row["calls"]) if row is not None else 0
            if used >= limit:
                return False
            self.connection.execute(
                """
                INSERT INTO analysis_api_usage(budget_day, calls, updated_at)
                VALUES (?, 1, ?)
                ON CONFLICT(budget_day) DO UPDATE SET
                    calls = analysis_api_usage.calls + 1,
                    updated_at = excluded.updated_at
                """,
                (budget_day, now),
            )
        return True

    def save_analysis_result(
        self,
        observation_id: int,
        lease_token: str,
        analysis: InformationAnalysis,
        attempts: list[AnalysisAttempt] | tuple[AnalysisAttempt, ...],
        *,
        processing_state: str,
        now: int,
        error: str | None = None,
    ) -> None:
        """Persist the final classification and its stage-by-stage audit trail."""
        if processing_state not in {"analyzed", "degraded"}:
            raise ValueError("analysis processing state is invalid")
        if not lease_token or len(lease_token) > 128:
            raise ValueError("analysis lease token is invalid")
        encoded_attempts: list[tuple[AnalysisAttempt, str | None, str | None]] = []
        for attempt in attempts:
            advisory_json = (
                json.dumps(attempt.advisory, ensure_ascii=False, sort_keys=True)
                if attempt.advisory is not None
                else None
            )
            encoded_attempts.append(
                (attempt, advisory_json, sanitize_error(attempt.error) if attempt.error else None)
            )
        with self.unit_of_work():
            updated = self.connection.execute(
                """
                UPDATE observations
                SET importance = ?, urgency = ?, relevance = ?, confidence = ?,
                    region = ?, topic = ?, source_tier = ?, information_type = ?,
                    handling = ?, processing_state = ?, analysis_lease_token = NULL,
                    analysis_lease_until = NULL, analysis_error = ?, analyzed_at = ?
                WHERE id = ? AND processing_state = 'processing'
                  AND analysis_lease_token = ?
                """,
                (
                    analysis.importance,
                    analysis.urgency,
                    analysis.relevance,
                    analysis.confidence,
                    analysis.region,
                    analysis.topic,
                    analysis.source_tier,
                    analysis.information_type,
                    analysis.handling,
                    processing_state,
                    sanitize_error(error) if error else None,
                    now,
                    observation_id,
                    lease_token,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError("analysis lease is stale or observation does not exist")
            for attempt, advisory_json, attempt_error in encoded_attempts:
                self.connection.execute(
                    """
                    INSERT INTO analysis_runs(
                        observation_id, analyzer, outcome, shadow, advisory_json,
                        error, duration_ms, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        observation_id,
                        attempt.analyzer[:64],
                        attempt.outcome,
                        int(attempt.shadow),
                        advisory_json,
                        attempt_error,
                        max(0, int(attempt.duration_ms)),
                        now,
                    ),
                )

    def list_source_coverage(
        self,
        since: int,
        until: int,
        *,
        source_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return source health and period coverage without exposing SQL upstream."""
        if since < 0 or until < since:
            raise ValueError("source coverage time range is invalid")
        requested: list[str] | None = None
        if source_ids is not None:
            requested = list(dict.fromkeys(str(item).strip() for item in source_ids))
            if any(not item or len(item) > 128 for item in requested):
                raise ValueError("source coverage contains an invalid source ID")
            if len(requested) > 1000:
                raise ValueError("source coverage source limit is out of range")
            if not requested:
                return []
            placeholders = ",".join("?" for _ in requested)
            state_rows = self.connection.execute(
                "SELECT c.source_id, c.last_attempt_at, c.last_success_at, "
                "c.consecutive_failures, r.configured_enabled, r.runtime_status "
                "FROM collector_state AS c LEFT JOIN source_runtime AS r "
                "ON r.source_id = c.source_id "
                f"WHERE c.source_id IN ({placeholders})",
                requested,
            )
        else:
            state_rows = self.connection.execute(
                "SELECT c.source_id, c.last_attempt_at, c.last_success_at, "
                "c.consecutive_failures, r.configured_enabled, r.runtime_status "
                "FROM collector_state AS c LEFT JOIN source_runtime AS r "
                "ON r.source_id = c.source_id"
            )
        states = {str(row["source_id"]): dict(row) for row in state_rows}
        expected = requested if requested is not None else sorted(states)
        counts: dict[str, int] = {}
        if expected:
            placeholders = ",".join("?" for _ in expected)
            rows = self.connection.execute(
                "SELECT source_id, COUNT(*) AS observation_count FROM observations "
                "WHERE published_at >= ? AND published_at < ? "
                f"AND source_id IN ({placeholders}) GROUP BY source_id",
                [since, until, *expected],
            )
            counts = {str(row["source_id"]): int(row["observation_count"]) for row in rows}

        result: list[dict[str, Any]] = []
        for source_id in expected:
            state = states.get(source_id, {})
            count = counts.get(source_id, 0)
            enabled = state.get("configured_enabled")
            runtime_status = state.get("runtime_status")
            failures = int(state.get("consecutive_failures") or 0)
            last_success = state.get("last_success_at")
            if enabled == 0:
                status = "disabled"
            elif runtime_status in {"invalid", "degraded"} or failures > 0:
                status = "degraded"
            elif runtime_status == "stale":
                status = "stale"
            elif count:
                status = "covered"
            elif last_success is not None and int(last_success) >= since:
                status = "quiet"
            elif last_success is not None:
                status = "stale"
            else:
                status = "unknown"
            result.append(
                {
                    "source_id": source_id,
                    "status": status,
                    "observation_count": count,
                    "last_attempt_at": state.get("last_attempt_at"),
                    "last_success_at": last_success,
                    "consecutive_failures": failures,
                }
            )
        return result

    @staticmethod
    def _validate_source_quality_source_id(source_id: str) -> str:
        normalized = source_id.strip()
        if not normalized or len(normalized) > 128:
            raise ValueError("source quality source ID is invalid")
        return normalized

    def _known_source_id(self, source_id: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM source_runtime WHERE source_id = ? "
            "UNION SELECT 1 FROM collector_state WHERE source_id = ? "
            "UNION SELECT 1 FROM observations WHERE source_id = ? LIMIT 1",
            (source_id, source_id, source_id),
        ).fetchone() is not None

    def list_source_quality(
        self,
        *,
        source_ids: Sequence[str] | None = None,
        now: int | None = None,
    ) -> list[dict[str, Any]]:
        calculated_at = int(time.time()) if now is None else int(now)
        if calculated_at < 0:
            raise ValueError("source quality calculation time is invalid")
        if source_ids is None:
            rows = self.connection.execute(
                "SELECT source_id FROM source_runtime UNION SELECT source_id FROM collector_state "
                "UNION SELECT source_id FROM source_quality_feedback "
                "UNION SELECT source_id FROM source_quality_overrides ORDER BY source_id"
            )
            identifiers = [str(row["source_id"]) for row in rows]
        else:
            identifiers = list(dict.fromkeys(
                self._validate_source_quality_source_id(str(item)) for item in source_ids
            ))
        if len(identifiers) > 1000:
            raise ValueError("source quality source limit is out of range")

        policy = SourceQualityPolicy()
        profiles: list[dict[str, Any]] = []
        for source_id in identifiers:
            feedback = [dict(row) for row in self.connection.execute(
                "SELECT signal, created_at FROM source_quality_feedback "
                "WHERE source_id = ? ORDER BY created_at, id",
                (source_id,),
            )]
            override = self.connection.execute(
                "SELECT weight, reason, actor, created_at, updated_at "
                "FROM source_quality_overrides WHERE source_id = ?",
                (source_id,),
            ).fetchone()
            state = self.connection.execute(
                "SELECT automatic_weight, calculated_at FROM source_quality_state WHERE source_id = ?",
                (source_id,),
            ).fetchone()
            previous_weight = float(state["automatic_weight"]) if state is not None else 1.0
            previous_at = int(state["calculated_at"]) if state is not None else None
            profile = calculate_quality(
                source_id,
                feedback,
                now=calculated_at,
                policy=policy,
                current_weight=previous_weight,
                manual_override=float(override["weight"]) if override is not None else None,
                updated_at=previous_at,
            )
            automatic = calculate_quality(
                source_id,
                feedback,
                now=calculated_at,
                policy=policy,
                current_weight=previous_weight,
                updated_at=previous_at,
            )
            with self.connection:
                self.connection.execute(
                    "INSERT INTO source_quality_state(source_id, automatic_weight, calculated_at) "
                    "VALUES (?, ?, ?) ON CONFLICT(source_id) DO UPDATE SET "
                    "automatic_weight = excluded.automatic_weight, calculated_at = excluded.calculated_at",
                    (source_id, automatic.weight, calculated_at),
                )
            item = {
                "source_id": profile.source_id,
                "weight": profile.weight,
                "automatic_weight": automatic.weight,
                "score": profile.score,
                "effective_samples": profile.effective_samples,
                "positive_count": profile.positive_count,
                "negative_count": profile.negative_count,
                "neutral_count": profile.neutral_count,
                "first_feedback_at": profile.first_feedback_at,
                "last_feedback_at": profile.last_feedback_at,
                "evidence_span_days": profile.evidence_span_days,
                "eligible": automatic.eligible,
                "manual_override": profile.manual_override,
                "updated_at": calculated_at,
                "override_reason": str(override["reason"]) if override is not None else None,
                "override_actor": str(override["actor"]) if override is not None else None,
                "override_updated_at": int(override["updated_at"]) if override is not None else None,
                "policy": {
                    "half_life_days": policy.half_life_days,
                    "min_effective_samples": policy.min_effective_samples,
                    "min_span_days": policy.min_span_days,
                    "prior_positive": policy.prior_positive,
                    "prior_negative": policy.prior_negative,
                    "minimum_weight": policy.minimum_weight,
                    "maximum_weight": policy.maximum_weight,
                    "maximum_change_per_30_days": policy.maximum_change_per_30_days,
                    "scope": "digest_ranking_only",
                },
            }
            profiles.append(item)
        return profiles

    def record_source_quality_feedback(
        self,
        source_id: str,
        signal: int,
        reason: str,
        actor: str,
        now: int,
        observation_id: int | None = None,
    ) -> int:
        source_id = self._validate_source_quality_source_id(source_id)
        if signal not in {-1, 0, 1} or now < 0:
            raise ValueError("source quality feedback is invalid")
        reason = reason.strip()
        actor = actor.strip()
        if not reason or len(reason) > 1000 or not actor or len(actor) > 128:
            raise ValueError("source quality feedback metadata is invalid")
        if not self._known_source_id(source_id):
            raise KeyError(f"unknown source: {source_id}")
        if observation_id is not None and self.connection.execute(
            "SELECT 1 FROM observations WHERE id = ? AND source_id = ?",
            (observation_id, source_id),
        ).fetchone() is None:
            raise ValueError("feedback observation does not belong to the source")
        with self.unit_of_work():
            cursor = self.connection.execute(
                "INSERT INTO source_quality_feedback(source_id, observation_id, signal, reason, actor, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (source_id, observation_id, signal, reason, actor, now),
            )
            self.connection.execute(
                "INSERT INTO source_quality_audit(source_id, action, actor, details_json, created_at) "
                "VALUES (?, 'feedback', ?, ?, ?)",
                (source_id, actor, json.dumps({"signal": signal, "reason": reason, "observation_id": observation_id}, ensure_ascii=False), now),
            )
        return int(cursor.lastrowid)

    def set_source_quality_override(
        self, source_id: str, weight: float, reason: str, actor: str, now: int
    ) -> None:
        source_id = self._validate_source_quality_source_id(source_id)
        if isinstance(weight, bool) or not 0.70 <= float(weight) <= 1.15 or now < 0:
            raise ValueError("source quality override weight is invalid")
        reason = reason.strip()
        actor = actor.strip()
        if not reason or len(reason) > 1000 or not actor or len(actor) > 128:
            raise ValueError("source quality override metadata is invalid")
        if not self._known_source_id(source_id):
            raise KeyError(f"unknown source: {source_id}")
        with self.unit_of_work():
            self.connection.execute(
                "INSERT INTO source_quality_overrides(source_id, weight, reason, actor, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(source_id) DO UPDATE SET "
                "weight = excluded.weight, reason = excluded.reason, actor = excluded.actor, "
                "updated_at = excluded.updated_at",
                (source_id, float(weight), reason, actor, now, now),
            )
            self.connection.execute(
                "INSERT INTO source_quality_audit(source_id, action, actor, details_json, created_at) "
                "VALUES (?, 'override_set', ?, ?, ?)",
                (source_id, actor, json.dumps({"weight": float(weight), "reason": reason}, ensure_ascii=False), now),
            )

    def clear_source_quality_override(self, source_id: str, actor: str, now: int) -> bool:
        source_id = self._validate_source_quality_source_id(source_id)
        actor = actor.strip()
        if not actor or len(actor) > 128 or now < 0:
            raise ValueError("source quality override request is invalid")
        with self.unit_of_work():
            row = self.connection.execute(
                "SELECT weight, reason FROM source_quality_overrides WHERE source_id = ?",
                (source_id,),
            ).fetchone()
            if row is None:
                return False
            self.connection.execute(
                "DELETE FROM source_quality_overrides WHERE source_id = ?", (source_id,)
            )
            self.connection.execute(
                "INSERT INTO source_quality_audit(source_id, action, actor, details_json, created_at) "
                "VALUES (?, 'override_cleared', ?, ?, ?)",
                (source_id, actor, json.dumps({"previous_weight": float(row["weight"]), "previous_reason": str(row["reason"])}, ensure_ascii=False), now),
            )
        return True

    def list_source_quality_audit(self, source_id: str, limit: int = 100) -> list[dict[str, Any]]:
        source_id = self._validate_source_quality_source_id(source_id)
        if not 1 <= limit <= 500:
            raise ValueError("source quality audit limit is out of range")
        result = []
        for row in self.connection.execute(
            "SELECT id, source_id, action, actor, details_json, created_at "
            "FROM source_quality_audit WHERE source_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (source_id, limit),
        ):
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            result.append(item)
        return result

    def save_digest(self, digest: DigestDocument) -> DigestDocument:
        """Allocate and store the next immutable version of a digest."""
        if digest.version != 0 or digest.status != "draft" or digest.published_at is not None:
            raise ValueError("new digest must be an unversioned draft")
        if len({item.cluster_key for item in digest.items}) != len(digest.items):
            raise ValueError("digest contains duplicate cluster keys")
        if len({item.source_id for item in digest.coverage}) != len(digest.coverage):
            raise ValueError("digest contains duplicate source coverage")
        with self.unit_of_work():
            row = self.connection.execute(
                "SELECT version, period_start, period_end, timezone FROM digests "
                "WHERE digest_key = ? ORDER BY version DESC LIMIT 1",
                (digest.digest_key,),
            ).fetchone()
            if row is not None and (
                int(row["period_start"]) != digest.period_start
                or int(row["period_end"]) != digest.period_end
                or str(row["timezone"]) != digest.timezone
            ):
                raise ValueError("digest revisions must describe the same period")
            version = int(row["version"]) + 1 if row is not None else 1
            cursor = self.connection.execute(
                """
                INSERT INTO digests(
                    digest_key, version, period_start, period_end, timezone, title,
                    summary, generation_kind, status, created_at, published_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, NULL)
                """,
                (
                    digest.digest_key,
                    version,
                    digest.period_start,
                    digest.period_end,
                    digest.timezone,
                    digest.title,
                    digest.summary,
                    digest.generation_kind,
                    digest.created_at,
                ),
            )
            digest_id = int(cursor.lastrowid)
            for position, item in enumerate(digest.items):
                self.connection.execute(
                    """
                    INSERT INTO digest_items(
                        digest_id, position, cluster_key, title, summary, score,
                        importance, urgency, relevance, confidence, published_at,
                        regions_json, topics_json, source_ids_json,
                        observation_ids_json, links_json, handling
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        digest_id,
                        position,
                        item.cluster_key,
                        item.title,
                        item.summary,
                        item.score,
                        item.importance,
                        item.urgency,
                        item.relevance,
                        item.confidence,
                        item.published_at,
                        json.dumps(item.regions, ensure_ascii=False),
                        json.dumps(item.topics, ensure_ascii=False),
                        json.dumps(item.source_ids, ensure_ascii=False),
                        json.dumps(item.observation_ids),
                        json.dumps(item.links, ensure_ascii=False),
                        item.handling,
                    ),
                )
            for item in digest.coverage:
                self.connection.execute(
                    """
                    INSERT INTO digest_source_coverage(
                        digest_id, source_id, status, observation_count,
                        last_attempt_at, last_success_at, consecutive_failures
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        digest_id,
                        item.source_id,
                        item.status,
                        item.observation_count,
                        item.last_attempt_at,
                        item.last_success_at,
                        item.consecutive_failures,
                    ),
                )
        saved = self.get_digest(digest.digest_key, version)
        assert saved is not None
        return saved

    def publish_digest(self, digest_key: str, version: int, now: int) -> DigestDocument:
        """Atomically publish one version and supersede the previous publication."""
        if not digest_key or version < 1 or now < 0:
            raise ValueError("digest publication request is invalid")
        with self.unit_of_work():
            row = self.connection.execute(
                "SELECT id, status FROM digests WHERE digest_key = ? AND version = ?",
                (digest_key, version),
            ).fetchone()
            if row is None:
                raise KeyError(f"digest does not exist: {digest_key}@{version}")
            if row["status"] != "published":
                self.connection.execute(
                    "UPDATE digests SET status = 'superseded' "
                    "WHERE digest_key = ? AND status = 'published' AND version != ?",
                    (digest_key, version),
                )
                self.connection.execute(
                    "UPDATE digests SET status = 'published', published_at = ? WHERE id = ?",
                    (now, int(row["id"])),
                )
        published = self.get_digest(digest_key, version)
        assert published is not None
        return published

    def enqueue_digest_notification(
        self,
        digest: DigestDocument,
        *,
        topic: str,
        click_url: str,
        now: int,
    ) -> bool:
        if digest.status != "published":
            raise ValueError("only a published digest can be notified")
        candidate = AlertCandidate(
            rule_id="digest.daily",
            dedupe_key=f"digest:{digest.digest_key}:{digest.version}",
            title=digest.title,
            message=digest.summary,
            priority=3,
            tags=("newspaper", "eye"),
            click_url=click_url,
            topic=topic,
            confidence=1.0,
            evidence=(f"digest {digest.digest_key}@{digest.version}",),
        )
        with self.connection:
            return self._insert_alert(candidate, None, now)

    def get_digest(
        self,
        digest_key: str,
        version: int | None = None,
        *,
        published_only: bool = False,
    ) -> DigestDocument | None:
        clauses = ["digest_key = ?"]
        params: list[Any] = [digest_key]
        if version is not None:
            if version < 1:
                raise ValueError("digest version is invalid")
            clauses.append("version = ?")
            params.append(version)
        if published_only:
            clauses.append("status = 'published'")
        row = self.connection.execute(
            f"SELECT * FROM digests WHERE {' AND '.join(clauses)} "
            "ORDER BY version DESC LIMIT 1",
            params,
        ).fetchone()
        return self._digest_from_row(row) if row is not None else None

    def list_digests(
        self, *, status: str | None = None, limit: int = 30
    ) -> list[DigestDocument]:
        if status is not None and status not in {"draft", "published", "superseded"}:
            raise ValueError("digest status is invalid")
        if not 1 <= limit <= 500:
            raise ValueError("digest limit is out of range")
        if status is None:
            rows = self.connection.execute(
                "SELECT * FROM digests ORDER BY period_start DESC, digest_key, version DESC LIMIT ?",
                (limit,),
            )
        else:
            rows = self.connection.execute(
                "SELECT * FROM digests WHERE status = ? "
                "ORDER BY period_start DESC, digest_key, version DESC LIMIT ?",
                (status, limit),
            )
        return [self._digest_from_row(row) for row in rows]

    def _digest_from_row(self, row: sqlite3.Row) -> DigestDocument:
        digest_id = int(row["id"])
        items = []
        for item in self.connection.execute(
            "SELECT * FROM digest_items WHERE digest_id = ? ORDER BY position", (digest_id,)
        ):
            try:
                items.append(
                    DigestCluster(
                        cluster_key=str(item["cluster_key"]),
                        title=str(item["title"]),
                        summary=str(item["summary"]),
                        score=float(item["score"]),
                        importance=int(item["importance"]),
                        urgency=int(item["urgency"]),
                        relevance=int(item["relevance"]),
                        confidence=float(item["confidence"]),
                        published_at=int(item["published_at"]),
                        regions=tuple(json.loads(item["regions_json"])),
                        topics=tuple(json.loads(item["topics_json"])),
                        source_ids=tuple(json.loads(item["source_ids_json"])),
                        observation_ids=tuple(int(value) for value in json.loads(item["observation_ids_json"])),
                        links=tuple(json.loads(item["links_json"])),
                        handling=str(item["handling"]),
                    )
                )
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"digest item {item['id']} is corrupt") from exc
        coverage = tuple(
            SourceCoverage(
                source_id=str(item["source_id"]),
                status=str(item["status"]),
                observation_count=int(item["observation_count"]),
                last_attempt_at=item["last_attempt_at"],
                last_success_at=item["last_success_at"],
                consecutive_failures=int(item["consecutive_failures"]),
            )
            for item in self.connection.execute(
                "SELECT * FROM digest_source_coverage WHERE digest_id = ? ORDER BY source_id",
                (digest_id,),
            )
        )
        return DigestDocument(
            digest_key=str(row["digest_key"]),
            version=int(row["version"]),
            period_start=int(row["period_start"]),
            period_end=int(row["period_end"]),
            timezone=str(row["timezone"]),
            title=str(row["title"]),
            summary=str(row["summary"]),
            generation_kind=str(row["generation_kind"]),
            items=tuple(items),
            coverage=coverage,
            created_at=int(row["created_at"]),
            status=str(row["status"]),
            published_at=row["published_at"],
        )

    def get_active_config_revision(self) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM config_revisions WHERE active = 1 ORDER BY revision DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        try:
            result["payload"] = json.loads(result.pop("payload_json"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("active configuration revision is invalid") from exc
        if not isinstance(result["payload"], dict):
            raise RuntimeError("active configuration revision must be an object")
        return result

    def save_managed_config(
        self,
        payload: Mapping[str, Any],
        actor: str,
        reason: str,
        *,
        expected_revision: int | None = None,
    ) -> int:
        """Atomically create and activate a new immutable desired revision."""
        now = int(time.time())
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            active = self.connection.execute(
                "SELECT revision FROM config_revisions WHERE active = 1 ORDER BY revision DESC LIMIT 1"
            ).fetchone()
            current_revision = int(active["revision"]) if active is not None else 0
            if expected_revision is not None and expected_revision != current_revision:
                raise RevisionConflictError(
                    f"configuration changed: expected revision {expected_revision}, current revision {current_revision}"
                )
            maximum = self.connection.execute(
                "SELECT COALESCE(MAX(revision), 0) FROM config_revisions"
            ).fetchone()
            revision = int(maximum[0]) + 1
            self.connection.execute("UPDATE config_revisions SET active = 0")
            cursor = self.connection.execute(
                """
                INSERT INTO config_revisions(
                    revision, payload_json, actor, reason, created_at, active
                ) VALUES (?, ?, ?, ?, ?, 1)
                """,
                (revision, encoded, actor[:128], reason[:512], now),
            )
            self.connection.execute(
                """
                INSERT INTO config_audit(revision_id, action, actor, details_json, created_at)
                VALUES (?, 'activate', ?, ?, ?)
                """,
                (
                    int(cursor.lastrowid),
                    actor[:128],
                    json.dumps({"reason": reason[:512]}, ensure_ascii=False),
                    now,
                ),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return revision

    def metrics_prometheus(self) -> str:
        status = self.status()
        lines = [
            "# HELP argus_observations_total Stored normalized observations.",
            "# TYPE argus_observations_total gauge",
            f"argus_observations_total {status['observations']}",
        ]
        for state in ("pending", "sending", "delivered", "dead", "cancelled"):
            count = status["outbox"].get(state, 0)
            lines.append(f'argus_outbox{{status="{state}"}} {count}')
        lines.append(f"argus_outbox_oldest_pending_age_seconds {status['outbox_metrics']['oldest_pending_age_seconds']}")
        lines.extend([
            "# TYPE argus_incidents gauge",
            f"argus_incidents{{status=\"open\"}} {status['incidents'].get('open', 0)}",
            f"argus_incidents{{status=\"recovered\"}} {status['incidents'].get('recovered', 0)}",
            f"argus_incidents{{status=\"recorded\"}} {status['incidents'].get('recorded', 0)}",
            "# TYPE argus_reminders gauge",
            f"argus_reminders{{status=\"enabled\"}} {status['reminders'].get('enabled', 0)}",
            f"argus_reminders{{status=\"disabled\"}} {status['reminders'].get('disabled', 0)}",
        ])
        for source in status["sources"]:
            source_id = str(source["source_id"]).replace('"', '')
            success = source.get("last_success_at") or 0
            age = max(0, int(time.time()) - int(success)) if success else -1
            lines.append(f'argus_source_last_success_age_seconds{{source="{source_id}"}} {age}')
            lines.append(f'argus_source_consecutive_failures{{source="{source_id}"}} {source["consecutive_failures"]}')
            lines.append(f'argus_source_polls_total{{source="{source_id}"}} {source.get("poll_count", 0)}')
            lines.append(f'argus_source_failures_total{{source="{source_id}"}} {source.get("failure_count", 0)}')
        runtime = status.get("engine") or {}
        heartbeat = runtime.get("heartbeat_at") or 0
        heartbeat_age = max(0, int(time.time()) - int(heartbeat)) if heartbeat else -1
        lines.append(f"argus_engine_heartbeat_age_seconds {heartbeat_age}")
        return "\n".join(lines) + "\n"

    def cleanup(
        self,
        cutoff: int,
        *,
        incident_cutoff: int | None = None,
        audit_cutoff: int | None = None,
        dead_cutoff: int | None = None,
    ) -> tuple[int, int]:
        with self.connection:
            deleted_alerts = self.connection.execute(
                "DELETE FROM alerts WHERE status = 'delivered' AND delivered_at < ?", (cutoff,)
            ).rowcount
            if dead_cutoff is not None:
                deleted_alerts += self.connection.execute(
                    """
                    DELETE FROM alerts
                    WHERE status IN ('dead', 'cancelled') AND COALESCE(dead_at, created_at) < ?
                    """,
                    (dead_cutoff,),
                ).rowcount
            deleted_observations = self.connection.execute(
                """
                DELETE FROM observations
                WHERE fetched_at < ?
                  AND NOT EXISTS (
                      SELECT 1 FROM alerts WHERE alerts.observation_id = observations.id
                  )
                """,
                (cutoff,),
            ).rowcount
            if incident_cutoff is not None:
                self.connection.execute(
                    """
                    DELETE FROM incidents
                    WHERE status IN ('recorded', 'recovered') AND updated_at < ?
                      AND NOT EXISTS (SELECT 1 FROM alerts WHERE alerts.incident_id = incidents.id)
                    """,
                    (incident_cutoff,),
                )
            if audit_cutoff is not None:
                self.connection.execute("DELETE FROM config_audit WHERE created_at < ?", (audit_cutoff,))
                self.connection.execute("DELETE FROM reminder_audit WHERE created_at < ?", (audit_cutoff,))
                self.connection.execute(
                    "DELETE FROM admin_jobs WHERE completed_at IS NOT NULL AND completed_at < ?",
                    (audit_cutoff,),
                )
                self.connection.execute(
                    "DELETE FROM analysis_api_usage WHERE updated_at < ?", (audit_cutoff,)
                )
        return int(deleted_alerts), int(deleted_observations)

    def status(self) -> dict[str, Any]:
        sources = [dict(row) for row in self.connection.execute(
            """
            SELECT c.source_id, c.initialized, c.last_attempt_at, c.last_success_at,
                   c.consecutive_failures, c.outage_alerted, c.last_error,
                   c.last_duration_ms, c.last_error_kind, c.last_http_status,
                   c.poll_count, c.success_count, c.failure_count,
                   c.last_observation_count, c.last_alert_count, c.last_warning,
                   r.kind, r.configured_enabled, r.runtime_status,
                   r.expected_interval_seconds, r.config_revision,
                   r.registered_at, r.heartbeat_at, r.updated_at AS runtime_updated_at
            FROM collector_state AS c
            LEFT JOIN source_runtime AS r ON r.source_id = c.source_id
            ORDER BY c.source_id
            """
        )]
        outbox = {
            str(row["status"]): int(row["count"])
            for row in self.connection.execute(
                "SELECT status, COUNT(*) AS count FROM alerts GROUP BY status"
            )
        }
        oldest = self.connection.execute(
            "SELECT MIN(created_at) FROM alerts WHERE status IN ('pending', 'sending')"
        ).fetchone()[0]
        now = int(time.time())
        outbox_metrics = {
            "oldest_pending_age_seconds": max(0, now - int(oldest)) if oldest else 0,
            "retrying": int(self.connection.execute(
                "SELECT COUNT(*) FROM alerts WHERE status = 'pending' AND attempts > 0"
            ).fetchone()[0]),
        }
        observations = int(self.connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0])
        analysis_states = {
            str(row["processing_state"]): int(row["count"])
            for row in self.connection.execute(
                "SELECT processing_state, COUNT(*) AS count FROM observations "
                "GROUP BY processing_state"
            )
        }
        budget_day = datetime.fromtimestamp(now, UTC).date().isoformat()
        usage_row = self.connection.execute(
            "SELECT calls FROM analysis_api_usage WHERE budget_day = ?", (budget_day,)
        ).fetchone()
        incidents = {
            str(row["status"]): int(row["count"])
            for row in self.connection.execute("SELECT status, COUNT(*) AS count FROM incidents GROUP BY status")
        }
        reminders = {
            "enabled": int(self.connection.execute(
                "SELECT COUNT(*) FROM reminders WHERE enabled = 1"
            ).fetchone()[0]),
            "disabled": int(self.connection.execute(
                "SELECT COUNT(*) FROM reminders WHERE enabled = 0"
            ).fetchone()[0]),
        }
        revisions = [dict(row) for row in self.connection.execute(
            "SELECT revision, created_at, active FROM config_revisions WHERE active = 1 ORDER BY revision DESC LIMIT 1"
        )]
        engine_row = self.connection.execute("SELECT * FROM engine_runtime WHERE id = 1").fetchone()
        engine = dict(engine_row) if engine_row is not None else None
        if engine is not None:
            heartbeat = engine.get("heartbeat_at")
            engine["heartbeat_age_seconds"] = max(0, now - int(heartbeat)) if heartbeat else None
        return {
            "database_schema": SCHEMA_VERSION,
            "observations": observations,
            "analysis": {
                "states": analysis_states,
                "api_calls_today": int(usage_row["calls"]) if usage_row is not None else 0,
                "budget_day": budget_day,
            },
            "outbox": outbox,
            "outbox_metrics": outbox_metrics,
            "incidents": incidents,
            "reminders": reminders,
            "config_revision": revisions[0] if revisions else None,
            "desired_revision": revisions[0]["revision"] if revisions else None,
            "engine": engine,
            "runtime": engine,
            "sources": sources,
        }
