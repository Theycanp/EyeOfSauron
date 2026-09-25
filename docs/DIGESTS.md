# Daily digest policy

## Deterministic selection

At each configured local wall-clock boundary, the scheduler reads the preceding
local-day window. It includes observations handled as `digest` and those already
sent as `immediate`, so a notification seen briefly during the day is still
available in the daily review.

Candidates are read from the continuously maintained event pool also used by the
daily-events reader. Missing observations in the target period are projected
before selection; async preparation yields between batches of 50, allowing other
daemon workers to continue. Selection follows event-page cursors up to the
configured candidate budget, so a filtered first page does not hide later eligible
events. The synchronous builder remains available to offline
tools. Records are clustered by the event-centric adapter (`cluster_events`) using
normalized title, entities, numbers, topic, region, and time-window signals.
Each event keeps first-party, secondary, and social reports as parallel
evidence. Ranking combines durable
importance, urgency, relevance, confidence, regional preference, corroboration,
and long-term source-quality weight. A soft repeated-source penalty prevents a
high-frequency feed from occupying the report, while immediate events stay ahead
of ordinary entries.

Stable event identities are unique within a digest. If conservative batch
clusters resolve to the same historical identity, their evidence is recombined
before AI synthesis and storage; reports are not discarded to satisfy uniqueness.
Delayed notifications can contribute old evidence to the notification-day window.
New digest versions freeze their selected EventReport evidence in the same
transaction as the digest items. Later report edits or 90-day observation cleanup
cannot change those published versions. Versions created before this change may
still use the legacy live-report reconstruction path.

The configured `item_limit` is a hard ceiling, currently 50. The actual count is
chosen without AI:

```text
signal_energy = sum(max(0, clamp(cluster.score, 0, 6) - 2.5)^2)
topic_bonus = min(3, max(0, number_of_distinct_meaningful_topics - 1))
score_target = ceil(2 * sqrt(signal_energy) + topic_bonus)
selected_count = min(item_limit, candidate_count,
                     max(1, score_target, number_of_immediate_clusters))
```

The 2.5 floor keeps background reports from filling the digest through volume
alone. Stronger and more varied days can still reach 50. The calculation is
deterministic and covered by tests; empty days remain empty.

Before adaptive ranking, the builder reads global event editorial choices through
the event repository port. `exclude` removes an event from this digest window;
`include` moves it ahead of ordinary candidates. Inclusion still obeys the
window, enabled-source policy, adaptive count and 50-item ceiling. Account-level
read/ignore preferences never alter the global digest.

## AI synthesis

AI receives only the already selected evidence. It does not decide notification
routing or alter source facts. If there are at most 12 topics, the adapter performs
one synthesis call. For larger reports, it first sends a compact index of every
topic, lets the model choose how many genuinely need deeper treatment, and then
sends those topics for synthesis. There is no fixed target topic count or prose
length. Input/output size limits and exact citation validation remain mandatory.

The active built-in Prompt is `digest@4` in `src/argus/prompts.py`. It explicitly
treats Chinese as the output language rather than a regional preference. Topic
selection is based on evidence and impact; similarly important material should
retain reasonable cross-region and cross-sector coverage without mechanical
quotas. A single region may dominate only when the supplied evidence warrants
it. Historical
versions stay registered so old reports remain auditable. The model must return
JSON, cite only supplied numeric IDs, match citations to `[N]` references in the
text, distinguish facts from inference, avoid external facts and instructions
embedded in articles, and produce concise Chinese prose of whatever length the
evidence warrants. Prompt text can be inspected and versioned from the admin API;
secrets never belong in Prompt text.

When `send_full_text` is enabled, the scheduler may substitute up to 4,000 stored
characters per selected item. It uses only content already retained under the
source's rights policy and never fetches a paywalled article for the model.

## Failure, retry, and escalation

Each digest has an independent, persistent allowance of five synthesis attempts;
this is separate from the per-observation remote-analysis daily budget.

1. If the first attempt fails, Argus immediately saves, publishes, and notifies
   the deterministic algorithmic version.
2. It stores retry state in SQLite and schedules four more attempts at +1, +2,
   +3 and +4 hours within a five-hour window. Times are anchored to the original
   fallback publication, so polling delays do not accumulate. The spare hour
   accommodates normal latency. A restart retains the deadline; downtime beyond
   it expires the task without pretending another inference ran. Missed slots
   can be attempted on subsequent worker polls while the window remains open.
3. The first successful retry creates and publishes a new immutable AI version,
   supersedes the prior published version, and sends a second digest notification.
4. If all five attempts fail, the state is marked failed and the day counts toward
   the consecutive-failure streak. The algorithmic report remains readable.
5. Three consecutive local digest days with fully exhausted attempts enqueue one
   high-priority operator notification. Reprocessing the same day is idempotent.
   The next successful AI digest resets the streak.

Retries also cover invalid model output (blank, oversized or non-text), including
validation after the adapter returns. Local evidence preparation and duplicate
identity/coverage checks occur before inference budget reservation. A structural
failure still requires fixing the evidence; it cannot be made publishable merely
by asking the model again. Model response validation is different: a subsequent
provider attempt can produce a valid answer, so it remains retryable.

Publication and notification enqueue are separate durable operations. Every
visit to an already published version idempotently ensures its notification is
queued, including recovery from a crash before retry acknowledgement. A published
AI version wins over a stale pending retry row, even after the retry deadline.
The initial sanitized failure reason is retained and retry initialization cannot
reset an existing window. The persisted `attempts` counts logical synthesis
attempts (one may try multiple providers or make index and synthesis requests),
not individual HTTP requests; deadline expiry alone does not increment it.

Existing schema-17 retry rows are compatible: their saved next-attempt and
deadline are retained, and subsequent retries use the anchored schedule. No old
digest or historical retry count is rewritten. Rollback to the previous code is
schema-compatible, but restores its former retry behaviour.

Persistent retry tables are `digest_retry_state`, `digest_api_usage`,
`digest_failure_state`, and `digest_generation_attempts` (the latter is added in
schema 19 after the event workspace migration in schema 18). Errors are sanitized
before persistence and notification. The outbox remains responsible for actual
delivery and retry.

The ntfy adapter caps the message at 4,000 UTF-8 bytes, below the server's
nominal 4,096-byte attachment boundary, and caps titles at 256 bytes. The
notification keeps the authenticated EOS
detail URL, so truncation affects only the push preview; the immutable full digest
remains available in the web reader.

The authenticated admin endpoint `GET /api/digest-runs` exposes derived run state,
publication version, retry window, and up to 50 logical generation attempts. Each
attempt records only an allowlisted provider hostname, model, Prompt identifier,
version and hash, elapsed time, status, and sanitized error. It never stores
request text or credentials. `POST /api/digest-runs/<key>/retry` can advance an
already scheduled retry only; it cannot reopen an exhausted window or increase
the five-attempt allowance. The request ID is idempotent and the operation
requires `operations:write` plus the existing CSRF/origin checks.

## Versioning and reading

Algorithmic and AI variants are immutable rows with increasing versions. Event
projections are persisted before the digest row; a repository without the event
port remains supported for lightweight tests and backwards-compatible tools.
Only one
version is `published` at a time; earlier versions become `superseded` but remain
available for audit. The ntfy body links to `/digests/<digest-key>`, where the
authenticated reader shows the current version and evidence items.

## Historical missing-digest incident: 2026-09-16

Production v0.18.1 failed to publish `daily:2026-09-16`. Multiple batch clusters
resolved to the same historical event key but were returned as separate digest
items. `save_digest` rejected them, including the AI-failure algorithmic fallback;
the five-attempt allowance was consumed without establishing a published fallback
or retry row. This was not just a model outage. v0.19.0 recombines these identities
and has an actual SQLite save/publication regression test. Check publication and
outbox status independently of the API-attempt counter when diagnosing missing
reports; an exhausted allowance does not prove an algorithmic digest was sent.

The errors observed on September 17 at 00:50–01:19 UTC preceded the v0.19.0
deployment around 01:23 UTC. They are historical evidence, not evidence of a
regression in v0.20.0. The September 17 report was published as an API version.
The new event-page uniqueness guard is additional defensive coverage; it is not
presented as a reproduced production race. All bounded reports are retained in
the selected evidence, including multiple updates from one source; representative
links remain one per source. Further work is tracked in `ROADMAP.md`.
