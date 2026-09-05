from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Any, Callable, Mapping, Protocol

from .config import SourceConfig
from .models import FeedFetchResult, Observation, SourceState
from .util import truncate


class MarketError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class MarketProvider(Protocol):
    def latest(self, symbol: str) -> Mapping[str, Any]: ...


class JsonMarketProvider:
    """Bounded JSON provider with credentials tied to an approved API host."""

    _TRUSTED_HOSTS = {
        "data.alpaca.markets",
        "api.alpaca.markets",
        "paper-api.alpaca.markets",
    }

    def __init__(
        self,
        source: SourceConfig,
        environment: Mapping[str, str] | None = None,
        opener: Any | None = None,
    ) -> None:
        settings = source.settings or {}
        base = str(settings.get("api_base_url", "")).rstrip("/")
        if not base:
            raise MarketError(f"market source {source.id} has no api_base_url")
        parsed = urllib.parse.urlsplit(base)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise MarketError("market api_base_url must be a plain HTTPS origin/path")
        allowed = {host.lower() for host in source.allowed_hosts} or self._TRUSTED_HOSTS
        if parsed.hostname.lower() not in allowed:
            raise MarketError(
                "market api_base_url host is not allowed for the configured credential"
            )
        self.source = source
        self.base_url = base
        self.path_template = str(
            settings.get("path_template", "/v2/stocks/{symbol}/snapshot")
        )
        if self.path_template.count("{symbol}") != 1:
            raise MarketError("market path_template must contain exactly one {symbol}")
        self.timeout = source.request_timeout_seconds
        env = os.environ if environment is None else environment
        self.api_key = env.get(str(settings.get("api_key_env", "")), "")
        self.api_secret = env.get(str(settings.get("api_secret_env", "")), "")
        self._opener = opener or urllib.request.build_opener(_NoRedirect())

    def latest(self, symbol: str) -> Mapping[str, Any]:
        path = self.path_template.format(symbol=urllib.parse.quote(symbol, safe="."))
        request = urllib.request.Request(
            self.base_url + "/" + path.lstrip("/"),
            headers={
                "Accept": "application/json",
                "User-Agent": "Argus/0.7 (market monitor)",
                **({"APCA-API-KEY-ID": self.api_key} if self.api_key else {}),
                **({"APCA-API-SECRET-KEY": self.api_secret} if self.api_secret else {}),
            },
            method="GET",
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                payload = response.read(self.source.max_response_bytes + 1)
        except urllib.error.HTTPError as exc:
            raise MarketError(f"market api returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MarketError(f"market request failed: {type(exc).__name__}") from exc
        if len(payload) > self.source.max_response_bytes:
            raise MarketError("market response exceeded configured size limit")
        try:
            value = json.loads(payload)
        except (TypeError, ValueError) as exc:
            raise MarketError("market API returned invalid JSON") from exc
        if not isinstance(value, Mapping):
            raise MarketError("market API returned a non-object")
        return value


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _timestamp_epoch(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or str(value).replace(".", "", 1).isdigit():
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(numeric):
            return None
        absolute = abs(numeric)
        if absolute >= 1e17:
            numeric /= 1e9
        elif absolute >= 1e14:
            numeric /= 1e6
        elif absolute >= 1e11:
            numeric /= 1e3
        return int(numeric)
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError, OverflowError):
        return None


def _quote(
    value: Mapping[str, Any],
) -> tuple[float | None, float | None, Any, float | None, float | None, float | None]:
    trade = value.get("latestTrade") if isinstance(value.get("latestTrade"), Mapping) else value.get("trade")
    trade = trade if isinstance(trade, Mapping) else value
    if not isinstance(trade, Mapping):
        return None, None, None, None, None, None
    daily = value.get("dailyBar") if isinstance(value.get("dailyBar"), Mapping) else {}
    previous = value.get("prevDailyBar") if isinstance(value.get("prevDailyBar"), Mapping) else {}
    price = _number(trade.get("p", trade.get("price", trade.get("last"))))
    if price is None and isinstance(daily, Mapping):
        price = _number(daily.get("c", daily.get("close")))
    volume = _number(daily.get("v", daily.get("volume"))) if isinstance(daily, Mapping) else None
    if volume is None:
        volume = _number(trade.get("s", trade.get("size", trade.get("volume"))))
    timestamp = trade.get("t", trade.get("timestamp"))
    previous_close = _number(previous.get("c", previous.get("close"))) if isinstance(previous, Mapping) else None
    previous_volume = _number(previous.get("v", previous.get("volume"))) if isinstance(previous, Mapping) else None
    day_open = _number(daily.get("o", daily.get("open"))) if isinstance(daily, Mapping) else None
    return price, volume, timestamp, previous_close, previous_volume, day_open


def _safe_click_url(value: Any) -> str:
    parsed = urllib.parse.urlsplit(str(value or ""))
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return ""
    return str(value)


def _old_condition(old: Mapping[str, Any], name: str) -> dict[str, Any]:
    conditions = old.get("conditions")
    if isinstance(conditions, Mapping) and isinstance(conditions.get(name), Mapping):
        state = dict(conditions[name])
        return {
            "active": bool(state.get("active")),
            "notified": bool(state.get("notified")),
            "last_notified_at": int(state.get("last_notified_at", 0) or 0),
        }
    active = set(old.get("active", [])) if isinstance(old.get("active"), list) else set()
    return {
        "active": name in active,
        "notified": name in active,
        "last_notified_at": int(old.get("last_event", 0) or 0),
    }


class MarketCollector:
    CONDITION_LABELS = {
        "price_move": "价格异动",
        "volume_spike": "成交量异动",
        "gap": "开盘跳空",
    }

    def __init__(
        self,
        config: SourceConfig,
        provider: MarketProvider | None = None,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.provider = provider or JsonMarketProvider(config)
        self.now_factory = now_factory or (lambda: datetime.now(UTC))

    def fetch(self, state: SourceState) -> FeedFetchResult:
        settings = self.config.settings or {}
        raw_symbols = settings.get("symbols", [])
        if not isinstance(raw_symbols, list) or not raw_symbols:
            raise MarketError(f"market source {self.config.id} has no symbols")
        symbols = tuple(dict.fromkeys(
            str(symbol).strip().upper() for symbol in raw_symbols if str(symbol).strip()
        ))
        if not symbols:
            raise MarketError(f"market source {self.config.id} has no valid symbols")
        try:
            previous = json.loads(state.cursor or "{}")
        except (TypeError, ValueError):
            previous = {}
        if not isinstance(previous, dict):
            previous = {}

        observations: list[Observation] = []
        warnings: list[str] = []
        next_cursor: dict[str, Any] = dict(previous)
        price_threshold = float(settings.get("price_change_threshold", 5.0))
        volume_multiplier = float(settings.get("volume_multiplier", 3.0))
        gap_threshold = float(settings.get("gap_threshold", 3.0))
        cooldown = max(60, int(settings.get("cooldown_seconds", 1800)))
        max_age = max(60, int(settings.get("max_quote_age_seconds", 345600)))
        require_timestamp = bool(settings.get("require_quote_timestamp", False))
        now = self.now_factory().astimezone(UTC)
        now_epoch = int(now.timestamp())

        for symbol in symbols:
            try:
                quote = self.provider.latest(symbol)
                price, volume, timestamp, previous_close, previous_volume, day_open = _quote(quote)
                if price is None or price <= 0:
                    raise MarketError("quote has no finite positive price")
                quote_epoch = _timestamp_epoch(timestamp)
                if timestamp not in (None, "") and quote_epoch is None:
                    raise MarketError("quote timestamp is invalid")
                if require_timestamp and quote_epoch is None:
                    raise MarketError("quote timestamp is required")
                if quote_epoch is not None and (
                    quote_epoch < now_epoch - max_age or quote_epoch > now_epoch + 300
                ):
                    raise MarketError("quote timestamp is outside the freshness window")

                old = previous.get(symbol) if isinstance(previous.get(symbol), Mapping) else {}
                old_price = _number(old.get("price")) if isinstance(old, Mapping) else None
                old_volume = _number(old.get("volume")) if isinstance(old, Mapping) else None
                reference_price = previous_close or old_price
                change = ((price - reference_price) / reference_price * 100.0) if reference_price else 0.0
                reference_volume = previous_volume or old_volume
                volume_ratio = (
                    volume / reference_volume
                    if volume is not None and reference_volume and reference_volume > 0
                    else 0.0
                )
                gap = (
                    (day_open - previous_close) / previous_close * 100.0
                    if day_open is not None and previous_close
                    else 0.0
                )

                old_states = {
                    name: _old_condition(old, name) for name in self.CONDITION_LABELS
                }
                active = {
                    "price_move": bool(reference_price) and abs(change) >= price_threshold * (0.7 if old_states["price_move"]["active"] else 1.0),
                    "volume_spike": volume_ratio >= volume_multiplier * (0.8 if old_states["volume_spike"]["active"] else 1.0),
                    "gap": abs(gap) >= gap_threshold * (0.7 if old_states["gap"]["active"] else 1.0),
                }
                condition_states: dict[str, dict[str, Any]] = {}
                click_url = _safe_click_url(quote.get("url"))
                for condition, is_active in active.items():
                    old_state = old_states[condition]
                    notified = bool(old_state["notified"])
                    last_notified = int(old_state["last_notified_at"])
                    should_open = is_active and (not old_state["active"] or not notified)
                    if should_open and now_epoch - last_notified >= cooldown:
                        direction = "上涨" if change > 0 else "下跌"
                        summary = (
                            f"最新价 {price:.6g}；相对昨收 {change:+.2f}%；"
                            f"跳空 {gap:+.2f}%；当日/前日成交量 {volume_ratio:.2f}x；"
                            f"触发：{self.CONDITION_LABELS[condition]}。"
                        )
                        observations.append(Observation(
                            source_id=self.config.id,
                            publisher=self.config.publisher,
                            dedupe_scope=self.config.dedupe_scope,
                            external_id=f"{symbol}:{condition}:open:{now_epoch}",
                            published_at=now,
                            title=f"{symbol} {self.CONDITION_LABELS[condition]}：{direction} {change:+.2f}%",
                            summary=truncate(summary, 4000),
                            url=click_url,
                            attributes={
                                "section": self.config.section,
                                "symbol": symbol,
                                "incident_key": f"symbol:{symbol}:{condition}",
                                "stateful": True,
                                "event_types": [condition],
                                "change_pct": round(change, 4),
                                "gap_pct": round(gap, 4),
                                "volume_ratio": round(volume_ratio, 4),
                            },
                        ))
                        notified = True
                        last_notified = now_epoch
                    elif not is_active and old_state["active"] and notified:
                        observations.append(Observation(
                            source_id=self.config.id,
                            publisher=self.config.publisher,
                            dedupe_scope=self.config.dedupe_scope,
                            external_id=f"{symbol}:{condition}:recovery:{now_epoch}",
                            published_at=now,
                            title=f"{symbol} {self.CONDITION_LABELS[condition]}已恢复",
                            summary=f"当前相对昨收 {change:+.2f}%；该检查项已回到恢复阈值内。",
                            url=click_url,
                            attributes={
                                "section": self.config.section,
                                "symbol": symbol,
                                "incident_key": f"symbol:{symbol}:{condition}",
                                "stateful": True,
                                "recovery": True,
                                "recovered": [condition],
                            },
                        ))
                        notified = False
                    condition_states[condition] = {
                        "active": is_active,
                        "notified": notified if is_active else False,
                        "last_notified_at": last_notified,
                    }

                next_cursor[symbol] = {
                    "price": price,
                    "volume": volume,
                    "timestamp": timestamp,
                    "active": sorted(name for name, value in active.items() if value),
                    "conditions": condition_states,
                    "last_event": max(
                        (int(value["last_notified_at"]) for value in condition_states.values()),
                        default=0,
                    ),
                }
            except Exception as exc:
                warnings.append(f"{symbol}:{type(exc).__name__}")
                continue

        if len(warnings) == len(symbols):
            raise MarketError("all configured market symbols failed: " + ", ".join(warnings))
        return FeedFetchResult(
            tuple(observations),
            None,
            None,
            not_modified=not observations,
            cursor=json.dumps(next_cursor, sort_keys=True),
            warnings=tuple(warnings),
        )
