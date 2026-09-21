"""SQLite adapter for event review, preferences and evidence-preserving repairs."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any, Sequence

from .event_clustering import event_aggregate_score, event_evidence_score, explain_event_match
from .events import EVENT_CLOSED_SECONDS, EVENT_QUIET_SECONDS, EventWorkspaceConflict, event_lifecycle

if TYPE_CHECKING:
    from .database import Database


def migrate_event_workspace(database: Database) -> None:
    """Additive schema 18; keep migration and version bump in one transaction."""
    connection = database.connection
    with database.unit_of_work():
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) >= 18:
            return
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(digest_items)")}
        if "event_reports_json" not in columns:
            connection.execute("ALTER TABLE digest_items ADD COLUMN event_reports_json TEXT")
        statements = (
            "CREATE TABLE IF NOT EXISTS event_preferences (event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE, actor TEXT NOT NULL, is_read INTEGER NOT NULL CHECK(is_read IN (0,1)), followed INTEGER NOT NULL CHECK(followed IN (0,1)), ignored INTEGER NOT NULL CHECK(ignored IN (0,1)), updated_at INTEGER NOT NULL, PRIMARY KEY(event_id, actor))",
            "CREATE TABLE IF NOT EXISTS event_editorial (event_id INTEGER PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE, choice TEXT NOT NULL CHECK(choice IN ('include','exclude')), actor TEXT NOT NULL, reason TEXT NOT NULL, updated_at INTEGER NOT NULL)",
            "CREATE TABLE IF NOT EXISTS event_aliases (event_id INTEGER PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE, target_id INTEGER NOT NULL REFERENCES events(id), CHECK(event_id != target_id))",
            "CREATE TABLE IF NOT EXISTS event_review_audit (id INTEGER PRIMARY KEY, action TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL, details_json TEXT NOT NULL, created_at INTEGER NOT NULL)",
            "CREATE TABLE IF NOT EXISTS event_review_audit_events (audit_id INTEGER NOT NULL REFERENCES event_review_audit(id) ON DELETE CASCADE, event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE, PRIMARY KEY(audit_id,event_id))",
            "CREATE INDEX IF NOT EXISTS event_review_audit_event_idx ON event_review_audit_events(event_id,audit_id DESC)",
            "CREATE INDEX IF NOT EXISTS events_lifecycle_idx ON events(status,last_seen_at,id)",
        )
        for statement in statements:
            connection.execute(statement)
        connection.execute("PRAGMA user_version=18")


class SQLiteEventWorkspace:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.connection = database.connection

    def _id(self, key: str) -> int:
        if not isinstance(key, str) or not key or len(key) > 160:
            raise ValueError("event key is invalid")
        row = self.connection.execute("SELECT id FROM events WHERE event_key=?", (key,)).fetchone()
        if row is None:
            raise ValueError("event no longer exists")
        return int(row[0])

    @staticmethod
    def _metadata(actor: str, reason: str, now: int) -> None:
        if not actor.strip() or len(actor) > 128 or not reason.strip() or len(reason) > 1000 or now < 0:
            raise ValueError("event review metadata is invalid")

    def _audit(self, action: str, actor: str, reason: str, now: int,
               details: dict[str, Any], identifiers: Sequence[int]) -> None:
        cursor = self.connection.execute(
            "INSERT INTO event_review_audit(action,actor,reason,details_json,created_at) VALUES(?,?,?,?,?)",
            (action, actor, reason, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
        )
        self.connection.executemany(
            "INSERT INTO event_review_audit_events(audit_id,event_id) VALUES(?,?)",
            [(cursor.lastrowid, value) for value in sorted(set(identifiers))],
        )

    def maintain_event_lifecycle(self, *, now: int, limit: int = 500) -> int:
        if now < 0 or not 1 <= limit <= 1000:
            raise ValueError("event lifecycle bounds are invalid")
        with self.database.unit_of_work():
            rows = self.connection.execute(
                "SELECT id,last_seen_at FROM events WHERE (status='active' AND last_seen_at<=?) OR "
                "(status='quiet' AND last_seen_at<=?) ORDER BY last_seen_at,id LIMIT ?",
                (now - EVENT_QUIET_SECONDS, now - EVENT_CLOSED_SECONDS, limit),
            ).fetchall()
            self.connection.executemany(
                "UPDATE events SET status=?,updated_at=? WHERE id=?",
                [(event_lifecycle(int(row["last_seen_at"]), now), now, row["id"]) for row in rows],
            )
            return len(rows)

    def get_event_digest_choices(self, event_keys: Sequence[str]) -> dict[str, str]:
        if len(event_keys) > 1000:
            raise ValueError("event selection is too large")
        rows = self.connection.execute(
            "SELECT e.event_key,c.choice FROM event_editorial c JOIN events e ON e.id=c.event_id "
            "WHERE e.event_key IN (SELECT value FROM json_each(?))", (json.dumps(list(event_keys)),),
        )
        return {str(row[0]): str(row[1]) for row in rows}

    def event_workspace_state(self, event_keys: Sequence[str], actor: str) -> dict[str, Any]:
        if len(event_keys) > 1000 or not actor or len(actor) > 128:
            raise ValueError("event preference query is invalid")
        rows = self.connection.execute(
            "SELECT e.event_key,p.is_read,p.followed,p.ignored,c.choice,c.reason FROM events e "
            "LEFT JOIN event_preferences p ON p.event_id=e.id AND p.actor=? "
            "LEFT JOIN event_editorial c ON c.event_id=e.id "
            "WHERE e.event_key IN (SELECT value FROM json_each(?))", (actor, json.dumps(list(event_keys))),
        )
        return {str(row["event_key"]): {
            "read": bool(row["is_read"]), "followed": bool(row["followed"]), "ignored": bool(row["ignored"]),
            "digest_choice": row["choice"] or "auto", "digest_reason": row["reason"] or "",
        } for row in rows}

    def set_event_preference(self, event_key: str, actor: str, *, read: bool,
                             followed: bool, ignored: bool, now: int) -> None:
        self._metadata(actor, "personal preference", now)
        if any(type(value) is not bool for value in (read, followed, ignored)):
            raise ValueError("event preferences must be boolean")
        with self.database.unit_of_work():
            identifier = self._id(event_key)
            self.connection.execute(
                "INSERT INTO event_preferences(event_id,actor,is_read,followed,ignored,updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(event_id,actor) DO UPDATE SET is_read=excluded.is_read,followed=excluded.followed,ignored=excluded.ignored,updated_at=excluded.updated_at",
                (identifier, actor, read, followed, ignored, now),
            )

    def set_event_digest_choice(self, event_key: str, choice: str, *, actor: str,
                                reason: str, now: int) -> None:
        self._metadata(actor, reason, now)
        if choice not in {"auto", "include", "exclude"}:
            raise ValueError("event digest choice is invalid")
        with self.database.unit_of_work():
            identifier = self._id(event_key)
            if choice == "auto":
                self.connection.execute("DELETE FROM event_editorial WHERE event_id=?", (identifier,))
            else:
                self.connection.execute(
                    "INSERT INTO event_editorial(event_id,choice,actor,reason,updated_at) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(event_id) DO UPDATE SET choice=excluded.choice,actor=excluded.actor,reason=excluded.reason,updated_at=excluded.updated_at",
                    (identifier, choice, actor, reason.strip(), now),
                )
            self._audit("digest_choice", actor, reason, now, {"event_key": event_key, "choice": choice}, [identifier])

    def preview_event_repair(self, action: str, event_keys: Sequence[str], *,
                             observation_ids: Sequence[int] = ()) -> dict[str, Any]:
        keys = sorted(set(event_keys))
        selected = sorted(set(observation_ids))
        if (action not in {"merge", "split"} or not 1 <= len(keys) <= 20
                or (action == "merge" and (len(keys) < 2 or selected))
                or (action == "split" and (len(keys) != 1 or not selected))
                or any(type(value) is not int or value < 1 for value in selected)):
            raise ValueError("invalid event repair selection")
        identifiers = [self._id(key) for key in keys]
        if self.connection.execute("SELECT 1 FROM event_aliases WHERE event_id IN (SELECT value FROM json_each(?))", (json.dumps(identifiers),)).fetchone():
            raise ValueError("select canonical events instead of previously merged aliases")
        reports = [report for key in keys for report in self.database.list_event_reports(key, limit=201)]
        if not reports or len(reports) > 200 or {report.event_key for report in reports} != set(keys):
            raise ValueError("repair requires 1 to 200 reports in nonempty events")
        if action == "split" and not set(selected) < {report.observation_id for report in reports}:
            raise ValueError("split must select some, but not all, existing observations")
        if any(self.database.list_event_claims(key, limit=1) for key in keys):
            raise ValueError("events with claims require a claim-aware repair and cannot be moved here")
        if self.connection.execute(
            "SELECT 1 FROM event_timeline WHERE event_id IN (SELECT value FROM json_each(?)) AND report_id IS NOT NULL LIMIT 1", (json.dumps(identifiers),),
        ).fetchone():
            raise ValueError("events with report-linked timelines require a claim-aware repair")
        choices = self.get_event_digest_choices(keys)
        if action == "merge" and len(set(choices.values())) > 1:
            raise ValueError("conflicting digest choices must be resolved before merging")
        serialized = [asdict(report) for report in sorted(reports, key=lambda item: item.observation_id)]
        evidence = {"action": action, "event_keys": keys, "observation_ids": selected,
                    "reports": serialized, "digest_choices": choices}
        revision = hashlib.sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        representative = serialized[0]
        comparisons = [{"observation_id": item["observation_id"], **explain_event_match(item, representative)} for item in serialized[1:]]
        return {**evidence, "revision": revision, "comparisons": comparisons,
                "warning": "人工调整只改变事件归属，不重发通知；低匹配分需要人工核对，拆分不训练聚类规则。"}

    def _freeze_digest_reports(self, identifiers: Sequence[int]) -> None:
        rows = self.connection.execute(
            "SELECT DISTINCT d.* FROM digests d JOIN digest_items di ON di.digest_id=d.id "
            "WHERE di.event_id IN (SELECT value FROM json_each(?)) AND di.event_reports_json IS NULL LIMIT 1001",
            (json.dumps(list(identifiers)),),
        ).fetchall()
        if len(rows) > 1000:
            raise ValueError("repair affects too many historical digests")
        for row in rows:
            digest = self.database._digest_from_row(row)
            for position, item in enumerate(digest.items):
                self.connection.execute(
                    "UPDATE digest_items SET event_reports_json=? WHERE digest_id=? AND position=? AND event_reports_json IS NULL",
                    (json.dumps([asdict(report) for report in item.reports], ensure_ascii=False), row["id"], position),
                )

    def _rebuild(self, event_key: str, now: int) -> None:
        identifier = self._id(event_key)
        reports = self.database.list_event_reports(event_key, limit=201)
        event = self.database.get_event(event_key)
        assert event is not None and reports
        rows = self.connection.execute(
            "SELECT o.* FROM observations o JOIN event_reports er ON er.observation_id=o.id WHERE er.event_id=?", (identifier,),
        ).fetchall()
        representative = min(reports, key=lambda report: (report.source_tier != "primary", -report.published_at, report.observation_id))
        independent = len({report.publisher.strip().casefold() or report.source_id for report in reports})
        last = max(report.published_at for report in reports)
        self.database._save_event(replace(
            event, title=representative.title, summary=representative.summary or representative.title,
            score=event_aggregate_score(max(event_evidence_score(row["importance"], row["urgency"], row["relevance"], row["confidence"]) for row in rows), independent),
            importance=max(int(row["importance"]) for row in rows), urgency=max(int(row["urgency"]) for row in rows),
            relevance=max(int(row["relevance"]) for row in rows), confidence=max(float(row["confidence"]) for row in rows),
            first_seen_at=min(report.published_at for report in reports), last_seen_at=last,
            regions=tuple(sorted({str(row["region"]) for row in rows})), topics=tuple(sorted({str(row["topic"]) for row in rows})),
            independent_source_count=independent, status=event_lifecycle(last, now), updated_at=now,
        ))
        # Normal projector upserts expand time ranges; a split must shrink its
        # remaining aggregate to exactly the evidence it still owns.
        self.connection.execute("UPDATE events SET first_seen_at=?,last_seen_at=? WHERE id=?",
                                (min(report.published_at for report in reports), last, identifier))
        self.connection.execute(
            "UPDATE event_reports SET is_representative=(id IN (SELECT id FROM (SELECT id,ROW_NUMBER() OVER(PARTITION BY source_id ORDER BY published_at DESC,id DESC) n FROM event_reports WHERE event_id=?) WHERE n=1)) WHERE event_id=?", (identifier, identifier),
        )

    def apply_event_repair(self, action: str, event_keys: Sequence[str], *,
                           observation_ids: Sequence[int], expected_revision: str,
                           actor: str, reason: str, now: int) -> str:
        self._metadata(actor, reason, now)
        with self.database.unit_of_work():
            preview = self.preview_event_repair(action, event_keys, observation_ids=observation_ids)
            if not expected_revision or preview["revision"] != expected_revision:
                raise EventWorkspaceConflict("事件证据已变化，请重新预览后确认")
            keys = preview["event_keys"]
            identifiers = [self._id(key) for key in keys]
            self._freeze_digest_reports(identifiers)
            if action == "merge":
                target = min(preview["reports"], key=lambda report: (report["source_tier"] != "primary", report["published_at"], report["observation_id"]))["event_key"]
                target_id = self._id(target)
                for identifier in identifiers:
                    if identifier == target_id:
                        continue
                    self.connection.execute("UPDATE event_reports SET event_id=? WHERE event_id=?", (target_id, identifier))
                    self.connection.execute("UPDATE events SET status='closed',updated_at=? WHERE id=?", (now, identifier))
                    self.connection.execute("UPDATE event_aliases SET target_id=? WHERE target_id=?", (target_id, identifier))
                    self.connection.execute("INSERT INTO event_aliases(event_id,target_id) VALUES(?,?)", (identifier, target_id))
                # An unread event remains unread after a merge; following any
                # member keeps the combined event in this user's bookmarks.
                preferences = self.connection.execute(
                    "SELECT actor,COUNT(*) AS members,MIN(is_read) AS is_read,MAX(followed) AS followed,MIN(ignored) AS ignored "
                    "FROM event_preferences WHERE event_id IN (SELECT value FROM json_each(?)) GROUP BY actor",
                    (json.dumps(identifiers),),
                ).fetchall()
                for preference in preferences:
                    target_preference = self.connection.execute(
                        "SELECT is_read,ignored FROM event_preferences WHERE event_id=? AND actor=?",
                        (target_id, preference["actor"]),
                    ).fetchone()
                    self.connection.execute(
                        "INSERT INTO event_preferences(event_id,actor,is_read,followed,ignored,updated_at) VALUES(?,?,?,?,?,?) "
                        "ON CONFLICT(event_id,actor) DO UPDATE SET is_read=excluded.is_read,followed=excluded.followed,ignored=excluded.ignored,updated_at=excluded.updated_at",
                        (target_id, preference["actor"],
                         preference["is_read"] if preference["members"] == len(identifiers) else (target_preference["is_read"] if target_preference is not None else 0),
                         preference["followed"],
                         preference["ignored"] if preference["members"] == len(identifiers) else (target_preference["ignored"] if target_preference is not None else 0), now),
                    )
                choices = preview["digest_choices"]
                if choices:
                    self.connection.execute(
                        "INSERT INTO event_editorial(event_id,choice,actor,reason,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(event_id) DO NOTHING",
                        (target_id, next(iter(choices.values())), actor, reason, now),
                    )
            else:
                original = self.database.get_event(keys[0])
                assert original is not None
                target = uuid.uuid4().hex[:24]
                self.database._save_event(replace(original, event_key=target, created_at=now, updated_at=now))
                target_id = self._id(target)
                identifiers.append(target_id)
                self.connection.execute(
                    "UPDATE event_reports SET event_id=? WHERE event_id=? AND observation_id IN (SELECT value FROM json_each(?))",
                    (target_id, identifiers[0], json.dumps(preview["observation_ids"])),
                )
                self._rebuild(keys[0], now)
            self._rebuild(target, now)
            self._audit(action, actor, reason, now, {**preview, "target_event_key": target}, identifiers)
            return str(target)

    def list_event_audit(self, event_key: str, *, limit: int = 50) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("event audit limit is invalid")
        identifier = self._id(event_key)
        return [{"id": row["id"], "action": row["action"], "actor": row["actor"], "reason": row["reason"],
                 "created_at": row["created_at"], "details": self._audit_summary(json.loads(row["details_json"]))} for row in self.connection.execute(
                     "SELECT a.* FROM event_review_audit a JOIN event_review_audit_events ae ON ae.audit_id=a.id WHERE ae.event_id=? ORDER BY a.id DESC LIMIT ?", (identifier, limit),
                 )]

    @staticmethod
    def _audit_summary(details: dict[str, Any]) -> dict[str, Any]:
        # Full pre-operation evidence stays in durable audit storage; browsing
        # the history need not retransmit hundreds of article bodies per action.
        return {**{key: value for key, value in details.items() if key not in {"reports", "comparisons"}},
                "previous_assignments": [{"observation_id": item["observation_id"], "event_key": item["event_key"]}
                                         for item in details.get("reports", [])]}
