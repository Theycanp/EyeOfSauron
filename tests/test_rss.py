from __future__ import annotations

import io
import unittest
import urllib.error
from dataclasses import replace
from email.message import Message
from unittest.mock import patch

from signalwatch.config import load_config
from signalwatch.models import SourceState
from signalwatch.rss import FeedError, RssCollector, parse_feed

from helpers import PROJECT_ROOT


class RssTests(unittest.TestCase):
    def setUp(self) -> None:
        config = load_config(PROJECT_ROOT / "config" / "signalwatch.production.toml")
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
        with patch("urllib.request.urlopen", return_value=response) as mocked:
            result = RssCollector(self.source).fetch(self._state('W/"old"', "yesterday"))
        request = mocked.call_args.args[0]
        self.assertEqual('W/"old"', request.get_header("If-none-match"))
        self.assertEqual("yesterday", request.get_header("If-modified-since"))
        self.assertEqual('W/"new"', result.etag)

    def test_collector_handles_not_modified(self) -> None:
        headers = Message()
        headers["ETag"] = 'W/"same"'
        error = urllib.error.HTTPError(self.source.url, 304, "Not Modified", headers, None)
        with patch("urllib.request.urlopen", side_effect=error):
            result = RssCollector(self.source).fetch(self._state('W/"same"'))
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

        with patch("urllib.request.urlopen", return_value=Response()):
            with self.assertRaisesRegex(FeedError, "size limit"):
                RssCollector(source).fetch(self._state())


if __name__ == "__main__":
    unittest.main()
