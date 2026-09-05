#!/usr/bin/env bash

# Keep the exact installed units, including local overrides in the unit files.
release_units=(argus.service argus-admin.service argus-heartbeat.service argus-heartbeat.timer argus-watchdog.service argus-watchdog.timer argus-backup.service argus-backup.timer)

snapshot_units() {
  local snapshot=$1 unit
  install -d -m 0700 -o root -g root "$snapshot"
  for unit in "${release_units[@]}"; do
    if [[ -e "/etc/systemd/system/$unit" || -L "/etc/systemd/system/$unit" ]]; then
      cp -a -- "/etc/systemd/system/$unit" "$snapshot/$unit"
    fi
  done
}

restore_units() {
  local snapshot=$1 unit
  [[ -d "$snapshot" ]] || return 1
  for unit in "${release_units[@]}"; do
    rm -f -- "/etc/systemd/system/$unit" || return 1
    if [[ -e "$snapshot/$unit" || -L "$snapshot/$unit" ]]; then
      cp -a -- "$snapshot/$unit" "/etc/systemd/system/$unit" || return 1
    fi
  done
  systemctl daemon-reload
}

lock_releases() {
  install -d -m 0755 -o root -g root "$install_root"
  exec 8>"$install_root/.release.lock"
  flock -n 8 || { echo "another release operation is running" >&2; exit 1; }
}
