from __future__ import annotations

import io
import gzip
import sqlite3
import tempfile
import unittest
import urllib.error
from dataclasses import replace
from email.message import Message
from pathlib import Path
from unittest.mock import Mock

from argus.content import (
    ContentDocumentDraft,
    ContentFetchError,
    ContentFetchRequest,
    ContentLevel,
    PublicDocumentFetcher,
    plain_text,
)
from argus.database import Database
from argus.models import FeedFetchResult
from argus.rules import RuleSet

from helpers import observation, production_config


NOW = 1_788_363_000
PUBLIC_IP = [(2, 1, 6, "", ("93.184.216.34", 443))]


class _Response(io.BytesIO):
    def __init__(self, payload: bytes, media_type: str, url: str) -> None:
        super().__init__(payload)
        self.headers = Message()
        self.headers["Content-Type"] = media_type
        self._url = url

    def geturl(self) -> str:
        return self._url


class ContentExtractionTests(unittest.TestCase):
    def test_html_reader_prefers_article_and_removes_active_content(self) -> None:
        body = plain_text(
            "<html><body><nav>menu</nav><article><h1>Policy decision</h1>"
            "<p>The central bank changed its policy stance after a formal vote.</p>"
            "<script>alert('x')</script><p>Implementation begins tomorrow.</p>"
            "</article><footer>copyright</footer></body></html>"
        )
        self.assertIn("Policy decision", body)
        self.assertIn("Implementation begins tomorrow", body)
        self.assertNotIn("menu", body)
        self.assertNotIn("alert", body)

    def test_fetcher_rejects_private_resolution_before_opening(self) -> None:
        opener = Mock()
        fetcher = PublicDocumentFetcher(
            opener=opener,
            resolver=lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))],
        )
        item = self._item()
        with self.assertRaisesRegex(ContentFetchError, "non-public"):
            fetcher.fetch(item)
        opener.open.assert_not_called()

    def test_fetcher_validates_redirect_target_and_mime_and_size(self) -> None:
        redirect_headers = Message()
        redirect_headers["Location"] = "https://private.example/document"
        redirect = urllib.error.HTTPError(
            "https://official.example/document", 302, "Found", redirect_headers, None
        )
        opener = Mock()
        opener.open.side_effect = redirect
        with self.assertRaisesRegex(ContentFetchError, "allowlist"):
            PublicDocumentFetcher(opener=opener, resolver=lambda *a, **k: PUBLIC_IP).fetch(
                self._item()
            )

        for response, message in (
            (_Response(b"{}", "application/json", "https://official.example/document"), "content type"),
            (_Response(b"x" * 1025, "text/plain", "https://official.example/document"), "size limit"),
        ):
            with self.subTest(message=message):
                opener = Mock()
                opener.open.return_value = response
                with self.assertRaisesRegex(ContentFetchError, message):
                    PublicDocumentFetcher(
                        opener=opener, resolver=lambda *a, **k: PUBLIC_IP
                    ).fetch(self._item(max_response_bytes=1024))

    def test_fetcher_extracts_html_and_pdf_as_plain_text(self) -> None:
        html = ("<article><h1>Official release</h1><p>" + "Public facts. " * 12 + "</p></article>").encode()
        opener = Mock()
        opener.open.return_value = _Response(
            html, "text/html; charset=utf-8", "https://official.example/release"
        )
        document = PublicDocumentFetcher(
            opener=opener, resolver=lambda *a, **k: PUBLIC_IP
        ).fetch(self._item())
        self.assertEqual(ContentLevel.FULL_TEXT, document.level)
        self.assertEqual("public_html", document.source_method)
        self.assertNotIn("<article>", document.body)

        opener.open.return_value = _Response(
            b"%PDF-1.7 payload", "application/pdf", "https://official.example/release.pdf"
        )
        pdf = PublicDocumentFetcher(
            opener=opener,
            resolver=lambda *a, **k: PUBLIC_IP,
            pdf_extractor=lambda payload, timeout: "Official PDF content. " * 8,
        ).fetch(self._item(url="https://official.example/release.pdf"))
        self.assertEqual(ContentLevel.DOCUMENT, pdf.level)
        self.assertEqual("public_pdf", pdf.source_method)

    def test_fetcher_decodes_bounded_gzip(self) -> None:
        html = ("<article><h1>Official release</h1><p>" + "Public facts. " * 12 + "</p></article>").encode()
        response = _Response(gzip.compress(html), "text/html; charset=utf-8", "https://official.example/release")
        response.headers["Content-Encoding"] = "gzip"
        opener = Mock()
        opener.open.return_value = response
        document = PublicDocumentFetcher(opener=opener, resolver=lambda *a, **k: PUBLIC_IP).fetch(self._item())
        self.assertIn("Official release", document.body)

    @staticmethod
    def _item(
        *, url: str = "https://official.example/document", max_response_bytes: int = 4096
    ):
        from argus.content import ContentFetchWorkItem

        return ContentFetchWorkItem(
            1,
            2,
            ContentFetchRequest(url, ("official.example",), max_response_bytes, 10),
            "lease",
            1,
        )


class ContentPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.config = production_config(Path(self.temporary.name))
        self.database = Database(self.config.service.database_path)
        self.rules = RuleSet.from_config(self.config.rules, self.config.ntfy.default_topic)

    def tearDown(self) -> None:
        self.database.close()
        self.temporary.cleanup()

    def _public_observation(self, identity: str):
        item = observation(identity, f"Breaking: official release {identity}", "Feed excerpt")
        return replace(
            item,
            content_documents=(
                ContentDocumentDraft(
                    ContentLevel.EXCERPT,
                    "rss_description",
                    "Feed excerpt",
                    canonical_url=item.url,
                ),
            ),
            content_fetch=ContentFetchRequest(
                item.url, ("www.bloomberg.com",), max_response_bytes=4096, timeout_seconds=10
            ),
        )

    def test_baseline_saves_feed_content_but_does_not_crawl_backlog(self) -> None:
        self.database.record_source_success(
            "bloomberg_markets",
            FeedFetchResult((self._public_observation("baseline"),), None, None),
            self.rules,
            NOW,
            "eos",
        )
        status = self.database.status()["content"]
        self.assertEqual(1, status["documents"]["excerpt"])
        self.assertEqual({}, status["fetch_jobs"])

    def test_new_item_has_durable_lease_retry_completion_and_detail(self) -> None:
        self.database.record_source_success(
            "bloomberg_markets", FeedFetchResult((), None, None), self.rules, NOW, "eos"
        )
        self.database.record_source_success(
            "bloomberg_markets",
            FeedFetchResult((self._public_observation("new"),), None, None),
            self.rules,
            NOW + 1,
            "eos",
        )
        first = self.database.claim_content_fetch(NOW + 1, lease_seconds=20)
        self.assertIsNotNone(first)
        assert first is not None
        self.assertIsNone(self.database.claim_content_fetch(NOW + 20, lease_seconds=20))
        second = self.database.claim_content_fetch(NOW + 21, lease_seconds=20)
        self.assertIsNotNone(second)
        assert second is not None
        self.assertFalse(
            self.database.complete_content_fetch(
                first,
                ContentDocumentDraft(ContentLevel.DOCUMENT, "public_text", "stale " * 20),
                NOW + 21,
            )
        )
        state = self.database.fail_content_fetch(
            second,
            "temporary",
            NOW + 21,
            NOW + 30,
            retryable=True,
            failure_kind="timeout",
        )
        self.assertEqual("retry", state)
        third = self.database.claim_content_fetch(NOW + 30)
        assert third is not None
        self.assertTrue(
            self.database.complete_content_fetch(
                third,
                ContentDocumentDraft(
                    ContentLevel.DOCUMENT,
                    "public_text",
                    "Official document body. " * 8,
                    canonical_url=third.request.url,
                    rights_policy="public_official_document",
                ),
                NOW + 31,
            )
        )
        alert_id = int(self.database.list_alerts()[0]["id"])
        detail = self.database.get_alert_detail(alert_id)
        assert detail is not None
        self.assertEqual("document", detail["documents"][0]["level"])
        self.assertEqual("completed", detail["content_fetch"]["status"])

    def test_nonretryable_failure_becomes_dead(self) -> None:
        self.database.record_source_success(
            "bloomberg_markets", FeedFetchResult((), None, None), self.rules, NOW, "eos"
        )
        self.database.record_source_success(
            "bloomberg_markets",
            FeedFetchResult((self._public_observation("dead"),), None, None),
            self.rules,
            NOW + 1,
            "eos",
        )
        item = self.database.claim_content_fetch(NOW + 1)
        assert item is not None
        self.assertEqual(
            "dead",
            self.database.fail_content_fetch(
                item,
                "unsupported",
                NOW + 2,
                NOW + 30,
                retryable=False,
                failure_kind="unsupported_type",
            ),
        )

    def test_schema_thirteen_migrates_content_tables(self) -> None:
        copied = Path(self.temporary.name) / "schema13.db"
        source = sqlite3.connect(self.database.path)
        target = sqlite3.connect(copied)
        source.backup(target)
        target.execute("DROP TABLE content_fetch_jobs")
        target.execute("DROP TABLE content_documents")
        target.execute("PRAGMA user_version=13")
        target.commit()
        source.close()
        target.close()
        migrated = Database(copied)
        try:
            self.assertEqual(14, migrated.status()["database_schema"])
            tables = {
                row[0]
                for row in migrated.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertIn("content_documents", tables)
            self.assertIn("content_fetch_jobs", tables)
        finally:
            migrated.close()


if __name__ == "__main__":
    unittest.main()
