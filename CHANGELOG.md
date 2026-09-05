# Changelog

All notable changes to EyeOfSauron are documented here. The project follows
Semantic Versioning and the Keep a Changelog structure.

## [Unreleased]

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

[Unreleased]: https://github.com/Theycanp/EyeOfSauron/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/Theycanp/EyeOfSauron/compare/v0.3.0...v0.7.0
[0.6.0]: https://github.com/Theycanp/EyeOfSauron/commits/1e12855
[0.3.0]: https://github.com/Theycanp/EyeOfSauron/releases/tag/v0.3.0
[0.2.0]: https://github.com/Theycanp/EyeOfSauron/releases/tag/v0.2.0
[0.1.0]: https://github.com/Theycanp/EyeOfSauron/releases/tag/v0.1.0
