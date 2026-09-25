from __future__ import annotations

import fcntl
import base64
import json
import math
import os
import sqlite3
from contextlib import nullcontext
from dataclasses import asdict, replace
import time
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from .analysis import AnalysisAttempt, InformationAnalysis, analyze_observation
from .content import (
    ContentDocumentDraft,
    ContentFetchRequest,
    ContentFetchWorkItem,
    ContentLevel,
    content_availability,
    content_fetch_host,
    content_host_backoff_seconds,
)
from .digest import (
    DIGEST_RETRY_INTERVAL_SECONDS, DigestCluster, DigestDocument, DigestRetryState, SourceCoverage,
)
from .event_clustering import cluster_events, event_aggregate_score, event_evidence_score
from .digest_operations import digest_run_state
from .event_identity import identify_semantic_event
from .events import (
    EventListItem,
    EventPage,
    PersistedEvent,
    PersistedEventClaim,
    PersistedEventClaimEvidence,
    PersistedEventReport,
    PersistedEventTimelineItem,
)
from .manual_events import ManualEventSpec
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
from .sqlite_event_workspace import SQLiteEventWorkspace, migrate_event_workspace
from .util import sanitize_error, to_epoch
from .persistence import RevisionConflictError, SQLiteUnitOfWork
from .prompts import BUILTIN_PROMPTS, BUILTIN_PROMPT_VERSIONS, PromptTemplate, TRIAGE_V1

SCHEMA_VERSION = 20


_DIGEST_PROVIDER_TRACE_FIELDS = frozenset({
    "provider", "model", "prompt_id", "prompt_version", "prompt_hash",
    "status", "error", "elapsed_ms",
})


def _read_digest_provider_trace(value: object) -> list[dict[str, Any]]:
    """Decode bounded diagnostics without letting one legacy row break the read API."""
    if not isinstance(value, str):
        return []
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return []
    if not isinstance(decoded, list):
        return []
    traces: list[dict[str, Any]] = []
    for row in decoded[:12]:
        if not isinstance(row, Mapping):
            continue
        traces.append({
            str(key): (
                item[:500] if isinstance(item, str)
                else item if item is None or isinstance(item, (bool, int, float))
                else sanitize_error(str(item))[:500]
            )
            for key, item in row.items()
            if str(key) in _DIGEST_PROVIDER_TRACE_FIELDS
        })
    return traces


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
                with self.connection:
                    for prompt in BUILTIN_PROMPT_VERSIONS:
                        latest = BUILTIN_PROMPTS[prompt.prompt_id]
                        self.connection.execute(
                            "INSERT OR IGNORE INTO prompts(prompt_id, version, system_text, active, actor, created_at) "
                            "VALUES (?, ?, ?, ?, 'system', ?)",
                            (prompt.prompt_id, prompt.version, prompt.system_text,
                             int(prompt.version == latest.version), int(time.time())),
                        )
                        if prompt.version == latest.version:
                            self.connection.execute(
                                "UPDATE prompts SET active = 0 WHERE prompt_id = ? AND version != ? "
                                "AND actor = 'system'",
                                (prompt.prompt_id, prompt.version),
                            )
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
                            source_tiers_json TEXT NOT NULL DEFAULT '[]',
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

        if version < 13:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                locked_version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
                if locked_version < 13:
                    self.connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS admin_users (
                            id INTEGER PRIMARY KEY,
                            username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                            display_name TEXT NOT NULL,
                            password_hash TEXT NOT NULL,
                            role TEXT NOT NULL CHECK (role IN ('admin', 'operator', 'viewer')),
                            enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                            session_version INTEGER NOT NULL DEFAULT 1,
                            created_at INTEGER NOT NULL,
                            updated_at INTEGER NOT NULL,
                            last_login_at INTEGER
                        )
                        """
                    )
                    self.connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS admin_sessions (
                            session_hash TEXT PRIMARY KEY,
                            csrf_hash TEXT NOT NULL,
                            user_id INTEGER NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
                            session_version INTEGER NOT NULL,
                            created_at INTEGER NOT NULL,
                            expires_at INTEGER NOT NULL,
                            last_seen_at INTEGER NOT NULL,
                            revoked_at INTEGER
                        )
                        """
                    )
                    self.connection.execute(
                        "CREATE INDEX IF NOT EXISTS admin_sessions_user_expiry_idx "
                        "ON admin_sessions(user_id, expires_at)"
                    )
                    self.connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS admin_login_limits (
                            subject_hash TEXT PRIMARY KEY,
                            failures INTEGER NOT NULL CHECK (failures >= 0),
                            window_started_at INTEGER NOT NULL,
                            blocked_until INTEGER NOT NULL DEFAULT 0,
                            updated_at INTEGER NOT NULL
                        )
                        """
                    )
                    self.connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS admin_auth_audit (
                            id INTEGER PRIMARY KEY,
                            user_id INTEGER REFERENCES admin_users(id) ON DELETE SET NULL,
                            action TEXT NOT NULL,
                            actor TEXT NOT NULL,
                            details_json TEXT NOT NULL DEFAULT '{}',
                            created_at INTEGER NOT NULL
                        )
                        """
                    )
                    self.connection.execute(
                        "CREATE INDEX IF NOT EXISTS admin_auth_audit_time_idx "
                        "ON admin_auth_audit(created_at DESC, id DESC)"
                    )
                    self.connection.execute("PRAGMA user_version=13")
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
            version = 13

        if version < 14:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                locked_version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
                if locked_version < 14:
                    self.connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS content_documents (
                            id INTEGER PRIMARY KEY,
                            observation_id INTEGER NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
                            level TEXT NOT NULL CHECK (
                                level IN ('metadata', 'excerpt', 'full_text', 'document', 'analysis')
                            ),
                            source_method TEXT NOT NULL,
                            body TEXT NOT NULL,
                            media_type TEXT NOT NULL,
                            canonical_url TEXT NOT NULL DEFAULT '',
                            content_hash TEXT NOT NULL,
                            rights_policy TEXT NOT NULL,
                            language TEXT NOT NULL DEFAULT '',
                            metadata_json TEXT NOT NULL DEFAULT '{}',
                            fetched_at INTEGER NOT NULL,
                            created_at INTEGER NOT NULL,
                            UNIQUE (observation_id, level, source_method, content_hash)
                        )
                        """
                    )
                    self.connection.execute(
                        "CREATE INDEX IF NOT EXISTS content_documents_observation_idx "
                        "ON content_documents(observation_id, id)"
                    )
                    self.connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS content_fetch_jobs (
                            id INTEGER PRIMARY KEY,
                            observation_id INTEGER NOT NULL UNIQUE
                                REFERENCES observations(id) ON DELETE CASCADE,
                            url TEXT NOT NULL,
                            allowed_hosts_json TEXT NOT NULL,
                            max_response_bytes INTEGER NOT NULL,
                            timeout_seconds INTEGER NOT NULL,
                            status TEXT NOT NULL CHECK (
                                status IN ('pending', 'leased', 'retry', 'completed', 'dead')
                            ),
                            attempts INTEGER NOT NULL DEFAULT 0,
                            next_attempt_at INTEGER NOT NULL,
                            lease_token TEXT,
                            lease_until INTEGER,
                            last_error TEXT,
                            failure_kind TEXT,
                            created_at INTEGER NOT NULL,
                            updated_at INTEGER NOT NULL,
                            completed_at INTEGER,
                            dead_at INTEGER
                        )
                        """
                    )
                    self.connection.execute(
                        "CREATE INDEX IF NOT EXISTS content_fetch_jobs_due_idx "
                        "ON content_fetch_jobs(status, next_attempt_at, id)"
                    )
                    self.connection.execute("PRAGMA user_version=14")
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
            version = 14

        if version < 15:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                locked_version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
                if locked_version < 15:
                    self.connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS digest_retry_state (
                            digest_key TEXT PRIMARY KEY,
                            fallback_version INTEGER NOT NULL CHECK (fallback_version >= 1),
                            attempts INTEGER NOT NULL CHECK (attempts >= 1),
                            next_attempt_at INTEGER,
                            retry_deadline_at INTEGER NOT NULL CHECK (retry_deadline_at >= 0),
                            status TEXT NOT NULL CHECK (status IN ('pending', 'succeeded', 'failed')),
                            last_error TEXT,
                            started_at INTEGER NOT NULL,
                            updated_at INTEGER NOT NULL,
                            CHECK ((status = 'pending' AND next_attempt_at IS NOT NULL) OR status != 'pending')
                        )
                        """
                    )
                    self.connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS digest_failure_state (
                            id INTEGER PRIMARY KEY CHECK (id = 1),
                            consecutive_failures INTEGER NOT NULL CHECK (consecutive_failures >= 0),
                            last_failed_digest_key TEXT,
                            updated_at INTEGER NOT NULL
                        )
                        """
                    )
                    self.connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS digest_api_usage (
                            digest_key TEXT PRIMARY KEY,
                            calls INTEGER NOT NULL CHECK (calls >= 0),
                            updated_at INTEGER NOT NULL
                        )
                        """
                    )
                    self.connection.execute("PRAGMA user_version=15")
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
            version = 15

        if version < 16:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                locked_version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
                if locked_version < 16:
                    digest_columns = {
                        str(row[1])
                        for row in self.connection.execute("PRAGMA table_info(digest_items)")
                    }
                    if "source_tiers_json" not in digest_columns:
                        self.connection.execute(
                            "ALTER TABLE digest_items ADD COLUMN source_tiers_json TEXT NOT NULL DEFAULT '[]'"
                        )
                    self.connection.execute("PRAGMA user_version=16")
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
            version = 16

        if version < 17:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                locked_version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
                if locked_version < 17:
                    digest_columns = {
                        str(row[1])
                        for row in self.connection.execute("PRAGMA table_info(digest_items)")
                    }
                    if "event_id" not in digest_columns:
                        self.connection.execute(
                            "ALTER TABLE digest_items ADD COLUMN event_id INTEGER REFERENCES events(id) ON DELETE SET NULL"
                        )
                        self.connection.execute(
                            "CREATE INDEX IF NOT EXISTS digest_items_event_idx ON digest_items(event_id)"
                        )
                    self.connection.executescript(
                        """
                        CREATE TABLE IF NOT EXISTS events (
                            id INTEGER PRIMARY KEY,
                            event_key TEXT NOT NULL UNIQUE,
                            fingerprint TEXT NOT NULL,
                            title TEXT NOT NULL,
                            summary TEXT NOT NULL DEFAULT '',
                            score REAL NOT NULL,
                            importance INTEGER NOT NULL CHECK (importance BETWEEN 1 AND 5),
                            urgency INTEGER NOT NULL CHECK (urgency BETWEEN 1 AND 5),
                            relevance INTEGER NOT NULL CHECK (relevance BETWEEN 1 AND 5),
                            confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
                            first_seen_at INTEGER NOT NULL,
                            last_seen_at INTEGER NOT NULL,
                            regions_json TEXT NOT NULL DEFAULT '[]',
                            topics_json TEXT NOT NULL DEFAULT '[]',
                            status TEXT NOT NULL CHECK (status IN ('active', 'quiet', 'closed')),
                            independent_source_count INTEGER NOT NULL DEFAULT 0 CHECK (independent_source_count >= 0),
                            created_at INTEGER NOT NULL,
                            updated_at INTEGER NOT NULL,
                            CHECK (last_seen_at >= first_seen_at)
                        );
                        CREATE INDEX IF NOT EXISTS events_fingerprint_idx
                            ON events(fingerprint, last_seen_at DESC);
                        CREATE INDEX IF NOT EXISTS events_period_idx
                            ON events(first_seen_at DESC, last_seen_at DESC, status);

                        CREATE TABLE IF NOT EXISTS event_reports (
                            id INTEGER PRIMARY KEY,
                            event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                            observation_id INTEGER NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
                            source_id TEXT NOT NULL,
                            source_tier TEXT NOT NULL CHECK (source_tier IN ('primary', 'secondary', 'social')),
                            relation TEXT NOT NULL CHECK (relation IN ('primary', 'corroborates', 'updates', 'contradicts', 'context', 'social')),
                            match_score REAL NOT NULL CHECK (match_score BETWEEN 0 AND 1),
                            is_representative INTEGER NOT NULL CHECK (is_representative IN (0, 1)),
                            published_at INTEGER NOT NULL,
                            title TEXT NOT NULL,
                            summary TEXT NOT NULL DEFAULT '',
                            url TEXT NOT NULL DEFAULT '',
                            created_at INTEGER NOT NULL,
                            UNIQUE (event_id, observation_id)
                        );
                        CREATE INDEX IF NOT EXISTS event_reports_event_idx
                            ON event_reports(event_id, published_at DESC, id DESC);
                        CREATE INDEX IF NOT EXISTS event_reports_observation_idx
                            ON event_reports(observation_id);

                        CREATE TABLE IF NOT EXISTS event_claims (
                            id INTEGER PRIMARY KEY,
                            event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                            claim_key TEXT NOT NULL,
                            text TEXT NOT NULL,
                            status TEXT NOT NULL CHECK (status IN ('active', 'superseded', 'disputed')),
                            confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
                            first_seen_at INTEGER NOT NULL,
                            last_seen_at INTEGER NOT NULL,
                            supersedes_claim_id INTEGER REFERENCES event_claims(id) ON DELETE SET NULL,
                            created_at INTEGER NOT NULL,
                            updated_at INTEGER NOT NULL,
                            UNIQUE (event_id, claim_key),
                            CHECK (last_seen_at >= first_seen_at)
                        );
                        CREATE INDEX IF NOT EXISTS event_claims_event_idx
                            ON event_claims(event_id, updated_at DESC, id DESC);

                        CREATE TABLE IF NOT EXISTS event_claim_evidence (
                            id INTEGER PRIMARY KEY,
                            claim_id INTEGER NOT NULL REFERENCES event_claims(id) ON DELETE CASCADE,
                            report_id INTEGER NOT NULL REFERENCES event_reports(id) ON DELETE CASCADE,
                            stance TEXT NOT NULL CHECK (stance IN ('supports', 'refutes', 'context')),
                            note TEXT NOT NULL DEFAULT '',
                            created_at INTEGER NOT NULL,
                            UNIQUE (claim_id, report_id, stance)
                        );
                        CREATE INDEX IF NOT EXISTS event_claim_evidence_claim_idx
                            ON event_claim_evidence(claim_id, created_at DESC, id DESC);

                        CREATE TABLE IF NOT EXISTS event_timeline (
                            id INTEGER PRIMARY KEY,
                            event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                            occurred_at INTEGER NOT NULL,
                            kind TEXT NOT NULL,
                            text TEXT NOT NULL,
                            confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
                            report_id INTEGER REFERENCES event_reports(id) ON DELETE SET NULL,
                            created_at INTEGER NOT NULL
                        );
                        CREATE INDEX IF NOT EXISTS event_timeline_event_idx
                            ON event_timeline(event_id, occurred_at ASC, id ASC);
                        """
                    )
                    self.connection.execute("PRAGMA user_version=17")
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
            version = 17

        if version < 18:
            migrate_event_workspace(self)
            version = 18
        if version < 19:
            with self.unit_of_work():
                self.connection.execute("""
                    CREATE TABLE IF NOT EXISTS digest_generation_attempts (
                        id INTEGER PRIMARY KEY,
                        digest_key TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','interrupted')),
                        started_at INTEGER NOT NULL,
                        finished_at INTEGER,
                        error TEXT,
                        providers_json TEXT NOT NULL DEFAULT '[]'
                    )
                """)
                self.connection.execute(
                    "CREATE INDEX IF NOT EXISTS digest_generation_attempts_key_idx "
                    "ON digest_generation_attempts(digest_key, id DESC)"
                )
                self.connection.execute("PRAGMA user_version=19")
            version = 19
        if version < 20:
            with self.unit_of_work():
                self.connection.execute("""
                    CREATE TABLE IF NOT EXISTS digest_preparation_failures (
                        digest_key TEXT PRIMARY KEY,
                        stage TEXT NOT NULL CHECK(stage IN ('projection', 'build')),
                        last_error TEXT NOT NULL,
                        first_failed_at INTEGER NOT NULL,
                        last_failed_at INTEGER NOT NULL,
                        failure_count INTEGER NOT NULL CHECK(failure_count > 0)
                    )
                """)
                self.connection.execute("PRAGMA user_version=20")

    def record_digest_preparation_failure(
        self, digest_key: str, *, stage: str, error: BaseException | str, now: int,
    ) -> None:
        if not digest_key or len(digest_key) > 128 or stage not in {"projection", "build"} or now < 0:
            raise ValueError("invalid digest preparation failure")
        with self.unit_of_work():
            self.connection.execute("""
                INSERT INTO digest_preparation_failures
                    (digest_key,stage,last_error,first_failed_at,last_failed_at,failure_count)
                VALUES (?,?,?,?,?,1)
                ON CONFLICT(digest_key) DO UPDATE SET
                    stage=excluded.stage,
                    last_error=excluded.last_error,
                    last_failed_at=excluded.last_failed_at,
                    failure_count=failure_count+1
            """, (digest_key, stage, sanitize_error(error), now, now))

    def clear_digest_preparation_failure(self, digest_key: str) -> None:
        if not digest_key or len(digest_key) > 128:
            raise ValueError("invalid digest key")
        with self.unit_of_work():
            self.connection.execute(
                "DELETE FROM digest_preparation_failures WHERE digest_key=?", (digest_key,),
            )

    def start_digest_attempt(self, digest_key: str, *, now: int) -> int:
        if not digest_key or len(digest_key) > 128 or now < 0:
            raise ValueError("invalid digest attempt")
        with self.unit_of_work():
            cursor = self.connection.execute(
                "INSERT INTO digest_generation_attempts(digest_key,status,started_at) "
                "VALUES (?, 'running', ?)", (digest_key, now),
            )
        return int(cursor.lastrowid)

    def finish_digest_attempt(
        self, attempt_id: int, *, status: str, now: int,
        error: str | None = None, providers: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        if status not in {"succeeded", "failed", "interrupted"} or now < 0:
            raise ValueError("invalid digest attempt result")
        # Only allow diagnostic fields. Neither request bodies nor credentials
        # belong in attempt history, even from an alternative provider adapter.
        allowed = {"provider", "model", "prompt_id", "prompt_version", "prompt_hash",
                   "status", "error", "elapsed_ms"}
        safe_providers = [{key: sanitize_error(str(value))[:500] for key, value in row.items()
                           if key in allowed} for row in list(providers)[:12]]
        with self.unit_of_work():
            self.connection.execute(
                "UPDATE digest_generation_attempts SET status=?, finished_at=?, error=?, providers_json=? "
                "WHERE id=? AND status='running'",
                (status, now, sanitize_error(error)[:1000] if error else None,
                 json.dumps(safe_providers, ensure_ascii=False), attempt_id),
            )

    def get_digest_run(self, digest_key: str, *, now: int) -> dict[str, Any] | None:
        if not digest_key or len(digest_key) > 128 or now < 0:
            raise ValueError("invalid digest run")
        with (nullcontext() if self.connection.in_transaction else self.unit_of_work(immediate=False)):
            digest = self.connection.execute(
                "SELECT version,generation_kind,published_at FROM digests "
                "WHERE digest_key=? AND status='published' ORDER BY version DESC LIMIT 1", (digest_key,),
            ).fetchone()
            retry_row = self.connection.execute(
                "SELECT * FROM digest_retry_state WHERE digest_key=?", (digest_key,),
            ).fetchone()
            retry = dict(retry_row) if retry_row else None
            preparation_row = self.connection.execute(
                "SELECT stage,last_error,first_failed_at,last_failed_at,failure_count "
                "FROM digest_preparation_failures WHERE digest_key=?", (digest_key,),
            ).fetchone()
            preparation = dict(preparation_row) if preparation_row else None
            attempts = [dict(row) for row in self.connection.execute(
                "SELECT * FROM digest_generation_attempts WHERE digest_key=? ORDER BY id DESC LIMIT 50",
                (digest_key,),
            )]
            usage = self.connection.execute(
                "SELECT calls FROM digest_api_usage WHERE digest_key=?", (digest_key,),
            ).fetchone()
        if digest is None and retry is None and not attempts and preparation is None:
            return None
        for attempt in attempts:
            attempt["providers"] = _read_digest_provider_trace(attempt.pop("providers_json", None))
        generation_kind = str(digest["generation_kind"]) if digest else None
        return {
            "digest_key": digest_key,
            "state": digest_run_state(generation_kind=generation_kind, retry=retry,
                                      latest_attempt=attempts[0] if attempts else None,
                                      preparation=preparation, now=now),
            "published_version": digest["version"] if digest else None,
            "generation_kind": generation_kind,
            "published_at": digest["published_at"] if digest else None,
            "retry": retry, "attempts": attempts, "preparation": preparation,
            "reserved_attempts": int(usage["calls"]) if usage else 0,
            "attempt_history_available": bool(attempts),
            "can_retry_now": bool(retry and retry["status"] == "pending"
                                  and (int(usage["calls"]) if usage else 0) < 5
                                  and int(retry["attempts"]) < 5
                                  and now < int(retry["retry_deadline_at"])
                                  and retry["next_attempt_at"] > now
                                  and generation_kind != "api"
                                  and not (attempts and attempts[0]["status"] == "running")),
        }

    def list_digest_runs(self, *, now: int, limit: int = 30) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100 or now < 0:
            raise ValueError("invalid digest run limit")
        rows = self.connection.execute(
            "SELECT digest_key FROM (SELECT digest_key,created_at AS at FROM digests "
            "UNION ALL SELECT digest_key,started_at FROM digest_generation_attempts "
            "UNION ALL SELECT digest_key,started_at FROM digest_retry_state "
            "UNION ALL SELECT digest_key,last_failed_at FROM digest_preparation_failures) "
            "GROUP BY digest_key ORDER BY MAX(at) DESC, digest_key DESC LIMIT ?", (limit,),
        ).fetchall()
        return [run for row in rows if (run := self.get_digest_run(str(row[0]), now=now)) is not None]

    def request_digest_retry_now(
        self, digest_key: str, *, actor: str, request_id: str, now: int
    ) -> dict[str, Any]:
        if not request_id or len(request_id) > 64 or not actor or len(actor) > 128:
            raise ValueError("invalid digest retry request")
        with self.unit_of_work():
            existing = self.get_admin_job(request_id)
            if existing:
                if (existing['kind'] != 'digest_retry_now' or existing['actor'] != actor
                        or existing['request'] != {'digest_key': digest_key}):
                    raise ValueError("retry request identity conflict")
                return existing
            run = self.get_digest_run(digest_key, now=now)
            if run is None:
                raise KeyError("digest run not found")
            if not run["can_retry_now"]:
                raise ValueError("digest has no scheduled retry that can be advanced")
            self.connection.execute(
                "UPDATE digest_retry_state SET next_attempt_at=?,updated_at=? WHERE digest_key=?",
                (now, now, digest_key),
            )
            self.connection.execute(
                "INSERT INTO admin_jobs(id,kind,status,request_json,result_json,actor,created_at,expires_at,completed_at) "
                "VALUES (?, 'digest_retry_now','succeeded',?, ?, ?, ?, ?, ?)",
                (request_id, json.dumps({'digest_key': digest_key}), json.dumps({'scheduled_at': now}),
                 actor, now, now+900, now),
            )
        result = self.get_admin_job(request_id)
        assert result is not None
        return result

    def close(self) -> None:
        self.connection.close()

    def unit_of_work(self, *, immediate: bool = True) -> SQLiteUnitOfWork:
        return SQLiteUnitOfWork(self.connection, immediate=immediate)

    def _insert_content_document(
        self,
        observation_id: int,
        document: ContentDocumentDraft,
        now: int,
    ) -> bool:
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO content_documents(
                observation_id, level, source_method, body, media_type, canonical_url,
                content_hash, rights_policy, language, metadata_json, fetched_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation_id,
                document.level.value,
                document.source_method,
                document.body,
                document.media_type,
                document.canonical_url,
                document.sha256,
                document.rights_policy,
                document.language,
                json.dumps(dict(document.metadata), ensure_ascii=False, sort_keys=True),
                now,
                now,
            ),
        )
        return cursor.rowcount == 1

    def _insert_default_content_document(
        self,
        observation_id: int,
        observation: Observation,
        now: int,
    ) -> None:
        level = ContentLevel.EXCERPT if observation.summary.strip() else ContentLevel.METADATA
        document = ContentDocumentDraft(
            level,
            "normalized_observation",
            observation.summary or observation.title,
            canonical_url=observation.url,
            rights_policy="source_terms_apply",
        )
        self._insert_content_document(observation_id, document, now)

    def _enqueue_content_fetch(
        self,
        observation_id: int,
        request: ContentFetchRequest,
        now: int,
    ) -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO content_fetch_jobs(
                observation_id, url, allowed_hosts_json, max_response_bytes,
                timeout_seconds, status, attempts, next_attempt_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)
            """,
            (
                observation_id,
                request.url,
                json.dumps(list(request.allowed_hosts), ensure_ascii=False),
                request.max_response_bytes,
                request.timeout_seconds,
                now,
                now,
                now,
            ),
        )

    def claim_content_fetch(
        self,
        now: int,
        lease_seconds: int = 60,
    ) -> ContentFetchWorkItem | None:
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("content fetch lease is out of range")
        lease_token = uuid.uuid4().hex
        with self.unit_of_work():
            self.connection.execute(
                """
                UPDATE content_fetch_jobs
                SET status = 'retry', lease_token = NULL, lease_until = NULL,
                    next_attempt_at = ?, updated_at = ?,
                    last_error = COALESCE(last_error, 'worker lease expired'),
                    failure_kind = COALESCE(failure_kind, 'lease_expired')
                WHERE status = 'leased' AND lease_until <= ?
                """,
                (now, now, now),
            )
            candidates = self.connection.execute(
                """
                SELECT id, url, status FROM content_fetch_jobs
                WHERE status IN ('pending', 'retry') AND next_attempt_at <= ?
                ORDER BY next_attempt_at, id LIMIT 50
                """,
                (now,),
            ).fetchall()
            if not candidates:
                return None
            # Failure rows are the durable source of short host pauses. This uses
            # existing task state, including after a restart, without a second queue.
            host_pauses: dict[str, int] = {}
            for failure in self.connection.execute(
                "SELECT url, failure_kind, updated_at FROM content_fetch_jobs "
                "WHERE updated_at > ? AND status IN ('dead', 'retry') "
                "AND failure_kind IS NOT NULL",
                (now - 3600,),
            ):
                seconds = content_host_backoff_seconds(str(failure["failure_kind"]))
                until = int(failure["updated_at"]) + seconds
                host = content_fetch_host(str(failure["url"]))
                if seconds and until > now:
                    host_pauses[host] = max(host_pauses.get(host, 0), until)
            row = None
            for candidate in candidates:
                until = host_pauses.get(content_fetch_host(str(candidate["url"])), 0)
                if until > now:
                    # Do not alter retry.updated_at: that is the failure timestamp,
                    # and changing it would keep extending the pause indefinitely.
                    self.connection.execute(
                        "UPDATE content_fetch_jobs SET next_attempt_at = MAX(next_attempt_at, ?), "
                        "failure_kind = CASE WHEN status = 'pending' THEN 'host_backoff' ELSE failure_kind END "
                        "WHERE id = ? AND status IN ('pending', 'retry')",
                        (until, int(candidate["id"])),
                    )
                    continue
                row = candidate
                break
            if row is None:
                return None
            job_id = int(row["id"])
            updated = self.connection.execute(
                """
                UPDATE content_fetch_jobs
                SET status = 'leased', attempts = attempts + 1, lease_token = ?,
                    lease_until = ?, updated_at = ?
                WHERE id = ? AND status IN ('pending', 'retry')
                """,
                (lease_token, now + lease_seconds, now, job_id),
            )
            if updated.rowcount != 1:
                return None
            claimed = self.connection.execute(
                "SELECT * FROM content_fetch_jobs WHERE id = ? AND lease_token = ?",
                (job_id, lease_token),
            ).fetchone()
        if claimed is None:
            return None
        try:
            allowed_hosts_value = json.loads(claimed["allowed_hosts_json"])
            allowed_hosts = tuple(str(item) for item in allowed_hosts_value)
        except (TypeError, ValueError):
            allowed_hosts = ()
        try:
            request = ContentFetchRequest(
                str(claimed["url"]),
                allowed_hosts,
                max_response_bytes=int(claimed["max_response_bytes"]),
                timeout_seconds=int(claimed["timeout_seconds"]),
            )
        except (TypeError, ValueError) as exc:
            with self.connection:
                self.connection.execute(
                    """
                    UPDATE content_fetch_jobs
                    SET status = 'dead', lease_token = NULL, lease_until = NULL,
                        last_error = ?, failure_kind = 'invalid_job', updated_at = ?, dead_at = ?
                    WHERE id = ? AND lease_token = ?
                    """,
                    (sanitize_error(exc), now, now, int(claimed["id"]), lease_token),
                )
            return None
        return ContentFetchWorkItem(
            id=int(claimed["id"]),
            observation_id=int(claimed["observation_id"]),
            request=request,
            lease_token=lease_token,
            attempts=int(claimed["attempts"]),
        )

    def complete_content_fetch(
        self,
        item: ContentFetchWorkItem,
        document: ContentDocumentDraft,
        now: int,
    ) -> bool:
        with self.unit_of_work():
            active = self.connection.execute(
                "SELECT 1 FROM content_fetch_jobs WHERE id = ? AND status = 'leased' "
                "AND lease_token = ? AND observation_id = ?",
                (item.id, item.lease_token, item.observation_id),
            ).fetchone()
            if active is None:
                return False
            self._insert_content_document(item.observation_id, document, now)
            updated = self.connection.execute(
                """
                UPDATE content_fetch_jobs
                SET status = 'completed', lease_token = NULL, lease_until = NULL,
                    last_error = NULL, failure_kind = NULL, updated_at = ?, completed_at = ?
                WHERE id = ? AND status = 'leased' AND lease_token = ?
                """,
                (now, now, item.id, item.lease_token),
            )
        return updated.rowcount == 1

    def fail_content_fetch(
        self,
        item: ContentFetchWorkItem,
        error: BaseException | str,
        now: int,
        next_attempt_at: int,
        *,
        retryable: bool,
        failure_kind: str,
        max_attempts: int = 5,
    ) -> str | None:
        if max_attempts < 1:
            raise ValueError("content fetch attempt limit must be positive")
        terminal = not retryable or item.attempts >= max_attempts
        status = "dead" if terminal else "retry"
        with self.connection:
            updated = self.connection.execute(
                """
                UPDATE content_fetch_jobs
                SET status = ?, lease_token = NULL, lease_until = NULL,
                    next_attempt_at = ?, last_error = ?, failure_kind = ?, updated_at = ?,
                    dead_at = CASE WHEN ? = 'dead' THEN ? ELSE NULL END
                WHERE id = ? AND status = 'leased' AND lease_token = ?
                """,
                (
                    status,
                    next_attempt_at if not terminal else now,
                    sanitize_error(error),
                    failure_kind[:128],
                    now,
                    status,
                    now,
                    item.id,
                    item.lease_token,
                ),
            )
        return status if updated.rowcount == 1 else None

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
                "UPDATE digest_generation_attempts SET status='interrupted',finished_at=?, "
                "error='engine restarted before attempt acknowledgement' WHERE status='running'",
                (started_at,),
            )
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
        explicitly_disabled = {source_id for source_id, _, enabled, _ in sources if not enabled}
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
            for source_id in sorted(explicitly_disabled):
                self.connection.execute(
                    """
                    UPDATE collector_state
                    SET consecutive_failures = 0, outage_alerted = 0,
                        outage_started_at = NULL, last_error = NULL,
                        last_error_kind = NULL, last_http_status = NULL
                    WHERE source_id = ?
                    """,
                    (source_id,),
                )
            open_source_incidents = list(self.connection.execute(
                "SELECT id, incident_key, evidence_json FROM incidents "
                "WHERE kind = 'stateful' AND status = 'open' "
                "AND incident_key LIKE 'system:source:%'"
            ))
            for incident in open_source_incidents:
                source_id = str(incident["incident_key"]).removeprefix("system:source:")
                if source_id in identifiers and source_id not in explicitly_disabled:
                    continue
                try:
                    evidence = list(json.loads(incident["evidence_json"]))
                except (TypeError, ValueError):
                    evidence = []
                resolution = (
                    "source disabled by configuration"
                    if source_id in explicitly_disabled
                    else "source removed from configuration"
                )
                if resolution not in evidence:
                    evidence.append(resolution)
                self.connection.execute(
                    """
                    UPDATE incidents
                    SET status = 'recovered', recovered_at = ?, updated_at = ?, evidence_json = ?
                    WHERE id = ?
                    """,
                    (now, now, json.dumps(evidence, ensure_ascii=False), int(incident["id"])),
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
            semantic_news = candidate.incident_kind == "event" and candidate.incident_key.startswith("news-event:")
            persistent_weather = candidate.incident_kind == "event" and candidate.incident_key.startswith("weather-event:")
            if semantic_news:
                existing = self.connection.execute(
                    """
                    SELECT MAX(a.priority) AS max_priority FROM alerts a
                    WHERE a.incident_id = ? AND a.topic = ? AND a.created_at >= ?
                      AND a.status IN ('pending', 'sending', 'delivered')
                    """,
                    (incident_id, candidate.topic, now - 21600),
                ).fetchone()
                if existing is not None and existing["max_priority"] is not None and int(existing["max_priority"]) >= candidate.priority:
                    return False
            elif persistent_weather:
                # JMA republishes the same warning through several bulletin
                # types and at regular intervals.  A stable weather identity
                # already includes hazard, severity and normalized area, so
                # the same sequence is suppressed indefinitely.  A changed
                # severity/area/hazard gets a new identity and is notified.
                existing = self.connection.execute(
                    """
                    SELECT MAX(a.priority) AS max_priority FROM alerts a
                    WHERE a.incident_id = ? AND a.topic = ?
                      AND a.status IN ('pending', 'sending', 'delivered')
                    """,
                    (incident_id, candidate.topic),
                ).fetchone()
                if existing is not None and existing["max_priority"] is not None and int(existing["max_priority"]) >= candidate.priority:
                    return False
            else:
                existing = self.connection.execute(
                    "SELECT 1 FROM alerts WHERE rule_id = ? AND incident_key = ? AND created_at >= ? LIMIT 1",
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
                if observation.content_documents:
                    for document in observation.content_documents:
                        self._insert_content_document(observation_id, document, now)
                else:
                    self._insert_default_content_document(observation_id, observation, now)
                # A first successful poll establishes the deduplication baseline.
                # Persist feed-provided content, but do not unexpectedly crawl all
                # historical article URLs from that initial batch.
                if state.initialized and observation.content_fetch is not None:
                    self._enqueue_content_fetch(observation_id, observation.content_fetch, now)
                if state.initialized or observation.attributes.get("live_state") is True:
                    for candidate in rules.evaluate(observation, now):
                        if self._insert_alert(candidate, observation_id, now):
                            queued += 1
                            self.connection.execute(
                                "UPDATE observations SET handling = 'immediate' WHERE id = ?",
                                (observation_id,),
                            )

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
            observation_id=(
                int(row["observation_id"]) if row["observation_id"] is not None else None
            ),
            incident_id=int(row["incident_id"]) if row["incident_id"] is not None else None,
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
                (SELECT a.id FROM alerts AS a WHERE a.incident_id = i.id
                 ORDER BY a.created_at DESC, a.id DESC LIMIT 1) AS latest_alert_id,
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

    def get_alert_detail(self, alert_id: int) -> dict[str, Any] | None:
        """Return one notification with its exact observation and incident context."""
        if alert_id < 1:
            return None
        row = self.connection.execute(
            """
            SELECT id, observation_id, incident_id, title, message, priority,
                   confidence, evidence_json, tags_json, click_url, status,
                   created_at, delivered_at
            FROM alerts WHERE id = ?
            """,
            (alert_id,),
        ).fetchone()
        if row is None:
            return None
        alert = dict(row)
        for field in ("evidence_json", "tags_json"):
            try:
                alert[field[:-5]] = json.loads(alert.pop(field))
            except (TypeError, ValueError):
                alert[field[:-5]] = []
        alert["source_url"] = alert.pop("click_url")

        observation = None
        if row["observation_id"] is not None:
            observation_row = self.connection.execute(
                """
                SELECT id, source_id, publisher, published_at, fetched_at, title,
                       summary, url, attributes_json, importance, urgency, relevance,
                       confidence, region, topic, source_tier, information_type, handling
                FROM observations WHERE id = ?
                """,
                (row["observation_id"],),
            ).fetchone()
            if observation_row is not None:
                observation = dict(observation_row)
                try:
                    observation["attributes"] = json.loads(
                        observation.pop("attributes_json")
                    )
                except (TypeError, ValueError):
                    observation["attributes"] = {}

        documents: list[dict[str, Any]] = []
        content_fetch = None
        if row["observation_id"] is not None:
            for document_row in self.connection.execute(
                """
                SELECT id, observation_id, level, source_method, body, media_type,
                       canonical_url, content_hash, rights_policy, language,
                       metadata_json, fetched_at, created_at
                FROM content_documents
                WHERE observation_id = ?
                ORDER BY CASE level
                    WHEN 'document' THEN 1
                    WHEN 'full_text' THEN 2
                    WHEN 'analysis' THEN 3
                    WHEN 'excerpt' THEN 4
                    ELSE 5 END, id DESC
                """,
                (row["observation_id"],),
            ):
                document = dict(document_row)
                try:
                    document["metadata"] = json.loads(document.pop("metadata_json"))
                except (TypeError, ValueError):
                    document["metadata"] = {}
                documents.append(document)
            fetch_row = self.connection.execute(
                """
                SELECT status, attempts, next_attempt_at, last_error, failure_kind,
                       updated_at, completed_at, dead_at
                FROM content_fetch_jobs WHERE observation_id = ?
                """,
                (row["observation_id"],),
            ).fetchone()
            if fetch_row is not None:
                content_fetch = dict(fetch_row)

        incident = None
        if row["incident_id"] is not None:
            incident_row = self.connection.execute(
                """
                SELECT id, kind, status, first_seen_at, last_seen_at, recovered_at,
                       confidence, evidence_json, source_ids_json, observation_count
                FROM incidents WHERE id = ?
                """,
                (row["incident_id"],),
            ).fetchone()
            if incident_row is not None:
                incident = dict(incident_row)
                for field in ("evidence_json", "source_ids_json"):
                    try:
                        incident[field[:-5]] = json.loads(incident.pop(field))
                    except (TypeError, ValueError):
                        incident[field[:-5]] = []

        return {
            "alert": alert,
            "observation": observation,
            "incident": incident,
            "documents": documents,
            "content_fetch": content_fetch,
            "content_availability": content_availability(documents, content_fetch),
        }

    def create_manual_event(
        self,
        event: ManualEventSpec,
        actor: str,
        now: int,
        topic: str,
    ) -> dict[str, Any]:
        """Persist an administrator event and its notification as one unit."""
        actor_name = actor.strip()[:128] or "unknown"
        notification_topic = topic.strip()
        if not notification_topic:
            raise ValueError("notification topic is required")
        identity = uuid.uuid4().hex
        dedupe_key = f"manual:{identity}"
        with self.unit_of_work():
            cursor = self.connection.execute(
                """
                INSERT INTO observations(
                    source_id, publisher, dedupe_scope, external_id,
                    published_at, fetched_at, title, summary, url, attributes_json,
                    importance, urgency, relevance, confidence, region, topic,
                    source_tier, information_type, handling, processing_state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "manual",
                    "人工录入",
                    "manual",
                    identity,
                    now,
                    now,
                    event.title,
                    event.summary,
                    event.source_url,
                    json.dumps(
                        {"manual": True, "actor": actor_name, "submitted_via": "admin"},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    event.importance,
                    event.importance,
                    5,
                    1.0,
                    event.region,
                    event.topic,
                    "primary",
                    "manual",
                    "immediate",
                    "analyzed",
                ),
            )
            observation_id = int(cursor.lastrowid)
            self._insert_content_document(
                observation_id,
                ContentDocumentDraft(
                    ContentLevel.FULL_TEXT,
                    "manual_entry",
                    event.summary,
                    canonical_url=event.source_url,
                    rights_policy="user_supplied",
                ),
                now,
            )
            candidate = AlertCandidate(
                rule_id="manual.admin",
                dedupe_key=dedupe_key,
                title=f"EyeOfSauron 人工事件：{event.title}",
                message=event.summary,
                priority=event.importance,
                confidence=1.0,
                evidence=("后台人工录入", f"提交者：{actor_name}"),
                tags=("memo",),
                click_url=event.source_url,
                topic=notification_topic,
                incident_key=f"manual:event:{identity}",
                incident_kind="event",
            )
            if not self._insert_alert(candidate, observation_id, now):
                raise RuntimeError("manual event notification could not be queued")
            alert_row = self.connection.execute(
                "SELECT id FROM alerts WHERE dedupe_key = ?", (dedupe_key,)
            ).fetchone()
            if alert_row is None:
                raise RuntimeError("manual event notification was not persisted")
            alert_id = int(alert_row["id"])
        detail = self.get_alert_detail(alert_id)
        if detail is None:
            raise RuntimeError("manual event detail could not be loaded")
        return detail

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

    def list_digest_observations(
        self, since: int, until: int, *, source_ids: Sequence[str] | None = None,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        """Filter before limiting, with fair per-source sampling and alert history.

        Alert creation time also admits delayed notifications about older news.
        A rule-selected observation remains immediate even after reanalysis.
        """
        if since < 0 or until < since or not 1 <= limit <= 5000:
            raise ValueError("digest candidate bounds are invalid")
        clauses = ["(o.handling IN ('digest', 'immediate') OR a.observation_id IS NOT NULL)",
                   "((o.published_at >= ? AND o.published_at < ?) OR a.in_period = 1)"]
        params: list[Any] = [since, until, since, until]
        if source_ids is not None:
            if not source_ids:
                return []
            clauses.append(f"o.source_id IN ({','.join('?' for _ in source_ids)})")
            params.extend(source_ids)
        params.append(limit)
        rows = self.connection.execute(
            "WITH notified AS (SELECT observation_id, "
            "MAX(CASE WHEN created_at >= ? AND created_at < ? THEN 1 ELSE 0 END) AS in_period "
            "FROM alerts WHERE observation_id IS NOT NULL AND status != 'cancelled' "
            "GROUP BY observation_id), candidates AS ("
            "SELECT o.*, CASE WHEN a.observation_id IS NOT NULL THEN 'immediate' "
            "ELSE o.handling END AS effective_handling, "
            "ROW_NUMBER() OVER (PARTITION BY o.source_id ORDER BY "
            "(a.observation_id IS NOT NULL) DESC, o.importance DESC, o.published_at DESC, o.id DESC) AS source_rank "
            "FROM observations o LEFT JOIN notified a ON a.observation_id = o.id "
            f"WHERE {' AND '.join(clauses)}) "
            "SELECT * FROM candidates ORDER BY (effective_handling = 'immediate') DESC, "
            "source_rank, importance DESC, published_at DESC, id DESC LIMIT ?", params,
        )
        result = []
        for row in rows:
            item = dict(row)
            item['handling'] = item.pop('effective_handling')
            item.pop('source_rank')
            try:
                attributes = json.loads(item.pop('attributes_json'))
            except (TypeError, ValueError):
                attributes = {}
            item['attributes'] = attributes if isinstance(attributes, dict) else {}
            result.append(item)
        return result

    def get_analysis_text(self, observation_id: int, *, max_chars: int) -> str:
        """Read bounded, already acquired content; never fetch on model demand."""
        row = self.connection.execute(
            "SELECT substr(body, 1, ?) FROM content_documents WHERE observation_id = ? "
            "AND level IN ('full_text', 'document') ORDER BY id DESC LIMIT 1",
            (max_chars, observation_id),
        ).fetchone()
        return str(row[0]) if row else ""

    def backfill_default_topics(self, topics: Mapping[str, str]) -> int:
        """Repair legacy generic topics without replacing item-specific values."""
        changed = 0
        with self.unit_of_work():
            for source_id, topic in topics.items():
                if not source_id or not topic or topic == "general":
                    continue
                cursor = self.connection.execute(
                    "UPDATE observations SET topic = ? WHERE source_id = ? AND topic = 'general'",
                    (topic[:80], source_id),
                )
                changed += cursor.rowcount
        return changed

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

    def reserve_digest_api_call(self, digest_key: str, *, limit: int, now: int) -> bool:
        """Reserve a synthesis attempt independently from per-article API triage."""
        if not digest_key or len(digest_key) > 128 or not 0 <= limit <= 5 or now < 0:
            raise ValueError("digest API budget is invalid")
        if limit == 0:
            return False
        with self.unit_of_work():
            row = self.connection.execute(
                "SELECT calls FROM digest_api_usage WHERE digest_key = ?", (digest_key,)
            ).fetchone()
            if row is not None and int(row["calls"]) >= limit:
                return False
            self.connection.execute(
                """
                INSERT INTO digest_api_usage(digest_key, calls, updated_at)
                VALUES (?, 1, ?)
                ON CONFLICT(digest_key) DO UPDATE SET
                    calls = digest_api_usage.calls + 1, updated_at = excluded.updated_at
                """,
                (digest_key, now),
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
                    handling = CASE WHEN handling = 'immediate' THEN handling ELSE ? END,
                    processing_state = ?, analysis_lease_token = NULL,
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

    def get_source_health(
        self, source_id: str, *, now: int | None = None
    ) -> dict[str, Any] | None:
        """Read one consistent diagnostics snapshot without changing source state."""
        source_id = self._validate_source_quality_source_id(source_id)
        current = int(time.time()) if now is None else now
        if current < 0:
            raise ValueError("source diagnostics time is invalid")
        since = max(0, current - 7 * 86400)
        with self.unit_of_work(immediate=False):
            row = self.connection.execute(
                "SELECT c.source_id, c.last_attempt_at, c.last_success_at, "
                "c.consecutive_failures, c.last_error, c.last_error_kind, "
                "c.last_http_status, c.last_warning, c.poll_count, c.success_count, "
                "c.failure_count, r.runtime_status, r.configured_enabled, r.last_error AS runtime_error "
                "FROM collector_state c LEFT JOIN source_runtime r "
                "ON r.source_id = c.source_id WHERE c.source_id = ?",
                (source_id,),
            ).fetchone()
            if row is None:
                return None
            state = dict(row)
            polls = int(state.pop("poll_count"))
            successes = int(state.pop("success_count"))
            failures = int(state.pop("failure_count"))
            evidence = self.connection.execute(
                "SELECT COUNT(*) AS observations, "
                "COALESCE(SUM(o.fetched_at >= ?), 0) AS observations_24h, "
                "COALESCE(SUM(EXISTS (SELECT 1 FROM content_documents d "
                "WHERE d.observation_id = o.id AND d.level IN ('full_text', 'document') "
                "AND length(trim(d.body)) > 0)), 0) AS with_full_text "
                "FROM observations o WHERE o.source_id = ? "
                "AND o.fetched_at >= ? AND o.fetched_at <= ?",
                (max(0, current - 86400), source_id, since, current),
            ).fetchone()
            jobs = list(self.connection.execute(
                "SELECT j.status, j.failure_kind, COUNT(*) AS count "
                "FROM content_fetch_jobs j JOIN observations o ON o.id = j.observation_id "
                "WHERE o.source_id = ? AND o.fetched_at >= ? AND o.fetched_at <= ? "
                "GROUP BY j.status, j.failure_kind ORDER BY j.status, j.failure_kind",
                (source_id, since, current),
            ))
        states: dict[str, int] = {}
        error_kinds: dict[str, int] = {}
        for job in jobs:
            status = str(job["status"])
            count = int(job["count"])
            states[status] = states.get(status, 0) + count
            if status in {"dead", "retry"}:
                kind = str(job["failure_kind"] or "unknown")
                error_kinds[kind] = error_kinds.get(kind, 0) + count
        observations = int(evidence["observations"])
        full_text = int(evidence["with_full_text"])
        return {
            "source_id": source_id,
            "as_of": current,
            "current": state,
            "polling": {
                "basis": "cumulative_persisted_counters",
                "attempts": polls,
                "successes": successes,
                "failures": failures,
                "success_rate": successes / polls if polls else None,
                "window_success_rate": None,
            },
            "evidence": {
                "basis": "retained_observations_by_ingestion_time",
                "since": since,
                "until": current,
                "observations_24h": int(evidence["observations_24h"]),
                "observations_7d": observations,
                "with_full_text": full_text,
                "full_text_coverage": full_text / observations if observations else None,
                "content_jobs": states,
                "content_failure_kinds": error_kinds,
            },
        }

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
            if state is None or automatic.weight != previous_weight:
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

    # Event persistence -------------------------------------------------
    # These methods intentionally expose domain models rather than sqlite rows;
    # clustering and presentation code can therefore move to another database.
    def save_event(self, event: PersistedEvent) -> PersistedEvent:
        with self.unit_of_work():
            return self._save_event(event)

    def _save_event(self, event: PersistedEvent) -> PersistedEvent:
        if not isinstance(event, PersistedEvent):
            raise TypeError("event must be a PersistedEvent")
        self.connection.execute(
            """
            INSERT INTO events(
                event_key, fingerprint, title, summary, score, importance, urgency,
                relevance, confidence, first_seen_at, last_seen_at, regions_json,
                topics_json, status, independent_source_count, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_key) DO UPDATE SET
                fingerprint=excluded.fingerprint, title=excluded.title,
                summary=excluded.summary, score=excluded.score,
                importance=excluded.importance, urgency=excluded.urgency,
                relevance=excluded.relevance, confidence=excluded.confidence,
                first_seen_at=MIN(events.first_seen_at, excluded.first_seen_at),
                last_seen_at=MAX(events.last_seen_at, excluded.last_seen_at),
                regions_json=excluded.regions_json, topics_json=excluded.topics_json,
                status=excluded.status, independent_source_count=excluded.independent_source_count,
                updated_at=excluded.updated_at
            """,
            (
                event.event_key, event.fingerprint, event.title, event.summary, event.score,
                event.importance, event.urgency, event.relevance, event.confidence,
                event.first_seen_at, event.last_seen_at,
                json.dumps(event.regions, ensure_ascii=False),
                json.dumps(event.topics, ensure_ascii=False), event.status,
                event.independent_source_count, event.created_at, event.updated_at,
            ),
        )
        result = self.get_event(event.event_key)
        assert result is not None
        return result

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> PersistedEvent:
        try:
            return PersistedEvent(
                event_key=str(row["event_key"]), fingerprint=str(row["fingerprint"]),
                title=str(row["title"]), summary=str(row["summary"]), score=float(row["score"]),
                importance=int(row["importance"]), urgency=int(row["urgency"]),
                relevance=int(row["relevance"]), confidence=float(row["confidence"]),
                first_seen_at=int(row["first_seen_at"]), last_seen_at=int(row["last_seen_at"]),
                regions=tuple(json.loads(row["regions_json"])),
                topics=tuple(json.loads(row["topics_json"])), status=str(row["status"]),
                independent_source_count=int(row["independent_source_count"]),
                created_at=int(row["created_at"]), updated_at=int(row["updated_at"]),
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"event row {row['id']} is corrupt") from exc

    def get_event(self, event_key: str) -> PersistedEvent | None:
        if not event_key or len(event_key) > 160:
            raise ValueError("event key is invalid")
        row = self.connection.execute("SELECT * FROM events WHERE event_key = ?", (event_key,)).fetchone()
        return self._event_from_row(row) if row is not None else None

    def list_events(
        self, *, since: int | None = None, until: int | None = None,
        status: str | None = None, limit: int = 100
    ) -> list[PersistedEvent]:
        if since is not None and since < 0 or until is not None and until < 0:
            raise ValueError("event time range is invalid")
        if since is not None and until is not None and until <= since:
            raise ValueError("event time range is invalid")
        if status is not None and status not in {"active", "quiet", "closed"}:
            raise ValueError("event status is invalid")
        if not 1 <= limit <= 1000:
            raise ValueError("event limit is out of range")
        clauses: list[str] = []
        params: list[Any] = []
        if since is not None:
            clauses.append("last_seen_at >= ?"); params.append(since)
        if until is not None:
            clauses.append("first_seen_at < ?"); params.append(until)
        if status is not None:
            clauses.append("status = ?"); params.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self.connection.execute(
            f"SELECT * FROM events{where} ORDER BY last_seen_at DESC, id DESC LIMIT ?", params
        )
        return [self._event_from_row(row) for row in rows]

    def list_unassigned_event_observations(
        self, *, since: int | None = None, until: int | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Return a bounded newest-first batch for the background event projector."""
        if not 1 <= limit <= 500:
            raise ValueError("event projection limit is out of range")
        if since is not None and since < 0 or until is not None and until < 0:
            raise ValueError("event projection time range is invalid")
        if since is not None and until is not None and until <= since:
            raise ValueError("event projection time range is invalid")
        clauses = ["er.id IS NULL", "o.handling IN ('digest', 'immediate')"]
        params: list[Any] = []
        if since is not None:
            clauses.append("o.published_at >= ?")
            params.append(since)
        if until is not None:
            clauses.append("o.published_at < ?")
            params.append(until)
        params.append(limit)
        rows = self.connection.execute(
            f"""
            SELECT o.id, o.source_id, o.publisher, o.external_id, o.published_at,
                   o.title, o.summary, o.url, o.attributes_json, o.importance,
                   o.urgency, o.relevance, o.confidence, o.region, o.topic,
                   o.source_tier, o.handling
            FROM observations AS o
            LEFT JOIN event_reports AS er ON er.observation_id = o.id
            WHERE {' AND '.join(clauses)} AND trim(COALESCE(o.title, '')) <> ''
            ORDER BY o.id DESC
            LIMIT ?
            """,
            params,
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["attributes"] = json.loads(item.pop("attributes_json"))
            except (TypeError, ValueError, json.JSONDecodeError):
                item["attributes"] = {}
            result.append(item)
        return result

    def list_event_candidates(
        self, since: int, until: int, *, limit: int = 200
    ) -> list[PersistedEvent]:
        if since < 0 or until <= since:
            raise ValueError("event candidate time range is invalid")
        if not 1 <= limit <= 1000:
            raise ValueError("event candidate limit is out of range")
        rows = self.connection.execute(
            "SELECT * FROM events WHERE last_seen_at >= ? AND first_seen_at < ? "
            "AND id NOT IN (SELECT event_id FROM event_aliases) "
            "ORDER BY last_seen_at DESC, id DESC LIMIT ?",
            (since, until, limit),
        )
        return [self._event_from_row(row) for row in rows]

    def maintain_event_lifecycle(self, *, now: int, limit: int = 500) -> int:
        return SQLiteEventWorkspace(self).maintain_event_lifecycle(now=now, limit=limit)

    def get_event_digest_choices(self, event_keys: Sequence[str]) -> dict[str, str]:
        return SQLiteEventWorkspace(self).get_event_digest_choices(event_keys)

    def event_workspace_state(self, event_keys: Sequence[str], actor: str) -> dict[str, Any]:
        return SQLiteEventWorkspace(self).event_workspace_state(event_keys, actor)

    def set_event_preference(self, event_key: str, actor: str, *, read: bool,
                             followed: bool, ignored: bool, now: int) -> None:
        SQLiteEventWorkspace(self).set_event_preference(event_key, actor, read=read, followed=followed, ignored=ignored, now=now)

    def set_event_digest_choice(self, event_key: str, choice: str, *, actor: str,
                                reason: str, now: int) -> None:
        SQLiteEventWorkspace(self).set_event_digest_choice(event_key, choice, actor=actor, reason=reason, now=now)

    def preview_event_repair(self, action: str, event_keys: Sequence[str], *,
                             observation_ids: Sequence[int] = ()) -> dict[str, Any]:
        with self.unit_of_work(immediate=False):
            return SQLiteEventWorkspace(self).preview_event_repair(action, event_keys, observation_ids=observation_ids)

    def apply_event_repair(self, action: str, event_keys: Sequence[str], *,
                           observation_ids: Sequence[int], expected_revision: str,
                           actor: str, reason: str, now: int) -> str:
        return SQLiteEventWorkspace(self).apply_event_repair(action, event_keys, observation_ids=observation_ids,
                                                            expected_revision=expected_revision, actor=actor, reason=reason, now=now)

    def list_event_audit(self, event_key: str, *, limit: int = 50) -> list[dict[str, Any]]:
        return SQLiteEventWorkspace(self).list_event_audit(event_key, limit=limit)

    def canonical_event_key(self, event_key: str) -> str:
        row = self.connection.execute(
            "SELECT target.event_key FROM event_aliases a JOIN events old ON old.id=a.event_id "
            "JOIN events target ON target.id=a.target_id WHERE old.event_key=?", (event_key,),
        ).fetchone()
        return str(row[0]) if row is not None else event_key

    @staticmethod
    def _event_cursor(payload: Mapping[str, Any]) -> str:
        raw = json.dumps(dict(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_event_cursor(cursor: str, sort: str) -> dict[str, Any]:
        if not cursor or len(cursor) > 512:
            raise ValueError("event cursor is invalid")
        try:
            raw = base64.b64decode(
                cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True
            )
            value = json.loads(raw)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("event cursor is invalid") from exc
        if not isinstance(value, dict) or value.get("sort") != sort:
            raise ValueError("event cursor does not match sort order")
        expected = {"sort", "since", "until", "report_max", "alert_max", "last", "id", "audit_max"}
        if sort == "importance":
            expected |= {"importance", "score"}
        integer_keys = expected - {"sort", "score"}
        if set(value) != expected or any(
            type(value[key]) is not int or not 0 <= value[key] <= 2**63 - 1
            for key in integer_keys
        ):
            raise ValueError("event cursor is invalid")
        if value["until"] <= value["since"] or value["id"] < 1:
            raise ValueError("event cursor is invalid")
        if sort == "importance" and (
            not 1 <= value["importance"] <= 5
            or isinstance(value["score"], bool)
            or not isinstance(value["score"], (int, float))
            or not 0 <= value["score"] <= 5
            or not math.isfinite(value["score"])
        ):
            raise ValueError("event cursor is invalid")
        return value

    def list_event_page(
        self,
        *,
        since: int,
        until: int,
        sort: str = "latest",
        limit: int = 30,
        cursor: str | None = None,
        reports_per_event: int = 20,
    ) -> EventPage:
        """Read a consistent page with frozen time bounds and evidence membership.

        Existing observation ratings remain live across separate page requests;
        this cursor is not a versioned snapshot of later reanalysis.
        """
        if sort not in {"latest", "importance"}:
            raise ValueError("event sort is invalid")
        if not 1 <= limit <= 1000 or not 1 <= reports_per_event <= 100:
            raise ValueError("event page limit is out of range")
        anchor = self._decode_event_cursor(cursor, sort) if cursor is not None else None
        if anchor is not None:
            since, until = anchor["since"], anchor["until"]
        if (
            type(since) is not int or type(until) is not int
            or not 0 <= since < until <= 2**63 - 1
        ):
            raise ValueError("event page time range is invalid")
        self.connection.create_function(
            "argus_event_window_score", 2, event_aggregate_score, deterministic=True
        )
        self.connection.create_function(
            "argus_event_evidence_score", 4, event_evidence_score, deterministic=True
        )
        window_sql = """
            WITH window_reports AS (
                SELECT er.*, o.publisher, o.importance, o.urgency, o.relevance,
                       o.confidence, o.region, o.topic,
                       CASE WHEN o.handling='immediate' OR EXISTS (
                           SELECT 1 FROM alerts a WHERE a.observation_id=er.observation_id
                             AND a.id <= :alert_max
                             AND a.created_at >= :since AND a.created_at < :until
                             AND a.status != 'cancelled'
                       ) THEN 1 ELSE 0 END AS is_immediate,
                       argus_event_evidence_score(
                           o.importance, o.urgency, o.relevance, o.confidence
                       ) AS report_score
                FROM event_reports er JOIN observations o ON o.id=er.observation_id
                WHERE er.id <= :report_max AND (
                    (er.published_at >= :since AND er.published_at < :until) OR EXISTS (
                       SELECT 1 FROM alerts a WHERE a.observation_id=er.observation_id
                         AND a.id <= :alert_max
                         AND a.created_at >= :since AND a.created_at < :until
                         AND a.status != 'cancelled'
                    )
                )
            ), window_metrics AS (
                SELECT event_id, COUNT(*) AS report_count,
                       MIN(published_at) AS first_seen_at, MAX(published_at) AS last_seen_at,
                       MAX(importance) AS importance, MAX(urgency) AS urgency,
                       MAX(relevance) AS relevance, MAX(confidence) AS confidence,
                       MAX(report_score) AS base_score,
                       COUNT(DISTINCT COALESCE(
                           NULLIF(LOWER(TRIM(publisher)), ''), NULLIF(source_id, '')
                       )) AS independent_source_count,
                       JSON_GROUP_ARRAY(DISTINCT region) AS regions_json,
                       JSON_GROUP_ARRAY(DISTINCT topic) AS topics_json,
                       CASE WHEN MAX(is_immediate)=1 THEN 'immediate' ELSE 'digest' END AS handling
                FROM window_reports GROUP BY event_id
            ), window_representatives AS (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY event_id
                    ORDER BY (source_tier='primary') DESC, report_score DESC,
                             published_at DESC, id DESC
                ) AS representative_position
                FROM window_reports
            ), window_events AS (
                SELECT e.id, e.event_key, e.fingerprint, r.title,
                       CASE WHEN TRIM(r.summary) != '' THEN r.summary ELSE r.title END AS summary,
                       argus_event_window_score(m.base_score, m.independent_source_count) AS score,
                       m.importance, m.urgency, m.relevance, m.confidence,
                       m.first_seen_at, m.last_seen_at, m.regions_json, m.topics_json,
                       e.status, m.independent_source_count, e.created_at, e.updated_at,
                       m.report_count, m.handling
                FROM events e JOIN window_metrics m ON m.event_id=e.id
                JOIN window_representatives r ON r.event_id=e.id AND r.representative_position=1
            )
        """
        clauses: list[str] = []
        params: dict[str, Any] = {"since": since, "until": until, "limit": limit + 1}
        if anchor is not None:
            params.update(anchor)
            if sort == "latest":
                clauses.append("(last_seen_at < :last OR (last_seen_at = :last AND id < :id))")
            else:
                clauses.append(
                    "(importance < :importance OR (importance = :importance AND score < :score) OR "
                    "(importance = :importance AND score = :score AND last_seen_at < :last) OR "
                    "(importance = :importance AND score = :score AND last_seen_at = :last AND id < :id))"
                )
        order = (
            "last_seen_at DESC, id DESC"
            if sort == "latest"
            else "importance DESC, score DESC, last_seen_at DESC, id DESC"
        )
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        transaction = (
            nullcontext() if self.connection.in_transaction else self.unit_of_work(immediate=False)
        )
        with transaction:
            audit_max = int(self.connection.execute("SELECT COALESCE(MAX(id),0) FROM event_review_audit WHERE action IN ('merge','split')").fetchone()[0])
            if anchor is not None and anchor["audit_max"] != audit_max:
                raise ValueError("事件归属已人工调整，请刷新列表后继续翻页")
            if anchor is None:
                high_water = self.connection.execute(
                    "SELECT (SELECT COALESCE(MAX(id), 0) FROM event_reports) AS report_max, "
                    "(SELECT COALESCE(MAX(id), 0) FROM alerts) AS alert_max"
                ).fetchone()
                params.update(
                    report_max=int(high_water["report_max"]),
                    alert_max=int(high_water["alert_max"]),
                    audit_max=audit_max,
                )
            rows = list(self.connection.execute(
                f"{window_sql} SELECT * FROM window_events{where} ORDER BY {order} LIMIT :limit",
                params,
            ))
            has_more = len(rows) > limit
            visible = rows[:limit]
            reports_by_event: dict[int, list[PersistedEventReport]] = {
                int(row["id"]): [] for row in visible
            }
            if visible:
                identifiers = list(reports_by_event)
                placeholders = ",".join(f":event_{index}" for index in range(len(identifiers)))
                report_params = {
                    "since": since, "until": until, "reports_limit": reports_per_event,
                    "report_max": params["report_max"], "alert_max": params["alert_max"],
                }
                report_params.update({
                    f"event_{index}": identifier for index, identifier in enumerate(identifiers)
                })
                report_rows = self.connection.execute(
                    f"""{window_sql}, source_reports AS (
                        SELECT *, ROW_NUMBER() OVER (
                            PARTITION BY event_id, source_id ORDER BY published_at DESC, id DESC
                        ) AS source_position FROM window_reports WHERE event_id IN ({placeholders})
                    ), ranked_reports AS (
                        SELECT *, ROW_NUMBER() OVER (
                            PARTITION BY event_id
                            ORDER BY (source_position=1) DESC, published_at DESC, id DESC
                        ) AS report_position FROM source_reports
                    )
                    SELECT r.id, e.event_key, r.event_id, r.observation_id, r.publisher,
                           r.source_id, r.source_tier, r.relation, r.match_score,
                           (r.source_position=1) AS is_representative,
                           r.published_at, r.title, r.summary, r.url, r.created_at
                    FROM ranked_reports r JOIN events e ON e.id=r.event_id
                    WHERE report_position <= :reports_limit ORDER BY event_id, report_position
                    """,
                    report_params,
                )
                for report_row in report_rows:
                    reports_by_event[int(report_row["event_id"])].append(
                        self._event_report_from_row(report_row)
                    )
            items = tuple(
                EventListItem(
                    event=self._event_snapshot(self._event_from_row(row)),
                    reports=tuple(reports_by_event[int(row["id"])]),
                    report_count=int(row["report_count"]),
                    handling=str(row["handling"]),
                )
                for row in visible
            )
        next_cursor = None
        if has_more and visible:
            last = visible[-1]
            values: dict[str, Any] = {
                "sort": sort, "since": since, "until": until,
                "report_max": params["report_max"], "alert_max": params["alert_max"],
                "audit_max": params["audit_max"],
                "last": int(last["last_seen_at"]), "id": int(last["id"])
            }
            if sort == "importance":
                values.update(importance=int(last["importance"]), score=float(last["score"]))
            next_cursor = self._event_cursor(values)
        return EventPage(items, next_cursor, window_since=since, window_until=until)

    @staticmethod
    def _event_snapshot(event: PersistedEvent) -> PersistedEvent:
        """Normalize the complete window aggregate independently of report limits."""
        return replace(
            event,
            regions=tuple(sorted(event.regions)),
            topics=tuple(sorted(event.topics)),
        )

    def save_event_report(self, report: PersistedEventReport) -> PersistedEventReport:
        with self.unit_of_work():
            return self._save_event_report(report)

    def merge_semantic_event_group(
        self, event_keys: Sequence[str], *, expected_observation_ids: Sequence[int],
        actor: str, now: int, primary_source_ids: Sequence[str] = (),
    ) -> str:
        """Repair an explicitly selected pure decision group in one transaction.

        Original observations, alert history, digest snapshots and old event
        containers survive. Existing claims/timelines require a richer repair
        workflow and are deliberately refused here.
        """
        keys = tuple(dict.fromkeys(event_keys))
        expected = set(expected_observation_ids)
        if not 2 <= len(keys) <= 20 or not 1 <= len(expected) <= 200 or not actor.strip() or len(actor) > 128 or now < 0:
            raise ValueError("invalid semantic event repair request")
        with self.unit_of_work():
            events = [self.get_event(key) for key in keys]
            if any(event is None for event in events):
                raise ValueError("repair event no longer exists")
            reports = [report for key in keys for report in self.list_event_reports(key, limit=201)]
            if (len(reports) > 200 or {report.observation_id for report in reports} != expected
                    or {report.event_key for report in reports} != set(keys)):
                raise ValueError("repair evidence changed; preview again")
            if any(self.list_event_claims(key, limit=1) or self.list_event_timeline(key, limit=1) for key in keys):
                raise ValueError("repair refuses events with existing claims or timeline")
            if any(self.connection.execute(
                "SELECT 1 FROM digest_items di JOIN events e ON e.id=di.event_id WHERE e.event_key=? LIMIT 1", (key,),
            ).fetchone() is not None for key in keys):
                raise ValueError("repair refuses events referenced by historical digests")
            original_tiers = {str(report.observation_id): report.source_tier for report in reports}
            reports = [replace(report, source_tier="primary") if report.source_id in primary_source_ids else report for report in reports]
            identities = [identify_semantic_event(report.title) for report in reports]
            if any(identity is None for identity in identities):
                raise ValueError("repair requires an explicit decision identity on every report")
            rows = [
                {"id": report.observation_id, "title": report.title, "summary": report.summary,
                 "published_at": report.published_at, "source_id": report.source_id,
                 "publisher": report.publisher, "source_tier": report.source_tier,
                 "topic": "general", "region": "GLOBAL"}
                for report in reports
            ]
            if len(cluster_events(rows)) != 1:
                raise ValueError("repair reports do not describe one compatible decision")
            # Prefer an existing official container, then the earliest evidence.
            first = min(reports, key=lambda report: (report.source_tier != "primary", report.published_at, report.observation_id))
            target = self.get_event(first.event_key)
            assert target is not None
            present = [event for event in events if event is not None]
            target = replace(
                target, score=max(event.score for event in present),
                importance=max(event.importance for event in present),
                urgency=max(event.urgency for event in present),
                relevance=max(event.relevance for event in present),
                confidence=max(event.confidence for event in present),
                first_seen_at=min(report.published_at for report in reports),
                last_seen_at=max(report.published_at for report in reports),
                regions=tuple(sorted({region for event in present for region in event.regions})),
                topics=tuple(sorted({topic for event in present for topic in event.topics})),
                status="active", updated_at=now,
            )
            self._save_event(target)
            target_id = self.connection.execute("SELECT id FROM events WHERE event_key=?", (target.event_key,)).fetchone()[0]
            seen: set[str] = set()
            for report in sorted(reports, key=lambda report: (report.published_at, report.observation_id)):
                identity = identify_semantic_event(report.title)
                assert identity is not None
                relation = ("context" if identity.relation_hint == "context" else
                            "updates" if report.source_id in seen else
                            "primary" if report.source_tier == "primary" else "corroborates")
                self.connection.execute(
                    "UPDATE event_reports SET event_id=?, source_tier=?, relation=?, match_score=0.96, is_representative=0 WHERE id=?",
                    (target_id, report.source_tier, relation, report.report_id),
                )
                seen.add(report.source_id)
            self.connection.execute(
                "UPDATE event_reports SET is_representative=1 WHERE id IN ("
                "SELECT id FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY source_id ORDER BY published_at DESC, id DESC) AS position "
                "FROM event_reports WHERE event_id=?) WHERE position=1)", (target_id,),
            )
            self.connection.execute(
                "UPDATE events SET independent_source_count=(SELECT COUNT(DISTINCT COALESCE(NULLIF(LOWER(TRIM(o.publisher)), ''), er.source_id)) "
                "FROM event_reports er JOIN observations o ON o.id=er.observation_id WHERE er.event_id=?) WHERE id=?",
                (target_id, target_id),
            )
            for key in keys:
                if key != target.event_key:
                    self.connection.execute("UPDATE events SET status='closed', updated_at=? WHERE event_key=?", (now, key))
            audit_parts = [{"actor": actor, "event_keys": keys}]
            for offset in range(0, len(reports), 10):
                audit_parts.append({"assignments": {
                    str(report.observation_id): {"event_key": report.event_key,
                        "source_tier": original_tiers[str(report.observation_id)]}
                    for report in reports[offset:offset + 10]
                }})
            for index, part in enumerate(audit_parts):
                self._save_event_timeline(PersistedEventTimelineItem(
                    event_key=target.event_key, occurred_at=now, kind="repair_merge",
                    text=json.dumps({"part": index, **part}, sort_keys=True), confidence=1.0, created_at=now,
                ))
            return target.event_key

    def save_event_projection(
        self, event: PersistedEvent, report: PersistedEventReport,
        *, relation_updates: Sequence[PersistedEventReport] = (),
    ) -> PersistedEventReport:
        if event.event_key != report.event_key:
            raise ValueError("event projection identities do not match")
        if any(update.event_key != event.event_key for update in relation_updates):
            raise ValueError("event relation update identity does not match")
        with self.unit_of_work():
            # Another projector may have assigned this observation while the
            # caller computed a match. Its committed identity wins.
            assigned = self.connection.execute(
                "SELECT er.id, e.event_key, o.publisher, er.* FROM event_reports er "
                "JOIN events e ON e.id=er.event_id JOIN observations o ON o.id=er.observation_id "
                "WHERE er.observation_id=? ORDER BY er.id LIMIT 1", (report.observation_id,),
            ).fetchone()
            if assigned is not None:
                return self._event_report_from_row(assigned)
            if self.canonical_event_key(event.event_key) != event.event_key:
                raise ValueError("event was merged during projection; retry with current candidates")
            self._save_event(event)
            saved = self._save_event_report(report)
            for update in relation_updates:
                if update.report_id is None or update.relation != "corroborates":
                    raise ValueError("event relation update is invalid")
                changed = self.connection.execute(
                    "UPDATE event_reports SET relation='corroborates' "
                    "WHERE id=? AND event_id=(SELECT id FROM events WHERE event_key=?) "
                    "AND source_tier='secondary' AND relation='context'",
                    (update.report_id, event.event_key),
                )
                if changed.rowcount != 1:
                    raise ValueError("event relation changed during projection")
            self.connection.execute(
                "UPDATE events SET independent_source_count=("
                "SELECT COUNT(DISTINCT COALESCE(NULLIF(LOWER(TRIM(o.publisher)), ''), er.source_id)) "
                "FROM event_reports er JOIN observations o ON o.id=er.observation_id "
                "WHERE er.event_id=events.id) WHERE event_key=?", (event.event_key,),
            )
            return saved

    def _save_event_report(self, report: PersistedEventReport) -> PersistedEventReport:
        if not isinstance(report, PersistedEventReport):
            raise TypeError("report must be a PersistedEventReport")
        event_row = self.connection.execute("SELECT id FROM events WHERE event_key = ?", (report.event_key,)).fetchone()
        if event_row is None:
            raise KeyError(f"event does not exist: {report.event_key}")
        observation = self.connection.execute(
            "SELECT source_id FROM observations WHERE id = ?", (report.observation_id,)
        ).fetchone()
        if observation is None:
            raise KeyError(f"observation does not exist: {report.observation_id}")
        if str(observation["source_id"]) != report.source_id:
            raise ValueError("event report source does not match observation")
        assigned = self.connection.execute(
            "SELECT er.id, er.event_id FROM event_reports er WHERE er.observation_id = ? "
            "ORDER BY er.id LIMIT 1",
            (report.observation_id,),
        ).fetchone()
        if assigned is not None and int(assigned["event_id"]) != int(event_row["id"]):
            existing = self.connection.execute(
                "SELECT er.id, e.event_key, o.publisher, er.* FROM event_reports er "
                "JOIN events e ON e.id=er.event_id "
                "JOIN observations o ON o.id=er.observation_id WHERE er.id = ?",
                (int(assigned["id"]),),
            ).fetchone()
            assert existing is not None
            return self._event_report_from_row(existing)
        if report.is_representative:
            self.connection.execute(
                "UPDATE event_reports SET is_representative = 0 "
                "WHERE event_id = ? AND source_id = ? AND observation_id != ?",
                (int(event_row["id"]), report.source_id, report.observation_id),
            )
        self.connection.execute(
            """
            INSERT INTO event_reports(
                event_id, observation_id, source_id, source_tier, relation, match_score,
                is_representative, published_at, title, summary, url, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id, observation_id) DO UPDATE SET
                source_id=excluded.source_id, source_tier=excluded.source_tier,
                relation=excluded.relation, match_score=excluded.match_score,
                is_representative=excluded.is_representative, published_at=excluded.published_at,
                title=excluded.title, summary=excluded.summary, url=excluded.url
            """,
            (
                int(event_row["id"]), report.observation_id, report.source_id,
                report.source_tier, report.relation, report.match_score,
                int(report.is_representative), report.published_at, report.title,
                report.summary, report.url, report.created_at,
            ),
        )
        row = self.connection.execute(
            "SELECT er.id, e.event_key, o.publisher, er.* FROM event_reports er "
            "JOIN events e ON e.id = er.event_id JOIN observations o ON o.id=er.observation_id "
            "WHERE er.event_id = ? AND er.observation_id = ?", (int(event_row["id"]), report.observation_id)
        ).fetchone()
        assert row is not None
        return self._event_report_from_row(row)

    @staticmethod
    def _event_report_from_row(row: sqlite3.Row) -> PersistedEventReport:
        return PersistedEventReport(
            event_key=str(row["event_key"]), observation_id=int(row["observation_id"]),
            source_id=str(row["source_id"]), publisher=str(row["publisher"]),
            source_tier=str(row["source_tier"]),
            relation=str(row["relation"]), match_score=float(row["match_score"]),
            is_representative=bool(row["is_representative"]), published_at=int(row["published_at"]),
            title=str(row["title"]), summary=str(row["summary"]), url=str(row["url"]),
            report_id=int(row["id"]), created_at=int(row["created_at"]),
        )

    def list_event_reports(self, event_key: str, *, limit: int = 500) -> list[PersistedEventReport]:
        event = self.connection.execute("SELECT id FROM events WHERE event_key = ?", (event_key,)).fetchone()
        if event is None:
            return []
        if not 1 <= limit <= 2000:
            raise ValueError("event report limit is out of range")
        rows = self.connection.execute(
            "SELECT er.id, e.event_key, o.publisher, er.* FROM event_reports er "
            "JOIN events e ON e.id = er.event_id JOIN observations o ON o.id=er.observation_id "
            "WHERE er.event_id = ? ORDER BY er.published_at DESC, er.id DESC LIMIT ?",
            (int(event["id"]), limit),
        )
        return [self._event_report_from_row(row) for row in rows]

    def save_event_claim(self, claim: PersistedEventClaim) -> PersistedEventClaim:
        if not isinstance(claim, PersistedEventClaim):
            raise TypeError("claim must be a PersistedEventClaim")
        with self.unit_of_work():
            event = self.connection.execute("SELECT id FROM events WHERE event_key = ?", (claim.event_key,)).fetchone()
            if event is None:
                raise KeyError(f"event does not exist: {claim.event_key}")
            event_id = int(event["id"])
            supersedes_id = None
            if claim.supersedes_claim_key:
                if claim.supersedes_claim_key == claim.claim_key:
                    raise ValueError("claim cannot supersede itself")
                row = self.connection.execute(
                    "SELECT id FROM event_claims WHERE event_id = ? AND claim_key = ?",
                    (event_id, claim.supersedes_claim_key),
                ).fetchone()
                if row is None:
                    raise ValueError("superseded claim does not exist in this event")
                supersedes_id = int(row["id"])
                existing = self.connection.execute(
                    "SELECT id FROM event_claims WHERE event_id = ? AND claim_key = ?",
                    (event_id, claim.claim_key),
                ).fetchone()
                if existing is not None and self.connection.execute(
                    "WITH RECURSIVE ancestors(id, parent_id) AS ("
                    "SELECT id, supersedes_claim_id FROM event_claims WHERE id = ? "
                    "UNION ALL SELECT ec.id, ec.supersedes_claim_id FROM event_claims ec "
                    "JOIN ancestors a ON ec.id = a.parent_id) "
                    "SELECT 1 FROM ancestors WHERE id = ? LIMIT 1",
                    (supersedes_id, int(existing["id"])),
                ).fetchone() is not None:
                    raise ValueError("claim supersedes relationship would form a cycle")
            self.connection.execute(
                """
                INSERT INTO event_claims(
                    event_id, claim_key, text, status, confidence, first_seen_at,
                    last_seen_at, supersedes_claim_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id, claim_key) DO UPDATE SET
                    text=excluded.text, status=excluded.status, confidence=excluded.confidence,
                    first_seen_at=excluded.first_seen_at, last_seen_at=excluded.last_seen_at,
                    supersedes_claim_id=excluded.supersedes_claim_id, updated_at=excluded.updated_at
                """,
                (event_id, claim.claim_key, claim.text, claim.status, claim.confidence,
                 claim.first_seen_at, claim.last_seen_at, supersedes_id, claim.created_at, claim.updated_at),
            )
            if supersedes_id is not None and claim.status == "active":
                self.connection.execute(
                    "UPDATE event_claims SET status = 'superseded', updated_at = ? WHERE id = ?",
                    (claim.updated_at, supersedes_id),
                )
        row = self.connection.execute(
            "SELECT ec.*, e.event_key, parent.claim_key AS supersedes_claim_key FROM event_claims ec "
            "JOIN events e ON e.id=ec.event_id LEFT JOIN event_claims parent ON parent.id=ec.supersedes_claim_id "
            "WHERE ec.event_id = ? AND ec.claim_key = ?", (event_id, claim.claim_key)
        ).fetchone()
        assert row is not None
        return self._event_claim_from_row(row)

    @staticmethod
    def _event_claim_from_row(row: sqlite3.Row) -> PersistedEventClaim:
        return PersistedEventClaim(
            event_key=str(row["event_key"]), claim_key=str(row["claim_key"]), text=str(row["text"]),
            status=str(row["status"]), confidence=float(row["confidence"]),
            first_seen_at=int(row["first_seen_at"]), last_seen_at=int(row["last_seen_at"]),
            supersedes_claim_key=(str(row["supersedes_claim_key"]) if row["supersedes_claim_key"] is not None else None),
            claim_id=int(row["id"]), created_at=int(row["created_at"]), updated_at=int(row["updated_at"]),
        )

    def list_event_claims(self, event_key: str, *, limit: int = 500) -> list[PersistedEventClaim]:
        if not 1 <= limit <= 2000:
            raise ValueError("event claim limit is out of range")
        rows = self.connection.execute(
            "SELECT ec.*, e.event_key, parent.claim_key AS supersedes_claim_key FROM event_claims ec "
            "JOIN events e ON e.id=ec.event_id LEFT JOIN event_claims parent ON parent.id=ec.supersedes_claim_id "
            "WHERE e.event_key = ? ORDER BY ec.updated_at DESC, ec.id DESC LIMIT ?", (event_key, limit)
        )
        return [self._event_claim_from_row(row) for row in rows]

    def save_claim_evidence(self, evidence: PersistedEventClaimEvidence) -> PersistedEventClaimEvidence:
        if not isinstance(evidence, PersistedEventClaimEvidence):
            raise TypeError("evidence must be PersistedEventClaimEvidence")
        with self.unit_of_work():
            report = self.connection.execute(
                "SELECT event_id FROM event_reports WHERE id = ?", (evidence.report_id,)
            ).fetchone()
            if report is None:
                raise KeyError(f"report does not exist: {evidence.report_id}")
            claim = self.connection.execute(
                "SELECT id FROM event_claims WHERE event_id = ? AND claim_key = ?",
                (int(report["event_id"]), evidence.claim_key),
            ).fetchone()
            if claim is None:
                raise ValueError("claim and evidence report must belong to the same event")
            self.connection.execute(
                "INSERT INTO event_claim_evidence(claim_id, report_id, stance, note, created_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(claim_id, report_id, stance) DO UPDATE SET note=excluded.note",
                (int(claim["id"]), evidence.report_id, evidence.stance, evidence.note, evidence.created_at),
            )
        row = self.connection.execute(
            "SELECT ece.id, claim_key, report_id, stance, note, ece.created_at FROM event_claim_evidence ece "
            "JOIN event_claims ec ON ec.id=ece.claim_id WHERE ece.claim_id = ? AND report_id = ? AND stance = ?",
            (int(claim["id"]), evidence.report_id, evidence.stance),
        ).fetchone()
        assert row is not None
        return PersistedEventClaimEvidence(claim_key=str(row["claim_key"]), report_id=int(row["report_id"]),
                                  stance=str(row["stance"]), note=str(row["note"]),
                                  created_at=int(row["created_at"]), evidence_id=int(row["id"]))

    def list_claim_evidence(self, claim_key: str, *, limit: int = 500, event_key: str | None = None) -> list[PersistedEventClaimEvidence]:
        if not 1 <= limit <= 2000:
            raise ValueError("claim evidence limit is out of range")
        if event_key is None:
            matches = self.connection.execute(
                "SELECT id FROM event_claims WHERE claim_key = ? LIMIT 2", (claim_key,)
            ).fetchall()
            if len(matches) > 1:
                raise ValueError("claim key is ambiguous; provide event_key")
            claim_id = int(matches[0]["id"]) if matches else None
        else:
            row = self.connection.execute(
                "SELECT ec.id FROM event_claims ec JOIN events e ON e.id = ec.event_id "
                "WHERE ec.claim_key = ? AND e.event_key = ?", (claim_key, event_key),
            ).fetchone()
            claim_id = int(row["id"]) if row is not None else None
        if claim_id is None:
            return []
        rows = self.connection.execute(
            "SELECT ece.id, ec.claim_key, ece.report_id, ece.stance, ece.note, ece.created_at "
            "FROM event_claim_evidence ece JOIN event_claims ec ON ec.id=ece.claim_id "
            "WHERE ec.id = ? ORDER BY ece.created_at DESC, ece.id DESC LIMIT ?", (claim_id, limit)
        )
        return [PersistedEventClaimEvidence(claim_key=str(row["claim_key"]), report_id=int(row["report_id"]),
                                   stance=str(row["stance"]), note=str(row["note"]),
                                   created_at=int(row["created_at"]), evidence_id=int(row["id"])) for row in rows]

    def save_event_timeline(self, item: PersistedEventTimelineItem) -> PersistedEventTimelineItem:
        if not isinstance(item, PersistedEventTimelineItem):
            raise TypeError("item must be a PersistedEventTimelineItem")
        with self.unit_of_work():
            return self._save_event_timeline(item)

    def _save_event_timeline(self, item: PersistedEventTimelineItem) -> PersistedEventTimelineItem:
        event = self.connection.execute("SELECT id FROM events WHERE event_key = ?", (item.event_key,)).fetchone()
        if event is None:
            raise KeyError(f"event does not exist: {item.event_key}")
        if item.report_id is not None:
            report = self.connection.execute(
                "SELECT event_id FROM event_reports WHERE id = ?", (item.report_id,)
            ).fetchone()
            if report is None:
                raise KeyError(f"report does not exist: {item.report_id}")
            if int(report["event_id"]) != int(event["id"]):
                raise ValueError("timeline report must belong to the same event")
        cursor = self.connection.execute(
            "INSERT INTO event_timeline(event_id, occurred_at, kind, text, confidence, report_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (int(event["id"]), item.occurred_at, item.kind, item.text, item.confidence, item.report_id, item.created_at),
        )
        return PersistedEventTimelineItem(event_key=item.event_key, occurred_at=item.occurred_at, kind=item.kind,
                                 text=item.text, confidence=item.confidence, report_id=item.report_id,
                                 timeline_id=int(cursor.lastrowid), created_at=item.created_at)

    def list_event_timeline(self, event_key: str, *, limit: int = 500) -> list[PersistedEventTimelineItem]:
        if not 1 <= limit <= 2000:
            raise ValueError("event timeline limit is out of range")
        rows = self.connection.execute(
            "SELECT et.id, e.event_key, et.occurred_at, et.kind, et.text, et.confidence, et.report_id, et.created_at "
            "FROM event_timeline et JOIN events e ON e.id=et.event_id WHERE e.event_key = ? "
            "ORDER BY et.occurred_at ASC, et.id ASC LIMIT ?", (event_key, limit)
        )
        return [PersistedEventTimelineItem(event_key=str(row["event_key"]), occurred_at=int(row["occurred_at"]),
                                  kind=str(row["kind"]), text=str(row["text"]), confidence=float(row["confidence"]),
                                  report_id=(int(row["report_id"]) if row["report_id"] is not None else None),
                                  timeline_id=int(row["id"]), created_at=int(row["created_at"])) for row in rows]

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
                event_row = None
                if item.event_id:
                    event_row = self.connection.execute(
                        "SELECT id FROM events WHERE event_key = ?", (item.event_id,)
                    ).fetchone()
                frozen_reports = None
                if event_row is not None:
                    if item.reports and all(isinstance(report, PersistedEventReport) for report in item.reports):
                        reports = item.reports
                    else:
                        rows = self.connection.execute(
                            "SELECT er.id, e.event_key, o.publisher, er.* FROM event_reports er "
                            "JOIN events e ON e.id=er.event_id JOIN observations o ON o.id=er.observation_id "
                            "WHERE er.event_id=? AND er.observation_id IN "
                            "(SELECT value FROM json_each(?)) ORDER BY er.published_at DESC, er.id DESC",
                            (int(event_row["id"]), json.dumps(item.observation_ids)),
                        )
                        reports = tuple(self._event_report_from_row(row) for row in rows)
                    frozen_reports = json.dumps([asdict(report) for report in reports], ensure_ascii=False)
                self.connection.execute(
                    """
                    INSERT INTO digest_items(
                        digest_id, position, cluster_key, title, summary, score,
                        importance, urgency, relevance, confidence, published_at,
                        regions_json, topics_json, source_ids_json,
                        observation_ids_json, links_json, handling, source_tiers_json,
                        event_id, event_reports_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        json.dumps(item.source_tiers, ensure_ascii=False),
                        (int(event_row["id"]) if event_row is not None else None),
                        frozen_reports,
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

    def enqueue_digest_failure_notification(
        self,
        *,
        digest_key: str,
        streak: int,
        error: str,
        topic: str,
        click_url: str,
        now: int,
    ) -> bool:
        """Queue the operator alert emitted after three consecutive AI failures."""
        if not digest_key or not 1 <= streak <= 10000 or not topic or now < 0:
            raise ValueError("digest failure notification request is invalid")
        candidate = AlertCandidate(
            rule_id="digest.ai_failure",
            dedupe_key=f"digest:ai-failure:{digest_key}:{streak}",
            title="日报 AI 连续生成失败",
            message=(
                f"日报 {digest_key} 已连续 {streak} 天无法生成 AI 版本，已保留算法版。"
                f"请检查模型服务后处理。最近错误：{error[:500]}"
            ),
            priority=5,
            tags=("digest", "ai", "incident"),
            click_url=click_url,
            topic=topic,
            confidence=1.0,
            evidence=(f"digest {digest_key}", f"consecutive_failures={streak}"),
        )
        with self.connection:
            return self._insert_alert(candidate, None, now)

    def start_digest_retry(
        self, digest_key: str, fallback_version: int, now: int, retry_deadline_at: int,
        *, last_error: str | None = None,
    ) -> DigestRetryState:
        if not digest_key or fallback_version < 1 or now < 0 or retry_deadline_at < now:
            raise ValueError("digest retry start request is invalid")
        next_attempt_at = min(retry_deadline_at, now + DIGEST_RETRY_INTERVAL_SECONDS)
        with self.unit_of_work():
            self.connection.execute(
                """
                INSERT INTO digest_retry_state(
                    digest_key, fallback_version, attempts, next_attempt_at,
                    retry_deadline_at, status, last_error, started_at, updated_at
                ) VALUES (?, ?, 1, ?, ?, 'pending', ?, ?, ?)
                ON CONFLICT(digest_key) DO NOTHING
                """,
                (digest_key, fallback_version, next_attempt_at, retry_deadline_at,
                 sanitize_error(last_error)[:1000] if last_error else None, now, now),
            )
        state = self.get_digest_retry(digest_key)
        assert state is not None
        return state

    def get_digest_retry(self, digest_key: str) -> DigestRetryState | None:
        row = self.connection.execute(
            "SELECT * FROM digest_retry_state WHERE digest_key = ?", (digest_key,)
        ).fetchone()
        if row is None:
            return None
        return DigestRetryState(
            digest_key=str(row["digest_key"]),
            fallback_version=int(row["fallback_version"]),
            attempts=int(row["attempts"]),
            next_attempt_at=(int(row["next_attempt_at"]) if row["next_attempt_at"] is not None else None),
            retry_deadline_at=int(row["retry_deadline_at"]),
            status=str(row["status"]),
            last_error=(str(row["last_error"]) if row["last_error"] is not None else None),
        )

    def list_expired_digest_retries(self, now: int) -> list[DigestRetryState]:
        if now < 0:
            raise ValueError("digest retry expiry timestamp is invalid")
        rows = self.connection.execute(
            "SELECT digest_key FROM digest_retry_state "
            "WHERE status = 'pending' AND retry_deadline_at < ? "
            "ORDER BY retry_deadline_at, digest_key LIMIT 30",
            (now,),
        )
        return [state for row in rows
                if (state := self.get_digest_retry(str(row["digest_key"]))) is not None]

    def record_digest_retry(
        self,
        digest_key: str,
        *,
        attempts: int,
        status: str,
        next_attempt_at: int | None,
        last_error: str | None,
        now: int,
    ) -> DigestRetryState:
        if status not in {"pending", "succeeded", "failed"} or not 1 <= attempts <= 5 or now < 0:
            raise ValueError("digest retry update is invalid")
        if status == "pending" and next_attempt_at is None:
            raise ValueError("pending digest retry requires next attempt")
        with self.unit_of_work():
            updated = self.connection.execute(
                """
                UPDATE digest_retry_state
                SET attempts = ?, status = ?, next_attempt_at = ?, last_error = ?, updated_at = ?
                WHERE digest_key = ?
                """,
                (attempts, status, next_attempt_at, (last_error[:1000] if last_error else None), now, digest_key),
            ).rowcount
            if not updated:
                raise KeyError(f"digest retry state does not exist: {digest_key}")
        state = self.get_digest_retry(digest_key)
        assert state is not None
        return state

    @staticmethod
    def _digest_date(digest_key: str) -> date | None:
        if not digest_key.startswith("daily:"):
            return None
        try:
            return datetime.strptime(digest_key[6:], "%Y-%m-%d").date()
        except ValueError:
            return None

    def record_digest_failure(self, digest_key: str, *, now: int) -> tuple[int, bool]:
        """Record one fully failed day and return (streak, threshold_crossed)."""
        if not digest_key or now < 0:
            raise ValueError("digest failure record is invalid")
        with self.unit_of_work():
            row = self.connection.execute(
                "SELECT consecutive_failures, last_failed_digest_key FROM digest_failure_state WHERE id = 1"
            ).fetchone()
            previous = int(row["consecutive_failures"]) if row is not None else 0
            previous_key = str(row["last_failed_digest_key"]) if row and row["last_failed_digest_key"] else None
            current_date = self._digest_date(digest_key)
            previous_date = self._digest_date(previous_key or "")
            if previous_key == digest_key:
                streak = previous
            elif current_date is not None and previous_date is not None and current_date.toordinal() == previous_date.toordinal() + 1:
                streak = previous + 1
            else:
                streak = 1
            self.connection.execute(
                """
                INSERT INTO digest_failure_state(id, consecutive_failures, last_failed_digest_key, updated_at)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    consecutive_failures = excluded.consecutive_failures,
                    last_failed_digest_key = excluded.last_failed_digest_key,
                    updated_at = excluded.updated_at
                """,
                (streak, digest_key, now),
            )
        return streak, streak == 3 and previous_key != digest_key

    def record_digest_success(self, *, now: int) -> None:
        if now < 0:
            raise ValueError("digest success timestamp is invalid")
        with self.unit_of_work():
            self.connection.execute(
                """
                INSERT INTO digest_failure_state(id, consecutive_failures, last_failed_digest_key, updated_at)
                VALUES (1, 0, NULL, ?)
                ON CONFLICT(id) DO UPDATE SET
                    consecutive_failures = 0,
                    last_failed_digest_key = NULL,
                    updated_at = excluded.updated_at
                """,
                (now,),
            )

    def get_digest_failure_state(self) -> tuple[int, str | None]:
        row = self.connection.execute(
            "SELECT consecutive_failures, last_failed_digest_key FROM digest_failure_state WHERE id = 1"
        ).fetchone()
        if row is None:
            return 0, None
        return int(row["consecutive_failures"]), (
            str(row["last_failed_digest_key"])
            if row["last_failed_digest_key"] else None
        )

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
                event_key = None
                event_reports: tuple[PersistedEventReport, ...] = ()
                if item["event_id"] is not None:
                    event_row = self.connection.execute(
                        "SELECT event_key FROM events WHERE id = ?", (int(item["event_id"]),)
                    ).fetchone()
                    if event_row is not None:
                        event_key = str(event_row["event_key"])
                        # The event continues to grow, but a published digest
                        # only references its immutable observation selection.
                        report_rows = self.connection.execute(
                            "SELECT er.id, e.event_key, o.publisher, er.* FROM event_reports er "
                            "JOIN events e ON e.id=er.event_id JOIN observations o ON o.id=er.observation_id "
                            "WHERE er.event_id=? AND er.observation_id IN "
                            "(SELECT value FROM json_each(?)) ORDER BY er.published_at DESC, er.id DESC",
                            (int(item["event_id"]), str(item["observation_ids_json"])),
                        )
                        frozen_reports = (
                            [PersistedEventReport(**report) for report in json.loads(item["event_reports_json"])]
                            if item["event_reports_json"] is not None
                            else [self._event_report_from_row(report) for report in report_rows]
                        )
                        representative_sources: set[str] = set()
                        reconstructed = []
                        for report in frozen_reports:
                            reconstructed.append(replace(
                                report, is_representative=report.source_id not in representative_sources,
                            ))
                            representative_sources.add(report.source_id)
                        event_reports = tuple(reconstructed)
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
                        source_tiers=tuple(json.loads(item["source_tiers_json"])),
                        handling=str(item["handling"]),
                        event_id=event_key,
                        reports=event_reports,
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
            "# TYPE argus_digest_ai_retries gauge",
            f"argus_digest_ai_retries{{status=\"pending\"}} {status['digest_ai']['retries'].get('pending', 0)}",
            f"argus_digest_ai_retries{{status=\"failed\"}} {status['digest_ai']['retries'].get('failed', 0)}",
            f"argus_digest_ai_failure_streak {status['digest_ai']['consecutive_failure_days']}",
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
                self.connection.execute(
                    "DELETE FROM digest_generation_attempts WHERE status!='running' AND finished_at < ?",
                    (audit_cutoff,),
                )
                self.connection.execute("DELETE FROM config_audit WHERE created_at < ?", (audit_cutoff,))
                self.connection.execute("DELETE FROM reminder_audit WHERE created_at < ?", (audit_cutoff,))
                self.connection.execute(
                    "DELETE FROM admin_jobs WHERE completed_at IS NOT NULL AND completed_at < ?",
                    (audit_cutoff,),
                )
                self.connection.execute(
                    "DELETE FROM analysis_api_usage WHERE updated_at < ?", (audit_cutoff,)
                )
                self.connection.execute(
                    "DELETE FROM digest_api_usage WHERE updated_at < ?", (audit_cutoff,)
                )
                self.connection.execute(
                    "DELETE FROM admin_auth_audit WHERE created_at < ?", (audit_cutoff,)
                )
                self.connection.execute(
                    "DELETE FROM admin_login_limits WHERE updated_at < ?", (audit_cutoff,)
                )
                self.connection.execute(
                    "DELETE FROM admin_sessions WHERE expires_at < ? OR "
                    "(revoked_at IS NOT NULL AND revoked_at < ?)",
                    (audit_cutoff, audit_cutoff),
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
        content_documents = {
            str(row["level"]): int(row["count"])
            for row in self.connection.execute(
                "SELECT level, COUNT(*) AS count FROM content_documents GROUP BY level"
            )
        }
        content_fetch = {
            str(row["status"]): int(row["count"])
            for row in self.connection.execute(
                "SELECT status, COUNT(*) AS count FROM content_fetch_jobs GROUP BY status"
            )
        }
        digest_retries = {
            str(row["status"]): int(row["count"])
            for row in self.connection.execute(
                "SELECT status, COUNT(*) AS count FROM digest_retry_state GROUP BY status"
            )
        }
        failure_row = self.connection.execute(
            "SELECT consecutive_failures, last_failed_digest_key "
            "FROM digest_failure_state WHERE id = 1"
        ).fetchone()
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
            "digest_ai": {
                "retries": digest_retries,
                "consecutive_failure_days": int(failure_row["consecutive_failures"])
                if failure_row is not None else 0,
                "last_failed_digest_key": str(failure_row["last_failed_digest_key"])
                if failure_row is not None and failure_row["last_failed_digest_key"] else None,
            },
            "outbox": outbox,
            "outbox_metrics": outbox_metrics,
            "incidents": incidents,
            "reminders": reminders,
            "content": {
                "documents": content_documents,
                "fetch_jobs": content_fetch,
            },
            "config_revision": revisions[0] if revisions else None,
            "desired_revision": revisions[0]["revision"] if revisions else None,
            "engine": engine,
            "runtime": engine,
            "sources": sources,
        }

    @staticmethod
    def _public_admin_user(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: row[key]
            for key in (
                "id", "username", "display_name", "role", "enabled",
                "created_at", "updated_at", "last_login_at",
            )
        }

    def get_admin_user_for_auth(self, username: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM admin_users WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
        return dict(row) if row is not None else None

    def list_admin_users(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT id, username, display_name, role, enabled, created_at, updated_at, "
            "last_login_at, (SELECT COUNT(*) FROM admin_sessions AS s WHERE s.user_id = u.id "
            "AND s.revoked_at IS NULL AND s.expires_at > ?) AS active_sessions "
            "FROM admin_users AS u ORDER BY username COLLATE NOCASE",
            (int(time.time()),),
        )
        return [dict(self._public_admin_user(dict(row)), active_sessions=int(row["active_sessions"])) for row in rows]

    def create_admin_user(
        self,
        username: str,
        display_name: str,
        password_hash: str,
        role: str,
        actor: str,
        now: int,
    ) -> dict[str, Any]:
        if role not in {"admin", "operator", "viewer"} or now < 0:
            raise ValueError("admin user role or timestamp is invalid")
        if not username or len(username) > 32 or not display_name.strip() or len(display_name) > 80:
            raise ValueError("admin user identity is invalid")
        if not password_hash.startswith("$argon2id$") or len(password_hash) > 512:
            raise ValueError("admin user password hash is invalid")
        try:
            with self.unit_of_work():
                cursor = self.connection.execute(
                    "INSERT INTO admin_users(username, display_name, password_hash, role, enabled, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?)",
                    (username, display_name.strip(), password_hash, role, now, now),
                )
                user_id = int(cursor.lastrowid)
                self.connection.execute(
                    "INSERT INTO admin_auth_audit(user_id, action, actor, details_json, created_at) "
                    "VALUES (?, 'user_created', ?, ?, ?)",
                    (user_id, actor[:128], json.dumps({"role": role}, sort_keys=True), now),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("username already exists") from exc
        row = self.connection.execute("SELECT * FROM admin_users WHERE id = ?", (user_id,)).fetchone()
        assert row is not None
        return self._public_admin_user(dict(row))

    def update_admin_user(
        self,
        user_id: int,
        display_name: str,
        role: str,
        enabled: bool,
        actor: str,
        now: int,
    ) -> dict[str, Any]:
        if user_id < 1 or role not in {"admin", "operator", "viewer"}:
            raise ValueError("admin user update is invalid")
        name = display_name.strip()
        if not name or len(name) > 80:
            raise ValueError("admin user display name is invalid")
        with self.unit_of_work():
            current = self.connection.execute(
                "SELECT role, enabled FROM admin_users WHERE id = ?", (user_id,)
            ).fetchone()
            if current is None:
                raise KeyError("admin user not found")
            removes_admin = current["role"] == "admin" and current["enabled"] and (
                role != "admin" or not enabled
            )
            if removes_admin:
                remaining = int(self.connection.execute(
                    "SELECT COUNT(*) FROM admin_users WHERE role = 'admin' AND enabled = 1 AND id != ?",
                    (user_id,),
                ).fetchone()[0])
                if remaining == 0:
                    raise ValueError("cannot disable or demote the final enabled administrator")
            self.connection.execute(
                "UPDATE admin_users SET display_name = ?, role = ?, enabled = ?, "
                "session_version = session_version + 1, updated_at = ? WHERE id = ?",
                (name, role, int(enabled), now, user_id),
            )
            self.connection.execute(
                "UPDATE admin_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                (now, user_id),
            )
            self.connection.execute(
                "INSERT INTO admin_auth_audit(user_id, action, actor, details_json, created_at) "
                "VALUES (?, 'user_updated', ?, ?, ?)",
                (user_id, actor[:128], json.dumps({"role": role, "enabled": bool(enabled)}, sort_keys=True), now),
            )
        row = self.connection.execute("SELECT * FROM admin_users WHERE id = ?", (user_id,)).fetchone()
        assert row is not None
        return self._public_admin_user(dict(row))

    def set_admin_user_password(
        self, user_id: int, password_hash: str, actor: str, now: int
    ) -> bool:
        if user_id < 1 or not password_hash.startswith("$argon2id$"):
            raise ValueError("admin password update is invalid")
        with self.unit_of_work():
            updated = self.connection.execute(
                "UPDATE admin_users SET password_hash = ?, session_version = session_version + 1, "
                "updated_at = ? WHERE id = ?",
                (password_hash, now, user_id),
            )
            if updated.rowcount != 1:
                return False
            self.connection.execute(
                "UPDATE admin_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                (now, user_id),
            )
            self.connection.execute(
                "INSERT INTO admin_auth_audit(user_id, action, actor, created_at) "
                "VALUES (?, 'password_changed', ?, ?)",
                (user_id, actor[:128], now),
            )
        return True

    def revoke_admin_user_sessions(self, user_id: int, actor: str, now: int) -> int:
        with self.unit_of_work():
            if self.connection.execute(
                "SELECT 1 FROM admin_users WHERE id = ?", (user_id,)
            ).fetchone() is None:
                raise KeyError("admin user not found")
            updated = self.connection.execute(
                "UPDATE admin_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                (now, user_id),
            )
            self.connection.execute(
                "INSERT INTO admin_auth_audit(user_id, action, actor, created_at) "
                "VALUES (?, 'sessions_revoked', ?, ?)",
                (user_id, actor[:128], now),
            )
        return int(updated.rowcount)

    def admin_login_blocked_until(self, subject_hash: str, now: int) -> int:
        row = self.connection.execute(
            "SELECT window_started_at, blocked_until FROM admin_login_limits WHERE subject_hash = ?",
            (subject_hash,),
        ).fetchone()
        if row is None or now - int(row["window_started_at"]) > 900:
            return 0
        return int(row["blocked_until"])

    def record_admin_login_attempt(self, subject_hash: str, success: bool, now: int) -> int:
        if len(subject_hash) != 64 or now < 0:
            raise ValueError("admin login attempt is invalid")
        with self.unit_of_work():
            if success:
                self.connection.execute(
                    "DELETE FROM admin_login_limits WHERE subject_hash = ?", (subject_hash,)
                )
                return 0
            row = self.connection.execute(
                "SELECT failures, window_started_at FROM admin_login_limits WHERE subject_hash = ?",
                (subject_hash,),
            ).fetchone()
            failures = int(row["failures"]) + 1 if row is not None and now - int(row["window_started_at"]) <= 900 else 1
            started = int(row["window_started_at"]) if row is not None and now - int(row["window_started_at"]) <= 900 else now
            blocked = now + min(3600, 300 * (2 ** max(0, failures - 5))) if failures >= 5 else 0
            self.connection.execute(
                "INSERT INTO admin_login_limits(subject_hash, failures, window_started_at, blocked_until, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(subject_hash) DO UPDATE SET failures = excluded.failures, "
                "window_started_at = excluded.window_started_at, blocked_until = excluded.blocked_until, updated_at = excluded.updated_at",
                (subject_hash, failures, started, blocked, now),
            )
        return blocked

    def create_admin_session(
        self, session_hash: str, csrf_hash: str, user_id: int, now: int, expires_at: int
    ) -> None:
        with self.unit_of_work():
            row = self.connection.execute(
                "SELECT session_version FROM admin_users WHERE id = ? AND enabled = 1", (user_id,)
            ).fetchone()
            if row is None:
                raise KeyError("admin user is unavailable")
            self.connection.execute(
                "INSERT INTO admin_sessions(session_hash, csrf_hash, user_id, session_version, "
                "created_at, expires_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_hash, csrf_hash, user_id, int(row["session_version"]), now, expires_at, now),
            )
            self.connection.execute(
                "UPDATE admin_users SET last_login_at = ?, updated_at = ? WHERE id = ?",
                (now, now, user_id),
            )
            self.connection.execute(
                "INSERT INTO admin_auth_audit(user_id, action, actor, created_at) "
                "VALUES (?, 'login', ?, ?)", (user_id, "self", now),
            )

    def resolve_admin_session(self, session_hash: str, now: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT s.session_hash, s.csrf_hash, s.user_id, s.last_seen_at, u.username, "
            "u.display_name, u.role FROM admin_sessions AS s JOIN admin_users AS u ON u.id = s.user_id "
            "WHERE s.session_hash = ? AND s.revoked_at IS NULL AND s.expires_at > ? "
            "AND u.enabled = 1 AND s.session_version = u.session_version",
            (session_hash, now),
        ).fetchone()
        if row is None:
            return None
        if now - int(row["last_seen_at"]) >= 300:
            with self.connection:
                self.connection.execute(
                    "UPDATE admin_sessions SET last_seen_at = ? WHERE session_hash = ?",
                    (now, session_hash),
                )
        return dict(row)

    def revoke_admin_session(self, session_hash: str, now: int) -> bool:
        with self.connection:
            updated = self.connection.execute(
                "UPDATE admin_sessions SET revoked_at = ? WHERE session_hash = ? AND revoked_at IS NULL",
                (now, session_hash),
            )
        return updated.rowcount == 1

    def list_admin_auth_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("admin auth audit limit is out of range")
        result = []
        for row in self.connection.execute(
            "SELECT a.id, a.user_id, u.username, a.action, a.actor, a.details_json, "
            "a.created_at FROM admin_auth_audit AS a LEFT JOIN admin_users AS u "
            "ON u.id = a.user_id ORDER BY a.created_at DESC, a.id DESC LIMIT ?",
            (limit,),
        ):
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            result.append(item)
        return result
