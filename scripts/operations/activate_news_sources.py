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
from argus.news_rollout import plan_official_news


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expect-revision", type=int, required=True)
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
        planned, additions = plan_official_news(current, base.sources)
        config = load_config(args.config, managed_override=planned)
        print(json.dumps({"revision": revision, "additions": [item["id"] for item in additions],
                          "enabled_after": sum(s.enabled for s in config.sources)}, ensure_ascii=False), flush=True)

        def probe(raw):
            source = parse_source_config(raw)
            state = SourceState(source.id, False, None, None, None, None, 0, False, None)
            result = build_collector(source).fetch(state)
            return {"source": source.id, "items": len(result.observations),
                    "newest": max(item.published_at for item in result.observations).isoformat()}

        # Every addition must fetch and parse successfully before any write.
        with ThreadPoolExecutor(max_workers=4) as pool:
            for result in pool.map(probe, additions):
                print(json.dumps(result, ensure_ascii=False), flush=True)
        if not args.apply or not additions:
            print("preview passed; no configuration changes")
            return
        store = ManagedConfigStore(
            base.service.managed_sources_path,
            {source.id for source in base.sources}, {rule.id for rule in base.rules}, database,
        )
        revision = store.write(planned, actor="operations:news-rollout",
                               reason="activate user-requested CN/JP/US/international official sources",
                               expected_revision=args.expect_revision)
        print(f"activated configuration revision {revision}; verify daemon acknowledgement and source baselines")
    finally:
        database.close()


if __name__ == "__main__":
    main()
