#!/usr/bin/env bash

# Keep the exact installed units, including local overrides in the unit files.
release_units=(argus.service argus-admin.service argus-heartbeat.service argus-heartbeat.timer argus-watchdog.service argus-watchdog.timer argus-backup.service argus-backup.timer)

release_unit_root=/etc/systemd/system
release_service_user=argus
release_python=${ARGUS_PYTHON:-/usr/bin/python3}
release_drill_root=${EOS_RELEASE_DRILL_ROOT:-}

configure_release_environment() {
  [[ -n "$release_drill_root" ]] || return 0
  [[ "$release_drill_root" == /* && -d "$release_drill_root" && ! -L "$release_drill_root" ]] || {
    echo "release drill requires an absolute, existing directory" >&2; exit 2;
  }
  release_drill_root=$(realpath -e "$release_drill_root")
  [[ "$release_drill_root" != / && -f "$release_drill_root/.eos-release-drill" && ! -L "$release_drill_root/.eos-release-drill" ]] || {
    echo "release drill marker is missing" >&2; exit 2;
  }
  local path resolved
  for path in "$install_root" "$backup_root" "$config_path" "$state_database" "$managed_config"; do
    [[ "$path" == /* ]] || { echo "release drill paths must be absolute" >&2; exit 2; }
    resolved=$(realpath -m "$path")
    [[ "$resolved" == "$release_drill_root/"* ]] || {
      echo "release drill path escapes its temporary root: $path" >&2; exit 2;
    }
  done
  for path in "$release_drill_root/systemctl" "$release_drill_root/health-gate.sh"; do
    [[ -f "$path" && -x "$path" && ! -L "$path" ]] || {
      echo "release drill command is missing or unsafe: $path" >&2; exit 2;
    }
  done
  release_unit_root="$release_drill_root/systemd"
  [[ ! -L "$release_unit_root" ]] || {
    echo "release drill unit directory must not be a symlink" >&2; exit 2;
  }
  release_service_user=root
  install -d -m 0755 "$release_unit_root"
}

release_systemctl() {
  if [[ -n "$release_drill_root" ]]; then
    "$release_drill_root/systemctl" "$@"
  else
    systemctl "$@"
  fi
}

release_health_gate() {
  local target=$1
  if [[ -n "$release_drill_root" ]]; then
    "$release_drill_root/health-gate.sh" --wait-seconds 120
  else
    "$target/scripts/operations/health-gate.sh" --wait-seconds 120
  fi
}

install_release_units() {
  local target=$1 unit
  [[ "$release_python" =~ ^/[A-Za-z0-9_./-]+$ && -x "$release_python" ]] || {
    echo "release Python must be an executable absolute path without shell metacharacters" >&2
    return 1
  }
  for unit in "$target"/deploy/*.service "$target"/deploy/*.timer; do
    [[ -f "$unit" ]] || continue
    if [[ "$unit" == *.service ]]; then
      sed -e "s#/usr/bin/python3#$release_python#g" \
          -e "/^\[Service\]$/a Environment=ARGUS_PYTHON=$release_python" \
          "$unit" >"$release_unit_root/$(basename "$unit")"
      chown root:root "$release_unit_root/$(basename "$unit")"
      chmod 0644 "$release_unit_root/$(basename "$unit")"
    else
      install -m 0644 -o root -g root "$unit" "$release_unit_root/"
    fi
  done
}

snapshot_units() {
  local snapshot=$1 unit
  install -d -m 0700 -o root -g root "$snapshot"
  for unit in "${release_units[@]}"; do
    if [[ -e "$release_unit_root/$unit" || -L "$release_unit_root/$unit" ]]; then
      cp -a -- "$release_unit_root/$unit" "$snapshot/$unit"
    fi
  done
}

restore_units() {
  local snapshot=$1 unit
  [[ -d "$snapshot" ]] || return 1
  for unit in "${release_units[@]}"; do
    rm -f -- "$release_unit_root/$unit" || return 1
    if [[ -e "$snapshot/$unit" || -L "$snapshot/$unit" ]]; then
      cp -a -- "$snapshot/$unit" "$release_unit_root/$unit" || return 1
    fi
  done
  release_systemctl daemon-reload
}

lock_releases() {
  install -d -m 0755 -o root -g root "$install_root"
  exec 8>"$install_root/.release.lock"
  flock -n 8 || { echo "another release operation is running" >&2; exit 1; }
}
