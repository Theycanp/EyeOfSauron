from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Mapping, Protocol
from urllib.parse import urlsplit

from .config import NtfyConfig
from .models import OutboxMessage


_TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class NotifyError(RuntimeError):
    pass


class Notifier(Protocol):
    def publish(self, alert: OutboxMessage) -> None:
        ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class NtfyNotifier:
    def __init__(self, base_url: str, token: str, timeout_seconds: int) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise NotifyError("ntfy base URL must be HTTPS without embedded credentials")
        if parsed.query or parsed.fragment:
            raise NotifyError("ntfy base URL cannot contain a query or fragment")
        if not token:
            raise NotifyError("ntfy token is empty")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds
        self._opener = urllib.request.build_opener(_NoRedirect())

    @classmethod
    def from_config(
        cls,
        config: NtfyConfig,
        environment: Mapping[str, str] | None = None,
    ) -> "NtfyNotifier":
        env = os.environ if environment is None else environment
        base_url = env.get(config.base_url_env, "")
        token = env.get(config.token_env, "")
        if not base_url:
            raise NotifyError(f"required environment variable {config.base_url_env} is missing")
        if not token:
            raise NotifyError(f"required environment variable {config.token_env} is missing")
        return cls(base_url, token, config.timeout_seconds)

    def publish(self, alert: OutboxMessage) -> None:
        if not _TOPIC_RE.fullmatch(alert.topic):
            raise NotifyError("outbox contains an invalid ntfy topic")
        payload: dict[str, object] = {
            "topic": alert.topic,
            "title": alert.title,
            "message": alert.message,
            "priority": alert.priority,
            "tags": list(alert.tags),
        }
        if alert.click_url:
            payload["click"] = alert.click_url
        request = urllib.request.Request(
            self.base_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "SignalWatch/0.1",
            },
            method="POST",
        )
        try:
            response = self._opener.open(request, timeout=self.timeout_seconds)
            with response:
                response.read(65537)
                if not 200 <= response.status < 300:
                    raise NotifyError(f"ntfy returned HTTP {response.status}")
        except urllib.error.HTTPError as exc:
            raise NotifyError(f"ntfy returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise NotifyError(f"ntfy request failed: {type(exc).__name__}") from exc
