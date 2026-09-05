from __future__ import annotations

import email
import email.policy
import imaplib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from email.header import decode_header
from typing import Any, Mapping

from .config import SourceConfig
from .market import MarketCollector
from .host import HostHealthCollector
from .models import FeedFetchResult, Observation, SourceState
from .providers import (
    DEFAULT_PROVIDER_REGISTRY,
    ProviderConfigError,
    ProviderRuntimeError,
    ProviderRegistry,
)
from .rss import RssCollector
from .util import truncate


class AdapterError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _header(value: str | None) -> str:
    if not value:
        return ""
    parts: list[str] = []
    for chunk, charset in decode_header(value):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return " ".join("".join(parts).split())


def _mail_body(message: email.message.Message) -> str:
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                return str(part.get_content())
        return ""
    if message.get_content_type() == "text/plain":
        return str(message.get_content())
    return ""


def _imap_cursor(value: str | None) -> tuple[str | None, int]:
    if not value:
        return None, 0
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        decoded = None
    if isinstance(decoded, Mapping):
        uidvalidity = str(decoded.get("uidvalidity", "")).strip() or None
        try:
            last_uid = max(0, int(decoded.get("last_uid", 0)))
        except (TypeError, ValueError):
            last_uid = 0
        return uidvalidity, last_uid
    try:
        return None, max(0, int(value))
    except (TypeError, ValueError):
        return None, 0


def _imap_uidvalidity(client: Any) -> str | None:
    try:
        _, values = client.response("UIDVALIDITY")
    except (AttributeError, imaplib.IMAP4.error, OSError):
        return None
    for value in values or []:
        if isinstance(value, bytes):
            decoded = value.decode("ascii", errors="ignore").strip()
        else:
            decoded = str(value).strip()
        if decoded.isdigit():
            return decoded
    return None


class ImapCollector:
    """Bounded incremental IMAP collector using UIDVALIDITY and contiguous UIDs."""

    def __init__(
        self,
        config: SourceConfig,
        environment: Mapping[str, str] | None = None,
        client_factory: Any | None = None,
    ) -> None:
        self.config = config
        settings = config.settings or {}
        self.host = str(settings.get("host", ""))
        self.port = int(settings.get("port", 993))
        self.mailbox = str(settings.get("mailbox", "INBOX"))
        self.search = str(settings.get("search", "ALL")).strip() or "ALL"
        self.batch_size = max(1, min(500, int(settings.get("batch_size", 100))))
        self.max_message_bytes = max(
            1024,
            min(config.max_response_bytes, int(settings.get("max_message_bytes", config.max_response_bytes))),
        )
        self.client_factory = client_factory or imaplib.IMAP4_SSL
        env = os.environ if environment is None else environment
        self.username = env.get(str(settings.get("username_env", "")), "")
        self.password = env.get(str(settings.get("password_env", "")), "")
        if not self.host or not self.username or not self.password:
            raise AdapterError(f"imap source {config.id} is missing host or credentials")

    def fetch(self, state: SourceState) -> FeedFetchResult:
        client: Any | None = None
        try:
            client = self.client_factory(
                self.host,
                self.port,
                timeout=self.config.request_timeout_seconds,
            )
            client.login(self.username, self.password)
            status, _ = client.select(self.mailbox, readonly=True)
            if status != "OK":
                raise AdapterError("imap mailbox selection failed")

            stored_validity, last_uid = _imap_cursor(state.cursor)
            current_validity = _imap_uidvalidity(client)
            if current_validity and stored_validity and current_validity != stored_validity:
                last_uid = 0
            effective_validity = current_validity or stored_validity

            criteria = f"{self.search} UID {last_uid + 1}:*"
            status, data = client.uid("SEARCH", None, criteria)
            if status != "OK":
                raise AdapterError("imap search failed")
            uids = sorted({int(item) for item in (data[0] or b"").split() if item.isdigit()})
            pending = [uid for uid in uids if uid > last_uid][: self.batch_size]
            observations: list[Observation] = []
            processed_uid = last_uid
            for uid in pending:
                request = f"(BODY.PEEK[]<0.{self.max_message_bytes + 1}>)"
                status, fetched = client.uid("FETCH", str(uid), request)
                if status != "OK":
                    raise AdapterError(f"imap fetch failed for UID {uid}")
                raw = next((part[1] for part in fetched if isinstance(part, tuple)), None)
                if not isinstance(raw, bytes):
                    raise AdapterError(f"imap fetch returned no message for UID {uid}")
                if len(raw) > self.max_message_bytes:
                    raise AdapterError(f"imap message UID {uid} exceeded configured size limit")
                try:
                    message = email.message_from_bytes(raw, policy=email.policy.default)
                except (TypeError, ValueError) as exc:
                    raise AdapterError(f"imap message UID {uid} is invalid") from exc
                subject = truncate(_header(message.get("Subject")) or "(无主题)", 1000)
                sender = _header(message.get("From"))
                body = truncate(" ".join(_mail_body(message).split()), 4000)
                date_value = message.get("Date")
                published = datetime.now(UTC)
                if date_value:
                    try:
                        parsed = email.utils.parsedate_to_datetime(date_value)
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=UTC)
                        published = parsed.astimezone(UTC)
                    except (TypeError, ValueError, OverflowError):
                        pass
                external = _header(message.get("Message-ID"))
                if not external:
                    external = f"uid:{effective_validity or 'unknown'}:{uid}"
                observations.append(Observation(
                    source_id=self.config.id,
                    publisher=self.config.publisher,
                    dedupe_scope=self.config.dedupe_scope,
                    external_id=truncate(external, 512),
                    published_at=published,
                    title=subject,
                    summary=body,
                    url="",
                    attributes={
                        "section": self.config.section,
                        "from": truncate(sender, 500),
                        "uid": uid,
                        "uidvalidity": effective_validity or "",
                    },
                ))
                processed_uid = uid

            cursor = json.dumps(
                {"uidvalidity": effective_validity, "last_uid": processed_uid},
                sort_keys=True,
            )
            return FeedFetchResult(
                tuple(observations),
                None,
                None,
                not_modified=not observations,
                cursor=cursor,
            )
        except AdapterError:
            raise
        except (imaplib.IMAP4.error, OSError, TimeoutError) as exc:
            raise AdapterError(f"imap request failed: {type(exc).__name__}") from exc
        finally:
            if client is not None:
                try:
                    client.logout()
                except (imaplib.IMAP4.error, OSError):
                    pass


def _x_cursor(value: str | None) -> tuple[str | None, str | None, str | None]:
    if not value:
        return None, None, None
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        decoded = None
    if isinstance(decoded, Mapping):
        since_id = str(decoded.get("since_id", "")).strip() or None
        pagination_token = str(decoded.get("pagination_token", "")).strip() or None
        max_seen_id = str(decoded.get("max_seen_id", "")).strip() or since_id
        return since_id, pagination_token, max_seen_id
    return (value if value.isdigit() else None), None, (value if value.isdigit() else None)


class XCollector:
    """X API v2 timeline collector with resumable, lossless pagination."""

    _TRUSTED_HOSTS = {"api.x.com", "api.twitter.com"}

    def __init__(
        self,
        config: SourceConfig,
        environment: Mapping[str, str] | None = None,
        opener: Any | None = None,
    ) -> None:
        settings = config.settings or {}
        self.config = config
        self.user_id = str(settings.get("user_id", "")).strip()
        if not self.user_id.isdigit():
            raise AdapterError(f"x source {config.id} requires numeric user_id")
        env = os.environ if environment is None else environment
        self.token = env.get(str(settings.get("bearer_token_env", "")), "")
        if not self.token:
            raise AdapterError(f"x source {config.id} is missing bearer token")
        self.base_url = str(settings.get("api_base_url", "https://api.x.com/2")).rstrip("/")
        parsed = urllib.parse.urlsplit(self.base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise AdapterError("x api_base_url must be a plain HTTPS origin/path")
        allowed = {host.lower() for host in config.allowed_hosts} or self._TRUSTED_HOSTS
        if parsed.hostname.lower() not in allowed:
            raise AdapterError("x api_base_url host is not allowed for the configured credential")
        self.max_pages = max(1, min(100, int(settings.get("max_pages_per_poll", 5))))
        self._opener = opener or urllib.request.build_opener(_NoRedirect())

    def fetch(self, state: SourceState) -> FeedFetchResult:
        since_id, pagination_token, max_seen_id = _x_cursor(state.cursor)
        observations: list[Observation] = []
        next_token = pagination_token
        pages = 0
        while pages < self.max_pages:
            query = {
                "tweet.fields": "created_at,lang,public_metrics",
                "max_results": "100",
            }
            if bool((self.config.settings or {}).get("exclude_replies", True)):
                query["exclude"] = "retweets,replies"
            if since_id:
                query["since_id"] = since_id
            if next_token:
                query["pagination_token"] = next_token
            url = f"{self.base_url}/users/{self.user_id}/tweets?{urllib.parse.urlencode(query)}"
            request = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/json",
                    "User-Agent": "Argus/0.7",
                },
            )
            try:
                with self._opener.open(
                    request, timeout=self.config.request_timeout_seconds
                ) as response:
                    payload = response.read(self.config.max_response_bytes + 1)
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
                raise AdapterError(f"x request failed: {type(exc).__name__}") from exc
            if len(payload) > self.config.max_response_bytes:
                raise AdapterError("x response exceeded configured size limit")
            try:
                data = json.loads(payload)
            except (TypeError, ValueError) as exc:
                raise AdapterError("x API returned invalid JSON") from exc
            if not isinstance(data, Mapping):
                raise AdapterError("x API returned a non-object")
            if data.get("errors") and not data.get("data"):
                raise AdapterError("x API returned an error response")
            rows = data.get("data", [])
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, Mapping) or not row.get("id"):
                    continue
                tweet_id = str(row["id"])
                if not tweet_id.isdigit():
                    continue
                if max_seen_id is None or int(tweet_id) > int(max_seen_id):
                    max_seen_id = tweet_id
                created = row.get("created_at")
                try:
                    published = datetime.fromisoformat(
                        str(created).replace("Z", "+00:00")
                    ).astimezone(UTC)
                except (TypeError, ValueError):
                    published = datetime.now(UTC)
                body = " ".join(str(row.get("text", "")).split())
                if not body:
                    continue
                observations.append(Observation(
                    source_id=self.config.id,
                    publisher=self.config.publisher,
                    dedupe_scope=self.config.dedupe_scope,
                    external_id=tweet_id,
                    published_at=published,
                    title=truncate(body, 1000),
                    summary="",
                    url=f"https://x.com/i/web/status/{tweet_id}",
                    attributes={
                        "section": self.config.section,
                        "user_id": self.user_id,
                        "lang": str(row.get("lang", "")),
                    },
                ))
            meta = data.get("meta") if isinstance(data.get("meta"), Mapping) else {}
            raw_next = meta.get("next_token") if isinstance(meta, Mapping) else None
            next_token = str(raw_next).strip() if raw_next else None
            pages += 1
            if not next_token:
                break

        if next_token:
            cursor_data = {
                "since_id": since_id,
                "pagination_token": next_token,
                "max_seen_id": max_seen_id,
            }
        else:
            cursor_data = {
                "since_id": max_seen_id or since_id,
                "pagination_token": None,
                "max_seen_id": max_seen_id or since_id,
            }
        return FeedFetchResult(
            tuple(observations),
            None,
            None,
            not_modified=not observations,
            cursor=json.dumps(cursor_data, sort_keys=True),
        )


def _build_youtube_collector(source: SourceConfig) -> RssCollector:
    if source.url:
        return RssCollector(source)
    channel_id = str((source.settings or {}).get("channel_id", "")).strip()
    if not channel_id:
        raise AdapterError(f"youtube source {source.id} requires channel_id")
    rss_source = SourceConfig(
        id=source.id,
        kind="rss",
        publisher=source.publisher,
        section=source.section,
        dedupe_scope=source.dedupe_scope,
        poll_interval_seconds=source.poll_interval_seconds,
        request_timeout_seconds=source.request_timeout_seconds,
        request_attempts=source.request_attempts,
        retry_base_seconds=source.retry_base_seconds,
        max_response_bytes=source.max_response_bytes,
        url=(
            "https://www.youtube.com/feeds/videos.xml?channel_id="
            + urllib.parse.quote(channel_id)
        ),
        allowed_hosts=("www.youtube.com",),
        enabled=True,
        settings=source.settings or {},
        credential_refs=source.credential_refs,
    )
    return RssCollector(rss_source)


_COLLECTOR_FACTORIES = {
    "rss": RssCollector,
    "youtube": _build_youtube_collector,
    "market": MarketCollector,
    "imap": ImapCollector,
    "x": XCollector,
    "host": HostHealthCollector,
}


def build_collector(
    source: SourceConfig,
    *,
    provider_registry: ProviderRegistry = DEFAULT_PROVIDER_REGISTRY,
    factories: Mapping[str, Any] | None = None,
):
    runtime_factories: Mapping[str, Any] = _COLLECTOR_FACTORIES
    if factories:
        runtime_factories = {**_COLLECTOR_FACTORIES, **factories}
    try:
        return provider_registry.build_collector(source, runtime_factories)
    except (ProviderConfigError, ProviderRuntimeError) as exc:
        raise AdapterError(str(exc)) from exc
