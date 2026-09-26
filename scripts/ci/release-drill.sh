#!/usr/bin/env bash
set -euo pipefail

archive=${1:-}
release_id=${2:-}
if ((EUID != 0)) || [[ ! -f "$archive" || ! "$release_id" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
  echo "usage (as root): $0 ARCHIVE RELEASE_ID" >&2
  exit 2
fi
script_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
drill_root=$(mktemp -d "${RUNNER_TEMP:-/tmp}/eos-release-drill.XXXXXXXX")
trap 'rm -rf -- "$drill_root"' EXIT
touch "$drill_root/.eos-release-drill"
install -m 0755 "$script_root/scripts/ci/release-drill-systemctl.sh" "$drill_root/systemctl"
install -m 0755 "$script_root/scripts/ci/release-drill-health.sh" "$drill_root/health-gate.sh"

export EOS_RELEASE_DRILL_ROOT="$drill_root"
export EOS_INSTALL_ROOT="$drill_root/install"
export EOS_BACKUP_ROOT="$drill_root/backups"
export ARGUS_CONFIG="$drill_root/config.toml"
export ARGUS_DATABASE="$drill_root/state.db"
export ARGUS_MANAGED_CONFIG="$drill_root/managed-sources.json"
export ARGUS_PYTHON=${ARGUS_PYTHON:-/usr/bin/python3}
export DRILL_CANDIDATE_PATH="$EOS_INSTALL_ROOT/releases/$release_id"
previous_id="drill-previous"
previous="$EOS_INSTALL_ROOT/releases/$previous_id"

install -d "$EOS_INSTALL_ROOT/releases" "$EOS_BACKUP_ROOT" "$drill_root/stage" "$drill_root/systemd"
tar -xzf "$archive" -C "$drill_root/stage"
mv "$drill_root/stage/eyeofsauron" "$previous"
printf '\n# release drill previous unit\n' >>"$previous/deploy/argus.service"
install -m 0644 "$previous/deploy/argus.service" "$drill_root/systemd/argus.service"
ln -s "$previous" "$EOS_INSTALL_ROOT/current"

/usr/bin/python3 - "$script_root/config/argus.example.toml" "$ARGUS_CONFIG" \
  "$ARGUS_DATABASE" "$ARGUS_MANAGED_CONFIG" <<'PY'
import pathlib
import sys

source, output, database, managed = map(pathlib.Path, sys.argv[1:])
config = source.read_text(encoding="utf-8")
config = config.replace("/tmp/argus-development.db", str(database))
config = config.replace("/tmp/argus-managed-sources.json", str(managed))
output.write_text(config, encoding="utf-8")
PY
/usr/bin/python3 - "$ARGUS_DATABASE" "$previous/RELEASE.json" <<'PY'
import json
import sqlite3
import sys

schema = json.loads(open(sys.argv[2], encoding="utf-8").read())["database_schema"]
with sqlite3.connect(sys.argv[1]) as connection:
    connection.execute("CREATE TABLE drill_state(value TEXT NOT NULL)")
    connection.execute("INSERT INTO drill_state(value) VALUES ('baseline')")
    connection.execute("CREATE TABLE config_revisions (revision INTEGER, payload_json TEXT, active INTEGER)")
    connection.execute(f"PRAGMA user_version={int(schema)}")
PY
/usr/bin/python3 - "$previous/RELEASE.json" "$drill_root" <<'PY'
import json
import pathlib
import sys

manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
root = pathlib.Path(sys.argv[2])
status = {
    "database_schema": manifest["database_schema"],
    "desired_revision": None,
    "engine": {"state": "running", "heartbeat_age_seconds": 0, "applied_revision": None},
    "sources": [{
        "source_id": "bloomberg_markets", "runtime_status": "active",
        "last_success_at": 990, "last_attempt_at": 990, "consecutive_failures": 0,
    }],
    "outbox": {},
}
(root / "status.json").write_text(json.dumps(status), encoding="utf-8")
status["engine"]["heartbeat_age_seconds"] = 1000
(root / "status-unhealthy.json").write_text(json.dumps(status), encoding="utf-8")
PY
printf '%s\n' '{"revision": 1, "sources": [], "rules": []}' >"$ARGUS_MANAGED_CONFIG"

"$script_root/scripts/release/install-release.sh" \
  --archive "$archive" --release-id "$release_id" --checksum "$archive.sha256"
[[ "$(readlink -f "$EOS_INSTALL_ROOT/current")" == "$DRILL_CANDIDATE_PATH" ]]
[[ -d "$EOS_BACKUP_ROOT" ]]
[[ "$(tail -n 1 "$drill_root/health.log")" == "$DRILL_CANDIDATE_PATH" ]]
grep -Fq "ExecStart=$ARGUS_PYTHON -m argus" "$drill_root/systemd/argus.service"
grep -Fq "Environment=ARGUS_PYTHON=$ARGUS_PYTHON" "$drill_root/systemd/argus-watchdog.service"

"$script_root/scripts/release/rollback-release.sh" "$previous_id"
[[ "$(readlink -f "$EOS_INSTALL_ROOT/current")" == "$previous" ]]
[[ "$(tail -n 1 "$drill_root/health.log")" == "$previous" ]]
grep -Fq "ExecStart=$ARGUS_PYTHON -m argus" "$drill_root/systemd/argus.service"
cp "$drill_root/systemd/argus.service" "$drill_root/prior-unit.service"

sleep 1
export DRILL_FAIL_HEALTH=1 DRILL_MUTATE_ON_START=1
if "$script_root/scripts/release/install-release.sh" \
    --archive "$archive" --release-id "$release_id" --checksum "$archive.sha256"; then
  echo "release drill: unhealthy activation unexpectedly succeeded" >&2
  exit 1
fi
[[ "$(readlink -f "$EOS_INSTALL_ROOT/current")" == "$previous" ]]
cmp "$drill_root/prior-unit.service" "$drill_root/systemd/argus.service"
/usr/bin/python3 - "$ARGUS_DATABASE" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as connection:
    values = connection.execute("SELECT value FROM drill_state ORDER BY rowid").fetchall()
assert values == [("baseline",)], values
PY
[[ "$(tail -n 1 "$drill_root/health.log")" == "$DRILL_CANDIDATE_PATH" ]]
[[ $(find "$EOS_BACKUP_ROOT" -name manifest.json -type f | wc -l) -eq 3 ]]
[[ $(wc -l <"$drill_root/systemctl.log") -ge 12 ]]
echo "isolated release activation, health failure recovery, and explicit rollback passed"
