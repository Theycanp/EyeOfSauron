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

New credential-free source features should default on only after a real fetch and
parser probe succeeds. Credential-dependent and device-control features remain
off until explicitly configured. The admin UI exposes supported switches and
labels the digest item value as a maximum, not a target.

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
