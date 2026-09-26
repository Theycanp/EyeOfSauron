"""Bounded event quality read models independent of database technology."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from typing import Any

from .event_clustering import explain_event_match, generic_event_title
from .events import EventEvidenceRepository, EventPageRepository, EventRepository


def event_quality(repository: EventPageRepository, *, since: int, until: int) -> dict[str, Any]:
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
