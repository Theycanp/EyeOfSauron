# Saved content and public-document enrichment

Collection saves the title, original URL, and Feed evidence before any optional
public-document work. An enrichment failure never removes that evidence, marks
the RSS collector unhealthy, or blocks notification/digest selection. Commercial
media retain their allowed Feed content; this worker does not unlock paywalls.

## Two independent dimensions

`content_documents.level` describes what is actually stored: `metadata`,
`excerpt`, `full_text`, `document`, or separately generated `analysis`. The alert
detail API additionally returns `content_availability` with `level`,
`has_full_text`, and `fetch_outcome`. Generated analysis does not count as saved
source full text.

| Enrichment outcome | Meaning and next action |
| --- | --- |
| `not_requested` | Policy, initial baseline, or unsupported attachment did not queue enrichment. |
| `queued` / `fetching` | Waiting for a worker / holding a fetch lease. |
| `deferred` | Another document on the same hostname triggered a bounded pause. |
| `retrying` | A transient request failure has a durable next attempt. |
| `available` | A public document was extracted and saved. |
| `blocked` | HTTP 401/403; this URL is terminal and is not automatically retried. |
| `parser_failed` | Too little readable text or invalid PDF; this URL is terminal. |
| `unsupported` | MIME type or local extraction support is unavailable. |
| `unavailable` | Other terminal errors or retry budget exhausted. |

The existing `content_fetch.status` remains the queue state (`pending`, `leased`,
`retry`, `completed`, `dead`). It includes attempts, failure kind, and scheduled
time. UI wording distinguishes origin access restrictions, parsing limits, and
waiting for retry, while continuing to display the saved evidence level. A
`dead` enrichment task does not mean the news item or source is dead.

## Host backoff and recovery

`content_host_backoff_seconds` in `src/argus/content.py` owns the small policy:

| Failure | Same-host pause |
| --- | --- |
| HTTP 403 | 1 hour |
| HTTP 429 | 15 minutes |
| HTTP 408/425/5xx, DNS/URL/timeout/socket errors | 2 minutes |
| Empty text, HTTP 404, unsupported formats, other document-specific failures | None |

The repository derives unexpired pauses from existing task failure timestamps
and persists deferred tasks' `next_attempt_at`; no new database schema or broker
is required. Restarting does not erase the pause. Deferral consumes no attempt
and makes no outbound request or per-document error log. Retry failure timestamps
are not moved during deferral, so a pause cannot extend itself indefinitely.
Expired pauses let the next eligible document probe the origin; success allows
the remaining queue to run, a new access/network failure starts a new bounded
pause. A terminal URL is not revived by another page's successful probe.

Claiming checks up to 50 due jobs, defers those on paused hosts, and can claim an
unrelated host in the same transaction. Batches consisting entirely of paused
jobs are drained across subsequent worker ticks. This bounds each transaction;
there is no indefinite head-of-line block. RSS fetch scheduling is independent.

Expected terminal enrichment limits (403/404, short text, unsupported format)
are logged as warnings rather than engine errors. Real failures remain in task
records for diagnosis; this is not a promise that full text exists everywhere.
Dead task rows follow their observation's retention and cascade deletion.

## European Commission Press Corner

On 2026-09-21 the official article pages returned an Angular shell, while their
public print-PDF links returned readable documents. The website's own detail
component constructs `/commission/presscorner/api/files/document/print/{lang}/`
`{ref}/{REF}_{LANG}.pdf`. `public_document_url` uses that route only for exact
HTTPS `ec.europa.eu/commission/presscorner/detail/{two-letter-language}/`
`{type}_{two-digit-year}_{numeric-id}` URLs without a query, fragment, or port.
It does not inspect or execute site JavaScript at runtime.

The existing exact host allowlist, public-address DNS checks, every-redirect
validation, response/decompression limits, MIME checks, PDF signature, and
bounded `pdftotext` extraction all remain in force. The saved document records
its fetched canonical URL plus `metadata.requested_url` for the original page.
If the print route or local PDF support fails, the Feed remains the fallback.
There is no browser automation, unofficial mirror, or access-control bypass.
IAEA HTTP 403 is intentionally reported as blocked.

Read-only live validation on 2026-09-21 retrieved the official print PDF for
`ip_26_1900` and extracted 4,828 characters via the same production fetcher.
This proves that tested route and document, not that every Commission attachment
contains machine-readable text. No observations, notifications, or production
configuration were written by the probe.

## Verification and limitations

`tests/test_content.py` covers the narrow official URL mapping, access denial,
DNS/redirect/MIME/size validation, independent evidence levels, durable host
pauses, restart, unrelated-host progress, exhausted pause, and short-page
isolation. `tests/test_service.py` verifies source health remains independent.
The notification-detail frontend tests verify blocked/deferred explanations
alongside readable saved excerpts.

Existing historical dead tasks are not automatically requeued after an extractor
upgrade. That would recrawl historical records and needs an explicit bounded
repair workflow. Host pauses use recent task records and their normal retention;
if task volume becomes large, move this read projection behind the same
repository contract to an indexed host-health table rather than exposing SQL
to the worker. PDF extraction cannot recover images-only scanned text without
OCR, which is outside the current resource budget.
