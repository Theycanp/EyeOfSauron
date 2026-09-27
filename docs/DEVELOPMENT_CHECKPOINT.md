# N1-N6 delivery record

Development resumed and was accepted on 2026-09-27. PR #69 passed the required
GitHub backend/frontend checks and merged as
`0b485b070c6fead2368d82bce15c4ed98a382208`. Release
`v0.27.0-0b485b0` was activated after an immutable-package preflight.
The server handover retains the live version and recovery path.

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
- Activation created a schema-27 pre-release backup, then migrated live SQLite
  to schema 28. Both services were active, desired/applied revision 22, all 45
  enabled sources had zero consecutive failures, outbox pending/sending was
  zero, database integrity was `ok` and foreign-key violations were zero.
- First production polls succeeded for Open-Meteo forecast/AQ and QWeather
  minute, alerts and astronomy. Conditional QWeather hourly remained at zero
  calls because the primary forecast was valid. Authenticated loopback reads
  of new weather/fact APIs succeeded; anonymous public weather API returned 401.

Rollback to pre-0.27 code requires the matching schema-27 pre-release backup,
not a code-only symlink switch. No test notification was sent in production.
