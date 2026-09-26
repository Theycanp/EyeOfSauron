# Reproduce a functional instance

This procedure reproduces the software and behavior. Production secrets, user
accounts, private state, DNS, certificates, and monitored account identifiers are
intentionally external and must be supplied by the operator.

## Build and test

Requirements are documented in `COMPATIBILITY.md`. Clone the repository, create
a Python virtual environment, install the pinned runtime and CI dependencies,
install the package without resolving a second dependency set, install frontend
dependencies from the lockfile, and run:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements/runtime.txt -r requirements/ci.txt
python -m pip install --no-deps -e .
```

```bash
scripts/ci/check.sh all
```

This runs backend unit/integration tests, coverage, Ruff, mypy, Bandit, dependency
checks, frontend type/unit tests, responsive Playwright tests, and reproducible
build/package checks. The generated React bundle under `src/argus/admin_web/`
must match the frontend source.

## Configure a host

1. Create a dedicated `argus` system user and private `/var/lib/argus` state path.
2. Copy and edit `config/argus.example.toml`. Keep database, managed-config,
   listener, public URL, and environment-file paths explicit.
3. Create root-owned secret environment files. Store only variable names in
   configuration.
4. Create a root-owned versioned Python virtual environment, install
   `requirements/runtime.txt`, and pass its interpreter as `ARGUS_PYTHON` to
   release prepare and activation. Package the exact tested commit using
   `scripts/release/package-release.sh`. See `RELEASES.md` for runtime rollback.
5. Install the verified archive using `scripts/release/install-release.sh`; do
   not copy a dirty working tree into `/opt`.
6. Create the first admin user with `scripts/operations/manage_admin_user.py`.
7. Publish the loopback admin service through a dedicated TLS reverse proxy and
   retain application-level auth. Never expose port 18080 directly.
8. Preview and apply reviewed sources with `activate_news_sources.py`, using the
   current active revision and requiring every new-source probe to pass.

## Acceptance

Confirm both services are active, release metadata matches the intended commit,
database integrity and schema are correct, desired and applied configuration
revisions match, the admin deep links work on desktop/mobile, new sources have
successful baselines, source failure counters are zero or explained, and the
outbox has no unexplained pending/dead backlog.
For schema 23 and later, verify the local-weather subscription's coordinates,
a fresh validated Open-Meteo poll, and the weather page's read/write permissions.
For QWeather, provision a separate Ed25519 private key readable only by the
service group, configure the five environment variables in `WEATHER.md` through
`/etc/argus/qweather.env`, and verify both endpoint success timestamps. A new
installation must obtain its own credentials; no provider key is bundled.
No weather alarm needs to be sent to production for this check; the transition
and dedupe scenarios are covered in the isolated tests documented in `WEATHER.md`.

Create a manual event from the admin UI to verify end-to-end outbox/ntfy/detail
routing. Generate a digest in a non-production test database to validate both
algorithm and AI paths; do not consume or rewrite a real day's production report
merely to test deployment. Finally create and verify a backup and document the
matching rollback release.
