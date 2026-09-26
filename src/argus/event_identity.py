"""Deterministic identities for high-value, repeatedly reported events.

The rules in this module are deliberately narrow.  They provide a shared
identity to clustering and notification suppression only when the text names a
known institution and a concrete decision.  Unmatched stories continue through
the general similarity scorer.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping
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


@dataclass(frozen=True, slots=True)
class WeatherEventIdentity:
    """Stable identity for a JMA warning update sequence.

    JMAXML publishes several bulletin types for the same warning and republishes
    them as the situation changes.  The identity intentionally includes the
    hazard, severity and normalized area so a real escalation or expansion can
    notify while routine re-publication stays attached to one incident.
    """

    hazard: str
    severity: str
    area: str
    system: str = ""

    def notification_key(self) -> str:
        # Some JMA bulletin types name the typhoon while the corresponding
        # warning bulletin does not. Area + hazard + severity therefore form
        # the stable notification identity; ``system`` remains useful context
        # but must not split those equivalent bulletins.
        material = "|".join((self.area, self.hazard, self.severity))
        return "weather-event:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class EventOccurrenceIdentity:
    """Evidence-backed occurrence identity, separate from a mutable event title."""

    kind: str
    namespace: str
    external_id: str | None
    occurred_at: int | None
    time_precision: str | None
    entity_ids: tuple[str, ...]
    location_id: str | None
    object_id: str | None
    basis: str

    @property
    def key(self) -> str:
        if self.basis == "official_id":
            material: tuple[Any, ...] = (self.kind, self.namespace, self.external_id)
        else:
            material = (
                self.kind, self.entity_ids, self.location_id, self.object_id,
                self.occurred_at, self.time_precision,
            )
        encoded = json.dumps(material, ensure_ascii=True, separators=(",", ":"))
        return "occurrence:" + hashlib.sha256(encoded.encode("ascii")).hexdigest()


_OCCURRENCE_ATOM = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}\Z")
_USGS_URN = re.compile(r"urn:earthquake-usgs-gov:([a-z0-9_-]+):([a-z0-9_-]+)\Z", re.IGNORECASE)
_USGS_SOURCE = "usgs_earthquakes_significant_month"


def _occurrence_atom(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold()
    return normalized if _OCCURRENCE_ATOM.fullmatch(normalized) else None


def identify_event_occurrence(observation: Mapping[str, Any]) -> EventOccurrenceIdentity | None:
    """Use only source-gated official IDs or explicit, validated occurrence metadata.

    A feed GUID and publication timestamp alone are never an occurrence identity.
    The structured contract is supplied by a reviewed primary collector as
    ``attributes.event_identity``; this function does not infer facts from prose.
    """
    source_id = str(observation.get("source_id", ""))
    if source_id == _USGS_SOURCE:
        match = _USGS_URN.fullmatch(str(observation.get("external_id", "")).strip())
        if match is not None:
            return EventOccurrenceIdentity(
                kind="earthquake", namespace="usgs", external_id=f"{match.group(1)}:{match.group(2)}".casefold(),
                occurred_at=None, time_precision=None, entity_ids=(), location_id=None,
                object_id=None, basis="official_id",
            )

    attributes = observation.get("attributes")
    if not isinstance(attributes, Mapping) or not isinstance(attributes.get("event_identity"), Mapping):
        return None
    if str(observation.get("source_tier", "")).casefold() != "primary":
        return None
    raw = attributes["event_identity"]
    kind = _occurrence_atom(raw.get("kind"))
    if kind is None:
        return None
    namespace = _occurrence_atom(raw.get("namespace"))
    external_id = _occurrence_atom(raw.get("external_id"))
    if namespace is not None and external_id is not None:
        if namespace != source_id.casefold():
            return None
        return EventOccurrenceIdentity(
            kind=kind, namespace=namespace, external_id=external_id,
            occurred_at=None, time_precision=None, entity_ids=(), location_id=None,
            object_id=None, basis="official_id",
        )
    if namespace is not None or external_id is not None:
        return None

    entities_raw = raw.get("entity_ids", ())
    if not isinstance(entities_raw, (list, tuple)):
        return None
    normalized_entities = {_occurrence_atom(value) for value in entities_raw}
    if None in normalized_entities:
        return None
    entities = tuple(sorted(value for value in normalized_entities if value is not None))
    location = _occurrence_atom(raw.get("location_id"))
    object_id = _occurrence_atom(raw.get("object_id"))
    occurred_at = raw.get("occurred_at")
    precision = raw.get("time_precision")
    units = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}
    if (
        type(occurred_at) is not int or occurred_at < 0 or not isinstance(precision, str)
        or precision not in units
        or occurred_at % units[precision] != 0 or not location
        or not (entities or object_id)
    ):
        return None
    return EventOccurrenceIdentity(
        kind=kind, namespace="structured", external_id=None,
        occurred_at=occurred_at, time_precision=precision,
        entity_ids=entities, location_id=location, object_id=object_id,
        basis="structured",
    )


def event_occurrences_compatible(
    left: EventOccurrenceIdentity | None, right: EventOccurrenceIdentity | None,
) -> bool | None:
    """Return unknown when either report lacks a validated occurrence identity."""
    if left is None or right is None:
        return None
    return left.key == right.key


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

_JMA_TYPOON = re.compile(r"台風\s*(?:第\s*)?(?P<number>\d+)\s*号|typhoon\s*(?:no\.?\s*)?(?P<latin>\d+)", re.IGNORECASE)
_JMA_SEVERITY = (
    ("cleared", re.compile(r"解除|解除しました|解除されました", re.IGNORECASE)),
    ("special-warning", re.compile(r"特別警報|大津波警報|大噴火警報|special warning|major tsunami", re.IGNORECASE)),
    ("warning", re.compile(r"警報|津波警報|噴火警報|warning", re.IGNORECASE)),
    ("advisory", re.compile(r"注意報|津波注意報|噴火警戒|advisory", re.IGNORECASE)),
)
_JMA_HAZARDS = (
    ("landslide", re.compile(r"土砂災害|土砂", re.IGNORECASE)),
    ("flood", re.compile(r"洪水|浸水", re.IGNORECASE)),
    ("heavy-rain", re.compile(r"大雨|豪雨|線状降水帯", re.IGNORECASE)),
    ("wind", re.compile(r"暴風|強風|竜巻", re.IGNORECASE)),
    ("storm-surge", re.compile(r"高潮", re.IGNORECASE)),
    ("wave", re.compile(r"波浪|高波|うねり", re.IGNORECASE)),
    ("snow", re.compile(r"大雪|暴風雪|雪崩", re.IGNORECASE)),
    ("tsunami", re.compile(r"津波", re.IGNORECASE)),
    ("volcano", re.compile(r"噴火|火山", re.IGNORECASE)),
    ("earthquake", re.compile(r"地震|震度", re.IGNORECASE)),
)
_JMA_AREAS = (
    # Tokyo-area bulletins alternate between prefecture-wide and island names
    # for the same warning sequence; keep them in one conservative scope.
    ("tokyo-izu", re.compile(r"伊豆諸島|大島|八丈島|三宅島|東京都|東京地方")),
    ("kanto-koshin", re.compile(r"関東甲信")),
    ("tohoku", re.compile(r"東北")),
    ("hokkaido", re.compile(r"北海道")),
    ("kyushu", re.compile(r"九州")),
    ("okinawa", re.compile(r"沖縄")),
)


def identify_weather_event(title: str, summary: str = "", source_id: str = "") -> WeatherEventIdentity | None:
    """Identify a JMA warning sequence without treating bulletin labels as events."""
    if source_id != "japan_meteorological_agency_high_frequency":
        return None
    text = " ".join((title, summary)).casefold()
    severity = next((name for name, pattern in _JMA_SEVERITY if pattern.search(text)), None)
    hazard = next((name for name, pattern in _JMA_HAZARDS if pattern.search(text)), None)
    if severity is None or hazard is None:
        return None
    storm = _JMA_TYPOON.search(text)
    system = f"typhoon-{storm.group('number') or storm.group('latin')}" if storm else ""
    area = next((name for name, pattern in _JMA_AREAS if pattern.search(text)), "unspecified")
    if not system and area == "unspecified":
        return None
    return WeatherEventIdentity(hazard=hazard, severity=severity, area=area, system=system)


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
