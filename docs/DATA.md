# Data, schema, and persistence

## Repository boundary

SQLite is an adapter, not a business dependency. Protocols in `persistence.py`
and feature modules expose the operations needed by collection, analysis,
digests, reminders, admin, and delivery. Business code must not issue SQL or rely
on SQLite row layout. A replacement repository must preserve transaction,
idempotency, leasing, and compare-and-swap semantics.

## Production database

The production database is `/var/lib/argus/state.db`. It uses WAL mode and
`synchronous=FULL`. Both `argus.service` and `argus-admin.service` are SQLite
writers and must be stopped for an offline restore or schema downgrade.

Major table groups include:

- observations, source cursor/runtime/quality state, and incidents;
- alerts/outbox delivery leases and audit history;
- content documents and independent content-fetch jobs;
- analysis attempts, Prompt versions, and API usage;
- immutable configuration revisions and applied runtime state;
- admin users, server sessions, throttling, and authentication audit;
- reminders and occurrence scheduling;
- versioned digests, digest items, source coverage, retry state, digest API usage,
  and consecutive failure state;
- stable events, parallel source reports, claims, claim evidence, and timeline
  items.

Eligible `digest` and `immediate` observations are continuously projected into
the event tables. The projector works in small batches and compares each new
report with a bounded set of recent candidates; administration reads are
read-only and use cursor pagination rather than clustering on demand. The daily
digest and the default 28-hour event reader consume this same projection.

## Schema and migrations

`SCHEMA_VERSION` in `database.py` and `PRAGMA user_version` are authoritative.
Startup applies forward-only, transactional, idempotent migrations in order and
refuses a database newer than the running code. Schema 15 adds persistent digest
AI attempt usage, retry state, and consecutive failure state; schema 16 adds
source-tier evidence to digest items; schema 17 adds the event/report/claim/
timeline projection and links digest items to stable events. Schema 18 adds the
event workspace preferences/editorial choices, aliases, review audit and frozen
historical report snapshots. Schema 19 adds bounded digest generation attempt
history and provider diagnostics. Release metadata records the required schema.

A code rollback that cannot read the current schema must restore the matching
pre-release backup; changing only the `current` symlink is unsafe in that case.
Migration tests construct older schemas and verify upgrade behavior.

## Retention and backup

Observations and delivered alerts are normally retained for 90 days; operational
audit and configuration history have their own cleanup rules. Cleanup never
removes an active outbox lease or current runtime state.

Backups use SQLite's Online Backup API and include integrity results, row counts,
checksums, schema version, and release/config metadata. The daily timer keeps 14
verified backups. A release deployment creates a verified pre-release recovery
point while both services are coordinated by the release lock. See
`OPERATIONS.md` and `RELEASES.md` for exact restore and rollback rules.
