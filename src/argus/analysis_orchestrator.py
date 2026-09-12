"""Application-layer orchestration for deterministic and semantic analysis.

The orchestrator depends only on repository and analyzer ports. It deliberately
keeps network calls outside persistence transactions and treats every model
response as untrusted advice.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import Protocol, Sequence, runtime_checkable

from .analysis import (
    HANDLING_DIGEST,
    AnalysisAttempt,
    Analyzer,
    InformationAnalysis,
    analyze_observation,
    merge_advisory_analysis,
)
from .config import AnalysisConfig
from .models import AnalysisWorkItem, Observation
from .util import now_epoch, sanitize_error


LOGGER = logging.getLogger("argus.analysis")


@runtime_checkable
class AnalysisRepository(Protocol):
    """Persistence operations required by the analysis application service."""

    def get_analysis_text(self, observation_id: int, *, max_chars: int) -> str: ...

    def claim_analysis_observations(
        self, now: int, *, limit: int, lease_seconds: int
    ) -> list[AnalysisWorkItem]: ...

    def reserve_analysis_api_call(
        self, budget_day: str, *, limit: int, now: int
    ) -> bool: ...

    def save_analysis_result(
        self,
        observation_id: int,
        lease_token: str,
        analysis: InformationAnalysis,
        attempts: Sequence[AnalysisAttempt],
        *,
        processing_state: str,
        now: int,
        error: str | None = None,
    ) -> None: ...


def _with_region_policy(
    baseline: InformationAnalysis, config: AnalysisConfig
) -> InformationAnalysis:
    """Apply the explicit personal region preference without model input."""

    weight = config.region_weights.get(
        baseline.region, config.region_weights.get("OTHER", 3)
    )
    relevance = max(baseline.relevance, max(1, min(5, int(weight))))
    handling = baseline.handling
    if handling != "immediate" and (baseline.importance >= 3 or relevance >= 3):
        handling = HANDLING_DIGEST
    reasons = baseline.reasons
    if relevance > baseline.relevance:
        reasons = (*reasons, f"区域关注权重 {baseline.region}={weight}")
    return replace(
        baseline,
        relevance=relevance,
        handling=handling,
        reasons=tuple(dict.fromkeys(reasons)),
    )


def _remote_candidate(analysis: InformationAnalysis) -> bool:
    """Keep paid/remote calls for meaningful, non-breaking digest material."""

    return (
        analysis.handling == HANDLING_DIGEST
        and analysis.relevance >= 4
        and analysis.importance >= 3
    )


class AnalysisOrchestrator:
    """Run deterministic triage with optional local/API advisory stages."""

    def __init__(
        self,
        config: AnalysisConfig,
        repository: AnalysisRepository,
        *,
        local_analyzer: Analyzer | None = None,
        api_analyzer: Analyzer | None = None,
        lease_seconds: int = 300,
    ) -> None:
        self.config = config
        self.repository = repository
        self.local_analyzer = local_analyzer
        self.api_analyzer = api_analyzer
        self.lease_seconds = lease_seconds
        if config.local_enabled and local_analyzer is None:
            raise ValueError("local analysis is enabled but no local analyzer was provided")
        if config.api_enabled and api_analyzer is None:
            raise ValueError("API analysis is enabled but no API analyzer was provided")
        if lease_seconds < config.timeout_seconds:
            raise ValueError("analysis lease must be at least the analyzer timeout")

    async def _call(
        self, analyzer: Analyzer, observation: Observation, *, shadow: bool
    ) -> tuple[dict[str, object] | None, AnalysisAttempt]:
        started = time.monotonic()
        try:
            advisory = dict(await asyncio.to_thread(analyzer.analyze, observation))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = sanitize_error(exc)
            return None, AnalysisAttempt(
                analyzer=analyzer.name,
                outcome="failed",
                error=error,
                duration_ms=int((time.monotonic() - started) * 1000),
                shadow=shadow,
            )
        return advisory, AnalysisAttempt(
            analyzer=analyzer.name,
            outcome="succeeded",
            advisory=advisory,
            duration_ms=int((time.monotonic() - started) * 1000),
            shadow=shadow,
        )

    async def _process_item(self, item: AnalysisWorkItem, now: int) -> None:
        baseline = _with_region_policy(analyze_observation(item.observation), self.config)
        final = baseline
        attempts: list[AnalysisAttempt] = [
            AnalysisAttempt(
                analyzer="deterministic",
                outcome="succeeded",
                advisory={
                    "importance": baseline.importance,
                    "urgency": baseline.urgency,
                    "relevance": baseline.relevance,
                    "confidence": baseline.confidence,
                    "region": baseline.region,
                    "topic": baseline.topic,
                    "handling": baseline.handling,
                    "reasons": list(baseline.reasons),
                },
            )
        ]
        successful_advisory: dict[str, object] | None = None
        model_observation = item.observation
        if self.config.send_full_text and (self.config.local_enabled or self.config.api_triage_enabled):
            body = self.repository.get_analysis_text(
                item.observation_id, max_chars=self.config.max_input_chars,
            )
            model_observation = replace(item.observation, attributes={
                **item.observation.attributes, "analysis_text": body,
            })

        if self.config.local_enabled:
            assert self.local_analyzer is not None
            advisory, attempt = await self._call(
                self.local_analyzer, model_observation, shadow=self.config.shadow_mode
            )
            attempts.append(attempt)
            if advisory is not None:
                successful_advisory = advisory

        if (
            self.config.api_enabled
            and self.config.api_triage_enabled
            and _remote_candidate(baseline)
            and 0 <= now - item.fetched_at <= 86400
        ):
            budget_day = datetime.fromtimestamp(now, UTC).date().isoformat()
            if self.repository.reserve_analysis_api_call(
                budget_day, limit=self.config.daily_api_budget, now=now
            ):
                assert self.api_analyzer is not None
                advisory, attempt = await self._call(
                    self.api_analyzer, model_observation, shadow=self.config.shadow_mode
                )
                attempts.append(attempt)
                if advisory is not None:
                    # Remote analysis wins over local advice, but is still merged
                    # only once against the deterministic authority.
                    successful_advisory = advisory
            else:
                attempts.append(
                    AnalysisAttempt(analyzer="api", outcome="budget_exhausted", shadow=True)
                )

        if successful_advisory is not None and not self.config.shadow_mode:
            final = merge_advisory_analysis(baseline, successful_advisory)

        failures = [attempt.error for attempt in attempts if attempt.outcome == "failed"]
        state = "degraded" if failures else "analyzed"
        self.repository.save_analysis_result(
            item.observation_id,
            item.lease_token,
            final,
            attempts,
            processing_state=state,
            now=now_epoch(),
            error="; ".join(error for error in failures if error) or None,
        )

    async def process_once(self, now: int | None = None) -> int:
        """Claim and process one bounded batch, returning the item count."""

        if not self.config.enabled:
            return 0
        claimed_at = now_epoch() if now is None else now
        # A lease must cover every sequential model request in this batch.
        calls_per_item = int(self.config.local_enabled) + int(self.config.api_enabled and self.config.api_triage_enabled)
        safe_limit = (max(1, (self.lease_seconds - 10) // (self.config.timeout_seconds * calls_per_item))
                      if calls_per_item else self.config.max_items_per_run)
        items = self.repository.claim_analysis_observations(
            claimed_at,
            limit=min(self.config.max_items_per_run, safe_limit),
            lease_seconds=self.lease_seconds,
        )
        for item in items:
            try:
                await self._process_item(item, claimed_at)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A programming or persistence error is isolated to this item;
                # a deterministic result remains usable and the lease can retry.
                LOGGER.error(
                    "analysis_item_failed observation_id=%d error=%s",
                    item.observation_id,
                    sanitize_error(exc),
                )
        return len(items)
