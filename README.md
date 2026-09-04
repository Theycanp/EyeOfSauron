# SignalWatch

SignalWatch is an event-monitoring service for this host. It collects data
from adapters, normalizes and deduplicates observations, evaluates rules, and
delivers durable notifications through ntfy.

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
- RSS requests use TLS validation, ETag/Last-Modified, bounded fast retries,
  timeouts, and a size cap.
- Source failures alert only after a threshold; recovery is also reported.
- The service runs as a dedicated unprivileged account with systemd hardening.
- Its ntfy identity has write-only access to the single `signalwatch` topic.
- No secret is stored in this repository, SQLite, or application logs.

## Management backend

The optional `admin` command serves a small JSON/HTML management backend. The
production template binds it to `127.0.0.1:18080`; access it remotely with an
SSH tunnel, for example `ssh -N -L 18080:127.0.0.1:18080 joker@host`. It does
not add a public firewall rule. Source and rule changes are validated and saved
to the service-owned managed JSON file and take effect after restarting
SignalWatch. Credentials are referenced by `*_env` names and are never entered
into the managed file.

The backend supports configuration revisions, audit history, full-set validation,
source connection tests, enable/disable, rollback, incident inspection, and
Prometheus-compatible metrics. It can add custom stock symbols and thresholds, official RSS/Atom
feeds (including WSJ, The Economist, blogs and YouTube channel feeds), X
numeric user IDs, and IMAP searches. Sources without credentials remain
disabled. Do not use display names as identity for X or other accounts.

## Local development

No third-party runtime dependency is required.

```bash
cd /home/joker/services/signalwatch
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m signalwatch --config config/signalwatch.example.toml check-config
```

See `docs/ARCHITECTURE.md` for design decisions and `docs/OPERATIONS.md` for
deployment and recovery commands.
