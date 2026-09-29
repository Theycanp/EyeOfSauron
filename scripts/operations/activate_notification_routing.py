"""Activate four-topic routing after exact ntfy ACL provisioning and client notice."""
from __future__ import annotations

import argparse
import copy
from pathlib import Path
from typing import Any, Mapping

from argus.admin import ManagedConfigStore
from argus.config import load_config
from argus.database import Database
from argus.notifications import notification_policy_payload, standard_notification_policy


def plan_migration(payload: Mapping[str, Any], old_topic: str) -> dict[str, Any]:
    planned = copy.deepcopy(dict(payload))
    planned.setdefault("sources", [])
    planned.setdefault("rules", [])
    if "notifications" in planned:
        raise ValueError("notification routing already exists; manage it through the admin UI")
    for rule in planned["rules"]:
        if rule.get("topic") == old_topic:
            rule.pop("topic")
    planned["notifications"] = notification_policy_payload(standard_notification_policy())
    return planned


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--expect-revision", type=int, required=True)
    parser.add_argument("--old-topic", default="eos")
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
        planned = plan_migration(active["payload"] if active else {}, args.old_topic)
        effective = load_config(args.config, managed_override=planned)
        assert effective.notifications is not None
        print("destinations: " + ", ".join(item.topic for item in effective.notifications.destinations))
        if not args.apply:
            print("configuration preview passed; ACL provisioning and subscriptions must be verified separately")
            return
        store = ManagedConfigStore(
            base.service.managed_sources_path, {source.id for source in base.sources},
            {rule.id for rule in base.rules}, database, notification_topics=base.ntfy.allowed_topics,
        )
        revision = store.write(planned, actor="operations:notification-migration",
                               reason="route news, weather, reminders and system notifications to separate topics",
                               expected_revision=revision)
        print(f"activated notification routing at configuration revision {revision}")
    finally:
        database.close()


if __name__ == "__main__":
    main()
