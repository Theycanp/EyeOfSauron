"""Offline replay of short, attributed public source snapshots."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from argus.config import parse_source_config
from argus.news_catalog import NEWS_SOURCE_CATALOG
from argus.official_list import OfficialListCollector
from argus.rss import FeedError, parse_feed

from helpers import PROJECT_ROOT


FIXTURES = PROJECT_ROOT / "tests" / "fixtures"


def source(entry: str, feed: str):
    return parse_source_config(NEWS_SOURCE_CATALOG.source_template(
        entry, feed, f"replay_{entry}", enabled=True, user_confirmed=True,
    ))


class SourceReplayTests(unittest.TestCase):
    def test_mhlw_rdf_snapshot(self) -> None:
        payload = (FIXTURES / "mhlw-news-snapshot.rdf").read_bytes()
        items = parse_feed(payload, source("japan_mhlw", "news"))

        self.assertEqual(2, len(items))
        self.assertEqual(
            [
                "https://www.mhlw.go.jp/toukei/saikin/hw/jinkou/geppo/s2026/07.html",
                "https://www.mhlw.go.jp/stf/newpage_76380.html",
            ],
            [item.external_id for item in items],
        )
        self.assertEqual(datetime(2026, 9, 25, 6, tzinfo=UTC), items[0].published_at)
        self.assertEqual("厚生労働省", items[1].attributes["creator"])
        self.assertFalse(items[1].attributes["published_at_inferred"])
        self.assertEqual(items[1].external_id, items[1].url)
        self.assertEqual(items, parse_feed(payload, source("japan_mhlw", "news")))

    def test_mof_html_snapshot(self) -> None:
        payload = (FIXTURES / "china-mof-index-snapshot.html").read_bytes()
        items = OfficialListCollector(source("china_mof", "announcements")).parse_payload(payload)

        self.assertEqual(1, len(items))
        self.assertEqual("蓝佛安会见香港特别行政区政府财政司司长陈茂波", items[0].title)
        self.assertEqual(
            "https://www.mof.gov.cn/zhengwuxinxi/caizhengxinwen/202609/t20260923_3998012.htm",
            items[0].url,
        )
        self.assertEqual(datetime(2026, 9, 22, 16, tzinfo=UTC), items[0].published_at)
        self.assertFalse(items[0].attributes["published_at_inferred"])

    def test_mof_layout_drift_and_bad_encoding_fail(self) -> None:
        collector = OfficialListCollector(source("china_mof", "announcements"))
        payload = (FIXTURES / "china-mof-index-snapshot.html").read_bytes()
        for invalid in (
            b"<html><body><a href='./index.html'>Navigation only</a></body></html>",
            payload.replace(b"t20260923_3998012", b"t20261399_3998012"),
        ):
            with self.subTest(invalid=invalid[:40]), self.assertRaisesRegex(FeedError, "no dated article links"):
                collector.parse_payload(invalid)
        with self.assertRaisesRegex(FeedError, "must use UTF-8"):
            collector.parse_payload(payload + b"\xff")

    def test_mhlw_structural_xml_drift_fails(self) -> None:
        payload = (FIXTURES / "mhlw-news-snapshot.rdf").read_bytes()
        with self.assertRaisesRegex(FeedError, "invalid XML"):
            parse_feed(payload.replace(b"</rdf:RDF>", b""), source("japan_mhlw", "news"))


if __name__ == "__main__":
    unittest.main()
