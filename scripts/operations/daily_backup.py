from __future__ import annotations

import argparse
import fcntl
import os
import re
import shutil
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from argus import __version__
from argus.backup import BackupError, create_backup, verify_backup


BUNDLE_NAME = re.compile(r"daily-\d{8}T\d{6}Z-[0-9a-f]{8}\Z")


def daily_backup(database: Path, root: Path, managed_config: Path | None, keep: int = 14) -> Path:
    if not 1 <= keep <= 365:
        raise ValueError("daily backup retention must be between 1 and 365")
    if not root.is_absolute() or root.name != "daily" or root.parent.name != "eyeofsauron":
        raise ValueError("backup root must be an absolute eyeofsauron/daily directory")
    if any(path.is_symlink() for path in (root, *root.parents)):
        raise ValueError("backup root cannot contain symbolic links")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_uid != os.geteuid():
        raise ValueError("backup root must be owned by the backup process user")
    root.chmod(0o700)
    descriptor = os.open(root / ".backup.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output = root / f"daily-{stamp}-{uuid.uuid4().hex[:8]}"
        create_backup(
            database, output, managed_config=managed_config,
            source_revision=f"scheduled:{__version__}", timeout_seconds=120,
        )
        verify_backup(output)
        candidates: list[Path] = []
        for path in root.iterdir():
            if not BUNDLE_NAME.fullmatch(path.name) or path.is_symlink() or not path.is_dir():
                continue
            if path.stat().st_uid != os.geteuid():
                continue
            try:
                verify_backup(path)
            except (BackupError, OSError, ValueError, KeyError, TypeError):
                print(f"preserving unverifiable backup: {path.name}", file=sys.stderr)
                continue
            candidates.append(path)
        # Delete only verified daily bundles in this private, locked directory.
        older = sorted((path for path in candidates if path != output), reverse=True)
        for path in older[max(0, keep - 1):]:
            shutil.rmtree(path)
        return output
    finally:
        os.close(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser(description="Create and retain verified daily Argus backups")
    parser.add_argument("--database", type=Path, default=Path("/var/lib/argus/state.db"))
    parser.add_argument("--root", type=Path, default=Path("/var/backups/eyeofsauron/daily"))
    parser.add_argument("--managed-config", type=Path, default=Path("/var/lib/argus/managed-sources.json"))
    parser.add_argument("--keep", type=int, default=14)
    parser.add_argument("--release-lock", type=Path, default=Path("/opt/eyeofsauron/.release.lock"))
    args = parser.parse_args()
    try:
        descriptor = os.open(args.release_lock, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            output = daily_backup(args.database, args.root, args.managed_config, args.keep)
        finally:
            os.close(descriptor)
    except (BackupError, OSError, ValueError) as exc:
        print(f"daily backup failed: {exc}", file=sys.stderr)
        return 1
    print(f"verified daily backup: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
