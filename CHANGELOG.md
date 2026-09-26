# Changelog

All notable changes to EyeOfSauron are documented here. The project follows
Semantic Versioning and the Keep a Changelog structure.

## [Unreleased]

## [0.24.2] - 2026-09-26

### Fixed

- Supply the required altitude parameter and correctly formatted timezone
  offset to QWeather's solar-angle endpoint. `alt=0` is an explicit sea-level
  reference, not a measured elevation for the subscribed location.

## [0.24.1] - 2026-09-26

### Added

- Expand daily weather and the responsive admin view with humidity, wind,
  sunrise/sunset, lunar and Gregorian dates, supported festivals, air-quality
  estimates, moonrise/moonset, moon phase and optional solar elevation.
- Add schema 24 typed air-quality and astronomy records. Optional-provider
  failures do not overwrite the validated forecast or official warnings.
- Add an operator-requested, clearly labelled weather snapshot test through
  the normal notification outbox.

### Changed

- Label old forecasts as historical and hide stale optional data; distinguish
  European from US AQI and model estimates from station measurements.
- Document that migration effort never justifies weakening data ownership or
  domain contracts. Schema rollback requires a matching backup.

## [0.24.0] - 2026-09-26

### Added

- Add local weather subscription for the BUPT Shahe campus, an authenticated
  responsive management page, hourly validated Open-Meteo forecasts, one daily
  forecast, and durable rain-change / significant-weather alerts. Model forecasts
  are explicitly distinguished from official warnings.
- Add QWeather Ed25519 JWT integration for minute precipitation and relayed
  official alerts, independent outage tracking, durable episode dedupe and
  warning upgrade/cancellation handling. Hourly cross-check/fallback is deferred.
- Add schema 23 weather state and settings audit with repository/provider ports.

## [0.23.0] - 2026-09-26

### Added

- Persist conservative, source-backed event occurrence identities and bounded
  automatic fact jobs, preserving report-level evidence and audited manual
  merge/split behavior. Expose an event evidence graph in the admin reader.

### Fixed

- Keep distinct verified occurrences separate across batches and publication
  dates, and stop source configuration reloads from waiting indefinitely for
  blocked network calls.

## [0.22.6] - 2026-09-26

### Changed

- Reduce Japan Meteorological Agency sampling from every six hours to every
  twelve hours and retain only signals with plausible international impact:
  major-tsunami warnings, maximum intensity 6-upper/7 earthquakes, magnitude-8+
  earthquakes, and Nankai Trough megaquake warnings. Routine Japanese emergency
  weather, volcano, early-earthquake, and ordinary tsunami bulletins are no
  longer stored. All retained JMA evidence remains importance 1 and ineligible
  for direct notification; five-minute USGS and global-news disaster alerts are
  unchanged.

### Fixed

- Namespace finance-ministry entities by jurisdiction, preventing similar
  announcements from different countries or ambiguous multi-ministry stories
  from being merged as one event.
- Expose bounded event Claim/Evidence/Timeline and linked notification history
  through a single read snapshot, with explicit missing/truncated states in the
  responsive administration reader. Automatic fact projection remains pending.

## [0.22.5] - 2026-09-25

### Changed

- Sample JMA's nationwide bulletin feed every six hours, retaining its
  importance-1, exceptional-evidence-only, no-direct-notification policy.
  Apply the same defaults to backend and administration catalog drafts;
  five-minute USGS significant-earthquake coverage is unchanged.

### Fixed

- Persist digest projection and build failures separately from the five-attempt
  AI budget, expose their stage and sanitized error in digest run history, and
  clear the failure record after successful preparation.
- Add bounded offline source parser replays and an isolated database restore
  drill, with explicit evidence that database restore time is not full service
  recovery time.
- Exercise release activation, readiness failure, explicit rollback, and
  automatic recovery in an isolated CI drill without touching production.

## [0.22.4] - 2026-09-25

### Fixed

- Keep ntfy push previews below its 4 KiB attachment boundary. The 24 September
  daily digest body at exactly 4,096 UTF-8 bytes was rejected with HTTP 400;
  the full digest remains available through its detail URL.
- Freeze selected EventReport evidence when each digest version is saved, so
  later report changes and observation retention cannot rewrite published
  evidence. Existing older versions retain their legacy read behavior.
- Read all event-pool pages within the digest candidate budget before source
  filtering; eligible events on later pages are no longer hidden by page one.
- Stop low-score event volume alone from filling the daily 50-item ceiling.
  Production read-only replays for 18–25 September selected 21–38 items instead
  of 50 every day; previously published digests were not modified.
- Correct late-primary report relations in one event-projection transaction,
  preserving true context reports and marking a same-source official report as
  primary.
- Scope Claim evidence and Timeline links to their event, reject invalid
  supersedes chains, and make digest run publication lookup deterministic.

## [0.22.3] - 2026-09-23

### Changed

- Reduce the JMA nationwide bulletin feed to hourly collection at importance 1.
  Retain only reviewed exceptional-hazard categories as supporting evidence and
  mark every retained JMA observation ineligible for direct notifications.
  USGS significant earthquakes and global news remain the authorities for
  internationally significant disaster alerts.

### Fixed

- Keep the daemon watchdog responsive by yielding between expensive event-pool
  projections instead of processing a burst of 50 on the asyncio thread.
- Bound ntfy title and message fields by UTF-8 bytes, preserving the authenticated
  detail link for long Chinese daily summaries.
- Recover XML feeds containing isolated invalid UTF-8 bytes without accepting
  structurally malformed XML, restoring the affected ASEAN feed.
- Stop the war-declaration rule from matching unrelated words such as
  `declaration software`.
- Surface dead-letter notifications in the operations health report.

## [0.22.2] - 2026-09-21

### Changed

- Treat the Japan Meteorological Agency high-frequency feed as digest-only local
  evidence: poll it every 15 minutes at importance 2 and exclude it from all
  immediate notification rules. USGS significant earthquakes and reviewed
  global breaking-news sources retain their existing alert cadence and weight.

## [0.22.1] - 2026-09-21

### Fixed

- Coalesce repeated Japan Meteorological Agency warning bulletins into a
  persistent weather incident. Re-publications no longer send another alert;
  a changed hazard, severity, or normalized area receives a new notification,
  while every raw observation remains available to the event page and digest.

## [0.22.0] - 2026-09-21

### Added

- Add conservative multilingual event clustering for reviewed Chinese, English,
  and Japanese institution and action aliases, with unit-normalized amounts,
  contradiction retention, and persistent event-pool replay coverage.
- Add matching explanations for canonical signals and decisive action boundaries;
  primary, secondary, and social reports remain parallel evidence.

### Fixed

- Keep opposite rate decisions and unrelated capital actions in separate events
  even when they share an institution or ministry.

## [0.21.0] - 2026-09-21

### Added

- Add bounded digest generation attempt history, provider trace metadata, durable
  retry controls, sanitized errors, and an authenticated idempotent “retry now”
  operation.
- Add source health diagnostics that separate collector availability, retained
  ingestion, full-text coverage, content failures, and long-term quality audits.
- Add event lifecycle management, matching evidence, account preferences, global
  digest include/exclude choices, audited merge/split review, and responsive daily
  event workbench controls.
- Add content enrichment outcomes, rights-aware fallback display, and persistent
  host backoff for repeated blocked or transient document fetches.

### Changed

- Digest selection now consumes event editorial choices: global exclusions remove
  candidates and inclusions receive priority while the existing time window,
  adaptive count, source policy, and hard limit remain authoritative.

## [0.20.1] - 2026-09-21

### Fixed

- Recombine duplicate stable event identities at the digest event-pool boundary
  before adaptive selection and database publication, preserving reports and
  retaining same-source updates alongside their representative link.
- Place all four AI retries inside the five-hour window, anchored at hourly
  offsets so polling jitter does not lose the final retry. Expiry no longer
  invents an inference attempt and retry reconstruction preserves the deadline.
- Route invalid summarizer output through algorithmic fallback and durable
  retries; validate local evidence before reserving inference budget.
- Recover notifications and AI retry acknowledgement after a publication crash,
  retain the initial failure reason, prevent retry-window resets, and emit the
  consecutive-failure notification only at the third failed day.
- Keep one representative link per source in the digest reader while retaining
  all evidence updates, and count covered/quiet sources as healthy.

### Documentation

- Add an implementation roadmap with verified baseline, staged acceptance
  criteria, current progress and explicit remaining work; correct the historical
  duplicate-key incident timeline.

## [0.20.0] - 2026-09-17

### Fixed

- Share narrowly scoped central-bank decision identities between event matching
  and immediate notifications, normalizing institution aliases and constraining
  matches by local decision day, six-hour window and compatible rate direction.
- Suppress same/lower-priority duplicate decision notifications across rules
  using durable outbox history, while retaining announcement-to-direction
  updates and priority upgrades. Dead/cancelled delivery does not block updates;
  host/source recovery and explicit collector identities retain their old policy.
- Preserve market-reaction reports as context without losing original evidence.
- Restore daily-event filters from shareable URLs and browser navigation; retain
  existing readable results when a refresh fails.

### Added

- Add read-only real-alert replay and explicit, evidence-checked decision-group
  repair tools through a dedicated repository port. Repair retains observations,
  notifications, historical digest snapshots and old event containers, and
  records original assignments and source tiers in the event timeline.
- Add a compare-and-swap rollout to fill missing legacy Federal Reserve source
  metadata, preserving explicit settings and disabled-source choices.

## [0.19.0] - 2026-09-17

### Added

- Add a continuously maintained daily-event pool for all observations that pass
  deterministic triage, with bounded incremental projection and stable event
  identities independent from the daily digest schedule.
- Add an authenticated, cursor-paginated daily-events API and responsive React
  reader. It defaults to the latest 28 hours, supports a custom window, and can
  sort by recency or importance while retaining the underlying source reports.

### Changed

- Build daily digest candidates from the same persisted event pool used by the
  reader, while preserving immediate notifications, adaptive item counts,
  source-quality weighting, regional policy, and source-diversity ranking.
- Document the event-pool contract, operating boundaries, performance limits,
  troubleshooting steps, and the remaining event-lifecycle work.
- Yield between small event-backfill batches during async digest preparation so
  notification, collection, and heartbeat workers can continue during rollout.

### Fixed

- Recombine digest candidates that resolve to the same historical event identity,
  preserving all reports instead of rejecting the algorithmic fallback with
  duplicate cluster keys.
- Atomically persist event/report projections, including competing assignments,
  and retain delayed immediate notifications in their notification-day window.
- Keep historical digest evidence scoped to its frozen observation selection and
  count multiple feeds from one publisher as one independent confirmation.
- Read event pages in a consistent transaction, rank historical windows by their
  own evidence, and freeze time/report/alert boundaries across pagination.

## [0.18.1] - 2026-09-16

### Fixed

- Keep the Australian Prime Minister RSS catalog-only after its production
  rollout probe returned HTTP 403, so it cannot roll back healthy additions.

## [0.18.0] - 2026-09-16

### Added

- Add stable event, report, claim, evidence, and timeline repository contracts
  with schema 17 persistence and event-aware digest/admin presentation.
- Add reviewed regional primary and secondary sources across Europe,
  Australia/Oceania, Southeast Asia, the Middle East, Africa, South America,
  North America, and technology coverage; inaccessible candidates remain
  catalog-only instead of breaking atomic rollout.
- Add a maintainer workflow and PR checklist that require documentation,
  migration, test, rollback, and production-acceptance updates with each change.

### Changed

- Add the versioned `digest@4` regional-neutrality policy: Chinese is only the
  output language, selection remains evidence-led, similarly important topics
  retain reasonable cross-region coverage, and no mechanical quotas are used.
- Make event-centric clustering the default digest path, persist stable events
  and parallel source reports (schema 17), and keep legacy digest reads intact.
- Migrate the previous built-in region-weight profile to macro regions and
  explicitly activate `digest@4` during the reviewed production rollout while
  preserving operator-customized weights.

## [0.17.0] - 2026-09-15

### Added

- Publish an algorithmic digest immediately when AI synthesis fails, persist four
  retries over five hours, republish a successful AI version, and alert after
  three consecutive fully failed days.
- Choose the daily digest size deterministically from weighted information
  density, with the configured limit retained as a hard ceiling.
- Add verified U.S. Embassy in China Alerts RSS and NVIDIA Newsroom RSS sources,
  including a dedicated embassy security-alert rule.
- Add a maintainer-oriented documentation map covering architecture, configuration,
  digest policy, persistence, sources, operations, and fresh reproduction.

### Changed

- Digest AI attempt accounting is persistent and independent from per-observation
  analysis budget (schema 15).
- Use a browser-compatible, honest product User-Agent for public RSS endpoints
  that reject library-only agents.

## [0.16.3] - 2026-09-15

### Changed

- Let the digest model choose any number of topics that genuinely require
  deeper synthesis instead of imposing a twelve-topic selection limit.
- Let the model choose a concise, fact-complete summary length based on the
  material rather than recommending a fixed word range.
- Keep bounded request and response budgets as reliability safeguards, and
  preserve strict citation validation for every selected topic.

## [0.16.2] - 2026-09-15

### Fixed

- Give the model an explicit versioned contract for the index and synthesis
  stages so large digests perform semantic topic selection instead of always
  falling back to the deterministic top twelve.
- Deduplicate model-selected topic identifiers before synthesis.

## [0.16.1] - 2026-09-15

### Fixed

- Preserve the original daily topic numbers through staged AI selection so
  citations in the synthesized summary point to the correct visible items.

## [0.16.0] - 2026-09-15

### Added

- Configurable ordered model failover for local and remote analysis.
- Staged daily digest synthesis: full-topic indexing followed by focused evidence synthesis.

### Changed

- Daily digest model processing now retries the next configured model after transport or validation failures while preserving one logical budget reservation.
- The React settings page exposes local and remote fallback model lists.

## [0.15.3] - 2026-09-14

### Changed

- Raise the default daily digest limit from 30 to 50 clustered topics.

### Fixed

- Keep one representative article link per actual source in each digest topic,
  instead of presenting repeated updates from a high-frequency feed as dozens
  of separate sources. The reader now labels repeated observations as merged
  updates and also limits legacy digest links to the recorded source count.

## [0.15.2] - 2026-09-12

### Fixed

- Prevent host health probe subprocesses from inheriting Argus's
  `NOTIFY_SOCKET`, which caused misleading systemd "notification from non-main
  PID" warnings during routine checks.

## [0.15.1] - 2026-09-12

### Fixed

- Distinguish the model output limit `max_tokens` from credential-bearing
  fields while applying the same exact secret-field policy to writes and API
  response redaction.

## [0.15.0] - 2026-09-12

### Added

- Add bounded, citation-checked API synthesis for daily digests with an
  algorithmic fallback, shared call budget, versioned prompt, and UI controls.
- Add production-ready host health configuration and a regular React editor for
  disk, inode, memory, load and systemd unit checks.
- Add verified State Council and Japanese Ministry of Finance official indexes,
  plus current Economist, FT and Federal Reserve feeds to the rollout plan.

### Fixed

- Decode gzip RSS responses within compressed and decompressed size limits;
  this fixes the UN Chinese feed's intermittent failure/recovery loop.
- Preserve rule-selected immediate events through later semantic analysis and
  guarantee they remain eligible for the daily digest.
- Prevent the JMA category label from generating severe-weather false alerts;
  evaluate the actual bulletin text instead.
- Filter daily candidates to enabled sources before the database limit, retain
  fractional quality scores, and apply a soft source-diversity penalty.
- Keep failed analysis and digest form edits dirty, align browser limits with
  backend validation, and reject ambiguous model endpoint URLs.
- Make wildcard listener family checks accurate and prevent unknown disk probes
  from being reported as recovery.
- Resolve stale source-outage incidents when a source is explicitly disabled or
  removed, without sending a misleading recovery notification.
- Keep known binary official attachments as linked metadata instead of creating
  unsupported full-text extraction jobs.
- Align catalog confirmation text with the reviewed default-on behavior for
  public sources; credential-bound providers still default off.

## [0.14.0] - 2026-09-12

### Fixed

- Complete the requested CN/JP/US/international news rollout: a catalog template
  alone does not activate production collection. Add a probe-before-apply command
  with revision checks, idempotency and explicit daemon/baseline acceptance.
- Replace the empty NDRC RSS endpoint with its official announcement index; mark
  the stale WHO feed unavailable instead of treating HTTP 200 as fresh coverage.
- Preserve region, source tier, importance and topic when creating sources from
  the React catalog.

### Added

- Official announcement indexes for China's finance ministry, NDRC, statistics
  bureau, foreign ministry and science ministry, plus Japan's prime minister.
- RSS 1.0/RDF parsing for Japan's health ministry and JAXA, sharing existing
  transport, dates, identity and content-policy behavior.
- Reviewed activation of 16 additional feeds, conservative multilingual urgent
  rules, and a visual official-index editor. Japanese sources start at importance 4.

## [0.13.0] - 2026-09-10

### Added

- Add policy-aware content documents with Feed excerpts, authorized Feed full text,
  and bounded public official HTML/PDF extraction.
- Add a durable content-fetch queue with leases, retry/dead states, SSRF defenses,
  MIME/size/time limits, and isolated source-health reporting.
- Show the best available saved content and its provenance in notification details;
  commercial publishers remain metadata/excerpt-only by default.

### Changed

- Upgrade SQLite schema to 14 and include content tables in verified backups.
- Add explicit content-policy controls to the RSS source editor with copyright guidance.

## [0.12.0] - 2026-09-10

### Added

- Add administrator-created events with strict validation, dedicated RBAC,
  creator attribution, and atomic observation/incident/outbox persistence.
- Add a responsive event dialog that opens the saved detail immediately and
  exercises the same ntfy and daily-digest path as collected intelligence.
- Add backend transaction, validation, CSRF, role, and digest tests plus desktop
  and mobile browser coverage for the complete manual-event workflow.

### Fixed

- Make the local and GitHub frontend quality gate stop on its first failed
  lint, type, unit, browser, or build command instead of allowing a later
  successful command to mask the failure.

## [0.11.0] - 2026-09-10

### Added

- Add authenticated, responsive notification detail pages that preserve the
  exact observation, incident context, triage fields, and triggering evidence.
- Add desktop and mobile integration coverage for notification deep links that
  survive the login flow.

### Changed

- Route event-backed ntfy notification clicks to the saved EyeOfSauron detail
  instead of the publisher website, while retaining the original article as a
  secondary `查看原文` action.
- Give the administration origin its own validated `admin.public_base_url`;
  reminder and digest links retain their existing destinations.

### Fixed

- Keep the strict mypy gate scoped to its declared type-checking islands while
  retaining imported type information, instead of reporting unrelated legacy
  modules reached transitively.
- Remove a five-second CPU-speed assumption from memory-hard Argon2 HTTP tests,
  add actionable failure annotations, and use one shared local/Actions check
  entry point so CI failures are reproducible instead of opaque.
- Upgrade official Actions to their Node 24-compatible major versions.

## [0.10.2] - 2026-09-07

### Changed

- Show the audited news catalog without a hidden disclosure step and distinguish
  enabled, disabled, and not-yet-added feeds in the administration interface.
- Explain that catalog templates remain inactive until their generated draft is
  explicitly saved, and surface catalog notes for disabled managed sources.

### Fixed

- Install the pinned runtime dependency set in CI before running authentication
  tests, while retaining the no-dependency editable package verification step.

## [0.10.1] - 2026-09-07

### Fixed

- Keep daily digest SQLite work on the event-loop thread so scheduled
  generation does not fail from cross-thread connection use.

## [0.10.0] - 2026-09-07

### Added

- Add Argon2id username/password accounts, 14-day server-side Cookie sessions,
  strict CSRF/origin validation, login throttling, and security audit history.
- Add administrator, operator, and viewer roles with backend-enforced permissions.
- Add responsive account management for user creation, role and status changes,
  password resets, active-session visibility, and immediate session revocation.
- Add a hardened HTTPS reverse-proxy template for the public management origin.
- Add a guarded Certbot deploy hook that validates and reloads Nginx after renewal.

### Changed

- Keep the application listener on loopback and restrict the legacy emergency
  bearer credential to direct loopback requests only.
- Upgrade the SQLite schema to 13 for account, session, throttle, and audit state.

## [0.9.1] - 2026-09-07

- Include immediate observations in daily digests while retaining their real-time notification path.
- Add long-horizon, decayed source-quality scoring with conservative eligibility thresholds, manual overrides, feedback, and audit history.
- Add the authenticated admin quality-management page and API; quality weights affect digest ranking only.
- Upgrade the SQLite schema to 12 with an isolated persistence boundary for quality data.
- Rate-limit automatic reputation changes by elapsed time rather than page refresh frequency.

## [0.8.0] - 2026-09-06

### Added

- Durable information triage fields and an audited deterministic/local/API
  analysis pipeline with shadow mode, leases, versioned Prompts, and daily API budgets.
- Versioned cross-source daily digests, source coverage reporting, authenticated
  responsive reading views, and idempotent timezone-aware publication notices.
- Official Bank of Japan, JMA, NDRC, WHO, NASA, and USGS source templates with
  region, source-tier, topic, and default-importance metadata.
- Dedicated analysis and digest management APIs with revision conflict protection.

### Changed

- Adopt Apache-2.0 for the project, synchronize Python/frontend metadata and
  release manifests, and include project and bundled third-party notices.
  Previously published MIT snapshots, including v0.7.0, retain their MIT grants.

## [0.7.0] - 2026-09-05

### Added

- Online SQLite backup bundles with checksums, integrity metadata, verification,
  and guarded restore support.
- Immutable release packaging, health gates, bounded watchdog templates, and
  automatic state/code recovery when activation or rollback fails.
- CI gates for tests, coverage, Ruff, strict type-checking islands, Bandit,
  Python and npm dependency audits, wheel smoke tests, and CycloneDX SBOMs.
- Dependabot policy for Python tooling, frontend dependencies, and Actions.
- A provider capability registry, reviewed news-source catalog, asynchronous
  source-test jobs, dead-letter controls, and engine/source runtime telemetry.

### Changed

- Systemd templates now target the atomic `current` release symlink and define
  explicit memory, task, file-descriptor, start-rate, and startup limits.
- Operations documentation now uses WAL-safe backups and versioned releases.
- Managed configuration is revisioned in SQLite and applied by a bounded,
  systemd-supervised Argus restart instead of a manual restart workflow.
- The administration client is now strict TypeScript with provider-driven forms
  and modern responsive interaction states.

## [0.6.0] - 2026-09-05

### Changed

- Renamed the product to EyeOfSauron, with EOS as the abbreviation and Argus as
  the watcher engine, CLI, Python package, and service namespace.
- Added the React administration application and explicit event versus stateful
  incident semantics.

## [0.3.0] - 2026-09-04

### Added

- Durable incidents, managed configuration revisions, operational metrics, and
  hardened production service templates.

## [0.2.0] - 2026-09-04

### Added

- Extensible market, X, YouTube, IMAP, host-health, heartbeat, and MQTT support.

## [0.1.0] - 2026-09-02

### Added

- Initial Bloomberg RSS monitoring, weighted alert rules, SQLite outbox, ntfy
  delivery, failure/recovery notifications, and baseline suppression.

[Unreleased]: https://github.com/Theycanp/EyeOfSauron/compare/v0.16.3...HEAD
[0.16.3]: https://github.com/Theycanp/EyeOfSauron/compare/v0.16.2...v0.16.3
[0.16.2]: https://github.com/Theycanp/EyeOfSauron/compare/v0.16.1...v0.16.2
[0.16.1]: https://github.com/Theycanp/EyeOfSauron/compare/v0.16.0...v0.16.1
[0.16.0]: https://github.com/Theycanp/EyeOfSauron/compare/v0.15.3...v0.16.0
[0.15.3]: https://github.com/Theycanp/EyeOfSauron/compare/v0.15.2...v0.15.3
[0.15.2]: https://github.com/Theycanp/EyeOfSauron/compare/v0.15.1...v0.15.2
[0.15.1]: https://github.com/Theycanp/EyeOfSauron/compare/v0.15.0...v0.15.1
[0.15.0]: https://github.com/Theycanp/EyeOfSauron/compare/v0.14.0...v0.15.0
[0.14.0]: https://github.com/Theycanp/EyeOfSauron/compare/v0.13.0...v0.14.0
[0.13.0]: https://github.com/Theycanp/EyeOfSauron/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/Theycanp/EyeOfSauron/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/Theycanp/EyeOfSauron/compare/v0.10.2...v0.11.0
[0.10.2]: https://github.com/Theycanp/EyeOfSauron/compare/v0.10.1...v0.10.2
[0.10.1]: https://github.com/Theycanp/EyeOfSauron/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/Theycanp/EyeOfSauron/compare/v0.9.1...v0.10.0
[0.9.1]: https://github.com/Theycanp/EyeOfSauron/compare/v0.8.0...v0.9.1
[0.8.0]: https://github.com/Theycanp/EyeOfSauron/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/Theycanp/EyeOfSauron/compare/v0.3.0...v0.7.0
[0.6.0]: https://github.com/Theycanp/EyeOfSauron/commits/1e12855
[0.3.0]: https://github.com/Theycanp/EyeOfSauron/releases/tag/v0.3.0
[0.2.0]: https://github.com/Theycanp/EyeOfSauron/releases/tag/v0.2.0
[0.1.0]: https://github.com/Theycanp/EyeOfSauron/releases/tag/v0.1.0
