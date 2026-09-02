from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit


_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
_ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TAG_RE = re.compile(r"^[A-Za-z0-9_+-]{1,64}$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    database_path: Path
    lock_path: Path
    log_level: str
    source_failure_alert_after: int
    retention_days: int
    delivery_lease_seconds: int
    max_delivery_retry_seconds: int


@dataclass(frozen=True, slots=True)
class NtfyConfig:
    enabled: bool
    base_url_env: str
    token_env: str
    default_topic: str
    timeout_seconds: int


@dataclass(frozen=True, slots=True)
class RssSourceConfig:
    id: str
    kind: str
    publisher: str
    section: str
    dedupe_scope: str
    url: str
    allowed_hosts: tuple[str, ...]
    poll_interval_seconds: int
    request_timeout_seconds: int
    request_attempts: int
    retry_base_seconds: int
    max_response_bytes: int


@dataclass(frozen=True, slots=True)
class PatternConfig:
    label: str
    regex: str
    title_weight: float
    summary_weight: float


@dataclass(frozen=True, slots=True)
class WeightedTextRuleConfig:
    id: str
    kind: str
    source_ids: tuple[str, ...]
    threshold: float
    max_item_age_seconds: int
    notification_title: str
    priority: int
    tags: tuple[str, ...]
    patterns: tuple[PatternConfig, ...]
    topic: str | None = None


@dataclass(frozen=True, slots=True)
class AppConfig:
    schema_version: int
    service: ServiceConfig
    ntfy: NtfyConfig
    sources: tuple[RssSourceConfig, ...]
    rules: tuple[WeightedTextRuleConfig, ...]


def _mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{location} must be a table")
    return value


def _reject_unknown(data: Mapping[str, Any], allowed: set[str], location: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigError(f"unknown {location} keys: {', '.join(unknown)}")


def _required(data: Mapping[str, Any], key: str, expected: type, location: str) -> Any:
    if key not in data:
        raise ConfigError(f"missing {location}.{key}")
    value = data[key]
    if expected is int and (not isinstance(value, int) or isinstance(value, bool)):
        raise ConfigError(f"{location}.{key} must be an integer")
    if expected is float and (not isinstance(value, (int, float)) or isinstance(value, bool)):
        raise ConfigError(f"{location}.{key} must be a number")
    if expected not in (int, float) and not isinstance(value, expected):
        raise ConfigError(f"{location}.{key} must be {expected.__name__}")
    return value


def _bounded_int(data: Mapping[str, Any], key: str, location: str, low: int, high: int) -> int:
    value = _required(data, key, int, location)
    if not low <= value <= high:
        raise ConfigError(f"{location}.{key} must be between {low} and {high}")
    return value


def _absolute_path(data: Mapping[str, Any], key: str, location: str) -> Path:
    value = Path(_required(data, key, str, location))
    if not value.is_absolute():
        raise ConfigError(f"{location}.{key} must be an absolute path")
    return value


def _identifier(value: str, location: str) -> str:
    if not _ID_RE.fullmatch(value):
        raise ConfigError(f"{location} must match {_ID_RE.pattern}")
    return value


def _string_list(value: Any, location: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{location} must be a non-empty string array")
    return tuple(value)


def _https_url(value: str, location: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ConfigError(f"{location} must be an HTTPS URL without embedded credentials")
    return value


def _parse_service(raw: Any) -> ServiceConfig:
    data = _mapping(raw, "service")
    allowed = {
        "database_path", "lock_path", "log_level", "source_failure_alert_after",
        "retention_days", "delivery_lease_seconds", "max_delivery_retry_seconds",
    }
    _reject_unknown(data, allowed, "service")
    log_level = _required(data, "log_level", str, "service").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ConfigError("service.log_level is invalid")
    return ServiceConfig(
        database_path=_absolute_path(data, "database_path", "service"),
        lock_path=_absolute_path(data, "lock_path", "service"),
        log_level=log_level,
        source_failure_alert_after=_bounded_int(data, "source_failure_alert_after", "service", 1, 100),
        retention_days=_bounded_int(data, "retention_days", "service", 1, 3650),
        delivery_lease_seconds=_bounded_int(data, "delivery_lease_seconds", "service", 10, 3600),
        max_delivery_retry_seconds=_bounded_int(data, "max_delivery_retry_seconds", "service", 30, 86400),
    )


def _parse_ntfy(raw: Any) -> NtfyConfig:
    data = _mapping(raw, "ntfy")
    allowed = {"enabled", "base_url_env", "token_env", "default_topic", "timeout_seconds"}
    _reject_unknown(data, allowed, "ntfy")
    base_env = _required(data, "base_url_env", str, "ntfy")
    token_env = _required(data, "token_env", str, "ntfy")
    topic = _required(data, "default_topic", str, "ntfy")
    if not _ENV_RE.fullmatch(base_env) or not _ENV_RE.fullmatch(token_env):
        raise ConfigError("ntfy environment variable names are invalid")
    if not _TOPIC_RE.fullmatch(topic):
        raise ConfigError("ntfy.default_topic is invalid")
    return NtfyConfig(
        enabled=_required(data, "enabled", bool, "ntfy"),
        base_url_env=base_env,
        token_env=token_env,
        default_topic=topic,
        timeout_seconds=_bounded_int(data, "timeout_seconds", "ntfy", 1, 120),
    )


def _parse_source(raw: Any, index: int) -> RssSourceConfig:
    location = f"sources[{index}]"
    data = _mapping(raw, location)
    allowed = {
        "id", "kind", "publisher", "section", "dedupe_scope", "url", "allowed_hosts",
        "poll_interval_seconds", "request_timeout_seconds", "request_attempts",
        "retry_base_seconds", "max_response_bytes",
    }
    _reject_unknown(data, allowed, location)
    source_id = _identifier(_required(data, "id", str, location), f"{location}.id")
    kind = _required(data, "kind", str, location)
    if kind != "rss":
        raise ConfigError(f"{location}.kind is unsupported: {kind}")
    allowed_hosts = tuple(host.lower() for host in _string_list(data.get("allowed_hosts"), f"{location}.allowed_hosts"))
    url = _https_url(_required(data, "url", str, location), f"{location}.url")
    if urlsplit(url).hostname.lower() not in allowed_hosts:
        raise ConfigError(f"{location}.url host must be present in allowed_hosts")
    publisher = _required(data, "publisher", str, location).strip()
    section = _required(data, "section", str, location).strip()
    dedupe_scope = _identifier(
        _required(data, "dedupe_scope", str, location), f"{location}.dedupe_scope"
    )
    if not publisher or not section:
        raise ConfigError(f"{location} publisher and section cannot be empty")
    return RssSourceConfig(
        id=source_id,
        kind=kind,
        publisher=publisher,
        section=section,
        dedupe_scope=dedupe_scope,
        url=url,
        allowed_hosts=allowed_hosts,
        poll_interval_seconds=_bounded_int(data, "poll_interval_seconds", location, 30, 86400),
        request_timeout_seconds=_bounded_int(data, "request_timeout_seconds", location, 1, 120),
        request_attempts=_bounded_int(data, "request_attempts", location, 1, 5),
        retry_base_seconds=_bounded_int(data, "retry_base_seconds", location, 1, 30),
        max_response_bytes=_bounded_int(data, "max_response_bytes", location, 1024, 10485760),
    )


def _parse_rule(raw: Any, index: int) -> WeightedTextRuleConfig:
    location = f"rules[{index}]"
    data = _mapping(raw, location)
    allowed = {
        "id", "kind", "source_ids", "threshold", "max_item_age_seconds",
        "notification_title", "priority", "tags", "topic", "patterns",
    }
    _reject_unknown(data, allowed, location)
    rule_id = _identifier(_required(data, "id", str, location), f"{location}.id")
    kind = _required(data, "kind", str, location)
    if kind != "weighted_text":
        raise ConfigError(f"{location}.kind is unsupported: {kind}")
    threshold = float(_required(data, "threshold", float, location))
    if threshold <= 0:
        raise ConfigError(f"{location}.threshold must be positive")
    tags = _string_list(data.get("tags"), f"{location}.tags")
    if any(not _TAG_RE.fullmatch(tag) for tag in tags):
        raise ConfigError(f"{location}.tags contains an invalid ntfy tag")
    topic = data.get("topic")
    if topic is not None and (not isinstance(topic, str) or not _TOPIC_RE.fullmatch(topic)):
        raise ConfigError(f"{location}.topic is invalid")
    pattern_rows = data.get("patterns")
    if not isinstance(pattern_rows, list) or not pattern_rows:
        raise ConfigError(f"{location}.patterns must be a non-empty table array")
    patterns: list[PatternConfig] = []
    for pattern_index, raw_pattern in enumerate(pattern_rows):
        pattern_location = f"{location}.patterns[{pattern_index}]"
        pattern = _mapping(raw_pattern, pattern_location)
        _reject_unknown(pattern, {"label", "regex", "title_weight", "summary_weight"}, pattern_location)
        expression = _required(pattern, "regex", str, pattern_location)
        if len(expression) > 512:
            raise ConfigError(f"{pattern_location}.regex is too long")
        try:
            re.compile(expression)
        except re.error as exc:
            raise ConfigError(f"{pattern_location}.regex is invalid: {exc}") from exc
        patterns.append(PatternConfig(
            label=_required(pattern, "label", str, pattern_location).strip(),
            regex=expression,
            title_weight=float(_required(pattern, "title_weight", float, pattern_location)),
            summary_weight=float(_required(pattern, "summary_weight", float, pattern_location)),
        ))
    return WeightedTextRuleConfig(
        id=rule_id,
        kind=kind,
        source_ids=_string_list(data.get("source_ids"), f"{location}.source_ids"),
        threshold=threshold,
        max_item_age_seconds=_bounded_int(data, "max_item_age_seconds", location, 60, 2592000),
        notification_title=_required(data, "notification_title", str, location).strip(),
        priority=_bounded_int(data, "priority", location, 1, 5),
        tags=tags,
        patterns=tuple(patterns),
        topic=topic,
    )


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot load configuration: {exc}") from exc

    _reject_unknown(data, {"schema_version", "service", "ntfy", "sources", "rules"}, "top-level")
    version = _required(data, "schema_version", int, "top-level")
    if version != 1:
        raise ConfigError(f"unsupported schema_version: {version}")
    raw_sources = data.get("sources")
    raw_rules = data.get("rules")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ConfigError("sources must be a non-empty table array")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ConfigError("rules must be a non-empty table array")

    sources = tuple(_parse_source(source, index) for index, source in enumerate(raw_sources))
    rules = tuple(_parse_rule(rule, index) for index, rule in enumerate(raw_rules))
    source_ids = [source.id for source in sources]
    rule_ids = [rule.id for rule in rules]
    if len(source_ids) != len(set(source_ids)):
        raise ConfigError("source IDs must be unique")
    if len(rule_ids) != len(set(rule_ids)):
        raise ConfigError("rule IDs must be unique")
    unknown_rule_sources = sorted({item for rule in rules for item in rule.source_ids} - set(source_ids))
    if unknown_rule_sources:
        raise ConfigError(f"rules reference unknown sources: {', '.join(unknown_rule_sources)}")

    return AppConfig(
        schema_version=version,
        service=_parse_service(data.get("service")),
        ntfy=_parse_ntfy(data.get("ntfy")),
        sources=sources,
        rules=rules,
    )
