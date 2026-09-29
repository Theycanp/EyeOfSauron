# Configuration and feature controls

## Authorities

Configuration has three layers:

1. `/etc/argus/config.toml` contains host-level paths, listener addresses,
   notifier endpoint, and bootstrap settings.
2. The active immutable `config_revisions` row in SQLite contains managed
   sources, rules, analysis policy, digest policy and notification routing. It is the authority after
   the one-time legacy JSON import.
3. Root-owned environment files contain secret values. Configuration contains
   only the environment-variable names.

The admin API validates the complete managed set and activates it using a
compare-and-swap revision. Argus notices a new desired revision, exits with
status 75, and systemd restarts it with the new validated snapshot. Rollback
creates another revision; it never edits history.

## Feature switches

- `notifications.routes`: news/weather/reminders/system map to destination IDs;
  `null` mutes newly generated notifications of that category, not collection.
- `notifications.destinations`: ID, provisioned ntfy topic, display label and
  notes; topics and IDs must be unique. Referenced entries cannot be deleted.
- `notifications.fallback`: required destination ID for future/unclassified
  categories. Severity remains the message priority, not a routing category.
- `ntfy.allowed_topics`: root-controlled exact publisher capabilities. The
  application cannot change ntfy ACLs; provision matching write-only permissions
  first. The default topic must be in this list. See `NOTIFICATIONS.md`.

- `[digest].enabled`: daily digest scheduler.
- `[digest].api_summary`: optional remote AI synthesis after deterministic
  selection. Disabling this still produces algorithmic digests.
- `[digest].notify`: enqueue digest notifications.
- `[digest].item_limit`: hard safety ceiling. Actual daily count is adaptive.
- `analysis.enabled`: analysis subsystem master switch.
- `analysis.api_enabled`: remote OpenAI-compatible adapter availability.
- `analysis.api_triage_enabled`: per-observation remote advisory calls; normally off.
- `analysis.local_enabled`: local-model adapter; normally off until evaluated.
- `analysis.shadow_mode`: retain model advice without changing deterministic fields.
- individual source `enabled`: collector activation.
- reminders, host checks, heartbeat, and MQTT/device integrations are independent.
- local weather settings live in `weather_subscriptions` (introduced by schema
  23), outside the
  managed source revision. The default Shahe campus subscription starts with
  daily forecast and change alerts enabled; the Weather page can independently
  disable either. Forecast polling continues for the status page. Settings use
  their own compare-and-swap revision and audit trail; see `WEATHER.md`.
- Weather background provider policies (schema 28) independently control
  `enabled`, `interval_seconds` and UTC daily HTTP budget for each channel.
  They are runtime settings outside config revisions and take effect without
  restarting; see the Weather budget panel and `WEATHER.md` for defaults.
  Turning off condition notifications does not turn off collection; turning off
  a provider channel does stop its background requests. Quota/disabled state
  must not be mistaken for an upstream outage.

New credential-free source features should default on only after a real fetch and
parser probe succeeds. Credential-dependent and device-control features remain
off until explicitly configured. The admin UI exposes supported switches and
labels the digest item value as a maximum, not a target.

## Host load alerts

Host-source `settings.load1` is an absolute one-minute Linux load-average
threshold, not CPU utilization; runnable and uninterruptible tasks contribute.
`settings.load_sustain_seconds` defaults to 300 (integer, 0--86400). A new load
incident is emitted only after successive known over-threshold samples span
that duration. Brief spikes produce neither an alert nor a recovery message.
Disk, memory, unit and listener checks keep their existing immediate behavior.

Pending progress belongs to the collector cursor and is saved even when no
observation is produced, so restarting Argus does not lose a valid streak.
A below-threshold or unknown sample, collector failure, backward clock jump,
policy change, or sampling gap larger than the greater of three poll intervals
and the probe timeout resets pending evidence. Samples do not prove continuous
utilization between polls. An already reported incident still remains open
while a probe is unknown and recovers on a known below-threshold sample.

The source's administrator-managed settings can override the duration; setting
zero explicitly restores the old immediate mode. Existing thresholds are not
raised or disabled during upgrade; missing duration settings get the new default.
No schema migration or system telemetry agent is required. Code rollback uses
the normal release procedure and restores immediate load checks; an explicit
new setting should be removed when rolling back provider configuration.

## Regional taxonomy and weighting

New source templates should use macro-region codes: `EAST_ASIA`,
`NORTH_AMERICA`, `EUROPE`, `AUSTRALIA_OCEANIA`, `SOUTHEAST_ASIA`,
`MIDDLE_EAST`, `SOUTH_AMERICA`, or `AFRICA`. `GLOBAL` and `OTHER` remain
valid neutral fallbacks. Existing `CN`, `JP`, and `US` observations and
configuration keys remain valid; they are mapped to East Asia or North America
only when resolving a policy weight, so historical data does not need a
migration.

Regional weights are soft ranking preferences, not quotas. The built-in
defaults place East Asia first, North America/Europe/Australia-Oceania next,
then Southeast Asia/Middle East, with South America/Africa lower. Operators can
override any subset in `[analysis.region_weights]`; omitted regions continue to
use the defaults. Exact legacy keys such as `CN` override their mapped macro
family for backward-compatible control.

## Secrets

Production secret files are `/etc/argus/ntfy.env`, `/etc/argus/providers.env`,
and `/etc/argus/admin.env`, owned by `root:argus` with mode `0640`. Never place
secret values in a command argument, log, Prompt, docs, managed revision, Git
remote, or test fixture. Changing an environment file requires restarting the
affected service because it is outside managed configuration revisions.

## Production profile

The production template is `config/argus.production.toml`. It contains only the
base Bloomberg set; reviewed managed sources are installed by the rollout tools
and live in SQLite. This separation prevents a package upgrade from replacing
operator choices. Use the status API/CLI to inspect the effective merged
configuration and applied revision.

Model order is configured as a primary model plus ordered fallbacks. One logical
digest attempt may try those adapters in order. Transport errors, timeouts,
invalid JSON, and invalid citations move to the next candidate; model failover
inside one attempt does not weaken validation or deterministic routing.
