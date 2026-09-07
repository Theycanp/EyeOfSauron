from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Callable, Mapping, Protocol, runtime_checkable
from urllib.parse import urlsplit

from . import __version__
from .config import NtfyConfig
from .models import OutboxMessage


_TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class NotifyError(RuntimeError):
    """A delivery failure with an explicit retry policy."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        failure_kind: str = "permanent",
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.failure_kind = failure_kind


def delivery_error_details(error: BaseException) -> tuple[bool, str]:
    """Classify notifier failures without coupling the service to one provider."""
    if isinstance(error, NotifyError):
        return error.retryable, error.failure_kind
    # Unknown adapters default to transient so omission cannot drop an alert.
    return True, "transient_unknown"


@runtime_checkable
class Notifier(Protocol):
    def publish(self, alert: OutboxMessage) -> None:
        ...


NotifierFactory = Callable[..., Notifier]


class NotifierRegistry:
    """Explicit adapter registry for delivery channels.

    The service depends on ``Notifier`` only. Registry entries are assembled
    at the composition root, keeping provider-specific authentication and
    configuration out of business logic.
    """

    def __init__(self, factories: Mapping[str, NotifierFactory] | None = None) -> None:
        self._factories: dict[str, NotifierFactory] = dict(factories or {})

    def register(self, kind: str, factory: NotifierFactory) -> None:
        if not kind or not kind.isidentifier():
            raise ValueError("notifier kind is invalid")
        if kind in self._factories:
            raise ValueError(f"notifier kind is already registered: {kind}")
        self._factories[kind] = factory

    def build(self, kind: str, **kwargs: object) -> Notifier:
        factory = self._factories.get(kind)
        if factory is None:
            raise NotifyError(f"unsupported notifier kind: {kind}", failure_kind="invalid_config")
        return factory(**kwargs)

    def kinds(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))


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
            raise NotifyError(
                "outbox contains an invalid ntfy topic", failure_kind="invalid_payload"
            )
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
                "User-Agent": f"Argus/{__version__}",
            },
            method="POST",
        )
        try:
            response = self._opener.open(request, timeout=self.timeout_seconds)
            with response:
                response.read(65537)
                if not 200 <= response.status < 300:
                    retryable = response.status in {408, 425, 429} or response.status >= 500
                    raise NotifyError(
                        f"ntfy returned HTTP {response.status}",
                        retryable=retryable,
                        failure_kind=(
                            "remote_transient" if retryable else "remote_rejected"
                        ),
                    )
        except urllib.error.HTTPError as exc:
            retryable = exc.code in {408, 425, 429} or exc.code >= 500
            raise NotifyError(
                f"ntfy returned HTTP {exc.code}",
                retryable=retryable,
                failure_kind="remote_transient" if retryable else "remote_rejected",
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise NotifyError(
                f"ntfy request failed: {type(exc).__name__}",
                retryable=True,
                failure_kind="transport",
            ) from exc


DEFAULT_NOTIFIER_REGISTRY = NotifierRegistry({"ntfy": NtfyNotifier.from_config})
