# Configuration and feature controls

## Authorities

Configuration has three layers:

1. `/etc/argus/config.toml` contains host-level paths, listener addresses,
   notifier endpoint, and bootstrap settings.
2. The active immutable `config_revisions` row in SQLite contains managed
   sources, rules, analysis policy, and digest policy. It is the authority after
   the one-time legacy JSON import.
3. Root-owned environment files contain secret values. Configuration contains
   only the environment-variable names.

The admin API validates the complete managed set and activates it using a
compare-and-swap revision. Argus notices a new desired revision, exits with
status 75, and systemd restarts it with the new validated snapshot. Rollback
creates another revision; it never edits history.

## Feature switches

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
- local weather settings live in schema-23 `weather_subscriptions`, outside the
  managed source revision. The default Shahe campus subscription starts with
  daily forecast and change alerts enabled; the Weather page can independently
  disable either. Forecast polling continues for the status page. Settings use
  their own compare-and-swap revision and audit trail; see `WEATHER.md`.

New credential-free source features should default on only after a real fetch and
parser probe succeeds. Credential-dependent and device-control features remain
off until explicitly configured. The admin UI exposes supported switches and
labels the digest item value as a maximum, not a target.

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
