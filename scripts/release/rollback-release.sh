#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

release_id=${1:-}
restore_bundle=${2:-}
install_root=${EOS_INSTALL_ROOT:-/opt/eyeofsauron}
config_path=${ARGUS_CONFIG:-/etc/argus/config.toml}
state_database=${ARGUS_DATABASE:-/var/lib/argus/state.db}
managed_config=${ARGUS_MANAGED_CONFIG:-/var/lib/argus/managed-sources.json}
backup_root=${EOS_BACKUP_ROOT:-/var/backups/eyeofsauron}

if ((EUID != 0)); then
  echo "rollback must run as root" >&2
  exit 2
fi
if [[ ! "$release_id" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
  echo "usage: $0 RELEASE_ID [PRE_RELEASE_BACKUP_BUNDLE]" >&2
  exit 2
fi
lock_releases
target="$install_root/releases/$release_id"
[[ -d "$target" && ! -L "$target" ]] || { echo "release not found or is a symlink: $target" >&2; exit 1; }
"$target/scripts/release/preflight.sh" "$target" "$config_path"
if [[ ! -L "$install_root/current" ]]; then
  echo "rollback requires an active release symlink at $install_root/current" >&2
  exit 1
fi
previous=$(readlink -f "$install_root/current")
[[ -d "$previous/src/argus" ]] || { echo "active release is missing or incomplete" >&2; exit 1; }

target_schema=$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["database_schema"])' "$target/RELEASE.json")
live_schema=$(/usr/bin/python3 -c 'import sqlite3,sys; c=sqlite3.connect("file:"+sys.argv[1]+"?mode=ro",uri=True); print(c.execute("pragma user_version").fetchone()[0])' "$state_database")
if ((live_schema > target_schema)) && [[ -z "$restore_bundle" ]]; then
  echo "target supports schema $target_schema but live database is $live_schema; provide its pre-release backup bundle" >&2
  exit 1
fi

services_stopped=0
rollback_required=0

restore_current() {
  exit_code=$?
  trap - EXIT
  set +e
  if ((rollback_required)); then
    echo "rollback failed; restoring the previously active release and state" >&2
    systemctl stop argus-admin argus || exit 1
    failed_state="$backup_root/failed-rollback-${release_id}-${timestamp}"
    install -d -m 0700 -o root -g root "$failed_state" || exit 1
    for item in "$state_database" "$state_database-wal" "$state_database-shm" "$managed_config"; do
      if [[ -e "$item" ]]; then
        mv "$item" "$failed_state/" || exit 1
      fi
    done
    if ! PYTHONPATH="$previous/src" /usr/bin/python3 -m argus.backup restore \
      "$safety_backup" --database "$state_database"; then
      echo "database recovery failed; services remain stopped; backup: $safety_backup" >&2
      exit 1
    fi
    chown argus:argus "$state_database" && chmod 0600 "$state_database" || exit 1
    if [[ -f "$safety_backup/managed-sources.json" ]]; then
      install -m 0600 -o argus -g argus "$safety_backup/managed-sources.json" "$managed_config" || exit 1
    fi
    recovery_link="$install_root/.current.recover.$$"
    rm -f "$recovery_link"
    ln -s "$previous" "$recovery_link" && mv -Tf "$recovery_link" "$install_root/current" || exit 1
    restore_units "$safety_backup/units" || exit 1
    systemctl start argus argus-admin || exit 1
  elif ((services_stopped)); then
    systemctl start argus argus-admin
  fi
  exit "$exit_code"
}
trap restore_current EXIT

services_stopped=1
systemctl stop argus-admin argus
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
safety_backup="$backup_root/pre-rollback-${release_id}-${timestamp}"
PYTHONPATH="$previous/src" /usr/bin/python3 -m argus.backup backup \
  --database "$state_database" --output "$safety_backup" \
  --managed-config "$managed_config" --source-revision "before-rollback-to-$release_id"
PYTHONPATH="$previous/src" /usr/bin/python3 -m argus.backup verify \
  "$safety_backup" >/dev/null
snapshot_units "$safety_backup/units"
rollback_required=1

if [[ -n "$restore_bundle" ]]; then
  restore_bundle=$(realpath "$restore_bundle")
  PYTHONPATH="$previous/src" /usr/bin/python3 -m argus.backup verify "$restore_bundle" >/dev/null
  restore_schema=$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["database"]["schema_version"])' "$restore_bundle/manifest.json")
  if ((restore_schema > target_schema)); then
    echo "restore bundle schema $restore_schema is newer than target schema $target_schema" >&2
    exit 1
  fi
  displaced="$backup_root/displaced-rollback-${release_id}-${timestamp}"
  install -d -m 0700 -o root -g root "$displaced"
  for item in "$state_database" "$state_database-wal" "$state_database-shm" "$managed_config"; do
    [[ -e "$item" ]] && mv "$item" "$displaced/"
  done
  PYTHONPATH="$target/src" /usr/bin/python3 -m argus.backup restore \
    "$restore_bundle" --database "$state_database"
  chown argus:argus "$state_database"
  chmod 0600 "$state_database"
  if [[ -f "$restore_bundle/managed-sources.json" ]]; then
    install -m 0600 -o argus -g argus "$restore_bundle/managed-sources.json" "$managed_config"
  fi
fi

rollback_link="$install_root/.current.rollback.$$"
rm -f "$rollback_link"
ln -s "$target" "$rollback_link"
mv -Tf "$rollback_link" "$install_root/current"
for unit in "$target"/deploy/*.service "$target"/deploy/*.timer; do
  [[ -f "$unit" ]] || continue
  install -m 0644 -o root -g root "$unit" /etc/systemd/system/
done
systemctl daemon-reload
systemctl start argus argus-admin
"$target/scripts/operations/health-gate.sh" --wait-seconds 120
rollback_required=0
services_stopped=0
trap - EXIT
echo "rolled back to $release_id; safety backup: $safety_backup"
