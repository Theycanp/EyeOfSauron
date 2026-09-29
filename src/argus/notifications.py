from __future__ import annotations

import re
import secrets
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit

from .models import AlertCandidate


CATEGORIES = ("news", "weather", "reminders", "system")
_TOPIC = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ID = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")


def public_subscription_base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            return ""
        _ = parsed.port
    except ValueError:
        return ""
    return value.rstrip("/")


@dataclass(frozen=True, slots=True)
class NotificationDestination:
    id: str
    topic: str
    label: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class NotificationPolicy:
    destinations: tuple[NotificationDestination, ...]
    routes: Mapping[str, str | None]
    fallback: str


@dataclass(frozen=True, slots=True)
class NotificationRoute:
    category: str
    topic: str | None
    revision: int | None


class NotificationPolicyRepository(Protocol):
    def get_active_config_revision(self) -> dict[str, Any] | None: ...


class NotificationRouter(Protocol):
    def resolve(self, candidate: AlertCandidate) -> NotificationRoute: ...


class NotificationAdministrationRepository(Protocol):
    def notification_topic_status(self, topics: tuple[str, ...]) -> list[dict[str, Any]]: ...
    def enqueue_notification_test(self, topic: str, actor: str, now: int) -> int: ...


def parse_notification_policy(raw: Any) -> NotificationPolicy:
    if not isinstance(raw, Mapping) or set(raw) != {"destinations", "routes", "fallback"}:
        raise ValueError("notifications requires destinations, routes and fallback")
    rows = raw["destinations"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 16:
        raise ValueError("notifications.destinations must contain 1 to 16 entries")
    destinations = []
    for row in rows:
        if not isinstance(row, Mapping) or set(row) - {"id", "topic", "label", "description"}:
            raise ValueError("invalid notification destination fields")
        identifier, topic, label = row.get("id"), row.get("topic"), row.get("label")
        description = row.get("description", "")
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
            raise ValueError("invalid notification destination ID")
        if not isinstance(topic, str) or not _TOPIC.fullmatch(topic):
            raise ValueError("invalid notification topic")
        if not isinstance(label, str) or not label.strip() or len(label) > 80:
            raise ValueError("notification destination label must contain 1 to 80 characters")
        if not isinstance(description, str) or len(description) > 300:
            raise ValueError("notification destination description is too long")
        destinations.append(NotificationDestination(identifier, topic, label.strip(), description.strip()))
    ids = {item.id for item in destinations}
    if len(ids) != len(destinations) or len({item.topic for item in destinations}) != len(destinations):
        raise ValueError("notification destination IDs and topics must be unique")
    routes = raw["routes"]
    if not isinstance(routes, Mapping) or set(routes) != set(CATEGORIES):
        raise ValueError("notifications.routes must specify news, weather, reminders and system")
    for destination_id in routes.values():
        if destination_id is not None and (not isinstance(destination_id, str) or destination_id not in ids):
            raise ValueError("notification route references a missing destination")
    fallback = raw["fallback"]
    if not isinstance(fallback, str) or fallback not in ids:
        raise ValueError("notification fallback references a missing destination")
    return NotificationPolicy(tuple(destinations), dict(routes), fallback)


def validate_notification_topics(policy: NotificationPolicy, allowed_topics: tuple[str, ...]) -> None:
    if any(item.topic not in allowed_topics for item in policy.destinations):
        raise ValueError("notification destination topic is not provisioned for this publisher")


def default_notification_policy(topic: str) -> NotificationPolicy:
    return NotificationPolicy(
        (NotificationDestination("default", topic, "默认通知"),),
        dict.fromkeys(CATEGORIES, "default"), "default",
    )


def standard_notification_policy() -> NotificationPolicy:
    labels = {"news": "新闻与日报", "weather": "天气", "reminders": "提醒", "system": "系统运行"}
    return NotificationPolicy(
        tuple(NotificationDestination(category, f"eos-{category}", labels[category]) for category in CATEGORIES),
        {category: category for category in CATEGORIES}, "news",
    )


def notification_policy_payload(policy: NotificationPolicy) -> dict[str, Any]:
    return {"destinations": [asdict(item) for item in policy.destinations],
            "routes": dict(policy.routes), "fallback": policy.fallback}


class NotificationRoutingService:
    """Resolve against the transaction's desired revision, before outbox insertion."""

    def __init__(
        self, repository: NotificationPolicyRepository, *, allowed_topics: tuple[str, ...],
        default_topic: str, base_policy: NotificationPolicy | None = None,
    ) -> None:
        self.repository = repository
        self.allowed_topics = allowed_topics
        self.default_topic = default_topic
        self.base_policy = base_policy or default_notification_policy(default_topic)
        validate_notification_topics(self.base_policy, allowed_topics)

    def current(self) -> tuple[NotificationPolicy, int | None]:
        active = self.repository.get_active_config_revision()
        raw = active["payload"].get("notifications") if active else None
        policy = parse_notification_policy(raw) if raw is not None else self.base_policy
        validate_notification_topics(policy, self.allowed_topics)
        return policy, int(active["revision"]) if active else None

    def resolve(self, candidate: AlertCandidate) -> NotificationRoute:
        policy, revision = self.current()
        category = candidate.category if candidate.category in CATEGORIES else "news"
        if candidate.rule_id == "system.topic_test":
            if candidate.topic not in self.allowed_topics:
                raise ValueError("notification test topic is not provisioned")
            return NotificationRoute(category, candidate.topic, revision)
        destination_id = policy.routes.get(candidate.category, policy.fallback)
        if destination_id is None:
            return NotificationRoute(category, None, revision)
        topic = next(item.topic for item in policy.destinations if item.id == destination_id)
        if candidate.topic_override:
            if candidate.topic not in self.allowed_topics:
                raise ValueError("notification override topic is not provisioned for this publisher")
            topic = candidate.topic
        return NotificationRoute(category, topic, revision)

    def public(self) -> dict[str, Any]:
        policy, revision = self.current()
        return {"policy": notification_policy_payload(policy), "allowed_topics": list(self.allowed_topics), "revision": revision or 0}


def notification_test_candidate(topic: str, actor: str, now: int) -> AlertCandidate:
    return AlertCandidate(
        rule_id="system.topic_test", dedupe_key=f"topic-test:{secrets.token_hex(16)}",
        title=f"EOS 主题测试 · {topic}", message=f"通知主题 {topic} 的投递测试。\n操作人：{actor[:128]}",
        priority=2, tags=("test_tube",), click_url="", topic=topic, category="system",
    )
