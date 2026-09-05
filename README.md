# EyeOfSauron

EyeOfSauron (EOS) is a personal intelligence monitoring system, powered by
Argus, its always-on watcher engine. Argus collects data from adapters,
normalizes and deduplicates observations, evaluates rules, and delivers durable
notifications through ntfy.

The first production adapter monitors Bloomberg's official RSS feeds. A
configurable weighted headline rule emits a high-priority notification for
likely breaking or market-moving news. The project is intentionally not tied to
Bloomberg: stock, email, X, YouTube, host-health, and MQTT adapters use the same
event, rule, state, and delivery contracts.

## Reliability properties

- First successful collection establishes a baseline and never floods old news.
- Bloomberg GUIDs are deduplicated across multiple RSS sections.
- Observations, durable incidents, and notifications are committed in one SQLite transaction.
- Notifications use an at-least-once outbox with leases and exponential retry.
- Manual one-time and daily reminders are durable, timezone-aware, and use the same outbox.
- RSS requests use TLS validation, ETag/Last-Modified, bounded fast retries,
  timeouts, and a size cap.
- Source failures alert only after a threshold; recovery is also reported.
- The service runs as a dedicated unprivileged account with systemd hardening.
- Its ntfy identity has write-only access to the single `eos` topic.
- No secret is stored in this repository, SQLite, or application logs.

## Management backend

The optional `admin` command serves a React/Vite-built management UI and a JSON
API. The production template binds it to `127.0.0.1:18080`; access it remotely
with an SSH tunnel, for example `ssh -N -L 18080:127.0.0.1:18080 joker@host`.
It does not add a public firewall rule. The UI is a static build: Node is used
only during development/build and is never a production runtime dependency.
Source and rule changes are validated and saved to the service-owned managed
JSON file and take effect after restarting Argus. Credentials are
referenced by `*_env` names and are never entered into the managed file.

The backend supports configuration revisions, audit history, full-set validation,
source connection tests, enable/disable, rollback, incident inspection, and
Prometheus-compatible metrics. It also creates, edits, disables, and deletes
one-time, countdown, and daily reminders without a service restart. Countdown
input is persisted as an absolute time; daily reminders keep an IANA timezone.
It can add custom stock symbols and thresholds, official RSS/Atom
feeds (including WSJ, The Economist, blogs and YouTube channel feeds), X
numeric user IDs, and IMAP searches. Sources without credentials remain
disabled. Do not use display names as identity for X or other accounts.

## Naming

- **EyeOfSauron** is the product and whole system; **EOS** is its short name.
- **Argus** is the core watcher engine, Python package, CLI, daemon, and service
  namespace (`argus`, `argus.service`, and `argus-admin.service`).
- **eos** is the shared ntfy topic used by system events and manual reminders.

Incidents have explicit semantics: ordinary news and one-off intelligence are
stored as `event / recorded`; conditions that can clear, such as source, host,
or market health, are `stateful / open` and transition to `recovered` only when
the same condition reports recovery.

## Local development

No third-party runtime dependency is required.

```bash
cd /home/joker/services/eyeofsauron
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m argus --config config/argus.example.toml check-config
```

See `docs/ARCHITECTURE.md` for design decisions and `docs/OPERATIONS.md` for
deployment and recovery commands.
