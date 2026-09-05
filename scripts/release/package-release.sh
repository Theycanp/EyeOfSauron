#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
output=${1:-}
release_id=${2:-}

if [[ -z "$output" ]]; then
  echo "usage: $0 OUTPUT.tar.gz [RELEASE_ID]" >&2
  exit 2
fi
if [[ -n "$(git -C "$repo_root" status --porcelain)" ]]; then
  echo "release packaging requires a clean Git worktree" >&2
  exit 1
fi

commit=$(git -C "$repo_root" rev-parse --verify HEAD)
commit_epoch=$(git -C "$repo_root" show -s --format=%ct HEAD)
release_id=${release_id:-$(git -C "$repo_root" describe --tags --always --dirty)}
if [[ ! "$release_id" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
  echo "invalid release ID: $release_id" >&2
  exit 2
fi

output=$(realpath -m "$output")
if [[ -e "$output" || -L "$output" || -e "$output.sha256" || -L "$output.sha256" ]]; then
  echo "release output or checksum already exists; refusing to overwrite" >&2
  exit 1
fi
mkdir -p "$(dirname "$output")"
temporary=$(mktemp -d)
trap 'rm -rf -- "$temporary"' EXIT
stage="$temporary/eyeofsauron"
mkdir -p "$stage"
git -C "$repo_root" archive HEAD | tar -x -C "$stage"

PYTHONPATH="$stage/src" /usr/bin/python3 - "$stage" "$release_id" "$commit" "$commit_epoch" <<'PY'
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from argus.database import SCHEMA_VERSION
from argus import __version__

root = Path(sys.argv[1])
payload = {
    "format_version": 1,
    "artifact_kind": "server-release",
    "product": "EyeOfSauron",
    "component": "Argus",
    "release_id": sys.argv[2],
    "version": __version__,
    "commit": sys.argv[3],
    "license": "MIT",
    "python_requires": ">=3.12",
    "database_schema": SCHEMA_VERSION,
    "created_at": datetime.fromtimestamp(int(sys.argv[4]), UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "companion_artifacts": [
        "python-sbom.cdx.json",
        "frontend-sbom.cdx.json",
    ],
}
(root / "RELEASE.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

tar --sort=name --mtime="@$commit_epoch" --owner=0 --group=0 --numeric-owner \
  --format=posix --pax-option=delete=atime,delete=ctime -czf "$output" -C "$temporary" eyeofsauron
(
  cd "$(dirname "$output")"
  sha256sum "$(basename "$output")" >"$(basename "$output").sha256"
)
echo "$output"
