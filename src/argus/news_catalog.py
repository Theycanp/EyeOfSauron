from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from .content import ContentPolicy


_CATALOG_ID = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_VERIFIED_ON = "2026-09-05"


class NewsCatalogError(ValueError):
    pass


class IntegrationMode(StrEnum):
    VERIFIED_RSS = "verified_rss"
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

    def __post_init__(self) -> None:
        if not _CATALOG_ID.fullmatch(self.id):
            raise NewsCatalogError(f"invalid catalog entry id: {self.id}")
        _validate_https_url(self.homepage_url)
        _validate_https_url(self.evidence_url)
        if self.default_enabled:
            raise NewsCatalogError("catalog entries must remain disabled by default")
        if not self.requires_user_confirmation:
            raise NewsCatalogError("catalog entries must require explicit user confirmation")
        if self.integration_mode is IntegrationMode.VERIFIED_RSS and not self.feeds:
            raise NewsCatalogError(f"verified RSS entry {self.id} has no feed templates")
        if self.region not in {"CN", "JP", "US", "GLOBAL", "OTHER"}:
            raise NewsCatalogError(f"invalid catalog region: {self.region}")
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
            "kind": "rss",
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
        if entry.integration_mode is IntegrationMode.VERIFIED_RSS:
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


NEWS_SOURCE_CATALOG = NewsSourceCatalog((
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
        content_policy=ContentPolicy.PUBLIC_DOCUMENT.value,
        region="JP", source_tier="primary", default_importance=5, topic="disaster",
    ),
    NewsSourceEntry(
        "china_ndrc",
        "National Development and Reform Commission",
        "https://www.ndrc.gov.cn/",
        AccessModel.PUBLIC,
        IntegrationMode.VERIFIED_RSS,
        (_feed("press_releases", "Press releases", "Policy and Economy", "https://www.ndrc.gov.cn/xwdt/xwfb/rss.xml", "www.ndrc.gov.cn"),),
        "https://www.ndrc.gov.cn/xwdt/xwfb/rss.xml",
        "Official NDRC RSS endpoint; publication cadence varies by announcement schedule.",
        content_policy=ContentPolicy.PUBLIC_DOCUMENT.value,
        region="CN", source_tier="primary", default_importance=4, topic="policy",
    ),
    NewsSourceEntry(
        "world_health_organization",
        "World Health Organization",
        "https://www.who.int/",
        AccessModel.PUBLIC,
        IntegrationMode.VERIFIED_RSS,
        (_feed("news_english", "News (English)", "Health", "https://www.who.int/rss-feeds/news-english.xml", "www.who.int"),),
        "https://www.who.int/rss-feeds",
        "Official WHO news releases, statements, and media notes.",
        content_policy=ContentPolicy.PUBLIC_DOCUMENT.value,
        region="GLOBAL", source_tier="primary", default_importance=4, topic="health",
    ),
    NewsSourceEntry(
        "nasa",
        "NASA",
        "https://www.nasa.gov/",
        AccessModel.PUBLIC,
        IntegrationMode.VERIFIED_RSS,
        (_feed("news_releases", "News releases", "Science and Space", "https://www.nasa.gov/news-release/feed/", "www.nasa.gov"),),
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
))
