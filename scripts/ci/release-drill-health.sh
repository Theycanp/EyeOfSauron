#!/usr/bin/env bash
set -euo pipefail
[[ "${1:-}" == --wait-seconds && "${2:-}" == 120 ]]
printf '%s\n' "$(readlink -f "$EOS_INSTALL_ROOT/current")" >>"$EOS_RELEASE_DRILL_ROOT/health.log"
status_file="$EOS_RELEASE_DRILL_ROOT/status.json"
if [[ "${DRILL_FAIL_HEALTH:-0}" == 1 &&
      "$(readlink -f "$EOS_INSTALL_ROOT/current")" == "$DRILL_CANDIDATE_PATH" ]]; then
  status_file="$EOS_RELEASE_DRILL_ROOT/status-unhealthy.json"
fi
PYTHONPATH="$EOS_INSTALL_ROOT/current/src" /usr/bin/python3 \
  "$EOS_INSTALL_ROOT/current/scripts/operations/check_status.py" \
  --status-file "$status_file" --config "$ARGUS_CONFIG" --now 1000
