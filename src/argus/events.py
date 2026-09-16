"""Event-centric domain models and persistence ports.

Observations are immutable collection records.  These models describe the
stable event projection built from one or more observations without coupling
the clustering algorithm to SQLite.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence, runtime_checkable


EVENT_STATUSES = frozenset({"active", "quiet", "closed"})
REPORT_TIERS = frozenset({"primary", "secondary", "social"})
REPORT_RELATIONS = frozenset(
    {"primary", "corroborates", "updates", "contradicts", "context", "social"}
)
CLAIM_STATUSES = frozenset({"active", "superseded", "disputed"})


@dataclass(frozen=True, slots=True)
class PersistedEvent:
    event_key: str
    fingerprint: str
    title: str
    summary: str
    score: float
    importance: int
    urgency: int
    relevance: int
    confidence: float
    first_seen_at: int
    last_seen_at: int
    regions: tuple[str, ...] = ()
    topics: tuple[str, ...] = ()
    status: str = "active"
    independent_source_count: int = 0
    created_at: int = 0
    updated_at: int = 0

    def __post_init__(self) -> None:
        if not self.event_key or len(self.event_key) > 160:
            raise ValueError("event key is invalid")
        if not self.fingerprint or len(self.fingerprint) > 256:
            raise ValueError("event fingerprint is invalid")
        if not self.title.strip() or len(self.title) > 500:
            raise ValueError("event title is invalid")
        if len(self.summary) > 10000:
            raise ValueError("event summary is too long")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("event confidence is out of range")
        if any(value < 1 or value > 5 for value in (self.importance, self.urgency, self.relevance)):
            raise ValueError("event score dimension is out of range")
        if self.first_seen_at < 0 or self.last_seen_at < self.first_seen_at:
            raise ValueError("event timestamps are invalid")
        if self.status not in EVENT_STATUSES:
            raise ValueError("event status is invalid")
        if self.independent_source_count < 0:
            raise ValueError("event source count cannot be negative")
        if self.created_at < 0 or self.updated_at < 0:
            raise ValueError("event audit timestamps are invalid")


@dataclass(frozen=True, slots=True)
class PersistedEventReport:
    event_key: str
    observation_id: int
    source_id: str
    source_tier: str
    relation: str
    match_score: float
    is_representative: bool
    published_at: int
    title: str
    summary: str
    url: str
    report_id: int | None = None
    created_at: int = 0

    def __post_init__(self) -> None:
        if not self.event_key or not self.source_id or self.observation_id < 1:
            raise ValueError("event report identity is invalid")
        if self.source_tier not in REPORT_TIERS:
            raise ValueError("event report source tier is invalid")
        if self.relation not in REPORT_RELATIONS:
            raise ValueError("event report relation is invalid")
        if not 0.0 <= self.match_score <= 1.0:
            raise ValueError("event report match score is out of range")
        if self.published_at < 0 or self.created_at < 0:
            raise ValueError("event report timestamps are invalid")
        if self.report_id is not None and self.report_id < 1:
            raise ValueError("event report ID is invalid")


@dataclass(frozen=True, slots=True)
class PersistedEventClaim:
    event_key: str
    claim_key: str
    text: str
    status: str
    confidence: float
    first_seen_at: int
    last_seen_at: int
    supersedes_claim_key: str | None = None
    claim_id: int | None = None
    created_at: int = 0
    updated_at: int = 0

    def __post_init__(self) -> None:
        if not self.event_key or not self.claim_key or len(self.claim_key) > 160:
            raise ValueError("event claim identity is invalid")
        if not self.text.strip() or len(self.text) > 4000:
            raise ValueError("event claim text is invalid")
        if self.status not in CLAIM_STATUSES:
            raise ValueError("event claim status is invalid")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("event claim confidence is out of range")
        if self.first_seen_at < 0 or self.last_seen_at < self.first_seen_at:
            raise ValueError("event claim timestamps are invalid")
        if self.created_at < 0 or self.updated_at < 0:
            raise ValueError("event claim audit timestamps are invalid")


@dataclass(frozen=True, slots=True)
class PersistedEventClaimEvidence:
    claim_key: str
    report_id: int
    stance: str
    note: str = ""
    created_at: int = 0
    evidence_id: int | None = None

    def __post_init__(self) -> None:
        if not self.claim_key or self.report_id < 1:
            raise ValueError("claim evidence identity is invalid")
        if self.stance not in {"supports", "refutes", "context"}:
            raise ValueError("claim evidence stance is invalid")
        if len(self.note) > 2000 or self.created_at < 0:
            raise ValueError("claim evidence is invalid")


@dataclass(frozen=True, slots=True)
class PersistedEventTimelineItem:
    event_key: str
    occurred_at: int
    kind: str
    text: str
    confidence: float
    report_id: int | None = None
    timeline_id: int | None = None
    created_at: int = 0

    def __post_init__(self) -> None:
        if not self.event_key or not self.kind or len(self.kind) > 80:
            raise ValueError("event timeline identity is invalid")
        if not self.text.strip() or len(self.text) > 4000:
            raise ValueError("event timeline text is invalid")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("event timeline confidence is out of range")
        if self.occurred_at < 0 or self.created_at < 0:
            raise ValueError("event timeline timestamps are invalid")
        if self.report_id is not None and self.report_id < 1:
            raise ValueError("event timeline report ID is invalid")


# Backward-compatible shorthand used by the repository protocol and API
# adapters.  The persisted aggregate remains named explicitly above.
Event = PersistedEvent


@runtime_checkable
class EventRepository(Protocol):
    """Storage port for event projections and their evidence graph."""

    def save_event(self, event: PersistedEvent) -> PersistedEvent: ...

    def get_event(self, event_key: str) -> PersistedEvent | None: ...

    def list_events(
        self,
        *,
        since: int | None = None,
        until: int | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[PersistedEvent]: ...

    def save_event_report(self, report: PersistedEventReport) -> PersistedEventReport: ...

    def list_event_reports(self, event_key: str, *, limit: int = 500) -> list[PersistedEventReport]: ...

    def save_event_claim(self, claim: PersistedEventClaim) -> PersistedEventClaim: ...

    def list_event_claims(self, event_key: str, *, limit: int = 500) -> list[PersistedEventClaim]: ...

    def save_claim_evidence(self, evidence: PersistedEventClaimEvidence) -> PersistedEventClaimEvidence: ...

    def list_claim_evidence(self, claim_key: str, *, limit: int = 500) -> list[PersistedEventClaimEvidence]: ...

    def save_event_timeline(self, item: PersistedEventTimelineItem) -> PersistedEventTimelineItem: ...

    def list_event_timeline(self, event_key: str, *, limit: int = 500) -> list[PersistedEventTimelineItem]: ...
