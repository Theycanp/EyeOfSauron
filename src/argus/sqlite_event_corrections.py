"""Explicit, human-reviewed fact correction adapter, not prose inference."""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any

from .events import EventWorkspaceConflict
from .models import AlertCandidate
from .sqlite_event_workspace import SQLiteEventWorkspace

if TYPE_CHECKING:
    from .database import Database


class SQLiteEventCorrections:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.connection = database.connection

    def preview(self, event_key: str, old_claim_key: str, new_claim_key: str,
                report_id: int) -> dict[str, Any]:
        with (nullcontext() if self.connection.in_transaction else self.database.unit_of_work(immediate=False)):
            return self._preview(event_key, old_claim_key, new_claim_key, report_id)

    def _preview(self, event_key: str, old_claim_key: str, new_claim_key: str,
                 report_id: int) -> dict[str, Any]:
        if (not event_key or len(event_key) > 160 or not old_claim_key or not new_claim_key
                or len(old_claim_key) > 160 or len(new_claim_key) > 160
                or old_claim_key == new_claim_key or type(report_id) is not int or report_id < 1):
            raise ValueError("fact correction selection is invalid")
        event = self.connection.execute("SELECT id,title FROM events WHERE event_key=?", (event_key,)).fetchone()
        if event is None:
            raise ValueError("event no longer exists")
        claims = self.connection.execute(
            "SELECT id,claim_key,text,status,slot_key,updated_at,supersedes_claim_id "
            "FROM event_claims WHERE event_id=? AND claim_key IN (?,?) ORDER BY claim_key",
            (event["id"], old_claim_key, new_claim_key),
        ).fetchall()
        if len(claims) != 2:
            raise ValueError("both correction claims must exist in the same event")
        by_key = {row["claim_key"]: row for row in claims}
        old, new = by_key[old_claim_key], by_key[new_claim_key]
        if (not old["slot_key"] or old["slot_key"] != new["slot_key"]
                or old["status"] not in {"active", "disputed"}
                or new["status"] not in {"active", "disputed"}
                or new["supersedes_claim_id"] is not None):
            raise ValueError("correction requires two live claims in the same known fact slot")
        live = self.connection.execute(
            "SELECT id FROM event_claims WHERE event_id=? AND slot_key=? AND status!='superseded'",
            (event["id"], old["slot_key"]),
        ).fetchall()
        if {row["id"] for row in live} != {old["id"], new["id"]}:
            raise ValueError("resolve other live fact-slot conflicts before this correction")
        report = self.connection.execute(
            "SELECT er.id,er.observation_id,er.url,er.title,er.source_tier,er.published_at,ce.id evidence_id "
            "FROM event_reports er JOIN event_claim_evidence ce ON ce.report_id=er.id "
            "WHERE er.event_id=? AND er.id=? AND er.source_tier='primary' "
            "AND ce.claim_id=? AND ce.stance='supports'",
            (event["id"], report_id, new["id"]),
        ).fetchone()
        if report is None:
            raise ValueError("correction requires a selected primary report supporting the new claim")
        old_publication = self.connection.execute(
            "SELECT MAX(er.published_at) FROM event_claim_evidence ce "
            "JOIN event_reports er ON er.id=ce.report_id WHERE ce.claim_id=? AND ce.stance='supports'",
            (old["id"],),
        ).fetchone()[0]
        if old_publication is not None and report["published_at"] < old_publication:
            raise ValueError("correction proof cannot predate the old claim's supporting evidence")
        details = {"event_key": event_key, "event_id": int(event["id"]), "title": str(event["title"]),
                   "old": dict(old), "new": dict(new), "report": dict(report)}
        revision = hashlib.sha256(json.dumps(details, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return {**details, "revision": revision,
                "warning": "Only confirm an explicit official correction after reading the primary proof; the system does not infer correction from a changed value."}

    def apply(self, event_key: str, old_claim_key: str, new_claim_key: str, report_id: int,
              *, expected_revision: str, actor: str, reason: str, topic: str, now: int) -> dict[str, Any]:
        SQLiteEventWorkspace._metadata(actor, reason, now)
        if (not isinstance(topic, str) or not topic.strip() or len(topic) > 128
                or not isinstance(expected_revision, str) or len(expected_revision) != 64):
            raise ValueError("fact correction delivery metadata is invalid")
        with self.database.unit_of_work():
            existing = self.connection.execute(
                "SELECT c.report_id FROM event_fact_corrections c JOIN events e ON e.id=c.event_id "
                "JOIN event_claims old ON old.id=c.old_claim_id JOIN event_claims new ON new.id=c.new_claim_id "
                "WHERE e.event_key=? AND old.claim_key=? AND new.claim_key=?",
                (event_key, old_claim_key, new_claim_key),
            ).fetchone()
            if existing is not None:
                if existing["report_id"] != report_id:
                    raise EventWorkspaceConflict("fact correction was already confirmed with different proof")
                return {"applied": False, "notification_queued": False, "event_key": event_key}
            preview = self.preview(event_key, old_claim_key, new_claim_key, report_id)
            if preview["revision"] != expected_revision:
                raise EventWorkspaceConflict("fact correction evidence changed; preview again")
            old, new, report = preview["old"], preview["new"], preview["report"]
            self.connection.execute(
                "UPDATE event_claims SET status='superseded',updated_at=? WHERE id=?", (now, old["id"]),
            )
            self.connection.execute(
                "UPDATE event_claims SET status='active',supersedes_claim_id=?,updated_at=? WHERE id=?",
                (old["id"], now, new["id"]),
            )
            self.connection.execute(
                "INSERT INTO event_fact_corrections(event_id,old_claim_id,new_claim_id,report_id,actor,reason,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (preview["event_id"], old["id"], new["id"], report_id, actor, reason.strip(), now),
            )
            signature = hashlib.sha256(f"{old_claim_key}:{new_claim_key}".encode()).hexdigest()[:32]
            candidate = AlertCandidate(
                rule_id="event_fact_correction", dedupe_key=f"event-fact:{event_key}:correction:{signature}:{topic}",
                title=f"官方事实更正 · {preview['title']}"[:300],
                message=f"先前：{old['text']}\n更正：{new['text']}\n说明：{reason.strip()}\n来源：{report['title']}",
                priority=4, tags=("warning",), click_url=report["url"], topic=topic,
                confidence=0.9, evidence=(f"correction:{old_claim_key}:{new_claim_key}", f"primary_report:{report_id}"),
            )
            queued = self.database._insert_alert(candidate, report["observation_id"], now)
            if not queued:
                raise EventWorkspaceConflict("correction notification identity already exists without its audit")
            SQLiteEventWorkspace(self.database)._audit(
                "fact_correction", actor, reason, now,
                {"event_key": event_key, "old_claim_key": old_claim_key, "new_claim_key": new_claim_key,
                 "report_id": report_id, "fact_signature": signature}, [preview["event_id"]],
            )
            return {"applied": True, "notification_queued": True, "event_key": event_key}
