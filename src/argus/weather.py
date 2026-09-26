"""Local forecast policy. Forecast signals are not official weather warnings."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .calendar_zh import calendar_context


DEFAULT_LOCATION = ("北京邮电大学沙河校区", 40.1561163, 116.2835626)
EXAMPLE_LOCATION = ("北京市天安门", 39.905, 116.397)
FORECAST_CREDIT = "Open-Meteo (CC BY 4.0): https://open-meteo.com/"
_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class WeatherError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WeatherSubscription:
    id: str
    label: str
    latitude: float
    longitude: float
    timezone: str
    daily_time: str
    daily_enabled: bool
    alerts_enabled: bool
    revision: int


@dataclass(frozen=True, slots=True)
class ForecastHour:
    at: int
    temperature: float | None
    precipitation: float | None
    rain_probability: float | None
    wind_gust: float | None


@dataclass(frozen=True, slots=True)
class WeatherForecast:
    observed_at: int
    temperature: float | None
    weather_code: int | None
    hours: tuple[ForecastHour, ...]
    humidity: float | None = None
    wind_speed: float | None = None
    wind_direction: float | None = None
    sunrise: int | None = None
    sunset: int | None = None


@dataclass(frozen=True, slots=True)
class WeatherAirQuality:
    observed_at: int
    pm2_5: float | None
    pm10: float | None
    european_aqi: float | None
    us_aqi: float | None


@dataclass(frozen=True, slots=True)
class WeatherAstronomy:
    date: str
    sunrise: int | None
    sunset: int | None
    moonrise: int | None
    moonset: int | None
    moon_phase: str | None
    moon_illumination: float | None
    solar_elevation: float | None = None
    solar_azimuth: float | None = None
    solar_noon_elevation: float | None = None
    solar_noon_at: int | None = None


@dataclass(frozen=True, slots=True)
class NowcastSlot:
    at: int
    precipitation: float
    phase: str


@dataclass(frozen=True, slots=True)
class WeatherNowcast:
    issued_at: int
    slots: tuple[NowcastSlot, ...]


@dataclass(frozen=True, slots=True)
class OfficialWeatherAlert:
    alert_id: str
    issued_at: int
    expires_at: int
    message_type: str
    supersedes: tuple[str, ...]
    severity: str
    color: str
    kind: str
    headline: str
    description: str
    sender: str
    instruction: str = ""


@dataclass(frozen=True, slots=True)
class WeatherSignal:
    kind: str
    title: str
    message: str
    priority: int


class WeatherRepository(Protocol):
    def get_weather_subscription(self) -> WeatherSubscription: ...
    def get_weather_status(self) -> dict[str, Any]: ...
    def update_weather_subscription(self, data: Mapping[str, Any], *, actor: str, now: int) -> WeatherSubscription: ...
    def record_weather_forecast(
        self, subscription: WeatherSubscription, forecast: WeatherForecast,
        *, now: int, topic: str, click_url: str,
    ) -> int: ...
    def record_weather_failure(
        self, subscription: WeatherSubscription, error: BaseException, now: int,
        *, topic: str, click_url: str,
    ) -> None: ...
    def record_weather_nowcast(
        self, subscription: WeatherSubscription, nowcast: WeatherNowcast,
        *, now: int, topic: str, click_url: str,
    ) -> int: ...
    def record_official_weather_alerts(
        self, subscription: WeatherSubscription, alerts: tuple[OfficialWeatherAlert, ...],
        *, now: int, topic: str, click_url: str,
    ) -> int: ...
    def record_qweather_failure(self, subscription: WeatherSubscription, kind: str,
                                error: BaseException, now: int, *, topic: str, click_url: str) -> None: ...
    def record_weather_air_quality(self, subscription: WeatherSubscription,
                                   air_quality: WeatherAirQuality, *, now: int) -> None: ...
    def record_weather_astronomy(self, subscription: WeatherSubscription,
                                 astronomy: WeatherAstronomy, *, now: int) -> None: ...


class WeatherProvider(Protocol):
    def fetch(self, subscription: WeatherSubscription) -> WeatherForecast: ...
    def fetch_air_quality(self, subscription: WeatherSubscription) -> WeatherAirQuality: ...


class LocalWeatherProvider(Protocol):
    def fetch_minutely(self, subscription: WeatherSubscription) -> WeatherNowcast: ...
    def fetch_alerts(self, subscription: WeatherSubscription) -> tuple[OfficialWeatherAlert, ...]: ...
    def fetch_astronomy(self, subscription: WeatherSubscription, date: str) -> WeatherAstronomy: ...


def nowcast_signal(nowcast: WeatherNowcast, now: int) -> tuple[str, int] | None:
    wet = [slot for slot in nowcast.slots if now - 300 <= slot.at <= now + 7200
           and slot.precipitation >= 0.05]
    if not wet or sum(slot.precipitation for slot in wet) < 0.3:
        return None
    start = wet[0].at
    if start > now + 2700:
        return None
    phase = "snow" if sum(slot.precipitation for slot in wet if slot.phase == "snow") >= 0.3 else "rain"
    return phase, start


def validate_nowcast(nowcast: WeatherNowcast, now: int) -> None:
    if not now - 1800 <= nowcast.issued_at <= now + 900 or not 12 <= len(nowcast.slots) <= 48:
        raise WeatherError("near-term forecast is stale or incomplete")
    if nowcast.slots[0].at > now + 600 or nowcast.slots[-1].at < now + 3600:
        raise WeatherError("near-term forecast has insufficient coverage")
    if any(right.at - left.at != 300 for left, right in zip(nowcast.slots, nowcast.slots[1:])):
        raise WeatherError("near-term forecast time slots are discontinuous")
    if any(not math.isfinite(slot.precipitation) or not 0 <= slot.precipitation <= 100
           or slot.phase not in {"rain", "snow"} for slot in nowcast.slots):
        raise WeatherError("near-term forecast contains invalid precipitation")


def validate_forecast(forecast: WeatherForecast, now: int) -> None:
    if not now - 7200 <= forecast.observed_at <= now + 900:
        raise WeatherError("weather provider current conditions are stale")
    if (forecast.temperature is None or not math.isfinite(forecast.temperature)
            or not -100 <= forecast.temperature <= 70
            or forecast.weather_code is None or not 0 <= forecast.weather_code <= 99):
        raise WeatherError("weather provider current conditions are invalid")
    for value, low, high, label in (
        (forecast.humidity, 0, 100, "humidity"),
        (forecast.wind_speed, 0, 400, "wind speed"),
        (forecast.wind_direction, 0, 360, "wind direction"),
    ):
        if value is not None and (not math.isfinite(value) or not low <= value <= high):
            raise WeatherError(f"weather provider current {label} is invalid")
    future = [hour for hour in forecast.hours if hour.at >= now]
    if not future or future[0].at > now + 3600 or future[-1].at < now + 24 * 3600:
        raise WeatherError("weather provider forecast does not cover the next day")
    previous = future[0].at - 3600
    for hour in future:
        if hour.at - previous != 3600:
            raise WeatherError("weather provider hourly forecast has a gap")
        previous = hour.at
        values = (
            (hour.temperature, -100, 70),
            (hour.precipitation, 0, 500),
            (hour.rain_probability, 0, 100),
            (hour.wind_gust, 0, 400),
        )
        if any(value is None or not math.isfinite(value) or not low <= value <= high
               for value, low, high in values):
            raise WeatherError("weather provider hourly forecast is incomplete or invalid")


def parse_weather_subscription(data: Mapping[str, Any], previous: WeatherSubscription) -> WeatherSubscription:
    allowed = {"label", "latitude", "longitude", "timezone", "daily_time",
               "daily_enabled", "alerts_enabled", "revision"}
    if set(data) != allowed:
        raise WeatherError("weather settings must contain exactly label, coordinates, timezone, schedule, toggles and revision")
    label = data["label"]
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 100:
        raise WeatherError("location label must contain 1 to 100 characters")
    latitude, longitude = data["latitude"], data["longitude"]
    if (type(latitude) not in (float, int) or type(longitude) not in (float, int)
            or not math.isfinite(latitude) or not math.isfinite(longitude)
            or not -90 <= latitude <= 90 or not -180 <= longitude <= 180):
        raise WeatherError("latitude or longitude is invalid")
    timezone = data["timezone"]
    if not isinstance(timezone, str) or not 1 <= len(timezone) <= 80:
        raise WeatherError("timezone must be an IANA name")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise WeatherError("timezone must be an IANA name") from exc
    daily_time = data["daily_time"]
    if not isinstance(daily_time, str) or not _TIME_RE.fullmatch(daily_time):
        raise WeatherError("daily_time must use HH:MM")
    if type(data["daily_enabled"]) is not bool or type(data["alerts_enabled"]) is not bool:
        raise WeatherError("weather toggles must be boolean")
    if type(data["revision"]) is not int or data["revision"] != previous.revision:
        raise WeatherError("weather settings changed; reload before saving")
    return WeatherSubscription(
        id=previous.id, label=label.strip(), latitude=float(latitude), longitude=float(longitude),
        timezone=timezone, daily_time=daily_time, daily_enabled=data["daily_enabled"],
        alerts_enabled=data["alerts_enabled"], revision=previous.revision + 1,
    )


def weather_description(code: int | None) -> str:
    if code is None:
        return "天气待确认"
    if code == 0:
        return "晴"
    if code in {1, 2, 3}:
        return "多云"
    if code in {45, 48}:
        return "有雾"
    if code in {51, 53, 55, 56, 57}:
        return "小雨或毛毛雨"
    if code in {61, 63, 65, 66, 67, 80, 81, 82}:
        return "有雨"
    if code in {71, 73, 75, 77, 85, 86}:
        return "有雪"
    if code in {95, 96, 99}:
        return "雷雨"
    return "天气变化"


def summarize_weather(subscription: WeatherSubscription, forecast: WeatherForecast, now: int) -> tuple[str, dict[str, Any]]:
    local_now = datetime.fromtimestamp(now, ZoneInfo(subscription.timezone))
    today = [hour for hour in forecast.hours
             if datetime.fromtimestamp(hour.at, ZoneInfo(subscription.timezone)).date() == local_now.date()]
    remaining = [hour for hour in today if hour.at >= now]
    temperatures = [hour.temperature for hour in today if hour.temperature is not None]
    rain = sum(max(0, hour.precipitation or 0) for hour in remaining)
    probability = max((hour.rain_probability or 0 for hour in remaining), default=0)
    gust = max((hour.wind_gust or 0 for hour in remaining), default=0)
    low = min(temperatures) if temperatures else None
    high = max(temperatures) if temperatures else None
    temp_range = f"{low:.0f}~{high:.0f}℃" if low is not None and high is not None else "温度暂无数据"
    calendar = calendar_context(local_now.date())
    humidity = f"相对湿度 {forecast.humidity:.0f}%" if forecast.humidity is not None else "相对湿度暂无数据"
    wind = (f"风速 {forecast.wind_speed:.0f} km/h、风向 {wind_direction_name(forecast.wind_direction)}"
            if forecast.wind_speed is not None and forecast.wind_direction is not None else "风况暂无数据")
    sun = (f"日出 {format_clock(forecast.sunrise, subscription.timezone)}、日落 "
           f"{format_clock(forecast.sunset, subscription.timezone)}"
           if forecast.sunrise and forecast.sunset else "日出日落暂无数据")
    message = (f"{subscription.label}：{weather_description(forecast.weather_code)}，当前 {forecast.temperature:.0f}℃，{temp_range}。"
               f"今日剩余时段预计降水 {rain:.1f} mm，最高降雨概率 {probability:.0f}%，"
               f"阵风最高 {gust:.0f} km/h；{humidity}；{wind}。\n"
               f"{sun}。\n"
               f"阳历 {local_now.date().isoformat()}，{calendar['lunar']}。{calendar['festivals']}"
               "\n数据：Open-Meteo 预报，非官方气象预警。")
    return message, {"condition": weather_description(forecast.weather_code),
                     "low": low, "high": high, "rain_mm": round(rain, 1),
                     "rain_probability": round(probability), "wind_gust_kmh": round(gust),
                     "temperature_now": forecast.temperature,
                     "humidity": forecast.humidity,
                     "wind_speed_kmh": forecast.wind_speed,
                     "wind_direction": forecast.wind_direction,
                     "wind_direction_name": wind_direction_name(forecast.wind_direction),
                     "sunrise": forecast.sunrise,
                     "sunset": forecast.sunset,
                     "calendar": calendar,
                     "observed_at": forecast.observed_at}


def format_clock(timestamp: int | None, timezone: str) -> str:
    if timestamp is None:
        return "—"
    return datetime.fromtimestamp(timestamp, ZoneInfo(timezone)).strftime("%H:%M")


def wind_direction_name(direction: float | None) -> str:
    if direction is None or not math.isfinite(direction):
        return "—"
    labels = ("北", "东北", "东", "东南", "南", "西南", "西", "西北")
    return labels[int((direction % 360 + 22.5) // 45) % 8]


def air_quality_text(value: WeatherAirQuality | None) -> str:
    if value is None:
        return "空气质量暂无数据"
    parts = ["空气质量（模型估计，非站点实测）"]
    if value.european_aqi is not None:
        parts.append(f"欧洲 AQI {value.european_aqi:.0f}")
    if value.us_aqi is not None:
        parts.append(f"美国 AQI {value.us_aqi:.0f}")
    if value.pm2_5 is not None:
        parts.append(f"PM2.5 {value.pm2_5:.1f} μg/m³")
    if value.pm10 is not None:
        parts.append(f"PM10 {value.pm10:.1f} μg/m³")
    return "，".join(parts) + "（Open-Meteo Air Quality）"


def astronomy_text(value: Mapping[str, Any] | None, timezone: str) -> str:
    if not isinstance(value, Mapping):
        return "月升月落暂无数据"
    phase = value.get("moon_phase") or "月相暂无数据"
    illumination = value.get("moon_illumination")
    light = f"，照明 {float(illumination):.0f}%" if isinstance(illumination, (int, float)) else ""
    angle = value.get("solar_noon_elevation")
    noon_at = value.get("solar_noon_at")
    solar = (f"，近似太阳正午高度角 {float(angle):.1f}°（{format_clock(noon_at, timezone)}，海平面基准）"
             if isinstance(angle, (int, float)) and isinstance(noon_at, int) else "")
    return (f"月升 {format_clock(value.get('moonrise'), timezone)}、月落 "
            f"{format_clock(value.get('moonset'), timezone)}，月相 {phase}{light}{solar}")


def weather_signals(forecast: WeatherForecast, now: int) -> tuple[WeatherSignal, ...]:
    near = [hour for hour in forecast.hours if now <= hour.at <= now + 6 * 3600]
    if not near:
        return ()
    signals: list[WeatherSignal] = []
    rain_mm = sum(max(0, hour.precipitation or 0) for hour in near)
    rain_probability = max((hour.rain_probability or 0 for hour in near), default=0)
    if rain_mm >= 5 and rain_probability >= 75:
        signals.append(WeatherSignal("rain", "本地明显降雨预报",
            f"未来 6 小时预计降雨约 {rain_mm:.1f} mm，最高概率 {rain_probability:.0f}%。预报可能变化，请留意当地官方预警。",
            5 if rain_mm >= 30 else 4))
    gust = max((hour.wind_gust or 0 for hour in near), default=0)
    if gust >= 55:
        signals.append(WeatherSignal("wind", "本地大风预报",
            f"未来 6 小时预报阵风最高约 {gust:.0f} km/h。请留意户外活动和当地官方预警。",
            5 if gust >= 85 else 4))
    if forecast.temperature is not None:
        tomorrow = [hour.temperature for hour in forecast.hours
                    if now + 23 * 3600 <= hour.at <= now + 25 * 3600 and hour.temperature is not None]
        if tomorrow and forecast.temperature - min(tomorrow) >= 8:
            drop = forecast.temperature - min(tomorrow)
            signals.append(WeatherSignal("cold", "本地明显降温预报",
                f"与当前时段相比，约 24 小时后预计降温 {drop:.0f}℃。请留意后续预报变化。", 4))
    return tuple(signals)


def remaining_day_rain(subscription: WeatherSubscription, forecast: WeatherForecast, now: int) -> tuple[float, float]:
    today = datetime.fromtimestamp(now, ZoneInfo(subscription.timezone)).date()
    remaining = [hour for hour in forecast.hours if hour.at >= now and
                 datetime.fromtimestamp(hour.at, ZoneInfo(subscription.timezone)).date() == today]
    return (sum(max(0, hour.precipitation or 0) for hour in remaining),
            max((hour.rain_probability or 0 for hour in remaining), default=0))


def daily_due(subscription: WeatherSubscription, last_date: str | None, now: int) -> str | None:
    if not subscription.daily_enabled:
        return None
    local = datetime.fromtimestamp(now, ZoneInfo(subscription.timezone))
    hour, minute = (int(part) for part in subscription.daily_time.split(":"))
    scheduled = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if local < scheduled:
        return None
    date_key = scheduled.date().isoformat()
    return date_key if last_date != date_key and 0 <= (local - scheduled).total_seconds() <= 6 * 3600 else None
