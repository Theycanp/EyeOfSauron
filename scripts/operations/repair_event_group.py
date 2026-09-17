"""Preview or explicitly repair a small, verified duplicate decision group."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from argus.config import load_config
from argus.database import Database, read_active_config
from argus.events import EventRepairRepository


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--event-key", action="append", required=True)
    parser.add_argument("--expected-observation-id", action="append", type=int)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    base = load_config(args.config, include_managed=False)
    config = load_config(args.config, managed_override=read_active_config(base.service.database_path))
    database = Database(config.service.database_path)
    try:
        reports = [report for key in args.event_key for report in database.list_event_reports(key, limit=201)]
        print(json.dumps({"event_keys": args.event_key, "reports": [
            {"observation_id": report.observation_id, "title": report.title, "event_key": report.event_key}
            for report in reports]}, ensure_ascii=False))
        if args.apply:
            if not args.expected_observation_id:
                parser.error("apply requires the complete expected observation ID set from preview")
            repository: EventRepairRepository = database
            target = repository.merge_semantic_event_group(
                args.event_key, expected_observation_ids=args.expected_observation_id,
                actor="operations:semantic-event-repair", now=int(time.time()),
                primary_source_ids=tuple(source.id for source in config.sources if source.source_tier == "primary"),
            )
            print(json.dumps({"merged_into": target, "reports": len(reports)}))
        else:
            print("preview only; original observations, notifications and digest snapshots will be preserved")
    finally:
        database.close()


if __name__ == "__main__":
    main()
