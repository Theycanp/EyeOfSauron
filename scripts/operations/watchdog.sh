#!/usr/bin/env bash
set -euo pipefail

failure_limit=${ARGUS_WATCHDOG_FAILURE_LIMIT:-3}
runtime_directory=${ARGUS_WATCHDOG_RUNTIME_DIR:-/run/argus-watchdog}
health_gate=${ARGUS_HEALTH_GATE:-/opt/eyeofsauron/current/scripts/operations/health-gate.sh}

if ((EUID != 0)); then
  echo "watchdog must run as root" >&2
  exit 2
fi
if [[ ! "$failure_limit" =~ ^[1-9][0-9]*$ ]]; then
  echo "ARGUS_WATCHDOG_FAILURE_LIMIT must be a positive integer" >&2
  exit 2
fi

install -d -m 0750 -o root -g root "$runtime_directory"
counter_file="$runtime_directory/failures"
lock_file="$runtime_directory/lock"
exec 9>"$lock_file"
flock -n 9 || exit 0

if "$health_gate" --wait-seconds 0 --source-age-multiplier 5 --skip-admin; then
  printf '0\n' >"$counter_file"
  exit 0
fi

failures=0
if [[ -r "$counter_file" ]]; then
  read -r failures <"$counter_file" || failures=0
fi
[[ "$failures" =~ ^[0-9]+$ ]] || failures=0
failures=$((failures + 1))
printf '%s\n' "$failures" >"$counter_file"
echo "argus watchdog failure ${failures}/${failure_limit}" >&2

if ((failures < failure_limit)); then
  exit 1
fi

printf '0\n' >"$counter_file"
systemctl restart argus
if ! "$health_gate" --wait-seconds 90 --source-age-multiplier 5 --skip-admin; then
  echo "argus watchdog restart did not restore health" >&2
  exit 1
fi
