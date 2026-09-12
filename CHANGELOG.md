# Changelog

All notable changes to EyeOfSauron are documented here. The project follows
Semantic Versioning and the Keep a Changelog structure.

## [Unreleased]

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

[Unreleased]: https://github.com/Theycanp/EyeOfSauron/compare/v0.15.0...HEAD
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
