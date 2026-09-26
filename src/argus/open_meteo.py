"""Bounded Open-Meteo adapter for local weather and optional dust forecasts."""

from __future__ import annotations

import json
import math
import re
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .weather import ForecastHour, WeatherAirQuality, WeatherForecast, WeatherSubscription


_FORECAST_ORIGIN = "https://api.open-meteo.com"
_GEOCODE_ORIGIN = "https://geocoding-api.open-meteo.com"
_MAX_BODY = 512_000
_LOCAL_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


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
    def resolve_timezone(self, latitude: float, longitude: float) -> str:
        if (not math.isfinite(latitude) or not math.isfinite(longitude)
                or not -90 <= latitude <= 90 or not -180 <= longitude <= 180):
            raise ValueError("latitude or longitude is invalid")
        payload = _request_json(_FORECAST_ORIGIN, "/v1/forecast", {
            "latitude": f"{latitude:.6f}",
            "longitude": f"{longitude:.6f}",
            "timezone": "auto",
            "current": "temperature_2m",
            "forecast_days": "1",
        })
        timezone = payload.get("timezone")
        if not isinstance(timezone, str) or not 1 <= len(timezone) <= 80:
            raise WeatherProviderError("weather provider timezone is missing")
        try:
            ZoneInfo(timezone)
        except (ValueError, KeyError) as exc:
            raise WeatherProviderError("weather provider timezone is invalid") from exc
        return timezone

    def fetch(self, subscription: WeatherSubscription) -> WeatherForecast:
        coordinates = {"latitude": f"{subscription.latitude:.6f}",
                       "longitude": f"{subscription.longitude:.6f}",
                       "timezone": subscription.timezone, "forecast_days": "3"}
        payload = _request_json(_FORECAST_ORIGIN, "/v1/forecast", {
            **coordinates,
            "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m,wind_direction_10m,wind_gusts_10m",
            "hourly": "temperature_2m,precipitation_probability,precipitation,wind_gusts_10m,weather_code",
            "daily": "sunrise,sunset,uv_index_max",
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
        raw_weather_codes = hourly.get("weather_code") if isinstance(hourly, dict) else None
        # Older Open-Meteo-compatible fixtures/providers may omit the hourly
        # weather code.  The chart can still use precipitation amounts; keep
        # the optional enrichment nullable rather than rejecting the whole
        # forecast.
        weather_codes = (raw_weather_codes if isinstance(raw_weather_codes, list)
                         and len(raw_weather_codes) == count else [None] * count)
        hours: list[ForecastHour] = []
        for index, raw_time in enumerate(times):
            if not isinstance(raw_time, str) or not _LOCAL_TIME.fullmatch(raw_time):
                raise WeatherProviderError("weather provider hourly time is invalid")
            try:
                at = int(datetime.fromisoformat(raw_time).replace(tzinfo=ZoneInfo(subscription.timezone)).timestamp())
            except ValueError as exc:
                raise WeatherProviderError("weather provider hourly time is invalid") from exc
            hours.append(ForecastHour(
                at=at, temperature=_number(temperature[index]),
                precipitation=_number(precipitation[index]),
                rain_probability=_number(probability[index]),
                wind_gust=_number(gusts[index]),
                weather_code=(weather_codes[index] if type(weather_codes[index]) is int else None),
            ))
        if any(right.at <= left.at for left, right in zip(hours, hours[1:])):
            raise WeatherProviderError("weather provider hourly times are unordered")
        current = payload.get("current")
        if not isinstance(current, dict):
            raise WeatherProviderError("weather provider current conditions are missing")
        code = current.get("weather_code")
        raw_current_time = current.get("time")
        if not isinstance(raw_current_time, str) or not _LOCAL_TIME.fullmatch(raw_current_time):
            raise WeatherProviderError("weather provider current time is missing")
        try:
            observed_at = int(datetime.fromisoformat(raw_current_time).replace(tzinfo=ZoneInfo(subscription.timezone)).timestamp())
        except ValueError as exc:
            raise WeatherProviderError("weather provider current time is invalid") from exc
        daily = payload.get("daily")
        sunrise = sunset = None
        uv_index_max = None
        if isinstance(daily, dict):
            dates = daily.get("time")
            sunrises, sunsets = daily.get("sunrise"), daily.get("sunset")
            if isinstance(dates, list) and isinstance(sunrises, list) and isinstance(sunsets, list):
                local_date = datetime.fromtimestamp(observed_at, ZoneInfo(subscription.timezone)).date().isoformat()
                for index, value in enumerate(dates):
                    if value == local_date and index < len(sunrises) and index < len(sunsets):
                        try:
                            sunrise = int(datetime.fromisoformat(str(sunrises[index])).replace(
                                tzinfo=ZoneInfo(subscription.timezone)).timestamp())
                            sunset = int(datetime.fromisoformat(str(sunsets[index])).replace(
                                tzinfo=ZoneInfo(subscription.timezone)).timestamp())
                        except (TypeError, ValueError):
                            sunrise = sunset = None
                        break
            uv_dates = daily.get("time")
            uv_values = daily.get("uv_index_max")
            if isinstance(uv_dates, list) and isinstance(uv_values, list):
                local_date = datetime.fromtimestamp(observed_at, ZoneInfo(subscription.timezone)).date().isoformat()
                for index, value in enumerate(uv_dates):
                    if value == local_date and index < len(uv_values):
                        uv_index_max = _number(uv_values[index])
                        break
        return WeatherForecast(
            observed_at=observed_at,
            temperature=_number(current.get("temperature_2m")),
            weather_code=code if type(code) is int else None,
            hours=tuple(hours),
            humidity=_number(current.get("relative_humidity_2m")),
            wind_speed=_number(current.get("wind_speed_10m")),
            wind_direction=_number(current.get("wind_direction_10m")),
            sunrise=sunrise,
            sunset=sunset,
            uv_index_max=uv_index_max,
        )

    def fetch_air_quality(self, subscription: WeatherSubscription) -> WeatherAirQuality:
        payload = _request_json("https://air-quality-api.open-meteo.com", "/v1/air-quality", {
            "latitude": f"{subscription.latitude:.6f}",
            "longitude": f"{subscription.longitude:.6f}",
            "timezone": subscription.timezone,
            "forecast_days": "1",
            "current": "pm10,pm2_5,european_aqi,us_aqi",
        })
        current = payload.get("current")
        if not isinstance(current, dict) or not isinstance(current.get("time"), str):
            raise WeatherProviderError("air quality current conditions are missing")
        try:
            observed_at = int(datetime.fromisoformat(str(current["time"])).replace(
                tzinfo=ZoneInfo(subscription.timezone)).timestamp())
        except ValueError as exc:
            raise WeatherProviderError("air quality current time is invalid") from exc
        values = [_number(current.get(key)) for key in ("pm2_5", "pm10", "european_aqi", "us_aqi")]
        if not any(value is not None for value in values):
            raise WeatherProviderError("air quality values are missing")
        return WeatherAirQuality(observed_at, values[0], values[1], values[2], values[3])

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
