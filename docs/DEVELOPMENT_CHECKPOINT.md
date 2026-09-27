# N1-N6 delivery checkpoint

Development resumed at the user's request on 2026-09-27. This records the release
candidate, not a claim that unfinished work is deployed. Actual production
acceptance and recovery paths belong in the server handover.

## Scope

| Stage | Implemented boundary | Reference |
| --- | --- | --- |
| N1 | Real official PDF replay; per-URL 404/410; bounded audited parser repair, excluding blocked/dead URLs | `CONTENT.md` |
| N2 | Version/job/hit-denominator diagnostics; active-version bounded retry/backfill; report ID reuse protection | `EVENT_FACTS.md` |
| N3 | Durable UTC daily HTTP budgets; independent channel enable/interval controls and admin panel | `WEATHER.md` |
| N4 | Human labels with explicit sample denominators; first/phase/upgrade/repost/cancelled notification contracts | `EVENTS.md` |
| N5 | Frozen digest Claim/Evidence; explicit primary-proof correction with atomic audit/outbox | `DIGESTS.md`, `EVENT_FACTS.md` |
| N6 | Conditional QWeather 24h fallback, source/time/unit metadata and durable cooldown | `WEATHER.md` |

N7 off-host backup is excluded by the user. Content repair and manual correction
are authenticated API operations, not dedicated browser editors. Labels do not
establish full-corpus accuracy; manual correction is not prose inference. No
broker, new database or second clustering pipeline was added.

## Verification

- Development version `0.27.0`, schema 28; rollback across the schema boundary
  requires the matching pre-release database backup.
- Focused tests cover permission/CSRF/origin, explicit primary confirmation,
  correction atomicity/replay, legacy NULL facts, independently truncated graph
  bounds, report ID reuse, concurrent migration and midnight request cooldown.
- Final UI renders were inspected on desktop/mobile without overflow/overlap.
  Complete local gate passed: 582 backend tests at 80% coverage; frontend
  lint/types/build, 76 unit tests and 26 E2E cases passed.
- Real Commission PDF replay runs the bounded production extractor offline.
- A production SQLite online-backup copy migrated to schema 28 with unchanged
  principal row counts, integrity `ok` and zero foreign-key violations. It did
  not write the live database.
- A read-only QWeather JWT probe returned 24 valid hourly records through the
  new adapter; no production state or notification was written.
- Runtime Python and frontend production dependency audits found no known
  vulnerabilities.

Required GitHub backend/frontend checks must pass on the committed candidate
before merge. Activation uses the immutable release scripts;
verify precise main commit, schema, revision, services, actual provider polls,
queues, public authentication boundary and logs before reporting completion.
