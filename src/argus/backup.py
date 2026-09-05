from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote


BUNDLE_FORMAT_VERSION = 1
DATABASE_FILENAME = "state.db"
MANAGED_CONFIG_FILENAME = "managed-sources.json"
MANIFEST_FILENAME = "manifest.json"

_COUNTED_TABLES = (
    "observations",
    "alerts",
    "incidents",
    "collector_state",
    "reminders",
    "reminder_audit",
    "config_revisions",
    "config_audit",
)


class BackupError(RuntimeError):
    """Raised when a backup bundle cannot be created, verified, or restored."""


def _utc_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _readonly_uri(path: Path) -> str:
    return f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def inspect_database(path: Path, expected_schema: int | None = None) -> dict[str, Any]:
    """Validate one standalone SQLite file and return non-sensitive metadata."""

    if not path.is_file():
        raise BackupError(f"database file does not exist: {path}")
    try:
        connection = sqlite3.connect(_readonly_uri(path), uri=True, timeout=10.0)
    except sqlite3.Error as exc:
        raise BackupError(f"cannot open database: {type(exc).__name__}") from exc
    try:
        connection.execute("PRAGMA query_only=ON")
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise BackupError(f"database integrity check failed: {integrity[:200]}")
        foreign_key_violations = list(connection.execute("PRAGMA foreign_key_check"))
        if foreign_key_violations:
            raise BackupError(
                f"database foreign-key check found {len(foreign_key_violations)} violation(s)"
            )
        schema = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if expected_schema is not None and schema != expected_schema:
            raise BackupError(
                f"database schema {schema} does not match expected schema {expected_schema}"
            )
        existing_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        counts = {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in _COUNTED_TABLES
            if table in existing_tables
        }
        return {
            "schema_version": schema,
            "integrity_check": integrity,
            "foreign_key_violations": 0,
            "page_count": int(connection.execute("PRAGMA page_count").fetchone()[0]),
            "page_size": int(connection.execute("PRAGMA page_size").fetchone()[0]),
            "table_counts": counts,
        }
    except sqlite3.Error as exc:
        raise BackupError(f"database validation failed: {type(exc).__name__}") from exc
    finally:
        connection.close()


def create_backup(
    database: Path,
    output: Path,
    *,
    managed_config: Path | None = None,
    source_revision: str | None = None,
    expected_schema: int | None = None,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    """Create an atomically published backup bundle using SQLite Online Backup."""

    database = database.resolve()
    output = output.resolve()
    if not database.is_file():
        raise BackupError(f"source database does not exist: {database}")
    if output.exists():
        raise BackupError(f"backup output already exists: {output}")
    if timeout_seconds < 1:
        raise BackupError("backup timeout must be positive")

    parent_existed = output.parent.exists()
    output.parent.mkdir(parents=True, exist_ok=True)
    if not parent_existed:
        os.chmod(output.parent, stat.S_IRWXU)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=str(output.parent))
    )
    os.chmod(temporary, stat.S_IRWXU)
    snapshot = temporary / DATABASE_FILENAME
    started = time.monotonic()
    source: sqlite3.Connection | None = None
    destination: sqlite3.Connection | None = None
    try:
        try:
            source = sqlite3.connect(
                _readonly_uri(database), uri=True, timeout=min(timeout_seconds, 30)
            )
            destination = sqlite3.connect(snapshot)
        except sqlite3.Error as exc:
            raise BackupError(f"cannot initialize SQLite backup: {type(exc).__name__}") from exc
        try:
            assert source is not None and destination is not None
            source.execute(f"PRAGMA busy_timeout={min(timeout_seconds, 30) * 1000}")

            def progress(status: int, remaining: int, total: int) -> None:
                del status, remaining, total
                if time.monotonic() - started > timeout_seconds:
                    raise BackupError("SQLite backup exceeded its time limit")

            source.backup(destination, pages=256, progress=progress, sleep=0.05)
            destination.execute("PRAGMA journal_mode=DELETE")
            destination.execute("PRAGMA synchronous=FULL")
            destination.commit()
        except sqlite3.Error as exc:
            raise BackupError(f"SQLite backup failed: {type(exc).__name__}") from exc
        finally:
            if destination is not None:
                destination.close()
            if source is not None:
                source.close()

        os.chmod(snapshot, stat.S_IRUSR | stat.S_IWUSR)
        database_metadata = inspect_database(snapshot, expected_schema)

        managed_metadata: dict[str, Any] | None = None
        if managed_config is not None:
            managed_config = managed_config.resolve()
            if managed_config.exists():
                managed_copy = temporary / MANAGED_CONFIG_FILENAME
                shutil.copyfile(managed_config, managed_copy)
                try:
                    managed_value = json.loads(managed_copy.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise BackupError(
                        f"managed configuration is not valid JSON: {type(exc).__name__}"
                    ) from exc
                if not isinstance(managed_value, Mapping):
                    raise BackupError("managed configuration must contain a JSON object")
                os.chmod(managed_copy, stat.S_IRUSR | stat.S_IWUSR)
                _fsync_file(managed_copy)
                managed_metadata = {
                    "file": MANAGED_CONFIG_FILENAME,
                    "size_bytes": managed_copy.stat().st_size,
                    "sha256": _sha256(managed_copy),
                }

        _fsync_file(snapshot)
        manifest: dict[str, Any] = {
            "format_version": BUNDLE_FORMAT_VERSION,
            "application": "EyeOfSauron",
            "component": "Argus",
            "created_at": _utc_timestamp(),
            "source_revision": source_revision or "unknown",
            "database": {
                "file": DATABASE_FILENAME,
                "size_bytes": snapshot.stat().st_size,
                "sha256": _sha256(snapshot),
                **database_metadata,
            },
            "managed_config": managed_metadata,
        }
        manifest_path = temporary / MANIFEST_FILENAME
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(manifest_path, stat.S_IRUSR | stat.S_IWUSR)
        _fsync_file(manifest_path)
        _fsync_directory(temporary)
        os.replace(temporary, output)
        _fsync_directory(output.parent)
        return manifest
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _load_manifest(bundle: Path) -> dict[str, Any]:
    manifest_path = bundle / MANIFEST_FILENAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackupError(f"cannot read backup manifest: {type(exc).__name__}") from exc
    if not isinstance(manifest, dict):
        raise BackupError("backup manifest must contain a JSON object")
    if manifest.get("format_version") != BUNDLE_FORMAT_VERSION:
        raise BackupError("unsupported backup bundle format")
    return manifest


def verify_backup(bundle: Path) -> dict[str, Any]:
    """Verify bundle checksums plus SQLite integrity and row-count metadata."""

    bundle = bundle.resolve()
    if not bundle.is_dir():
        raise BackupError(f"backup bundle does not exist: {bundle}")
    manifest = _load_manifest(bundle)
    database_entry = manifest.get("database")
    if not isinstance(database_entry, Mapping):
        raise BackupError("backup manifest has no database entry")
    database_file = database_entry.get("file")
    if database_file != DATABASE_FILENAME:
        raise BackupError("backup manifest contains an unexpected database filename")
    snapshot = bundle / DATABASE_FILENAME
    if snapshot.stat().st_size != int(database_entry.get("size_bytes", -1)):
        raise BackupError("database size does not match the backup manifest")
    if _sha256(snapshot) != database_entry.get("sha256"):
        raise BackupError("database checksum does not match the backup manifest")
    actual = inspect_database(
        snapshot,
        int(database_entry["schema_version"]),
    )
    if actual["table_counts"] != database_entry.get("table_counts", {}):
        raise BackupError("database row counts do not match the backup manifest")

    managed_entry = manifest.get("managed_config")
    if managed_entry is not None:
        if not isinstance(managed_entry, Mapping):
            raise BackupError("managed-config manifest entry is invalid")
        managed_path = bundle / MANAGED_CONFIG_FILENAME
        if managed_path.stat().st_size != int(managed_entry.get("size_bytes", -1)):
            raise BackupError("managed configuration size does not match the manifest")
        if _sha256(managed_path) != managed_entry.get("sha256"):
            raise BackupError("managed configuration checksum does not match the manifest")
        try:
            managed_value = json.loads(managed_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BackupError("managed configuration in backup is invalid") from exc
        if not isinstance(managed_value, Mapping):
            raise BackupError("managed configuration in backup must be an object")
    return manifest


def restore_backup(
    bundle: Path,
    database: Path,
    *,
    replace: bool = False,
) -> dict[str, Any]:
    """Restore a verified standalone DB; existing WAL/SHM files are never overwritten."""

    manifest = verify_backup(bundle)
    database = database.resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    companions = [Path(f"{database}-wal"), Path(f"{database}-shm")]
    existing_companions = [path for path in companions if path.exists()]
    if existing_companions:
        raise BackupError(
            "refusing restore while SQLite WAL/SHM files exist; stop every writer and "
            "move the old database set aside first"
        )
    if database.exists() and not replace:
        raise BackupError("destination database already exists; use --replace after stopping writers")
    if database.exists() and not database.is_file():
        raise BackupError("destination database exists but is not a regular file")

    prior_stat = database.stat() if database.exists() else None
    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix=f".{database.name}.restore-", dir=str(database.parent)
    )
    os.close(temporary_fd)
    temporary = Path(temporary_name)
    rollback_path: Path | None = None
    try:
        shutil.copyfile(bundle.resolve() / DATABASE_FILENAME, temporary)
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        if prior_stat is not None and os.geteuid() == 0:
            os.chown(temporary, prior_stat.st_uid, prior_stat.st_gid)
        inspect_database(
            temporary,
            int(manifest["database"]["schema_version"]),
        )
        _fsync_file(temporary)
        if database.exists():
            rollback_path = database.with_name(
                f"{database.name}.before-restore-{_safe_timestamp()}"
            )
            if rollback_path.exists():
                raise BackupError(f"rollback destination already exists: {rollback_path}")
            os.replace(database, rollback_path)
        try:
            os.replace(temporary, database)
            _fsync_directory(database.parent)
        except Exception:
            if rollback_path is not None and rollback_path.exists() and not database.exists():
                os.replace(rollback_path, database)
            raise
        return {
            "database": str(database),
            "schema_version": manifest["database"]["schema_version"],
            "rollback_database": str(rollback_path) if rollback_path else None,
        }
    finally:
        temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="argus-backup",
        description="Create, verify, and stage restores of EyeOfSauron SQLite backup bundles.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    backup = commands.add_parser("backup", help="create an online, consistent backup bundle")
    backup.add_argument("--database", required=True, type=Path)
    backup.add_argument("--output", required=True, type=Path)
    backup.add_argument("--managed-config", type=Path)
    backup.add_argument("--source-revision")
    backup.add_argument("--expected-schema", type=int)
    backup.add_argument("--timeout-seconds", type=int, default=120)

    verify = commands.add_parser("verify", help="verify checksums and SQLite integrity")
    verify.add_argument("bundle", type=Path)

    restore = commands.add_parser("restore", help="restore into a stopped, WAL-free destination")
    restore.add_argument("bundle", type=Path)
    restore.add_argument("--database", required=True, type=Path)
    restore.add_argument("--replace", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "backup":
            result = create_backup(
                arguments.database,
                arguments.output,
                managed_config=arguments.managed_config,
                source_revision=arguments.source_revision,
                expected_schema=arguments.expected_schema,
                timeout_seconds=arguments.timeout_seconds,
            )
        elif arguments.command == "verify":
            result = verify_backup(arguments.bundle)
        elif arguments.command == "restore":
            result = restore_backup(
                arguments.bundle,
                arguments.database,
                replace=arguments.replace,
            )
        else:  # pragma: no cover - argparse enforces the command set
            raise AssertionError(f"unsupported command: {arguments.command}")
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (BackupError, OSError, sqlite3.Error) as exc:
        print(f"argus-backup: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
