#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$EOS_RELEASE_DRILL_ROOT/systemctl.log"
if [[ "$1" == start && "${DRILL_MUTATE_ON_START:-0}" == 1 &&
      "$(readlink -f "$EOS_INSTALL_ROOT/current")" == "$DRILL_CANDIDATE_PATH" ]]; then
  /usr/bin/python3 - "$ARGUS_DATABASE" <<'PY'
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as connection:
    connection.execute("INSERT INTO drill_state(value) VALUES ('failed activation')")
PY
fi
