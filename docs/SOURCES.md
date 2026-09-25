# Source lifecycle and reviewed coverage

## Catalog and adapters

The catalog in `news_catalog.py` is inert reviewed metadata. It declares exact
HTTPS endpoints, host allowlists, access model, content policy, region, source
tier, default importance, and evidence. Runtime sources are managed configuration
and are instantiated through provider/collector ports. A catalog entry alone does
not run a collector.

Before a source is enabled by a rollout, the operations script performs a real
network fetch and parser pass. If any new source fails, no configuration revision
is written. First success establishes a baseline, so adding an existing feed does
not flood ntfy with historical entries.

Offline parser replay fixtures in `tests/fixtures/` retain short attributed
public RSS/RDF and official HTML excerpts. `tests/test_source_replay.py` checks
stable IDs, publication times, publisher metadata, duplicate filtering and
fail-closed behavior when a page layout or encoding changes. Add a reviewed
fixture when a source-specific parser is introduced or repaired.

## Current policy

Commercial sources retain feed metadata/excerpts and original links. Public
first-party sources may queue bounded text extraction for published HTML/PDF.
Exact host allowlists, public-address DNS checks, manual redirect validation,
timeouts, compressed/output size limits, and content-type checks constrain access.

Source regions use the macro taxonomy `EAST_ASIA`, `NORTH_AMERICA`, `EUROPE`,
`AUSTRALIA_OCEANIA`, `SOUTHEAST_ASIA`, `MIDDLE_EAST`, `SOUTH_AMERICA`, and
`AFRICA`, with `GLOBAL`/`OTHER` fallbacks. Historical `CN`, `JP`, and `US`
values remain accepted and are mapped to their macro families only for ranking;
there is no forced region quota.

Normal government, science, and corporate announcements enter the digest.
Auditable weighted rules send immediate messages only for high-signal patterns
such as major emergencies, material monetary-policy changes, severe disasters,
or embassy security alerts. A source being primary does not mean every item is
urgent.

The JMA nationwide feed is a deliberately stricter exception. It is sampled
every six hours at importance 1, routine local advisories are discarded before storage,
and retained exceptional-hazard evidence is never directly notification-eligible.
USGS significant earthquakes and independently reviewed global reporting decide
whether a disaster has enough international impact to interrupt the user.

## Source diagnostics and long-term quality

In the administration **监测来源** page, use **查看来源诊断** to inspect an
enabled or disabled source. The read-only route
`GET /api/source-health/<source_id>` uses `SourceHealthRepository`; SQL remains
inside the SQLite adapter. It reads one consistent transaction and never creates
collector state or changes quality weights. No schema migration is required.

The fields deliberately describe different things:

| Field | Evidence and interpretation |
|---|---|
| Last success / consecutive failures | Current collector state; a recovered failure is not an active outage. |
| Cumulative success rate | Persisted `success_count / poll_count`; a successful 304 counts as a successful poll. Counters include no per-attempt transport retry history. The start timestamp is unknown for migrated counters. |
| 24-hour / 7-day output | Distinct retained observations first ingested in the requested window, not all articles published then. Shared-scope deduplication attributes an item only to its first stored source. |
| Full-text coverage | Distinct observations in that 7-day ingestion cohort with a non-empty `full_text` or `document`; multiple document versions count once. This is content availability, not extraction success rate. |
| Content queue / error kinds | Current statuses of enrichment jobs attached to the same cohort. Metadata-only sources and first-poll baseline articles need not have such jobs. |

There is no per-poll history table, so `window_success_rate` is explicitly null;
the UI must not label cumulative counters as a 7-day success rate. Short
observation retention can make output counts incomplete; all counts refer to
data still retained. The diagnostic timestamp is displayed and refresh failures
retain the last readable snapshot with an error. This page does not trigger any
network request to the publisher. Connection tests remain a separate, bounded,
read-only action in the source editor; enable/disable still creates a managed
configuration revision.

**信源质量 → 权重调整记录** shows the existing audited feedback, override and
override-removal operations (latest 100), including actor, time and reason.
Quality remains based on explicit feedback: a 90-day decay half-life, at least
30 effective samples spanning at least 90 days, an 8-positive/2-negative prior,
automatic weights between 0.70 and 1.15, and at most 0.05 change per 30 days.
Manual bounded overrides take precedence. Transport failures and missing paid
article bodies do not become negative quality votes. Existing feedback/audit
records are preserved to support long-horizon evidence and accountability.
The UI also exposes the formula: each sample decays by `0.5^(age_days/90)`,
score is `(8 + positive) / (10 + positive + negative)`, and target weight is
`1 + (score - 0.8) * 0.75`, followed by eligibility, bounds and rate limiting.
Neutral votes count toward effective sample size but not the score denominator.

To roll back this UI/API addition, use the previous compatible release; no
source configuration or historical evidence is rewritten. Exact 7-day poll
availability remains deferred until a bounded history repository has an agreed
retention and schema migration. It must record every completed poll, including
304 and failures, survive restarts, and expose missing coverage before its rate
can be displayed honestly.

## 2026-09-15 additions

- U.S. Embassy and Consulates in China official Alerts RSS remains a reviewed
  catalog candidate. It previously returned parseable items, but on 2026-09-16
  consistently returned HTTP 403 to the bounded EOS collector. It is excluded
  from the default atomic rollout because one broken endpoint would roll back
  every otherwise healthy source addition. The dedicated security rule remains
  available for a future stable official endpoint.
- NVIDIA Newsroom official RSS:
  `https://nvidianews.nvidia.com/rss.xml`. It provides first-party AI,
  semiconductor, product, and corporate announcements and is added to the daily
  technology coverage. It cannot replace independent reporting about customer
  restrictions or confidential enterprise policy.

The referenced Sina story, “英伟达，开始限制 Claude 使用了”, is a secondary
technology report about enterprises limiting model use around sensitive data.
Sina's discoverable public technology/finance RSS endpoints were tested but their
newest items were from 2018, so they were not enabled as if current. The system
instead adds the verified NVIDIA primary feed and retains its pluggable catalog
for future current Chinese technology sources. A source must be genuinely live;
headline relevance alone is not enough to waive freshness and licensing checks.

The UN OCHA RSS candidate is handled the same way: its endpoint returned HTTP
406 (bot-activity rejection) to the EOS collector on 2026-09-16. It remains in
the reviewed catalog but is not part of the default activation set.

The Australian Prime Minister RSS candidate returned HTTP 403 during the
production atomic rollout on 2026-09-16. It also remains catalog-only; the
failed probe made no configuration change and the other verified sources were
re-probed before a new rollout attempt.

## Activation and change control

Use `scripts/operations/activate_news_sources.py` with the active configuration
revision. Run without `--apply` first; it lists additions and probes every one.
The planner is idempotent and extends existing reviewed rules without replacing
operator-edited thresholds or patterns. With `--apply`, one validated immutable
configuration revision is written. Then wait for Argus to acknowledge that exact
revision and verify every new source baseline and failure counter.

Disabling a source is an operator choice and rollout code must preserve it. A feed
that becomes stale, forbidden, or structurally invalid should be disabled or
marked unavailable rather than silently scraped through an unofficial mirror.
