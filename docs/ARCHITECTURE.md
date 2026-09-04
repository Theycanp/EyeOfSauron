# SignalWatch architecture

## Scope

SignalWatch is an event system, not a scraper dedicated to one website. Inputs
produce immutable `Observation` records. Rules turn observations into
`AlertCandidate` records. SQLite stores source cursors, deduplication state,
rule output, and the notification outbox. Notifiers deliver outbox entries.

```text
collectors -> normalized observations -> rules -> incidents -> SQLite outbox -> notifiers
                     |                    |
                     +---- SQLite --------+
```

## Contracts

- A collector owns network access and returns normalized observations.
- A rule is deterministic for an observation and configuration.
- A notifier knows nothing about collectors or rules.
- A dedupe scope plus external ID identifies the same item across collectors.
- A rule ID plus observation identity identifies the same alert.
- A matching rule records confidence and human-readable evidence. A normalized
  incident key suppresses repeated headlines from independent feeds for a short
  window while retaining the original observation and link.

These contracts allow future adapters for market APIs, X, host events, webhooks,
and MQTT without changing delivery reliability.

Incidents are durable records separate from notification attempts. An incident
collects source IDs, evidence, confidence, timestamps, and recovery state;
multiple alerts can refer to one incident while the outbox remains at-least-once.
This prevents notification deduplication from erasing operational history.

Configuration changes are revisioned and audited. The management API validates
the complete managed set before writing an atomic current file, stores a private
revision snapshot, and rolls back by creating a new revision.

## Delivery semantics

The outbox is at-least-once. An alert is inserted in the same transaction as its
observation. A worker claims one due alert with a lease, publishes it, then marks
it delivered. If the process dies after publish but before acknowledgement, the
lease expires and the notification may be repeated. This favors a rare duplicate
over silently losing a critical alert.

No external message queue is needed at current volume. The database and in-process
async tasks form the queue boundary. A future transport can implement the same
contracts with NATS JetStream if workers are ever split across hosts.

## Bloomberg stage

The source is Bloomberg's public official RSS service, not scraped article HTML.
Markets, politics, technology, and economics feeds are polled independently.
The feeds do not expose a reliable `breaking` field, so the first rule is a
transparent weighted classifier over titles and summaries. Its patterns,
threshold, priority, and topic are configuration, not hidden application logic.

This classifier is deliberately conservative and auditable. It cannot understand
every important event and may produce false positives. Later improvements can add
an optional semantic classifier as a second opinion while retaining deterministic
rules as the reliable fallback.

## Configurable sources

`kind = "rss"` is the generic HTTPS RSS/Atom adapter, so publisher-specific
feeds are configuration rather than hard-coded integrations. `kind = "market"`
uses a provider interface and stores a compact per-symbol cursor; it supports
price moves, gaps, volume spikes and cooldowns. `kind = "imap"`, `"x"`, and
`"youtube"` use UID, numeric user ID, and channel ID cursors respectively.
All are disabled until explicitly configured.

`kind = "host"` is an opt-in low-frequency local probe for disk/inode,
memory, load and selected systemd units. It emits transitions (including
recovery) rather than storing every sample. For CPU, temperature, SMART,
network rates and certificate age, use Prometheus exporters and forward only
alert events.

Continuous host metrics should be collected by Prometheus exporters and sent as
already-evaluated incidents. SignalWatch also includes outbound heartbeat,
MQTT sensor normalization, and an allowlisted command policy for future
devices; neither opens an inbound public control port nor persists high-rate
telemetry in SQLite.

## Security

- Runtime secrets arrive only through environment variable names declared in
  configuration.
- Feed redirects must remain HTTPS and inside the configured hostname allowlist.
- Remote responses are size-limited before XML parsing.
- The service has no inbound network listener and needs no firewall rule.
- The systemd account has no shell, capabilities, device access, or writable
  paths outside its state directory.
