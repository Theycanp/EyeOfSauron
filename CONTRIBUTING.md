# Contributing to EyeOfSauron

EyeOfSauron is a small modular monolith. Changes should preserve clear adapter,
rule, persistence, delivery, and management boundaries instead of adding a new
framework or service by default.

## Development setup

Argus has no third-party runtime dependency. Use Python 3.12 or newer and install
CI-only tools in an isolated environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements/ci.txt
python -m pip install --no-deps -e .
```

The admin UI uses Node 22 and npm:

```bash
cd frontend
npm ci
```

Never add credentials, provider tokens, private feed URLs, production databases,
or generated host configuration to the repository.

## Required checks

Before opening a pull request, run the checks relevant to the change:

```bash
ruff check src tests scripts
mypy
bandit --recursive src/argus --exclude src/argus/admin_web \
  --severity-level high --confidence-level high
coverage run -m unittest discover -s tests -v
coverage report
argus --config config/argus.example.toml check-config
bash -n scripts/operations/*.sh scripts/release/*.sh
```

For frontend work:

```bash
cd frontend
npm run lint
npm run typecheck
npm run test
npm run build
cd ..
git diff --exit-code -- src/argus/admin_web
```

The compiled admin UI under `src/argus/admin_web/` is a committed release
artifact. Any frontend source change must update it in the same pull request.

## Type-checking policy

Strict mypy currently gates the reliability boundary listed in `pyproject.toml`.
Do not silence new errors with broad module-level ignores. Expand the checked
file list as a module becomes clean, and document any narrow exception inline.
Ruff currently blocks parser failures, undefined names, and other correctness
errors; broader style rules may be added only after the existing tree passes.

## Tests and change design

- Add a regression test before or with every bug fix.
- Exercise failure and recovery paths for stateful behavior.
- Preserve at-least-once delivery and idempotent ingestion semantics.
- Keep external I/O behind an adapter or notifier contract.
- Treat configuration changes and database migrations as compatibility changes.
- Avoid adding a queue, cache, database, or framework without measured need.

## Commits and pull requests

Keep commits focused and write an imperative summary. Pull requests should state
the user-visible behavior, failure modes, migration impact, security impact, and
verification performed. Update `CHANGELOG.md` for externally observable changes.

Report vulnerabilities using the private process in `SECURITY.md`, not a public
issue. Releases follow `docs/RELEASES.md`; do not deploy directly from a dirty
working tree.
