"""Bounded event quality read models independent of database technology."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from typing import Any, Iterable, Mapping, Protocol

from .event_clustering import explain_event_match, generic_event_title
from .events import EventEvidenceRepository, EventPageRepository, EventRepository


EVENT_QUALITY_LABELS = frozenset({
    "correct_merge", "false_merge", "missed_merge", "duplicate_support", "conflict",
})
MERGE_OUTCOME_LABELS = frozenset({"correct_merge", "false_merge", "missed_merge"})


class EventQualityRepository(Protocol):
    def save_event_quality_label(
        self, event_key: str, *, label: str, reason: str, actor: str, now: int,
    ) -> dict[str, Any]: ...
    def list_event_quality_labels(
        self, *, since: int, until: int, limit: int = 1000,
    ) -> list[dict[str, Any]]: ...


def event_quality_label_metrics(labels: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Calculate transparent metrics from a bounded, human-labelled sample.

    Labels are deliberately kept outside the clustering algorithm.  A duplicate
    supporting report is useful for coverage review, but it is not evidence for
    either merge precision or recall.  The returned numerators and denominators
    make a small sample impossible to mistake for production accuracy.
    """
    counts: Counter[str] = Counter()
    event_labels: dict[str, set[str]] = {}
    sample_keys: set[tuple[str, str]] = set()
    sample_count = 0
    for item in labels:
        if not isinstance(item, Mapping):
            raise ValueError("event quality label must be an object")
        event_key = str(item.get("event_key", "")).strip()
        label = str(item.get("label", "")).strip()
        if not event_key or len(event_key) > 160:
            raise ValueError("event quality label event key is invalid")
        if label not in EVENT_QUALITY_LABELS:
            raise ValueError("event quality label is invalid")
        identity = (event_key, label)
        if identity in sample_keys:
            raise ValueError("duplicate event quality label")
        sample_keys.add(identity)
        counts[label] += 1
        event_labels.setdefault(event_key, set()).add(label)
        sample_count += 1

    contradictory = sum(len(outcomes & MERGE_OUTCOME_LABELS) > 1 for outcomes in event_labels.values())

    correct = int(counts["correct_merge"])
    false = int(counts["false_merge"])
    missed = int(counts["missed_merge"])
    conflict = int(counts["conflict"])
    merge_precision_denominator = correct + false
    merge_recall_denominator = correct + missed
    reviewed_denominator = sum(bool(outcomes & (MERGE_OUTCOME_LABELS | {"conflict"}))
                               for outcomes in event_labels.values())

    def ratio(numerator: int, denominator: int) -> float | None:
        return round(numerator / denominator, 4) if denominator else None

    return {
        "sample_count": sample_count,
        "reviewed_event_count": len(event_labels),
        "contradictory_event_count": contradictory,
        "by_label": {label: int(counts[label]) for label in sorted(EVENT_QUALITY_LABELS)},
        "merge_precision_like": {
            "value": ratio(correct, merge_precision_denominator) if not contradictory else None,
            "numerator": correct, "denominator": merge_precision_denominator,
        },
        "merge_recall_like": {
            "value": ratio(correct, merge_recall_denominator) if not contradictory else None,
            "numerator": correct, "denominator": merge_recall_denominator,
        },
        "conflict_rate": {
            "value": ratio(conflict, reviewed_denominator),
            "numerator": conflict, "denominator": reviewed_denominator,
        },
        "duplicate_support_count": int(counts["duplicate_support"]),
        "interpretation": (
            "人工标注样本的方向性指标，不代表全量准确率；"
            "duplicate_support 不计入合并精确率/召回率分母；"
            "合并结论矛盾或分母为空时合并指标为 null。冲突率按独立事件计数。"
        ),
    }


def event_quality(
    repository: EventPageRepository, *, since: int, until: int,
    labels: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    page = repository.list_event_page(since=since, until=until, limit=1000, reports_per_event=1)
    items = page.items
    total = len(items)
    counts = Counter(item.event.title.strip().casefold() for item in items)
    singleton = sum(item.event.independent_source_count == 1 for item in items)
    return {
        "window_since": since, "window_until": until, "sample_limit": 1000,
        "sample_truncated": page.next_cursor is not None,
        "event_count": total, "report_count": sum(item.report_count for item in items),
        "single_publisher_events": singleton,
        "single_publisher_ratio": round(singleton / total, 4) if total else 0,
        "multi_publisher_events": sum(item.event.independent_source_count > 1 for item in items),
        "generic_title_events": sum(generic_event_title(item.event.title) for item in items),
        "repeated_titles": [{"title": title, "events": count} for title, count in counts.most_common(20) if count > 1],
        "label_metrics": event_quality_label_metrics(labels),
        "interpretation": "单一出版方比例反映覆盖，不代表聚类错误率。重复标题和栏目名只是人工抽查线索；无标注样本不能推算误合并率。",
    }


def event_match_evidence(repository: EventRepository, event_key: str) -> dict[str, Any]:
    graph = (repository.read_event_evidence(event_key)
             if isinstance(repository, EventEvidenceRepository) else None)
    event = graph.event if graph is not None else repository.get_event(event_key)
    if event is None:
        raise ValueError("event no longer exists")
    reports = list(graph.reports) if graph is not None else repository.list_event_reports(event_key, limit=201)
    representative = {"title": event.title, "summary": event.summary, "published_at": event.last_seen_at,
                      "topic": event.topics[0] if event.topics else "general",
                      "region": event.regions[0] if event.regions else "GLOBAL"}
    history = ({
        "event": asdict(graph.event),
        "claims": [asdict(item) for item in graph.claims],
        "claim_evidence": [asdict(item) for item in graph.evidence],
        "timeline": [asdict(item) for item in graph.timeline],
        "notifications": list(graph.notifications),
        "history_truncated": graph.truncated,
    } if graph is not None else {})
    return {**history, "reports_truncated": (graph.truncated["reports"] if graph is not None else len(reports) > 200), "reports": [
        {**asdict(report), "evidence": explain_event_match(asdict(report), representative)} for report in reports[:200]
    ]}
