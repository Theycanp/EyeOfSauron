# System and runtime model

## Purpose

EyeOfSauron is a personal intelligence and operational alerting system. Argus is
the always-on engine. It monitors heterogeneous sources, normalizes facts into a
common event model, decides their handling with auditable rules, stores the
result, and sends durable notifications. The React application is the control
and reading surface; it is not part of the data path.

## End-to-end flow

```text
source adapter -> Observation -> deterministic triage -> rules -> Incident
      |                |                    |              |
      +---- state/cursor+---- content jobs --+---- SQLite transaction
                                                      |
                                                   outbox
                                                      |
                                            Notifier port -> ntfy

Observations + coverage -> cluster/rank/select -> digest draft
                                      |              |
                                      +-> optional AI summary
                                                     |
                                           versioned digest + outbox
```

Collectors own network access and source cursors. They return immutable,
normalized observations; they do not send notifications. Deterministic triage
assigns importance, urgency, relevance, confidence, region, topic, source tier,
information type, and handling. Optional model analysis is advisory and cannot
independently bypass the deterministic notification policy.

Rules produce alert candidates. The repository commits observations, incident
state, and outbox rows together. The delivery worker leases outbox rows and calls
the configured `Notifier` adapter. Delivery is at-least-once: a crash after the
remote server accepts a message but before local acknowledgement can rarely
produce a duplicate, but cannot silently lose the alert.

## Module boundaries

- `config.py`: validated immutable runtime configuration.
- `adapters.py` and provider modules: collector construction and external input.
- `models.py`: normalized observations, alerts, incidents, and work items.
- `triage.py`, `rules.py`: deterministic classification and notification policy.
- `database.py`: SQLite adapter implementing repository ports.
- `persistence.py`: business-facing persistence protocols and unit-of-work boundary.
- `service.py`: schedules collectors, analysis, reminders, digest, content jobs,
  and delivery workers.
- `digest.py`, `digest_analysis.py`: deterministic daily selection and optional
  model synthesis.
- `prompts.py`: trusted, versioned built-in Prompt definitions.
- `notifiers.py`: notification adapters; ntfy is the current implementation.
- `admin.py`, `admin_server.py`: revisioned management API and authenticated reader.
- `frontend/`: React source; `src/argus/admin_web/` is its deployable static build.

Business services depend on narrow ports rather than `Database`, ntfy, or one AI
provider. A PostgreSQL repository, email notifier, or another OpenAI-compatible
model can be added behind the corresponding port without rewriting collection,
rules, or digest policy.

## Durable identities and idempotency

- A source cursor and `dedupe_scope + external_id` identify an observation.
- `rule_id + observation identity` identifies an alert candidate.
- Incidents use stable condition keys and are distinct from delivery attempts.
- A digest uses `daily:YYYY-MM-DD`; every revision is immutable and increasing.
- A digest notification includes its version in the outbox dedupe key.
- Reminder occurrence keys contain their scheduled timestamp.

The first successful source fetch establishes a baseline and does not replay old
items. Disabling or deleting a source clears obsolete source-health incidents.
One-off news is a recorded event; only a condition that can actually recover is
a stateful open/recovered incident.

## Security boundary

The daemon and admin service run as the unprivileged `argus` account. The admin
listener is loopback-only and is published by the TLS reverse proxy. Passwords
use Argon2id. Server-side sessions, secure cookies, CSRF validation, same-origin
checks, throttling, and backend RBAC protect changes. Feed input and model input
are untrusted data; Prompt text is trusted configuration and is never taken from
an article. Credentials are referenced by environment-variable names and never
stored in managed JSON, SQLite configuration payloads, or source control.
