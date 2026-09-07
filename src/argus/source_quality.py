"""Long-horizon source quality scoring.

Quality is deliberately separate from source availability and alert severity.
The score is based on explicit, auditable feedback and only becomes active
after a sufficiently long observation window.  It is intended to influence
digest ranking, never to suppress a deterministic immediate alert.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Mapping


@dataclass(frozen=True, slots=True)
class SourceQualityPolicy:
    """Conservative defaults for long-term reputation updates."""

    half_life_days: float = 90.0
    min_effective_samples: float = 30.0
    min_span_days: int = 90
    prior_positive: float = 8.0
    prior_negative: float = 2.0
    minimum_weight: float = 0.70
    maximum_weight: float = 1.15
    maximum_change_per_30_days: float = 0.05

    def __post_init__(self) -> None:
        if self.half_life_days <= 0:
            raise ValueError("quality half-life must be positive")
        if self.min_effective_samples < 1 or self.min_span_days < 1:
            raise ValueError("quality evidence thresholds must be positive")
        if self.prior_positive <= 0 or self.prior_negative <= 0:
            raise ValueError("quality priors must be positive")
        if not 0 < self.minimum_weight <= 1 <= self.maximum_weight:
            raise ValueError("quality weight bounds are invalid")
        if not 0 < self.maximum_change_per_30_days <= 1:
            raise ValueError("quality change limit is invalid")


@dataclass(frozen=True, slots=True)
class SourceQualityProfile:
    source_id: str
    weight: float
    score: float
    effective_samples: float
    positive_count: int
    negative_count: int
    neutral_count: int
    first_feedback_at: int | None
    last_feedback_at: int | None
    evidence_span_days: int
    eligible: bool
    manual_override: float | None = None
    updated_at: int | None = None


def calculate_quality(
    source_id: str,
    feedback: Iterable[Mapping[str, object]],
    *,
    now: int,
    policy: SourceQualityPolicy | None = None,
    current_weight: float = 1.0,
    manual_override: float | None = None,
    updated_at: int | None = None,
) -> SourceQualityProfile:
    """Calculate a bounded, slowly moving quality profile.

    Feedback is exponentially decayed with a 90-day default half-life, while
    eligibility additionally requires 30 effective samples spanning 90 days.
    A Bayesian prior prevents a small number of votes from moving a source.
    """

    if not source_id or len(source_id) > 128:
        raise ValueError("source ID is invalid")
    if now < 0:
        raise ValueError("quality calculation time is invalid")
    policy = policy or SourceQualityPolicy()
    positive = negative = neutral = 0
    weighted_positive = weighted_negative = weighted_neutral = 0.0
    timestamps: list[int] = []
    for row in feedback:
        try:
            signal = int(row.get("signal", 0))
            created_at = int(row.get("created_at", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("quality feedback row is invalid") from exc
        if signal not in {-1, 0, 1} or created_at < 0:
            raise ValueError("quality feedback row is invalid")
        age_days = max(0.0, (now - created_at) / 86400.0)
        decay = math.pow(0.5, age_days / policy.half_life_days)
        timestamps.append(created_at)
        if signal > 0:
            positive += 1
            weighted_positive += decay
        elif signal < 0:
            negative += 1
            weighted_negative += decay
        else:
            neutral += 1
            weighted_neutral += decay

    first = min(timestamps) if timestamps else None
    last = max(timestamps) if timestamps else None
    span_days = int((last - first) / 86400) if first is not None and last is not None else 0
    effective = weighted_positive + weighted_negative + weighted_neutral
    denominator = policy.prior_positive + policy.prior_negative + weighted_positive + weighted_negative
    score = (policy.prior_positive + weighted_positive) / denominator
    eligible = effective >= policy.min_effective_samples and span_days >= policy.min_span_days

    if manual_override is not None:
        target = max(policy.minimum_weight, min(policy.maximum_weight, float(manual_override)))
        weight = target
    elif not eligible:
        weight = 1.0
    else:
        # A neutral score of 0.8 preserves the normal weight. Negative evidence
        # can lower a source by up to 30%; positive evidence earns a smaller
        # reward so a popular but noisy source cannot dominate the digest.
        target = 1.0 + (score - 0.8) * 0.75
        target = max(policy.minimum_weight, min(policy.maximum_weight, target))
        previous = max(policy.minimum_weight, min(policy.maximum_weight, current_weight))
        elapsed_days = max(0.0, (now - updated_at) / 86400.0) if updated_at is not None else 30.0
        allowed = policy.maximum_change_per_30_days * max(1.0, elapsed_days / 30.0)
        weight = max(previous - allowed, min(previous + allowed, target))

    return SourceQualityProfile(
        source_id=source_id,
        weight=round(weight, 6),
        score=round(max(0.0, min(1.0, score)), 6),
        effective_samples=round(effective, 4),
        positive_count=positive,
        negative_count=negative,
        neutral_count=neutral,
        first_feedback_at=first,
        last_feedback_at=last,
        evidence_span_days=span_days,
        eligible=eligible or manual_override is not None,
        manual_override=manual_override,
        updated_at=now,
    )
