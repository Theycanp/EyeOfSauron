# Operations

## Paths

- Source project: `/home/joker/services/eyeofsauron`
- Installed code: `/opt/eyeofsauron`
- Configuration: `/etc/argus/config.toml`
- State database: `/var/lib/argus/state.db`
- Service unit: `/etc/systemd/system/argus.service`
- Managed sources/rules: `/var/lib/argus/managed-sources.json`
- Management unit: `/etc/systemd/system/argus-admin.service`
- ntfy credentials: `/etc/argus/ntfy.env`, root-owned and never printed
- optional provider credentials: `/etc/argus/providers.env`, root-owned
- admin credential: `/etc/argus/admin.env`, root-owned and never logged
- admin UI build: `src/argus/admin_web/` (static React/Vite output)

## Commands

```bash
sudo systemctl status argus
sudo journalctl -u argus -n 100 --no-pager
sudo -u argus env PYTHONPATH=/opt/eyeofsauron/src \
  /usr/bin/python3 -m argus --config /etc/argus/config.toml status
```

The ntfy topic is `eos`. Subscribe to it on the existing ntfy server as
the existing `joker` account. The service uses a separate write-only ntfy user;
its token cannot subscribe or access other topics.

## Management backend

Install the unit from `deploy/argus-admin.service`, then check that
`[admin]` is enabled in `/etc/argus/config.toml`:

```bash
sudo install -m 0644 deploy/argus-admin.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now argus-admin
ssh -N -L 18080:127.0.0.1:18080 joker@bk
```

Open `http://127.0.0.1:18080/` on the client running the SSH tunnel. The API
supports `GET /api/config`, `POST /api/source-bundles` (a source plus its
notification rule in one revision), the individual `POST /api/sources` and
`POST /api/rules` compatibility routes, and DELETE on the corresponding ID
paths. POST bodies are validated against the same
configuration parser as the service. Changes report `restart_required`; apply
them with `sudo systemctl restart argus` after reviewing the managed
configuration. The listener is loopback-only and must not be reverse proxied
to the public ntfy endpoint.

The same API also provides `POST /api/validate`, `POST /api/test-source`,
`GET /api/revisions`, `POST /api/revisions/<id>/rollback`,
`POST /api/sources/<id>/enable|disable`, `GET /api/incidents`, and `/metrics`.
Every accepted change receives a revision and audit record; rollback creates a
new revision rather than mutating history.

The browser UI is compiled from `frontend/` with Node/Vite. Node and npm are
not required on the production host: the running service serves the compiled
files from `src/argus/admin_web/`. Keep the admin listener loopback-only
and continue using the SSH tunnel above.

The reminders panel writes directly to SQLite and does not require a service
restart. It supports a specific future date/time, a relative countdown, and a
daily time in an IANA timezone such as `Asia/Shanghai`. Reminder messages publish
to the configured `eos` ntfy topic. API routes are `GET/POST
/api/reminders`, `GET/DELETE /api/reminders/<id>`, and `POST
/api/reminders/<id>/enable|disable`. One-time reminders missed while the host is
offline are sent after recovery; daily downtime is coalesced to one missed
notification rather than replaying every elapsed day.

Retrieve the one-time browser credential locally when needed; do not send it
through chat or store it in the project:

```bash
sudo sed -n 's/^ARGUS_ADMIN_TOKEN=//p' /etc/argus/admin.env
```

For a stock source, use `kind = "market"` and put symbols and thresholds in
`settings`: `symbols`, `price_change_threshold`, `gap_threshold`,
`volume_multiplier`, and `cooldown_seconds`. Keep provider secrets in a root
owned environment file and reference only its variable names. X sources must
use the platform numeric `user_id`; YouTube sources use `channel_id`; IMAP
sources use `username_env` and `password_env`.

Host checks can be enabled with `kind = "host"` and a `settings` table containing
`paths`, `units`, `disk_used_percent`, `inode_used_percent`,
`memory_used_percent`, `load1`, `allowed_listen_ports`, and
`required_listen_ports`. This probe is
transition-based and intentionally low frequency; use node_exporter,
smartctl_exporter and blackbox_exporter for continuous metrics.

Provider variables referenced by managed sources belong in
`/etc/argus/providers.env` with mode `0640`, owner `root`, and group
`argus`. Restart the main service after changing this file. Never paste
the values into the web form, managed JSON, logs, or this document.

## Optional heartbeat and devices

`argus.heartbeat.HeartbeatSender` is outbound-only and intentionally not
enabled by the production config. A systemd timer can call it with a URL and
token from a root-owned environment file; it sends no host metadata. The MQTT
module normalizes ESPHome/Zigbee2MQTT state messages and validates device
commands against an explicit device/action allowlist, expiry, and idempotency
key. Add Mosquitto/Home Assistant only after selecting the broker and device
fail-safe behavior; do not expose a generic MQTT or control port publicly.

## Safe restart

```bash
sudo systemctl restart argus
sudo systemctl is-active argus
```

Pending outbox rows and collector baselines survive a restart. Restarting does
not cause the current feed contents or an already-enqueued reminder occurrence
to be sent again. A publish acknowledged by ntfy immediately before a process
crash can still be repeated after its lease expires, by the documented
at-least-once policy.

## Backup and restore

Stop the service before a byte-for-byte database backup:

```bash
sudo systemctl stop argus
sudo cp -a /var/lib/argus/state.db /var/lib/argus/state.db.backup
sudo systemctl start argus
```

Configuration and source code contain no credentials. Preserve the private ntfy
credential file separately as part of host secret management.

## Remove or roll back

```bash
sudo systemctl disable --now argus
```

Disabling the unit leaves its code, configuration, and database recoverable.
Do not delete `/var/lib/argus` unless the alert history and feed baseline
are intentionally being discarded.
