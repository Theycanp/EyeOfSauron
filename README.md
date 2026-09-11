# EyeOfSauron

EyeOfSauron (EOS) is a personal intelligence monitoring system, powered by
Argus, its always-on watcher engine. Argus collects data from adapters,
normalizes and deduplicates observations, evaluates rules, and delivers durable
notifications through ntfy.

Observations carry explicit importance, urgency, relevance, confidence,
region, topic, source tier, information type, handling route, and processing
state. Deterministic triage remains authoritative; optional local and remote
model adapters provide bounded, audited advice through one analyzer contract.

The first production adapter monitors Bloomberg's official RSS feeds. A
configurable weighted headline rule emits a high-priority notification for
likely breaking or market-moving news. The project is intentionally not tied to
Bloomberg: stock, email, X, YouTube, host-health, and MQTT adapters use the same
event, rule, state, and delivery contracts.

Saved event details use a policy-aware content layer. Commercial publishers such
as Bloomberg, WSJ, FT, and The Economist remain limited to the title, Feed excerpt,
and original link. Public first-party sources such as governments, central banks,
regulators, and scientific agencies may opt into bounded HTML/PDF text extraction.
Only plain text is retained; the fetch queue is durable and independent from RSS
source health.

## Reliability properties

- First successful collection establishes a baseline and never floods old news.
- Bloomberg GUIDs are deduplicated across multiple RSS sections.
- Observations, durable incidents, and notifications are committed in one SQLite transaction.
- Notifications use an at-least-once outbox with leases and exponential retry.
- Manual one-time and daily reminders are durable, timezone-aware, and use the same outbox.
- Administrator-created events atomically enter the event history, daily digest,
  and normal notification outbox.
- RSS requests use TLS validation, ETag/Last-Modified, bounded fast retries,
  timeouts, and a size cap.
- Source failures alert only after a threshold; recovery is also reported.
- The service runs as a dedicated unprivileged account with systemd hardening.
- Its ntfy identity has write-only access to the single `eos` topic.
- No secret is stored in this repository, SQLite, or application logs.
- Daily digests are clustered across sources, versioned, and published through
  the same durable notification outbox.

## Management backend

The optional `admin` command serves a React/Vite-built management UI and a JSON
API. The production template binds it to `127.0.0.1:18080` and publishes it only
through a dedicated TLS virtual host; port 18080 must never be exposed directly.
Administrators sign in with an individual username and Argon2id-protected
password. A revocable server-side session lasts at most 14 days; the browser
holds only Secure, HttpOnly/SameSite session material and a session-bound CSRF
token. Administrator, operator, and viewer roles are enforced by the backend.
The UI is a static build: Node is used
only during development/build and is never a production runtime dependency.
Source and rule changes are validated and committed as versioned SQLite
configuration snapshots. The engine detects a new revision within five seconds
and gracefully restarts under systemd; the UI distinguishes saved and applied
revisions. The old managed JSON file is a one-time migration input, not the authority;
after import it is not rewritten. Use the management API to export current settings.
Concurrent edits are rejected instead of overwriting a newer revision.
Credentials are referenced by `*_env` names, never entered as literal values.

The backend supports configuration revisions, audit history, full-set validation,
bounded asynchronous source connection tests, enable/disable, rollback, incident inspection, and
Prometheus-compatible metrics. It also creates, edits, disables, and deletes
one-time, countdown, and daily reminders without a service restart. Countdown
input is persisted as an absolute time; daily reminders keep an IANA timezone.
Operators and administrators can create a one-off event from the event page;
viewers remain read-only. The saved creator identity and classification are
auditable, and delivery uses the configured notifier rather than a UI-only test path.
It can add custom stock symbols and thresholds, official RSS/Atom
feeds (including WSJ, The Economist, blogs and YouTube channel feeds), X
numeric user IDs, and IMAP searches. Sources without credentials remain
disabled. Do not use display names as identity for X or other accounts.

Failed notifications have explicit retryable, dead-letter, and cancelled states.
Operators can retry a dead letter or cancel a pending message from the UI.
The reviewed news catalog includes Bloomberg, WSJ, The Economist, FT, official
Chinese, Japanese, US and international primary sources, and disaster/health/
science feeds. Licensed-only providers are labeled unavailable rather than
using an unofficial scraping endpoint.

The authenticated reader provides responsive digest list/detail views and
read-only APIs. Daily generation is disabled by default. When enabled, Argus
uses the configured IANA timezone and wall-clock time, catches up one missed
publication after downtime, publishes one immutable version per local day, and
sends its HTTPS reader URL through the notifier port.

Event-backed ntfy notifications open an authenticated EyeOfSauron detail page
instead of sending the browser directly to the publisher. The page displays the
summary and analysis context already saved at collection time, so it remains
useful when Bloomberg or another publisher is slow or unavailable. The original
article remains available as a secondary action. This routing applies only to
newly delivered notifications; previously delivered ntfy messages are immutable.

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

Python runtime dependencies are declared in `pyproject.toml`; password hashing
uses `argon2-cffi`.

```bash
cd /home/joker/services/eyeofsauron
scripts/ci/check.sh backend
scripts/ci/check.sh frontend
```

These are the same core checks used by GitHub Actions. The backend uses Python's
built-in `unittest` runner; `pytest` is intentionally not required. The scripts
prefer the repository `.venv` when present. Dependency installation and online
vulnerability audits remain explicit CI setup steps rather than hidden test
network access.

See `docs/ARCHITECTURE.md` for design decisions, `docs/PROVIDERS.md` for the
provider capability contract and reviewed news catalog, and `docs/OPERATIONS.md`
for deployment and recovery commands.

## License

EyeOfSauron, including the Argus engine and administration UI, is licensed under
the [Apache License 2.0](LICENSE). See [NOTICE](NOTICE) for project attribution
and [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES) for bundled dependency licenses.
Third-party code and monitored content retain their own licenses and terms.

This change applies from the Apache-2.0 licensing commit onward. Snapshots
previously published under MIT, including tag `v0.7.0`, retain their original
MIT grants; historical tags and release artifacts are not relicensed.
