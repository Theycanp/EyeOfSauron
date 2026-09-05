from __future__ import annotations

import hashlib
import re
from datetime import datetime

from .config import WeightedTextRuleConfig
from .models import AlertCandidate, Observation
from .util import to_epoch, truncate


class WeightedTextRule:
    def __init__(self, config: WeightedTextRuleConfig, default_topic: str) -> None:
        self.config = config
        self.default_topic = default_topic
        self._patterns = tuple((pattern, re.compile(pattern.regex)) for pattern in config.patterns)

    @property
    def source_ids(self) -> tuple[str, ...]:
        return self.config.source_ids

    def evaluate(self, observation: Observation, now: int) -> AlertCandidate | None:
        if observation.source_id not in self.config.source_ids:
            return None
        published = to_epoch(observation.published_at)
        if published < now - self.config.max_item_age_seconds or published > now + 3600:
            return None

        score = 0.0
        reasons: list[str] = []
        for pattern, expression in self._patterns:
            title_match = expression.search(observation.title) is not None
            summary_match = expression.search(observation.summary) is not None
            if not title_match and not summary_match:
                continue
            weight = pattern.title_weight if title_match else pattern.summary_weight
            score += weight
            reasons.append(pattern.label)

        if score < self.config.threshold:
            return None

        identity = f"{self.config.id}\x1f{observation.dedupe_scope}\x1f{observation.external_id}"
        dedupe_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        # Prefer stable domain identities when a collector has one. A headline
        # fallback is retained for generic RSS, but is scoped by the rule so
        # unrelated publishers cannot collapse into the same incident.
        attributes = observation.attributes
        explicit_identity = str(attributes.get("incident_key", "")).strip()
        if not explicit_identity and attributes.get("check"):
            explicit_identity = f"check:{attributes['check']}"
        if not explicit_identity and attributes.get("symbol"):
            event_types = attributes.get("event_types", attributes.get("recovered", []))
            if isinstance(event_types, (list, tuple)):
                explicit_identity = f"symbol:{attributes['symbol']}:{','.join(map(str, event_types))}"
            else:
                explicit_identity = f"symbol:{attributes['symbol']}"
        normalized = re.sub(r"[^a-z0-9 ]+", " ", observation.title.lower())
        normalized = re.sub(r"\s+", " ", normalized).strip()[:240]
        normalized = explicit_identity or normalized
        incident_key = hashlib.sha256(
            f"{self.config.id}\x1f{normalized}".encode("utf-8")
        ).hexdigest()
        stateful = bool(attributes.get("stateful") or attributes.get("recovery"))
        section = str(observation.attributes.get("section", "")).strip()
        source_label = observation.publisher if not section else f"{observation.publisher} · {section}"
        summary = truncate(observation.summary, 700)
        reason_text = "、".join(dict.fromkeys(reasons))
        body_parts = [part for part in (summary, f"来源：{source_label}", f"判断依据：{reason_text}") if part]
        return AlertCandidate(
            rule_id=self.config.id,
            dedupe_key=dedupe_key,
            title=truncate(f"{self.config.notification_title}：{observation.title}", 250),
            message="\n\n".join(body_parts),
            priority=self.config.priority,
            tags=self.config.tags,
            click_url=observation.url,
            topic=self.config.topic or self.default_topic,
            confidence=max(0.0, min(1.0, score / max(self.config.threshold * 2, 1.0))),
            evidence=tuple(dict.fromkeys(reasons)),
            incident_key=incident_key,
            incident_kind="stateful" if stateful else "event",
            recovery=stateful and bool(attributes.get("recovery")),
        )


class RuleSet:
    def __init__(self, rules: tuple[WeightedTextRule, ...]) -> None:
        self.rules = rules

    def evaluate(self, observation: Observation, now: int) -> tuple[AlertCandidate, ...]:
        matches = []
        for rule in self.rules:
            candidate = rule.evaluate(observation, now)
            if candidate is not None:
                matches.append(candidate)
        return tuple(matches)

    @classmethod
    def from_config(cls, rule_configs: tuple[WeightedTextRuleConfig, ...], default_topic: str) -> "RuleSet":
        return cls(tuple(WeightedTextRule(config, default_topic) for config in rule_configs))
