# Compatibility policy

## Supported runtime

- Python: 3.12 or newer within the same major release.
- Operating system: Linux with systemd; production templates target Ubuntu
  24.04-class systems.
- Database: SQLite supplied by the supported Python runtime.
- Frontend build: Node 22 and the exact npm dependency graph in
  `frontend/package-lock.json`.
- Production frontend runtime: a modern browser; Node is not installed or run as
  part of the service.

Argus intentionally has no third-party Python runtime dependency. CI and release
tools in `requirements/ci.txt` are development dependencies only.

## Configuration compatibility

Unknown keys are rejected. A new release must continue reading the documented
configuration of the preceding minor release or provide an explicit migration.
Secrets remain environment references and must never move into TOML, managed
JSON, SQLite, logs, or release artifacts.

## Database compatibility

`PRAGMA user_version` is the database compatibility boundary. Forward migrations
must be transactional, idempotent, and tested from every supported prior schema.
A newer schema is not assumed to be readable by older code. Downgrades therefore
require the matching pre-release backup unless the target release explicitly
documents support for the live schema.

## API and adapter compatibility

The loopback management API is internal until a stable public versioning scheme
is declared. UI and backend changes must ship together. Collector, rule,
repository, and notifier contracts should remain backward-compatible within a
minor release; a breaking contract change requires coordinated callers and
regression tests.

External providers are not controlled by this project. Each adapter must bound
timeouts and response sizes, validate destinations, reject unsafe redirects,
and fail closed when required credentials or capabilities are absent.
