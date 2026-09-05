from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime, time as datetime_time, timedelta
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_REMINDER_ID_RE = re.compile(r"^rem_[a-f0-9]{16}$")
_DAILY_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_TAG_RE = re.compile(r"^[A-Za-z0-9_+-]{1,64}$")
_MAX_FUTURE_SECONDS = 20 * 366 * 86400


class ReminderError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ReminderSpec:
    id: str
    title: str
    message: str
    schedule_kind: str
    run_at: int | None
    daily_time: str | None
    timezone: str
    enabled: bool
    priority: int
    tags: tuple[str, ...]


def new_reminder_id() -> str:
    return f"rem_{secrets.token_hex(8)}"


def _text(data: Mapping[str, Any], key: str, maximum: int) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ReminderError(f"{key} must be a string")
    value = value.strip()
    if not value:
        raise ReminderError(f"{key} cannot be empty")
    if len(value) > maximum:
        raise ReminderError(f"{key} is too long (maximum {maximum} characters)")
    return value


def _timezone(name: Any) -> str:
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 128:
        raise ReminderError("timezone must be a valid IANA timezone name")
    name = name.strip()
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ReminderError(f"unknown timezone: {name}") from exc
    return name


def parse_reminder(data: Mapping[str, Any], now: int) -> ReminderSpec:
    allowed = {
        "id", "title", "message", "schedule_kind", "run_at", "delay_seconds",
        "daily_time", "timezone", "enabled", "priority", "tags",
    }
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ReminderError(f"unknown reminder fields: {', '.join(unknown)}")

    identifier = data.get("id") or new_reminder_id()
    if not isinstance(identifier, str) or not _REMINDER_ID_RE.fullmatch(identifier):
        raise ReminderError("reminder id is invalid")
    title = _text(data, "title", 128)
    message = _text(data, "message", 4096)
    kind = data.get("schedule_kind")
    if kind not in {"once", "after", "daily"}:
        raise ReminderError("schedule_kind must be once, after, or daily")
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ReminderError("enabled must be boolean")
    priority = data.get("priority", 3)
    if not isinstance(priority, int) or isinstance(priority, bool) or not 1 <= priority <= 5:
        raise ReminderError("priority must be an integer between 1 and 5")
    raw_tags = data.get("tags", ["alarm_clock"])
    if (
        not isinstance(raw_tags, list)
        or not 1 <= len(raw_tags) <= 5
        or not all(isinstance(tag, str) and _TAG_RE.fullmatch(tag) for tag in raw_tags)
    ):
        raise ReminderError("tags must contain one to five valid ntfy tags")
    timezone = _timezone(data.get("timezone", "UTC"))

    run_at: int | None = None
    daily_time: str | None = None
    if kind == "after":
        delay = data.get("delay_seconds")
        if not isinstance(delay, int) or isinstance(delay, bool) or not 1 <= delay <= _MAX_FUTURE_SECONDS:
            raise ReminderError("delay_seconds must be between 1 second and 20 years")
        run_at = now + delay
        kind = "once"
    elif kind == "once":
        value = data.get("run_at")
        if not isinstance(value, int) or isinstance(value, bool):
            raise ReminderError("run_at must be an epoch timestamp in seconds")
        if value <= now:
            raise ReminderError("one-time reminder must be scheduled in the future")
        if value > now + _MAX_FUTURE_SECONDS:
            raise ReminderError("one-time reminder cannot be more than 20 years in the future")
        run_at = value
    else:
        value = data.get("daily_time")
        if not isinstance(value, str) or not _DAILY_TIME_RE.fullmatch(value):
            raise ReminderError("daily_time must use HH:MM in 24-hour time")
        daily_time = value

    return ReminderSpec(
        id=identifier,
        title=title,
        message=message,
        schedule_kind=kind,
        run_at=run_at,
        daily_time=daily_time,
        timezone=timezone,
        enabled=enabled,
        priority=priority,
        tags=tuple(raw_tags),
    )


def _resolved_local(day: date, wall_time: datetime_time, timezone: ZoneInfo) -> datetime:
    """Resolve a local wall time, choosing the first occurrence across DST changes.

    Ambiguous fall-back times use ``fold=0`` and therefore fire once at the first
    occurrence. Nonexistent spring-forward times move to the first valid minute
    after the gap.
    """

    naive = datetime.combine(day, wall_time)
    for offset in range(181):
        candidate_naive = naive + timedelta(minutes=offset)
        candidate = candidate_naive.replace(tzinfo=timezone, fold=0)
        round_trip = candidate.astimezone(UTC).astimezone(timezone).replace(tzinfo=None)
        if round_trip == candidate_naive:
            return candidate
    raise ReminderError("could not resolve daily reminder time in timezone")


def next_daily_occurrence(daily_time: str, timezone_name: str, after: int) -> int:
    if not _DAILY_TIME_RE.fullmatch(daily_time):
        raise ReminderError("daily_time must use HH:MM in 24-hour time")
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ReminderError(f"unknown timezone: {timezone_name}") from exc
    hour, minute = (int(part) for part in daily_time.split(":"))
    wall_time = datetime_time(hour=hour, minute=minute)
    local_after = datetime.fromtimestamp(after, timezone)
    for offset in range(3):
        candidate = _resolved_local(local_after.date() + timedelta(days=offset), wall_time, timezone)
        epoch = int(candidate.timestamp())
        if epoch > after:
            return epoch
    raise ReminderError("could not calculate next daily reminder")
