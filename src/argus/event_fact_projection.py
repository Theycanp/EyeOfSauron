"""Durable, bounded projection of reviewed facts from event reports."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Mapping, Protocol, Sequence

from .event_facts import (
    FACT_EXTRACTOR_VERSION, FACT_PRODUCER, EventFactCandidate,
    FactExtractionInput, extract_event_facts,
)
from .util import sanitize_error

if TYPE_CHECKING:
    from .database import Database

LOGGER = logging.getLogger("argus.event_facts")


@dataclass(frozen=True, slots=True)
class EventFactWorkItem:
    report_id: int
    observation_id: int
    event_key: str
    source_id: str
    external_id: str
    title: str
    summary: str
    published_at: int
    source_tier: str
    attributes: Mapping[str, Any]
    lease_token: str
    extractor_version: int

    def extraction_input(self) -> FactExtractionInput:
        return FactExtractionInput(
            source_id=self.source_id, external_id=self.external_id,
            title=self.title, summary=self.summary,
            published_at=self.published_at, attributes=self.attributes,
        )


class EventFactWorkRepository(Protocol):
    def claim_event_fact_job(self, now: int, *, version: int, lease_seconds: int = 60) -> EventFactWorkItem | None: ...
    def complete_event_fact_job(
        self, work: EventFactWorkItem, facts: Sequence[EventFactCandidate], now: int,
    ) -> bool: ...
    def fail_event_fact_job(self, work: EventFactWorkItem, error: BaseException | str, now: int) -> bool: ...


class EventFactProjector:
    def __init__(self, repository: EventFactWorkRepository) -> None:
        self.repository = repository

    def process_once(self, now: int) -> bool:
        work = self.repository.claim_event_fact_job(now, version=FACT_EXTRACTOR_VERSION)
        if work is None:
            return False
        try:
            facts = extract_event_facts(work.extraction_input())
            if len(facts) > 20:
                raise ValueError("fact extractor exceeded the per-report limit")
            self.repository.complete_event_fact_job(work, facts, now)
        except Exception as exc:
            LOGGER.warning("event_fact_projection_failed report_id=%d error=%s", work.report_id, sanitize_error(exc))
            self.repository.fail_event_fact_job(work, exc, now)
        return True


class SQLiteEventFacts:
    """SQLite adapter; extraction always happens outside its write transactions."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.connection = database.connection

    def claim_event_fact_job(
        self, now: int, *, version: int, lease_seconds: int = 60,
    ) -> EventFactWorkItem | None:
        if now < 0 or version < 1 or not 1 <= lease_seconds <= 3600:
            raise ValueError("invalid fact job lease")
        token = uuid.uuid4().hex
        with self.database.unit_of_work():
            self.connection.execute(
                "INSERT OR IGNORE INTO event_fact_jobs(report_id,extractor_version,status,next_attempt_at,updated_at) "
                "SELECT er.id,?,'pending',0,? FROM event_reports er "
                "WHERE NOT EXISTS(SELECT 1 FROM event_fact_jobs j WHERE j.report_id=er.id "
                "AND j.extractor_version=?) ORDER BY er.id LIMIT 100",
                (version, now, version),
            )
            self.connection.execute(
                "UPDATE event_fact_jobs SET status='retry',lease_token=NULL,lease_until=NULL,"
                "next_attempt_at=?,updated_at=? WHERE status='leased' AND lease_until<=? "
                "AND extractor_version=?", (now, now, now, version),
            )
            row = self.connection.execute(
                "SELECT j.report_id FROM event_fact_jobs j WHERE j.extractor_version=? "
                "AND j.status IN ('pending','retry') AND j.next_attempt_at<=? "
                "ORDER BY j.next_attempt_at,j.report_id LIMIT 1", (version, now),
            ).fetchone()
            if row is None:
                return None
            report_id = int(row["report_id"])
            changed = self.connection.execute(
                "UPDATE event_fact_jobs SET status='leased',attempts=attempts+1,lease_token=?,"
                "lease_until=?,updated_at=? WHERE report_id=? AND extractor_version=? "
                "AND status IN ('pending','retry')",
                (token, now + lease_seconds, now, report_id, version),
            )
            if changed.rowcount != 1:
                return None
            detail = self.connection.execute(
                "SELECT er.id report_id,er.observation_id,e.event_key,o.source_id,o.external_id,"
                "er.title,er.summary,er.published_at,er.source_tier,o.attributes_json "
                "FROM event_reports er JOIN events e ON e.id=er.event_id "
                "JOIN observations o ON o.id=er.observation_id WHERE er.id=?", (report_id,),
            ).fetchone()
            if detail is None:
                raise RuntimeError("fact job lost its report")
            try:
                attributes = json.loads(detail["attributes_json"])
            except (TypeError, ValueError):
                attributes = {}
        return EventFactWorkItem(
            report_id=report_id, observation_id=int(detail["observation_id"]),
            event_key=str(detail["event_key"]), source_id=str(detail["source_id"]),
            external_id=str(detail["external_id"]), title=str(detail["title"]),
            summary=str(detail["summary"]), published_at=int(detail["published_at"]),
            source_tier=str(detail["source_tier"]),
            attributes=attributes if isinstance(attributes, dict) else {},
            lease_token=token, extractor_version=version,
        )

    def complete_event_fact_job(
        self, work: EventFactWorkItem, facts: Sequence[EventFactCandidate], now: int,
    ) -> bool:
        if now < 0 or len(facts) > 20 or any(fact.version != work.extractor_version for fact in facts):
            raise ValueError("invalid extracted fact batch")
        with self.database.unit_of_work():
            job = self.connection.execute(
                "SELECT status,lease_token FROM event_fact_jobs WHERE report_id=? AND extractor_version=?",
                (work.report_id, work.extractor_version),
            ).fetchone()
            if job is None or job["status"] != "leased" or job["lease_token"] != work.lease_token:
                return False
            report = self.connection.execute(
                "SELECT er.event_id,e.event_key,er.title,er.summary,er.published_at "
                "FROM event_reports er JOIN events e ON e.id=er.event_id WHERE er.id=?",
                (work.report_id,),
            ).fetchone()
            if (report is None or report["event_key"] != work.event_key
                    or report["title"] != work.title or report["summary"] != work.summary
                    or report["published_at"] != work.published_at):
                self.connection.execute(
                    "UPDATE event_fact_jobs SET status='retry',lease_token=NULL,lease_until=NULL,"
                    "next_attempt_at=?,updated_at=? WHERE report_id=? AND extractor_version=?",
                    (now, now, work.report_id, work.extractor_version),
                )
                return False
            event_id = int(report["event_id"])
            for fact in facts:
                self._save_fact(event_id, work, fact, now)
            for slot in {fact.slot_key for fact in facts}:
                self._reconcile_slot(event_id, slot, now)
            self.connection.execute(
                "UPDATE event_fact_jobs SET status='completed',lease_token=NULL,lease_until=NULL,"
                "last_error=NULL,updated_at=? WHERE report_id=? AND extractor_version=?",
                (now, work.report_id, work.extractor_version),
            )
        return True

    def _save_fact(self, event_id: int, work: EventFactWorkItem, fact: EventFactCandidate, now: int) -> None:
        confidence = 0.9 if work.source_tier == "primary" else 0.7 if work.source_tier == "secondary" else 0.5
        fact_json = json.dumps({
            key: value for key, value in asdict(fact).items()
            if key not in {"evidence_span", "slot_key", "claim_key"}
        }, ensure_ascii=False, sort_keys=True)
        self.connection.execute(
            "INSERT INTO event_claims(event_id,claim_key,text,status,confidence,first_seen_at,"
            "last_seen_at,created_at,updated_at,producer,extractor_version,slot_key,fact_json) "
            "VALUES(?,?,?,'active',?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(event_id,claim_key) DO UPDATE SET "
            "last_seen_at=MAX(event_claims.last_seen_at,excluded.last_seen_at),"
            "confidence=MAX(event_claims.confidence,excluded.confidence),"
            "updated_at=excluded.updated_at",
            (event_id, fact.claim_key, fact.evidence_span, confidence,
             work.published_at, work.published_at, now, now,
             FACT_PRODUCER, fact.version, fact.slot_key, fact_json),
        )
        claim = self.connection.execute(
            "SELECT id,producer FROM event_claims WHERE event_id=? AND claim_key=?",
            (event_id, fact.claim_key),
        ).fetchone()
        assert claim is not None
        if claim["producer"] != FACT_PRODUCER:
            raise ValueError("automatic fact key collides with a manual claim")
        self.connection.execute(
            "INSERT INTO event_claim_evidence(claim_id,report_id,stance,note,created_at,producer) "
            "VALUES(?,?,'supports',?,?,?) ON CONFLICT(claim_id,report_id,stance) DO NOTHING",
            (int(claim["id"]), work.report_id, fact.evidence_span[:2000], now, FACT_PRODUCER),
        )
        if fact.effective_at is not None:
            self.connection.execute(
                "INSERT INTO event_timeline(event_id,occurred_at,kind,text,confidence,report_id,"
                "created_at,producer,timeline_key) VALUES(?,?,'fact',?,?,?,?,?,?) "
                "ON CONFLICT(event_id,timeline_key) WHERE timeline_key IS NOT NULL DO NOTHING",
                (event_id, fact.effective_at, fact.evidence_span, confidence,
                 work.report_id, now, FACT_PRODUCER, f"fact:{fact.claim_key}"),
            )

    def _reconcile_slot(self, event_id: int, slot_key: str, now: int) -> None:
        rows = self.connection.execute(
            "SELECT id FROM event_claims WHERE event_id=? AND slot_key=? "
            "AND producer=? AND EXISTS(SELECT 1 FROM event_claim_evidence ce "
            "WHERE ce.claim_id=event_claims.id) LIMIT 2",
            (event_id, slot_key, FACT_PRODUCER),
        ).fetchall()
        status = "disputed" if len(rows) > 1 else "active"
        self.connection.execute(
            "UPDATE event_claims SET status=?,updated_at=? WHERE event_id=? AND slot_key=? "
            "AND producer=? AND status!='superseded'",
            (status, now, event_id, slot_key, FACT_PRODUCER),
        )

    def fail_event_fact_job(self, work: EventFactWorkItem, error: BaseException | str, now: int) -> bool:
        if now < 0:
            raise ValueError("invalid fact failure time")
        with self.database.unit_of_work():
            row = self.connection.execute(
                "SELECT attempts FROM event_fact_jobs WHERE report_id=? AND extractor_version=? "
                "AND status='leased' AND lease_token=?",
                (work.report_id, work.extractor_version, work.lease_token),
            ).fetchone()
            if row is None:
                return False
            attempts = int(row["attempts"])
            terminal = attempts >= 5
            self.connection.execute(
                "UPDATE event_fact_jobs SET status=?,lease_token=NULL,lease_until=NULL,"
                "next_attempt_at=?,last_error=?,updated_at=? "
                "WHERE report_id=? AND extractor_version=? AND lease_token=?",
                ("dead" if terminal else "retry", now + min(3600, 30 * 2 ** (attempts - 1)),
                 sanitize_error(error), now, work.report_id, work.extractor_version, work.lease_token),
            )
        return True

    def detach_automatic_facts(self, report_ids: Sequence[int], now: int) -> None:
        """Must run inside the same repair transaction that moves these reports."""
        if not self.connection.in_transaction or not report_ids or len(report_ids) > 200:
            raise ValueError("invalid automatic fact repair")
        encoded = json.dumps(list(report_ids))
        affected = self.connection.execute(
            "SELECT DISTINCT c.event_id,c.slot_key FROM event_claim_evidence ce "
            "JOIN event_claims c ON c.id=ce.claim_id WHERE ce.report_id IN "
            "(SELECT value FROM json_each(?)) AND ce.producer=?",
            (encoded, FACT_PRODUCER),
        ).fetchall()
        self.connection.execute(
            "DELETE FROM event_timeline WHERE producer=? AND report_id IN "
            "(SELECT value FROM json_each(?))", (FACT_PRODUCER, encoded),
        )
        self.connection.execute(
            "DELETE FROM event_claim_evidence WHERE producer=? AND report_id IN "
            "(SELECT value FROM json_each(?))", (FACT_PRODUCER, encoded),
        )
        self.connection.execute(
            "DELETE FROM event_claims WHERE producer=? AND NOT EXISTS "
            "(SELECT 1 FROM event_claim_evidence ce WHERE ce.claim_id=event_claims.id)",
            (FACT_PRODUCER,),
        )
        self.connection.execute(
            "UPDATE event_fact_jobs SET status='pending',attempts=0,next_attempt_at=?,"
            "lease_token=NULL,lease_until=NULL,last_error=NULL,updated_at=? WHERE report_id IN "
            "(SELECT value FROM json_each(?))", (now, now, encoded),
        )
        for row in affected:
            if row["slot_key"]:
                self._reconcile_slot(int(row["event_id"]), str(row["slot_key"]), now)
            remaining = self.connection.execute(
                "SELECT c.id,c.claim_key,c.text,c.confidence,c.fact_json,MIN(ce.report_id) report_id "
                "FROM event_claims c JOIN event_claim_evidence ce ON ce.claim_id=c.id "
                "WHERE c.event_id=? AND c.slot_key=? AND c.producer=? GROUP BY c.id",
                (int(row["event_id"]), row["slot_key"], FACT_PRODUCER),
            ).fetchall()
            for claim in remaining:
                payload = json.loads(claim["fact_json"] or "{}")
                effective_at = payload.get("effective_at")
                if type(effective_at) is not int:
                    continue
                self.connection.execute(
                    "INSERT INTO event_timeline(event_id,occurred_at,kind,text,confidence,report_id,"
                    "created_at,producer,timeline_key) VALUES(?,?,'fact',?,?,?,?,?,?) "
                    "ON CONFLICT(event_id,timeline_key) WHERE timeline_key IS NOT NULL DO NOTHING",
                    (int(row["event_id"]), effective_at, str(claim["text"]),
                     float(claim["confidence"]), int(claim["report_id"]), now,
                     FACT_PRODUCER, f"fact:{claim['claim_key']}"),
                )
