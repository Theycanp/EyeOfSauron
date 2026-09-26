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

Local weather is managed at `/#/weather` on the same public admin origin.
`GET /api/weather` returns the subscription, last good forecast, last success,
and failure count. `GET /api/weather/places?q=...` searches locations; both
require login. `POST /api/weather` needs `settings:write`, same-origin, CSRF,
and the current weather revision. The two notification toggles can be disabled
independently without disabling collection. The default location and exact
alert rules and official-warning coverage limits are in
`WEATHER.md`. After a weather schema rollout, verify a real `weather_poll_succeeded`
log line and the Weather page's success timestamp; do not simulate a severe
weather warning on the production ntfy topic.
When QWeather is configured, also verify independent `minutely`, `alerts` and
`astronomy`
success timestamps and zero unexplained failures. Keep JWT identifiers and the
private-key path in `/etc/argus/qweather.env` (root:argus 0640), loaded only by
the daemon. See `WEATHER.md` for variable names and `RELEASES.md` for selecting
the locked, root-owned runtime using `ARGUS_PYTHON`.
On explicit operator request, `argus weather-test` queues one labelled current
weather snapshot through the normal outbox. It refuses missing or stale forecasts
and does not alter the daily/rain alert state; verify delivery in outbox status.

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
users and every setting, `operator` can manage sources, reminders, events, source
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

Security review (2026-09-21, source review and regression tests): these controls
already exist; they must not be described as future additions or replaced by
another login system. The cookie is `SameSite=Strict`, not Lax. The readable
CSRF cookie is intentionally distinct from the HttpOnly session cookie; the
server binds its hash to the session. Password hashes and raw session tokens
are never returned in user APIs. Source diagnostics and quality-audit reads
require an authenticated reader; changes still require the corresponding write
permission and CSRF/origin checks. This review does not change the 14-day login
lifetime or force users to sign in again. MFA, fresh-password confirmation for
sensitive actions and automatic hash-cost upgrades are not currently implemented
and must not be claimed as deployed controls. Any future password-cost upgrade
should use a conditional hash update after successful verification, preserving
concurrent password resets and existing valid sessions.

The same API also provides `POST /api/validate`, `POST /api/test-source`,
`GET /api/revisions`, `POST /api/revisions/<id>/rollback`,
`POST /api/sources/<id>/enable|disable`, `GET /api/incidents`, and `/metrics`.
Every accepted change receives a revision and audit record; rollback creates a
new revision rather than mutating history.

`POST /api/events` accepts an authenticated manual event with a title, detailed
summary, importance from 1 to 5, region, topic, and an optional credential-free
HTTPS source URL. It requires `events:write` plus the normal same-origin and
CSRF checks. The repository atomically stores the observation, recorded event,
and pending outbox alert; a successful `201` response returns the same detail
document used by `/events/<alert-id>`. These events are immediately eligible
for ntfy delivery and remain available to the daily digest. Do not use this API
for recurring reminders or recoverable health incidents.

Do not confuse the manual-event command or `/events/<alert-id>` alert reader
with the unified daily event pool. The read-only pool uses
`GET /api/news-events?hours=28&sort=newest&limit=50`; `hours` is adjustable,
`sort` accepts `newest` or `importance`, and subsequent requests pass the opaque
`pagination.next_cursor`. The response reports its effective window and whether
more rows remain. Operators must not query or edit the event tables directly.
Capacity limits, diagnostics, and acceptance tests are maintained in `EVENTS.md`.

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

`analysis.api_enabled` supplies the remote adapter used by the daily summary;
`digest.api_summary` controls that low-frequency stage. Keep
`analysis.api_triage_enabled = false` unless individual-item advisory calls are
worth their separate daily budget. The daily digest has an independent allowance
of five attempts. If the first attempt fails, the algorithmic digest is published
on time; Argus retries four times at hourly intervals within five hours and
publishes a new AI version if one succeeds. Three consecutive fully failed digest
days enqueue an operator alert. All state survives restart and errors are logged
without response secrets. Set
`analysis.api_model_fallbacks` and `analysis.local_model_fallbacks` to ordered
arrays of model IDs when more than one model is available. A call consumes one
budget unit regardless of how many candidates are attempted; transport errors,
timeouts, invalid JSON, and invalid digest citations advance to the next model.
`send_full_text` uses
only content already retained under its source rights policy; it never triggers
an extra crawl or bypasses a paywall.

The runtime-audit rollout is previewed with `activate_news_sources.py` plus
`--runtime-audit`; add `--apply` only after every new source probe succeeds. It
also installs the local host check, repairs legacy `general` topics from source
metadata, and keeps the local-model, heartbeat and hardware adapters disabled.

The reviewed JMA retention policy is previewed with
`activate_news_sources.py --disaster-signal-policy --expect-revision N`. The
preview fetches and parses the live feed without writing configuration; `--apply`
creates the next managed revision. Verify the applied revision, one successful
twelve-hour JMA poll, zero direct JMA alerts, and continued five-minute USGS coverage after restart.
An empty retained JMA batch is healthy when the unfiltered Feed is current.

`scripts/operations/check_status.py` reports dead-letter outbox rows as warnings.
A warning is not cleared by restarting the daemon; inspect the sanitized delivery
error, correct the provider or payload issue, and retry through the repository or
authenticated operations API.

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

In the reminder editor, enable **require acknowledgement** to make the ntfy
notification open its occurrence in the EOS admin page. Set the interval in
minutes (1 to 43,200) and the number of additional sends (0 means unlimited,
up to 100). Repeats start after actual delivery, never while the previous copy
is still queued or retrying. Click **已收到** on the linked page to stop repeats
for that occurrence; for daily reminders, tomorrow's occurrence remains
independent. The read endpoint is `GET /api/reminders/occurrences/<id>` and the
CSRF-protected write endpoint is `POST
/api/reminders/occurrences/<id>/acknowledge`. Editing or disabling the parent
reminder ends outstanding repeat schedules; an in-flight notification cannot
be recalled. Deleting a reminder also stops future sends, while delivered
occurrence details remain accessible from earlier ntfy links until normal
retention cleanup. A dead-letter delivery does not start another repeat timer:
inspect and retry it from the outbox; do not treat an undelivered reminder as
already received. Unlimited repeats are per occurrence, so a daily reminder
left unconfirmed across several days can have several independent schedules.

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

Content enrichment runs inside the Argus daemon but uses its own persisted queue.
`argus status --json` reports `content.documents` by level and `content.fetch_jobs`
by state. A `dead` content job means the Feed item and notification remain valid but
the optional public-document body could not be retained. Correct the source URL or
policy and wait for a newly observed item; do not treat this as an RSS outage.

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
For a non-disruptive database-only restore drill, run
`python scripts/operations/restore_drill.py --bundle BACKUP_DIRECTORY --report artifacts/restore-drill-YYYYMMDD.json`.
The command verifies the bundle, restores into a private temporary directory,
checks SQLite integrity, foreign keys and table counts, and reports the backup
age and measured database restore time. It never writes the production database
or restarts services. Its `rto_database_seconds` does not establish the full
service RTO; activation and readiness must be measured separately.

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

Production uses `/opt/eyeofsauron/current` as a symlink to a read-only release.
The one-time migration instructions in `RELEASES.md` apply only to older
installations that still use a normal directory; the installer refuses to
reinterpret such a directory as a symlink.

## Remove or disable

```bash
sudo systemctl disable --now argus
```

Disabling the unit leaves its code, configuration, and database recoverable.
Do not delete `/var/lib/argus` unless the alert history and feed baseline
are intentionally being discarded.
