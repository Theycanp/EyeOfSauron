from __future__ import annotations

import argparse
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping

from argus.config import load_config
from argus.database import SCHEMA_VERSION


def evaluate_status(
    status: Mapping[str, Any],
    config_path: Path,
    *,
    now: int,
    source_age_multiplier: float,
    minimum_source_age: int,
    max_pending: int,
) -> dict[str, Any]:
    config = load_config(config_path, include_managed=False)
    with closing(sqlite3.connect(
        config.service.database_path.resolve().as_uri() + "?mode=ro", uri=True,
    )) as connection:
        active = connection.execute(
            "SELECT revision, payload_json FROM config_revisions "
            "WHERE active = 1 ORDER BY revision DESC LIMIT 1"
        ).fetchone()
    if active is not None:
        if active[0] != status.get("desired_revision"):
            raise ValueError("configuration changed during the health check; retry")
        config = load_config(config_path, managed_override=json.loads(active[1]))
    errors: list[str] = []
    warnings: list[str] = []
    schema = int(status.get("database_schema", -1))
    if schema != SCHEMA_VERSION:
        errors.append(f"database schema is {schema}; running code expects {SCHEMA_VERSION}")

    engine = status.get("engine")
    if not isinstance(engine, Mapping):
        errors.append("engine runtime registration is missing")
        engine_state = None
        heartbeat_age = None
        applied_revision = None
    else:
        engine_state = str(engine.get("state") or "")
        heartbeat_age_value = engine.get("heartbeat_age_seconds")
        heartbeat_age = (
            int(heartbeat_age_value) if heartbeat_age_value is not None else None
        )
        applied_revision = engine.get("applied_revision")
        if engine_state not in {"running", "degraded"}:
            errors.append(f"engine runtime state is {engine_state or 'unknown'}")
        heartbeat_limit = max(60, config.service.heartbeat_interval_seconds * 4)
        if heartbeat_age is None:
            errors.append("engine heartbeat is missing")
        elif heartbeat_age > heartbeat_limit:
            errors.append(
                f"engine heartbeat is stale: age {heartbeat_age}s, limit {heartbeat_limit}s"
            )

    desired_revision = status.get("desired_revision")
    if desired_revision != applied_revision:
        errors.append(
            "engine has not applied the desired configuration revision: "
            f"desired {desired_revision!r}, applied {applied_revision!r}"
        )

    states = {
        str(item.get("source_id")): item
        for item in status.get("sources", [])
        if isinstance(item, Mapping) and item.get("source_id")
    }
    source_results: list[dict[str, Any]] = []
    for source in config.sources:
        if not source.enabled:
            continue
        state = states.get(source.id)
        allowed_age = max(
            minimum_source_age,
            int(source.poll_interval_seconds * source_age_multiplier)
            + source.request_timeout_seconds * source.request_attempts
            + source.retry_base_seconds * max(0, source.request_attempts - 1),
        )
        if state is None:
            errors.append(f"enabled source {source.id} has no persisted runtime state")
            source_results.append({"source_id": source.id, "ok": False, "reason": "missing state"})
            continue
        last_success = int(state.get("last_success_at") or 0)
        age = now - last_success if last_success else None
        outage = bool(state.get("outage_alerted"))
        failures = int(state.get("consecutive_failures") or 0)
        if last_success == 0:
            warnings.append(f"enabled source {source.id} has never completed successfully")
        elif age is not None and age > allowed_age:
            warnings.append(
                f"enabled source {source.id} is stale: last success {age}s ago, limit {allowed_age}s"
            )
        if outage:
            warnings.append(f"enabled source {source.id} has an open outage")
        elif failures:
            warnings.append(f"enabled source {source.id} has {failures} consecutive failure(s)")
        runtime_status = str(state.get("runtime_status") or "")
        last_activity = max(
            int(state.get("last_attempt_at") or 0),
            int(state.get("registered_at") or 0),
        )
        poll_age = max(0, now - last_activity)
        poll_limit = max(90, source.poll_interval_seconds * 3)
        if not last_activity or poll_age > poll_limit:
            errors.append(
                f"enabled source {source.id} worker has not polled within {poll_limit}s"
            )
        if runtime_status not in {"active", "degraded"}:
            errors.append(
                f"enabled source {source.id} runtime state is {runtime_status or 'unknown'}"
            )
        source_results.append(
            {
                "source_id": source.id,
                "ok": runtime_status in {"active", "degraded"} and bool(last_activity) and poll_age <= poll_limit,
                "last_poll_age_seconds": poll_age,
                "last_success_age_seconds": age,
                "allowed_age_seconds": allowed_age,
                "consecutive_failures": failures,
            }
        )

    outbox = status.get("outbox", {})
    if not isinstance(outbox, Mapping):
        errors.append("outbox status is missing")
        pending = 0
    else:
        pending = int(outbox.get("pending", 0)) + int(outbox.get("sending", 0))
        if pending > max_pending:
            errors.append(f"outbox has {pending} in-flight messages; limit is {max_pending}")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "database_schema": schema,
        "engine_state": engine_state,
        "engine_heartbeat_age_seconds": heartbeat_age,
        "desired_revision": desired_revision,
        "applied_revision": applied_revision,
        "pending_or_sending": pending,
        "sources": source_results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate Argus persisted status for a release gate.")
    parser.add_argument("--status-file", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--now", type=int, default=int(time.time()))
    parser.add_argument("--source-age-multiplier", type=float, default=4.0)
    parser.add_argument("--minimum-source-age", type=int, default=300)
    parser.add_argument("--max-pending", type=int, default=1000)
    args = parser.parse_args(argv)
    try:
        status = json.loads(args.status_file.read_text(encoding="utf-8"))
        if not isinstance(status, Mapping):
            raise ValueError("status payload is not an object")
        result = evaluate_status(
            status,
            args.config,
            now=args.now,
            source_age_multiplier=args.source_age_multiplier,
            minimum_source_age=args.minimum_source_age,
            max_pending=args.max_pending,
        )
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        result = {"ok": False, "errors": [f"cannot evaluate status: {exc}"], "warnings": []}
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
