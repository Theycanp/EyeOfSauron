"""Deterministic, order-independent event clustering.

This module is deliberately storage agnostic.  It turns raw observation-like
mappings into stable event projections while retaining each source report as
parallel evidence.  The implementation is conservative: a report is only
attached when there is enough shared signal, and complete-linkage guards avoid
the classic A~B~C chain merge.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Mapping, Sequence


_SPACE_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?%?")
_CJK_RUN_RE = re.compile(r"[\u3400-\u9fff]{2,}")
_STOPWORDS = frozenset(
    "表示 发布 关于 相关 记者 消息 最新 今日 日前 将在 以及 进行 召开".split()
)


@dataclass(frozen=True, slots=True)
class EventReport:
    """One source's report, retained as a first-class event member."""

    report_id: str
    observation_id: int | None
    source_id: str
    publisher: str
    source_tier: str
    title: str
    summary: str
    url: str
    published_at: int
    score: float
    relation: str = "primary"
    match_score: float = 1.0
    contradicts: bool = False

    def __post_init__(self) -> None:
        if not self.report_id or len(self.report_id) > 128:
            raise ValueError("event report ID is invalid")
        if self.source_tier not in {"primary", "secondary", "social"}:
            raise ValueError("event report source tier is invalid")
        if self.relation not in {"primary", "secondary", "social", "corroborates", "updates", "context", "contradicts"}:
            raise ValueError("event report relation is invalid")
        if not 0.0 <= self.match_score <= 1.0:
            raise ValueError("event report match score is invalid")


@dataclass(frozen=True, slots=True)
class ClusteredEvent:
    """An event and its parallel reports."""

    event_id: str
    title: str
    summary: str
    reports: tuple[EventReport, ...]
    topics: tuple[str, ...]
    regions: tuple[str, ...]
    score: float
    importance: int
    urgency: int
    relevance: int
    confidence: float
    published_at: int
    independent_source_count: int
    has_contradictions: bool


@dataclass(frozen=True, slots=True)
class EventClaim:
    """A normalized fact claim; evidence remains attached to reports."""

    claim_id: str
    text: str
    status: str = "unverified"
    report_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EventTimelineItem:
    """A point in an event timeline, retaining its originating report."""

    timeline_id: str
    occurred_at: int
    text: str
    report_id: str
    kind: str = "update"


# Public vocabulary used by the persistence and presentation layers.  The
# implementation name remains explicit for callers that need to distinguish a
# clustered projection from a future persisted Event aggregate.
Event = ClusteredEvent
Claim = EventClaim
TimelineItem = EventTimelineItem


def _text(value: Any) -> str:
    return _SPACE_RE.sub(" ", unicodedata.normalize("NFKC", str(value or "")).casefold()).strip()


def _tokens(value: str) -> frozenset[str]:
    normalized = _text(value)
    raw = _TOKEN_RE.findall(normalized)
    result = {token for token in raw if token and token not in _STOPWORDS}
    cjk = "".join(token for token in raw if len(token) == 1 and ord(token) >= 0x3400)
    result.update(cjk[index : index + 2] for index in range(max(0, len(cjk) - 1)))
    for run in _CJK_RUN_RE.findall(normalized):
        if run not in _STOPWORDS and len(run) <= 12:
            result.add(run)
    return frozenset(result)


def _numbers(item: Mapping[str, Any]) -> frozenset[str]:
    return frozenset(_NUMBER_RE.findall(_text(f"{item.get('title', '')} {item.get('summary', '')}")))


def _entities(item: Mapping[str, Any]) -> frozenset[str]:
    attrs = item.get("attributes")
    if isinstance(attrs, Mapping):
        explicit = attrs.get("entities")
        if isinstance(explicit, (list, tuple, set)):
            values = {_text(value) for value in explicit if str(value).strip()}
            if values:
                return frozenset(values)
    title = _text(item.get("title", ""))
    # Keep longer CJK runs as coarse entities.  This is intentionally light;
    # richer NER can be layered on later without changing this interface.
    return frozenset(run for run in _CJK_RUN_RE.findall(title) if run not in _STOPWORDS)


def _title_similarity(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    a = _text(left.get("title", ""))
    b = _text(right.get("title", ""))
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta, tb = _tokens(a), _tokens(b)
    union = ta | tb
    jaccard = len(ta & tb) / len(union) if union else 0.0
    sequence = SequenceMatcher(None, a, b, autojunk=False).ratio()
    return max(jaccard, sequence * 0.9)


def _region_compatible(left: str, right: str) -> bool:
    if not left or not right or left == right or "GLOBAL" in {left, right}:
        return True
    families = {"CN": "EAST_ASIA", "JP": "EAST_ASIA", "US": "NORTH_AMERICA"}
    return families.get(left, left) == families.get(right, right)


def _pair_score(left: Mapping[str, Any], right: Mapping[str, Any], *, window: int) -> tuple[float, bool]:
    topic_left = _text(left.get("topic", "general"))
    topic_right = _text(right.get("topic", "general"))
    if topic_left != topic_right and "general" not in {topic_left, topic_right}:
        return 0.0, False
    region_left = _text(left.get("region", "GLOBAL")).upper()
    region_right = _text(right.get("region", "GLOBAL")).upper()
    title = _title_similarity(left, right)
    entities_left, entities_right = _entities(left), _entities(right)
    entity_score = len(entities_left & entities_right) / max(1, len(entities_left | entities_right))
    left_ts, right_ts = int(left.get("published_at", 0) or 0), int(right.get("published_at", 0) or 0)
    time_score = max(0.0, 1.0 - abs(left_ts - right_ts) / max(1, window))
    nums_left, nums_right = _numbers(left), _numbers(right)
    number_overlap = len(nums_left & nums_right) / max(1, len(nums_left | nums_right))
    if not nums_left and not nums_right:
        number_overlap = 0.5
    region_score = 1.0 if _region_compatible(region_left, region_right) else 0.0
    score = 0.35 * title + 0.25 * entity_score + 0.15 * time_score + 0.15 * number_overlap + 0.10 * region_score
    # Distinct figures in otherwise similar reports are retained as one event
    # with a contradiction marker, rather than silently replacing either fact.
    contradictory = bool(nums_left and nums_right and not (nums_left & nums_right) and title >= 0.72 and entity_score >= 0.2)
    if not _region_compatible(region_left, region_right) and title < 0.88:
        return 0.0, contradictory
    return min(1.0, score), contradictory


def _report_id(item: Mapping[str, Any]) -> str:
    external = str(item.get("external_id", "")).strip()
    identity = external or str(item.get("url", "")).strip() or f"{item.get('source_id', '')}|{item.get('title', '')}|{item.get('published_at', 0)}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _tier(value: Any) -> str:
    tier = str(value or "secondary").strip().lower()
    return tier if tier in {"primary", "secondary", "social"} else "secondary"


def _score(item: Mapping[str, Any]) -> float:
    def bounded(name: str, default: float) -> float:
        value = item.get(name, default)
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default

    return bounded("importance", 3) * 0.35 + bounded("urgency", 2) * 0.15 + bounded("relevance", 3) * 0.25 + max(0.0, min(1.0, bounded("confidence", 0.5))) * 0.5


def _component_guard(component: Sequence[int], items: Sequence[Mapping[str, Any]], edges: Mapping[tuple[int, int], float], threshold: float) -> bool:
    # Complete-linkage with a small tolerance prevents transitive chain merges.
    floor = max(0.5, threshold - 0.15)
    for pos, left in enumerate(component):
        for right in component[pos + 1 :]:
            if edges.get((min(left, right), max(left, right)), 0.0) < floor:
                return False
    return True


def cluster_events(
    observations: Sequence[Mapping[str, Any]],
    *,
    similarity_threshold: float = 0.62,
    time_window: int = 72 * 3600,
) -> tuple[ClusteredEvent, ...]:
    """Cluster observations deterministically, independent of input order."""
    if not 0.5 <= similarity_threshold <= 1.0:
        raise ValueError("similarity threshold is out of range")
    if time_window < 1:
        raise ValueError("time window must be positive")
    items = sorted(
        (item for item in observations if str(item.get("title", "")).strip()),
        key=lambda item: (_report_id(item), int(item.get("id", 0) or 0)),
    )
    edges: dict[tuple[int, int], float] = {}
    contradictions: set[tuple[int, int]] = set()
    for left in range(len(items)):
        for right in range(left + 1, len(items)):
            score, contradictory = _pair_score(items[left], items[right], window=time_window)
            if score >= similarity_threshold:
                edges[(left, right)] = score
                if contradictory:
                    contradictions.add((left, right))

    # Process edges in a fixed order and accept only complete-linkage merges.
    # This keeps A~B and B~C from swallowing an unrelated A/C pair.
    components: list[list[int]] = [[index] for index in range(len(items))]
    membership = {index: index for index in range(len(items))}
    for (left, right), _ in sorted(edges.items(), key=lambda entry: (-entry[1], entry[0])):
        li, ri = membership[left], membership[right]
        if li == ri:
            continue
        merged = components[li] + components[ri]
        if not _component_guard(merged, items, edges, similarity_threshold):
            continue
        components[li] = sorted(merged)
        for member in merged:
            membership[member] = li
        components[ri] = []

    result: list[ClusteredEvent] = []
    for component in (value for value in components if value):
        members = [items[index] for index in component]
        representative = min(
            members,
            key=lambda item: (-_score(item), _text(item.get("title", "")), _report_id(item)),
        )
        title = str(representative.get("title", "")).strip()[:500]
        topics = tuple(sorted({_text(item.get("topic", "general")) for item in members}))
        regions = tuple(sorted({_text(item.get("region", "GLOBAL")).upper() for item in members}))
        entity_union = sorted(set().union(*(_entities(item) for item in members)))
        fingerprint = "|".join([_text(title), ",".join(topics), ",".join(regions), ",".join(entity_union)])
        event_id = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:24]
        primary_present = any(_tier(item.get("source_tier")) == "primary" for item in members)
        # Classify updates chronologically, then present reports newest first.
        relation_by_id: dict[str, str] = {}
        source_seen: set[str] = set()
        for index in sorted(component, key=lambda value: (int(items[value].get("published_at", 0) or 0), _report_id(items[value]))):
            item = items[index]
            tier = _tier(item.get("source_tier"))
            source_id = str(item.get("source_id", ""))
            if source_id in source_seen:
                relation_by_id[_report_id(item)] = "updates"
            elif tier == "primary":
                relation_by_id[_report_id(item)] = "primary"
            elif tier == "secondary" and primary_present:
                relation_by_id[_report_id(item)] = "corroborates"
            elif tier == "social":
                relation_by_id[_report_id(item)] = "context" if primary_present else "social"
            else:
                relation_by_id[_report_id(item)] = "context" if tier == "secondary" else tier
            source_seen.add(source_id)
        report_rows: list[EventReport] = []
        for index in sorted(component, key=lambda value: (-int(items[value].get("published_at", 0) or 0), _report_id(items[value]))):
            item = items[index]
            tier = _tier(item.get("source_tier"))
            source_id = str(item.get("source_id", ""))
            relation = relation_by_id[_report_id(item)]
            pair_scores = [edges.get((min(index, other), max(index, other)), 0.0) for other in component if other != index]
            report_rows.append(
                EventReport(
                    report_id=_report_id(item),
                    observation_id=int(item["id"]) if item.get("id") is not None else None,
                    source_id=source_id,
                    publisher=str(item.get("publisher", source_id)),
                    source_tier=tier,
                    title=str(item.get("title", "")).strip()[:500],
                    summary=str(item.get("summary", "")).strip()[:4000],
                    url=str(item.get("url", "")),
                    published_at=int(item.get("published_at", 0) or 0),
                    score=_score(item),
                    relation=relation,
                    match_score=min(1.0, max(pair_scores, default=1.0)),
                    contradicts=any((min(index, other), max(index, other)) in contradictions for other in component if other != index),
                )
            )
        summary = next((report.summary for report in report_rows if report.summary), title)
        importance = max(_bounded_score(item.get("importance"), 3) for item in members)
        urgency = max(_bounded_score(item.get("urgency"), 2) for item in members)
        relevance = max(_bounded_score(item.get("relevance"), 3) for item in members)
        confidence = max(_bounded_float(item.get("confidence"), 0.5) for item in members)
        independent = len({report.source_id for report in report_rows if report.source_id})
        score = max(report.score for report in report_rows) + min(0.75, 0.25 * math.log2(max(1, independent)))
        result.append(
            ClusteredEvent(
                event_id=event_id,
                title=title,
                summary=summary[:4000],
                reports=tuple(report_rows),
                topics=topics,
                regions=regions,
                score=round(score, 4),
                importance=importance,
                urgency=urgency,
                relevance=relevance,
                confidence=confidence,
                published_at=max(report.published_at for report in report_rows),
                independent_source_count=independent,
                has_contradictions=any(report.contradicts for report in report_rows),
            )
        )
    return tuple(sorted(result, key=lambda event: (-event.score, -event.published_at, event.event_id)))


def event_match_score(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    """Score a new event projection against a persisted event candidate."""
    score, _ = _pair_score(left, right, window=72 * 3600)
    return score


def _bounded_score(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(1, min(5, value))


def _bounded_float(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(0.0, min(1.0, float(value)))
