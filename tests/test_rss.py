from __future__ import annotations

import io
import gzip
import unittest
import urllib.error
from dataclasses import replace
from datetime import UTC, datetime
from email.message import Message
from unittest.mock import Mock, patch

from argus.config import load_config
from argus.content import ContentLevel
from argus.models import SourceState
from argus.rss import FeedError, RssCollector, parse_feed

from helpers import PROJECT_ROOT


class RssTests(unittest.TestCase):
    def setUp(self) -> None:
        config = load_config(PROJECT_ROOT / "config" / "argus.production.toml")
        self.source = config.sources[0]
        self.payload = (PROJECT_ROOT / "tests" / "fixtures" / "bloomberg.rss").read_bytes()

    def test_parses_and_orders_bloomberg_items(self) -> None:
        observations = parse_feed(self.payload, self.source)
        self.assertEqual(["routine-guid", "breaking-guid"], [item.external_id for item in observations])
        self.assertEqual("Routine market coverage with daily analysis.", observations[0].summary)
        self.assertEqual("Markets", observations[0].attributes["section"])
        self.assertEqual(ContentLevel.EXCERPT, observations[0].content_documents[0].level)
        self.assertIsNone(observations[0].content_fetch)

    def test_commercial_feed_never_schedules_article_scraping(self) -> None:
        payload = b"""<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>
        <item><title>Paid article</title><link>https://www.bloomberg.com/news/articles/paid</link>
        <description>Allowed excerpt</description><content:encoded><![CDATA[<p>Long body</p>]]></content:encoded>
        </item></channel></rss>"""
        item = parse_feed(payload, self.source)[0]
        self.assertEqual([ContentLevel.EXCERPT], [doc.level for doc in item.content_documents])
        self.assertIsNone(item.content_fetch)

    def test_authorized_feed_full_text_and_public_document_policy(self) -> None:
        payload = b"""<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>
        <item><title>Official release</title><link>https://www.bloomberg.com/news/articles/release</link>
        <description>Short excerpt</description><content:encoded><![CDATA[<article><p>Full authorized body</p></article>]]></content:encoded>
        </item></channel></rss>"""
        authorized = replace(
            self.source, settings={**self.source.settings, "content_policy": "feed_full_text_allowed"}
        )
        item = parse_feed(payload, authorized)[0]
        self.assertEqual(
            [ContentLevel.EXCERPT, ContentLevel.FULL_TEXT],
            [doc.level for doc in item.content_documents],
        )
        self.assertIsNone(item.content_fetch)

        public = replace(
            self.source, settings={**self.source.settings, "content_policy": "public_document_full_text"}
        )
        without_full = payload.replace(
            b"<content:encoded><![CDATA[<article><p>Full authorized body</p></article>]]></content:encoded>",
            b"",
        )
        item = parse_feed(without_full, public)[0]
        self.assertIsNotNone(item.content_fetch)
        assert item.content_fetch is not None
        self.assertIn("www.bloomberg.com", item.content_fetch.allowed_hosts)

    def test_public_binary_attachment_keeps_link_without_scheduling_fetch(self) -> None:
        payload = b"""<rss><channel><item><title>Official spreadsheet</title>
        <link>https://www.bloomberg.com/reports/data.XLSX?download=1</link>
        <description>Official statistics are available in the attached workbook.</description>
        </item></channel></rss>"""
        public = replace(
            self.source,
            settings={**self.source.settings, "content_policy": "public_document_full_text"},
        )
        item = parse_feed(payload, public)[0]
        self.assertEqual("https://www.bloomberg.com/reports/data.XLSX?download=1", item.url)
        self.assertEqual([ContentLevel.EXCERPT], [doc.level for doc in item.content_documents])
        self.assertIsNone(item.content_fetch)

    def test_atom_content_obeys_full_text_policy(self) -> None:
        payload = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>x</id>
        <title>Official release</title><link href="https://www.bloomberg.com/news/articles/atom"/>
        <summary>Short excerpt</summary><content type="html">&lt;p&gt;Full atom body&lt;/p&gt;</content>
        </entry></feed>"""
        source = replace(
            self.source, settings={**self.source.settings, "content_policy": "feed_full_text_allowed"}
        )
        item = parse_feed(payload, source)[0]
        self.assertEqual("Full atom body", item.content_documents[-1].body)

    def test_rejects_empty_feed(self) -> None:
        with self.assertRaisesRegex(FeedError, "no usable entries"):
            parse_feed(b"<rss><channel></channel></rss>", self.source)

    def test_rejects_invalid_xml(self) -> None:
        with self.assertRaisesRegex(FeedError, "invalid XML"):
            parse_feed(b"<rss>", self.source)

    def test_rejects_doctype_and_entities_in_all_encodings(self) -> None:
        document = '<!DOCTYPE rss [<!ENTITY x "expanded">]><rss><channel><item><title>&x;</title></item></channel></rss>'
        for encoding in ("utf-8", "utf-16"):
            with self.subTest(encoding=encoding), self.assertRaisesRegex(FeedError, "document types"):
                parse_feed(document.encode(encoding), self.source)

    def test_parser_enforces_size_limit_for_direct_callers(self) -> None:
        with self.assertRaisesRegex(FeedError, "response limit"):
            parse_feed(self.payload, replace(self.source, max_response_bytes=10))

    def test_article_links_cannot_embed_credentials_or_invalid_ports(self) -> None:
        from argus.rss import _safe_link
        for value in ("https://secret@www.bloomberg.com/news", "https://www.bloomberg.com:bad/news",
                      "https://[broken/news", "https://www.bloomberg.com/\nnews"):
            self.assertEqual("", _safe_link(value, self.source.allowed_hosts))

    def test_drops_article_link_outside_allowlist(self) -> None:
        payload = self.payload.replace(
            b"https://www.bloomberg.com/news/articles/routine",
            b"https://attacker.example/news/articles/routine",
        )
        observations = parse_feed(payload, self.source)
        self.assertEqual("", observations[0].url)

    def test_upgrades_legacy_http_article_link_on_allowlisted_host(self) -> None:
        from argus.rss import _safe_link
        self.assertEqual(
            "https://www.bloomberg.com/news",
            _safe_link("http://www.bloomberg.com/news", self.source.allowed_hosts),
        )

    def _state(self, etag: str | None = None, modified: str | None = None) -> SourceState:
        return SourceState(
            source_id=self.source.id,
            initialized=True,
            etag=etag,
            last_modified=modified,
            last_attempt_at=None,
            last_success_at=None,
            consecutive_failures=0,
            outage_alerted=False,
            outage_started_at=None,
        )

    def test_collector_sends_conditional_headers(self) -> None:
        headers = Message()
        headers["Content-Type"] = "text/xml; charset=utf-8"
        headers["ETag"] = 'W/"new"'

        class Response(io.BytesIO):
            status = 200

            def __init__(self, payload: bytes) -> None:
                super().__init__(payload)
                self.headers = headers

            def geturl(self) -> str:
                return "https://www.bloomberg.com/feeds/markets/news.rss"

        response = Response(self.payload)
        opener = Mock()
        opener.open.return_value = response
        resolver = lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))]
        result = RssCollector(self.source, opener=opener, resolver=resolver).fetch(
            self._state('W/"old"', "yesterday")
        )
        request = opener.open.call_args.args[0]
        self.assertEqual('W/"old"', request.get_header("If-none-match"))
        self.assertEqual("yesterday", request.get_header("If-modified-since"))
        self.assertEqual('W/"new"', result.etag)

    def test_collector_handles_not_modified(self) -> None:
        headers = Message()
        headers["ETag"] = 'W/"same"'
        error = urllib.error.HTTPError(self.source.url, 304, "Not Modified", headers, None)
        opener = Mock()
        opener.open.side_effect = error
        resolver = lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))]
        result = RssCollector(self.source, opener=opener, resolver=resolver).fetch(
            self._state('W/"same"')
        )
        self.assertTrue(result.not_modified)
        self.assertEqual((), result.observations)

    def test_collector_rejects_oversized_response(self) -> None:
        source = replace(self.source, max_response_bytes=1024)
        headers = Message()
        headers["Content-Type"] = "text/xml"

        class Response(io.BytesIO):
            status = 200

            def __init__(self) -> None:
                super().__init__(b"x" * 2048)
                self.headers = headers

            def geturl(self) -> str:
                return "https://www.bloomberg.com/feeds/markets/news.rss"

        opener = Mock()
        opener.open.return_value = Response()
        resolver = lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))]
        with self.assertRaisesRegex(FeedError, "size limit"):
            RssCollector(source, opener=opener, resolver=resolver).fetch(self._state())

    def test_collector_decodes_gzip_with_a_decompressed_size_limit(self) -> None:
        headers = Message()
        headers["Content-Type"] = "application/rss+xml"
        headers["Content-Encoding"] = "gzip"

        class Response(io.BytesIO):
            def __init__(self, payload: bytes) -> None:
                super().__init__(gzip.compress(payload))
                self.headers = headers

            def geturl(self) -> str:
                return "https://www.bloomberg.com/feeds/markets/news.rss"

        opener = Mock()
        opener.open.return_value = Response(self.payload)
        resolver = lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))]
        result = RssCollector(self.source, opener=opener, resolver=resolver).fetch(self._state())
        self.assertEqual(2, len(result.observations))
        source = replace(self.source, max_response_bytes=1024)
        opener.open.return_value = Response(b"<rss>" + b" " * 2000 + b"</rss>")
        with self.assertRaisesRegex(FeedError, "decoded feed response"):
            RssCollector(source, opener=opener, resolver=resolver).fetch(self._state())

    def test_fallback_id_is_stable_when_date_is_missing(self) -> None:
        payload = b"""<rss><channel><item><title>Same title</title><description>x</description></item></channel></rss>"""
        first = parse_feed(payload, self.source)[0]
        second = parse_feed(payload, self.source)[0]
        self.assertEqual(first.external_id, second.external_id)

    def test_collector_rejects_private_resolution_before_request(self) -> None:
        opener = Mock()
        resolver = lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))]
        with self.assertRaisesRegex(FeedError, "non-public"):
            RssCollector(self.source, opener=opener, resolver=resolver).fetch(self._state())
        opener.open.assert_not_called()

    def test_redirect_is_validated_before_following(self) -> None:
        headers = Message()
        headers["Location"] = "https://127.0.0.1/private"
        redirect = urllib.error.HTTPError(self.source.url, 302, "Found", headers, None)
        opener = Mock()
        opener.open.side_effect = redirect
        resolver = lambda host, *args, **kwargs: [
            (2, 1, 6, "", ("93.184.216.34", 443))
        ]
        with self.assertRaisesRegex(FeedError, "allowlist"):
            RssCollector(self.source, opener=opener, resolver=resolver).fetch(self._state())
        self.assertEqual(1, opener.open.call_count)

    def _content_age_fetch(self, payload, max_age, opener=None):
        headers = Message()
        headers["Content-Type"] = "text/xml"
        source = replace(self.source, settings={"max_content_age_seconds": max_age})

        class Response(io.BytesIO):
            def __init__(self):
                super().__init__(payload)
                self.headers = headers

            def geturl(self):
                return source.url

        opener = opener or Mock()
        opener.open.return_value = Response()
        resolver = lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))]
        with patch("argus.rss.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 9, 5, 15, tzinfo=UTC)
            result = RssCollector(source, opener=opener, resolver=resolver).fetch(
                self._state('W/"cached"', "yesterday")
            )
        return result, opener

    def test_content_age_policy_rejects_stopped_feed_despite_http_success(self) -> None:
        with self.assertRaisesRegex(FeedError, "content is stale"):
            self._content_age_fetch(self.payload, 86400)
        result, _ = self._content_age_fetch(self.payload, 0)
        self.assertEqual(2, len(result.observations))

    def test_content_age_policy_uses_newest_entry_and_bypasses_conditional_cache(self) -> None:
        result, opener = self._content_age_fetch(self.payload, 3 * 86400)
        self.assertEqual(2, len(result.observations))
        request = opener.open.call_args.args[0]
        self.assertIsNone(request.get_header("If-none-match"))
        self.assertIsNone(request.get_header("If-modified-since"))

    def test_content_age_policy_cannot_accept_unsolicited_304(self) -> None:
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError(self.source.url, 304, "Not Modified", Message(), None)
        with self.assertRaisesRegex(FeedError, "freshness cannot be verified from HTTP 304"):
            self._content_age_fetch(self.payload, 86400, opener)

    def test_undated_entries_cannot_mask_archived_feed(self) -> None:
        undated = b"<item><title>Undated item</title></item>"
        stale_with_undated = self.payload.replace(b"</channel>", undated + b"</channel>")
        with self.assertRaisesRegex(FeedError, "content is stale"):
            self._content_age_fetch(stale_with_undated, 86400)
        for date in (b"", b"<pubDate>invalid</pubDate>"):
            payload = b"<rss><channel><item><title>Test</title>" + date + b"</item></channel></rss>"
            with self.subTest(date=date), self.assertRaisesRegex(FeedError, "without publication timestamps"):
                self._content_age_fetch(payload, 86400)

    def test_atom_updated_timestamp_is_used_for_content_age(self) -> None:
        payload = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>x</id><title>Test</title><updated>2026-09-05T14:00:00Z</updated></entry></feed>'
        result, _ = self._content_age_fetch(payload, 3600)
        self.assertFalse(result.observations[0].attributes["published_at_inferred"])
        with self.assertRaisesRegex(FeedError, "content is stale"):
            self._content_age_fetch(payload, 3599)


if __name__ == "__main__":
    unittest.main()
