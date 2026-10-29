"""Preview, probe and explicitly activate Tibo's public reset-announcement feed."""
from __future__ import annotations

import argparse
import copy
from dataclasses import replace
from datetime import UTC, datetime
import json
from pathlib import Path
import re
from typing import Any, Mapping

from argus.adapters import build_collector
from argus.admin import ManagedConfigStore
from argus.config import AppConfig, load_config, parse_source_config
from argus.database import Database
from argus.models import SourceState
from argus.rules import RuleSet


SOURCE = {
    "id": "tibo_codex_resets",
    "kind": "rss",
    "enabled": True,
    "publisher": "Tibo @thsottiaux（经 tokenusage.dev 收录）",
    "section": "Codex 额度重置公告；以原帖为准",
    "dedupe_scope": "tibo_codex_resets",
    "url": "https://tokenusage.dev/api/resets.atom",
    "allowed_hosts": ["tokenusage.dev", "x.com"],
    "poll_interval_seconds": 60,
    "request_timeout_seconds": 15,
    "request_attempts": 2,
    "retry_base_seconds": 2,
    "max_response_bytes": 262144,
    "region": "GLOBAL",
    "source_tier": "secondary",
    "default_importance": 4,
    "settings": {"topic": "technology"},
}

RULE = {
    "id": "tibo_codex_reset_notice",
    "kind": "weighted_text",
    "source_ids": [SOURCE["id"]],
    "threshold": 1,
    "max_item_age_seconds": 172800,
    "notification_title": "Tibo：Codex 重置公告（生效时间以原帖为准）",
    "priority": 4,
    "tags": ["bell", "computer"],
    "patterns": [{
        "label": "专门订阅 Tibo 重置公告；第三方收录，不代表个人额度已到账",
        "regex": r"(?i)^Codex\b.*\breset\b.*\bannouncement\b",
        "title_weight": 1,
        "summary_weight": 0,
    }],
}

_POST_URL = re.compile(r"https://x\.com/(?:i|thsottiaux)/status/[0-9]+\Z")


def plan_migration(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Add the subscription without overwriting any operator-owned settings."""
    planned = copy.deepcopy(dict(payload))
    for key, addition in (("sources", SOURCE), ("rules", RULE)):
        entries = planned.setdefault(key, [])
        existing = next((item for item in entries if item["id"] == addition["id"]), None)
        if existing is not None and existing != addition:
            raise ValueError("Existing Tibo configuration differs; preserve operator settings")
        if existing is None:
            entries.append(copy.deepcopy(addition))
    return planned


def probe(config: AppConfig) -> dict[str, Any]:
    source = parse_source_config(SOURCE)
    state = SourceState(source.id, False, None, None, None, None, 0, False, None)
    collector = build_collector(source)
    if collector is None:
        raise RuntimeError("Reset feed collector is unavailable")
    result = collector.fetch(state)
    if not result.observations:
        raise RuntimeError("No feed entries; activation refused")
    if any(not _POST_URL.fullmatch(item.url) for item in result.observations):
        raise RuntimeError("Unexpected source links; activation refused")
    if any(item.attributes.get("published_at_inferred") for item in result.observations):
        raise RuntimeError("Missing publication timestamps; activation refused")
    rules = RuleSet.from_config(
        tuple(rule for rule in config.rules if rule.id == RULE["id"]),
        config.ntfy.default_topic,
    )
    # Replay at publication time without persisting or enqueuing any notification.
    for item in result.observations:
        matches = rules.evaluate(item, int(item.published_at.timestamp()) + 1)
        if len(matches) != 1 or matches[0].priority != 4:
            raise RuntimeError("Reset-feed rule replay failed")
    latest = max(result.observations, key=lambda item: item.published_at)
    latest_time = int(latest.published_at.timestamp())
    if rules.evaluate(replace(latest, source_id="unrelated_source"), latest_time + 1):
        raise RuntimeError("Rule source isolation replay failed")
    if rules.evaluate(replace(latest, title="Ordinary product update"), latest_time + 1):
        raise RuntimeError("Rule non-reset replay failed")
    if rules.evaluate(latest, latest_time + RULE["max_item_age_seconds"] + 1):
        raise RuntimeError("Rule old-message replay failed")
    return {
        "items": len(result.observations),
        "latest_at": latest.published_at.isoformat(),
        "latest_url": latest.url,
        "latest_age_seconds": int((datetime.now(UTC) - latest.published_at).total_seconds()),
        "rule_replay": "passed",
        "poll_seconds": SOURCE["poll_interval_seconds"],
        "upstream_poll_freshness": "not_checked",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--expect-revision", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
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
        planned = plan_migration(current)
        config = load_config(args.config, managed_override=planned)
        print(json.dumps({"revision": revision, **probe(config)}, ensure_ascii=False), flush=True)
        if not args.apply or planned == current:
            print("No configuration changes; probe/replay passed.")
            return
        store = ManagedConfigStore(
            base.service.managed_sources_path,
            {source.id for source in base.sources}, {rule.id for rule in base.rules}, database,
            notification_topics=config.ntfy.allowed_topics,
        )
        revision = store.write(
            planned, actor="operations:tibo-reset-monitor",
            reason="User-requested Tibo reset announcements via public Atom feed; direct priority-4 rule",
            expected_revision=args.expect_revision,
        )
        print(f"Activated revision {revision}; verify acknowledgement and first feed baseline.")
    finally:
        database.close()


if __name__ == "__main__":
    main()
