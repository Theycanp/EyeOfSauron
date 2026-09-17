"""Read-only production sample replay; writes only to an isolated temporary DB."""
from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from argus.config import load_config
from argus.database import Database, read_active_config
from argus.event_pool import EventPoolProjector
from argus.models import FeedFetchResult, Observation
from argus.rules import RuleSet


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--first-alert", type=int, required=True)
    parser.add_argument("--last-alert", type=int, required=True)
    args = parser.parse_args()
    if not 1 <= args.first_alert <= args.last_alert < args.first_alert + 200:
        parser.error("replay is limited to 200 alerts")
    base = load_config(args.config, include_managed=False)
    config = load_config(args.config, managed_override=read_active_config(base.service.database_path))
    production = sqlite3.connect(base.service.database_path.resolve().as_uri() + "?mode=ro", uri=True)
    production.row_factory = sqlite3.Row
    try:
        rows = production.execute(
            "SELECT DISTINCT o.* FROM alerts a JOIN observations o ON o.id=a.observation_id "
            "WHERE a.id BETWEEN ? AND ? ORDER BY o.fetched_at, o.id", (args.first_alert, args.last_alert),
        ).fetchall()
    finally:
        production.close()
    if not rows:
        parser.error("no observation-backed alerts in range")
    rules = RuleSet.from_config(config.rules, config.ntfy.default_topic)
    with tempfile.TemporaryDirectory(prefix="eos-event-replay-") as temporary:
        isolated = Database(Path(temporary) / "state.db")
        try:
            for source in {str(row["source_id"]) for row in rows}:
                isolated.record_source_success(source, FeedFetchResult((), None, None), rules, int(rows[0]["fetched_at"]) - 1, "eos")
            for row in rows:
                item = Observation(
                    source_id=row["source_id"], publisher=row["publisher"],
                    dedupe_scope=row["dedupe_scope"], external_id=row["external_id"],
                    published_at=datetime.fromtimestamp(row["published_at"], UTC),
                    title=row["title"], summary=row["summary"], url=row["url"],
                    attributes=json.loads(row["attributes_json"]), importance=row["importance"],
                    region=row["region"], source_tier=row["source_tier"], topic=row["topic"],
                )
                isolated.record_source_success(item.source_id, FeedFetchResult((item,), None, None), rules, row["fetched_at"], "eos")
                EventPoolProjector(isolated).project_pending(now=row["fetched_at"], limit=50)
            result = {
                "input_observations": len(rows), "events": [
                    {"title": event.title, "report_count": len(isolated.list_event_reports(event.event_key))}
                    for event in isolated.list_events(limit=200)
                ], "queued_notifications": [dict(row) for row in isolated.connection.execute(
                    "SELECT title, priority FROM alerts ORDER BY id")],
                "production_modified": False, "notifications_sent": False,
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            isolated.close()


if __name__ == "__main__":
    main()
