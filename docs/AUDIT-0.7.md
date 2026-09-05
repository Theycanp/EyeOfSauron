# 0.7 engineering review

Reviewed on 2026-09-05. This release tightens the existing single-host service;
it does not claim zero vulnerabilities, high availability, or a completed
PostgreSQL backend.

## Verified changes

- Runtime and management code depend on separate repository contracts. SQLite
  owns transactional cursors, incidents, outbox, jobs, configuration revisions,
  and migrations. WAL uses FULL synchronization for durable commits.
- Configuration updates use revision checks; stale editors cannot overwrite a
  newer snapshot. Applied and desired revisions are distinct. Systemd performs
  the controlled restart after a revision change.
- Source failures are isolated. A normal in-flight poll does not falsely mark
  the source as starting. Engine ownership fences stale process heartbeats.
- Notification errors distinguish retryable transport/service failures from
  permanent rejection. Manual retry renews the retry period without changing
  the original creation time. Cancellation does not promise message recall.
- Source tests are durable, bounded jobs, not long operations in the HTTP
  handler. Stopping a browser wait does not claim to kill an in-flight request.
- Provider capabilities and the reviewed publisher catalog are independent of
  collection and delivery. A new RSS publisher is configuration, not a new
  branch in the runtime. Paid-only Reuters/AP integrations remain unavailable
  until a licensed adapter is supplied.
- Feed parsing rejects document types/entities, including UTF-16 input, and
  enforces byte limits. Feed hosts and redirects are checked; authenticated
  outbound requests do not forward credentials through redirects. Error
  redaction covers URL userinfo and authorization values.
- Releases are immutable and checksummed. Upgrade failure restores matching
  database state and actual installed units. Daily verified backups retain the
  latest 14 daily bundles without pruning manual or unverifiable backups.
- React uses strict TypeScript, separate feature modules, explicit operation
  states and responsive dialogs. Source type is selected only once; editing
  one source cannot silently narrow a shared rule to that source.

## Verification

The local release gate includes Python unit/integration tests with a 75 percent
branch-inclusive coverage floor, frontend unit tests, desktop/mobile browser
flows, strict frontend type checking, critical Python lint, selected strict
Python type checks, high-severity security scanning, dependency auditing,
release unpack/preflight tests, and production-database-copy migration.
Actual production smoke results are recorded separately in the server handoff.

The medium static-analysis findings were reviewed: dynamic SQL uses fixed
table names or bound placeholders; the wildcard address is a listener-inspection
comparison, not a bind; XML parsing has a rejecting doctype target and size
limits. Assertions are internal invariants, not authorization checks. Subprocess
calls use explicit arguments without a shell and fixed system executable paths.

## Remaining boundaries

- Delivery is at least once. A crash after ntfy accepts a message but before
  the local acknowledgement can create a duplicate.
- Same-host ntfy cannot report total power or network loss. External heartbeat
  and hardware-command activation remain off until separately configured.
- Breaking-news scoring is a transparent heuristic, not editorial judgement.
  Feed timeliness, licenses and provider account quotas remain external limits.
- Host allowlists and DNS checks are defense in depth, not a complete outbound
  network sandbox. A compromised configured DNS name remains a trust boundary.
- SQLite remains single-host. A future database adapter needs transactional
  contract and migration tests; changing a connection string is not sufficient.
- Python strict typing is incremental, not yet applied to every module. Source
  tests use real public feeds plus fixtures; no private provider credentials are
  fabricated to claim end-to-end market, X or mailbox verification.
