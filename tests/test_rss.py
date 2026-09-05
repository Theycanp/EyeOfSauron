from __future__ import annotations

import io
import unittest
import urllib.error
from dataclasses import replace
from email.message import Message
from unittest.mock import Mock

from argus.config import load_config
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


if __name__ == "__main__":
    unittest.main()
