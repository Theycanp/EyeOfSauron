from __future__ import annotations

import hashlib
import html
import ipaddress
import socket
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from .config import RssSourceConfig
from .content import (
    ContentDocumentDraft,
    ContentFetchRequest,
    ContentLevel,
    ContentPolicy,
    parse_content_policy,
    plain_text,
)
from .models import FeedFetchResult, Observation, SourceState
from .util import truncate


class FeedError(RuntimeError):
    pass


class _NoDoctypeTreeBuilder(ET.TreeBuilder):
    def doctype(self, name: str, pubid: str | None, system: str | None) -> None:
        raise FeedError("feed document types and entity declarations are not allowed")


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


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _safe_link(value: str | None, allowed_hosts: tuple[str, ...]) -> str:
    if not value:
        return ""
    try:
        parsed = urlsplit(value.strip())
        parsed.port
    except ValueError:
        return ""
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or any(ord(char) < 32 for char in value)):
        return ""
    hostname = parsed.hostname.lower()
    if hostname not in allowed_hosts:
        return ""
    if parsed.scheme == "http":
        return parsed._replace(scheme="https").geturl()
    return value.strip()


def _external_id(guid: str, link: str, title: str, published_at: datetime) -> str:
    if guid.strip():
        return truncate(guid.strip(), 512)
    material = "\x1f".join((link.strip(), " ".join(title.casefold().split())))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def parse_feed(payload: bytes, source: RssSourceConfig) -> tuple[Observation, ...]:
    if len(payload) > source.max_response_bytes:
        raise FeedError("feed exceeds the configured response limit")
    try:
        root = ET.fromstring(payload, parser=ET.XMLParser(target=_NoDoctypeTreeBuilder()))
    except ET.ParseError as exc:
        raise FeedError(f"invalid XML: {exc}") from exc

    observations: list[Observation] = []
    policy = parse_content_policy(source.settings.get("content_policy"))
    content_character_limit = int(source.settings.get("content_max_characters", 100_000))
    content_response_limit = int(source.settings.get("content_max_response_bytes", 4 * 1024 * 1024))
    content_timeout = int(source.settings.get("content_timeout_seconds", source.request_timeout_seconds))

    def content_payloads(
        summary_raw: str | None,
        full_raw: str | None,
        link: str,
        *,
        summary_method: str,
        full_method: str,
    ) -> tuple[tuple[ContentDocumentDraft, ...], ContentFetchRequest | None]:
        documents: list[ContentDocumentDraft] = []
        excerpt = _plain_text(summary_raw or full_raw, 4000)
        if excerpt:
            documents.append(ContentDocumentDraft(
                ContentLevel.EXCERPT,
                summary_method,
                excerpt,
                canonical_url=link,
                rights_policy="source_terms_apply",
            ))
        has_feed_full_text = False
        if policy in {ContentPolicy.FEED_FULL_TEXT, ContentPolicy.PUBLIC_DOCUMENT} and full_raw:
            full_text = plain_text(full_raw, limit=content_character_limit)
            if full_text:
                documents.append(ContentDocumentDraft(
                    ContentLevel.FULL_TEXT,
                    full_method,
                    full_text,
                    canonical_url=link,
                    rights_policy=(
                        "public_official_document"
                        if policy is ContentPolicy.PUBLIC_DOCUMENT
                        else "source_authorized_feed"
                    ),
                ))
                has_feed_full_text = True
        fetch_request = None
        if policy is ContentPolicy.PUBLIC_DOCUMENT and link and not has_feed_full_text:
            fetch_request = ContentFetchRequest(
                link,
                source.allowed_hosts,
                max_response_bytes=content_response_limit,
                timeout_seconds=content_timeout,
            )
        return tuple(documents), fetch_request

    # RSS 1.0 (RDF), used by Japanese ministries, has namespaced sibling items.
    if root.tag == "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF":
        normalized = ET.Element("rss")
        rdf_channel = ET.SubElement(normalized, "channel")
        for entry in root.findall("{http://purl.org/rss/1.0/}item"):
            item = ET.SubElement(rdf_channel, "item")
            for child in entry:
                tag = child.tag.removeprefix("{http://purl.org/rss/1.0/}")
                if tag == "{http://purl.org/dc/elements/1.1/}date":
                    tag = "pubDate"
                ET.SubElement(item, tag).text = child.text
            ET.SubElement(item, "guid").text = entry.get(
                "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about", ""
            )
        root = normalized
    channel = root.find("channel")
    if channel is not None:
        for item in channel.findall("item"):
            title = _plain_text(item.findtext("title"), 1000)
            if not title:
                continue
            summary_raw = item.findtext("description")
            full_raw = item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded")
            summary = _plain_text(summary_raw or full_raw, 4000)
            link = _safe_link(item.findtext("link"), source.allowed_hosts)
            documents, content_fetch = content_payloads(
                summary_raw,
                full_raw,
                link,
                summary_method="rss_description",
                full_method="rss_content",
            )
            parsed_date = _parse_datetime(item.findtext("pubDate"))
            published_at = parsed_date or datetime.now(UTC)
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
                attributes={"section": source.section, "creator": truncate(creator, 500),
                            "published_at_inferred": parsed_date is None},
                content_documents=documents,
                content_fetch=content_fetch,
            ))
    else:
        namespace = "{http://www.w3.org/2005/Atom}"
        entries = root.findall(f"{namespace}entry")
        for entry in entries:
            title = _plain_text(entry.findtext(f"{namespace}title"), 1000)
            if not title:
                continue
            summary_raw = entry.findtext(f"{namespace}summary")
            full_raw = entry.findtext(f"{namespace}content")
            summary = _plain_text(summary_raw or full_raw, 4000)
            link_value = ""
            for link_node in entry.findall(f"{namespace}link"):
                if link_node.attrib.get("rel", "alternate") == "alternate":
                    link_value = link_node.attrib.get("href", "")
                    break
            link = _safe_link(link_value, source.allowed_hosts)
            documents, content_fetch = content_payloads(
                summary_raw,
                full_raw,
                link,
                summary_method="atom_summary",
                full_method="atom_content",
            )
            parsed_date = _parse_datetime(
                entry.findtext(f"{namespace}published") or entry.findtext(f"{namespace}updated")
            )
            published_at = parsed_date or datetime.now(UTC)
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
                attributes={"section": source.section, "published_at_inferred": parsed_date is None},
                content_documents=documents,
                content_fetch=content_fetch,
            ))

    if not observations:
        raise FeedError("feed contained no usable entries")
    observations.sort(key=lambda item: (item.published_at, item.external_id))
    return tuple(observations)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _validate_fetch_url(
    value: str,
    allowed_hosts: tuple[str, ...],
    resolver: object = socket.getaddrinfo,
) -> None:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not host
        or host not in allowed_hosts
        or parsed.username
        or parsed.password
    ):
        raise FeedError("feed URL is outside the configured HTTPS host allowlist")
    try:
        addresses = resolver(host, parsed.port or 443, type=socket.SOCK_STREAM)  # type: ignore[operator]
    except OSError as exc:
        raise FeedError(f"feed host resolution failed: {type(exc).__name__}") from exc
    if not addresses:
        raise FeedError("feed host resolution returned no addresses")
    for item in addresses:
        address = ipaddress.ip_address(item[4][0])
        if not address.is_global:
            raise FeedError("feed host resolved to a non-public address")


class RssCollector:
    content_types = frozenset({
        "application/rss+xml", "application/atom+xml", "application/xml", "text/xml",
        "application/rdf+xml",
    })

    def parse_payload(self, payload: bytes) -> tuple[Observation, ...]:
        return parse_feed(payload, self.config)

    def __init__(
        self,
        config: RssSourceConfig,
        opener: object | None = None,
        resolver: object = socket.getaddrinfo,
    ) -> None:
        self.config = config
        self._opener = opener or urllib.request.build_opener(_NoRedirect())
        self._resolver = resolver

    def fetch(self, state: SourceState) -> FeedFetchResult:
        max_content_age = int(self.config.settings.get("max_content_age_seconds", 0))
        headers = {
            "Accept": ", ".join(sorted(self.content_types)),
            "User-Agent": "Argus/0.7 (personal feed monitor)",
        }
        if state.etag and not max_content_age:
            headers["If-None-Match"] = state.etag
        if state.last_modified and not max_content_age:
            headers["If-Modified-Since"] = state.last_modified
        current_url = str(self.config.url)
        response = None
        for _ in range(6):
            _validate_fetch_url(current_url, self.config.allowed_hosts, self._resolver)
            request = urllib.request.Request(current_url, headers=headers, method="GET")
            try:
                response = self._opener.open(
                    request, timeout=self.config.request_timeout_seconds  # type: ignore[attr-defined]
                )
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 304:
                    if max_content_age:
                        raise FeedError("feed freshness cannot be verified from HTTP 304") from exc
                    return FeedFetchResult(
                        observations=(),
                        etag=exc.headers.get("ETag") or state.etag,
                        last_modified=exc.headers.get("Last-Modified") or state.last_modified,
                        not_modified=True,
                    )
                if exc.code in {301, 302, 303, 307, 308}:
                    location = exc.headers.get("Location")
                    if not location:
                        raise FeedError("feed redirect omitted Location") from exc
                    current_url = urljoin(current_url, location)
                    continue
                raise FeedError(f"feed returned HTTP {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise FeedError(f"feed request failed: {type(exc).__name__}") from exc
        else:
            raise FeedError("feed exceeded the redirect limit")
        if response is None:
            raise FeedError("feed returned no response")

        with response:
            final_url = response.geturl()
            _validate_fetch_url(final_url, self.config.allowed_hosts, self._resolver)
            content_type = response.headers.get_content_type().lower()
            if content_type not in self.content_types:
                raise FeedError(f"unexpected feed content type: {content_type}")
            payload = response.read(self.config.max_response_bytes + 1)
            if len(payload) > self.config.max_response_bytes:
                raise FeedError("feed response exceeded configured size limit")
            observations = self.parse_payload(payload)
            if max_content_age:
                published_dates = [
                    item.published_at for item in observations
                    if not item.attributes.get("published_at_inferred")
                ]
                if not published_dates:
                    raise FeedError("feed freshness cannot be verified without publication timestamps")
                age = int((datetime.now(UTC) - max(published_dates)).total_seconds())
                if age > max_content_age:
                    raise FeedError(
                        f"feed content is stale: newest entry is {age} seconds old "
                        f"(limit {max_content_age} seconds)"
                    )
            return FeedFetchResult(
                observations=observations,
                etag=response.headers.get("ETag"),
                last_modified=response.headers.get("Last-Modified"),
            )
