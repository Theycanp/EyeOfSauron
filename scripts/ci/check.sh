#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$project_root"

if [[ -x .venv/bin/coverage ]]; then
  export PATH="$project_root/.venv/bin:$PATH"
fi

annotate_failure() {
  local title="$1"
  local log_file="$2"
  local summary
  summary="$(tail -n 100 "$log_file")"
  summary="${summary//'%'/'%25'}"
  summary="${summary//$'\r'/'%0D'}"
  summary="${summary//$'\n'/'%0A'}"
  if [[ ${#summary} -gt 6000 ]]; then
    summary="${summary: -6000}"
  fi
  if [[ "${GITHUB_ACTIONS:-}" == "true" ]]; then
    echo "::error title=${title}::${summary}"
    {
      echo "### ${title}"
      echo
      echo '```text'
      tail -n 100 "$log_file"
      echo '```'
    } >> "$GITHUB_STEP_SUMMARY"
  fi
}

run_backend() {
  mkdir -p artifacts
  python --version
  ruff check src tests scripts
  mypy
  bandit --recursive src/argus --exclude src/argus/admin_web \
    --severity-level high --confidence-level high
  coverage erase
  set +e
  coverage run -m unittest discover -s tests -v 2>&1 | tee artifacts/backend-tests.log
  local test_status=${PIPESTATUS[0]}
  set -e
  if [[ $test_status -ne 0 ]]; then
    annotate_failure "Backend test suite failed" artifacts/backend-tests.log
    return "$test_status"
  fi
  coverage report
  coverage xml
  python -m argus --config config/argus.example.toml check-config
}

bundle_manifest() {
  find src/argus/admin_web -type f -print0 \
    | sort -z \
    | xargs -0 sha256sum
}

run_frontend() {
  local before_bundle after_bundle
  before_bundle="$(mktemp)"
  after_bundle="$(mktemp)"
  bundle_manifest > "$before_bundle"
  if ! (
    cd frontend
    node --version
    npm --version
    npm run lint
    npm run typecheck
    npm run test
    npm run test:e2e
    npm run build
  ); then
    rm -f "$before_bundle" "$after_bundle"
    return 1
  fi
  bundle_manifest > "$after_bundle"
  if ! diff -u "$before_bundle" "$after_bundle"; then
    rm -f "$before_bundle" "$after_bundle"
    echo "frontend build changed the committed deployable bundle" >&2
    return 1
  fi
  rm -f "$before_bundle" "$after_bundle"
  test -s src/argus/admin_web/index.html
  find src/argus/admin_web/assets -maxdepth 1 -type f -name '*.js' -size +0c -print -quit \
    | grep -q .
  find src/argus/admin_web/assets -maxdepth 1 -type f -name '*.css' -size +0c -print -quit \
    | grep -q .
}

case "${1:-all}" in
  backend)
    run_backend
    ;;
  frontend)
    run_frontend
    ;;
  all)
    run_backend
    run_frontend
    ;;
  *)
    echo "usage: $0 [backend|frontend|all]" >&2
    exit 2
    ;;
esac
