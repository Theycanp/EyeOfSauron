from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from argus.backup import BackupError, inspect_database, restore_backup, verify_backup


DEFAULT_MAX_RPO_SECONDS = 24 * 3600
DEFAULT_MAX_RTO_SECONDS = 30 * 60


def write_report(report: dict[str, Any], output: Path) -> None:
    """Publish a private JSON report atomically without replacing prior evidence."""
    parent = output.parent
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.tmp-", dir=parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(report, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, output)
        directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def restore_drill(
    bundle: Path,
    *,
    max_rpo_seconds: int = DEFAULT_MAX_RPO_SECONDS,
    max_rto_seconds: int = DEFAULT_MAX_RTO_SECONDS,
    scratch_parent: Path | None = None,
) -> dict[str, Any]:
    """Measure a verified database restore in an isolated temporary directory."""
    if max_rpo_seconds < 1 or max_rto_seconds < 1:
        raise ValueError("RPO and RTO objectives must be positive")
    started_at = datetime.now(UTC)
    started = time.monotonic()
    manifest = verify_backup(bundle)
    try:
        backup_at = datetime.fromisoformat(str(manifest["created_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        raise BackupError("backup manifest has an invalid creation time") from exc
    if backup_at.tzinfo is None or backup_at.utcoffset() != UTC.utcoffset(backup_at):
        raise BackupError("backup creation time must be UTC")
    rpo_seconds = (started_at - backup_at).total_seconds()
    if rpo_seconds < 0:
        raise BackupError("backup creation time is in the future")

    with tempfile.TemporaryDirectory(prefix="argus-restore-drill-", dir=scratch_parent) as root:
        destination = Path(root) / "state.db"
        restored = restore_backup(bundle, destination)
        metadata = inspect_database(destination, int(manifest["database"]["schema_version"]))
        if metadata["table_counts"] != manifest["database"]["table_counts"]:
            raise BackupError("restored database row counts do not match the backup manifest")
        if restored["rollback_database"] is not None:
            raise BackupError("isolated restore unexpectedly replaced a database")

    finished_at = datetime.now(UTC)
    rto_seconds = time.monotonic() - started
    rpo_met = rpo_seconds <= max_rpo_seconds
    rto_met = rto_seconds <= max_rto_seconds
    return {
        "bundle": str(bundle.resolve()),
        "backup_created_at": backup_at.isoformat().replace("+00:00", "Z"),
        "drill_started_at": started_at.isoformat().replace("+00:00", "Z"),
        "drill_finished_at": finished_at.isoformat().replace("+00:00", "Z"),
        "source_revision": manifest["source_revision"],
        "schema_version": metadata["schema_version"],
        "integrity_check": metadata["integrity_check"],
        "foreign_key_violations": metadata["foreign_key_violations"],
        "table_counts": metadata["table_counts"],
        "rpo_seconds": round(rpo_seconds, 3),
        "rto_database_seconds": round(rto_seconds, 3),
        "max_rpo_seconds": max_rpo_seconds,
        "max_rto_seconds": max_rto_seconds,
        "rpo_met": rpo_met,
        "rto_met": rto_met,
        "passed": rpo_met and rto_met,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify and time an isolated Argus database restore")
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--max-rpo-seconds", type=int, default=DEFAULT_MAX_RPO_SECONDS)
    parser.add_argument("--max-rto-seconds", type=int, default=DEFAULT_MAX_RTO_SECONDS)
    parser.add_argument("--report", type=Path, help="write JSON evidence without overwriting an existing file")
    args = parser.parse_args(argv)
    try:
        result = restore_drill(
            args.bundle,
            max_rpo_seconds=args.max_rpo_seconds,
            max_rto_seconds=args.max_rto_seconds,
        )
    except (BackupError, OSError, ValueError) as exc:
        print(f"restore drill failed: {exc}", file=sys.stderr)
        return 1
    if args.report is not None:
        try:
            write_report(result, args.report)
        except OSError as exc:
            print(f"restore drill report failed: {exc}", file=sys.stderr)
            return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
