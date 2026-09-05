from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping

from .models import (
    AlertCandidate,
    FeedFetchResult,
    IngestReport,
    OutboxMessage,
    SourceState,
)
from .rules import RuleSet
from .reminders import ReminderError, ReminderSpec, next_daily_occurrence
from .util import sanitize_error, to_epoch


SCHEMA_VERSION = 6


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path, timeout=5.0)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self._migrate()

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
                PRAGMA user_version=6;
                COMMIT;
                """
            )
            return
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

    def close(self) -> None:
        self.connection.close()

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
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            for observation in result.observations:
                cursor = self.connection.execute(
                    """
                    INSERT OR IGNORE INTO observations(
                        source_id, publisher, dedupe_scope, external_id,
                        published_at, fetched_at, title, summary, url, attributes_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    last_http_status = NULL
                WHERE source_id = ?
                """,
                (result.etag, result.last_modified, now, now, result.cursor, duration_ms, source_id),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
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
        self.connection.execute("BEGIN IMMEDIATE")
        try:
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
                    last_http_status = ?
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
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
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
                SET status = 'delivered', delivered_at = ?, lease_until = NULL, last_error = NULL
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
                SET status = 'pending', next_attempt_at = ?, lease_until = NULL, last_error = ?
                WHERE id = ? AND status = 'sending'
                """,
                (next_attempt_at, sanitize_error(error), alert_id),
            )

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
    ) -> int:
        now = int(time.time())
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self.connection:
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
        except (TypeError, ValueError):
            result["payload"] = {}
        return result

    def metrics_prometheus(self) -> str:
        status = self.status()
        lines = [
            "# HELP argus_observations_total Stored normalized observations.",
            "# TYPE argus_observations_total gauge",
            f"argus_observations_total {status['observations']}",
        ]
        for state, count in status["outbox"].items():
            lines.append(f'argus_outbox{{status="{state}"}} {count}')
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
        return "\n".join(lines) + "\n"

    def cleanup(self, cutoff: int) -> tuple[int, int]:
        with self.connection:
            deleted_alerts = self.connection.execute(
                "DELETE FROM alerts WHERE status = 'delivered' AND delivered_at < ?", (cutoff,)
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
        return int(deleted_alerts), int(deleted_observations)

    def status(self) -> dict[str, Any]:
        sources = [dict(row) for row in self.connection.execute(
            """
            SELECT source_id, initialized, last_attempt_at, last_success_at,
                   consecutive_failures, outage_alerted, last_error,
                   last_duration_ms, last_error_kind, last_http_status
            FROM collector_state ORDER BY source_id
            """
        )]
        outbox = {
            str(row["status"]): int(row["count"])
            for row in self.connection.execute(
                "SELECT status, COUNT(*) AS count FROM alerts GROUP BY status"
            )
        }
        observations = int(self.connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0])
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
        return {
            "database_schema": SCHEMA_VERSION,
            "observations": observations,
            "outbox": outbox,
            "incidents": incidents,
            "reminders": reminders,
            "config_revision": revisions[0] if revisions else None,
            "sources": sources,
        }
