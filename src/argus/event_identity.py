"""Deterministic identities for high-value, repeatedly reported events.

The rules in this module are deliberately narrow.  They provide a shared
identity to clustering and notification suppression only when the text names a
known institution and a concrete decision.  Unmatched stories continue through
the general similarity scorer.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo


@dataclass(frozen=True, slots=True)
class SemanticEventIdentity:
    family: str
    entity: str
    timezone: str
    relation_hint: str = "corroborates"
    action: str = "announcement"

    @property
    def match_key(self) -> str:
        return f"{self.family}:{self.entity}"

    def dated_key(self, published_at: int) -> str:
        instant = datetime.fromtimestamp(max(0, published_at), UTC)
        local_date = instant.astimezone(ZoneInfo(self.timezone)).date().isoformat()
        return f"{self.match_key}:{local_date}"

    def notification_key(self, published_at: int) -> str:
        material = f"{self.dated_key(published_at)}\x1f{self.action}"
        return "news-event:" + hashlib.sha256(material.encode("utf-8")).hexdigest()

    def compatible(self, other: SemanticEventIdentity, left_at: int, right_at: int) -> bool:
        return (
            self.dated_key(left_at) == other.dated_key(right_at)
            and abs(left_at - right_at) <= 6 * 3600
            and (self.action == other.action or "announcement" in {self.action, other.action})
        )


_CENTRAL_BANKS = (
    ("federal-reserve", "America/New_York", re.compile(
        r"\b(?:federal reserve(?: board)?|federal open market committee|fomc|fed)\b",
        re.IGNORECASE,
    )),
    ("ecb", "Europe/Berlin", re.compile(
        r"\b(?:european central bank|ecb)\b", re.IGNORECASE,
    )),
    ("bank-of-japan", "Asia/Tokyo", re.compile(
        r"\b(?:bank of japan|boj)\b|日本銀行|日銀", re.IGNORECASE,
    )),
    ("pboc", "Asia/Shanghai", re.compile(
        r"\b(?:people'?s bank of china|pboc)\b|中国人民银行", re.IGNORECASE,
    )),
    ("bank-of-england", "Europe/London", re.compile(
        r"\b(?:bank of england|boe)\b", re.IGNORECASE,
    )),
)

_RATE_ACTION = re.compile(
    r"\b(?:raises?|raised|hikes?|hiked|increases?|increased|cuts?|cut|lowers?|lowered|"
    r"holds?|held|keeps?|kept|maintains?|maintained|leaves?|left)\b[-\s]+"
    r"(?:(?:its|the|benchmark|key|official|target|policy|interest|federal|fed|funds|bank|lending)[-\s]+){0,4}rates?\b|"
    r"\brates?\b\s+(?:(?:were|was|are|is|remain|remains|remained)\s+)?"
    r"\b(?:raised|hiked|increased|cut|lowered|held|kept|maintained|unchanged)\b|"
    r"\bdefies\b.{0,50}\bwith (?:its )?(?:first )?rate (?:rise|cut)\b",
    re.IGNORECASE,
)
_POLICY_RELEASE = re.compile(
    r"\b(?:issues? (?:an? )?fomc statement|releases? (?:an? )?"
    r"(?:monetary policy statement|economic projections)|"
    r"(?:announces?|publishes?) (?:its )?(?:rate decision|monetary policy decision))\b",
    re.IGNORECASE,
)
_SPECULATION_ONLY = re.compile(
    r"\b(?:may|might|could|should|will|would|expected to|likely to|forecast to|set to)\s+"
    r"(?:raise|hike|increase|cut|lower|hold|keep)\b|"
    r"\b(?:preview|predicts?|forecasts?|last (?:week|month|year)|yesterday)\b",
    re.IGNORECASE,
)
_MARKET_REACTION = re.compile(
    r"\b(?:dollar|treasur(?:y|ies)|bonds?|yields?|stocks?|equities|markets?|gold|oil)\b"
    r".{0,80}\b(?:after|as|following|on)\b|"
    r"\b(?:jumps?|falls?|gains?|slides?|surges?|tumbles?|rallies?)\b.{0,80}"
    r"\b(?:fed|fomc|ecb|boj|boe|pboc|central bank)\b",
    re.IGNORECASE,
)


def identify_semantic_event(
    title: str,
    summary: str = "",
) -> SemanticEventIdentity | None:
    """Return a conservative shared identity for a concrete known event."""
    text = " ".join(title.split())
    if not text:
        return None
    institutions = [(entity, timezone) for entity, timezone, pattern in _CENTRAL_BANKS if pattern.search(text)]
    if len(institutions) != 1:
        return None
    institution = institutions[0]
    has_action = _RATE_ACTION.search(text) is not None
    has_release = _POLICY_RELEASE.search(text) is not None
    if _SPECULATION_ONLY.search(text) is not None:
        return None
    if not has_action and not has_release:
        return None
    relation = "context" if _MARKET_REACTION.search(text) is not None else "corroborates"
    action = "announcement"
    match = _RATE_ACTION.search(text)
    if match is not None:
        verbs = re.findall(
            r"\b(?:raises?|raised|hikes?|hiked|increases?|increased|cuts?|cut|lowers?|lowered|"
            r"holds?|held|keeps?|kept|maintains?|maintained|leaves?|left|unchanged|rise)\b",
            match.group(0), re.I,
        )
        action_text = verbs[-1].casefold()
        if re.fullmatch(r"raises?|raised|hikes?|hiked|increases?|increased|rise", action_text):
            action = "raise"
        elif re.fullmatch(r"cuts?|cut|lowers?|lowered", action_text):
            action = "cut"
        else:
            action = "hold"
    return SemanticEventIdentity(
        family="central-bank-rate-decision",
        entity=institution[0],
        timezone=institution[1],
        relation_hint=relation,
        action=action,
    )
