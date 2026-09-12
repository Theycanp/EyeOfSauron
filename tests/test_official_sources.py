from __future__ import annotations

import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime

from argus.config import parse_source_config, _parse_rule
from argus.content import ContentLevel
from argus.models import Observation
from argus.news_catalog import NEWS_SOURCE_CATALOG
from argus.news_rollout import plan_official_news
from argus.official_list import OfficialListCollector
from argus.rss import FeedError, parse_feed
from argus.rules import RuleSet


class OfficialSourcesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = NEWS_SOURCE_CATALOG.source_template(
            "china_mof", "announcements", "cn_mof", enabled=True, user_confirmed=True,
        )
        self.source = parse_source_config(self.raw)

    def test_index_only_collects_dated_allowed_articles_and_deduplicates(self) -> None:
        payload = '''<html><script>var x="<a href='./202609/t20260911_9.htm'>Script false news</a>";</script>
        <a href="./202609/t20260911_1.htm" title="财政政策重要公告">截断...</a>
        <a href="./202609/t20260911_1.htm">手机端重复的标题</a>
        <a href="http://jrs.mof.gov.cn/zhengcefabu/202609/t20260910_2.htm">中央金融机构公告</a>
        <a href="https://evil.example/202609/t20260911_3.htm">外部网站恶意内容</a>
        <a href="https://www.mof.gov.cn/other/202609/t20260911_4.htm">不属于指定目录</a>
        <a href="./index.html">新闻导航没有日期</a>
        <a href="./202609/t20260999_5.htm">非法日期不会被收录</a></html>'''.encode()
        items = OfficialListCollector(self.source).parse_payload(payload)
        self.assertEqual(2, len(items))
        self.assertEqual("财政政策重要公告", items[-1].title)
        self.assertEqual(datetime(2026, 9, 10, 16, tzinfo=UTC), items[-1].published_at)
        self.assertTrue(items[0].url.startswith("https://jrs.mof.gov.cn/"))
        self.assertIsNotNone(items[-1].content_fetch)
        self.assertFalse(items[-1].attributes["published_at_inferred"])

    def test_layout_change_and_oversized_response_are_errors(self) -> None:
        collector = OfficialListCollector(self.source)
        for body in (b"<html>Access denied</html>", b"<a href='/'>Navigation</a>", b"\xff"):
            with self.subTest(body=body), self.assertRaises(FeedError):
                collector.parse_payload(body)
        with self.assertRaises(FeedError):
            collector.parse_payload(b"x" * (self.source.max_response_bytes + 1))

    def test_japan_index_uses_explicit_time_without_putting_date_in_title(self) -> None:
        source = parse_source_config(NEWS_SOURCE_CATALOG.source_template(
            "japan_cabinet", "announcements", "jp_cabinet", enabled=True, user_confirmed=True,
        ))
        items = OfficialListCollector(source).parse_payload(b'''<a href="/105/actions/202609/10meeting.html">
            <div>Emergency cabinet meeting</div><time datetime="2026-09-10">September 10</time></a>''')
        self.assertEqual("Emergency cabinet meeting", items[0].title)
        self.assertEqual(datetime(2026, 9, 9, 15, tzinfo=UTC), items[0].published_at)

    def test_rdf_dates_full_text_and_stable_ids_obey_existing_content_policy(self) -> None:
        source = replace(self.source, allowed_hosts=("www.mhlw.go.jp",))
        body = b'''<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
            xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/"
            xmlns:content="http://purl.org/rss/1.0/modules/content/">
            <channel><title>Ministry</title></channel>
            <item rdf:about="https://www.mhlw.go.jp/stf/newpage_123.html">
            <title>Public health announcement</title><link>https://www.mhlw.go.jp/stf/newpage_123.html</link>
            <dc:date>2026-09-11T16:56:00+09:00</dc:date><description>Summary</description>
            <content:encoded>&lt;p&gt;Official public text&lt;/p&gt;</content:encoded></item></rdf:RDF>'''
        item = parse_feed(body, source)[0]
        self.assertEqual(datetime(2026, 9, 11, 7, 56, tzinfo=UTC), item.published_at)
        self.assertEqual(item.url, item.external_id)
        self.assertEqual(ContentLevel.FULL_TEXT, item.content_documents[-1].level)
        self.assertIsNone(item.content_fetch)
        metadata = replace(source, settings={"content_policy": "feed_metadata_and_original_link_only"})
        self.assertEqual([ContentLevel.EXCERPT], [doc.level for doc in parse_feed(body, metadata)[0].content_documents])

    def test_official_prefixes_and_timezone_are_validated(self) -> None:
        for settings in (
            {"article_url_prefixes": []},
            {"article_url_prefixes": ["https://evil.example/"]},
            {"article_url_prefixes": ["https://www.mof.gov.cn"]},
            {**self.raw["settings"], "timezone": "Invalid/Zone"},
        ):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                parse_source_config({**self.raw, "settings": settings})

    def test_rollout_preserves_policy_is_idempotent_and_rules_cover_every_addition(self) -> None:
        current = {"sources": [], "rules": [], "analysis": {"enabled": False},
                   "digest": {"enabled": True, "timezone": "Asia/Shanghai"}}
        original = deepcopy(current)
        planned, additions = plan_official_news(current)
        self.assertEqual(current, original)
        self.assertEqual(current["digest"], planned["digest"])
        self.assertEqual(current["analysis"], planned["analysis"])
        for raw in additions:
            source = parse_source_config(raw)
            self.assertTrue(source.enabled)
            if source.region == "JP":
                self.assertEqual(4, source.default_importance)
        self.assertEqual({row["id"] for row in additions}, set(planned["rules"][0]["source_ids"]))
        _parse_rule(planned["rules"][0], 0)
        second, added = plan_official_news(planned)
        self.assertEqual([], added)
        self.assertEqual(planned, second)
        planned["sources"][0]["enabled"] = False
        self.assertFalse(plan_official_news(planned)[0]["sources"][0]["enabled"])

    def test_ordinary_fiscal_news_stays_for_digest_critical_news_alerts(self) -> None:
        planned, _ = plan_official_news({})
        rules = RuleSet.from_config((_parse_rule(planned["rules"][0], 0),), "test")
        now = datetime.now(UTC)
        observation = Observation("china_mof_announcements", "财政部", "china_mof", "id", now,
                                  "财政部将向金融机构注资3600亿元", "", "https://www.mof.gov.cn/")
        self.assertEqual((), rules.evaluate(observation, int(now.timestamp())))
        for title in ("启动一级应急响应", "大津波警報を発表", "M 7.8 - Major earthquake"):
            self.assertTrue(rules.evaluate(replace(observation, title=title), int(now.timestamp())), title)
        self.assertFalse(rules.evaluate(replace(observation, title="一级应急响应演练"), int(now.timestamp())))
