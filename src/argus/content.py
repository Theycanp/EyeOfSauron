"""Policy-aware content documents and bounded public-document extraction."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import StrEnum
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urljoin, urlsplit

from .util import decode_http_content


MAX_DOCUMENT_CHARACTERS = 200_000
_METHOD_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_BLOCK_TAGS = frozenset({
    "address", "article", "blockquote", "br", "dd", "div", "dl", "dt", "figcaption",
    "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li",
    "main", "p", "pre", "section", "table", "td", "th", "tr",
})
_SKIP_TAGS = frozenset({"aside", "canvas", "form", "nav", "noscript", "script", "style", "svg"})
_UNSUPPORTED_DOCUMENT_SUFFIXES = frozenset({
    ".7z", ".avi", ".bmp", ".csv", ".doc", ".docm", ".docx", ".gif", ".jpeg", ".jpg",
    ".mov", ".mp3", ".mp4", ".ods", ".odt", ".png", ".ppt", ".pptm", ".pptx",
    ".rar", ".tar", ".tif", ".tiff", ".wav", ".webp", ".xls", ".xlsb", ".xlsm",
    ".xlsx", ".xml.gz", ".zip",
})


class ContentLevel(StrEnum):
    METADATA = "metadata"
    EXCERPT = "excerpt"
    FULL_TEXT = "full_text"
    DOCUMENT = "document"
    ANALYSIS = "analysis"


class ContentPolicy(StrEnum):
    FEED_METADATA_ONLY = "feed_metadata_and_original_link_only"
    FEED_FULL_TEXT = "feed_full_text_allowed"
    PUBLIC_DOCUMENT = "public_document_full_text"


class ContentFetchError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool, kind: str) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.kind = kind


@dataclass(frozen=True, slots=True)
class ContentDocumentDraft:
    level: ContentLevel
    source_method: str
    body: str
    media_type: str = "text/plain"
    canonical_url: str = ""
    rights_policy: str = "source_terms_apply"
    language: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _METHOD_RE.fullmatch(self.source_method):
            raise ValueError("content source method is invalid")
        if len(self.body) > MAX_DOCUMENT_CHARACTERS:
            raise ValueError("content document exceeds the character limit")
        if not self.media_type or len(self.media_type) > 128:
            raise ValueError("content media type is invalid")
        if self.canonical_url:
            parsed = urlsplit(self.canonical_url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("content canonical URL must be an HTTPS URL without credentials")
        if len(self.rights_policy) > 128 or len(self.language) > 32:
            raise ValueError("content document metadata is invalid")
        try:
            encoded = json.dumps(dict(self.metadata), ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("content metadata must be JSON serializable") from exc
        if len(encoded) > 16_384:
            raise ValueError("content metadata exceeds the size limit")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ContentFetchRequest:
    url: str
    allowed_hosts: tuple[str, ...]
    max_response_bytes: int = 4 * 1024 * 1024
    timeout_seconds: int = 20

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme != "https"
            or not host
            or parsed.username
            or parsed.password
            or host not in self.allowed_hosts
        ):
            raise ValueError("content fetch URL is outside the HTTPS host allowlist")
        if not 1024 <= self.max_response_bytes <= 10 * 1024 * 1024:
            raise ValueError("content response limit is invalid")
        if not 1 <= self.timeout_seconds <= 120:
            raise ValueError("content fetch timeout is invalid")


@dataclass(frozen=True, slots=True)
class ContentFetchWorkItem:
    id: int
    observation_id: int
    request: ContentFetchRequest
    lease_token: str
    attempts: int


def parse_content_policy(value: object) -> ContentPolicy:
    try:
        return ContentPolicy(str(value or ContentPolicy.FEED_METADATA_ONLY))
    except ValueError as exc:
        raise ValueError("content policy is invalid") from exc


def supports_public_document_fetch(url: str) -> bool:
    """Reject links that the bounded text/PDF extractor cannot consume."""
    path = urlsplit(url).path.casefold().rstrip("/")
    return not any(path.endswith(suffix) for suffix in _UNSUPPORTED_DOCUMENT_SUFFIXES)


def plain_text(value: str, *, limit: int = MAX_DOCUMENT_CHARACTERS) -> str:
    """Convert bounded HTML-ish input to readable, paragraph-preserving text."""
    parser = _ReadableHtmlParser()
    try:
        parser.feed(value)
        parser.close()
        result = parser.text()
    except Exception:
        result = _normalize_lines(value)
    return result[:limit].rstrip()


class _ReadableHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._article_depth = 0
        self._main_depth = 0
        self._body_depth = 0
        self._all: list[str] = []
        self._main: list[str] = []
        self._article: list[str] = []

    def _append(self, value: str) -> None:
        self._all.append(value)
        if self._main_depth:
            self._main.append(value)
        if self._article_depth:
            self._article.append(value)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "body":
            self._body_depth += 1
        if tag == "main":
            self._main_depth += 1
        if tag == "article":
            self._article_depth += 1
        if tag in _BLOCK_TAGS:
            self._append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if not self._skip_depth and tag.lower() in _BLOCK_TAGS:
            self._append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._skip_depth:
            if tag in _SKIP_TAGS:
                self._skip_depth -= 1
            return
        if tag in _BLOCK_TAGS:
            self._append("\n")
        if tag == "article" and self._article_depth:
            self._article_depth -= 1
        if tag == "main" and self._main_depth:
            self._main_depth -= 1
        if tag == "body" and self._body_depth:
            self._body_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._append(data)

    def text(self) -> str:
        article = _normalize_lines("".join(self._article))
        main = _normalize_lines("".join(self._main))
        body = _normalize_lines("".join(self._all))
        if len(article) >= 160:
            return article
        if len(main) >= 160:
            return main
        return body


def _normalize_lines(value: str) -> str:
    paragraphs: list[str] = []
    for raw in value.replace("\r", "\n").split("\n"):
        line = " ".join(raw.split())
        if line:
            paragraphs.append(line)
    return "\n\n".join(paragraphs)


def _decode_text(payload: bytes, charset: str | None) -> str:
    for encoding in (charset, "utf-8", "utf-8-sig", "gb18030", "latin-1"):
        if not encoding:
            continue
        try:
            return payload.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def _default_pdf_extractor(payload: bytes, timeout_seconds: int) -> str:
    pdftotext = Path("/usr/bin/pdftotext")
    prlimit = Path("/usr/bin/prlimit")
    if not pdftotext.is_file() or not prlimit.is_file():
        raise ContentFetchError(
            "PDF text extraction is unavailable", retryable=False, kind="unsupported_pdf"
        )
    with tempfile.TemporaryDirectory(prefix="argus-pdf-") as directory:
        source = Path(directory) / "document.pdf"
        output = Path(directory) / "document.txt"
        source.write_bytes(payload)
        try:
            completed = subprocess.run(
                [
                    str(prlimit), "--as=268435456", "--cpu=15", "--fsize=1048576", "--",
                    str(pdftotext), "-q", "-nopgbrk", str(source), str(output),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=min(timeout_seconds, 30),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ContentFetchError(
                "PDF text extraction timed out", retryable=True, kind="pdf_timeout"
            ) from exc
        if completed.returncode != 0 or not output.is_file():
            raise ContentFetchError(
                "PDF text extraction failed", retryable=False, kind="invalid_pdf"
            )
        return plain_text(output.read_text(encoding="utf-8", errors="replace"))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class PublicDocumentFetcher:
    """Fetch public official documents without becoming a general article scraper."""

    def __init__(
        self,
        opener: object | None = None,
        resolver: object = socket.getaddrinfo,
        pdf_extractor: Callable[[bytes, int], str] = _default_pdf_extractor,
    ) -> None:
        self._opener = opener or urllib.request.build_opener(_NoRedirect())
        self._resolver = resolver
        self._pdf_extractor = pdf_extractor

    def _validate_url(self, value: str, allowed_hosts: tuple[str, ...]) -> None:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme != "https"
            or not host
            or host not in allowed_hosts
            or parsed.username
            or parsed.password
        ):
            raise ContentFetchError(
                "document URL is outside the configured HTTPS host allowlist",
                retryable=False,
                kind="url_policy",
            )
        try:
            addresses = self._resolver(host, parsed.port or 443, type=socket.SOCK_STREAM)  # type: ignore[operator]
        except OSError as exc:
            raise ContentFetchError(
                "document host resolution failed", retryable=True, kind="dns"
            ) from exc
        if not addresses:
            raise ContentFetchError(
                "document host resolution returned no addresses", retryable=True, kind="dns"
            )
        for item in addresses:
            if not ipaddress.ip_address(item[4][0]).is_global:
                raise ContentFetchError(
                    "document host resolved to a non-public address",
                    retryable=False,
                    kind="url_policy",
                )

    def fetch(self, item: ContentFetchWorkItem) -> ContentDocumentDraft:
        request_spec = item.request
        current_url = request_spec.url
        response = None
        headers = {
            "Accept": "text/html, text/plain, application/pdf, application/xml, text/xml;q=0.8",
            "User-Agent": "Argus/0.13 (public document archiver; contact via source URL)",
        }
        for _ in range(6):
            self._validate_url(current_url, request_spec.allowed_hosts)
            request = urllib.request.Request(current_url, headers=headers, method="GET")
            try:
                response = self._opener.open(request, timeout=request_spec.timeout_seconds)  # type: ignore[attr-defined]
                break
            except urllib.error.HTTPError as exc:
                if exc.code in {301, 302, 303, 307, 308}:
                    location = exc.headers.get("Location")
                    if not location:
                        raise ContentFetchError(
                            "document redirect omitted Location",
                            retryable=False,
                            kind="redirect",
                        ) from exc
                    current_url = urljoin(current_url, location)
                    continue
                retryable = exc.code in {408, 425, 429} or 500 <= exc.code <= 599
                raise ContentFetchError(
                    f"document returned HTTP {exc.code}",
                    retryable=retryable,
                    kind=f"http_{exc.code}",
                ) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise ContentFetchError(
                    "document request failed", retryable=True, kind=type(exc).__name__.lower()
                ) from exc
        else:
            raise ContentFetchError(
                "document exceeded the redirect limit", retryable=False, kind="redirect"
            )
        if response is None:
            raise ContentFetchError("document returned no response", retryable=True, kind="empty")

        with response:
            final_url = response.geturl()
            self._validate_url(final_url, request_spec.allowed_hosts)
            media_type = response.headers.get_content_type().lower()
            allowed_types = {
                "text/html", "application/xhtml+xml", "text/plain", "application/pdf",
                "application/xml", "text/xml",
            }
            if media_type not in allowed_types:
                raise ContentFetchError(
                    f"unsupported document content type: {media_type}",
                    retryable=False,
                    kind="unsupported_type",
                )
            payload = response.read(request_spec.max_response_bytes + 1)
            if len(payload) > request_spec.max_response_bytes:
                raise ContentFetchError(
                    "document response exceeded the configured size limit",
                    retryable=False,
                    kind="size_limit",
                )
            try:
                payload = decode_http_content(
                    payload, response.headers.get("Content-Encoding", ""),
                    request_spec.max_response_bytes,
                )
            except ValueError as exc:
                raise ContentFetchError(
                    str(exc), retryable=False, kind="content_encoding",
                ) from exc
            charset = response.headers.get_content_charset()

        if media_type == "application/pdf":
            if not payload.startswith(b"%PDF-"):
                raise ContentFetchError("document is not a valid PDF", retryable=False, kind="invalid_pdf")
            body = self._pdf_extractor(payload, request_spec.timeout_seconds)
            level = ContentLevel.DOCUMENT
            source_method = "public_pdf"
        else:
            decoded = _decode_text(payload, charset)
            body = plain_text(decoded)
            level = ContentLevel.FULL_TEXT if media_type in {"text/html", "application/xhtml+xml"} else ContentLevel.DOCUMENT
            source_method = "public_html" if level is ContentLevel.FULL_TEXT else "public_text"
        if len(body.strip()) < 80:
            raise ContentFetchError(
                "document contained too little readable text",
                retryable=False,
                kind="empty_content",
            )
        return ContentDocumentDraft(
            level=level,
            source_method=source_method,
            body=body[:MAX_DOCUMENT_CHARACTERS],
            media_type="text/plain",
            canonical_url=final_url,
            rights_policy="public_official_document",
            metadata={"original_media_type": media_type, "source_bytes": len(payload)},
        )
