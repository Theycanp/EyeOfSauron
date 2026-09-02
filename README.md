# SignalWatch

SignalWatch is a small event-monitoring service for this host. It collects data
from adapters, normalizes and deduplicates observations, evaluates rules, and
delivers durable notifications through ntfy.

The first production adapter monitors Bloomberg's official RSS feeds. A
configurable weighted headline rule emits a high-priority notification for
likely breaking or market-moving news. The project is intentionally not tied to
Bloomberg: stock, host-health, social, and MQTT adapters can use the same event,
rule, state, and delivery contracts.

## Reliability properties

- First successful collection establishes a baseline and never floods old news.
- Bloomberg GUIDs are deduplicated across multiple RSS sections.
- Observations and notifications are committed in one SQLite transaction.
- Notifications use an at-least-once outbox with leases and exponential retry.
- RSS requests use TLS validation, ETag/Last-Modified, bounded fast retries,
  timeouts, and a size cap.
- Source failures alert only after a threshold; recovery is also reported.
- The service runs as a dedicated unprivileged account with systemd hardening.
- Its ntfy identity has write-only access to the single `signalwatch` topic.
- No secret is stored in this repository, SQLite, or application logs.

## Local development

No third-party runtime dependency is required.

```bash
cd /home/joker/services/signalwatch
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m signalwatch --config config/signalwatch.example.toml check-config
```

See `docs/ARCHITECTURE.md` for design decisions and `docs/OPERATIONS.md` for
deployment and recovery commands.
