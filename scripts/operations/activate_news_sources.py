"""Preview, probe and explicitly activate the reviewed official news rollout."""
from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from argus.adapters import build_collector
from argus.admin import ManagedConfigStore
from argus.config import load_config, parse_source_config
from argus.database import Database
from argus.models import SourceState
from argus.news_rollout import (
    plan_disaster_signal_policy,
    plan_event_metadata,
    plan_official_news,
)
from argus.runtime_rollout import plan_runtime_audit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expect-revision", type=int, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--runtime-audit", action="store_true", help="apply the reviewed v0.15 runtime fixes")
    mode.add_argument("--event-metadata", action="store_true", help="fill missing legacy Fed source metadata")
    mode.add_argument(
        "--disaster-signal-policy", action="store_true",
        help="retain only exceptional JMA evidence while preserving global disaster alerts",
    )
    args = parser.parse_args()
    base = load_config(args.config, include_managed=False)
    if base.service.managed_sources_path is None:
        parser.error("managed configuration path is required")
    database = Database(base.service.database_path)
    try:
        active = database.get_active_config_revision()
        revision = int(active["revision"]) if active else 0
        if revision != args.expect_revision:
            parser.error(f"configuration changed: expected {args.expect_revision}, found {revision}")
        current = active["payload"] if active else {}
        if args.disaster_signal_policy:
            planned, additions = plan_disaster_signal_policy(current)
        elif args.event_metadata:
            planned, additions = plan_event_metadata(current)
        else:
            planned, additions = (plan_runtime_audit(current) if args.runtime_audit
                                  else plan_official_news(current, base.sources))
        config = load_config(args.config, managed_override=planned)
        print(json.dumps({"revision": revision, "additions": [item["id"] for item in additions],
                          "enabled_after": sum(s.enabled for s in config.sources)}, ensure_ascii=False), flush=True)

        def probe(raw):
            source = parse_source_config(raw)
            state = SourceState(source.id, False, None, None, None, None, 0, False, None)
            result = build_collector(source).fetch(state)
            return {"source": source.id, "items": len(result.observations),
                    "newest": max((item.published_at for item in result.observations), default=None),
                    "warnings": result.warnings}

        # Every addition must fetch and parse successfully before any write.
        with ThreadPoolExecutor(max_workers=4) as pool:
            for result in pool.map(probe, additions):
                print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
        if not args.apply or planned == current:
            print("preview passed; no configuration changes")
            return
        store = ManagedConfigStore(
            base.service.managed_sources_path,
            {source.id for source in base.sources}, {rule.id for rule in base.rules}, database,
        )
        revision = store.write(planned, actor="operations:news-rollout",
                               reason=("reduce JMA to low-frequency exceptional-disaster evidence"
                                       if args.disaster_signal_policy else
                                       "fill legacy Fed source metadata" if args.event_metadata else
                                       "runtime audit: host monitoring, API digest, official sources and JMA correction"
                                       if args.runtime_audit else "activate user-requested CN/JP/US/international official sources"),
                               expected_revision=args.expect_revision)
        if args.runtime_audit:
            repaired = database.backfill_default_topics({
                source.id: str(source.settings.get("topic", "general"))
                for source in config.sources
            })
            print(f"repaired default topics on {repaired} existing observations")
        print(f"activated configuration revision {revision}; verify daemon acknowledgement and source baselines")
    finally:
        database.close()


if __name__ == "__main__":
    main()
