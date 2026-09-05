#!/usr/bin/env bash
set -euo pipefail

install_root=${EOS_INSTALL_ROOT:-/opt/eyeofsauron}
config_path=${ARGUS_CONFIG:-/etc/argus/config.toml}
service_user=${ARGUS_SERVICE_USER:-argus}
wait_seconds=0
source_age_multiplier=4
minimum_source_age=300
max_pending=1000
check_admin=1

usage() {
  echo "usage: $0 [--wait-seconds N] [--source-age-multiplier N] [--minimum-source-age N] [--max-pending N] [--skip-admin]" >&2
}

while (($#)); do
  case "$1" in
    --wait-seconds) wait_seconds=$2; shift 2 ;;
    --source-age-multiplier) source_age_multiplier=$2; shift 2 ;;
    --minimum-source-age) minimum_source_age=$2; shift 2 ;;
    --max-pending) max_pending=$2; shift 2 ;;
    --skip-admin) check_admin=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done
for value in "$wait_seconds" "$source_age_multiplier" "$minimum_source_age" "$max_pending"; do
  [[ "$value" =~ ^[0-9]+$ && ${#value} -le 8 ]] || {
    echo "health gate numeric options must be non-negative integers below 100000000" >&2
    exit 2
  }
done

current_root="$install_root/current"
if [[ ! -d "$current_root/src/argus" ]]; then
  echo "health gate: current release is missing under $current_root" >&2
  exit 1
fi

deadline=$((SECONDS + wait_seconds))
last_error="health gate did not run"
while :; do
  status_file=$(mktemp)
  trap 'rm -f "$status_file"' EXIT
  if ! systemctl is-active --quiet argus; then
    last_error="argus.service is not active"
  elif ! timeout --kill-after=5 20 runuser -u "$service_user" -- env \
      PYTHONPATH="$current_root/src" PYTHONDONTWRITEBYTECODE=1 \
      /usr/bin/python3 -m argus --config "$config_path" status --json >"$status_file"; then
    last_error="argus status command failed"
  elif ! PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$current_root/src" timeout --kill-after=5 20 /usr/bin/python3 \
      "$current_root/scripts/operations/check_status.py" \
      --status-file "$status_file" \
      --config "$config_path" \
      --source-age-multiplier "$source_age_multiplier" \
      --minimum-source-age "$minimum_source_age" \
      --max-pending "$max_pending"; then
    last_error="persisted status failed the reliability gate"
  elif ((check_admin)) && ! systemctl is-active --quiet argus-admin; then
    last_error="argus-admin.service is not active"
  elif ((check_admin)); then
    http_status=$(curl --silent --show-error --output /dev/null \
      --write-out '%{http_code}' --max-time 5 http://127.0.0.1:18080/api/health || true)
    if [[ "$http_status" != 200 && "$http_status" != 401 ]]; then
      last_error="argus-admin health endpoint returned HTTP ${http_status:-000}"
    else
      rm -f "$status_file"
      trap - EXIT
      exit 0
    fi
  else
    rm -f "$status_file"
    trap - EXIT
    exit 0
  fi

  rm -f "$status_file"
  trap - EXIT
  if ((SECONDS >= deadline)); then
    echo "health gate: $last_error" >&2
    exit 1
  fi
  sleep 5
done
