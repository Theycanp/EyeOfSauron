from __future__ import annotations

import re
from datetime import UTC, datetime


_URL_QUERY = re.compile(r"(https?://[^\s?#]+)\?[^\s]+", re.IGNORECASE)
_URL_USERINFO = re.compile(r"(https?://)[^\s/@]+@", re.IGNORECASE)
_AUTHORIZATION = re.compile(r"(?i)\b(?:bearer|basic)\s+[^\s,;\"']+")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(token|password|passwd|secret|authorization|api[_-]?key)\s*[:=]\s*[^\s,;]+"
)


def now_epoch() -> int:
    return int(datetime.now(UTC).timestamp())


def to_epoch(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.astimezone(UTC).timestamp())


def sanitize_error(error: BaseException | str, limit: int = 500) -> str:
    text = str(error).replace("\n", " ").replace("\r", " ")
    text = _URL_USERINFO.sub(r"\1<redacted>@", text)
    text = _AUTHORIZATION.sub("<redacted>", text)
    text = _URL_QUERY.sub(r"\1?<redacted>", text)
    text = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    if isinstance(error, BaseException):
        text = f"{type(error).__name__}: {text}"
    return text[:limit]


def truncate(text: str, limit: int) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)].rstrip() + "…"
