"""Bounded parser-repair controls over the existing public-document queue."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Sequence

if TYPE_CHECKING:
    from .database import Database


class SQLiteContentAdministration:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.connection = database.connection

    def list(self, source_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        if not source_id or len(source_id) > 128 or not 1 <= limit <= 100:
            raise ValueError("content diagnostic arguments are invalid")
        return [dict(row) for row in self.connection.execute(
            "SELECT j.id,j.observation_id,j.url,j.status,j.failure_kind,j.attempts,j.updated_at,"
            "j.next_attempt_at FROM content_fetch_jobs j JOIN observations o ON o.id=j.observation_id "
            "WHERE o.source_id=? ORDER BY j.updated_at DESC,j.id DESC LIMIT ?", (source_id, limit),
        )]

    def retry(self, ids: Sequence[int], *, reason: str, actor: str, now: int) -> int:
        if not 1 <= len(ids) <= 50 or len(set(ids)) != len(ids) or any(type(i) is not int or i < 1 for i in ids):
            raise ValueError("choose 1 to 50 distinct content job IDs")
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000 or not actor or now < 0:
            raise ValueError("content retry requires audit metadata and a reason")
        with self.database.unit_of_work():
            placeholders = ",".join("?" for _ in ids)
            rows = self.connection.execute(
                f"SELECT id,status,failure_kind FROM content_fetch_jobs WHERE id IN ({placeholders})", tuple(ids),
            ).fetchall()
            if len(rows) != len(ids) or any(
                row["status"] != "dead" or row["failure_kind"] not in {"empty_content", "invalid_pdf"}
                for row in rows
            ):
                raise ValueError("only terminal parser failures can be retried; access denial and dead links are excluded")
            changed = self.connection.execute(
                f"UPDATE content_fetch_jobs SET status='pending',attempts=0,next_attempt_at=?,"
                f"lease_token=NULL,lease_until=NULL,last_error=NULL,failure_kind=NULL,dead_at=NULL,"
                f"updated_at=? WHERE id IN ({placeholders})", (now, now, *ids),
            )
            self.connection.execute(
                "INSERT INTO admin_auth_audit(user_id,action,actor,details_json,created_at) VALUES(NULL,?,?,?,?)",
                ("content_parser_retry", actor, json.dumps({"jobs": [dict(row) for row in rows],
                    "reason": reason.strip()}, ensure_ascii=False, sort_keys=True), now),
            )
            return changed.rowcount
