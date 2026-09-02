from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class Observation:
    source_id: str
    publisher: str
    dedupe_scope: str
    external_id: str
    published_at: datetime
    title: str
    summary: str
    url: str
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AlertCandidate:
    rule_id: str
    dedupe_key: str
    title: str
    message: str
    priority: int
    tags: tuple[str, ...]
    click_url: str
    topic: str | None = None


@dataclass(frozen=True, slots=True)
class FeedFetchResult:
    observations: tuple[Observation, ...]
    etag: str | None
    last_modified: str | None
    not_modified: bool = False


@dataclass(frozen=True, slots=True)
class SourceState:
    source_id: str
    initialized: bool
    etag: str | None
    last_modified: str | None
    last_attempt_at: int | None
    last_success_at: int | None
    consecutive_failures: int
    outage_alerted: bool
    outage_started_at: int | None


@dataclass(frozen=True, slots=True)
class IngestReport:
    inserted_observations: int
    queued_alerts: int
    baseline_created: bool
    recovery_queued: bool


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    id: int
    topic: str
    title: str
    message: str
    priority: int
    tags: tuple[str, ...]
    click_url: str
    attempts: int
