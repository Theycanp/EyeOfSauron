#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

archive=""
release_id=""
checksum_file=""
install_root=${EOS_INSTALL_ROOT:-/opt/eyeofsauron}
config_path=${ARGUS_CONFIG:-/etc/argus/config.toml}
state_database=${ARGUS_DATABASE:-/var/lib/argus/state.db}
managed_config=${ARGUS_MANAGED_CONFIG:-/var/lib/argus/managed-sources.json}
backup_root=${EOS_BACKUP_ROOT:-/var/backups/eyeofsauron}
prepare_only=0

usage() {
  echo "usage: $0 --archive FILE --release-id ID [--checksum FILE] [--prepare-only]" >&2
}

while (($#)); do
  case "$1" in
    --archive) archive=$2; shift 2 ;;
    --release-id) release_id=$2; shift 2 ;;
    --checksum) checksum_file=$2; shift 2 ;;
    --prepare-only) prepare_only=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done
if ((EUID != 0)); then
  echo "release installation must run as root" >&2
  exit 2
fi
if [[ ! "$release_id" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
  echo "invalid or missing release ID" >&2
  exit 2
fi
archive=$(realpath "$archive")
[[ -f "$archive" ]] || { echo "release archive does not exist" >&2; exit 2; }
if [[ -n "$checksum_file" ]]; then
  checksum_file=$(realpath "$checksum_file")
  [[ -f "$checksum_file" ]] || { echo "checksum file does not exist" >&2; exit 2; }
  mapfile -t checksum_lines < <(awk 'NF { print }' "$checksum_file")
  if ((${#checksum_lines[@]} != 1)); then
    echo "checksum file must contain exactly one non-empty SHA-256 record" >&2
    exit 2
  fi
  expected_checksum=${checksum_lines[0]%%[[:space:]]*}
  if [[ ! "$expected_checksum" =~ ^[A-Fa-f0-9]{64}$ ]]; then
    echo "checksum file does not begin with a valid SHA-256 digest" >&2
    exit 2
  fi
  actual_checksum=$(sha256sum "$archive")
  actual_checksum=${actual_checksum%%[[:space:]]*}
  if [[ "${actual_checksum,,}" != "${expected_checksum,,}" ]]; then
    echo "release archive checksum mismatch" >&2
    exit 1
  fi
fi

lock_releases
releases="$install_root/releases"
target="$releases/$release_id"
install -d -m 0755 -o root -g root "$releases"
temporary=$(mktemp -d "$releases/.${release_id}.stage-XXXXXX")
trap 'rm -rf -- "$temporary"' EXIT
/usr/bin/python3 "$(dirname "${BASH_SOURCE[0]}")/unpack-release.py" "$archive" "$temporary"
release_root="$temporary/eyeofsauron"
"$release_root/scripts/release/preflight.sh" "$release_root" "$config_path"

manifest_release=$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["release_id"])' "$release_root/RELEASE.json")
[[ "$manifest_release" == "$release_id" ]] || {
  echo "release ID does not match RELEASE.json" >&2
  exit 1
}
if [[ -e "$target" || -L "$target" ]]; then
  if [[ ! -d "$target" || -L "$target" ]]; then
    echo "release target exists but is not a regular directory: $target" >&2
    exit 1
  fi
  if ((prepare_only)); then
    echo "release is already prepared: $target" >&2
    exit 1
  fi
  if ! diff --brief --recursive --no-dereference "$release_root" "$target" >/dev/null; then
    echo "prepared release content does not match the supplied archive: $target" >&2
    exit 1
  fi
else
  chown -R root:root "$release_root"
  chmod -R a-w "$release_root"
  mv "$release_root" "$target"
fi
rm -rf -- "$temporary"
trap - EXIT

if ((prepare_only)); then
  echo "prepared immutable release: $target"
  exit 0
fi

previous=""
if [[ -L "$install_root/current" ]]; then
  previous=$(readlink -f "$install_root/current")
  if [[ "$previous" == "$target" ]]; then
    echo "release is already active: $target" >&2
    exit 1
  fi
elif [[ -e "$install_root/current" ]]; then
  echo "$install_root/current exists but is not a symlink; complete the documented one-time layout migration first" >&2
  exit 1
else
  echo "activation requires a previous release at $install_root/current; complete the documented initial layout migration first" >&2
  exit 1
fi
[[ -d "$previous/src/argus" ]] || { echo "previous release is missing or incomplete" >&2; exit 1; }

services_stopped=0
rollback_required=0
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_bundle="$backup_root/pre-release-${release_id}-${timestamp}"
failed_state="$backup_root/failed-release-${release_id}-${timestamp}"

restore_previous() {
  exit_code=$?
  trap - EXIT
  set +e
  if ((rollback_required)); then
    echo "release activation failed; restoring prior code and state" >&2
    systemctl stop argus-admin argus || exit 1
    install -d -m 0700 -o root -g root "$failed_state" || exit 1
    for item in "$state_database" "$state_database-wal" "$state_database-shm" "$managed_config"; do
      if [[ -e "$item" ]]; then
        mv "$item" "$failed_state/" || exit 1
      fi
    done
    if ! PYTHONPATH="$target/src" /usr/bin/python3 -m argus.backup restore \
      "$backup_bundle" --database "$state_database"; then
      echo "database recovery failed; services remain stopped; backup: $backup_bundle" >&2
      exit 1
    fi
    chown argus:argus "$state_database" && chmod 0600 "$state_database" || exit 1
    if [[ -f "$backup_bundle/managed-sources.json" ]]; then
      install -m 0600 -o argus -g argus "$backup_bundle/managed-sources.json" "$managed_config" || exit 1
    fi
    if [[ -n "$previous" ]]; then
      rollback_link="$install_root/.current.rollback.$$"
      rm -f "$rollback_link"
      ln -s "$previous" "$rollback_link" && mv -Tf "$rollback_link" "$install_root/current" || exit 1
      restore_units "$backup_bundle/units" || exit 1
      systemctl start argus argus-admin || exit 1
    else
      rm -f "$install_root/current"
    fi
  elif ((services_stopped)) && [[ -n "$previous" ]]; then
    systemctl start argus argus-admin
  fi
  exit "$exit_code"
}
trap restore_previous EXIT

services_stopped=1
systemctl stop argus-admin argus
PYTHONPATH="$target/src" /usr/bin/python3 -m argus.backup backup \
  --database "$state_database" \
  --output "$backup_bundle" \
  --managed-config "$managed_config" \
  --source-revision "$release_id"
PYTHONPATH="$target/src" /usr/bin/python3 -m argus.backup verify \
  "$backup_bundle" >/dev/null
snapshot_units "$backup_bundle/units"
rollback_required=1

current_link="$install_root/.current.${release_id}.$$"
rm -f "$current_link"
ln -s "$target" "$current_link"
mv -Tf "$current_link" "$install_root/current"
for unit in "$target"/deploy/*.service "$target"/deploy/*.timer; do
  [[ -f "$unit" ]] || continue
  install -m 0644 -o root -g root "$unit" /etc/systemd/system/
done
systemctl daemon-reload
systemctl start argus argus-admin

if "$target/scripts/operations/health-gate.sh" --wait-seconds 120; then
  rollback_required=0
  services_stopped=0
  trap - EXIT
  echo "activated release $release_id; backup: $backup_bundle"
  exit 0
fi

echo "release health gate failed" >&2
exit 1
