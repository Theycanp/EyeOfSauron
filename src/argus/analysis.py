"""Information enrichment and conservative triage contracts.

The deterministic policy is the authority for routing. Optional model
analyzers may enrich an observation, but their output is advisory and must be
validated by the caller before it can affect delivery.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

from .models import Observation


HANDLING_IMMEDIATE = "immediate"
HANDLING_DIGEST = "digest"
HANDLING_ARCHIVE = "archive"
HANDLINGS = frozenset({HANDLING_IMMEDIATE, HANDLING_DIGEST, HANDLING_ARCHIVE})


@dataclass(frozen=True, slots=True)
class InformationAnalysis:
    importance: int
    urgency: int
    relevance: int
    confidence: float
    region: str
    topic: str
    source_tier: str
    information_type: str
    handling: str
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AnalysisAttempt:
    """Auditable outcome of one stage in the analysis pipeline."""

    analyzer: str
    outcome: str
    advisory: Mapping[str, Any] | None = None
    error: str | None = None
    duration_ms: int = 0
    shadow: bool = False


@runtime_checkable
class Analyzer(Protocol):
    """Optional enrichment port implemented by rules, local models, or APIs."""

    name: str

    def analyze(self, observation: Observation) -> Mapping[str, Any]: ...


def _bounded_int(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(1, min(5, value))


def _bounded_confidence(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(0.0, min(1.0, float(value)))


def _text(value: Any, default: str, limit: int = 64) -> str:
    if not isinstance(value, str):
        return default
    value = value.strip()
    return value[:limit] or default


def analyze_observation(observation: Observation) -> InformationAnalysis:
    """Return a conservative, deterministic classification.

    Collectors may provide explicit values on ``Observation``. Attribute
    values are accepted for source adapters that cannot yet use the expanded
    model fields. No language model or network call occurs here.
    """

    attributes = observation.attributes
    importance = _bounded_int(attributes.get("importance", observation.importance), 3)
    urgency = _bounded_int(attributes.get("urgency", observation.urgency), 2)
    relevance = _bounded_int(attributes.get("relevance", observation.relevance), 3)
    confidence = _bounded_confidence(attributes.get("confidence", observation.confidence), 0.5)
    region = _text(attributes.get("region", observation.region), "GLOBAL", 16).upper()
    topic = _text(attributes.get("topic", observation.topic), "general", 80)
    source_tier = _text(attributes.get("source_tier", observation.source_tier), "secondary", 24)
    information_type = _text(
        attributes.get("information_type", observation.information_type), "report", 32
    )
    reasons: list[str] = []
    if source_tier == "primary":
        confidence = max(confidence, 0.8)
        reasons.append("一手来源")
    if relevance >= 4:
        reasons.append("命中关注区域或对象")
    if importance >= 4:
        reasons.append("影响范围较大")
    if urgency >= 4:
        reasons.append("时效性较高")

    # Existing explicit rule-generated alerts retain their behavior. For
    # unclassified observations the default is digest, never an interruption.
    if attributes.get("immediate") is True:
        handling = HANDLING_IMMEDIATE
        reasons.append("来源策略要求即时处理")
    elif importance >= 4 and urgency >= 4 and confidence >= 0.7:
        handling = HANDLING_IMMEDIATE
    elif importance >= 3 or relevance >= 3:
        handling = HANDLING_DIGEST
    else:
        handling = HANDLING_ARCHIVE

    return InformationAnalysis(
        importance=importance,
        urgency=urgency,
        relevance=relevance,
        confidence=confidence,
        region=region,
        topic=topic,
        source_tier=source_tier,
        information_type=information_type,
        handling=handling,
        reasons=tuple(dict.fromkeys(reasons)),
    )


def merge_advisory_analysis(
    baseline: InformationAnalysis, advisory: Mapping[str, Any]
) -> InformationAnalysis:
    """Merge untrusted analyzer output without allowing unsafe escalation.

    An advisory analyzer can move a score by one point, but it cannot create
    an immediate notification. The deterministic policy must independently
    meet the immediate threshold.
    """

    importance = _bounded_int(advisory.get("importance"), baseline.importance)
    urgency = _bounded_int(advisory.get("urgency"), baseline.urgency)
    relevance = _bounded_int(advisory.get("relevance"), baseline.relevance)
    importance = max(baseline.importance - 1, min(baseline.importance + 1, importance))
    urgency = max(baseline.urgency - 1, min(baseline.urgency + 1, urgency))
    relevance = max(baseline.relevance - 1, min(baseline.relevance + 1, relevance))
    confidence = min(
        baseline.confidence,
        _bounded_confidence(advisory.get("confidence"), baseline.confidence),
    )
    if baseline.importance >= 4 and baseline.urgency >= 4 and baseline.confidence >= 0.7:
        handling = HANDLING_IMMEDIATE
    elif importance >= 3 or relevance >= 3:
        handling = HANDLING_DIGEST
    else:
        handling = HANDLING_ARCHIVE
    rationale = _text(advisory.get("rationale"), "", 240)
    reasons = baseline.reasons + ((f"模型建议：{rationale}",) if rationale else ())
    return InformationAnalysis(
        importance=importance,
        urgency=urgency,
        relevance=relevance,
        confidence=confidence,
        region=_text(advisory.get("region"), baseline.region, 16).upper(),
        topic=_text(advisory.get("topic"), baseline.topic, 80),
        source_tier=baseline.source_tier,
        information_type=baseline.information_type,
        handling=handling,
        reasons=tuple(dict.fromkeys(reasons)),
    )
