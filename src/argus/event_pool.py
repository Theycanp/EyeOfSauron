"""Continuous, bounded projection from observations into stable events."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import replace
from typing import Any

from .event_clustering import cluster_events, event_match_score, generic_event_title
from .event_identity import identify_semantic_event
from .events import (
    EventPoolRepository,
    EventRepository,
    PersistedEvent,
    PersistedEventReport,
    EventWorkspaceRepository,
    event_lifecycle,
)


LOGGER = logging.getLogger("argus.event_pool")


class EventPoolProjector:
    """Incrementally maintain the event pool outside HTTP read paths.

    Each unassigned observation is compared with a fixed-size candidate set.
    Runtime is therefore O(new observations * candidate_limit), never an
    unbounded all-history pairwise comparison.
    """

    def __init__(
        self,
        repository: EventPoolRepository | EventRepository,
        *,
        match_window_seconds: int = 72 * 3600,
        candidate_limit: int = 1000,
        match_threshold: float = 0.57,
    ) -> None:
        if match_window_seconds < 1 or not 1 <= candidate_limit <= 1000:
            raise ValueError("event projector bounds are invalid")
        if not 0.5 <= match_threshold <= 1.0:
            raise ValueError("event projector threshold is invalid")
        if not isinstance(repository, EventPoolRepository) or not isinstance(
            repository, EventRepository
        ):
            raise TypeError("event projector requires event pool persistence ports")
        self.repository = repository
        self.match_window_seconds = match_window_seconds
        self.candidate_limit = candidate_limit
        self.match_threshold = match_threshold
        self._last_maintenance = -60

    @staticmethod
    def _shape(event: PersistedEvent) -> dict[str, Any]:
        return {
            "title": event.title,
            "summary": event.summary,
            "topic": event.topics[0] if event.topics else "general",
            "region": event.regions[0] if event.regions else "GLOBAL",
            "published_at": (event.first_seen_at if identify_semantic_event(event.title) is not None
                             else event.last_seen_at),
        }

    def _match(self, observation: dict[str, Any]) -> tuple[PersistedEvent | None, float]:
        published_at = int(observation.get("published_at", 0))
        candidates = self.repository.list_event_candidates(
            max(0, published_at - self.match_window_seconds),
            published_at + self.match_window_seconds + 1,
            limit=self.candidate_limit,
        )
        ranked = sorted(
            (
                (event_match_score(observation, self._shape(candidate)), candidate)
                for candidate in candidates
            ),
            key=lambda item: (-item[0], item[1].event_key),
        )
        semantic = identify_semantic_event(str(observation.get("title", "")))
        for score, candidate in ranked:
            if score < self.match_threshold:
                break
            reports = self.repository.list_event_reports(candidate.event_key, limit=2000)
            if len(reports) == 2000:
                continue
            # Guard the whole evidence set, not just a representative that can
            # change when an official source arrives later.
            if any(event_match_score(observation, {
                "title": report.title, "summary": report.summary, "published_at": report.published_at,
                "region": candidate.regions[0] if candidate.regions else "GLOBAL",
                "topic": candidate.topics[0] if candidate.topics else "general",
            }) < 0.5 for report in reports):
                continue
            if semantic is not None:
                if any(
                    prior is not None and not semantic.compatible(prior, published_at, report.published_at)
                    for report in reports
                    if (prior := identify_semantic_event(report.title)) is not None
                ):
                    continue
            return candidate, score
        return None, 0.0

    def project_pending(
        self,
        *,
        now: int,
        limit: int = 500,
        since: int | None = None,
        until: int | None = None,
    ) -> int:
        if now < 0 or not 1 <= limit <= 500:
            raise ValueError("event projection request is invalid")
        if since is not None and since < 0 or until is not None and until < 0:
            raise ValueError("event projection time range is invalid")
        if since is not None and until is not None and until <= since:
            raise ValueError("event projection time range is invalid")
        projected = 0
        if now - self._last_maintenance >= 60 and isinstance(self.repository, EventWorkspaceRepository):
            self.repository.maintain_event_lifecycle(now=now)
            self._last_maintenance = now
        for observation in self.repository.list_unassigned_event_observations(
            since=since, until=until, limit=limit
        ):
            try:
                clustered = cluster_events((observation,))
                if not clustered:
                    continue
                incoming = clustered[0]
                report = incoming.reports[0]
                existing, match_score = self._match(observation)
                semantic = identify_semantic_event(report.title, report.summary)
                bucket = incoming.published_at if semantic is not None else incoming.published_at // 86400
                identity_seed = f"{incoming.event_id}:{bucket}"
                if generic_event_title(report.title):
                    identity_seed += f":{report.observation_id}"
                new_identity = hashlib.sha256(identity_seed.encode("ascii")).hexdigest()[:24]
                event_key = existing.event_key if existing is not None else new_identity
                prior_reports = (
                    self.repository.list_event_reports(event_key, limit=500)
                    if existing is not None
                    else []
                )
                prior_sources = {item.source_id for item in prior_reports}
                has_primary = any(item.source_tier == "primary" for item in prior_reports)
                if semantic is not None and semantic.relation_hint == "context":
                    relation = "context"
                elif report.source_id in prior_sources:
                    relation = "updates"
                elif report.source_tier == "primary":
                    relation = "primary"
                elif report.source_tier == "secondary":
                    relation = "corroborates" if has_primary else "context"
                else:
                    relation = "context" if prior_reports else "social"

                if existing is None:
                    event = PersistedEvent(
                        event_key=event_key,
                        fingerprint=incoming.event_id,
                        title=incoming.title,
                        summary=incoming.summary,
                        score=incoming.score,
                        importance=incoming.importance,
                        urgency=incoming.urgency,
                        relevance=incoming.relevance,
                        confidence=incoming.confidence,
                        first_seen_at=incoming.published_at,
                        last_seen_at=incoming.published_at,
                        regions=incoming.regions,
                        topics=incoming.topics,
                        status=event_lifecycle(incoming.published_at, now),
                        independent_source_count=1 if report.source_id else 0,
                        created_at=now,
                        updated_at=now,
                    )
                else:
                    prefer_incoming = report.source_tier == "primary" and not has_primary
                    event = replace(
                        existing,
                        title=incoming.title if prefer_incoming else existing.title,
                        summary=incoming.summary if prefer_incoming else existing.summary,
                        score=max(existing.score, incoming.score),
                        importance=max(existing.importance, incoming.importance),
                        urgency=max(existing.urgency, incoming.urgency),
                        relevance=max(existing.relevance, incoming.relevance),
                        confidence=max(existing.confidence, incoming.confidence),
                        first_seen_at=min(existing.first_seen_at, incoming.published_at),
                        last_seen_at=max(existing.last_seen_at, incoming.published_at),
                        regions=tuple(sorted(set(existing.regions) | set(incoming.regions))),
                        topics=tuple(sorted(set(existing.topics) | set(incoming.topics))),
                        status=event_lifecycle(max(existing.last_seen_at, incoming.published_at), now),
                        independent_source_count=len(prior_sources | {report.source_id}),
                        updated_at=now,
                    )
                self.repository.save_event_projection(
                    event,
                    PersistedEventReport(
                        event_key=event_key,
                        observation_id=int(report.observation_id or observation["id"]),
                        source_id=report.source_id,
                        publisher=report.publisher,
                        source_tier=report.source_tier,
                        relation=relation,
                        match_score=match_score if existing is not None else 1.0,
                        is_representative=True,
                        published_at=report.published_at,
                        title=report.title,
                        summary=report.summary,
                        url=report.url,
                        created_at=now,
                    )
                )
                projected += 1
            except (KeyError, TypeError, ValueError) as exc:
                LOGGER.warning(
                    "event_projection_skipped observation_id=%s error=%s",
                    observation.get("id"), exc,
                )
        return projected
