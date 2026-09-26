"""Conservative, deterministic fact candidates derived from one report.

This module is deliberately independent of event persistence.  It recognizes a
small reviewed vocabulary and returns no candidate when identity, scope, value,
or assertion status is ambiguous.  Callers remain responsible for attaching a
candidate to a canonical event and reconciling it with existing claims.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


FACT_PRODUCER = "argus.event_facts"
FACT_EXTRACTOR_VERSION = 1

_SPACE_RE = re.compile(r"\s+")
_SENTENCE_RE = re.compile(r"(?<!\d)[.!?。！？；;\n]+(?!\d)")
_QUOTATION_RE = re.compile(r"[\"“”「」『』]")
_SINGLE_QUOTATION_RE = re.compile(
    r"(?:^|[\s:])(?:'[^']{2,}'|‘[^’]{2,}’)(?:$|[\s.,!?])"
)
_UNSAFE_ASSERTION_RE = re.compile(
    r"\b(?:could|may|might|would|will|expected|expects?|forecast|forecasted|"
    r"projected|likely|possibly|plans?|planned|considering|minutes|transcript|"
    r"den(?:y|ies|ied)|not|never|no\s+decision|according\s+to|sources?\s+say|"
    r"analysts?\s+(?:say|expect)|says?|said|stated)\b|"
    r"预计|预期|预测|可能|或将|将会|计划|考虑|据悉|消息人士|分析师|称|表示|"
    r"会议纪要|会议记录|否认|并未|没有|不会|"
    r"見通し|予想|可能性|予定|検討|議事要旨|議事録|否定|述べ|していない|"
    r"しない|ないと述べ",
    re.IGNORECASE,
)
_HISTORICAL_RE = re.compile(
    r"\b(?:historical|historically|anniversary|years?\s+ago|decades?\s+ago|"
    r"previously|former|past)\b|历史上|历史回顾|周年|年前|かつて|過去|周年",
    re.IGNORECASE,
)
_IN_YEAR_RE = re.compile(r"\b(?:in\s+)?((?:18|19|20)\d{2})\b")
_INEXACT_NUMBER_RE = re.compile(
    r"\b(?:at\s+least|more\s+than|over|nearly|approximately|about)\s+\d|"
    r"(?:至少|超过|超過|逾|约|約)\s*\d|(?:少なくとも|およそ)\s*\d|\d\s*(?:人)?以上",
    re.IGNORECASE,
)

_INSTITUTIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "federal_reserve",
        re.compile(
            r"\b(?:federal\s+reserve|fomc|(?:the\s+)?(?-i:Fed))\b|美联储|美聯儲|"
            r"美国联邦储备(?:委员会)?|米連邦準備制度理事会|FRB",
            re.IGNORECASE,
        ),
    ),
    (
        "bank_of_japan",
        re.compile(r"\b(?:bank\s+of\s+japan|boj)\b|日本银行|日本銀行|日本央行|日银|日銀", re.IGNORECASE),
    ),
    (
        "european_central_bank",
        re.compile(r"\b(?:european\s+central\s+bank|ecb)\b|欧洲央行|歐洲央行|欧州中央銀行", re.IGNORECASE),
    ),
    (
        "peoples_bank_of_china",
        re.compile(
            r"\b(?:people(?:['’]s|s)\s+bank\s+of\s+china|pboc)\b|"
            r"中国人民银行|中國人民銀行|中国央行|中國央行",
            re.IGNORECASE,
        ),
    ),
)

_ACTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "raise",
        re.compile(
            r"\b(?:raises?|raised|hikes?|hiked|increases?|increased)\b.{0,48}"
            r"\b(?:(?:interest|policy|benchmark|key|target|federal\s+funds)\s+)?rates?\b|"
            r"(?:加息|上调.{0,16}利率|上調.{0,16}利率|提高.{0,16}利率|"
            r"利上げ|政策金利.{0,20}引き上げ|引き上げ.{0,20}政策金利)",
            re.IGNORECASE,
        ),
    ),
    (
        "cut",
        re.compile(
            r"\b(?:cuts?|cut|lowers?|lowered|reduces?|reduced)\b.{0,48}"
            r"\b(?:(?:interest|policy|benchmark|key|target|federal\s+funds)\s+)?rates?\b|"
            r"(?:降息|下调.{0,16}利率|下調.{0,16}利率|降低.{0,16}利率|"
            r"利下げ|政策金利.{0,20}引き下げ|引き下げ.{0,20}政策金利)",
            re.IGNORECASE,
        ),
    ),
    (
        "hold",
        re.compile(
            r"\b(?:holds?|held|keeps?|kept|maintains?|maintained)\b.{0,48}"
            r"\b(?:(?:interest|policy|benchmark|key|target|federal\s+funds)\s+)?rates?\b|"
            r"(?:维持.{0,16}利率|維持.{0,16}利率|利率.{0,10}不变|利率.{0,10}不變|"
            r"政策金利.{0,20}据え置き|政策金利.{0,20}据え置いた)",
            re.IGNORECASE,
        ),
    ),
)

_BASIS_POINT_RE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?:bp|bps|basis\s+points?|(?:个|個)?基点|"
    r"(?:個)?ベーシスポイント)",
    re.IGNORECASE,
)
_RATE_RANGE_RE = re.compile(
    r"(?P<low>\d+(?:\.\d+)?)\s*[%％]\s*(?P<separator>-|–|—|to|至|到|から)\s*"
    r"(?P<high>\d+(?:\.\d+)?)\s*[%％]",
    re.IGNORECASE,
)
_RATE_SINGLE_PATTERNS = (
    re.compile(r"\b(?:to|at)\s+(?P<value>\d+(?:\.\d+)?)\s*%", re.IGNORECASE),
    re.compile(r"(?:至|到|为|為|维持在|維持在)\s*(?P<value>\d+(?:\.\d+)?)\s*[%％]"),
    re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*[%％]\s*(?:に|とした|で据え置)"),
)

_USGS_SOURCE_ID = "usgs_earthquakes_significant_month"
_USGS_EXTERNAL_ID_RE = re.compile(
    r"^urn:earthquake-usgs-gov:[a-z0-9_-]+:([a-z0-9_-]+)$", re.IGNORECASE
)
_USGS_TITLE_RE = re.compile(r"^M\s+(?P<magnitude>\d+(?:\.\d+)?)\s+-\s+(?P<place>\S.+)$")
_USGS_TIME_RE = re.compile(r"\bTime\s+(?P<time>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+UTC\b")
_NON_POLICY_RATE_RE = re.compile(
    r"\b(?:tax|mortgage|exchange|growth|inflation|unemployment)\s+rates?\b",
    re.IGNORECASE,
)

_EVENT_NOUN_RE = re.compile(
    r"\b(?:earthquake|flood|explosion|blast|fire|wildfire|attack|bombing|crash|"
    r"derailment|landslide|storm|hurricane|typhoon|tsunami|war|collapse)\b|"
    r"地震|洪水|爆炸|爆発|火灾|火災|山火|袭击|襲撃|空袭|空襲|坠毁|墜落|"
    r"事故|山体滑坡|地滑り|风暴|暴風|台风|台風|海啸|津波|战争|戦争|倒塌|崩落",
    re.IGNORECASE,
)
_CASUALTY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?P<subject>[A-Za-z0-9][A-Za-z0-9 ,()/-]{1,100}?)\s+"
        r"(?:death\s+toll|fatalit(?:y|ies)\s+count)\s+"
        r"(?:has\s+|have\s+)?(?:risen|rose|rises|reached|reaches|stands|stood|is|was)"
        r"(?:\s+at|\s+to)?\s+(?P<count>\d[\d,]*)"
        r"(?:\s+(?:people|persons|deaths|fatalities|dead))?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<count>\d[\d,]*)\s+(?:people|persons)\s+(?:were\s+|are\s+|have\s+been\s+)?"
        r"(?:killed|dead)\s+(?:in|after|from)\s+"
        r"(?P<subject>[A-Za-z0-9][A-Za-z0-9 ,()/-]{1,100})",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<subject>[\u3400-\u9fffA-Za-z0-9·（）()、，, -]{2,80}?)"
        r"(?:的)?(?:死亡人数|死亡人數|遇难人数|遇難人數)"
        r"(?:已)?(?:升至|增至|达到|達到|为|為|是)\s*"
        r"(?P<count>\d[\d,]*)\s*(?:人|名)"
    ),
    re.compile(
        r"(?P<subject>[\u3400-\u9fffA-Za-z0-9·（）()、，, -]{2,80}?)"
        r"(?:已)?造成\s*(?P<count>\d[\d,]*)\s*(?:人|名)(?:死亡|遇难|遇難)"
    ),
    re.compile(
        r"(?P<subject>[\u3400-\u9fff\u3040-\u30ffA-Za-z0-9・（）()、，, -]{2,80}?)"
        r"(?:で|の)(?:死者|死亡者)(?:は|が)?\s*(?P<count>\d[\d,]*)\s*人"
    ),
    re.compile(
        r"(?P<subject>[\u3400-\u9fff\u3040-\u30ffA-Za-z0-9・（）()、，, -]{2,80}?)"
        r"で\s*(?P<count>\d[\d,]*)\s*人(?:が)?死亡"
    ),
)


def _canonical_text(value: Any) -> str:
    return _SPACE_RE.sub(" ", unicodedata.normalize("NFKC", str(value or ""))).strip()


def _canonical_decimal(value: str) -> str | None:
    try:
        number = Decimal(unicodedata.normalize("NFKC", value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    rendered = format(number.normalize(), "f")
    return "0" if rendered in {"-0", ""} else rendered


def _stable_key(prefix: str, version: int, material: Mapping[str, object]) -> str:
    payload = json.dumps(material, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode()).hexdigest()[:32]
    return f"{prefix}:v{version}:{digest}"


@dataclass(frozen=True, slots=True)
class FactExtractionInput:
    """Minimal immutable report surface required by the pure extractor.

    ``published_at`` timestamps the evidence report. ``effective_at`` is only
    set when an upstream structured source explicitly supplies the fact's
    effective time; the extractor never treats publication as occurrence.
    """

    source_id: str
    external_id: str
    title: str
    summary: str
    published_at: int
    attributes: Mapping[str, object] = field(default_factory=dict)
    effective_at: int | None = None

    def __post_init__(self) -> None:
        if self.published_at < 1 or (self.effective_at is not None and self.effective_at < 1):
            raise ValueError("fact input timestamp is invalid")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> FactExtractionInput:
        published = value.get("published_at")
        if published is None:
            raise ValueError("fact input timestamp is missing")
        if isinstance(published, datetime):
            normalized = (
                published.replace(tzinfo=UTC)
                if published.tzinfo is None
                else published.astimezone(UTC)
            )
            published = int(normalized.timestamp())
        attributes = value.get("attributes", {})
        if not isinstance(attributes, Mapping):
            attributes = {}
        effective = value.get("effective_at")
        if isinstance(effective, datetime):
            normalized = (
                effective.replace(tzinfo=UTC)
                if effective.tzinfo is None
                else effective.astimezone(UTC)
            )
            effective = int(normalized.timestamp())
        return cls(
            source_id=str(value.get("source_id", "")),
            external_id=str(value.get("external_id", "")),
            title=str(value.get("title", "")),
            summary=str(value.get("summary", "")),
            published_at=int(published),
            attributes=attributes,
            effective_at=int(effective) if effective is not None else None,
        )


@dataclass(frozen=True, slots=True)
class EventFactCandidate:
    """A normalized proposition awaiting event-local persistence and reconciliation.

    ``effective_at`` remains ``None`` when the proposition has no explicit
    occurrence/effective time. Persistence must not substitute report time.
    """

    kind: str
    subject: str
    predicate: str
    scope: str
    value: str
    unit: str
    polarity: str
    effective_at: int | None
    evidence_span: str
    producer: str = FACT_PRODUCER
    version: int = FACT_EXTRACTOR_VERSION
    slot_key: str = field(init=False)
    claim_key: str = field(init=False)

    def __post_init__(self) -> None:
        fields = (
            self.kind,
            self.subject,
            self.predicate,
            self.scope,
            self.value,
            self.unit,
            self.polarity,
            self.evidence_span,
            self.producer,
        )
        if (
            any(not item.strip() for item in fields)
            or (self.effective_at is not None and self.effective_at < 1)
            or self.version < 1
        ):
            raise ValueError("fact candidate is incomplete")
        if self.polarity not in {"asserted", "negated"}:
            raise ValueError("fact candidate polarity is invalid")
        if (
            any(len(item) > 80 for item in (self.kind, self.predicate, self.unit, self.polarity))
            or any(len(item) > 512 for item in (self.subject, self.scope, self.value))
            or len(self.evidence_span) > 4000
        ):
            raise ValueError("fact candidate exceeds size bounds")
        slot_material = {
            "kind": self.kind,
            "predicate": self.predicate,
            "scope": self.scope,
            "subject": self.subject,
        }
        slot_key = _stable_key("slot", self.version, slot_material)
        claim_key = _stable_key(
            "claim",
            self.version,
            {
                **slot_material,
                "polarity": self.polarity,
                "unit": self.unit,
                "value": self.value,
            },
        )
        object.__setattr__(self, "slot_key", slot_key)
        object.__setattr__(self, "claim_key", claim_key)


def _segments(source: FactExtractionInput) -> tuple[str, ...]:
    values: list[str] = []
    for raw in (source.title, source.summary):
        for segment in _SENTENCE_RE.split(_canonical_text(raw)):
            if segment and segment not in values:
                values.append(segment)
    return tuple(values)


def _is_asserted(segment: str, published_at: int) -> bool:
    if (
        _QUOTATION_RE.search(segment)
        or _SINGLE_QUOTATION_RE.search(segment)
        or _UNSAFE_ASSERTION_RE.search(segment)
    ):
        return False
    if _HISTORICAL_RE.search(segment):
        return False
    published_year = datetime.fromtimestamp(published_at, UTC).year
    years = {int(match.group(1)) for match in _IN_YEAR_RE.finditer(segment)}
    return not years or years == {published_year}


def _central_bank_candidates(source: FactExtractionInput) -> list[EventFactCandidate]:
    candidates: list[EventFactCandidate] = []
    for segment in _segments(source):
        if not _is_asserted(segment, source.published_at):
            continue
        if _NON_POLICY_RATE_RE.search(segment) or _INEXACT_NUMBER_RE.search(segment):
            continue
        institutions = [name for name, pattern in _INSTITUTIONS if pattern.search(segment)]
        actions = [name for name, pattern in _ACTION_PATTERNS if pattern.search(segment)]
        if len(institutions) != 1 or len(actions) != 1:
            continue
        subject = institutions[0]
        action = actions[0]
        basis_points = list(_BASIS_POINT_RE.finditer(segment))
        if len(basis_points) == 1 and action in {"raise", "cut"}:
            value = _canonical_decimal(basis_points[0].group("value"))
            if value is not None and Decimal(value) > 0 and Decimal(value) <= 10_000:
                if action == "cut":
                    value = f"-{value}"
                candidates.append(EventFactCandidate(
                    kind="central_bank_decision",
                    subject=subject,
                    predicate="policy_rate_change",
                    scope="decision",
                    value=value,
                    unit="basis_point",
                    polarity="asserted",
                    effective_at=source.effective_at,
                    evidence_span=segment,
                ))
        ranges = []
        for match in _RATE_RANGE_RE.finditer(segment):
            prefix = segment[max(0, match.start() - 8):match.start()]
            is_transition = bool(re.search(r"(?:\bfrom|从|從|由)\s*$", prefix, re.IGNORECASE))
            if match.group("separator") == "から" and action in {"raise", "cut"}:
                is_transition = True
            if not is_transition:
                ranges.append(match)
        singles = [
            match for pattern in _RATE_SINGLE_PATTERNS for match in pattern.finditer(segment)
        ]
        if len(ranges) == 1:
            low = _canonical_decimal(ranges[0].group("low"))
            high = _canonical_decimal(ranges[0].group("high"))
            if (
                low is not None and high is not None
                and Decimal("0") <= Decimal(low) <= Decimal(high) <= Decimal("100")
            ):
                candidates.append(EventFactCandidate(
                    kind="central_bank_decision",
                    subject=subject,
                    predicate="policy_rate_target",
                    scope="decision",
                    value=f"{low}..{high}",
                    unit="percent_range",
                    polarity="asserted",
                    effective_at=source.effective_at,
                    evidence_span=segment,
                ))
        elif not ranges and len(singles) == 1:
            value = _canonical_decimal(singles[0].group("value"))
            if value is not None and Decimal("0") <= Decimal(value) <= Decimal("100"):
                candidates.append(EventFactCandidate(
                    kind="central_bank_decision",
                    subject=subject,
                    predicate="policy_rate_target",
                    scope="decision",
                    value=value,
                    unit="percent",
                    polarity="asserted",
                    effective_at=source.effective_at,
                    evidence_span=segment,
                ))
    return candidates


def _usgs_candidates(source: FactExtractionInput) -> list[EventFactCandidate]:
    # The current RSS adapter preserves only section/published_at_inferred in
    # attributes.  Magnitude is therefore read only from USGS's source-gated,
    # structured Atom title; no imaginary attribute contract is assumed here.
    if source.source_id != _USGS_SOURCE_ID:
        return []
    external = _USGS_EXTERNAL_ID_RE.fullmatch(source.external_id.strip())
    title = _USGS_TITLE_RE.fullmatch(_canonical_text(source.title))
    times = list(_USGS_TIME_RE.finditer(_canonical_text(source.summary)))
    if external is None or title is None or len(times) != 1:
        return []
    magnitude = _canonical_decimal(title.group("magnitude"))
    if magnitude is None or not Decimal("0") <= Decimal(magnitude) <= Decimal("10"):
        return []
    try:
        occurred_at = int(datetime.strptime(
            times[0].group("time"), "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=UTC).timestamp())
    except ValueError:
        return []
    return [EventFactCandidate(
        kind="earthquake_measurement",
        subject="earthquake",
        predicate="magnitude",
        scope=f"usgs:{external.group(1).casefold()}",
        value=magnitude,
        unit="magnitude",
        polarity="asserted",
        effective_at=occurred_at,
        evidence_span=_canonical_text(source.title),
    )]


def _casualty_subject(value: str) -> str | None:
    subject = _canonical_text(value).strip(" ,-()")
    subject = re.sub(r"^(?:breaking|update|latest)\s*[:,-]?\s*", "", subject, flags=re.IGNORECASE)
    subject = re.sub(r"^(?:最新消息|快讯|速報)[:：,，]?", "", subject)
    if not subject or len(subject) > 160 or not _EVENT_NOUN_RE.search(subject):
        return None
    return subject.casefold()


def _casualty_candidates(source: FactExtractionInput) -> list[EventFactCandidate]:
    candidates: list[EventFactCandidate] = []
    for segment in _segments(source):
        if not _is_asserted(segment, source.published_at):
            continue
        if _INEXACT_NUMBER_RE.search(segment):
            continue
        matches = [match for pattern in _CASUALTY_PATTERNS for match in pattern.finditer(segment)]
        if len(matches) != 1:
            continue
        match = matches[0]
        subject = _casualty_subject(match.group("subject"))
        count = _canonical_decimal(match.group("count"))
        if subject is None or count is None or Decimal(count) != Decimal(count).to_integral_value():
            continue
        if not Decimal("0") <= Decimal(count) <= Decimal("1000000000"):
            continue
        candidates.append(EventFactCandidate(
            kind="casualty_count",
            subject=subject,
            predicate="death_toll",
            scope="total",
            value=count,
            unit="person",
            polarity="asserted",
            effective_at=source.effective_at,
            evidence_span=segment,
        ))
    return candidates


def extract_event_facts(
    source: FactExtractionInput | Mapping[str, Any],
) -> tuple[EventFactCandidate, ...]:
    """Return a stable, duplicate-free tuple of reviewed facts for one report."""
    try:
        item = (
            source
            if isinstance(source, FactExtractionInput)
            else FactExtractionInput.from_mapping(source)
        )
    except (TypeError, ValueError, OverflowError):
        return ()
    by_claim: dict[str, EventFactCandidate] = {}
    for candidate in (
        *_central_bank_candidates(item),
        *_usgs_candidates(item),
        *_casualty_candidates(item),
    ):
        existing = by_claim.get(candidate.claim_key)
        if existing is None or candidate.evidence_span < existing.evidence_span:
            by_claim[candidate.claim_key] = candidate
    return tuple(
        sorted(by_claim.values(), key=lambda candidate: (candidate.slot_key, candidate.claim_key))
    )


__all__ = [
    "EventFactCandidate",
    "FACT_EXTRACTOR_VERSION",
    "FACT_PRODUCER",
    "FactExtractionInput",
    "extract_event_facts",
]
