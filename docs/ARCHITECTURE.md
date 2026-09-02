# SignalWatch architecture

## Scope

SignalWatch is an event system, not a scraper dedicated to one website. Inputs
produce immutable `Observation` records. Rules turn observations into
`AlertCandidate` records. SQLite stores source cursors, deduplication state,
rule output, and the notification outbox. Notifiers deliver outbox entries.

```text
collectors -> normalized observations -> rules -> SQLite outbox -> notifiers
                     |                    |
                     +---- SQLite --------+
```

## Contracts

- A collector owns network access and returns normalized observations.
- A rule is deterministic for an observation and configuration.
- A notifier knows nothing about collectors or rules.
- A dedupe scope plus external ID identifies the same item across collectors.
- A rule ID plus observation identity identifies the same alert.

These contracts allow future adapters for market APIs, X, host events, webhooks,
and MQTT without changing delivery reliability.

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

## Security

- Runtime secrets arrive only through environment variable names declared in
  configuration.
- Feed redirects must remain HTTPS and inside the configured hostname allowlist.
- Remote responses are size-limited before XML parsing.
- The service has no inbound network listener and needs no firewall rule.
- The systemd account has no shell, capabilities, device access, or writable
  paths outside its state directory.
