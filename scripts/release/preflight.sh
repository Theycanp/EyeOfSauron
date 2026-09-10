#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

release_root=${1:-}
config_path=${2:-/etc/argus/config.toml}
python_bin=${ARGUS_PYTHON:-/usr/bin/python3}
if [[ -z "$release_root" ]]; then
  echo "usage: $0 RELEASE_DIRECTORY [CONFIG_PATH]" >&2
  exit 2
fi
release_root=$(realpath "$release_root")
if [[ "$python_bin" != /* || ! -x "$python_bin" ]]; then
  echo "preflight: ARGUS_PYTHON must be an absolute path to an executable" >&2
  exit 2
fi

required=(
  RELEASE.json
  pyproject.toml
  LICENSE
  NOTICE
  THIRD_PARTY_NOTICES
  src/argus/__init__.py
  src/argus/admin_web/index.html
  deploy/argus.service
  deploy/argus-admin.service
  scripts/release/common.sh
  scripts/release/unpack-release.py
  scripts/operations/health-gate.sh
  scripts/operations/check_status.py
)
for relative in "${required[@]}"; do
  [[ -s "$release_root/$relative" ]] || {
    echo "preflight: required release file is missing: $relative" >&2
    exit 1
  }
done
if find "$release_root" -type l -print -quit | grep -q .; then
  echo "preflight: release archives may not contain symbolic links" >&2
  exit 1
fi
for extension in js css; do
  if [[ -z "$(find "$release_root/src/argus/admin_web/assets" -maxdepth 1 -type f -name "*.${extension}" -size +0c -print -quit)" ]]; then
    echo "preflight: compiled admin UI has no non-empty ${extension} asset" >&2
    exit 1
  fi
done
if find "$release_root" -xdev -perm /022 -print -quit | grep -q .; then
  echo "preflight: release contains group/world-writable files" >&2
  exit 1
fi

"$python_bin" - "$release_root" <<'PY'
import json
import re
import sys
import tomllib
from pathlib import Path

root = Path(sys.argv[1])
manifest = json.loads((root / "RELEASE.json").read_text(encoding="utf-8"))
project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
sys.path.insert(0, str(root / "src"))
from argus import __version__  # noqa: E402
from argus.database import SCHEMA_VERSION  # noqa: E402

expected = {
    "format_version": 1,
    "artifact_kind": "server-release",
    "product": "EyeOfSauron",
    "component": "Argus",
    "version": __version__,
    "database_schema": SCHEMA_VERSION,
    "license": project["license"],
}
for key, value in expected.items():
    if manifest.get(key) != value:
        raise SystemExit(
            f"preflight: RELEASE.json {key} is {manifest.get(key)!r}; expected {value!r}"
        )
if project.get("version") != __version__:
    raise SystemExit(
        "preflight: pyproject.toml and argus.__version__ must be updated together"
    )
if not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("commit", ""))):
    raise SystemExit("preflight: RELEASE.json does not contain a full Git commit SHA")
PY
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$release_root/src" "$python_bin" \
  -m argus --config "$config_path" check-config >/dev/null
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$release_root/src" "$python_bin" \
  -m argus.backup --help >/dev/null
unit_stage=$(mktemp -d)
trap 'rm -rf -- "$unit_stage"' EXIT
for unit in "$release_root"/deploy/*.service "$release_root"/deploy/*.timer; do
  [[ -f "$unit" ]] || continue
  sed "s#/opt/eyeofsauron/current#$release_root#g" "$unit" >"$unit_stage/$(basename "$unit")"
done
systemd-analyze verify "$unit_stage"/*.service "$unit_stage"/*.timer
rm -rf -- "$unit_stage"
trap - EXIT
echo "preflight passed: $release_root"
