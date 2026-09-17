# Daily digest policy

## Deterministic selection

At each configured local wall-clock boundary, the scheduler reads the preceding
local-day window. It includes observations handled as `digest` and those already
sent as `immediate`, so a notification seen briefly during the day is still
available in the daily review.

Candidates are read from the continuously maintained event pool also used by the
daily-events reader. Missing observations in the target period are projected
before selection; async preparation yields between batches of 50, allowing other
daemon workers to continue. The synchronous builder remains available to offline
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

The configured `item_limit` is a hard ceiling, currently 50. The actual count is
chosen without AI:

```text
signal_mass = sum(clamp(cluster.score, 1, 6))
score_target = ceil(signal_mass / 4)
selected_count = min(item_limit,
                     max(1, score_target, number_of_immediate_clusters))
```

This makes quiet days shorter and dense, high-value days longer. The calculation
is deterministic, explainable, and covered by tests. Empty days remain empty.

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
2. It stores retry state in SQLite and retries four more times, every 75 minutes,
   within the following five hours. A restart does not lose the schedule.
3. The first successful retry creates and publishes a new immutable AI version,
   supersedes the prior published version, and sends a second digest notification.
4. If all five attempts fail, the state is marked failed and the day counts toward
   the consecutive-failure streak. The algorithmic report remains readable.
5. Three consecutive local digest days with fully exhausted attempts enqueue one
   high-priority operator notification. Reprocessing the same day is idempotent.
   The next successful AI digest resets the streak.

Persistent retry tables are `digest_retry_state`, `digest_api_usage`, and
`digest_failure_state` (introduced in schema 15; the current database schema is
17). Errors are sanitized before persistence and
notification. The outbox remains responsible for actual delivery and retry.

## Versioning and reading

Algorithmic and AI variants are immutable rows with increasing versions. Event
projections are persisted before the digest row; a repository without the event
port remains supported for lightweight tests and backwards-compatible tools.
Only one
version is `published` at a time; earlier versions become `superseded` but remain
available for audit. The ntfy body links to `/digests/<digest-key>`, where the
authenticated reader shows the current version and evidence items.

## Missing-digest incident: 2026-09-16

Production v0.18.1 failed to publish `daily:2026-09-16`. Multiple batch clusters
resolved to the same historical event key but were returned as separate digest
items. `save_digest` rejected them, including the AI-failure algorithmic fallback;
the five-attempt allowance was consumed without establishing a published fallback
or retry row. This was not just a model outage. v0.19.0 recombines these identities
and has an actual SQLite save/publication regression test. Check publication and
outbox status independently of the API-attempt counter when diagnosing missing
reports; an exhausted allowance does not prove an algorithmic digest was sent.
