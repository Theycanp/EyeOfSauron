"""Event-centric domain models and persistence ports.

Observations are immutable collection records.  These models describe the
stable event projection built from one or more observations without coupling
the clustering algorithm to SQLite.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, Sequence, runtime_checkable


EVENT_STATUSES = frozenset({"active", "quiet", "closed"})
REPORT_TIERS = frozenset({"primary", "secondary", "social"})
REPORT_RELATIONS = frozenset(
    {"primary", "corroborates", "updates", "contradicts", "context", "social"}
)
EVENT_QUIET_SECONDS = 28 * 3600
EVENT_CLOSED_SECONDS = 7 * 86400


def event_lifecycle(last_seen_at: int, now: int) -> str:
    """Publication time determines activity; historical backfills never look live."""
    age = max(0, now - last_seen_at)
    return "closed" if age >= EVENT_CLOSED_SECONDS else "quiet" if age >= EVENT_QUIET_SECONDS else "active"
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
    publisher: str
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


@dataclass(frozen=True, slots=True)
class EventListItem:
    """Bounded read model shared by the event browser and digest selector."""

    event: PersistedEvent
    reports: tuple[PersistedEventReport, ...]
    report_count: int
    handling: str = "digest"

    def __post_init__(self) -> None:
        if self.report_count < len(self.reports):
            raise ValueError("event report count is invalid")
        if self.handling not in {"digest", "immediate"}:
            raise ValueError("event handling is invalid")

    @property
    def reports_truncated(self) -> bool:
        return self.report_count > len(self.reports)


@dataclass(frozen=True, slots=True)
class EventPage:
    items: tuple[EventListItem, ...]
    next_cursor: str | None
    window_since: int | None = None
    window_until: int | None = None


@runtime_checkable
class EventPageRepository(Protocol):
    """Read-only event browser and digest selection port."""

    def list_event_page(
        self,
        *,
        since: int,
        until: int,
        sort: str = "latest",
        limit: int = 30,
        cursor: str | None = None,
        reports_per_event: int = 20,
    ) -> EventPage: ...


@runtime_checkable
class EventPoolRepository(EventPageRepository, Protocol):
    """Bounded work port used by the continuous event projector."""

    def save_event_projection(
        self, event: PersistedEvent, report: PersistedEventReport,
        *, relation_updates: Sequence[PersistedEventReport] = (),
    ) -> PersistedEventReport:
        """Atomically attach evidence and revise earlier report relationships."""
        ...

    def list_unassigned_event_observations(
        self, *, since: int | None = None, until: int | None = None, limit: int = 500
    ) -> list[dict[str, Any]]: ...

    def list_event_candidates(
        self, since: int, until: int, *, limit: int = 200
    ) -> list[PersistedEvent]: ...

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

    def list_claim_evidence(
        self, claim_key: str, *, limit: int = 500, event_key: str | None = None
    ) -> list[PersistedEventClaimEvidence]: ...

    def save_event_timeline(self, item: PersistedEventTimelineItem) -> PersistedEventTimelineItem: ...

    def list_event_timeline(self, event_key: str, *, limit: int = 500) -> list[PersistedEventTimelineItem]: ...


@runtime_checkable
class EventRepairRepository(Protocol):
    """Explicit, audited repair port; never used automatically by a collector."""

    def merge_semantic_event_group(
        self, event_keys: Sequence[str], *, expected_observation_ids: Sequence[int],
        actor: str, now: int, primary_source_ids: Sequence[str] = (),
    ) -> str: ...


class EventWorkspaceConflict(ValueError):
    """The evidence changed after the operator's preview."""


@runtime_checkable
class EventEditorialRepository(Protocol):
    def get_event_digest_choices(self, event_keys: Sequence[str]) -> dict[str, str]: ...


@runtime_checkable
class EventWorkspaceRepository(EventEditorialRepository, Protocol):
    """Review commands are separate from collection and immutable digest storage."""

    def maintain_event_lifecycle(self, *, now: int, limit: int = 500) -> int: ...

    def event_workspace_state(self, event_keys: Sequence[str], actor: str) -> dict[str, Any]: ...

    def set_event_preference(self, event_key: str, actor: str, *, read: bool,
                             followed: bool, ignored: bool, now: int) -> None: ...

    def set_event_digest_choice(self, event_key: str, choice: str, *, actor: str,
                                reason: str, now: int) -> None: ...

    def preview_event_repair(self, action: str, event_keys: Sequence[str], *,
                             observation_ids: Sequence[int] = ()) -> dict[str, Any]: ...

    def apply_event_repair(self, action: str, event_keys: Sequence[str], *,
                           observation_ids: Sequence[int], expected_revision: str,
                           actor: str, reason: str, now: int) -> str: ...

    def list_event_audit(self, event_key: str, *, limit: int = 50) -> list[dict[str, Any]]: ...

    def canonical_event_key(self, event_key: str) -> str: ...
