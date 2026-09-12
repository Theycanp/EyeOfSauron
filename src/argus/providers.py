from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Callable, Mapping


_KIND_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")



class ProviderConfigError(ValueError):
    """Provider-specific configuration is invalid."""


class ProviderRuntimeError(RuntimeError):
    """A configured provider has no usable runtime collector."""


class SourceCapability(StrEnum):
    FEED_ITEMS = "feed_items"
    NEWS_ITEMS = "news_items"
    VIDEO_UPDATES = "video_updates"
    SOCIAL_POSTS = "social_posts"
    EMAIL_MESSAGES = "email_messages"
    MARKET_QUOTES = "market_quotes"
    HOST_HEALTH = "host_health"
    STATEFUL_INCIDENTS = "stateful_incidents"
    SENSOR_EVENTS = "sensor_events"
    OUTBOUND_LIVENESS = "outbound_liveness"


class TestMode(StrEnum):
    BOUNDED_FETCH = "bounded_fetch"
    READ_ONLY_API = "read_only_api"
    READ_ONLY_MAILBOX = "read_only_mailbox"
    LOCAL_PROBE = "local_probe"
    CONFIG_ONLY = "config_only"


class UrlMode(StrEnum):
    REQUIRED = "required"
    DERIVED = "derived"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class SettingField:
    name: str
    value_type: str
    required_when_enabled: bool = False
    secret_reference: bool = False
    default: Any = None
    minimum: float | None = None
    maximum: float | None = None
    description: str = ""

    def describe(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "type": self.value_type,
            "required_when_enabled": self.required_when_enabled,
            "secret_reference": self.secret_reference,
        }
        if self.default is not None:
            result["default"] = self.default
        if self.minimum is not None:
            result["minimum"] = self.minimum
        if self.maximum is not None:
            result["maximum"] = self.maximum
        if self.description:
            result["description"] = self.description
        return result


@dataclass(frozen=True, slots=True)
class CredentialRequirement:
    setting_name: str
    purpose: str
    required_when_enabled: bool = True

    def describe(self) -> dict[str, Any]:
        return {
            "setting_name": self.setting_name,
            "purpose": self.purpose,
            "required_when_enabled": self.required_when_enabled,
            "stores_environment_variable_name_only": True,
        }


@dataclass(frozen=True, slots=True)
class ProviderTestStrategy:
    mode: TestMode
    side_effect_free: bool
    requires_credentials: bool
    description: str

    def describe(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "side_effect_free": self.side_effect_free,
            "requires_credentials": self.requires_credentials,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    kind: str
    display_name: str
    capabilities: frozenset[SourceCapability]
    url_mode: UrlMode
    settings: tuple[SettingField, ...]
    credentials: tuple[CredentialRequirement, ...]
    test_strategy: ProviderTestStrategy
    runtime_collector: bool = True

    def describe(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "display_name": self.display_name,
            "capabilities": sorted(capability.value for capability in self.capabilities),
            "url_mode": self.url_mode.value,
            "runtime_collector": self.runtime_collector,
            "settings": [field.describe() for field in self.settings],
            "credentials": [item.describe() for item in self.credentials],
            "test_strategy": self.test_strategy.describe(),
        }


CollectorFactory = Callable[[Any], Any]


class ProviderRegistry:
    """Immutable provider metadata plus a small runtime factory boundary."""

    def __init__(self, specs: tuple[ProviderSpec, ...]) -> None:
        by_kind: dict[str, ProviderSpec] = {}
        for spec in specs:
            if not _KIND_RE.fullmatch(spec.kind):
                raise ValueError(f"invalid provider kind: {spec.kind}")
            if spec.kind in by_kind:
                raise ValueError(f"duplicate provider kind: {spec.kind}")
            field_names = [field.name for field in spec.settings]
            if any(not _FIELD_RE.fullmatch(name) for name in field_names):
                raise ValueError(f"invalid provider setting name in {spec.kind}")
            if len(field_names) != len(set(field_names)):
                raise ValueError(f"duplicate provider setting in {spec.kind}")
            for field in spec.settings:
                if (field.minimum is not None or field.maximum is not None) and (
                    field.value_type not in {"integer", "number"}
                ):
                    raise ValueError(
                        f"provider {spec.kind}.{field.name} has bounds on a non-numeric field"
                    )
                if (
                    field.minimum is not None
                    and field.maximum is not None
                    and field.minimum > field.maximum
                ):
                    raise ValueError(
                        f"provider {spec.kind}.{field.name} has inverted bounds"
                    )
            credential_names = [item.setting_name for item in spec.credentials]
            if len(credential_names) != len(set(credential_names)):
                raise ValueError(f"duplicate provider credential in {spec.kind}")
            fields_by_name = {field.name: field for field in spec.settings}
            for credential in spec.credentials:
                field = fields_by_name.get(credential.setting_name)
                if field is None or not field.secret_reference:
                    raise ValueError(
                        f"provider credential {spec.kind}.{credential.setting_name} "
                        "must reference a declared secret field"
                    )
            if any(
                isinstance(field.default, (dict, list, set)) for field in spec.settings
            ):
                raise ValueError(f"provider {spec.kind} has a mutable field default")
            by_kind[spec.kind] = spec
        self._specs = MappingProxyType(by_kind)

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def get(self, kind: str) -> ProviderSpec | None:
        return self._specs.get(kind)

    def require(self, kind: str) -> ProviderSpec:
        spec = self.get(kind)
        if spec is None:
            raise ProviderConfigError(f"unsupported provider kind: {kind}")
        return spec

    def describe(self) -> tuple[dict[str, Any], ...]:
        return tuple(spec.describe() for spec in self._specs.values())

    def extend(self, *specs: ProviderSpec) -> "ProviderRegistry":
        """Return a new registry; the process-wide default remains immutable."""
        return ProviderRegistry(tuple(self._specs.values()) + specs)

    def build_collector(
        self,
        source: Any,
        factories: Mapping[str, CollectorFactory],
    ) -> Any | None:
        if not source.enabled:
            return None
        spec = self.require(source.kind)
        if not spec.runtime_collector:
            raise ProviderRuntimeError(
                f"source kind {source.kind} has no runtime collector; keep it disabled"
            )
        factory = factories.get(source.kind)
        if factory is None:
            raise ProviderRuntimeError(
                f"source kind {source.kind} has no registered collector factory"
            )
        return factory(source)


_READ_ONLY_FETCH = ProviderTestStrategy(
    TestMode.BOUNDED_FETCH,
    side_effect_free=True,
    requires_credentials=False,
    description="Fetch one bounded response, validate transport and parse it without persisting state.",
)
_READ_ONLY_API = ProviderTestStrategy(
    TestMode.READ_ONLY_API,
    side_effect_free=True,
    requires_credentials=True,
    description="Call one read-only provider endpoint with configured credential references.",
)
_READ_ONLY_MAILBOX = ProviderTestStrategy(
    TestMode.READ_ONLY_MAILBOX,
    side_effect_free=True,
    requires_credentials=True,
    description="Open the mailbox read-only and fetch at most one bounded message.",
)
_LOCAL_PROBE = ProviderTestStrategy(
    TestMode.LOCAL_PROBE,
    side_effect_free=True,
    requires_credentials=False,
    description="Run local probes without changing units, sockets, filesystems, or host state.",
)
_CONFIG_ONLY = ProviderTestStrategy(
    TestMode.CONFIG_ONLY,
    side_effect_free=True,
    requires_credentials=False,
    description="Validate configuration only; no runtime collector is currently registered.",
)


def _field(
    name: str,
    value_type: str,
    *,
    required: bool = False,
    secret: bool = False,
    default: Any = None,
    minimum: float | None = None,
    maximum: float | None = None,
    description: str = "",
) -> SettingField:
    return SettingField(
        name,
        value_type,
        required,
        secret,
        default,
        minimum,
        maximum,
        description,
    )


DEFAULT_PROVIDER_REGISTRY = ProviderRegistry((
    ProviderSpec(
        "official_list",
        "官方公告列表",
        frozenset({SourceCapability.NEWS_ITEMS}),
        UrlMode.REQUIRED,
        (
            _field("article_url_prefixes", "string_array", required=True,
                   description="Exact HTTPS article directory prefixes; only dated links are collected."),
            _field("timezone", "string", default="UTC"),
            _field("index_format", "string", default="html"),
            _field("max_content_age_seconds", "integer", default=0, minimum=0, maximum=31536000),
            _field("content_policy", "string", default="public_document_full_text"),
        ),
        (),
        _READ_ONLY_FETCH,
    ),
    ProviderSpec(
        "rss",
        "RSS / Atom",
        frozenset({SourceCapability.FEED_ITEMS, SourceCapability.NEWS_ITEMS}),
        UrlMode.REQUIRED,
        (
            _field("max_content_age_seconds", "integer", default=0, minimum=0, maximum=31536000,
                   description="Maximum age of the newest dated entry; zero disables content freshness checks."),
            _field("content_policy", "string", default="feed_metadata_and_original_link_only",
                   description="Metadata-only, authorized feed full text, or public official document extraction."),
            _field("content_max_characters", "integer", default=100000, minimum=1000, maximum=200000),
            _field("content_max_response_bytes", "integer", default=4194304, minimum=1024, maximum=10485760),
            _field("content_timeout_seconds", "integer", default=20, minimum=1, maximum=120),
            _field("headline_from_summary", "boolean", default=False,
                   description="Use actual bulletin text when feed titles only identify a category."),
        ),
        (),
        _READ_ONLY_FETCH,
    ),
    ProviderSpec(
        "youtube",
        "YouTube channel feed",
        frozenset({SourceCapability.FEED_ITEMS, SourceCapability.VIDEO_UPDATES}),
        UrlMode.DERIVED,
        (_field("channel_id", "string", required=True, description="Stable YouTube channel ID."),),
        (),
        _READ_ONLY_FETCH,
    ),
    ProviderSpec(
        "x",
        "X user timeline",
        frozenset({SourceCapability.SOCIAL_POSTS}),
        UrlMode.NONE,
        (
            _field("user_id", "numeric_string", required=True),
            _field("bearer_token_env", "environment_variable", required=True, secret=True),
            _field("api_base_url", "https_url", default="https://api.x.com/2"),
            _field("max_pages_per_poll", "integer", default=5, minimum=1, maximum=100),
            _field("exclude_replies", "boolean", default=True),
        ),
        (CredentialRequirement("bearer_token_env", "X API bearer token"),),
        _READ_ONLY_API,
    ),
    ProviderSpec(
        "imap",
        "IMAP mailbox",
        frozenset({SourceCapability.EMAIL_MESSAGES}),
        UrlMode.NONE,
        (
            _field("host", "hostname", required=True),
            _field("port", "integer", default=993, minimum=1, maximum=65535),
            _field("mailbox", "string", default="INBOX"),
            _field("search", "string", default="ALL"),
            _field("username_env", "environment_variable", required=True, secret=True),
            _field("password_env", "environment_variable", required=True, secret=True),
            _field("batch_size", "integer", default=100, minimum=1, maximum=500),
            _field("max_message_bytes", "integer", minimum=1024, maximum=10485760),
        ),
        (
            CredentialRequirement("username_env", "IMAP username"),
            CredentialRequirement("password_env", "IMAP password or app password"),
        ),
        _READ_ONLY_MAILBOX,
    ),
    ProviderSpec(
        "market",
        "Alpaca market data",
        frozenset({SourceCapability.MARKET_QUOTES, SourceCapability.STATEFUL_INCIDENTS}),
        UrlMode.NONE,
        (
            _field("symbols", "string_array", required=True),
            _field("api_base_url", "https_url", required=True),
            _field("path_template", "path_template", default="/v2/stocks/{symbol}/snapshot"),
            _field("api_key_env", "environment_variable", required=True, secret=True),
            _field("api_secret_env", "environment_variable", required=True, secret=True),
            _field("price_change_threshold", "number", default=5.0, minimum=0.01, maximum=1000.0),
            _field("volume_multiplier", "number", default=3.0, minimum=0.01, maximum=1000000.0),
            _field("gap_threshold", "number", default=3.0, minimum=0.01, maximum=1000.0),
            _field("cooldown_seconds", "integer", default=1800, minimum=60, maximum=86400),
            _field("max_quote_age_seconds", "integer", default=345600, minimum=60, maximum=604800),
            _field("require_quote_timestamp", "boolean", default=False),
        ),
        (
            CredentialRequirement("api_key_env", "market data API key"),
            CredentialRequirement("api_secret_env", "market data API secret"),
        ),
        _READ_ONLY_API,
    ),
    ProviderSpec(
        "host",
        "Local host health",
        frozenset({SourceCapability.HOST_HEALTH, SourceCapability.STATEFUL_INCIDENTS}),
        UrlMode.NONE,
        (
            _field("paths", "string_array", default=("/",)),
            _field("units", "string_array", default=()),
            _field("disk_used_percent", "number", default=90.0, minimum=0.1, maximum=100.0),
            _field("inode_used_percent", "number", default=90.0, minimum=0.1, maximum=100.0),
            _field("memory_used_percent", "number", default=90.0, minimum=0.1, maximum=100.0),
            _field("load1", "number", minimum=0.01, maximum=1000000.0),
            _field("allowed_listen_ports", "listen_endpoint_array", default=()),
            _field("required_listen_ports", "listen_endpoint_array", default=()),
        ),
        (),
        _LOCAL_PROBE,
    ),
    ProviderSpec(
        "mqtt",
        "MQTT sensor events",
        frozenset({SourceCapability.SENSOR_EVENTS, SourceCapability.STATEFUL_INCIDENTS}),
        UrlMode.NONE,
        (),
        (),
        _CONFIG_ONLY,
        runtime_collector=False,
    ),
    ProviderSpec(
        "heartbeat",
        "External heartbeat",
        frozenset({SourceCapability.OUTBOUND_LIVENESS}),
        UrlMode.NONE,
        (),
        (),
        _CONFIG_ONLY,
        runtime_collector=False,
    ),
))
