"""Validated domain input for administrator-created events."""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from typing import Any, Mapping


_REGION_RE = re.compile(r"^[A-Z][A-Z0-9_-]{1,15}$")
_TOPIC_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")


class ManualEventError(ValueError):
    """The requested manual event cannot be accepted."""


@dataclass(frozen=True, slots=True)
class ManualEventSpec:
    title: str
    summary: str
    importance: int
    region: str
    topic: str
    source_url: str = ""


def _text(value: Any, name: str, *, maximum: int, multiline: bool = False) -> str:
    if not isinstance(value, str):
        raise ManualEventError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ManualEventError(f"{name} is required")
    if len(normalized) > maximum:
        raise ManualEventError(f"{name} must contain at most {maximum} characters")
    if any(ord(char) < 32 and (not multiline or char not in "\n\t") for char in normalized):
        raise ManualEventError(f"{name} contains unsupported control characters")
    return normalized


def _source_url(value: Any) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise ManualEventError("source_url must be a string")
    normalized = value.strip()
    if len(normalized) > 2048:
        raise ManualEventError("source_url must contain at most 2048 characters")
    if any(ord(char) < 32 or char.isspace() for char in normalized):
        raise ManualEventError("source_url contains unsupported whitespace")
    try:
        parsed = urllib.parse.urlsplit(normalized)
        _ = parsed.port
    except ValueError as exc:
        raise ManualEventError("source_url is invalid") from exc
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ManualEventError("source_url must be an absolute HTTPS URL")
    if parsed.username or parsed.password:
        raise ManualEventError("source_url must not contain credentials")
    if parsed.fragment:
        raise ManualEventError("source_url must not contain a fragment")
    return normalized


def parse_manual_event(data: Mapping[str, Any]) -> ManualEventSpec:
    """Normalize an untrusted administration request into a domain value."""
    importance = data.get("importance", 3)
    if not isinstance(importance, int) or isinstance(importance, bool) or not 1 <= importance <= 5:
        raise ManualEventError("importance must be an integer from 1 to 5")
    region = _text(data.get("region", "GLOBAL"), "region", maximum=16).upper()
    topic = _text(data.get("topic", "general"), "topic", maximum=32).lower()
    if not _REGION_RE.fullmatch(region):
        raise ManualEventError("region must be a 2-16 character code")
    if not _TOPIC_RE.fullmatch(topic):
        raise ManualEventError("topic must be a 2-32 character identifier")
    return ManualEventSpec(
        title=_text(data.get("title"), "title", maximum=180),
        summary=_text(data.get("summary"), "summary", maximum=5000, multiline=True),
        importance=importance,
        region=region,
        topic=topic,
        source_url=_source_url(data.get("source_url", "")),
    )
