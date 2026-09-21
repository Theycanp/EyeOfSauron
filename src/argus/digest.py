"""Deterministic daily digest construction.

The module owns digest policy and clustering, but no storage or HTTP details.
Persistence implementations consume and return these immutable domain models.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .config import DigestConfig
from .event_clustering import cluster_events, event_match_score
from .event_pool import EventPoolProjector
from .events import EventPoolRepository, EventRepository, PersistedEvent, PersistedEventReport
from .reminders import next_daily_occurrence
from .regions import effective_region_weights, region_weight
from .util import sanitize_error


_WORD_RE = re.compile(r"[a-z0-9]+|[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_SPACE_RE = re.compile(r"\s+")
_COVERAGE_STATUSES = frozenset(
    {"covered", "quiet", "degraded", "stale", "disabled", "unknown"}
)
DIGEST_RETRY_INTERVAL_SECONDS = 3600
DIGEST_RETRY_WINDOW_SECONDS = 5 * 3600


@dataclass(frozen=True, slots=True)
class DigestCluster:
    cluster_key: str
    title: str
    summary: str
    score: float
    importance: int
    urgency: int
    relevance: int
    confidence: float
    published_at: int
    regions: tuple[str, ...]
    topics: tuple[str, ...]
    source_ids: tuple[str, ...]
    observation_ids: tuple[int, ...]
    links: tuple[str, ...]
    # Aligned with source_ids; keeps primary/secondary/social reports visible
    # as parallel evidence under one event cluster.
    source_tiers: tuple[str, ...] = ()
    handling: str = "digest"
    event_id: str | None = None
    reports: tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        if not self.cluster_key or len(self.cluster_key) > 128:
            raise ValueError("digest cluster key is invalid")
        if not self.title.strip() or len(self.title) > 500:
            raise ValueError("digest cluster title is invalid")
        if len(self.summary) > 4000 or not math.isfinite(self.score):
            raise ValueError("digest cluster summary or score is invalid")
        if any(value < 1 or value > 5 for value in (self.importance, self.urgency, self.relevance)):
            raise ValueError("digest cluster score dimension is out of range")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("digest cluster confidence is out of range")
        if self.published_at < 0 or any(identifier < 1 for identifier in self.observation_ids):
            raise ValueError("digest cluster observation identity is invalid")
        if self.source_tiers and len(self.source_tiers) != len(self.source_ids):
            raise ValueError("digest cluster source tier mapping is invalid")
        if any(tier not in {"primary", "secondary", "social"} for tier in self.source_tiers):
            raise ValueError("digest cluster source tier is invalid")
        if self.handling not in {"digest", "immediate"}:
            raise ValueError("digest cluster handling is invalid")


@dataclass(frozen=True, slots=True)
class SourceCoverage:
    source_id: str
    status: str
    observation_count: int
    last_attempt_at: int | None = None
    last_success_at: int | None = None
    consecutive_failures: int = 0

    def __post_init__(self) -> None:
        if self.status not in _COVERAGE_STATUSES:
            raise ValueError(f"unsupported source coverage status: {self.status}")
        if not self.source_id or len(self.source_id) > 128:
            raise ValueError("source coverage ID is invalid")
        if self.observation_count < 0 or self.consecutive_failures < 0:
            raise ValueError("source coverage counters cannot be negative")


@dataclass(frozen=True, slots=True)
class DigestDocument:
    digest_key: str
    version: int
    period_start: int
    period_end: int
    timezone: str
    title: str
    summary: str
    generation_kind: str
    items: tuple[DigestCluster, ...]
    coverage: tuple[SourceCoverage, ...]
    created_at: int
    status: str = "draft"
    published_at: int | None = None

    def __post_init__(self) -> None:
        if not self.digest_key or len(self.digest_key) > 128:
            raise ValueError("digest key is invalid")
        if self.version < 0:
            raise ValueError("digest version cannot be negative")
        if self.period_start < 0 or self.period_end <= self.period_start:
            raise ValueError("digest period is invalid")
        if not self.timezone or len(self.timezone) > 64:
            raise ValueError("digest timezone is invalid")
        if not self.title.strip() or len(self.title) > 300 or len(self.summary) > 20000:
            raise ValueError("digest title or summary is invalid")
        if len(self.items) > 500 or len(self.coverage) > 1000:
            raise ValueError("digest content limit is exceeded")
        if self.created_at < 0 or (self.published_at is not None and self.published_at < 0):
            raise ValueError("digest timestamp is invalid")
        if self.status not in {"draft", "published", "superseded"}:
            raise ValueError("digest status is invalid")
        if self.generation_kind not in {"algorithm", "api"}:
            raise ValueError("digest generation kind is invalid")
        if self.status == "published" and self.published_at is None:
            raise ValueError("published digest requires a publication timestamp")


@runtime_checkable
class DigestInputRepository(Protocol):
    """Read port needed by the digest builder."""

    def list_digest_observations(
        self, since: int, until: int, *, source_ids: Sequence[str] | None = None,
        limit: int = 5000,
    ) -> list[dict[str, Any]]: ...

    def list_observations(
        self,
        since: int,
        until: int,
        *,
        handling: str | None = None,
        region: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]: ...

    def list_source_coverage(
        self,
        since: int,
        until: int,
        *,
        source_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]: ...

    def list_source_quality(
        self,
        *,
        source_ids: Sequence[str] | None = None,
        now: int | None = None,
    ) -> list[dict[str, Any]]: ...


@runtime_checkable
class DigestReaderRepository(Protocol):
    """Read-only persistence port used by digest readers and HTTP views."""

    def get_digest(
        self,
        digest_key: str,
        version: int | None = None,
        *,
        published_only: bool = False,
    ) -> DigestDocument | None: ...

    def list_digests(
        self, *, status: str | None = None, limit: int = 30
    ) -> list[DigestDocument]: ...


@runtime_checkable
class DigestRepository(DigestReaderRepository, Protocol):
    """Runtime persistence port for immutable, versioned digest documents."""

    def save_digest(self, digest: DigestDocument) -> DigestDocument: ...

    def publish_digest(self, digest_key: str, version: int, now: int) -> DigestDocument: ...


@runtime_checkable
class DigestNotificationRepository(Protocol):
    def enqueue_digest_notification(
        self,
        digest: DigestDocument,
        *,
        topic: str,
        click_url: str,
        now: int,
    ) -> bool: ...

    def enqueue_digest_failure_notification(
        self, *, digest_key: str, streak: int, error: str, topic: str, click_url: str, now: int
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class DigestRetryState:
    digest_key: str
    fallback_version: int
    attempts: int
    next_attempt_at: int | None
    retry_deadline_at: int
    status: str
    last_error: str | None = None


class DigestRetryRepository(Protocol):
    def start_digest_retry(
        self, digest_key: str, fallback_version: int, now: int, retry_deadline_at: int,
        *, last_error: str | None = None,
    ) -> DigestRetryState: ...

    def get_digest_retry(self, digest_key: str) -> DigestRetryState | None: ...

    def list_expired_digest_retries(self, now: int) -> list[DigestRetryState]: ...

    def record_digest_retry(
        self,
        digest_key: str,
        *,
        attempts: int,
        status: str,
        next_attempt_at: int | None,
        last_error: str | None,
        now: int,
    ) -> DigestRetryState: ...

    def record_digest_failure(self, digest_key: str, *, now: int) -> tuple[int, bool]: ...

    def record_digest_success(self, *, now: int) -> None: ...


class DigestSummarizer(Protocol):
    def summarize(self, digest: DigestDocument) -> str: ...


@runtime_checkable
class DigestAnalysisRepository(Protocol):
    def reserve_analysis_api_call(self, budget_day: str, *, limit: int, now: int) -> bool: ...
    def reserve_digest_api_call(self, digest_key: str, *, limit: int, now: int) -> bool: ...
    def get_analysis_text(self, observation_id: int, *, max_chars: int) -> str: ...


def _normalized_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = "".join(character if character.isalnum() else " " for character in normalized)
    return _SPACE_RE.sub(" ", normalized).strip()


def _title_tokens(value: str) -> frozenset[str]:
    normalized = _normalized_title(value)
    raw = _WORD_RE.findall(normalized)
    tokens = set(raw)
    # Adjacent CJK characters carry much more meaning than individual glyphs.
    cjk = "".join(token for token in raw if len(token) == 1 and ord(token) >= 0x3400)
    tokens.update(cjk[index : index + 2] for index in range(max(0, len(cjk) - 1)))
    return frozenset(token for token in tokens if token)


def _similarity(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    left_title = _normalized_title(str(left.get("title", "")))
    right_title = _normalized_title(str(right.get("title", "")))
    if not left_title or not right_title:
        return 0.0
    if left_title == right_title:
        return 1.0
    left_tokens = _title_tokens(left_title)
    right_tokens = _title_tokens(right_title)
    union = left_tokens | right_tokens
    jaccard = len(left_tokens & right_tokens) / len(union) if union else 0.0
    sequence = SequenceMatcher(None, left_title, right_title, autojunk=False).ratio()
    return max(jaccard, sequence * 0.9)


def _observation_score(
    item: Mapping[str, Any],
    region_weights: Mapping[str, int],
    source_quality_weights: Mapping[str, float] | None = None,
) -> float:
    importance = _bounded_int(item.get("importance"), 3)
    urgency = _bounded_int(item.get("urgency"), 2)
    relevance = _bounded_int(item.get("relevance"), 3)
    confidence = _bounded_float(item.get("confidence"), 0.5)
    region = str(item.get("region", "GLOBAL")).upper()
    regional_interest = _bounded_int(region_weight(region_weights, region), 3)
    source_bonus = 0.35 if item.get("source_tier") == "primary" else 0.0
    base = (
        importance * 0.35
        + urgency * 0.15
        + relevance * 0.25
        + confidence * 0.5
        + regional_interest * 0.15
        + source_bonus
    )
    quality = 1.0
    if source_quality_weights is not None:
        raw_quality = source_quality_weights.get(str(item.get("source_id", "")), 1.0)
        if isinstance(raw_quality, (int, float)) and not isinstance(raw_quality, bool):
            quality = max(0.70, min(1.15, float(raw_quality)))
    return round(base * quality, 4)


def _bounded_int(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(1, min(5, value))


def _bounded_float(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return default
    return max(0.0, min(1.0, float(value)))


def _cluster_key(observation_ids: Sequence[int], title: str) -> str:
    identity = ",".join(str(identifier) for identifier in sorted(observation_ids))
    if not identity:
        identity = _normalized_title(title)
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def adaptive_digest_item_count(
    clusters: Sequence[DigestCluster],
    *,
    max_items: int = 50,
) -> int:
    """Choose a deterministic daily item count from the ranked cluster scores.

    ``max_items`` is a safety ceiling, not the normal publication size.  A
    cluster contributes one slot for roughly four points of weighted signal;
    lower-signal days therefore produce shorter digests while a dense day can
    use most of the configured ceiling.  Immediate clusters are never removed
    merely because the day is otherwise quiet (subject to the hard ceiling).
    """
    if not 1 <= max_items <= 500:
        raise ValueError("digest item limit is out of range")
    if not clusters:
        return 0
    # Scores are already a bounded combination of durable triage dimensions,
    # confidence, regional interest and corroboration.  Clamp malformed or
    # future-extensible values before they can influence the publication size.
    signal_mass = sum(max(1.0, min(6.0, float(cluster.score))) for cluster in clusters)
    score_target = math.ceil(signal_mass / 4.0)
    immediate_count = sum(cluster.handling == "immediate" for cluster in clusters)
    return min(max_items, max(1, score_target, immediate_count))


def cluster_observations(
    observations: Sequence[Mapping[str, Any]],
    *,
    region_weights: Mapping[str, int] | None = None,
    similarity_threshold: float = 0.62,
    max_items: int = 50,
    source_quality_weights: Mapping[str, float] | None = None,
    adaptive: bool = False,
) -> tuple[DigestCluster, ...]:
    """Cluster related reports and return ranked, deterministic digest items."""
    if not 0.5 <= similarity_threshold <= 1.0:
        raise ValueError("digest similarity threshold is out of range")
    if not 1 <= max_items <= 500:
        raise ValueError("digest item limit is out of range")
    weights = effective_region_weights(region_weights)
    ordered = sorted(
        observations,
        key=lambda item: (
            -_observation_score(item, weights, source_quality_weights),
            -int(item.get("published_at", 0)),
            int(item.get("id", 0)),
        ),
    )
    groups: list[list[Mapping[str, Any]]] = []
    for item in ordered:
        best_index: int | None = None
        best_similarity = 0.0
        for index, group in enumerate(groups):
            representative = group[0]
            left_topic = str(item.get("topic", "general"))
            right_topic = str(representative.get("topic", "general"))
            if left_topic != right_topic and "general" not in {left_topic, right_topic}:
                continue
            similarity = _similarity(item, representative)
            if similarity >= similarity_threshold and similarity > best_similarity:
                best_index = index
                best_similarity = similarity
        if best_index is None:
            groups.append([item])
        else:
            groups[best_index].append(item)

    clusters: list[DigestCluster] = []
    for group in groups:
        representative = group[0]
        source_ids = tuple(
            sorted({str(item.get("source_id", "")) for item in group if item.get("source_id")})
        )
        source_tiers = tuple(
            next(
                (str(item.get("source_tier", "secondary")) for item in group
                 if str(item.get("source_id", "")) == source_id),
                "secondary",
            )
            for source_id in source_ids
        )
        links_by_source: dict[str, str] = {}
        for item in group:
            source_id = str(item.get("source_id", ""))
            url = str(item.get("url", ""))
            if source_id and url:
                links_by_source.setdefault(source_id, url)
        observation_ids = tuple(
            sorted({int(item["id"]) for item in group if item.get("id") is not None})
        )
        corroboration = min(0.75, 0.25 * math.log2(max(1, len(source_ids))))
        score = round(
            max(_observation_score(item, weights, source_quality_weights) for item in group)
            + corroboration,
            4,
        )
        summaries = [str(item.get("summary", "")).strip() for item in group]
        summary = next(
            (value for value in summaries if value), str(representative.get("title", ""))
        )
        clusters.append(
            DigestCluster(
                cluster_key=_cluster_key(observation_ids, str(representative.get("title", ""))),
                title=str(representative.get("title", "")).strip()[:500],
                summary=summary[:4000],
                score=score,
                importance=max(_bounded_int(item.get("importance"), 3) for item in group),
                urgency=max(_bounded_int(item.get("urgency"), 2) for item in group),
                relevance=max(_bounded_int(item.get("relevance"), 3) for item in group),
                confidence=max(_bounded_float(item.get("confidence"), 0.5) for item in group),
                published_at=max(int(item.get("published_at", 0)) for item in group),
                regions=tuple(
                    sorted({str(item.get("region", "GLOBAL")).upper() for item in group})
                ),
                topics=tuple(sorted({str(item.get("topic", "general")) for item in group})),
                source_ids=source_ids,
                observation_ids=observation_ids,
                links=tuple(
                    links_by_source[source_id]
                    for source_id in source_ids
                    if source_id in links_by_source
                ),
                source_tiers=source_tiers,
                handling=(
                    "immediate"
                    if any(str(item.get("handling", "digest")) == "immediate" for item in group)
                    else "digest"
                ),
            )
        )
    selection_limit = adaptive_digest_item_count(clusters, max_items=max_items) if adaptive else max_items
    # Select immediate reports first, then reduce repeated-source dominance.
    # This is a soft diversity penalty, not a quota that hides an urgent event.
    selected: list[DigestCluster] = []
    counts: dict[str, int] = {}
    while clusters and len(selected) < selection_limit:
        clusters.sort(key=lambda item: (
            item.handling != "immediate",
            -(item.score / (1 + 0.35 * min((counts.get(s, 0) for s in item.source_ids), default=0))),
            -item.published_at, item.cluster_key,
        ))
        chosen = clusters.pop(0)
        selected.append(chosen)
        for source_id in chosen.source_ids:
            counts[source_id] = counts.get(source_id, 0) + 1
    return tuple(selected)


def cluster_observations_event_centric(
    observations: Sequence[Mapping[str, Any]],
    *,
    region_weights: Mapping[str, int] | None = None,
    similarity_threshold: float = 0.62,
    max_items: int = 50,
    source_quality_weights: Mapping[str, float] | None = None,
    adaptive: bool = False,
) -> tuple[DigestCluster, ...]:
    """Build legacy digest items from the event-centric clustering domain.

    The adapter intentionally leaves persistence and HTTP contracts unchanged:
    callers receive ``DigestCluster`` values, while the event ID becomes the
    stable ``cluster_key`` and report tiers remain aligned with source IDs.
    New readers can call :func:`argus.event_clustering.cluster_events` to see
    the full report relationships.
    """
    if not 0.5 <= similarity_threshold <= 1.0:
        raise ValueError("digest similarity threshold is out of range")
    if not 1 <= max_items <= 500:
        raise ValueError("digest item limit is out of range")
    weights = effective_region_weights(region_weights)
    events = cluster_events(observations, similarity_threshold=similarity_threshold)
    clusters: list[DigestCluster] = []
    for event in events:
        reports_by_source: dict[str, Any] = {}
        for report in event.reports:
            # Prefer the highest-scored/latest report as the source's link.
            current = reports_by_source.get(report.source_id)
            if current is None or (report.score, report.published_at, report.report_id) > (current.score, current.published_at, current.report_id):
                reports_by_source[report.source_id] = report
        source_ids = tuple(sorted(reports_by_source))
        representative_reports = tuple(reports_by_source[source_id] for source_id in source_ids)
        quality = max(
            (
                max(0.70, min(1.15, float(source_quality_weights.get(source_id, 1.0))))
                for source_id in source_ids
                if source_quality_weights is not None and isinstance(source_quality_weights.get(source_id, 1.0), (int, float))
            ),
            default=1.0,
        )
        regional_interest = max((region_weight(weights, region) for region in event.regions), default=3)
        primary_bonus = 0.35 if any(report.source_tier == "primary" for report in event.reports) else 0.0
        score = round(
            event.score * quality
            + max(0, regional_interest - 3) * 0.15
            + primary_bonus,
            4,
        )
        clusters.append(
            DigestCluster(
                cluster_key=event.event_id,
                title=event.title,
                summary=event.summary,
                score=score,
                importance=event.importance,
                urgency=event.urgency,
                relevance=event.relevance,
                confidence=event.confidence,
                published_at=event.published_at,
                regions=event.regions,
                topics=event.topics,
                source_ids=source_ids,
                observation_ids=tuple(sorted(report.observation_id for report in event.reports if report.observation_id is not None)),
                links=tuple(report.url for report in representative_reports if report.url),
                source_tiers=tuple(report.source_tier for report in representative_reports),
                handling=("immediate" if any(str(item.get("handling", "digest")) == "immediate" for item in observations if item.get("id") in {report.observation_id for report in event.reports}) else "digest"),
                event_id=event.event_id,
                reports=event.reports,
            )
        )
    if adaptive:
        limit = adaptive_digest_item_count(clusters, max_items=max_items)
    else:
        limit = max_items
    return tuple(clusters[:limit])


def source_coverage_from_rows(rows: Sequence[Mapping[str, Any]]) -> tuple[SourceCoverage, ...]:
    """Validate repository coverage rows and convert them to domain values."""
    coverage = []
    for row in rows:
        coverage.append(
            SourceCoverage(
                source_id=str(row.get("source_id", "")),
                status=str(row.get("status", "unknown")),
                observation_count=max(0, int(row.get("observation_count", 0))),
                last_attempt_at=(
                    int(row["last_attempt_at"])
                    if row.get("last_attempt_at") is not None
                    else None
                ),
                last_success_at=(
                    int(row["last_success_at"])
                    if row.get("last_success_at") is not None
                    else None
                ),
                consecutive_failures=max(0, int(row.get("consecutive_failures", 0))),
            )
        )
    return tuple(sorted(coverage, key=lambda item: item.source_id))


class DigestBuilder:
    """Application service for an algorithmic, storage-independent digest."""

    def __init__(
        self,
        repository: DigestInputRepository,
        *,
        region_weights: Mapping[str, int] | None = None,
        observation_limit: int = 5000,
        item_limit: int = 50,
        source_quality_weights: Mapping[str, float] | None = None,
    ) -> None:
        if not 1 <= observation_limit <= 5000:
            raise ValueError("digest observation limit is out of range")
        self.repository = repository
        self.region_weights = dict(region_weights or {})
        self.observation_limit = observation_limit
        self.item_limit = item_limit
        self.source_quality_weights = dict(source_quality_weights or {})

    def build(
        self,
        *,
        digest_key: str,
        period_start: int,
        period_end: int,
        timezone: str,
        created_at: int,
        source_ids: Sequence[str] | None = None,
        title: str | None = None,
        project_events: bool = True,
    ) -> DigestDocument:
        if project_events and isinstance(self.repository, EventPoolRepository) and isinstance(
            self.repository, EventRepository
        ):
            # Close the small race between the latest source poll and the
            # background projector. The batch is fixed so digest generation
            # cannot turn into an unbounded history rebuild.
            projector = EventPoolProjector(self.repository)
            for _ in range((self.observation_limit + 499) // 500 + 1):
                batch = projector.project_pending(
                    now=created_at,
                    since=period_start,
                    until=period_end,
                    limit=500,
                )
                if batch == 0:
                    break
        page_reader = getattr(self.repository, "list_event_page", None)
        page = (
            page_reader(
                since=period_start,
                until=period_end,
                sort="importance",
                limit=min(1000, self.observation_limit),
                reports_per_event=100,
            )
            if page_reader is not None
            else None
        )
        if page is not None and page.items:
            clusters = self._clusters_from_event_pool(page.items, source_ids=source_ids)
        else:
            # Compatibility/backfill path for a newly migrated database. It is
            # never used by HTTP reads; the dedicated projector will make this
            # branch disappear once the bounded event-pool backfill catches up.
            observations = self.repository.list_digest_observations(
                period_start,
                period_end,
                source_ids=source_ids,
                limit=self.observation_limit,
            )
            observations = [
                item
                for item in observations
                if str(item.get("handling", "digest")) in {"digest", "immediate"}
            ]
            clusters = cluster_observations_event_centric(
                observations,
                region_weights=self.region_weights,
                max_items=self.item_limit,
                source_quality_weights=self.source_quality_weights,
                adaptive=True,
            )
            clusters = self._persist_events(clusters, created_at=created_at)
        coverage = source_coverage_from_rows(
            self.repository.list_source_coverage(
                period_start, period_end, source_ids=source_ids
            )
        )
        summary = _digest_summary(clusters, coverage)
        return DigestDocument(
            digest_key=digest_key,
            version=0,
            period_start=period_start,
            period_end=period_end,
            timezone=timezone,
            title=(title or "EyeOfSauron 每日情报摘要")[:300],
            summary=summary,
            generation_kind="algorithm",
            items=clusters,
            coverage=coverage,
            created_at=created_at,
        )

    def _clusters_from_event_pool(
        self, items: Sequence[Any], *, source_ids: Sequence[str] | None = None
    ) -> tuple[DigestCluster, ...]:
        weights = effective_region_weights(self.region_weights)
        allowed_sources = set(source_ids) if source_ids is not None else None
        # Recombine by stable identity before ranking; a repeated page item
        # must not occupy two selection slots or discard its extra evidence.
        candidates_by_key: dict[str, DigestCluster] = {}
        for item in items:
            event = item.event
            scoped_reports = tuple(
                report for report in item.reports
                if allowed_sources is None or report.source_id in allowed_sources
            )
            reports_by_source: dict[str, PersistedEventReport] = {}
            for report in scoped_reports:
                current = reports_by_source.get(report.source_id)
                if current is None or (
                    report.is_representative,
                    report.published_at,
                    report.report_id or 0,
                ) > (
                    current.is_representative,
                    current.published_at,
                    current.report_id or 0,
                ):
                    reports_by_source[report.source_id] = report
            source_ids = tuple(sorted(reports_by_source))
            if not source_ids:
                continue
            reports = tuple(reports_by_source[source_id] for source_id in source_ids)
            quality = max(
                (
                    max(0.70, min(1.15, float(self.source_quality_weights.get(source_id, 1.0))))
                    for source_id in source_ids
                    if isinstance(self.source_quality_weights.get(source_id, 1.0), (int, float))
                ),
                default=1.0,
            )
            regional_interest = max(
                (region_weight(weights, region) for region in event.regions), default=3
            )
            primary_bonus = 0.35 if any(
                report.source_tier == "primary" for report in reports
            ) else 0.0
            candidate = DigestCluster(
                cluster_key=event.event_key,
                title=event.title,
                summary=event.summary,
                score=round(
                    event.score * quality
                    + max(0, regional_interest - 3) * 0.15
                    + primary_bonus,
                    4,
                ),
                importance=event.importance,
                urgency=event.urgency,
                relevance=event.relevance,
                confidence=event.confidence,
                published_at=event.last_seen_at,
                regions=event.regions,
                topics=event.topics,
                source_ids=source_ids,
                observation_ids=tuple(sorted(report.observation_id for report in scoped_reports)),
                links=tuple(report.url for report in reports if report.url),
                source_tiers=tuple(report.source_tier for report in reports),
                handling=item.handling,
                event_id=event.event_key,
                reports=scoped_reports,
            )
            existing = candidates_by_key.get(candidate.cluster_key)
            candidates_by_key[candidate.cluster_key] = (
                self._merge_event_clusters(existing, candidate)
                if existing is not None else candidate
            )
        candidates = list(candidates_by_key.values())
        selection_limit = adaptive_digest_item_count(
            candidates, max_items=self.item_limit
        )
        selected: list[DigestCluster] = []
        counts: dict[str, int] = {}
        while candidates and len(selected) < selection_limit:
            candidates.sort(key=lambda candidate: (
                candidate.handling != "immediate",
                -(
                    candidate.score
                    / (1 + 0.35 * min(
                        (counts.get(source_id, 0) for source_id in candidate.source_ids),
                        default=0,
                    ))
                ),
                -candidate.published_at,
                candidate.cluster_key,
            ))
            chosen = candidates.pop(0)
            selected.append(chosen)
            for source_id in chosen.source_ids:
                counts[source_id] = counts.get(source_id, 0) + 1
        return tuple(selected)

    def _persist_events(
        self, clusters: Sequence[DigestCluster], *, created_at: int
    ) -> tuple[DigestCluster, ...]:
        save_event = getattr(self.repository, "save_event", None)
        save_report = getattr(self.repository, "save_event_report", None)
        if save_event is None or save_report is None:
            return tuple(clusters)
        list_events = getattr(self.repository, "list_events", None)
        candidates = list_events(limit=1000) if list_events is not None else []
        persisted: dict[str, DigestCluster] = {}
        for cluster in clusters:
            reports = tuple(cluster.reports)
            if not reports:
                continue
            published = [int(report.published_at) for report in reports]
            fingerprint = cluster.event_id or cluster.cluster_key
            event_key = hashlib.sha256(
                f"{fingerprint}:{cluster.published_at // 86400}".encode("utf-8")
            ).hexdigest()[:24]
            cluster = replace(cluster, cluster_key=event_key, event_id=event_key)
            event_shape = {
                "title": cluster.title,
                "summary": cluster.summary,
                "topic": cluster.topics[0] if cluster.topics else "general",
                "region": cluster.regions[0] if cluster.regions else "GLOBAL",
                "published_at": cluster.published_at,
            }
            compatible = [
                candidate for candidate in candidates
                if abs(candidate.last_seen_at - cluster.published_at) <= 72 * 3600
            ]
            if compatible:
                best = max(
                    compatible,
                    key=lambda candidate: event_match_score(event_shape, {
                        "title": candidate.title, "summary": candidate.summary,
                        "topic": candidate.topics[0] if candidate.topics else "general",
                        "region": candidate.regions[0] if candidate.regions else "GLOBAL",
                        "published_at": candidate.last_seen_at,
                    }),
                )
                best_score = event_match_score(event_shape, {
                    "title": best.title, "summary": best.summary,
                    "topic": best.topics[0] if best.topics else "general",
                    "region": best.regions[0] if best.regions else "GLOBAL",
                    "published_at": best.last_seen_at,
                })
                # Historical continuation has an additional 72-hour and
                # topic/region gate, so it can use a slightly lower threshold
                # than within-batch clustering without enabling chain merges.
                if best_score >= 0.57:
                    event_key = best.event_key
                    fingerprint = best.fingerprint
                    cluster = replace(cluster, cluster_key=event_key, event_id=event_key)
            if event_key in persisted:
                # Conservative batch clusters can independently match the
                # same historical event. Recombine their evidence before
                # persisting and returning the unique digest event.
                cluster = self._merge_event_clusters(persisted[event_key], cluster)
                reports = tuple(cluster.reports)
                published = [int(report.published_at) for report in reports]
            representative_by_source: dict[str, Any] = {}
            for report in reports:
                current = representative_by_source.get(report.source_id)
                if current is None or (report.score, report.published_at, report.report_id) > (
                    current.score, current.published_at, current.report_id
                ):
                    representative_by_source[report.source_id] = report
            save_event(PersistedEvent(
                event_key=event_key, fingerprint=fingerprint, title=cluster.title,
                summary=cluster.summary, score=cluster.score,
                importance=cluster.importance, urgency=cluster.urgency,
                relevance=cluster.relevance, confidence=cluster.confidence,
                first_seen_at=min(published), last_seen_at=max(published),
                regions=cluster.regions, topics=cluster.topics,
                independent_source_count=len({
                    report.publisher.strip().lower() or report.source_id for report in reports
                }),
                created_at=created_at, updated_at=created_at,
            ))
            for report in reports:
                if report.observation_id is None:
                    continue
                save_report(PersistedEventReport(
                    event_key=event_key, observation_id=int(report.observation_id),
                    source_id=report.source_id, source_tier=report.source_tier,
                    publisher=report.publisher,
                    relation=("context" if report.relation == "secondary" else report.relation),
                    match_score=report.match_score,
                    is_representative=(
                        representative_by_source.get(report.source_id) is report
                    ),
                    published_at=report.published_at, title=report.title,
                    summary=report.summary, url=report.url, created_at=created_at,
                ))
            persisted[event_key] = cluster
        return tuple(persisted.values())

    @staticmethod
    def _report_rank(report: Any) -> tuple[float, int, str]:
        # Persisted reports have a representative flag; legacy in-memory
        # clustering reports have a score instead. IDs differ in type too.
        rank = float(report.is_representative) if isinstance(report, PersistedEventReport) else report.score
        return rank, report.published_at, str(report.report_id or "")

    @staticmethod
    def _merge_event_clusters(left: DigestCluster, right: DigestCluster) -> DigestCluster:
        representative = max((left, right), key=lambda item: (item.score, item.published_at, item.title))
        reports_by_identity = {
            (report.source_id, report.observation_id or report.report_id): report
            for item in (left, right) for report in item.reports
        }
        reports = tuple(sorted(
            reports_by_identity.values(),
            key=lambda report: (report.source_id, *DigestBuilder._report_rank(report)),
        ))
        by_source: dict[str, Any] = {}
        for report in reports:
            current = by_source.get(report.source_id)
            if current is None or DigestBuilder._report_rank(report) > DigestBuilder._report_rank(current):
                by_source[report.source_id] = report
        source_ids = tuple(sorted(by_source))
        return replace(
            representative,
            importance=max(left.importance, right.importance),
            urgency=max(left.urgency, right.urgency),
            relevance=max(left.relevance, right.relevance),
            confidence=max(left.confidence, right.confidence),
            published_at=max(left.published_at, right.published_at),
            regions=tuple(sorted(set(left.regions) | set(right.regions))),
            topics=tuple(sorted(set(left.topics) | set(right.topics))),
            source_ids=source_ids,
            observation_ids=tuple(sorted(set(left.observation_ids) | set(right.observation_ids))),
            links=tuple(by_source[source_id].url for source_id in source_ids if by_source[source_id].url),
            source_tiers=tuple(by_source[source_id].source_tier for source_id in source_ids),
            handling="immediate" if "immediate" in {left.handling, right.handling} else "digest",
            reports=reports,
        )

    def build_and_save(self, **kwargs: Any) -> DigestDocument:
        repository = self.repository
        if not isinstance(repository, DigestRepository):
            raise TypeError("digest repository does not support versioned storage")
        return repository.save_digest(self.build(**kwargs))


def _digest_summary(
    clusters: Sequence[DigestCluster], coverage: Sequence[SourceCoverage]
) -> str:
    covered = sum(item.status in {"covered", "quiet"} for item in coverage)
    unhealthy = sum(item.status in {"degraded", "stale"} for item in coverage)
    return f"收录 {len(clusters)} 个主题；{covered}/{len(coverage)} 个来源正常，{unhealthy} 个来源需关注。"


def with_api_summary(digest: DigestDocument, summary: str, *, created_at: int) -> DigestDocument:
    """Create a new draft payload from an API-polished summary without mutating history."""
    if not isinstance(summary, str):
        raise ValueError("digest API summary must be text")
    cleaned = summary.strip()
    if not cleaned or len(cleaned) > 20000:
        raise ValueError("digest API summary is invalid")
    return replace(
        digest,
        version=0,
        summary=cleaned,
        generation_kind="api",
        created_at=created_at,
        status="draft",
        published_at=None,
    )


class DigestScheduler:
    """Idempotent daily publication application service."""

    def __init__(
        self,
        config: DigestConfig,
        repository: DigestInputRepository,
        *,
        topic: str,
        source_ids: Sequence[str] | None = None,
        region_weights: Mapping[str, int] | None = None,
        source_quality_weights: Mapping[str, float] | None = None,
        summarizer: DigestSummarizer | None = None,
        daily_api_budget: int = 2,
        send_full_text: bool = False,
    ) -> None:
        if not isinstance(repository, DigestRepository) or not isinstance(
            repository, DigestNotificationRepository
        ):
            raise TypeError("digest scheduler requires a versioned digest repository")
        self.config = config
        self.repository = repository
        self.topic = topic
        self.source_ids = tuple(source_ids) if source_ids is not None else None
        self.summarizer = summarizer
        self.daily_api_budget = daily_api_budget
        self.send_full_text = send_full_text
        self.builder = DigestBuilder(
            repository,
            region_weights=region_weights,
            source_quality_weights=source_quality_weights,
            observation_limit=config.observation_limit,
            item_limit=config.item_limit,
        )
        # Retry state is persisted by production repositories.  The in-memory
        # fallback keeps the domain service usable with lightweight test ports.
        self._retry_memory: dict[str, DigestRetryState] = {}
        self._failure_streak_memory = 0

    def process_once(self, now: int) -> DigestDocument | None:
        if self.summarizer is not None:
            raise RuntimeError("model-assisted digests require process_once_async")
        document = self._prepare(now)
        return self._publish(document, now) if document else None

    async def process_once_async(self, now: int) -> DigestDocument | None:
        self._finalize_expired_retries(now)
        await self._project_events_async(now)
        document = self._prepare(now, project_events=False)
        if document is None:
            return None
        if document.status == "published":
            # Publication and outbox enqueue are separate transactions. Repair
            # a crash between them before taking any retry-state shortcut.
            self._publish(document, now)

        # A previously published algorithm digest may have a durable retry
        # state.  Handle that state before the normal publication path so a
        # restart does not lose the five-hour retry window.
        retry = self._get_retry(document.digest_key)
        # Recover the tiny crash window between publishing the deterministic
        # fallback and persisting its retry row.  The marker is only emitted
        # for an AI failure, so ordinary algorithm-only digests are untouched.
        if (
            retry is None
            and document.status == "published"
            and self.summarizer is not None
            and "AI 总结暂时失败" in document.summary
        ):
            self._start_retry(document, document.published_at or now, "retry state reconstructed after publication")
            retry = self._get_retry(document.digest_key)
        if retry is not None and retry.status == "pending":
            if document.generation_kind == "api":
                self._finish_retry(document.digest_key, retry, now)
                return document
            if now > retry.retry_deadline_at:
                streak, notify = self._finish_retry_failure(
                    document.digest_key, now, retry.last_error or "digest AI retry window expired",
                    attempts=retry.attempts,
                )
                if notify:
                    self._notify_failure(document.digest_key, streak, retry.last_error or "digest AI retry window expired", now)
                return document
            if retry.next_attempt_at is not None and now < retry.next_attempt_at and now < retry.retry_deadline_at:
                return document
            return await self._retry_api(document, retry, now)
        if retry is not None and retry.status == "failed":
            state_reader = getattr(self.repository, "get_digest_failure_state", None)
            if state_reader is not None:
                streak, failed_key = state_reader()
                if failed_key == document.digest_key and streak == 3:
                    self._notify_failure(document.digest_key, streak, retry.last_error or "AI retries exhausted", now)
            return document

        if document.status == "published" or not document.items or self.summarizer is None:
            return self._publish(document, now)

        try:
            summary = await self._summarize(document, now)
            polished = with_api_summary(document, summary, created_at=now)
        except Exception as exc:
            # Publish a useful deterministic report immediately.  AI retries
            # are deliberately decoupled from this first notification.
            error = sanitize_error(exc)
            logging.getLogger("argus.digest").warning(
                "digest_api_fallback error=%s", error,
            )
            fallback = replace(document, summary=document.summary + " AI 总结暂时失败，已先发送算法版日报。")
            published = self._publish(fallback, now)
            self._start_retry(published, now, error)
            return published
        published = self._publish(polished, now)
        success = getattr(self.repository, "record_digest_success", None)
        if success is not None:
            success(now=now)
        return published

    async def _summarize(self, document: DigestDocument, now: int) -> str:
        repository = self.repository
        if not isinstance(repository, DigestAnalysisRepository):
            raise TypeError("digest API requires budget and content repository ports")
        evidence = document
        if self.send_full_text:
            evidence = replace(document, items=tuple(
                replace(item, summary=(repository.get_analysis_text(
                    item.observation_ids[0], max_chars=4000,
                ) or item.summary)) if item.observation_ids else item
                for item in document.items
            ))
        if len({item.cluster_key for item in evidence.items}) != len(evidence.items):
            raise ValueError("digest contains duplicate cluster keys before inference")
        if len({item.source_id for item in evidence.coverage}) != len(evidence.coverage):
            raise ValueError("digest contains duplicate source coverage before inference")
        digest_reserver = getattr(repository, "reserve_digest_api_call", None)
        if digest_reserver is not None:
            allowed = digest_reserver(document.digest_key, limit=5, now=now)
        else:
            day = datetime.fromtimestamp(now, UTC).date().isoformat()
            allowed = repository.reserve_analysis_api_call(day, limit=self.daily_api_budget, now=now)
        if not allowed:
            raise RuntimeError("digest API attempt budget exhausted")
        # Only inference crosses threads. Repository operations remain on the
        # connection owner's event-loop thread.
        return await asyncio.to_thread(self.summarizer.summarize, evidence)  # type: ignore[union-attr]

    async def _retry_api(
        self, document: DigestDocument, retry: DigestRetryState, now: int
    ) -> DigestDocument:
        try:
            summary = await self._summarize(document, now)
            polished = with_api_summary(document, summary, created_at=now)
        except Exception as exc:
            error = sanitize_error(exc)
            attempts = retry.attempts + 1
            exhausted = attempts >= 5 or now >= retry.retry_deadline_at
            if exhausted:
                streak, notify = self._finish_retry_failure(document.digest_key, now, error, attempts=attempts)
                if notify:
                    self._notify_failure(document.digest_key, streak, error, now)
                logging.getLogger("argus.digest").error(
                    "digest_api_retries_exhausted key=%s attempts=%d error=%s",
                    document.digest_key, attempts, error,
                )
            else:
                self._record_retry_failure(document.digest_key, retry, attempts, now, error)
                logging.getLogger("argus.digest").warning(
                    "digest_api_retry_failed key=%s attempt=%d error=%s",
                    document.digest_key, attempts, error,
                )
            return document
        published = self._publish(polished, now)
        self._finish_retry(document.digest_key, retry, now)
        return published

    def _get_retry(self, digest_key: str) -> DigestRetryState | None:
        getter = getattr(self.repository, "get_digest_retry", None)
        return getter(digest_key) if getter is not None else self._retry_memory.get(digest_key)

    def _start_retry(self, document: DigestDocument, now: int, error: str) -> None:
        deadline = now + DIGEST_RETRY_WINDOW_SECONDS
        starter = getattr(self.repository, "start_digest_retry", None)
        if starter is not None:
            starter(document.digest_key, document.version, now, deadline, last_error=error)
        else:
            self._retry_memory[document.digest_key] = DigestRetryState(
                document.digest_key, document.version, 1, now + DIGEST_RETRY_INTERVAL_SECONDS, deadline, "pending", error
            )

    def _record_retry_failure(
        self, digest_key: str, retry: DigestRetryState, attempts: int, now: int, error: str
    ) -> None:
        # Anchor to the original window, not the previous poll, so normal
        # scheduler jitter cannot push the last retry beyond the deadline.
        scheduled_at = (retry.retry_deadline_at - DIGEST_RETRY_WINDOW_SECONDS
                        + attempts * DIGEST_RETRY_INTERVAL_SECONDS)
        next_at = min(retry.retry_deadline_at, max(now, scheduled_at))
        recorder = getattr(self.repository, "record_digest_retry", None)
        if recorder is not None:
            recorder(digest_key, attempts=attempts, status="pending", next_attempt_at=next_at,
                     last_error=error, now=now)
        else:
            self._retry_memory[digest_key] = replace(
                retry, attempts=attempts, next_attempt_at=next_at, last_error=error
            )

    def _finish_retry(self, digest_key: str, retry: DigestRetryState, now: int) -> None:
        recorder = getattr(self.repository, "record_digest_retry", None)
        if recorder is not None:
            recorder(digest_key, attempts=retry.attempts + 1, status="succeeded",
                     next_attempt_at=None, last_error=None, now=now)
        else:
            self._retry_memory.pop(digest_key, None)
        success = getattr(self.repository, "record_digest_success", None)
        if success is not None:
            success(now=now)

    def _finish_retry_failure(
        self, digest_key: str, now: int, error: str, *, attempts: int
    ) -> tuple[int, bool]:
        recorder = getattr(self.repository, "record_digest_retry", None)
        retry = self._get_retry(digest_key)
        if recorder is not None and retry is not None:
            recorder(digest_key, attempts=attempts, status="failed",
                     next_attempt_at=None, last_error=error, now=now)
        elif retry is not None:
            self._retry_memory[digest_key] = replace(
                retry, attempts=attempts, status="failed", next_attempt_at=None, last_error=error
            )
        failure = getattr(self.repository, "record_digest_failure", None)
        if failure is not None:
            streak, notify = failure(digest_key, now=now)
            return int(streak), bool(notify)
        self._failure_streak_memory += 1
        return self._failure_streak_memory, self._failure_streak_memory >= 3

    def _finalize_expired_retries(self, now: int) -> None:
        expired_reader = getattr(self.repository, "list_expired_digest_retries", None)
        if expired_reader is None:
            return
        for retry in expired_reader(now):
            published = self.repository.get_digest(retry.digest_key, published_only=True)
            if published is not None and published.generation_kind == "api":
                self._publish(published, now)
                self._finish_retry(retry.digest_key, retry, now)
                continue
            error = retry.last_error or "digest AI retry window expired before all attempts"
            streak, notify = self._finish_retry_failure(retry.digest_key, now, error, attempts=retry.attempts)
            if notify:
                self._notify_failure(retry.digest_key, streak, error, now)
            logging.getLogger("argus.digest").error(
                "digest_api_retry_window_expired key=%s attempts=%d error=%s",
                retry.digest_key, retry.attempts, error,
            )

    def _notify_failure(self, digest_key: str, streak: int, error: str, now: int) -> None:
        if not self.config.notify:
            return
        enqueue = getattr(self.repository, "enqueue_digest_failure_notification", None)
        if enqueue is not None:
            click_url = f"{self.config.public_base_url}/digests/{quote(digest_key, safe='')}"
            enqueue(digest_key=digest_key, streak=streak, error=error,
                    topic=self.topic, click_url=click_url, now=now)

    async def _project_events_async(self, now: int) -> None:
        """Yield between backlog batches so preparation cannot starve workers."""
        if not self.config.enabled or not isinstance(self.repository, EventPoolRepository) or not isinstance(
            self.repository, EventRepository
        ):
            return
        scheduled_at = self._latest_occurrence(now)
        if scheduled_at is None:
            return
        local_date = datetime.fromtimestamp(scheduled_at, ZoneInfo(self.config.timezone)).date()
        if self.repository.get_digest(f"daily:{local_date.isoformat()}", published_only=True) is not None:
            return
        previous_at = self._latest_occurrence(scheduled_at - 1)
        if previous_at is None:
            raise RuntimeError("cannot resolve previous digest boundary")
        projector = EventPoolProjector(self.repository)
        for _ in range((self.builder.observation_limit + 49) // 50 + 1):
            projected = projector.project_pending(
                now=now, since=previous_at, until=scheduled_at, limit=50,
            )
            if projected == 0:
                break
            await asyncio.sleep(0)

    def _prepare(self, now: int, *, project_events: bool = True) -> DigestDocument | None:
        if not self.config.enabled:
            return None
        scheduled_at = self._latest_occurrence(now)
        if scheduled_at is None:
            return None
        previous_at = self._latest_occurrence(scheduled_at - 1)
        if previous_at is None:
            raise RuntimeError("cannot resolve previous digest boundary")
        local_date = datetime.fromtimestamp(scheduled_at, ZoneInfo(self.config.timezone)).date()
        digest_key = f"daily:{local_date.isoformat()}"
        published = self.repository.get_digest(digest_key, published_only=True)
        if published is None:
            quality_rows = self.repository.list_source_quality(
                source_ids=self.source_ids or None,
                now=now,
            )
            self.builder.source_quality_weights = {
                str(row["source_id"]): float(row["weight"])
                for row in quality_rows
                if isinstance(row, Mapping) and row.get("source_id")
            }
            return self.builder.build(
                digest_key=digest_key,
                period_start=previous_at,
                period_end=scheduled_at,
                timezone=self.config.timezone,
                created_at=now,
                source_ids=self.source_ids,
                title=f"EyeOfSauron 每日情报摘要 · {local_date.isoformat()}",
                project_events=project_events,
            )
        return published

    def _publish(self, document: DigestDocument, now: int) -> DigestDocument:
        published = document
        if document.status != "published":
            saved = self.repository.save_digest(document)
            published = self.repository.publish_digest(saved.digest_key, saved.version, now)
        if self.config.notify:
            click_url = (
                f"{self.config.public_base_url}/digests/{quote(published.digest_key, safe='')}"
            )
            self.repository.enqueue_digest_notification(
                published, topic=self.topic, click_url=click_url, now=now
            )
        return published

    def _latest_occurrence(self, at_or_before: int) -> int | None:
        cursor = at_or_before - 3 * 86400
        latest: int | None = None
        for _ in range(5):
            candidate = next_daily_occurrence(
                self.config.daily_time, self.config.timezone, cursor
            )
            if candidate > at_or_before:
                break
            latest = candidate
            cursor = candidate
        return latest
