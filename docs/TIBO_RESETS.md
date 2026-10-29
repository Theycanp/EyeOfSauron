# Tibo / Codex reset-announcement subscription

This opt-in operations profile watches reset announcements originally posted by
[`@thsottiaux`](https://x.com/thsottiaux). It uses the existing RSS/Atom adapter,
weighted-rule engine, revisioned configuration and notification outbox. It adds
no daemon, schema migration, model call or paid API requirement.

## Current behavior

| Setting | Value |
| --- | --- |
| Source ID | `tibo_codex_resets` |
| Public Atom endpoint | `https://tokenusage.dev/api/resets.atom` |
| Provenance | Tibo posts relayed/classified by tokenusage.dev; `secondary` tier |
| Topic / default importance | `technology` / 4 |
| Polling | Every 60 seconds; 15-second timeout, at most two attempts |
| Network limits | HTTPS, exact `tokenusage.dev` / `x.com` allowlist, 256 KiB |
| Rule ID | `tibo_codex_reset_notice` |
| Notification | Priority 4, normal **news** category route |
| Eligible age | At most 48 hours old; the normal one-hour future-time tolerance applies |
| First successful poll | Establish a baseline without sending historical announcements |

The rule matches only this source's Codex reset-announcement titles. Automatic
resets, banked grants and combined announcements can qualify. It does not wait
for ordinary major-news scoring, AI processing or the daily digest. The message
keeps source attribution, the original X URL and the feed's qualification that
this is not proof of any individual account's reset. Notification routing,
mute settings and durable delivery retries remain owned by the existing
[notification subsystem](NOTIFICATIONS.md).

An eligible **banked grant** announcement does not mean the user has redeemed
it. A third-party announcement record is also not a verified completed global
reset. No completion-stage or personal-account notification is implemented by
this profile.

## Source investigation and limits

The provider publishes its interface and reuse terms at
[`tokenusage.dev/developers`](https://tokenusage.dev/developers). Its Atom feed
contains stable original-post URLs, publication/detection timestamps and credit,
but no post text or completion-stage field. Its JSON endpoint,
`https://tokenusage.dev/api/resets`, additionally exposes `lastPolledAt` for the
provider's most recent successful timeline fetch. Neither a successful HTTP
response nor `lastPolledAt` proves that classification was complete.

In the 2026-10-28 investigation, the provider recorded
[the user's example announcement](https://x.com/thsottiaux/status/2115273064481218829)
about 18 seconds after publication. The separate
[completion post](https://x.com/thsottiaux/status/2115330127886463244) was not in
that provider's JSON/Atom window. This is evidence of a coverage gap, not a
long-term latency or availability guarantee. The timeline had 14 records when
the initial production baseline was established.

Other reviewed candidates:

- [WhenReset](https://whenreset.dev/) exposes
  `https://whenreset.dev/api/reset-feed?provider=codex`. It contained the
  completion-post link but not the example announcement. Some entries combine
  announcement links with edited summaries or inferred completion times;
  third-party labels must not be treated as original text or proof of completion.
  It is not enabled as an automatic fallback by this profile.
- [codexresets.org](https://codexresets.org/codex-reset) provides a public history
  page, but a stable subscription contract was not established in this review.
- A public FxTwitter lookup could retrieve these known post IDs, including the
  completion post's quoted context. It was not validated as an account-timeline
  discovery interface and is not a complete subscription.
- Anonymous X HTML requests returned 403. The
  [official X stream](https://docs.x.com/x-api/posts/filtered-stream/introduction)
  is a candidate for a direct primary channel when a developer provides
  credentials and authorizes its paid usage. It is not configured by this script.

The 60-second interval respects the provider's documented minimum of 15 seconds;
use canonical URLs without cache-busting query strings. Keep **Data:
tokenusage.dev** linked to `https://tokenusage.dev/resets` when displaying or
relaying records. The fixture's rights statement carries the same condition.

## Preview and activation

Run from the repository or a release containing the script, with the project's
Python dependencies installed and permission to access the configured state.
Use the current configuration revision, not a revision copied from an old report:

```sh
python scripts/operations/activate_tibo_reset_monitor.py \
  --config /etc/argus/config.toml --expect-revision 25
```

The example revision `25` must be replaced with the instance's current revision;
use `0` only when there is no managed revision yet. The script requires an
explicit configuration path and uses that instance's configured database and
managed-configuration paths. It does not depend on a particular user's home
directory or write SQL directly.

Preview validates the complete planned configuration, fetches and parses the
actual feed, validates original-post links/dates, and replays the dedicated rule
without ingesting observations or enqueuing alerts. It prints the newest
publication age, but an old announcement is not itself an outage: there may be
no reset for weeks. `upstream_poll_freshness: not_checked` explicitly means
the provider's JSON heartbeat was not checked.

Repeat the same command with `--apply` to create one audited immutable revision.
The write uses `ManagedConfigStore` and a compare-and-swap revision. A mismatch,
failed fetch/parser/replay or conflicting operator-edited source/rule prevents
activation. An identical existing profile is a no-op, even with `--apply`.
Other sources, rules, analysis, digests and notification routes are preserved.

After applying, verify the engine acknowledges the new revision, the source is
enabled with a successful baseline, and no historical notifications were sent.
The daemon observes the revision and follows the normal managed-config restart
workflow; do not also edit bootstrap TOML or manipulate the database manually.

## Failures, management and rollback

- Use the source diagnostics page for collector success/failure counters. The
  normal consecutive-failure escalation and recovery rules apply to HTTP/parser
  failures. A third-party delay is separate from EOS's own collection interval.
- A provider can stop discovering posts while continuing to serve valid Atom.
  The runtime currently cannot detect that using `lastPolledAt`; this profile
  has neither a dedicated upstream-heartbeat alarm nor automatic dual-source
  failover. Do not advertise it as guaranteed complete monitoring.
- Disable `tibo_codex_resets` through managed source settings to stop collection.
  Edit the dedicated rule to change notification priority. Manage the **news**
  route separately if its destination should change.
- Reverting or removing this operations script does not undo a configuration
  already activated in an instance. Disable/remove this profile through a new
  audited revision. Do not restore an old entire configuration and overwrite
  unrelated operator changes.

## Validation and remaining work

`tests/test_tibo_reset_operations.py` replays short attributed automatic/banked
Atom records offline and exercises idempotence, operator-edit protection, source
and age filtering, preview versus apply, failed probes and revision conflicts in
temporary databases. The tests never call a live endpoint or send ntfy messages.
The fixture represents announcements only and cannot validate completion logic.

Before claiming reliable announcement **and** completion coverage, implement and
verify all of the following through existing collector/event/repository ports:

1. Detect both phases, using original author, timestamps, reply/quote context and
   the relevant reset event. A completion message may not contain `reset`.
2. Deduplicate an original post across sources while allowing a later completion
   phase to notify once. Never infer completion just from a countdown ending.
3. Track upstream fetch freshness and parser/classifier failure independently;
   missing observations, missing heartbeats and genuine no-news periods must be
   distinguishable across restart and recovery.
4. Validate an independently operating fallback or official X channel, including
   disconnection catch-up, paid-budget exhaustion and alerting. Credentials and
   spending authorization are required before enabling paid calls.
5. Keep personal-account allowance recovery separate from public announcements;
   confirm which account/window is observed before reporting it as restored.

These are documented follow-ups, not features delivered by this operations-only
change. The current production instance's revision, source count and live
acceptance evidence belong in its host handoff rather than hard-coded defaults.
