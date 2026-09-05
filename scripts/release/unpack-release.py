from __future__ import annotations

import sys
import tarfile
from pathlib import Path, PurePosixPath


def unpack_release(archive: Path, destination: Path) -> None:
    """Extract bounded regular files without permitting links or path aliases."""
    seen: set[PurePosixPath] = set()
    total_size = 0
    with tarfile.open(archive, mode="r|gz") as bundle:
        for member in bundle:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or path.parts[:1] != ("eyeofsauron",):
                raise ValueError(f"unsafe release archive path: {member.name!r}")
            if not (member.isfile() or member.isdir()):
                raise ValueError(f"unsupported release archive member type: {member.name!r}")
            if path in seen or member.size < 0:
                raise ValueError(f"duplicate or invalid release archive member: {member.name!r}")
            seen.add(path)
            if len(seen) > 20_000:
                raise ValueError("release archive exceeds the 20,000 member limit")
            total_size += member.size
            if total_size > 512 * 1024 * 1024:
                raise ValueError("release archive expands beyond the 512 MiB safety limit")
            bundle.extract(member, destination, filter="data")
    if not seen:
        raise ValueError("release archive is empty")


if __name__ == "__main__":
    try:
        unpack_release(Path(sys.argv[1]), Path(sys.argv[2]))
    except (OSError, ValueError, tarfile.TarError) as exc:
        raise SystemExit(str(exc)) from exc
