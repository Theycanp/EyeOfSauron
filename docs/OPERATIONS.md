# Operations

## Paths

- Source project: `/home/joker/services/eyeofsauron`
- Active immutable release: `/opt/eyeofsauron/current`
- Configuration: `/etc/argus/config.toml`
- State database: `/var/lib/argus/state.db`
- Service unit: `/etc/systemd/system/argus.service`
- Managed sources/rules authority: revisioned records in `/var/lib/argus/state.db`
- Legacy configuration migration input: `/var/lib/argus/managed-sources.json`
- Management unit: `/etc/systemd/system/argus-admin.service`
- ntfy credentials: `/etc/argus/ntfy.env`, root-owned and never printed
- optional provider credentials: `/etc/argus/providers.env`, root-owned
- loopback-only emergency admin credential: `/etc/argus/admin.env`, root-owned
  and never logged or accepted through the public proxy
- admin UI build: `src/argus/admin_web/` (static React/Vite output)

## Commands

```bash
sudo systemctl status argus
sudo journalctl -u argus -n 100 --no-pager
sudo -u argus env PYTHONPATH=/opt/eyeofsauron/current/src \
  /usr/bin/python3 -m argus --config /etc/argus/config.toml status
```

The ntfy topic is `eos`. Subscribe to it on the existing ntfy server as
the existing `joker` account. The service uses a separate write-only ntfy user;
its token cannot subscribe or access other topics.

## Management backend

Install the unit from `deploy/argus-admin.service`, then check that `[admin]` is
enabled in `/etc/argus/config.toml`. Keep the application listener on loopback:

```bash
sudo install -m 0644 deploy/argus-admin.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now argus-admin
```

Create or reset the initial administrator from a local root shell. The helper
reads the password interactively or from a protected file; never place it in
the command line:

```bash
sudo -u argus env PYTHONPATH=/opt/eyeofsauron/current/src \
  /usr/bin/python3 /opt/eyeofsauron/current/scripts/operations/manage_admin_user.py joker
```

Install `deploy/nginx/eos-rate-limits.conf` under `/etc/nginx/conf.d/` and the
dedicated `deploy/nginx/eos.juggler.cc.conf` virtual host under
`/etc/nginx/sites-available/`, enable it, test Nginx, then reload. The public
origin is `https://eos.juggler.cc:10008`; it reuses the existing TLS port through
SNI while ntfy retains its own hostname. No firewall rule is added, and
`127.0.0.1:18080` remains unreachable from the network.
Install `deploy/certbot/nginx-reload` as an executable Certbot deploy hook so a
successfully renewed certificate is loaded only after `nginx -t` succeeds.

The browser signs in with an individual username and password. Its server-side
session has a fixed 14-day lifetime. The session cookie uses the browser-enforced
`__Host-` prefix and is Secure, HttpOnly, and SameSite Strict; changing a role,
disabling a user, resetting a password, or
using “force logout” revokes applicable sessions immediately. `admin` can manage
users and every setting, `operator` can manage sources, reminders, source
quality, and queue operations, and `viewer` is read-only. The final enabled
administrator cannot be disabled or demoted.

The API supports `GET /api/config`, `POST /api/source-bundles` (a source plus its
notification rule in one revision), the individual `POST /api/sources` and
`POST /api/rules` compatibility routes, and DELETE on the corresponding ID
paths. POST bodies are validated against the same configuration parser as the
service. SQLite stores the authoritative immutable revision; the old JSON file
is a one-time migration input, not a live export. Argus notices a desired
revision change, exits with status 75, and systemd starts a fresh process that
loads it. The UI may briefly show “waiting for Argus” until the new process
reports the revision as applied. The listener stays loopback-only. Cookie-backed
state changes require both a session-bound CSRF token and an exact same-origin
request. Login attempts are limited at both the reverse proxy and application;
the application tracks account/address and address-wide failures without storing
raw addresses. The legacy bearer credential works only on a direct loopback
request and the public reverse proxy removes incoming Authorization headers.

The same API also provides `POST /api/validate`, `POST /api/test-source`,
`GET /api/revisions`, `POST /api/revisions/<id>/rollback`,
`POST /api/sources/<id>/enable|disable`, `GET /api/incidents`, and `/metrics`.
Every accepted change receives a revision and audit record; rollback creates a
new revision rather than mutating history.

Analysis policy uses `POST /api/analysis`; Prompt versions use
`GET/POST /api/prompts`. Daily publication policy uses
`POST /api/digest-config`. The authenticated read surface is
`GET /api/digests` and `GET /api/digests/<digest-key>`; browser routes under
`/digests` support direct refresh. Configure `[digest].public_base_url` as the
externally reachable HTTPS origin before enabling notifications.

Set `[admin].public_base_url` to the externally reachable, credential-free HTTPS
origin of the administration service. Event-backed ntfy messages then use
`/events/<alert-id>` as their primary click target and expose the publisher URL
as a secondary `查看原文` action. The reader requires a normal administration
session; after login the original deep link is retained. Alerts without event
context, including reminders and digest publication notices, keep their existing
click targets. Changing this setting affects future deliveries only because ntfy
messages already accepted by the server cannot be rewritten.

Model secrets remain in `/etc/argus/providers.env`; configuration stores only
the variable name. Local model HTTP endpoints are restricted to loopback.
Remote endpoints require HTTPS. The default `shadow_mode = true` records model
advice without changing the deterministic result; disable shadow mode only
after reviewing model behavior and false positives.

The browser UI is compiled from `frontend/` with Node/Vite. Node and npm are
not required on the production host: the running service serves the compiled
files from `src/argus/admin_web/`. Keep the admin listener loopback-only.

The reminders panel writes directly to SQLite and does not require a service
restart. It supports a specific future date/time, a relative countdown, and a
daily time in an IANA timezone such as `Asia/Shanghai`. Reminder messages publish
to the configured `eos` ntfy topic. API routes are `GET/POST
/api/reminders`, `GET/DELETE /api/reminders/<id>`, and `POST
/api/reminders/<id>/enable|disable`. One-time reminders missed while the host is
offline are sent after recovery; daily downtime is coalesced to one missed
notification rather than replaying every elapsed day.

The emergency bearer credential is for local recovery only. Retrieve it only
when performing a direct loopback API repair; do not send it through chat or
store it in the project:

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
`argus`. Environment-file changes are outside the revision store, so restart the
main service after changing this file. Never paste the values into the web form,
managed export, logs, or this document.

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

The main unit uses `Type=notify`: Argus declares readiness only after database
setup and runtime registration, then emits watchdog heartbeats. Exit status 75
is reserved for controlled configuration activation and is explicitly treated
as restartable by systemd. Repeated startup failures remain bounded by the unit's
start-rate limit.

## Backup and restore

Create backups with the SQLite Online Backup API. The resulting private bundle
contains a standalone database, checksums, integrity results, row counts, schema
version, and an optional managed-source snapshot.

```bash
sudo install -d -m 0700 -o root -g root /var/backups/eyeofsauron
sudo env PYTHONPATH=/opt/eyeofsauron/current/src \
  /usr/bin/python3 -m argus.backup backup \
  --database /var/lib/argus/state.db \
  --output /var/backups/eyeofsauron/manual-YYYYMMDDTHHMMSSZ \
  --managed-config /var/lib/argus/managed-sources.json \
  --source-revision manual
sudo env PYTHONPATH=/opt/eyeofsauron/current/src \
  /usr/bin/python3 -m argus.backup verify /var/backups/eyeofsauron/manual-YYYYMMDDTHHMMSSZ
```

The online backup includes committed WAL transactions and does not require a
service outage. Take one before every release or configuration migration and at
least daily once the service contains information that cannot be reconstructed.
The initial operating objectives are RPO 24 hours and RTO 30 minutes.

`argus-backup.timer` runs daily at 03:15 UTC with up to ten minutes of jitter;
missed runs execute after boot. Enable it after confirming the first manual run:

```bash
sudo install -d -m 0700 /var/backups/eyeofsauron
sudo systemctl start argus-backup.service
sudo systemctl enable --now argus-backup.timer
```

Each run verifies its new online snapshot before retaining the latest 14 verified
bundles under `/var/backups/eyeofsauron/daily`. Only matching daily bundle names
inside that private directory are eligible for retention deletion. Symlinks,
unverifiable bundles, manual backups and pre-release recovery points are preserved.
The daily job shares the release lock, preventing a snapshot from overlapping a
code/database replacement. A collision fails visibly instead of racing the upgrade.
The service journal records verification failures; inspect timer and unit status
alongside the latest backup timestamp. These local backups protect against bad
changes, not complete loss of the host's storage.

Restore only during a maintenance window:

```bash
sudo systemctl stop argus-admin argus
sudo test ! -e /var/lib/argus/state.db-wal -a ! -e /var/lib/argus/state.db-shm
sudo env PYTHONPATH=/opt/eyeofsauron/current/src \
  /usr/bin/python3 -m argus.backup restore BACKUP_DIRECTORY \
  --database /var/lib/argus/state.db --replace
sudo chown argus:argus /var/lib/argus/state.db
sudo chmod 0600 /var/lib/argus/state.db
sudo systemctl start argus argus-admin
```

A restore refuses to run while WAL/SHM companions exist. With `--replace`, the
old database is renamed rather than overwritten; keep it until the restored
service passes the health gate. Restore `managed-sources.json` separately from
the verified bundle only for a legacy database without configuration revisions.
For schema 7 and later, restoring the database also restores the authoritative
configuration. The old JSON file is not regenerated; editing or restoring it
cannot override an existing SQLite revision. Export current settings using the
authenticated management API.

## Immutable releases and rollback

Release archives are built only from a clean Git commit. Prepare and activate
them under versioned, root-owned directories:

```bash
scripts/release/package-release.sh /tmp/eyeofsauron-RELEASE.tar.gz RELEASE
sudo scripts/release/install-release.sh --archive /tmp/eyeofsauron-RELEASE.tar.gz \
  --release-id RELEASE --checksum /tmp/eyeofsauron-RELEASE.tar.gz.sha256 \
  --prepare-only
sudo scripts/release/install-release.sh --archive /tmp/eyeofsauron-RELEASE.tar.gz \
  --release-id RELEASE --checksum /tmp/eyeofsauron-RELEASE.tar.gz.sha256
sudo scripts/release/rollback-release.sh PREVIOUS_RELEASE [PRE_RELEASE_BACKUP]
```

Activation stops both SQLite writers, creates and verifies a pre-release backup,
atomically switches `/opt/eyeofsauron/current`, installs matching unit templates,
and runs a bounded health gate. Any failed activation restores the previous
database, managed configuration, release symlink, units, and services. A schema
downgrade requires the matching pre-release bundle.

Release operations hold a host-local lock to prevent simultaneous activation or
rollback. The backup includes the exact installed service and timer unit files,
so recovery retains local changes to those files. If database or unit restoration
fails, recovery leaves both writers stopped and reports the recoverable backup
path. The health gate requires a fresh engine heartbeat, the applied SQLite
revision, active source workers and a bounded delivery backlog. Upstream outages
and old successful polls remain visible as warnings: restarting healthy workers
cannot repair an unavailable publisher.
An active worker that has not attempted a poll for three configured intervals
(at least 90 seconds) fails readiness even if the engine heartbeat is fresh.
Newly registered workers receive the same grace period before their first poll.

The current production installation predates the `current` symlink layout. Its
one-time migration must be performed in an explicit maintenance window with a
verified backup; the installer deliberately refuses to reinterpret a normal
directory as a symlink.

## Remove or disable

```bash
sudo systemctl disable --now argus
```

Disabling the unit leaves its code, configuration, and database recoverable.
Do not delete `/var/lib/argus` unless the alert history and feed baseline
are intentionally being discarded.
