# Operations

## Paths

- Source project: `/home/joker/services/signalwatch`
- Installed code: `/opt/signalwatch`
- Configuration: `/etc/signalwatch/config.toml`
- State database: `/var/lib/signalwatch/state.db`
- Service unit: `/etc/systemd/system/signalwatch.service`
- Managed sources/rules: `/var/lib/signalwatch/managed-sources.json`
- Management unit: `/etc/systemd/system/signalwatch-admin.service`
- ntfy credentials: `/etc/signalwatch/ntfy.env`, root-owned and never printed
- optional provider credentials: `/etc/signalwatch/providers.env`, root-owned
- admin credential: `/etc/signalwatch/admin.env`, root-owned and never logged

## Commands

```bash
sudo systemctl status signalwatch
sudo journalctl -u signalwatch -n 100 --no-pager
sudo -u signalwatch env PYTHONPATH=/opt/signalwatch/src \
  /usr/bin/python3 -m signalwatch --config /etc/signalwatch/config.toml status
```

The ntfy topic is `signalwatch`. Subscribe to it on the existing ntfy server as
the existing `joker` account. The service uses a separate write-only ntfy user;
its token cannot subscribe or access other topics.

## Management backend

Install the unit from `deploy/signalwatch-admin.service`, then check that
`[admin]` is enabled in `/etc/signalwatch/config.toml`:

```bash
sudo install -m 0644 deploy/signalwatch-admin.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now signalwatch-admin
ssh -N -L 18080:127.0.0.1:18080 joker@bk
```

Open `http://127.0.0.1:18080/` on the client running the SSH tunnel. The API
supports `GET /api/config`, `POST /api/sources`, `POST /api/rules`, and DELETE
on the corresponding ID paths. POST bodies are validated against the same
configuration parser as the service. Changes report `restart_required`; apply
them with `sudo systemctl restart signalwatch` after reviewing the managed
configuration. The listener is loopback-only and must not be reverse proxied
to the public ntfy endpoint.

Retrieve the one-time browser credential locally when needed; do not send it
through chat or store it in the project:

```bash
sudo sed -n 's/^SIGNALWATCH_ADMIN_TOKEN=//p' /etc/signalwatch/admin.env
```

For a stock source, use `kind = "market"` and put symbols and thresholds in
`settings`: `symbols`, `price_change_threshold`, `gap_threshold`,
`volume_multiplier`, and `cooldown_seconds`. Keep provider secrets in a root
owned environment file and reference only its variable names. X sources must
use the platform numeric `user_id`; YouTube sources use `channel_id`; IMAP
sources use `username_env` and `password_env`.

Host checks can be enabled with `kind = "host"` and a `settings` table containing
`paths`, `units`, `disk_used_percent`, `inode_used_percent`,
`memory_used_percent`, `load1`, and `allowed_listen_ports`. This probe is
transition-based and intentionally low frequency; use node_exporter,
smartctl_exporter and blackbox_exporter for continuous metrics.

Provider variables referenced by managed sources belong in
`/etc/signalwatch/providers.env` with mode `0640`, owner `root`, and group
`signalwatch`. Restart the main service after changing this file. Never paste
the values into the web form, managed JSON, logs, or this document.

## Optional heartbeat and devices

`signalwatch.heartbeat.HeartbeatSender` is outbound-only and intentionally not
enabled by the production config. A systemd timer can call it with a URL and
token from a root-owned environment file; it sends no host metadata. The MQTT
module normalizes ESPHome/Zigbee2MQTT state messages and validates device
commands against an explicit device/action allowlist, expiry, and idempotency
key. Add Mosquitto/Home Assistant only after selecting the broker and device
fail-safe behavior; do not expose a generic MQTT or control port publicly.

## Safe restart

```bash
sudo systemctl restart signalwatch
sudo systemctl is-active signalwatch
```

Pending outbox rows and collector baselines survive a restart. Restarting does
not cause the current feed contents to be sent again.

## Backup and restore

Stop the service before a byte-for-byte database backup:

```bash
sudo systemctl stop signalwatch
sudo cp -a /var/lib/signalwatch/state.db /var/lib/signalwatch/state.db.backup
sudo systemctl start signalwatch
```

Configuration and source code contain no credentials. Preserve the private ntfy
credential file separately as part of host secret management.

## Remove or roll back

```bash
sudo systemctl disable --now signalwatch
```

Disabling the unit leaves its code, configuration, and database recoverable.
Do not delete `/var/lib/signalwatch` unless the alert history and feed baseline
are intentionally being discarded.
