from __future__ import annotations

import hashlib
import html
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit

from .config import RssSourceConfig
from .models import FeedFetchResult, Observation, SourceState
from .util import truncate


class FeedError(RuntimeError):
    pass


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return " ".join("".join(self.parts).split())


def _plain_text(value: str | None, limit: int) -> str:
    if not value:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(html.unescape(value))
        parser.close()
        result = parser.text()
    except Exception:
        result = " ".join(value.split())
    return truncate(result, limit)


def _parse_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _safe_link(value: str | None, allowed_hosts: tuple[str, ...]) -> str:
    if not value:
        return ""
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or not parsed.hostname:
        return ""
    hostname = parsed.hostname.lower()
    if hostname not in allowed_hosts:
        return ""
    return value.strip()


def _external_id(guid: str, link: str, title: str, published_at: datetime) -> str:
    if guid.strip():
        return truncate(guid.strip(), 512)
    material = "\x1f".join((link, title, published_at.isoformat()))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def parse_feed(payload: bytes, source: RssSourceConfig) -> tuple[Observation, ...]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise FeedError(f"invalid XML: {exc}") from exc

    observations: list[Observation] = []
    channel = root.find("channel")
    if channel is not None:
        for item in channel.findall("item"):
            title = _plain_text(item.findtext("title"), 1000)
            if not title:
                continue
            summary = _plain_text(item.findtext("description"), 4000)
            link = _safe_link(item.findtext("link"), source.allowed_hosts)
            published_at = _parse_datetime(item.findtext("pubDate"))
            guid = item.findtext("guid") or ""
            creator = item.findtext("{http://purl.org/dc/elements/1.1/}creator") or ""
            observations.append(Observation(
                source_id=source.id,
                publisher=source.publisher,
                dedupe_scope=source.dedupe_scope,
                external_id=_external_id(guid, link, title, published_at),
                published_at=published_at,
                title=title,
                summary=summary,
                url=link,
                attributes={"section": source.section, "creator": truncate(creator, 500)},
            ))
    else:
        namespace = "{http://www.w3.org/2005/Atom}"
        entries = root.findall(f"{namespace}entry")
        for entry in entries:
            title = _plain_text(entry.findtext(f"{namespace}title"), 1000)
            if not title:
                continue
            summary = _plain_text(
                entry.findtext(f"{namespace}summary") or entry.findtext(f"{namespace}content"), 4000
            )
            link_value = ""
            for link_node in entry.findall(f"{namespace}link"):
                if link_node.attrib.get("rel", "alternate") == "alternate":
                    link_value = link_node.attrib.get("href", "")
                    break
            link = _safe_link(link_value, source.allowed_hosts)
            published_at = _parse_datetime(
                entry.findtext(f"{namespace}published") or entry.findtext(f"{namespace}updated")
            )
            guid = entry.findtext(f"{namespace}id") or ""
            observations.append(Observation(
                source_id=source.id,
                publisher=source.publisher,
                dedupe_scope=source.dedupe_scope,
                external_id=_external_id(guid, link, title, published_at),
                published_at=published_at,
                title=title,
                summary=summary,
                url=link,
                attributes={"section": source.section},
            ))

    if not observations:
        raise FeedError("feed contained no usable entries")
    observations.sort(key=lambda item: (item.published_at, item.external_id))
    return tuple(observations)


class RssCollector:
    def __init__(self, config: RssSourceConfig) -> None:
        self.config = config

    def fetch(self, state: SourceState) -> FeedFetchResult:
        headers = {
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9",
            "User-Agent": "SignalWatch/0.1 (personal feed monitor)",
        }
        if state.etag:
            headers["If-None-Match"] = state.etag
        if state.last_modified:
            headers["If-Modified-Since"] = state.last_modified
        request = urllib.request.Request(self.config.url, headers=headers, method="GET")

        try:
            response = urllib.request.urlopen(request, timeout=self.config.request_timeout_seconds)
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return FeedFetchResult(
                    observations=(),
                    etag=exc.headers.get("ETag") or state.etag,
                    last_modified=exc.headers.get("Last-Modified") or state.last_modified,
                    not_modified=True,
                )
            raise FeedError(f"feed returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise FeedError(f"feed request failed: {exc}") from exc

        with response:
            final_url = urlsplit(response.geturl())
            final_host = (final_url.hostname or "").lower()
            if final_url.scheme != "https" or final_host not in self.config.allowed_hosts:
                raise FeedError("feed redirected outside the configured HTTPS host allowlist")
            content_type = response.headers.get_content_type().lower()
            if content_type not in {"application/rss+xml", "application/atom+xml", "application/xml", "text/xml"}:
                raise FeedError(f"unexpected feed content type: {content_type}")
            payload = response.read(self.config.max_response_bytes + 1)
            if len(payload) > self.config.max_response_bytes:
                raise FeedError("feed response exceeded configured size limit")
            return FeedFetchResult(
                observations=parse_feed(payload, self.config),
                etag=response.headers.get("ETag"),
                last_modified=response.headers.get("Last-Modified"),
            )
