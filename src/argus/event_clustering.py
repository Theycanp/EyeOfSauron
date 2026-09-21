"""Deterministic, order-independent event clustering.

This module is deliberately storage agnostic.  It turns raw observation-like
mappings into stable event projections while retaining each source report as
parallel evidence.  The implementation is conservative: a report is only
attached when there is enough shared signal, and complete-linkage guards avoid
the classic A~B~C chain merge.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import Any, Mapping, Sequence

from .event_identity import identify_semantic_event


_SPACE_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_NUMBER_RE = re.compile(
    r"(?P<number>\d+(?:[.,]\d+)?)\s*(?P<unit>%|％|bp|bps|basis\s+points?|基点|个基点|亿元|万亿元|亿|万亿|百万|十亿|"
    r"trillion|billion|million|thousand|bn|mn|tn)?",
    re.IGNORECASE,
)
_CJK_RUN_RE = re.compile(r"[\u3400-\u9fff]{2,}")
_STOPWORDS = frozenset(
    "表示 发布 关于 相关 记者 消息 最新 今日 日前 将在 以及 进行 召开".split()
)
_GENERIC_TITLES = frozenset({
    "politics", "business", "world", "news", "technology", "the weekly cartoon",
    "latest news", "breaking news", "新闻", "国际新闻", "财经", "时政", "科技",
})

# A small, explicit vocabulary makes the deterministic matcher useful across
# the languages used by the reviewed source set.  It is deliberately limited
# to stable institutions, actions and event nouns; it must never become a
# machine translation system or turn a shared generic word into an event key.
_MULTILINGUAL_ALIASES = (
    ("federal reserve", "entity_federal_reserve"),
    ("fomc", "entity_federal_reserve"),
    ("fed", "entity_federal_reserve"),
    ("federal funds", "policy_rate"),
    ("interest rate", "policy_rate"),
    ("bank of japan", "entity_bank_of_japan"),
    ("boj", "entity_bank_of_japan"),
    ("european central bank", "entity_european_central_bank"),
    ("ecb", "entity_european_central_bank"),
    ("people's bank of china", "entity_people_bank_of_china"),
    ("peoples bank of china", "entity_people_bank_of_china"),
    ("pboc", "entity_people_bank_of_china"),
    ("finance ministry", "entity_finance_ministry"),
    ("ministry of finance", "entity_finance_ministry"),
    ("industrial and commercial bank of china", "entity_icbc"),
    ("icbc", "entity_icbc"),
    ("central bank", "central_bank"),
    ("美联储", "entity_federal_reserve"),
    ("联储", "entity_federal_reserve"),
    ("日银", "entity_bank_of_japan"),
    ("日銀", "entity_bank_of_japan"),
    ("日本银行", "entity_bank_of_japan"),
    ("日本銀行", "entity_bank_of_japan"),
    ("欧洲央行", "entity_european_central_bank"),
    ("中国人民银行", "entity_people_bank_of_china"),
    ("财政部", "entity_finance_ministry"),
    ("财务省", "entity_finance_ministry"),
    ("工商银行", "entity_icbc"),
    ("中国工商银行", "entity_icbc"),
    ("日本央行", "entity_bank_of_japan"),
    ("日元", "entity_japanese_yen"),
    ("日圆", "entity_japanese_yen"),
    ("央行", "central_bank"),
    ("加息", "action_rate_hike"),
    ("上调利率", "action_rate_hike"),
    ("利上げ", "action_rate_hike"),
    ("rate hike", "action_rate_hike"),
    ("rate hikes", "action_rate_hike"),
    ("raises rates", "action_rate_hike"),
    ("raise rates", "action_rate_hike"),
    ("raises interest rates", "action_rate_hike"),
    ("raise interest rates", "action_rate_hike"),
    ("hikes interest rates", "action_rate_hike"),
    ("hike interest rates", "action_rate_hike"),
    ("raises policy rate", "action_rate_hike"),
    ("raise policy rate", "action_rate_hike"),
    ("hikes policy rate", "action_rate_hike"),
    ("raised rates", "action_rate_hike"),
    ("hiked rates", "action_rate_hike"),
    ("提高利率", "action_rate_hike"),
    ("利率上调", "action_rate_hike"),
    ("降息", "action_rate_cut"),
    ("下调利率", "action_rate_cut"),
    ("利下げ", "action_rate_cut"),
    ("rate cut", "action_rate_cut"),
    ("rate cuts", "action_rate_cut"),
    ("cuts rates", "action_rate_cut"),
    ("cuts interest rates", "action_rate_cut"),
    ("cut interest rates", "action_rate_cut"),
    ("lowers interest rates", "action_rate_cut"),
    ("lower interest rates", "action_rate_cut"),
    ("cuts policy rate", "action_rate_cut"),
    ("cut policy rate", "action_rate_cut"),
    ("lowers policy rate", "action_rate_cut"),
    ("lowers rates", "action_rate_cut"),
    ("cut rates", "action_rate_cut"),
    ("降低利率", "action_rate_cut"),
    ("利率下调", "action_rate_cut"),
    ("维持利率", "action_rate_hold"),
    ("利率不变", "action_rate_hold"),
    ("rate hold", "action_rate_hold"),
    ("holds rates", "action_rate_hold"),
    ("keeps policy rate", "action_rate_hold"),
    ("holds policy rate", "action_rate_hold"),
    ("rate unchanged", "action_rate_hold"),
    ("unchanged", "action_rate_hold"),
    ("据え置き", "action_rate_hold"),
    ("维持政策利率", "action_rate_hold"),
    ("利率上げ", "action_rate_hike"),
    ("注资", "action_capital_injection"),
    ("增资", "action_capital_injection"),
    ("capital injection", "action_capital_injection"),
    ("injects", "action_capital_injection"),
    ("event decision", "action_decision"),
    ("policy decision", "action_decision"),
    ("decision", "action_decision"),
    ("决议", "action_decision"),
    ("会合", "action_decision"),
    ("earthquake", "event_earthquake"),
    ("地震", "event_earthquake"),
    ("explosion", "event_explosion"),
    ("爆炸", "event_explosion"),
    ("resigns", "event_resignation"),
    ("resignation", "event_resignation"),
    ("辞职", "event_resignation"),
    ("sanctions", "event_sanctions"),
    ("制裁", "event_sanctions"),
    ("ceasefire", "event_ceasefire"),
    ("停火", "event_ceasefire"),
    ("election", "event_election"),
    ("选举", "event_election"),
    ("大选", "event_election"),
    ("tariff", "event_tariff"),
    ("tariffs", "event_tariff"),
    ("关税", "event_tariff"),
)


def _canonical_terms(value: Any) -> frozenset[str]:
    """Return reviewed multi-word signals before they are split into tokens."""
    return frozenset(
        term
        for term in re.findall(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+", _canonicalize(value))
        if term.startswith(("entity_", "action_", "event_", "policy_"))
    )


def _canonicalize(value: Any) -> str:
    """Replace only reviewed cross-language aliases with stable signal terms."""
    text = _text(value)
    for alias, canonical in sorted(_MULTILINGUAL_ALIASES, key=lambda pair: len(pair[0]), reverse=True):
        if all(ord(character) < 128 for character in alias):
            pattern = rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])"
        else:
            pattern = re.escape(alias)
        text = re.sub(pattern, f" {canonical} ", text)
    return _SPACE_RE.sub(" ", text).strip()


def generic_event_title(title: str) -> bool:
    return _text(title) in _GENERIC_TITLES


@dataclass(frozen=True, slots=True)
class EventReport:
    """One source's report, retained as a first-class event member."""

    report_id: str
    observation_id: int | None
    source_id: str
    publisher: str
    source_tier: str
    title: str
    summary: str
    url: str
    published_at: int
    score: float
    relation: str = "primary"
    match_score: float = 1.0
    contradicts: bool = False

    def __post_init__(self) -> None:
        if not self.report_id or len(self.report_id) > 128:
            raise ValueError("event report ID is invalid")
        if self.source_tier not in {"primary", "secondary", "social"}:
            raise ValueError("event report source tier is invalid")
        if self.relation not in {"primary", "secondary", "social", "corroborates", "updates", "context", "contradicts"}:
            raise ValueError("event report relation is invalid")
        if not 0.0 <= self.match_score <= 1.0:
            raise ValueError("event report match score is invalid")


@dataclass(frozen=True, slots=True)
class ClusteredEvent:
    """An event and its parallel reports."""

    event_id: str
    title: str
    summary: str
    reports: tuple[EventReport, ...]
    topics: tuple[str, ...]
    regions: tuple[str, ...]
    score: float
    importance: int
    urgency: int
    relevance: int
    confidence: float
    published_at: int
    independent_source_count: int
    has_contradictions: bool


@dataclass(frozen=True, slots=True)
class EventClaim:
    """A normalized fact claim; evidence remains attached to reports."""

    claim_id: str
    text: str
    status: str = "unverified"
    report_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EventTimelineItem:
    """A point in an event timeline, retaining its originating report."""

    timeline_id: str
    occurred_at: int
    text: str
    report_id: str
    kind: str = "update"


# Public vocabulary used by the persistence and presentation layers.  The
# implementation name remains explicit for callers that need to distinguish a
# clustered projection from a future persisted Event aggregate.
Event = ClusteredEvent
Claim = EventClaim
TimelineItem = EventTimelineItem


def _text(value: Any) -> str:
    return _SPACE_RE.sub(" ", unicodedata.normalize("NFKC", str(value or "")).casefold()).strip()


def _tokens(value: str) -> frozenset[str]:
    normalized = _canonicalize(value)
    raw = _TOKEN_RE.findall(normalized)
    result = {token for token in raw if token and token not in _STOPWORDS}
    cjk = "".join(token for token in raw if len(token) == 1 and ord(token) >= 0x3400)
    result.update(cjk[index : index + 2] for index in range(max(0, len(cjk) - 1)))
    for run in _CJK_RUN_RE.findall(normalized):
        if run not in _STOPWORDS and len(run) <= 12:
            result.add(run)
    return frozenset(result)


def _numbers(item: Mapping[str, Any]) -> frozenset[str]:
    """Return unit-normalized figures while preserving incompatible values."""
    text = _text(f"{item.get('title', '')} {item.get('summary', '')}")
    multipliers = {
        "万亿元": Decimal("1e12"), "trillion": Decimal("1e12"), "tn": Decimal("1e12"),
        "亿元": Decimal("1e8"), "billion": Decimal("1e9"), "bn": Decimal("1e9"), "十亿": Decimal("1e9"),
        "亿": Decimal("1e8"), "million": Decimal("1e6"), "mn": Decimal("1e6"), "百万": Decimal("1e6"),
        "万亿": Decimal("1e12"), "thousand": Decimal("1e3"),
    }
    values: set[str] = set()
    for match in _NUMBER_RE.finditer(text):
        try:
            number = Decimal(match.group("number").replace(",", ""))
        except InvalidOperation:
            continue
        unit = (match.group("unit") or "").replace("％", "%").lower().replace(" ", "")
        if unit == "%":
            values.add(f"pct:{number.normalize()}")
        elif unit in {"bp", "bps", "basispoints", "basispoint", "基点", "个基点"}:
            values.add(f"bp:{number.normalize()}")
        elif unit in multipliers:
            values.add(f"amount:{(number * multipliers[unit]).normalize()}")
        else:
            values.add(f"number:{number.normalize()}")
    return frozenset(values)


def _entities(item: Mapping[str, Any]) -> frozenset[str]:
    attrs = item.get("attributes")
    if isinstance(attrs, Mapping):
        explicit = attrs.get("entities")
        if isinstance(explicit, (list, tuple, set)):
            values = {_canonicalize(value) for value in explicit if str(value).strip()}
            if values:
                return frozenset(values) | frozenset(
                    term for term in _canonical_terms(
                        f"{item.get('title', '')} {item.get('summary', '')}"
                    ) if term.startswith("entity_")
                )
    title = _canonicalize(f"{item.get('title', '')} {item.get('summary', '')}")
    # Keep longer CJK runs as coarse entities.  This is intentionally light;
    # richer NER can be layered on later without changing this interface.
    return (
        frozenset(run for run in _CJK_RUN_RE.findall(title) if run not in _STOPWORDS)
        | frozenset(term for term in _canonical_terms(title) if term.startswith("entity_"))
    )


def _title_similarity(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    a = _canonicalize(left.get("title", ""))
    b = _canonicalize(right.get("title", ""))
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta, tb = _tokens(a), _tokens(b)
    union = ta | tb
    jaccard = len(ta & tb) / len(union) if union else 0.0
    sequence = SequenceMatcher(None, a, b, autojunk=False).ratio()
    return max(jaccard, sequence * 0.9)


def _canonical_actions(item: Mapping[str, Any]) -> frozenset[str]:
    return frozenset(
        term for term in _canonical_terms(
            f"{item.get('title', '')} {item.get('summary', '')}"
        ) if term.startswith("action_")
    )


def _is_context_report(item: Mapping[str, Any]) -> bool:
    text = _text(f"{item.get('title', '')} {item.get('summary', '')}")
    markers = (
        " after ", " following ", " reaction", " reacts ", " market ",
        "上涨", "下跌", "反应", "之后", "受影响",
    )
    return str(item.get("source_tier", "")).lower() == "social" or any(
        marker in f" {text} " for marker in markers
    )


def _region_compatible(left: str, right: str) -> bool:
    if not left or not right or left == right or "GLOBAL" in {left, right}:
        return True
    families = {"CN": "EAST_ASIA", "JP": "EAST_ASIA", "US": "NORTH_AMERICA"}
    return families.get(left, left) == families.get(right, right)


def _pair_score(left: Mapping[str, Any], right: Mapping[str, Any], *, window: int) -> tuple[float, bool]:
    if abs(int(left.get("published_at", 0) or 0) - int(right.get("published_at", 0) or 0)) > window:
        return 0.0, False
    # Section headings and recurring columns identify a format, not an event.
    if generic_event_title(str(left.get("title", ""))) or generic_event_title(str(right.get("title", ""))):
        return 0.0, False
    topic_left = _text(left.get("topic", "general"))
    topic_right = _text(right.get("topic", "general"))
    region_left = _text(left.get("region", "GLOBAL")).upper()
    region_right = _text(right.get("region", "GLOBAL")).upper()
    title = _title_similarity(left, right)
    entities_left, entities_right = _entities(left), _entities(right)
    entity_score = len(entities_left & entities_right) / max(1, len(entities_left | entities_right))
    left_ts, right_ts = int(left.get("published_at", 0) or 0), int(right.get("published_at", 0) or 0)
    time_score = max(0.0, 1.0 - abs(left_ts - right_ts) / max(1, window))
    nums_left, nums_right = _numbers(left), _numbers(right)
    number_overlap = len(nums_left & nums_right) / max(1, len(nums_left | nums_right))
    if not nums_left and not nums_right:
        number_overlap = 0.5
    region_score = 1.0 if _region_compatible(region_left, region_right) else 0.0
    score = 0.35 * title + 0.25 * entity_score + 0.15 * time_score + 0.15 * number_overlap + 0.10 * region_score
    # A reviewed decisive action is a hard semantic boundary. Reports that
    # share an institution but describe opposite policy directions remain
    # sibling reports under separate events (for example, hike vs cut).
    actions_left, actions_right = _canonical_actions(left), _canonical_actions(right)
    decisive_left = {term for term in actions_left if term.startswith("action_rate_")}
    decisive_right = {term for term in actions_right if term.startswith("action_rate_")}
    if decisive_left and decisive_right and decisive_left.isdisjoint(decisive_right):
        return 0.0, False
    # A shared ministry or company is insufficient to merge unrelated action
    # types.  In particular, a budget announcement must not absorb a capital
    # injection merely because both mention the finance ministry.  Keep the
    # guard narrow so an official announcement and a later detailed report can
    # still be joined when their titles are clearly about the same decision.
    capital_left = "action_capital_injection" in actions_left
    capital_right = "action_capital_injection" in actions_right
    if capital_left != capital_right and title < 0.72:
        return 0.0, False
    canonical_left = _canonical_terms(f"{left.get('title', '')} {left.get('summary', '')}")
    canonical_right = _canonical_terms(f"{right.get('title', '')} {right.get('summary', '')}")
    shared_canonical = canonical_left & canonical_right
    shared_entities = _entities(left) & _entities(right)
    if shared_canonical and (actions_left & actions_right or _is_context_report(left) or _is_context_report(right)):
        score = max(
            score,
            min(
                0.92,
                0.42
                + 0.28 * (len(shared_entities) / max(1, len(_entities(left) | _entities(right))))
                + 0.12 * time_score
                + 0.10 * region_score
                + 0.10 * number_overlap,
            ),
        )
    semantic_left = identify_semantic_event(
        str(left.get("title", "")), str(left.get("summary", ""))
    )
    semantic_right = identify_semantic_event(
        str(right.get("title", "")), str(right.get("summary", ""))
    )
    shared_semantic = (
        semantic_left is not None
        and semantic_right is not None
        and semantic_left.compatible(semantic_right, left_ts, right_ts)
        and abs(left_ts - right_ts) <= window
    )
    if shared_semantic:
        score = max(score, 0.96)
    elif semantic_left is not None and semantic_right is not None:
        return 0.0, False
    elif topic_left != topic_right and "general" not in {topic_left, topic_right}:
        if not shared_entities or not (_is_context_report(left) or _is_context_report(right)):
            return 0.0, False
    # Distinct figures in otherwise similar reports are retained as one event
    # with a contradiction marker, rather than silently replacing either fact.
    contradictory = bool(
        nums_left and nums_right and not (nums_left & nums_right)
        and title >= 0.55 and entity_score >= 0.2
    )
    if not shared_semantic and not _region_compatible(region_left, region_right) and title < 0.88:
        return 0.0, contradictory
    return min(1.0, score), contradictory


def _report_id(item: Mapping[str, Any]) -> str:
    external = str(item.get("external_id", "")).strip()
    identity = external or str(item.get("url", "")).strip() or f"{item.get('source_id', '')}|{item.get('title', '')}|{item.get('published_at', 0)}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _tier(value: Any) -> str:
    tier = str(value or "secondary").strip().lower()
    return tier if tier in {"primary", "secondary", "social"} else "secondary"


def event_evidence_score(
    importance: Any = 3, urgency: Any = 2, relevance: Any = 3, confidence: Any = 0.5
) -> float:
    """Score durable triage evidence with the same defaults for every projection."""
    def numeric(value: Any, default: float) -> float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result = float(value)
            if math.isfinite(result):
                return result
        return default

    return (
        numeric(importance, 3) * 0.35
        + numeric(urgency, 2) * 0.15
        + numeric(relevance, 3) * 0.25
        + max(0.0, min(1.0, numeric(confidence, 0.5))) * 0.5
    )


def event_aggregate_score(base_score: float, independent_source_count: int) -> float:
    """Add bounded independent-source corroboration to the strongest evidence."""
    return round(
        base_score + min(0.75, 0.25 * math.log2(max(1, independent_source_count))), 4
    )


def _score(item: Mapping[str, Any]) -> float:
    return event_evidence_score(
        item.get("importance", 3), item.get("urgency", 2),
        item.get("relevance", 3), item.get("confidence", 0.5),
    )


def _component_guard(component: Sequence[int], items: Sequence[Mapping[str, Any]], edges: Mapping[tuple[int, int], float], threshold: float) -> bool:
    # Complete-linkage with a small tolerance prevents transitive chain merges.
    floor = max(0.5, threshold - 0.15)
    for pos, left in enumerate(component):
        for right in component[pos + 1 :]:
            if edges.get((min(left, right), max(left, right)), 0.0) < floor:
                return False
    return True


def cluster_events(
    observations: Sequence[Mapping[str, Any]],
    *,
    similarity_threshold: float = 0.62,
    time_window: int = 72 * 3600,
) -> tuple[ClusteredEvent, ...]:
    """Cluster observations deterministically, independent of input order."""
    if not 0.5 <= similarity_threshold <= 1.0:
        raise ValueError("similarity threshold is out of range")
    if time_window < 1:
        raise ValueError("time window must be positive")
    items = sorted(
        (item for item in observations if str(item.get("title", "")).strip()),
        key=lambda item: (_report_id(item), int(item.get("id", 0) or 0)),
    )
    edges: dict[tuple[int, int], float] = {}
    contradictions: set[tuple[int, int]] = set()
    for left in range(len(items)):
        for right in range(left + 1, len(items)):
            score, contradictory = _pair_score(items[left], items[right], window=time_window)
            if score >= similarity_threshold:
                edges[(left, right)] = score
                if contradictory:
                    contradictions.add((left, right))

    # Process edges in a fixed order and accept only complete-linkage merges.
    # This keeps A~B and B~C from swallowing an unrelated A/C pair.
    components: list[list[int]] = [[index] for index in range(len(items))]
    membership = {index: index for index in range(len(items))}
    for (left, right), _ in sorted(edges.items(), key=lambda entry: (-entry[1], entry[0])):
        li, ri = membership[left], membership[right]
        if li == ri:
            continue
        merged = components[li] + components[ri]
        if not _component_guard(merged, items, edges, similarity_threshold):
            continue
        components[li] = sorted(merged)
        for member in merged:
            membership[member] = li
        components[ri] = []

    result: list[ClusteredEvent] = []
    for component in (value for value in components if value):
        members = [items[index] for index in component]
        representative = min(
            members,
            key=lambda item: (-_score(item), _text(item.get("title", "")), _report_id(item)),
        )
        title = str(representative.get("title", "")).strip()[:500]
        topics = tuple(sorted({_text(item.get("topic", "general")) for item in members}))
        regions = tuple(sorted({_text(item.get("region", "GLOBAL")).upper() for item in members}))
        entity_union = sorted(set().union(*(_entities(item) for item in members)))
        semantic_keys = {
            identity.dated_key(int(item.get("published_at", 0) or 0))
            for item in members
            if (identity := identify_semantic_event(
                str(item.get("title", "")), str(item.get("summary", ""))
            )) is not None
        }
        actions = {
            identity.action for item in members
            if (identity := identify_semantic_event(str(item.get("title", "")))) is not None
            and identity.action != "announcement"
        }
        if len(semantic_keys) == 1:
            fingerprint = ":".join((
                next(iter(semantic_keys)),
                next(iter(actions)) if len(actions) == 1 else "announcement",
                str(min(int(item.get("published_at", 0) or 0) for item in members) // 21600),
            ))
        else:
            signal_sets = [
                _tokens(f"{item.get('title', '')} {item.get('summary', '')}")
                for item in members
            ]
            common_signals = (
                set(signal_sets[0]).intersection(*signal_sets[1:])
                if signal_sets
                else set()
            )
            number_sets = [_numbers(item) for item in members]
            common_numbers = (
                set(number_sets[0]).intersection(*number_sets[1:])
                if number_sets
                else set()
            )
            canonical_sets = [
                _canonical_terms(f"{item.get('title', '')} {item.get('summary', '')}")
                for item in members
            ]
            common_canonical = (
                set(canonical_sets[0]).intersection(*canonical_sets[1:])
                if canonical_sets
                else set()
            )
            stable_signals = sorted(
                common_signals | common_canonical | common_numbers | set(entity_union)
            )
            if not stable_signals:
                stable_signals = sorted(_tokens(title))[:24]
            fingerprint = "|".join([
                ",".join(stable_signals), ",".join(topics), ",".join(regions),
            ])
            # Repeated headlines outside the time gate are separate events.
            # Persistent projection still reuses an already matched stable key.
            fingerprint += f"|{min(int(item.get('published_at', 0) or 0) for item in members) // 86400}"
            if generic_event_title(title):
                fingerprint += f"|{_report_id(representative)}"
        event_id = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:24]
        primary_present = any(_tier(item.get("source_tier")) == "primary" for item in members)
        # Classify updates chronologically, then present reports newest first.
        relation_by_id: dict[str, str] = {}
        source_seen: set[str] = set()
        for index in sorted(component, key=lambda value: (int(items[value].get("published_at", 0) or 0), _report_id(items[value]))):
            item = items[index]
            tier = _tier(item.get("source_tier"))
            source_id = str(item.get("source_id", ""))
            semantic = identify_semantic_event(
                str(item.get("title", "")), str(item.get("summary", ""))
            )
            if semantic is not None and semantic.relation_hint == "context":
                relation_by_id[_report_id(item)] = "context"
            elif source_id in source_seen:
                relation_by_id[_report_id(item)] = "updates"
            elif tier == "primary":
                relation_by_id[_report_id(item)] = "primary"
            elif tier == "secondary" and primary_present:
                relation_by_id[_report_id(item)] = "corroborates"
            elif tier == "social":
                relation_by_id[_report_id(item)] = "context" if primary_present else "social"
            else:
                relation_by_id[_report_id(item)] = "context" if tier == "secondary" else tier
            source_seen.add(source_id)
        report_rows: list[EventReport] = []
        for index in sorted(component, key=lambda value: (-int(items[value].get("published_at", 0) or 0), _report_id(items[value]))):
            item = items[index]
            tier = _tier(item.get("source_tier"))
            source_id = str(item.get("source_id", ""))
            relation = relation_by_id[_report_id(item)]
            pair_scores = [edges.get((min(index, other), max(index, other)), 0.0) for other in component if other != index]
            report_rows.append(
                EventReport(
                    report_id=_report_id(item),
                    observation_id=int(item["id"]) if item.get("id") is not None else None,
                    source_id=source_id,
                    publisher=str(item.get("publisher", source_id)),
                    source_tier=tier,
                    title=str(item.get("title", "")).strip()[:500],
                    summary=str(item.get("summary", "")).strip()[:4000],
                    url=str(item.get("url", "")),
                    published_at=int(item.get("published_at", 0) or 0),
                    score=_score(item),
                    relation=relation,
                    match_score=min(1.0, max(pair_scores, default=1.0)),
                    contradicts=any((min(index, other), max(index, other)) in contradictions for other in component if other != index),
                )
            )
        summary = next((report.summary for report in report_rows if report.summary), title)
        importance = max(_bounded_score(item.get("importance"), 3) for item in members)
        urgency = max(_bounded_score(item.get("urgency"), 2) for item in members)
        relevance = max(_bounded_score(item.get("relevance"), 3) for item in members)
        confidence = max(_bounded_float(item.get("confidence"), 0.5) for item in members)
        independent = len({
            report.publisher.strip().lower() or report.source_id
            for report in report_rows if report.publisher.strip() or report.source_id
        })
        score = event_aggregate_score(max(report.score for report in report_rows), independent)
        result.append(
            ClusteredEvent(
                event_id=event_id,
                title=title,
                summary=summary[:4000],
                reports=tuple(report_rows),
                topics=topics,
                regions=regions,
                score=round(score, 4),
                importance=importance,
                urgency=urgency,
                relevance=relevance,
                confidence=confidence,
                published_at=max(report.published_at for report in report_rows),
                independent_source_count=independent,
                has_contradictions=any(report.contradicts for report in report_rows),
            )
        )
    return tuple(sorted(result, key=lambda event: (-event.score, -event.published_at, event.event_id)))


def event_match_score(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    """Score a new event projection against a persisted event candidate."""
    score, _ = _pair_score(left, right, window=72 * 3600)
    return score


def explain_event_match(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    """Explain the current deterministic scorer; never present it as a probability."""
    score, conflict = _pair_score(left, right, window=72 * 3600)
    first = identify_semantic_event(str(left.get("title", "")), str(left.get("summary", "")))
    second = identify_semantic_event(str(right.get("title", "")), str(right.get("summary", "")))
    a, b = int(left.get("published_at", 0) or 0), int(right.get("published_at", 0) or 0)
    return {
        "score": round(score, 4), "threshold": 0.57,
        "title_similarity": round(_title_similarity(left, right), 4),
        "shared_entities": sorted(_entities(left) & _entities(right))[:20],
        "shared_canonical_signals": sorted(
            _canonical_terms(f"{left.get('title', '')} {left.get('summary', '')}")
            & _canonical_terms(f"{right.get('title', '')} {right.get('summary', '')}")
        )[:20],
        "left_canonical_actions": sorted(
            term for term in _canonical_terms(f"{left.get('title', '')} {left.get('summary', '')}")
            if term.startswith("action_rate_")
        ),
        "right_canonical_actions": sorted(
            term for term in _canonical_terms(f"{right.get('title', '')} {right.get('summary', '')}")
            if term.startswith("action_rate_")
        ),
        "shared_numbers": sorted(_numbers(left) & _numbers(right))[:20],
        "time_distance_hours": round(abs(a - b) / 3600, 2),
        "semantic_identity": bool(first and second and first.compatible(second, a, b)),
        "generic_title": generic_event_title(str(left.get("title", ""))) or generic_event_title(str(right.get("title", ""))),
        "possible_numeric_conflict": conflict,
        "scope": "current_rule_replay_not_historical_decision",
    }


def _bounded_score(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(1, min(5, value))


def _bounded_float(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(0.0, min(1.0, float(value)))
