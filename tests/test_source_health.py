from dataclasses import replace
from pathlib import Path

from argus.content import ContentDocumentDraft, ContentFetchRequest, ContentLevel
from argus.database import Database
from argus.models import FeedFetchResult
from argus.rules import RuleSet
from helpers import observation


NOW = 1789704000
SOURCE = "bloomberg_markets"


def ingest(database, now, *items):
    return database.record_source_success(
        SOURCE, FeedFetchResult(tuple(items), None, None), RuleSet.from_config((), "eos"),
        now, "eos",
    )


def test_health_distinguishes_recovered_failure_from_cumulative_counters(tmp_path: Path):
    database = Database(tmp_path / "state.db")
    try:
        ingest(database, NOW - 100)
        database.record_source_failure(SOURCE, "temporary failure", 3, "eos", NOW - 50)
        ingest(database, NOW)
        changes = database.connection.total_changes
        report = database.get_source_health(SOURCE, now=NOW)
        assert report["current"]["consecutive_failures"] == 0
        assert report["current"]["last_error"] is None
        assert report["polling"]["failures"] == 1
        assert report["polling"]["success_rate"] == 2 / 3
        assert report["polling"]["window_success_rate"] is None
        assert report["evidence"]["full_text_coverage"] is None
        assert database.connection.total_changes == changes
        assert database.get_source_health("unknown", now=NOW) is None
        assert database.connection.total_changes == changes
    finally:
        database.close()


def test_health_counts_ingestion_windows_and_distinct_article_bodies(tmp_path: Path):
    database = Database(tmp_path / "state.db")
    try:
        ingest(database, NOW - 8 * 86400, observation("old", "Outside retained window"))
        ingest(database, NOW - 2 * 86400, observation("two-days", "Previous day"))
        docs = (
            ContentDocumentDraft(ContentLevel.FULL_TEXT, "feed", "Full body one"),
            ContentDocumentDraft(ContentLevel.DOCUMENT, "public_html", "Another extraction"),
        )
        item = replace(
            observation("recent", "Recently ingested old publication", timestamp=NOW - 30 * 86400),
            content_documents=docs,
        )
        ingest(database, NOW, item)
        ingest(database, NOW, item)  # deduplication must not inflate output
        ingest(database, NOW + 1, observation("future", "Not in this snapshot"))
        report = database.get_source_health(SOURCE, now=NOW)
        assert report["evidence"]["observations_24h"] == 1
        assert report["evidence"]["observations_7d"] == 2
        assert report["evidence"]["with_full_text"] == 1
        assert report["evidence"]["full_text_coverage"] == 0.5
    finally:
        database.close()


def test_health_content_failure_does_not_mark_collector_unhealthy(tmp_path: Path):
    database = Database(tmp_path / "state.db")
    try:
        ingest(database, NOW - 20)
        item = replace(observation("blocked", "Public document"), content_fetch=ContentFetchRequest(
            "https://example.org/news", ("example.org",),
        ))
        ingest(database, NOW - 10, item)
        job = database.claim_content_fetch(NOW)
        assert job is not None
        database.fail_content_fetch(
            job, "forbidden", NOW, NOW, retryable=False, failure_kind="http_403",
        )
        report = database.get_source_health(SOURCE, now=NOW)
        assert report["polling"]["success_rate"] == 1
        assert report["current"]["consecutive_failures"] == 0
        assert report["evidence"]["content_jobs"] == {"dead": 1}
        assert report["evidence"]["content_failure_kinds"] == {"http_403": 1}
    finally:
        database.close()
