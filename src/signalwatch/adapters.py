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


class ImapCollector:
    """Incremental IMAP collector using UIDs; disabled unless credentials exist."""

    def __init__(self, config: SourceConfig, environment: Mapping[str, str] | None = None) -> None:
        self.config = config
        settings = config.settings or {}
        self.host = str(settings.get("host", ""))
        self.port = int(settings.get("port", 993))
        self.mailbox = str(settings.get("mailbox", "INBOX"))
        self.search = str(settings.get("search", "ALL"))
        env = os.environ if environment is None else environment
        self.username = env.get(str(settings.get("username_env", "")), "")
        self.password = env.get(str(settings.get("password_env", "")), "")
        if not self.host or not self.username or not self.password:
            raise AdapterError(f"imap source {config.id} is missing host or credentials")

    def fetch(self, state: SourceState) -> FeedFetchResult:
        try:
            client = imaplib.IMAP4_SSL(self.host, self.port)
            client.login(self.username, self.password)
            status, _ = client.select(self.mailbox, readonly=True)
            if status != "OK":
                raise AdapterError("imap mailbox selection failed")
            criteria = self.search if self.search.upper().startswith("UID ") else f"{self.search}"
            status, data = client.uid("SEARCH", None, criteria)
            if status != "OK":
                raise AdapterError("imap search failed")
            uids = [int(item) for item in (data[0] or b"").split() if item.isdigit()]
            last_uid = int(state.cursor or 0)
            uids = [uid for uid in uids if uid > last_uid]
            observations: list[Observation] = []
            for uid in uids[-100:]:
                status, fetched = client.uid("FETCH", str(uid), "(RFC822)")
                if status != "OK":
                    continue
                raw = next((part[1] for part in fetched if isinstance(part, tuple)), None)
                if not isinstance(raw, bytes):
                    continue
                message = email.message_from_bytes(raw, policy=email.policy.default)
                subject = _header(message.get("Subject")) or "(无主题)"
                sender = _header(message.get("From"))
                body = truncate(" ".join(_mail_body(message).split()), 4000)
                date_value = message.get("Date")
                published = datetime.now(UTC)
                if date_value:
                    try:
                        published = email.utils.parsedate_to_datetime(date_value).astimezone(UTC)
                    except (TypeError, ValueError, OverflowError):
                        pass
                external = _header(message.get("Message-ID")) or f"uid:{uid}"
                observations.append(Observation(
                    source_id=self.config.id,
                    publisher=self.config.publisher,
                    dedupe_scope=self.config.dedupe_scope,
                    external_id=external,
                    published_at=published,
                    title=subject,
                    summary=body,
                    url="",
                    attributes={"section": self.config.section, "from": truncate(sender, 500), "uid": uid},
                ))
            try:
                client.logout()
            except OSError:
                pass
            cursor = str(max([last_uid, *uids], default=last_uid))
            return FeedFetchResult(tuple(observations), None, None, not_modified=not observations, cursor=cursor)
        except (imaplib.IMAP4.error, OSError) as exc:
            raise AdapterError("imap request failed") from exc


class XCollector:
    """X API v2 timeline collector; stable numeric user_id is required."""

    def __init__(self, config: SourceConfig, environment: Mapping[str, str] | None = None) -> None:
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
        if parsed.scheme != "https" or not parsed.hostname:
            raise AdapterError("x api_base_url must be HTTPS")
        self._opener = urllib.request.build_opener(_NoRedirect())

    def fetch(self, state: SourceState) -> FeedFetchResult:
        query = {"tweet.fields": "created_at,lang,public_metrics", "max_results": "100"}
        if bool((self.config.settings or {}).get("exclude_replies", True)):
            query["exclude"] = "retweets,replies"
        if state.cursor:
            query["since_id"] = state.cursor
        url = f"{self.base_url}/users/{self.user_id}/tweets?{urllib.parse.urlencode(query)}"
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json", "User-Agent": "SignalWatch/0.2"})
        try:
            with self._opener.open(request, timeout=self.config.request_timeout_seconds) as response:
                payload = response.read(self.config.max_response_bytes + 1)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            raise AdapterError(f"x request failed: {type(exc).__name__}") from exc
        if len(payload) > self.config.max_response_bytes:
            raise AdapterError("x response exceeded configured size limit")
        try:
            data = json.loads(payload)
        except (TypeError, ValueError) as exc:
            raise AdapterError("x API returned invalid JSON") from exc
        rows = data.get("data", []) if isinstance(data, Mapping) else []
        observations: list[Observation] = []
        max_id = int(state.cursor or 0)
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, Mapping) or not row.get("id"):
                continue
            tweet_id = str(row["id"])
            max_id = max(max_id, int(tweet_id) if tweet_id.isdigit() else max_id)
            created = row.get("created_at")
            try:
                published = datetime.fromisoformat(str(created).replace("Z", "+00:00")).astimezone(UTC)
            except (TypeError, ValueError):
                published = datetime.now(UTC)
            text = " ".join(str(row.get("text", "")).split())
            if not text:
                continue
            observations.append(Observation(
                source_id=self.config.id,
                publisher=self.config.publisher,
                dedupe_scope=self.config.dedupe_scope,
                external_id=tweet_id,
                published_at=published,
                title=truncate(text, 1000),
                summary="",
                url=f"https://x.com/i/web/status/{tweet_id}",
                attributes={"section": self.config.section, "user_id": self.user_id, "lang": str(row.get("lang", ""))},
            ))
        return FeedFetchResult(tuple(observations), None, None, not_modified=not observations, cursor=str(max_id) if max_id else state.cursor)


def build_collector(source: SourceConfig):
    if not source.enabled:
        return None
    if source.kind in {"rss", "youtube"}:
        if source.kind == "youtube" and not source.url:
            channel_id = str((source.settings or {}).get("channel_id", "")).strip()
            if not channel_id:
                raise AdapterError(f"youtube source {source.id} requires channel_id")
            source = SourceConfig(
                id=source.id, kind="rss", publisher=source.publisher, section=source.section,
                dedupe_scope=source.dedupe_scope, poll_interval_seconds=source.poll_interval_seconds,
                request_timeout_seconds=source.request_timeout_seconds, request_attempts=source.request_attempts,
                retry_base_seconds=source.retry_base_seconds, max_response_bytes=source.max_response_bytes,
                url=f"https://www.youtube.com/feeds/videos.xml?channel_id={urllib.parse.quote(channel_id)}",
                allowed_hosts=("www.youtube.com",), enabled=True, settings=source.settings or {},
            )
        return RssCollector(source)
    if source.kind == "market":
        return MarketCollector(source)
    if source.kind == "imap":
        return ImapCollector(source)
    if source.kind == "x":
        return XCollector(source)
    if source.kind == "host":
        return HostHealthCollector(source)
    raise AdapterError(f"source kind {source.kind} has no collector")
