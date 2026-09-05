from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

from .providers import (
    DEFAULT_PROVIDER_REGISTRY,
    ProviderConfigError,
    ProviderRegistry,
    UrlMode,
)


_ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


@dataclass(frozen=True, slots=True)
class ProviderValidationResult:
    settings: Mapping[str, Any]
    credential_refs: Mapping[str, str]


def _secret_ref(settings: Mapping[str, Any], key: str, location: str) -> str:
    value = settings.get(key)
    if not isinstance(value, str) or not _ENV_RE.fullmatch(value):
        raise ProviderConfigError(f"{location}.{key} is invalid")
    return value


def _setting_int(
    settings: Mapping[str, Any], key: str, default: int, location: str, low: int, high: int
) -> int:
    value = settings.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ProviderConfigError(f"{location}.{key} must be between {low} and {high}")
    return value


def _setting_float(
    settings: Mapping[str, Any], key: str, default: float, location: str, low: float, high: float
) -> float:
    value = settings.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ProviderConfigError(f"{location}.{key} must be a number")
    result = float(value)
    if not math.isfinite(result) or not low <= result <= high:
        raise ProviderConfigError(
            f"{location}.{key} must be finite and between {low} and {high}"
        )
    return result


def _plain_https_base(value: Any, location: str) -> tuple[str, str]:
    if not isinstance(value, str):
        raise ProviderConfigError(f"{location} is required")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ProviderConfigError(f"{location} must be a plain HTTPS origin/path")
    return value, parsed.hostname.lower()


def _validate_declared_settings(spec: Any, settings: Mapping[str, Any], location: str) -> None:
    """Execute the portable subset of each provider's declared field schema."""
    for field in spec.settings:
        present = field.name in settings
        value = settings.get(field.name)
        if field.required_when_enabled and (
            not present or value is None or value == "" or value == []
        ):
            raise ProviderConfigError(f"{location}.{field.name} is required")
        if not present:
            continue
        value_type = field.value_type
        if value_type in {"string", "hostname", "path_template"}:
            valid = isinstance(value, str)
        elif value_type == "numeric_string":
            valid = isinstance(value, str) and value.isdigit()
        elif value_type in {"environment_variable", "https_url"}:
            valid = isinstance(value, str)
        elif value_type == "integer":
            valid = isinstance(value, int) and not isinstance(value, bool)
        elif value_type == "number":
            valid = (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
            )
        elif value_type == "boolean":
            valid = isinstance(value, bool)
        elif value_type == "string_array":
            valid = isinstance(value, list) and all(isinstance(item, str) for item in value)
        elif value_type == "listen_endpoint_array":
            valid = isinstance(value, list) and all(
                isinstance(item, (int, str)) and not isinstance(item, bool) for item in value
            )
        else:
            raise ProviderConfigError(
                f"provider {spec.kind} declares unsupported field type {value_type}"
            )
        if not valid:
            raise ProviderConfigError(
                f"{location}.{field.name} must be {value_type}"
            )
        if field.secret_reference or value_type == "environment_variable":
            _secret_ref(settings, field.name, location)
        if value_type == "https_url":
            _plain_https_base(value, f"{location}.{field.name}")
        if field.minimum is not None and float(value) < field.minimum:
            raise ProviderConfigError(
                f"{location}.{field.name} must be at least {field.minimum:g}"
            )
        if field.maximum is not None and float(value) > field.maximum:
            raise ProviderConfigError(
                f"{location}.{field.name} must be at most {field.maximum:g}"
            )


def _validate_market(
    settings: Mapping[str, Any], allowed_hosts: tuple[str, ...], location: str
) -> dict[str, Any]:
    normalized = dict(settings)
    symbols = settings.get("symbols")
    if (
        not isinstance(symbols, list)
        or not symbols
        or len(symbols) > 500
        or not all(isinstance(item, str) and item.strip() for item in symbols)
    ):
        raise ProviderConfigError(f"{location}.symbols must contain 1 to 500 symbols")
    normalized["symbols"] = list(dict.fromkeys(item.strip().upper() for item in symbols))
    api_base, api_host = _plain_https_base(settings.get("api_base_url"), f"{location}.api_base_url")
    trusted = {"data.alpaca.markets", "api.alpaca.markets", "paper-api.alpaca.markets"}
    if api_host not in (set(allowed_hosts) or trusted):
        raise ProviderConfigError(
            f"{location}.api_base_url host is not allowed for its credentials"
        )
    normalized["api_base_url"] = api_base
    path_template = settings.get("path_template", "/v2/stocks/{symbol}/snapshot")
    if (
        not isinstance(path_template, str)
        or len(path_template) > 512
        or not path_template.startswith("/")
        or path_template.count("{symbol}") != 1
        or "?" in path_template
        or "#" in path_template
    ):
        raise ProviderConfigError(
            f"{location}.path_template must be a relative path containing exactly one {{symbol}}"
        )
    normalized["path_template"] = path_template
    normalized["price_change_threshold"] = _setting_float(
        settings, "price_change_threshold", 5.0, location, 0.01, 1000.0
    )
    normalized["volume_multiplier"] = _setting_float(
        settings, "volume_multiplier", 3.0, location, 0.01, 1000000.0
    )
    normalized["gap_threshold"] = _setting_float(
        settings, "gap_threshold", 3.0, location, 0.01, 1000.0
    )
    normalized["cooldown_seconds"] = _setting_int(
        settings, "cooldown_seconds", 1800, location, 60, 86400
    )
    normalized["max_quote_age_seconds"] = _setting_int(
        settings, "max_quote_age_seconds", 345600, location, 60, 604800
    )
    require_timestamp = settings.get("require_quote_timestamp", False)
    if not isinstance(require_timestamp, bool):
        raise ProviderConfigError(f"{location}.require_quote_timestamp must be bool")
    normalized["require_quote_timestamp"] = require_timestamp
    return normalized


def _validate_x(
    settings: Mapping[str, Any], allowed_hosts: tuple[str, ...], location: str
) -> dict[str, Any]:
    normalized = dict(settings)
    if not str(settings.get("user_id", "")).isdigit():
        raise ProviderConfigError(f"{location}.user_id must be numeric")
    api_base, api_host = _plain_https_base(
        settings.get("api_base_url", "https://api.x.com/2"),
        f"{location}.api_base_url",
    )
    trusted = {"api.x.com", "api.twitter.com"}
    if api_host not in (set(allowed_hosts) or trusted):
        raise ProviderConfigError(
            f"{location}.api_base_url host is not allowed for its credential"
        )
    normalized["api_base_url"] = api_base
    normalized["max_pages_per_poll"] = _setting_int(
        settings, "max_pages_per_poll", 5, location, 1, 100
    )
    exclude_replies = settings.get("exclude_replies", True)
    if not isinstance(exclude_replies, bool):
        raise ProviderConfigError(f"{location}.exclude_replies must be bool")
    normalized["exclude_replies"] = exclude_replies
    return normalized


def _validate_imap(
    settings: Mapping[str, Any], max_response_bytes: int, location: str
) -> dict[str, Any]:
    normalized = dict(settings)
    host = settings.get("host")
    if not isinstance(host, str) or not host.strip() or any(character.isspace() for character in host):
        raise ProviderConfigError(f"{location}.host is required")
    normalized["host"] = host.strip()
    normalized["port"] = _setting_int(settings, "port", 993, location, 1, 65535)
    normalized["batch_size"] = _setting_int(settings, "batch_size", 100, location, 1, 500)
    normalized["max_message_bytes"] = _setting_int(
        settings,
        "max_message_bytes",
        max_response_bytes,
        location,
        1024,
        10485760,
    )
    return normalized


def _validate_host(settings: Mapping[str, Any], location: str) -> dict[str, Any]:
    normalized = dict(settings)
    for key, default in (
        ("disk_used_percent", 90.0),
        ("inode_used_percent", 90.0),
        ("memory_used_percent", 90.0),
    ):
        normalized[key] = _setting_float(settings, key, default, location, 0.1, 100.0)
    normalized["load1"] = _setting_float(
        settings,
        "load1",
        float(max(1, os.cpu_count() or 1)),
        location,
        0.01,
        1000000.0,
    )
    return normalized


def validate_provider_configuration(
    *,
    kind: str,
    url: str | None,
    allowed_hosts: tuple[str, ...],
    settings: Mapping[str, Any],
    enabled: bool,
    max_response_bytes: int,
    location: str,
    registry: ProviderRegistry = DEFAULT_PROVIDER_REGISTRY,
) -> ProviderValidationResult:
    """Validate one provider while preserving disabled-source compatibility."""

    spec = registry.require(kind)
    source_location = (
        location.removesuffix(".settings")
        if location.endswith(".settings")
        else location
    )
    if spec.url_mode is UrlMode.REQUIRED:
        if url is None:
            raise ProviderConfigError(f"missing {source_location}.url")
        host = (urlsplit(url).hostname or "").lower()
        if host not in allowed_hosts:
            raise ProviderConfigError(
                f"{source_location}.url host must be present in allowed_hosts"
            )
    if enabled and not spec.runtime_collector:
        raise ProviderConfigError(
            f"{source_location}.kind {kind} is experimental and has no runtime collector; keep it disabled"
        )
    normalized: dict[str, Any] = dict(settings)
    if enabled:
        _validate_declared_settings(spec, settings, location)
        if kind == "market":
            normalized = _validate_market(settings, allowed_hosts, location)
        elif kind == "x":
            normalized = _validate_x(settings, allowed_hosts, location)
        elif kind == "youtube" and not str(settings.get("channel_id", "")).strip():
            raise ProviderConfigError(f"{location}.channel_id is required")
        elif kind == "imap":
            normalized = _validate_imap(settings, max_response_bytes, location)
        elif kind == "host":
            normalized = _validate_host(settings, location)

    credential_refs: dict[str, str] = {}
    if enabled:
        for requirement in spec.credentials:
            if requirement.required_when_enabled:
                credential_refs[requirement.setting_name] = _secret_ref(
                    settings, requirement.setting_name, location
                )
    return ProviderValidationResult(normalized, credential_refs)
