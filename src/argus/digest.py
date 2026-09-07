"""Deterministic daily digest construction.

The module owns digest policy and clustering, but no storage or HTTP details.
Persistence implementations consume and return these immutable domain models.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .config import DigestConfig
from .reminders import next_daily_occurrence


_WORD_RE = re.compile(r"[a-z0-9]+|[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_SPACE_RE = re.compile(r"\s+")
_COVERAGE_STATUSES = frozenset(
    {"covered", "quiet", "degraded", "stale", "disabled", "unknown"}
)


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
    handling: str = "digest"

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
    regional_interest = _bounded_int(region_weights.get(region, region_weights.get("OTHER", 3)), 3)
    source_bonus = 0.35 if item.get("source_tier") == "primary" else 0.0
    base = round(
        importance * 0.35
        + urgency * 0.15
        + relevance * 0.25
        + confidence * 0.5
        + regional_interest * 0.15
        + source_bonus,
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


def cluster_observations(
    observations: Sequence[Mapping[str, Any]],
    *,
    region_weights: Mapping[str, int] | None = None,
    similarity_threshold: float = 0.62,
    max_items: int = 50,
    source_quality_weights: Mapping[str, float] | None = None,
) -> tuple[DigestCluster, ...]:
    """Cluster related reports and return ranked, deterministic digest items."""
    if not 0.5 <= similarity_threshold <= 1.0:
        raise ValueError("digest similarity threshold is out of range")
    if not 1 <= max_items <= 500:
        raise ValueError("digest item limit is out of range")
    weights = dict(region_weights or {"CN": 5, "US": 5, "JP": 4, "GLOBAL": 3, "OTHER": 3})
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
                    dict.fromkeys(str(item.get("url", "")) for item in group if item.get("url"))
                ),
                handling=(
                    "immediate"
                    if any(str(item.get("handling", "digest")) == "immediate" for item in group)
                    else "digest"
                ),
            )
        )
    clusters.sort(key=lambda item: (-item.score, -item.published_at, item.cluster_key))
    return tuple(clusters[:max_items])


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
    ) -> DigestDocument:
        observations = self.repository.list_observations(
            period_start,
            period_end,
            handling=None,
            limit=self.observation_limit,
        )
        observations = [
            item
            for item in observations
            if str(item.get("handling", "digest")) in {"digest", "immediate"}
        ]
        clusters = cluster_observations(
            observations,
            region_weights=self.region_weights,
            max_items=self.item_limit,
            source_quality_weights=self.source_quality_weights,
        )
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
    ) -> None:
        if not isinstance(repository, DigestRepository) or not isinstance(
            repository, DigestNotificationRepository
        ):
            raise TypeError("digest scheduler requires a versioned digest repository")
        self.config = config
        self.repository = repository
        self.topic = topic
        self.source_ids = tuple(source_ids or ())
        self.builder = DigestBuilder(
            repository,
            region_weights=region_weights,
            source_quality_weights=source_quality_weights,
            observation_limit=config.observation_limit,
            item_limit=config.item_limit,
        )

    def process_once(self, now: int) -> DigestDocument | None:
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
            draft = self.builder.build_and_save(
                digest_key=digest_key,
                period_start=previous_at,
                period_end=scheduled_at,
                timezone=self.config.timezone,
                created_at=now,
                source_ids=self.source_ids,
                title=f"EyeOfSauron 每日情报摘要 · {local_date.isoformat()}",
            )
            published = self.repository.publish_digest(digest_key, draft.version, now)
        if self.config.notify:
            click_url = (
                f"{self.config.public_base_url}/digests/{quote(digest_key, safe='')}"
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
