from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Any, Mapping, Protocol

from .config import SourceConfig
from .models import FeedFetchResult, Observation, SourceState
from .util import truncate


class MarketError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class MarketProvider(Protocol):
    def latest(self, symbol: str) -> Mapping[str, Any]:
        ...


class JsonMarketProvider:
    """Small provider for JSON APIs such as Alpaca's latest trade endpoint.

    The endpoint template is configurable so the collector can be used with a
    self-hosted proxy or another provider without adding a dependency.
    """

    def __init__(self, source: SourceConfig, environment: Mapping[str, str] | None = None) -> None:
        settings = source.settings or {}
        base = str(settings.get("api_base_url", "")).rstrip("/")
        if not base:
            raise MarketError(f"market source {source.id} has no api_base_url")
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme != "https" or not parsed.hostname:
            raise MarketError("market api_base_url must be HTTPS")
        self.source = source
        self.base_url = base
        self.path_template = str(settings.get("path_template", "/v2/stocks/{symbol}/snapshot"))
        self.timeout = source.request_timeout_seconds
        env = os.environ if environment is None else environment
        self.api_key = env.get(str(settings.get("api_key_env", "")), "")
        self.api_secret = env.get(str(settings.get("api_secret_env", "")), "")
        self._opener = urllib.request.build_opener(_NoRedirect())

    def latest(self, symbol: str) -> Mapping[str, Any]:
        path = self.path_template.format(symbol=urllib.parse.quote(symbol, safe="."))
        request = urllib.request.Request(
            self.base_url + "/" + path.lstrip("/"),
            headers={
                "Accept": "application/json",
                "User-Agent": "Argus/0.6 (market monitor)",
                **({"APCA-API-KEY-ID": self.api_key} if self.api_key else {}),
                **({"APCA-API-SECRET-KEY": self.api_secret} if self.api_secret else {}),
            },
            method="GET",
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.status < 200 or response.status >= 300:
                    raise MarketError(f"market api returned HTTP {response.status}")
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
    return result if result == result else None


def _quote(value: Mapping[str, Any]) -> tuple[float | None, float | None, str, float | None, float | None, float | None]:
    trade = value.get("latestTrade") if isinstance(value.get("latestTrade"), Mapping) else value.get("trade")
    trade = trade if isinstance(trade, Mapping) else value
    if not isinstance(trade, Mapping):
        return None, None, "", None, None, None
    daily = value.get("dailyBar") if isinstance(value.get("dailyBar"), Mapping) else {}
    previous = value.get("prevDailyBar") if isinstance(value.get("prevDailyBar"), Mapping) else {}
    price = _number(trade.get("p", trade.get("price", trade.get("last"))))
    if price is None and isinstance(daily, Mapping):
        price = _number(daily.get("c", daily.get("close")))
    volume = _number(daily.get("v", daily.get("volume"))) if isinstance(daily, Mapping) else None
    if volume is None:
        volume = _number(trade.get("s", trade.get("size", trade.get("volume"))))
    timestamp = str(trade.get("t", trade.get("timestamp", "")))
    previous_close = _number(previous.get("c", previous.get("close"))) if isinstance(previous, Mapping) else None
    previous_volume = _number(previous.get("v", previous.get("volume"))) if isinstance(previous, Mapping) else None
    day_open = _number(daily.get("o", daily.get("open"))) if isinstance(daily, Mapping) else None
    return price, volume, timestamp, previous_close, previous_volume, day_open


class MarketCollector:
    def __init__(self, config: SourceConfig, provider: MarketProvider | None = None) -> None:
        self.config = config
        self.provider = provider or JsonMarketProvider(config)

    def fetch(self, state: SourceState) -> FeedFetchResult:
        settings = self.config.settings or {}
        raw_symbols = settings.get("symbols", [])
        if not isinstance(raw_symbols, list) or not raw_symbols:
            raise MarketError(f"market source {self.config.id} has no symbols")
        symbols = tuple(str(symbol).strip().upper() for symbol in raw_symbols if str(symbol).strip())
        if not symbols:
            raise MarketError(f"market source {self.config.id} has no valid symbols")
        try:
            previous = json.loads(state.cursor or "{}")
        except (TypeError, ValueError):
            previous = {}
        if not isinstance(previous, dict):
            previous = {}
        observations: list[Observation] = []
        next_cursor: dict[str, Any] = dict(previous)
        price_threshold = float(settings.get("price_change_threshold", 5.0))
        volume_multiplier = float(settings.get("volume_multiplier", 3.0))
        gap_threshold = float(settings.get("gap_threshold", 3.0))
        cooldown = max(60, int(settings.get("cooldown_seconds", 1800)))
        now = datetime.now(UTC)
        now_epoch = int(now.timestamp())
        for symbol in symbols:
            quote = self.provider.latest(symbol)
            price, volume, timestamp, previous_close, previous_volume, day_open = _quote(quote)
            if price is None or price <= 0:
                continue
            old = previous.get(symbol) if isinstance(previous.get(symbol), Mapping) else {}
            old_price = _number(old.get("price")) if isinstance(old, Mapping) else None
            old_volume = _number(old.get("volume")) if isinstance(old, Mapping) else None
            reference_price = previous_close or old_price
            change = ((price - reference_price) / reference_price * 100.0) if reference_price else 0.0
            reference_volume = previous_volume or old_volume
            volume_ratio = (volume / reference_volume) if volume and reference_volume and reference_volume > 0 else 0.0
            gap = ((day_open - previous_close) / previous_close * 100.0) if day_open and previous_close else 0.0
            old_active = set(old.get("active", [])) if isinstance(old, Mapping) and isinstance(old.get("active", []), list) else set()
            event_types: list[str] = []
            if reference_price and abs(change) >= price_threshold * (0.7 if "price_move" in old_active else 1.0):
                event_types.append("price_move")
            if volume_ratio >= volume_multiplier * (0.8 if "volume_spike" in old_active else 1.0):
                event_types.append("volume_spike")
            if abs(gap) >= gap_threshold:
                event_types.append("gap")
            active = set(event_types)
            newly_active = sorted(active - old_active)
            recovered = sorted(old_active - active)
            last_event = int(old.get("last_event", 0)) if isinstance(old, Mapping) else 0
            if newly_active and now_epoch - last_event >= cooldown:
                direction = "上涨" if change > 0 else "下跌"
                title = f"{symbol} 市场异动：{direction} {change:+.2f}%"
                summary = f"最新价 {price:.6g}；相对昨收 {change:+.2f}%；跳空 {gap:+.2f}%；当日/前日成交量 {volume_ratio:.2f}x；新触发：{', '.join(newly_active)}。"
                external_id = f"{symbol}:{','.join(newly_active)}:{now_epoch // cooldown}"
                observations.append(Observation(
                    source_id=self.config.id,
                    publisher=self.config.publisher,
                    dedupe_scope=self.config.dedupe_scope,
                    external_id=external_id,
                    published_at=now,
                    title=truncate(title, 1000),
                    summary=truncate(summary, 4000),
                    url=str(quote.get("url", "")) if isinstance(quote, Mapping) else "",
                    attributes={"section": self.config.section, "symbol": symbol, "incident_key": f"symbol:{symbol}", "stateful": True, "change_pct": round(change, 4), "gap_pct": round(gap, 4), "volume_ratio": round(volume_ratio, 4), "event_types": event_types},
                ))
                last_event = now_epoch
            elif recovered:
                observations.append(Observation(
                    source_id=self.config.id, publisher=self.config.publisher,
                    dedupe_scope=self.config.dedupe_scope,
                    external_id=f"{symbol}:recovery:{now_epoch // cooldown}", published_at=now,
                    title=f"{symbol} 市场异动已恢复",
                    summary=f"已恢复项目：{', '.join(recovered)}；当前相对昨收 {change:+.2f}%。",
                    url=str(quote.get("url", "")),
                    attributes={"section": self.config.section, "symbol": symbol, "incident_key": f"symbol:{symbol}", "stateful": True, "recovery": True, "recovered": recovered},
                ))
            next_cursor[symbol] = {"price": price, "volume": volume, "timestamp": timestamp, "last_event": last_event, "active": sorted(active)}
        return FeedFetchResult(tuple(observations), None, None, not_modified=not observations, cursor=json.dumps(next_cursor, sort_keys=True))
