"""Bounded Open-Meteo adapter for local weather and optional dust forecasts."""

from __future__ import annotations

import json
import math
import re
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Any, Mapping

from .weather import ForecastHour, WeatherForecast, WeatherSubscription


_FORECAST_ORIGIN = "https://api.open-meteo.com"
_GEOCODE_ORIGIN = "https://geocoding-api.open-meteo.com"
_MAX_BODY = 512_000
_UTC_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


class WeatherProviderError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        raise WeatherProviderError("weather provider redirect is not allowed")


def _number(value: Any) -> float | None:
    if type(value) not in (int, float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _request_json(origin: str, path: str, params: Mapping[str, str]) -> dict[str, Any]:
    url = origin + path + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": "EyeOfSauron/0.24 (+personal weather alerts)",
                                                   "Accept": "application/json"})
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=12) as response:
            if response.status != 200:
                raise WeatherProviderError(f"weather provider returned HTTP {response.status}")
            if not response.headers.get("Content-Type", "").lower().startswith("application/json"):
                raise WeatherProviderError("weather provider returned non-JSON content")
            body = response.read(_MAX_BODY + 1)
            if len(body) > _MAX_BODY:
                raise WeatherProviderError("weather provider response is too large")
        value = json.loads(body)
    except (OSError, ValueError) as exc:
        raise WeatherProviderError(f"weather provider request failed: {type(exc).__name__}") from exc
    if not isinstance(value, dict) or value.get("error"):
        raise WeatherProviderError("weather provider returned an invalid response")
    return value


def _series(value: Any, key: str, count: int) -> list[Any]:
    series = value.get(key) if isinstance(value, dict) else None
    if not isinstance(series, list) or len(series) != count:
        raise WeatherProviderError(f"weather provider is missing hourly {key}")
    return series


class OpenMeteoProvider:
    def fetch(self, subscription: WeatherSubscription) -> WeatherForecast:
        coordinates = {"latitude": f"{subscription.latitude:.6f}",
                       "longitude": f"{subscription.longitude:.6f}",
                       "timezone": "UTC", "forecast_days": "3"}
        payload = _request_json(_FORECAST_ORIGIN, "/v1/forecast", {
            **coordinates,
            "current": "temperature_2m,weather_code",
            "hourly": "temperature_2m,precipitation_probability,precipitation,wind_gusts_10m",
        })
        hourly = payload.get("hourly")
        times = hourly.get("time") if isinstance(hourly, dict) else None
        if not isinstance(times, list) or not 1 <= len(times) <= 96:
            raise WeatherProviderError("weather provider hourly times are invalid")
        count = len(times)
        temperature = _series(hourly, "temperature_2m", count)
        precipitation = _series(hourly, "precipitation", count)
        probability = _series(hourly, "precipitation_probability", count)
        gusts = _series(hourly, "wind_gusts_10m", count)
        hours: list[ForecastHour] = []
        for index, raw_time in enumerate(times):
            if not isinstance(raw_time, str) or not _UTC_TIME.fullmatch(raw_time):
                raise WeatherProviderError("weather provider hourly time is invalid")
            try:
                at = int(datetime.fromisoformat(raw_time).replace(tzinfo=UTC).timestamp())
            except ValueError as exc:
                raise WeatherProviderError("weather provider hourly time is invalid") from exc
            hours.append(ForecastHour(
                at=at, temperature=_number(temperature[index]),
                precipitation=_number(precipitation[index]),
                rain_probability=_number(probability[index]),
                wind_gust=_number(gusts[index]),
            ))
        if any(right.at <= left.at for left, right in zip(hours, hours[1:])):
            raise WeatherProviderError("weather provider hourly times are unordered")
        current = payload.get("current")
        if not isinstance(current, dict):
            raise WeatherProviderError("weather provider current conditions are missing")
        code = current.get("weather_code")
        raw_current_time = current.get("time")
        if not isinstance(raw_current_time, str) or not _UTC_TIME.fullmatch(raw_current_time):
            raise WeatherProviderError("weather provider current time is missing")
        try:
            observed_at = int(datetime.fromisoformat(raw_current_time).replace(tzinfo=UTC).timestamp())
        except ValueError as exc:
            raise WeatherProviderError("weather provider current time is invalid") from exc
        return WeatherForecast(
            observed_at=observed_at,
            temperature=_number(current.get("temperature_2m")),
            weather_code=code if type(code) is int else None,
            hours=tuple(hours),
        )

    def search_places(self, query: str) -> list[dict[str, Any]]:
        if not 2 <= len(query.strip()) <= 80:
            raise WeatherProviderError("place search must contain 2 to 80 characters")
        payload = _request_json(_GEOCODE_ORIGIN, "/v1/search", {
            "name": query.strip(), "count": "8", "language": "zh", "format": "json",
        })
        results = payload.get("results", [])
        if not isinstance(results, list):
            raise WeatherProviderError("place search returned invalid results")
        places = []
        for item in results[:8]:
            if not isinstance(item, dict):
                continue
            lat, lon = _number(item.get("latitude")), _number(item.get("longitude"))
            if (lat is None or lon is None or not -90 <= lat <= 90 or not -180 <= lon <= 180
                    or not isinstance(item.get("name"), str)):
                continue
            places.append({"label": " · ".join(str(item[key]) for key in ("name", "admin1", "country")
                                                  if item.get(key)),
                           "latitude": lat, "longitude": lon,
                           "timezone": str(item.get("timezone") or "UTC")})
        return places
