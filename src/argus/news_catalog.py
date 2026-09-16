from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from .content import ContentPolicy
from .regions import normalize_region


_CATALOG_ID = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_VERIFIED_ON = "2026-09-05"


class NewsCatalogError(ValueError):
    pass


class IntegrationMode(StrEnum):
    VERIFIED_RSS = "verified_rss"
    VERIFIED_OFFICIAL_LIST = "verified_official_list"
    USER_CONFIRMED_OFFICIAL_URL = "user_confirmed_official_url"
    LICENSED_PROVIDER = "licensed_provider"


class AccessModel(StrEnum):
    PUBLIC = "public"
    MIXED = "mixed"
    SUBSCRIPTION = "subscription"
    LICENSED = "licensed"


@dataclass(frozen=True, slots=True)
class NewsFeedTemplate:
    id: str
    label: str
    section: str
    url: str
    allowed_hosts: tuple[str, ...]
    max_content_age_seconds: int = 0
    article_url_prefixes: tuple[str, ...] = ()
    index_format: str = "html"

    def __post_init__(self) -> None:
        if not _CATALOG_ID.fullmatch(self.id):
            raise NewsCatalogError(f"invalid feed template id: {self.id}")
        _validate_official_url(self.url, self.allowed_hosts)

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "section": self.section,
            "url": self.url,
            "allowed_hosts": list(self.allowed_hosts),
            "max_content_age_seconds": self.max_content_age_seconds,
            "article_url_prefixes": list(self.article_url_prefixes),
            "index_format": self.index_format,
        }


@dataclass(frozen=True, slots=True)
class NewsSourceEntry:
    id: str
    publisher: str
    homepage_url: str
    access_model: AccessModel
    integration_mode: IntegrationMode
    feeds: tuple[NewsFeedTemplate, ...]
    evidence_url: str
    notes: str
    verified_on: str = _VERIFIED_ON
    default_enabled: bool = False
    requires_user_confirmation: bool = True
    content_policy: str = "feed_metadata_and_original_link_only"
    region: str = "GLOBAL"
    source_tier: str = "secondary"
    default_importance: int = 3
    topic: str = "general"

    @property
    def source_kind(self) -> str:
        return "official_list" if self.integration_mode is IntegrationMode.VERIFIED_OFFICIAL_LIST else "rss"

    def __post_init__(self) -> None:
        if not _CATALOG_ID.fullmatch(self.id):
            raise NewsCatalogError(f"invalid catalog entry id: {self.id}")
        _validate_https_url(self.homepage_url)
        _validate_https_url(self.evidence_url)
        if self.default_enabled:
            raise NewsCatalogError("catalog entries must remain disabled by default")
        if not self.requires_user_confirmation:
            raise NewsCatalogError("catalog entries must require explicit user confirmation")
        if self.integration_mode in {IntegrationMode.VERIFIED_RSS, IntegrationMode.VERIFIED_OFFICIAL_LIST} and not self.feeds:
            raise NewsCatalogError(f"verified RSS entry {self.id} has no feed templates")
        try:
            normalize_region(self.region)
        except ValueError as exc:
            raise NewsCatalogError(f"invalid catalog region: {self.region}") from exc
        if self.source_tier not in {"primary", "secondary", "social"}:
            raise NewsCatalogError(f"invalid catalog source tier: {self.source_tier}")
        if not 1 <= self.default_importance <= 5:
            raise NewsCatalogError("catalog default importance must be between 1 and 5")
        try:
            ContentPolicy(self.content_policy)
        except ValueError as exc:
            raise NewsCatalogError("catalog content policy is invalid") from exc

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "publisher": self.publisher,
            "homepage_url": self.homepage_url,
            "access_model": self.access_model.value,
            "integration_mode": self.integration_mode.value,
            "feeds": [feed.describe() for feed in self.feeds],
            "evidence_url": self.evidence_url,
            "notes": self.notes,
            "verified_on": self.verified_on,
            "default_enabled": self.default_enabled,
            "requires_user_confirmation": self.requires_user_confirmation,
            "content_policy": self.content_policy,
            "region": self.region,
            "source_tier": self.source_tier,
            "default_importance": self.default_importance,
            "topic": self.topic,
            "source_kind": self.source_kind,
        }


class NewsSourceCatalog:
    """Audited templates; it never enables a source or fetches article HTML."""

    def __init__(self, entries: tuple[NewsSourceEntry, ...]) -> None:
        by_id: dict[str, NewsSourceEntry] = {}
        for entry in entries:
            if entry.id in by_id:
                raise NewsCatalogError(f"duplicate catalog entry: {entry.id}")
            feed_ids = [feed.id for feed in entry.feeds]
            if len(feed_ids) != len(set(feed_ids)):
                raise NewsCatalogError(f"duplicate feed id in {entry.id}")
            by_id[entry.id] = entry
        self._entries = MappingProxyType(by_id)

    def get(self, entry_id: str) -> NewsSourceEntry | None:
        return self._entries.get(entry_id)

    def require(self, entry_id: str) -> NewsSourceEntry:
        entry = self.get(entry_id)
        if entry is None:
            raise NewsCatalogError(f"unknown news catalog entry: {entry_id}")
        return entry

    def describe(self) -> tuple[dict[str, Any], ...]:
        return tuple(entry.describe() for entry in self._entries.values())

    def source_template(
        self,
        entry_id: str,
        feed_id: str,
        source_id: str,
        *,
        enabled: bool = False,
        user_confirmed: bool = False,
        poll_interval_seconds: int = 300,
    ) -> dict[str, Any]:
        entry = self.require(entry_id)
        feed = next((item for item in entry.feeds if item.id == feed_id), None)
        if feed is None:
            raise NewsCatalogError(f"unknown feed {entry_id}/{feed_id}")
        if enabled and not user_confirmed:
            raise NewsCatalogError("enabling a catalog source requires explicit user confirmation")
        if not _CATALOG_ID.fullmatch(source_id):
            raise NewsCatalogError(f"invalid source id: {source_id}")
        if not 30 <= poll_interval_seconds <= 86400:
            raise NewsCatalogError("poll interval must be between 30 and 86400 seconds")
        return {
            "id": source_id,
            "kind": entry.source_kind,
            "publisher": entry.publisher,
            "section": feed.section,
            "dedupe_scope": entry.id,
            "url": feed.url,
            "allowed_hosts": list(feed.allowed_hosts),
            "enabled": enabled,
            "poll_interval_seconds": poll_interval_seconds,
            "request_timeout_seconds": 20,
            "request_attempts": 3,
            "retry_base_seconds": 2,
            "max_response_bytes": 2097152,
            "settings": {
                "catalog_entry": entry.id,
                "catalog_feed": feed.id,
                "content_policy": entry.content_policy,
                "max_content_age_seconds": feed.max_content_age_seconds,
                "topic": entry.topic,
                **({"headline_from_summary": True}
                   if entry.id == "japan_meteorological_agency" else {}),
                **({"article_url_prefixes": list(feed.article_url_prefixes),
                    "index_format": feed.index_format,
                    "timezone": ("Asia/Shanghai" if entry.region == "CN"
                                  else "Asia/Tokyo" if entry.region == "JP" else "UTC")}
                   if entry.source_kind == "official_list" else {}),
            },
            "region": entry.region,
            "source_tier": entry.source_tier,
            "default_importance": entry.default_importance,
        }

    def user_url_template(
        self,
        entry_id: str,
        source_id: str,
        url: str,
        allowed_hosts: tuple[str, ...],
        *,
        section: str = "News",
        enabled: bool = False,
        user_confirmed: bool = False,
        poll_interval_seconds: int = 300,
    ) -> dict[str, Any]:
        entry = self.require(entry_id)
        if entry.integration_mode in {IntegrationMode.VERIFIED_RSS, IntegrationMode.VERIFIED_OFFICIAL_LIST}:
            raise NewsCatalogError("use a verified feed template for this catalog entry")
        if entry.integration_mode is IntegrationMode.LICENSED_PROVIDER:
            raise NewsCatalogError(
                "a licensed provider requires a dedicated authorized adapter, not an RSS template"
            )
        if not user_confirmed:
            raise NewsCatalogError("a user-supplied official URL requires explicit confirmation")
        _validate_official_url(url, allowed_hosts)
        if not 30 <= poll_interval_seconds <= 86400:
            raise NewsCatalogError("poll interval must be between 30 and 86400 seconds")
        if not _CATALOG_ID.fullmatch(source_id):
            raise NewsCatalogError(f"invalid source id: {source_id}")
        return {
            "id": source_id,
            "kind": "rss",
            "publisher": entry.publisher,
            "section": section.strip() or "News",
            "dedupe_scope": entry.id,
            "url": url,
            "allowed_hosts": list(allowed_hosts),
            "enabled": enabled,
            "poll_interval_seconds": poll_interval_seconds,
            "request_timeout_seconds": 20,
            "request_attempts": 3,
            "retry_base_seconds": 2,
            "max_response_bytes": 2097152,
            "settings": {
                "catalog_entry": entry.id,
                "content_policy": entry.content_policy,
                "user_confirmed_official_url": True,
            },
        }


def _validate_https_url(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise NewsCatalogError("catalog URLs must be credential-free HTTPS URLs")


def _validate_official_url(value: str, allowed_hosts: tuple[str, ...]) -> None:
    _validate_https_url(value)
    if not allowed_hosts or any(not host or host != host.lower() for host in allowed_hosts):
        raise NewsCatalogError("catalog allowlists must contain lowercase exact hosts")
    host = (urlsplit(value).hostname or "").lower()
    if host not in allowed_hosts:
        raise NewsCatalogError("catalog URL host must be present in its exact host allowlist")


def _feed(
    feed_id: str,
    label: str,
    section: str,
    url: str,
    *allowed_hosts: str,
) -> NewsFeedTemplate:
    hostname = urlsplit(url).hostname
    max_age = 0
    if hostname in {"feeds.bloomberg.com", "www.ft.com"}:
        max_age = 3 * 86400
    elif hostname == "feeds.a.dj.com":
        max_age = 7 * 86400
    elif hostname == "www.economist.com":
        max_age = 14 * 86400
    return NewsFeedTemplate(feed_id, label, section, url, tuple(allowed_hosts), max_age)


def _official_index(
    identifier: str, publisher: str, url: str, prefixes: tuple[str, ...],
    *, region: str = "CN", topic: str = "policy", index_format: str = "html",
) -> NewsSourceEntry:
    hosts = tuple(dict.fromkeys(urlsplit(value).hostname or "" for value in (url, *prefixes)))
    return NewsSourceEntry(
        identifier, publisher, url, AccessModel.PUBLIC, IntegrationMode.VERIFIED_OFFICIAL_LIST,
        (NewsFeedTemplate("announcements", "最新公告", topic, url, hosts,
                          article_url_prefixes=prefixes, index_format=index_format),),
        url, "直接监测官方列表中的带日期公告；每 15 分钟检查，普通信息收录日报，重大事件按规则通知。",
        verified_on="2026-09-12", content_policy=ContentPolicy.PUBLIC_DOCUMENT.value,
        region=region, source_tier="primary", default_importance=4, topic=topic,
    )


NEWS_SOURCE_CATALOG = NewsSourceCatalog((
    _official_index(
        "china_state_council", "中国国务院政策文件", "https://www.gov.cn/zhengce/zuixin/ZUIXINZHENGCE.json",
        ("https://www.gov.cn/zhengce/",), topic="policy", index_format="govcn_json",
    ),
    _official_index(
        "japan_mof", "日本财务省", "https://www.mof.go.jp/english/",
        ("https://www.mof.go.jp/english/policy/", "https://www.mof.go.jp/english/international_policy/"),
        region="JP", topic="economy",
    ),
    _official_index(
        "china_mof", "中国财政部", "https://www.mof.gov.cn/zhengwuxinxi/caizhengxinwen/",
        ("https://www.mof.gov.cn/zhengwuxinxi/caizhengxinwen/",
         "https://jrs.mof.gov.cn/zhengcefabu/", "https://jdjc.mof.gov.cn/jianchagonggao/"),
        topic="finance",
    ),
    _official_index(
        "china_ndrc", "国家发展改革委", "https://www.ndrc.gov.cn/xwdt/xwfb/",
        ("https://www.ndrc.gov.cn/xwdt/xwfb/",),
    ),
    _official_index(
        "china_stats", "国家统计局", "https://www.stats.gov.cn/sj/zxfb/",
        ("https://www.stats.gov.cn/sj/zxfb/",), topic="economy",
    ),
    _official_index(
        "china_mfa", "中国外交部", "https://www.mfa.gov.cn/wjdt_674879/fyrbt_674889/",
        ("https://www.mfa.gov.cn/wjdt_674879/fyrbt_674889/",), topic="diplomacy",
    ),
    _official_index(
        "china_most", "中国科学技术部", "https://www.most.gov.cn/kjbgz/",
        ("https://www.most.gov.cn/kjbgz/",), topic="science",
    ),
    _official_index(
        "japan_cabinet", "日本首相官邸", "https://japan.kantei.go.jp/",
        ("https://japan.kantei.go.jp/",), region="JP", topic="diplomacy",
    ),
    NewsSourceEntry(
        "japan_mhlw", "日本厚生劳动省", "https://www.mhlw.go.jp/",
        AccessModel.PUBLIC, IntegrationMode.VERIFIED_RSS,
        (_feed("news", "新着情報", "健康・劳动・社会保障",
               "https://www.mhlw.go.jp/stf/news.rdf", "www.mhlw.go.jp"),),
        "https://www.mhlw.go.jp/stf/news.rdf", "官方 RSS 1.0：公共卫生、劳动和社会保障公告。",
        verified_on="2026-09-12", content_policy=ContentPolicy.PUBLIC_DOCUMENT.value,
        region="JP", source_tier="primary", default_importance=4, topic="health",
    ),
    NewsSourceEntry(
        "jaxa", "日本宇宙航空研究开发机构 JAXA", "https://global.jaxa.jp/",
        AccessModel.PUBLIC, IntegrationMode.VERIFIED_RSS,
        (_feed("press", "Press releases", "航天与科学", "https://global.jaxa.jp/rss/press.rdf", "global.jaxa.jp"),),
        "https://global.jaxa.jp/", "官方 RSS 1.0：航天任务、科学研究与发射公告。",
        verified_on="2026-09-12", content_policy=ContentPolicy.PUBLIC_DOCUMENT.value,
        region="JP", source_tier="primary", default_importance=4, topic="science",
    ),
    NewsSourceEntry(
        "un_news", "联合国新闻", "https://news.un.org/zh/",
        AccessModel.PUBLIC, IntegrationMode.VERIFIED_RSS,
        (_feed("chinese", "中文新闻", "国际与人道事务", "https://news.un.org/feed/subscribe/zh/news/all/rss.xml", "news.un.org"),),
        "https://news.un.org/zh/", "联合国官方中文新闻，覆盖国际安全、人道、发展和公共卫生。",
        verified_on="2026-09-12", content_policy=ContentPolicy.PUBLIC_DOCUMENT.value,
        region="GLOBAL", source_tier="primary", default_importance=4, topic="world",
    ),
    NewsSourceEntry(
        "bloomberg",
        "Bloomberg",
        "https://www.bloomberg.com/",
        AccessModel.MIXED,
        IntegrationMode.VERIFIED_RSS,
        (
            _feed("markets", "Markets", "Markets", "https://feeds.bloomberg.com/markets/news.rss", "feeds.bloomberg.com", "www.bloomberg.com"),
            _feed("politics", "Politics", "Politics", "https://feeds.bloomberg.com/politics/news.rss", "feeds.bloomberg.com", "www.bloomberg.com"),
            _feed("technology", "Technology", "Technology", "https://feeds.bloomberg.com/technology/news.rss", "feeds.bloomberg.com", "www.bloomberg.com"),
            _feed("economics", "Economics", "Economics", "https://feeds.bloomberg.com/economics/news.rss", "feeds.bloomberg.com", "www.bloomberg.com"),
        ),
        "https://feeds.bloomberg.com/markets/news.rss",
        "Official feed metadata only; individual articles may require a Bloomberg subscription.",
        region="US", source_tier="secondary", default_importance=3, topic="markets",
    ),
    NewsSourceEntry(
        "wall_street_journal",
        "The Wall Street Journal",
        "https://www.wsj.com/",
        AccessModel.SUBSCRIPTION,
        IntegrationMode.VERIFIED_RSS,
        (
            _feed("world", "World News", "World", "https://feeds.a.dj.com/rss/RSSWorldNews.xml", "feeds.a.dj.com", "www.wsj.com"),
            _feed("markets", "Markets", "Markets", "https://feeds.a.dj.com/rss/RSSMarketsMain.xml", "feeds.a.dj.com", "www.wsj.com"),
            _feed("business", "US Business", "Business", "https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml", "feeds.a.dj.com", "www.wsj.com"),
        ),
        "https://feeds.a.dj.com/rss/RSSWorldNews.xml",
        "2026-09-05 check: public feeds respond but their newest items are dated 2025-01-27. Not usable for current alerts; keep disabled until a current authorized feed is supplied.",
    ),
    NewsSourceEntry(
        "economist",
        "The Economist",
        "https://www.economist.com/",
        AccessModel.SUBSCRIPTION,
        IntegrationMode.VERIFIED_RSS,
        (
            _feed("world_this_week", "The world this week", "World", "https://www.economist.com/the-world-this-week/rss.xml", "www.economist.com"),
            _feed("finance_economics", "Finance & economics", "Finance and economics", "https://www.economist.com/finance-and-economics/rss.xml", "www.economist.com"),
            _feed("business", "Business", "Business", "https://www.economist.com/business/rss.xml", "www.economist.com"),
            _feed("science_technology", "Science & technology", "Science and technology", "https://www.economist.com/science-and-technology/rss.xml", "www.economist.com"),
        ),
        "https://www.economist.com/the-world-this-week/rss.xml",
        "Official section feed endpoints may enforce subscription or anti-automation policy; test before enabling.",
    ),
    NewsSourceEntry(
        "financial_times",
        "Financial Times",
        "https://www.ft.com/",
        AccessModel.SUBSCRIPTION,
        IntegrationMode.VERIFIED_RSS,
        (
            _feed("world", "World", "World", "https://www.ft.com/world?format=rss", "www.ft.com"),
            _feed("global_economy", "Global Economy", "Global Economy", "https://www.ft.com/global-economy?format=rss", "www.ft.com"),
        ),
        "https://www.ft.com/world?format=rss",
        "Official FT RSS-format endpoints; full articles remain subject to FT access controls.",
    ),
    NewsSourceEntry(
        "reuters",
        "Reuters",
        "https://www.reuters.com/",
        AccessModel.LICENSED,
        IntegrationMode.LICENSED_PROVIDER,
        (),
        "https://reutersagency.com/content-delivery-platforms/reuters-connect/",
        "No generic public Reuters feed URL is pinned here. Use a licensed Reuters product or a user-confirmed official URL.",
    ),
    NewsSourceEntry(
        "associated_press",
        "Associated Press",
        "https://apnews.com/",
        AccessModel.LICENSED,
        IntegrationMode.LICENSED_PROVIDER,
        (),
        "https://developer.ap.org/ap-media-api/",
        "No generic public AP feed URL is pinned here. Use AP Media API or a user-confirmed official URL under its terms.",
    ),
    NewsSourceEntry(
        "federal_reserve",
        "Federal Reserve Board",
        "https://www.federalreserve.gov/",
        AccessModel.PUBLIC,
        IntegrationMode.VERIFIED_RSS,
        (
            _feed("press_all", "All press releases", "Press Releases", "https://www.federalreserve.gov/feeds/press_all.xml", "www.federalreserve.gov"),
            _feed("monetary_policy", "Monetary policy", "Monetary Policy", "https://www.federalreserve.gov/feeds/press_monetary.xml", "www.federalreserve.gov"),
            _feed("speeches", "Speeches and testimony", "Speeches", "https://www.federalreserve.gov/feeds/speeches.xml", "www.federalreserve.gov"),
        ),
        "https://www.federalreserve.gov/feeds/feeds.htm",
        "Official Federal Reserve RSS feeds for public releases.",
        content_policy=ContentPolicy.PUBLIC_DOCUMENT.value,
        region="US", source_tier="primary", default_importance=4, topic="policy",
    ),
    NewsSourceEntry(
        "sec",
        "U.S. Securities and Exchange Commission",
        "https://www.sec.gov/",
        AccessModel.PUBLIC,
        IntegrationMode.VERIFIED_RSS,
        (
            _feed("press_releases", "Press releases", "Press Releases", "https://www.sec.gov/news/pressreleases.rss", "www.sec.gov"),
        ),
        "https://www.sec.gov/news/pressreleases.rss",
        "Official SEC press-release feed. Respect SEC fair-access guidance and use a conservative polling interval.",
        content_policy=ContentPolicy.PUBLIC_DOCUMENT.value,
        region="US", source_tier="primary", default_importance=4, topic="finance",
    ),
    NewsSourceEntry(
        "ecb",
        "European Central Bank",
        "https://www.ecb.europa.eu/",
        AccessModel.PUBLIC,
        IntegrationMode.VERIFIED_RSS,
        (
            _feed("press_releases", "Press releases", "Press Releases", "https://www.ecb.europa.eu/rss/press.html", "www.ecb.europa.eu"),
        ),
        "https://www.ecb.europa.eu/rss/press.html",
        "Official ECB press-release RSS feed.",
        content_policy=ContentPolicy.PUBLIC_DOCUMENT.value,
        region="GLOBAL", source_tier="primary", default_importance=4, topic="policy",
    ),
    NewsSourceEntry(
        "bank_of_japan",
        "Bank of Japan",
        "https://www.boj.or.jp/en/",
        AccessModel.PUBLIC,
        IntegrationMode.VERIFIED_RSS,
        (_feed("whats_new", "What's New", "Monetary Policy and Markets", "https://www.boj.or.jp/en/rss/whatsnew.xml", "www.boj.or.jp"),),
        "https://www.boj.or.jp/en/rss/",
        "Official Bank of Japan English RSS; article pages and PDFs remain on the BOJ site.",
        content_policy=ContentPolicy.PUBLIC_DOCUMENT.value,
        region="JP", source_tier="primary", default_importance=4, topic="policy",
    ),
    NewsSourceEntry(
        "japan_meteorological_agency",
        "Japan Meteorological Agency",
        "https://www.jma.go.jp/jma/indexe.html",
        AccessModel.PUBLIC,
        IntegrationMode.VERIFIED_RSS,
        (_feed("high_frequency", "High-frequency alerts", "Disaster and Weather", "https://www.data.jma.go.jp/developer/xml/feed/extra.xml", "www.data.jma.go.jp"),),
        "https://www.data.jma.go.jp/developer/xml/feed/",
        "Official JMAXML Atom feed for high-frequency weather and disaster information.",
        region="JP", source_tier="primary", default_importance=4, topic="disaster",
    ),
    NewsSourceEntry(
        "world_health_organization",
        "World Health Organization",
        "https://www.who.int/",
        AccessModel.PUBLIC,
        IntegrationMode.VERIFIED_RSS,
        (NewsFeedTemplate("news_english", "News (English)", "Health", "https://www.who.int/rss-feeds/news-english.xml", ("www.who.int",), 30 * 86400),),
        "https://www.who.int/rss-feeds",
        "2026-09-12 实测：旧 RSS 最新消息停在 2026-02-25，暂不启用；网页入口返回 403，等待有效官方入口。",
        content_policy=ContentPolicy.PUBLIC_DOCUMENT.value,
        region="GLOBAL", source_tier="primary", default_importance=4, topic="health",
    ),
    NewsSourceEntry(
        "nasa",
        "NASA",
        "https://www.nasa.gov/",
        AccessModel.PUBLIC,
        IntegrationMode.VERIFIED_RSS,
        (_feed("news_releases", "News releases", "Science and Space", "https://www.nasa.gov/news-release/feed/", "www.nasa.gov", "science.nasa.gov"),),
        "https://www.nasa.gov/rss-feeds/",
        "Official NASA news-release feed.",
        content_policy=ContentPolicy.PUBLIC_DOCUMENT.value,
        region="US", source_tier="primary", default_importance=3, topic="science",
    ),
    NewsSourceEntry(
        "usgs_earthquakes",
        "U.S. Geological Survey",
        "https://earthquake.usgs.gov/",
        AccessModel.PUBLIC,
        IntegrationMode.VERIFIED_RSS,
        (_feed("significant_month", "Significant earthquakes", "Disaster", "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_month.atom", "earthquake.usgs.gov"),),
        "https://earthquake.usgs.gov/earthquakes/feed/v1.0/atom.php",
        "Official USGS significant-earthquake Atom feed. Consumers still apply age and deduplication policy.",
        content_policy=ContentPolicy.PUBLIC_DOCUMENT.value,
        region="GLOBAL", source_tier="primary", default_importance=5, topic="disaster",
    ),
    NewsSourceEntry(
        "nvidia_newsroom",
        "NVIDIA Newsroom",
        "https://nvidianews.nvidia.com/",
        AccessModel.PUBLIC,
        IntegrationMode.VERIFIED_RSS,
        (
            _feed(
                "news",
                "Newsroom",
                "Technology and AI",
                "https://nvidianews.nvidia.com/rss.xml",
                "nvidianews.nvidia.com",
                "blogs.nvidia.com",
            ),
        ),
        "https://nvidianews.nvidia.com/rss.xml",
        "Official NVIDIA newsroom RSS. Primary source for NVIDIA product, AI and semiconductor announcements; it will not capture third-party reporting about NVIDIA.",
        content_policy=ContentPolicy.PUBLIC_DOCUMENT.value,
        region="US", source_tier="primary", default_importance=3, topic="technology",
    ),
    NewsSourceEntry(
        "european_commission",
        "European Commission Press Corner",
        "https://ec.europa.eu/commission/presscorner/",
        AccessModel.PUBLIC,
        IntegrationMode.VERIFIED_RSS,
        (_feed("press", "Press Corner", "European policy and regulation",
               "https://ec.europa.eu/commission/presscorner/api/rss?language=en",
               "ec.europa.eu"),),
        "https://ec.europa.eu/commission/presscorner/",
        "Official European Commission RSS, verified current on 2026-09-16; covers EU policy, regulation and external affairs.",
        content_policy=ContentPolicy.PUBLIC_DOCUMENT.value,
        region="EUROPE", source_tier="primary", default_importance=4, topic="policy",
    ),
    NewsSourceEntry(
        "australian_prime_minister",
        "Prime Minister of Australia",
        "https://www.pm.gov.au/",
        AccessModel.PUBLIC,
        IntegrationMode.VERIFIED_RSS,
        (_feed("media", "Media releases", "Australian government and policy",
               "https://www.pm.gov.au/rss.xml", "www.pm.gov.au"),),
        "https://www.pm.gov.au/rss.xml",
        "Official Australian Prime Minister RSS, verified current on 2026-09-16; Australia/Oceania follows the Europe priority tier.",
        content_policy=ContentPolicy.PUBLIC_DOCUMENT.value,
        region="AUSTRALIA_OCEANIA", source_tier="primary", default_importance=4, topic="policy",
    ),
    NewsSourceEntry(
        "uk_government",
        "UK Government",
        "https://www.gov.uk/",
        AccessModel.PUBLIC,
        IntegrationMode.VERIFIED_RSS,
        (_feed("government", "Government news", "European government and policy",
               "https://www.gov.uk/government/feed", "www.gov.uk"),),
        "https://www.gov.uk/government/feed",
        "Official UK Government Atom feed, verified current on 2026-09-16; broad coverage is filtered by topic and importance.",
        content_policy=ContentPolicy.PUBLIC_DOCUMENT.value,
        region="EUROPE", source_tier="primary", default_importance=3, topic="policy",
    ),
    NewsSourceEntry(
        "bank_of_england",
        "Bank of England",
        "https://www.bankofengland.co.uk/",
        AccessModel.PUBLIC,
        IntegrationMode.VERIFIED_RSS,
        (_feed("news", "News", "European monetary and financial policy",
               "https://www.bankofengland.co.uk/rss/news", "www.bankofengland.co.uk"),),
        "https://www.bankofengland.co.uk/rss/news",
        "Official Bank of England news RSS, verified current on 2026-09-16; central-bank announcements are infrequent by design.",
        content_policy=ContentPolicy.PUBLIC_DOCUMENT.value,
        region="EUROPE", source_tier="primary", default_importance=4, topic="economy",
    ),
    NewsSourceEntry(
        "asean_secretariat",
        "ASEAN Secretariat",
        "https://asean.org/",
        AccessModel.PUBLIC,
        IntegrationMode.VERIFIED_RSS,
        (_feed("news", "News", "Southeast Asian regional affairs",
               "https://asean.org/feed/", "asean.org"),),
        "https://asean.org/",
        "Official ASEAN Secretariat RSS, verified current on 2026-09-16; covers regional diplomacy, economic and security cooperation.",
        content_policy=ContentPolicy.PUBLIC_DOCUMENT.value,
        region="SOUTHEAST_ASIA", source_tier="primary", default_importance=4, topic="diplomacy",
    ),
    NewsSourceEntry(
        "who_africa",
        "WHO Regional Office for Africa",
        "https://www.afro.who.int/",
        AccessModel.PUBLIC,
        IntegrationMode.VERIFIED_RSS,
        (_feed("news", "News", "African public health",
               "https://www.afro.who.int/rss.xml", "www.afro.who.int"),),
        "https://www.afro.who.int/",
        "Official WHO Africa RSS, verified current on 2026-09-16; covers regional outbreaks, health systems and emergency response.",
        content_policy=ContentPolicy.PUBLIC_DOCUMENT.value,
        region="AFRICA", source_tier="primary", default_importance=4, topic="health",
    ),
    NewsSourceEntry(
        "un_news_africa",
        "United Nations News - Africa",
        "https://news.un.org/en/",
        AccessModel.PUBLIC,
        IntegrationMode.VERIFIED_RSS,
        (_feed("africa", "Africa", "African diplomacy and humanitarian affairs",
               "https://news.un.org/feed/subscribe/en/news/region/africa/feed/rss.xml",
               "news.un.org"),),
        "https://news.un.org/en/",
        "Official United Nations Africa regional RSS, verified current on 2026-09-16; complements WHO Africa with diplomacy and humanitarian coverage.",
        content_policy=ContentPolicy.PUBLIC_DOCUMENT.value,
        region="AFRICA", source_tier="primary", default_importance=4, topic="world",
    ),
    NewsSourceEntry(
        "who_paho",
        "WHO Pan American Health Organization",
        "https://www.paho.org/",
        AccessModel.PUBLIC,
        IntegrationMode.VERIFIED_RSS,
        (_feed("news", "News", "Latin American and Caribbean public health",
               "https://www.paho.org/en/rss.xml", "www.paho.org"),),
        "https://www.paho.org/en",
        "Official PAHO RSS, verified current on 2026-09-16; regional public-health signal for Latin America and the Caribbean.",
        content_policy=ContentPolicy.PUBLIC_DOCUMENT.value,
        region="SOUTH_AMERICA", source_tier="primary", default_importance=4, topic="health",
    ),
    NewsSourceEntry(
        "channel_news_asia",
        "CNA (Channel NewsAsia)",
        "https://www.channelnewsasia.com/",
        AccessModel.PUBLIC,
        IntegrationMode.VERIFIED_RSS,
        (_feed("news", "News", "Southeast Asian and international current affairs",
               "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml",
               "www.channelnewsasia.com"),),
        "https://www.channelnewsasia.com/",
        "Current CNA RSS verified on 2026-09-16. High-quality secondary reporting, useful for regional context but not a substitute for primary sources.",
        content_policy=ContentPolicy.FEED_METADATA_ONLY.value,
        region="SOUTHEAST_ASIA", source_tier="secondary", default_importance=3, topic="world",
    ),
    NewsSourceEntry(
        "nist_news",
        "U.S. National Institute of Standards and Technology",
        "https://www.nist.gov/",
        AccessModel.PUBLIC,
        IntegrationMode.VERIFIED_RSS,
        (_feed("news", "News", "Technology, standards and cybersecurity",
               "https://www.nist.gov/news-events/news/rss.xml", "www.nist.gov"),),
        "https://www.nist.gov/news-events/news/rss.xml",
        "Official NIST RSS, verified current on 2026-09-16; primary technology, standards and cybersecurity research source.",
        content_policy=ContentPolicy.PUBLIC_DOCUMENT.value,
        region="US", source_tier="primary", default_importance=4, topic="technology",
    ),
    NewsSourceEntry(
        "new_york_times",
        "The New York Times",
        "https://www.nytimes.com/",
        AccessModel.SUBSCRIPTION,
        IntegrationMode.VERIFIED_RSS,
        (_feed("world", "World", "International politics and society",
               "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
               "rss.nytimes.com", "www.nytimes.com"),),
        "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
        "Official NYT World RSS, verified current on 2026-09-16. Secondary reporting and headline metadata only; full articles may require subscription.",
        region="NORTH_AMERICA", source_tier="secondary", default_importance=3, topic="world",
    ),
    NewsSourceEntry(
        "google_news",
        "Google News",
        "https://news.google.com/",
        AccessModel.PUBLIC,
        IntegrationMode.VERIFIED_RSS,
        (_feed("zh_cn", "中文聚合", "News aggregation and social signals",
               "https://news.google.com/rss?hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
               "news.google.com"),),
        "https://news.google.com/",
        "Google News public aggregation feed, verified current on 2026-09-16. It is a discovery and corroboration signal, never treated as a primary source.",
        content_policy=ContentPolicy.FEED_METADATA_ONLY.value,
        region="GLOBAL", source_tier="secondary", default_importance=2, topic="social",
    ),
    NewsSourceEntry(
        "un_news_middle_east",
        "United Nations News - Middle East",
        "https://news.un.org/en/",
        AccessModel.PUBLIC,
        IntegrationMode.VERIFIED_RSS,
        (_feed("middle_east", "Middle East", "Middle East diplomacy and humanitarian affairs",
               "https://news.un.org/feed/subscribe/en/news/region/middle-east/feed/rss.xml",
               "news.un.org"),),
        "https://news.un.org/en/",
        "Official United Nations regional RSS, verified current on 2026-09-16; covers Middle East diplomacy, conflict and humanitarian developments.",
        content_policy=ContentPolicy.PUBLIC_DOCUMENT.value,
        region="MIDDLE_EAST", source_tier="primary", default_importance=4, topic="world",
    ),
    NewsSourceEntry(
        "un_ocha",
        "United Nations Office for the Coordination of Humanitarian Affairs",
        "https://www.unocha.org/",
        AccessModel.PUBLIC,
        IntegrationMode.VERIFIED_RSS,
        (_feed("news", "News", "Global humanitarian affairs",
               "https://www.unocha.org/rss.xml", "www.unocha.org"),),
        "https://www.unocha.org/",
        "Official OCHA RSS candidate. The endpoint returned HTTP 406 to the bounded EOS collector on 2026-09-16, so it remains catalog-only until a stable public feed is available.",
        content_policy=ContentPolicy.PUBLIC_DOCUMENT.value,
        region="GLOBAL", source_tier="primary", default_importance=4, topic="world",
    ),
    NewsSourceEntry(
        "iaea_news",
        "International Atomic Energy Agency",
        "https://www.iaea.org/",
        AccessModel.PUBLIC,
        IntegrationMode.VERIFIED_RSS,
        (_feed("news", "News", "Nuclear safety, energy and non-proliferation",
               "https://www.iaea.org/feeds/news", "www.iaea.org"),),
        "https://www.iaea.org/feeds/news",
        "Official IAEA RSS, verified current on 2026-09-16; global primary source for nuclear safety, safeguards and energy developments.",
        content_policy=ContentPolicy.PUBLIC_DOCUMENT.value,
        region="GLOBAL", source_tier="primary", default_importance=5, topic="security",
    ),
    NewsSourceEntry(
        "us_embassy_china",
        "美国驻华大使馆",
        "https://china.usembassy-china.org.cn/",
        AccessModel.PUBLIC,
        IntegrationMode.VERIFIED_RSS,
        (
            _feed(
                "alerts",
                "Alerts",
                "Security and consular alerts",
                "https://china.usembassy-china.org.cn/category/alert/feed/",
                "china.usembassy-china.org.cn",
            ),
        ),
        "https://china.usembassy-china.org.cn/category/alert/feed/",
        "美国驻华大使馆官方 Alerts RSS 候选，覆盖自然灾害、领事和安全提醒。入口曾可解析，但 2026-09-16 对 EOS 的有界采集请求持续返回 403，因此保留在目录中但不进入默认上线批次；恢复稳定访问后再启用专用安全规则。",
        content_policy=ContentPolicy.PUBLIC_DOCUMENT.value,
        region="CN", source_tier="primary", default_importance=5, topic="security",
    ),
))
