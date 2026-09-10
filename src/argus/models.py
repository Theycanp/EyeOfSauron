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
    importance: int = 3
    urgency: int = 2
    relevance: int = 3
    confidence: float = 0.5
    region: str = "GLOBAL"
    topic: str = "general"
    source_tier: str = "secondary"
    information_type: str = "report"
    handling: str = "digest"
    processing_state: str = "new"


@dataclass(frozen=True, slots=True)
class AnalysisWorkItem:
    """A persisted observation leased to the analysis application service."""

    observation_id: int
    observation: Observation
    lease_token: str
    fetched_at: int
    attempts: int = 0


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
    confidence: float = 0.5
    evidence: tuple[str, ...] = ()
    incident_key: str | None = None
    incident_kind: str = "event"
    recovery: bool = False


@dataclass(frozen=True, slots=True)
class FeedFetchResult:
    observations: tuple[Observation, ...]
    etag: str | None
    last_modified: str | None
    not_modified: bool = False
    cursor: str | None = None
    warnings: tuple[str, ...] = ()


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
    cursor: str | None = None


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
    observation_id: int | None = None
    incident_id: int | None = None
    created_at: int = 0
    confidence: float = 0.5
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Incident:
    id: int
    incident_key: str
    kind: str
    status: str
    first_seen_at: int
    last_seen_at: int
    recovered_at: int | None
    confidence: float
    evidence: tuple[str, ...]
    source_ids: tuple[str, ...]
    observation_count: int
