"""Dated official announcement indexes using the same bounded transport as RSS."""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from .models import Observation
from .rss import FeedError, RssCollector, _safe_link, parse_feed


class _AnnouncementLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str, str]] = []
        self.anchor: dict[str, str | None] | None = None
        self.parts: list[str] = []
        self.date = ""
        self.in_time = False
        self.ignored = 0
        self.group_date = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.ignored += 1
        if self.ignored:
            return
        if tag == "a":
            self.anchor = dict(attrs)
            self.parts = []
            self.date = ""
            marker = re.fullmatch(r"a(20\d{2})(\d{2})(\d{2})", self.anchor.get("name") or "")
            if marker:
                self.group_date = "-".join(marker.groups())
            if "information-item-inner" in (self.anchor.get("class") or "").split():
                self.date = self.group_date
        if tag == "time" and self.anchor is not None:
            self.date = dict(attrs).get("datetime") or ""
            self.in_time = True

    def handle_data(self, data: str) -> None:
        if self.anchor is not None and not self.in_time and not self.ignored:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.ignored = max(0, self.ignored - 1)
        if tag == "time":
            self.in_time = False
        if tag == "a" and self.anchor is not None:
            title = self.anchor.get("title") or " ".join("".join(self.parts).split())
            self.links.append((self.anchor.get("href") or "", title, self.date))
            self.anchor = None
            self.in_time = False


def _publication_date(url: str, explicit: str, timezone: str) -> datetime | None:
    match = re.search(r"(?:/t|/)(20\d{2})(\d{2})(\d{2})[_./]", urlsplit(url).path)
    value = explicit[:10] if explicit else (
        "-".join(match.groups()) if match else ""
    )
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(timezone))
    except ValueError:
        return None


class OfficialListCollector(RssCollector):
    """Only retain dated article links in explicitly configured URL prefixes.

    No scripts, pagination or linked pages execute here. Public text enrichment
    remains in the independent content queue; an empty index is an error.
    """
    content_types = frozenset({"text/html", "application/xhtml+xml", "application/json"})

    def parse_payload(self, payload: bytes) -> tuple[Observation, ...]:
        if len(payload) > self.config.max_response_bytes:
            raise FeedError("official index exceeds configured response limit")
        try:
            text = payload.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise FeedError("official index must use UTF-8") from exc
        if self.config.settings.get("index_format") == "govcn_json":
            try:
                rows = json.loads(text)
                if not isinstance(rows, list):
                    raise ValueError("index is not a list")
                links = [(row['URL'], row['TITLE'], row['DOCRELPUBTIME']) for row in rows]
                if any(not all(isinstance(value, str) for value in row) for row in links):
                    raise ValueError("index fields are not text")
            except (ValueError, KeyError, TypeError) as exc:
                raise FeedError("official JSON index has an invalid shape") from exc
        else:
            parser = _AnnouncementLinks()
            parser.feed(text)
            parser.close()
            links = parser.links
        root = ET.Element("rss")
        channel = ET.SubElement(root, "channel")
        seen: set[str] = set()
        prefixes = self.config.settings.get("article_url_prefixes", [])
        timezone = str(self.config.settings.get("timezone", "UTC"))
        for href, title, date in links:
            link = _safe_link(urljoin(str(self.config.url), href), self.config.allowed_hosts)
            if not link or link in seen or len(title.strip()) < 6:
                continue
            if not any(link.startswith(prefix) for prefix in prefixes):
                continue
            published = _publication_date(link, date, timezone)
            if published is None:
                continue
            seen.add(link)
            item = ET.SubElement(channel, "item")
            for tag, value in (
                ("title", title[:1000]), ("link", link), ("guid", link),
                ("pubDate", published.isoformat()),
            ):
                ET.SubElement(item, tag).text = value
        if not seen:
            raise FeedError("official index contained no dated article links; check layout and prefixes")
        return parse_feed(ET.tostring(root, encoding="utf-8"), self.config)
