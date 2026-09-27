"""Human review persistence; labels never change clustering or notification policy."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from .event_review import EVENT_QUALITY_LABELS, MERGE_OUTCOME_LABELS

if TYPE_CHECKING:
    from .database import Database


class SQLiteEventQuality:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.connection = database.connection

    def save(self, event_key: str, *, label: str, reason: str, actor: str, now: int) -> dict[str, Any]:
        if label not in EVENT_QUALITY_LABELS:
            raise ValueError("event quality label is invalid")
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000:
            raise ValueError("review reason is required (up to 2000 characters)")
        if not actor or len(actor) > 128 or now < 0:
            raise ValueError("review audit metadata is invalid")
        with self.database.unit_of_work():
            event = self.database.get_event(event_key)
            if event is None:
                raise ValueError("event no longer exists")
            previous_outcomes: list[dict[str, Any]] = []
            if label in MERGE_OUTCOME_LABELS:
                previous_outcomes = [dict(row) for row in self.connection.execute(
                    "SELECT label,reason,actor,created_at FROM event_quality_labels WHERE event_key=? "
                    "AND label IN ('correct_merge','false_merge','missed_merge') AND label!=?",
                    (event_key, label),
                )]
                self.connection.execute(
                    "DELETE FROM event_quality_labels WHERE event_key=? "
                    "AND label IN ('correct_merge','false_merge','missed_merge') AND label!=?",
                    (event_key, label),
                )
            self.connection.execute(
                "INSERT INTO event_quality_labels(event_key,label,reason,actor,created_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(event_key,label) DO UPDATE SET reason=excluded.reason,actor=excluded.actor,"
                "created_at=excluded.created_at", (event_key, label, reason.strip(), actor, now),
            )
            self.connection.execute(
                "INSERT INTO admin_auth_audit(user_id,action,actor,details_json,created_at) "
                "VALUES(NULL,'event_quality_label',?,?,?)",
                (actor, json.dumps({"event_key": event_key, "label": label, "reason": reason.strip(),
                                    "replaced_outcomes": previous_outcomes},
                                   ensure_ascii=False, sort_keys=True), now),
            )
            row = self.connection.execute(
                "SELECT * FROM event_quality_labels WHERE event_key=? AND label=?", (event_key, label),
            ).fetchone()
            assert row is not None
            return dict(row)

    def list(self, *, since: int, until: int, limit: int = 1000) -> list[dict[str, Any]]:
        if since < 0 or until <= since or not 1 <= limit <= 1000:
            raise ValueError("review sample window is invalid")
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM event_quality_labels WHERE created_at>=? AND created_at<? "
            "ORDER BY created_at DESC,event_key,label LIMIT ?", (since, until, limit),
        )]
