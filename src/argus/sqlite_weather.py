"""SQLite implementation of the local weather persistence port."""

from __future__ import annotations

import json
import math
import time
from dataclasses import asdict
from datetime import datetime
from typing import TYPE_CHECKING, Any, Mapping
from zoneinfo import ZoneInfo

from .models import AlertCandidate
from .util import sanitize_error
from .weather import (
    FORECAST_CREDIT, OfficialWeatherAlert, WeatherAirQuality, WeatherAstronomy,
    WeatherError, WeatherForecast, WeatherNowcast,
    WeatherSubscription, daily_due, nowcast_signal, parse_weather_subscription,
    remaining_day_rain, summarize_weather, validate_forecast, validate_nowcast, weather_signals,
    air_quality_text, astronomy_text, format_clock,
)

if TYPE_CHECKING:
    from .database import Database


class SQLiteWeather:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.connection = database.connection

    def get_weather_subscription(self) -> WeatherSubscription:
        row = self.connection.execute("SELECT * FROM weather_subscriptions WHERE id='home'").fetchone()
        if row is None:
            raise WeatherError("weather subscription is missing")
        return WeatherSubscription(
            id=str(row["id"]), label=str(row["label"]), latitude=float(row["latitude"]),
            longitude=float(row["longitude"]), timezone=str(row["timezone"]),
            daily_time=str(row["daily_time"]), daily_enabled=bool(row["daily_enabled"]),
            alerts_enabled=bool(row["alerts_enabled"]), revision=int(row["revision"]),
        )

    def get_weather_status(self) -> dict[str, Any]:
        subscription = self.get_weather_subscription()
        row = self.connection.execute(
            "SELECT * FROM weather_state WHERE subscription_id=?", (subscription.id,)
        ).fetchone()
        assert row is not None
        providers = self.connection.execute(
            "SELECT kind,last_success_at,last_error,consecutive_failures FROM weather_provider_state "
            "WHERE subscription_id=?", (subscription.id,),
        ).fetchall()
        air_row = self.connection.execute(
            "SELECT observed_at,pm2_5,pm10,european_aqi,us_aqi FROM weather_air_quality "
            "WHERE subscription_id=?", (subscription.id,),
        ).fetchone()
        sky_row = self.connection.execute(
            "SELECT local_date,sunrise,sunset,moonrise,moonset,moon_phase,moon_illumination,"
            "solar_elevation,solar_azimuth,solar_noon_elevation,solar_noon_at,updated_at "
            "FROM weather_astronomy WHERE subscription_id=?",
            (subscription.id,),
        ).fetchone()
        latest = json.loads(row["latest_json"]) if row["latest_json"] else None
        if isinstance(latest, dict):
            observed_at = latest.get("observed_at")
            forecast_date = (datetime.fromtimestamp(observed_at, ZoneInfo(subscription.timezone))
                             .strftime("%Y%m%d") if isinstance(observed_at, int) else None)
            latest["is_today"] = forecast_date == datetime.fromtimestamp(
                time.time(), ZoneInfo(subscription.timezone)).strftime("%Y%m%d")
            latest["air_quality"] = (dict(air_row) if air_row
                                     and 0 <= int(time.time()) - int(air_row["observed_at"]) <= 6 * 3600 else None)
            latest["astronomy"] = (dict(sky_row) if sky_row and sky_row["local_date"] == forecast_date
                                    else None)
            if isinstance(latest["astronomy"], dict):
                latest["astronomy"]["date"] = latest["astronomy"].pop("local_date")
        return {
            "subscription": asdict(subscription),
            "latest": latest,
            "last_success_at": row["last_success_at"],
            "last_daily_date": row["last_daily_date"],
            "last_error": row["last_error"],
            "consecutive_failures": int(row["consecutive_failures"]),
            "rain_expected": bool(row["rain_expected"]) if row["rain_expected"] is not None else None,
            "qweather": {str(item["kind"]): {
                "last_success_at": item["last_success_at"], "last_error": item["last_error"],
                "consecutive_failures": int(item["consecutive_failures"]),
            } for item in providers},
        }

    def update_weather_subscription(
        self, data: Mapping[str, Any], *, actor: str, now: int,
    ) -> WeatherSubscription:
        if not actor or len(actor) > 128 or now < 0:
            raise WeatherError("weather audit metadata is invalid")
        with self.database.unit_of_work():
            previous = self.get_weather_subscription()
            updated = parse_weather_subscription(data, previous)
            changed_location = (previous.latitude, previous.longitude, previous.timezone) != (
                updated.latitude, updated.longitude, updated.timezone)
            changed = self.connection.execute(
                "UPDATE weather_subscriptions SET label=?,latitude=?,longitude=?,timezone=?,daily_time=?,"
                "daily_enabled=?,alerts_enabled=?,revision=?,updated_at=?,updated_by=? "
                "WHERE id=? AND revision=?",
                (updated.label, updated.latitude, updated.longitude, updated.timezone,
                 updated.daily_time, int(updated.daily_enabled), int(updated.alerts_enabled),
                 updated.revision, now, actor, updated.id, previous.revision),
            )
            if changed.rowcount != 1:
                raise WeatherError("weather settings changed; reload before saving")
            if changed_location:
                self.connection.execute(
                    "UPDATE weather_state SET last_daily_date=NULL,hazard_state_json='{}',latest_json=NULL,"
                    "last_success_at=NULL,last_error=NULL,consecutive_failures=0,outage_alerted=0,"
                    "rain_state_date=NULL,rain_expected=NULL "
                    "WHERE subscription_id=?", (updated.id,),
                )
                self.connection.execute(
                    "UPDATE weather_provider_state SET state_json='{}',last_success_at=NULL,last_error=NULL,"
                    "consecutive_failures=0,outage_alerted=0 WHERE subscription_id=?", (updated.id,),
                )
                self.connection.execute(
                    "DELETE FROM weather_warning_messages WHERE subscription_id=?", (updated.id,),
                )
                self.connection.execute("DELETE FROM weather_air_quality WHERE subscription_id=?", (updated.id,))
                self.connection.execute("DELETE FROM weather_astronomy WHERE subscription_id=?", (updated.id,))
            self.connection.execute(
                "INSERT INTO weather_subscription_audit(revision,actor,settings_json,created_at) "
                "VALUES(?,?,?,?)",
                (updated.revision, actor, json.dumps(asdict(updated), ensure_ascii=False), now),
            )
        return updated

    def record_weather_forecast(
        self, subscription: WeatherSubscription, forecast: WeatherForecast,
        *, now: int, topic: str, click_url: str,
    ) -> int:
        if now < 0 or not topic or len(topic) > 200:
            raise WeatherError("weather delivery metadata is invalid")
        validate_forecast(forecast, now)
        daily_message, summary = summarize_weather(subscription, forecast, now)
        signals = weather_signals(forecast, now)
        queued = 0
        with self.database.unit_of_work():
            current = self.get_weather_subscription()
            if current.revision != subscription.revision:
                return 0
            state = self.connection.execute(
                "SELECT * FROM weather_state WHERE subscription_id=?", (subscription.id,)
            ).fetchone()
            assert state is not None
            day = daily_due(subscription, state["last_daily_date"], now)
            if day is not None:
                sky_row = self.connection.execute(
                    "SELECT moonrise,moonset,moon_phase,moon_illumination,solar_noon_elevation,solar_noon_at "
                    "FROM weather_astronomy WHERE subscription_id=? AND local_date=?",
                    (subscription.id, day.replace("-", "")),
                ).fetchone()
                daily_message += "\n" + astronomy_text(dict(sky_row) if sky_row else None, subscription.timezone)
                air_row = self.connection.execute(
                    "SELECT observed_at,pm2_5,pm10,european_aqi,us_aqi FROM weather_air_quality "
                    "WHERE subscription_id=?", (subscription.id,),
                ).fetchone()
                if air_row and 0 <= now - int(air_row["observed_at"]) <= 6 * 3600:
                    daily_message += "\n" + air_quality_text(WeatherAirQuality(**dict(air_row)))
                else:
                    daily_message += "\n空气质量暂无数据"
                queued += int(self.database._insert_alert(AlertCandidate(
                    rule_id="weather.daily", dedupe_key=f"weather:{subscription.id}:daily:{day}",
                    title=f"今日天气 · {subscription.label}", message=daily_message + "\n" + FORECAST_CREDIT,
                    priority=3, tags=("sunny",), click_url=click_url, topic=topic,
                    confidence=0.7, evidence=("Open-Meteo forecast; not an official warning",),
                ), None, now))
            prior: dict[str, Any] = json.loads(state["hazard_state_json"])
            active = {signal.kind: signal for signal in signals}
            local_date = datetime.fromtimestamp(now, ZoneInfo(subscription.timezone)).date().isoformat()
            remaining_rain, rain_probability = remaining_day_rain(subscription, forecast, now)
            previous_rain = (bool(state["rain_expected"]) if state["rain_state_date"] == local_date
                             and state["rain_expected"] is not None else None)
            rain_expected = remaining_rain >= 1 and rain_probability >= 60
            if previous_rain is True and remaining_rain >= 0.3 and rain_probability >= 35:
                rain_expected = True
            if (subscription.alerts_enabled and previous_rain is False and rain_expected
                    and "rain" not in active):
                queued += int(self.database._insert_alert(AlertCandidate(
                    rule_id="weather.rain_change",
                    dedupe_key=f"weather:{subscription.id}:rain-change:{local_date}",
                    title=f"今日预报转为有雨 · {subscription.label}",
                    message=(f"最新逐小时预报显示今日剩余时间可能降雨约 {remaining_rain:.1f} mm，"
                             f"最高概率 {rain_probability:.0f}%。持续有雨不会重复提醒。\n"
                             "数据：Open-Meteo 模型预报，非官方气象预警。\n" + FORECAST_CREDIT),
                    priority=3, tags=("umbrella",), click_url=click_url, topic=topic,
                    confidence=0.65,
                ), None, now))
            next_state: dict[str, Any] = {
                kind: {**value, "active": False}
                for kind, value in prior.items() if isinstance(value, dict)
            }
            for kind, signal in active.items():
                old = prior.get(kind, {})
                last_alert = int(old.get("last_alert_at", 0))
                old_priority = int(old.get("priority", 0))
                should_alert = subscription.alerts_enabled and (
                    (not old.get("active") and now - last_alert >= 6 * 3600)
                    or (bool(old.get("active")) and signal.priority > old_priority)
                )
                if should_alert:
                    queued += int(self.database._insert_alert(AlertCandidate(
                        rule_id=f"weather.{kind}",
                        dedupe_key=f"weather:{subscription.id}:{kind}:{signal.priority}:{now // (6 * 3600)}",
                        title=f"{signal.title} · {subscription.label}",
                        message=signal.message + "\n数据：Open-Meteo 模型预报，非官方气象预警。\n" + FORECAST_CREDIT,
                        priority=signal.priority, tags=("warning",), click_url=click_url,
                        topic=topic, confidence=0.65,
                        evidence=("Open-Meteo forecast; not an official warning",),
                    ), None, now))
                next_state[kind] = {
                    "active": bool(subscription.alerts_enabled),
                    "priority": max(old_priority, signal.priority) if old.get("active") else signal.priority,
                    "last_alert_at": now if should_alert else last_alert,
                }
            if state["outage_alerted"]:
                queued += int(self.database._insert_alert(AlertCandidate(
                    rule_id="weather.provider_recovered",
                    dedupe_key=f"weather:{subscription.id}:provider-recovered:{now // 3600}",
                    title=f"天气数据恢复 · {subscription.label}", message=f"{subscription.label} 的天气预报获取已恢复。",
                    priority=3, tags=("white_check_mark",), click_url=click_url, topic=topic,
                    confidence=1.0,
                ), None, now))
            self.connection.execute(
                "UPDATE weather_state SET last_daily_date=COALESCE(?,last_daily_date),"
                "hazard_state_json=?,latest_json=?,last_success_at=?,last_error=NULL,"
                "consecutive_failures=0,outage_alerted=0,rain_state_date=?,rain_expected=? "
                "WHERE subscription_id=?",
                (day, json.dumps(next_state), json.dumps(summary, ensure_ascii=False),
                 now, local_date, int(rain_expected), subscription.id),
            )
        return queued

    def record_weather_air_quality(self, subscription: WeatherSubscription,
                                   air_quality: WeatherAirQuality, *, now: int) -> None:
        values = (air_quality.pm2_5, air_quality.pm10, air_quality.european_aqi, air_quality.us_aqi)
        if (now < 0 or not now - 7200 <= air_quality.observed_at <= now + 900
                or any(value is not None and (not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0)
                          for value in values)):
            raise WeatherError("air quality values are invalid")
        with self.database.unit_of_work():
            if self.get_weather_subscription().revision != subscription.revision:
                return
            self.connection.execute(
                "INSERT INTO weather_air_quality(subscription_id,observed_at,pm2_5,pm10,european_aqi,us_aqi) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(subscription_id) DO UPDATE SET "
                "observed_at=excluded.observed_at,pm2_5=excluded.pm2_5,pm10=excluded.pm10,"
                "european_aqi=excluded.european_aqi,us_aqi=excluded.us_aqi",
                (subscription.id, air_quality.observed_at, *values),
            )

    def record_weather_astronomy(self, subscription: WeatherSubscription,
                                 astronomy: WeatherAstronomy, *, now: int) -> None:
        if now < 0 or len(astronomy.date) != 8:
            raise WeatherError("astronomy data is invalid")
        with self.database.unit_of_work():
            if self.get_weather_subscription().revision != subscription.revision:
                return
            self.connection.execute(
                "INSERT INTO weather_astronomy(subscription_id,local_date,sunrise,sunset,moonrise,moonset,"
                "moon_phase,moon_illumination,solar_elevation,solar_azimuth,solar_noon_elevation,solar_noon_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(subscription_id) DO UPDATE SET "
                "local_date=excluded.local_date,sunrise=excluded.sunrise,sunset=excluded.sunset,"
                "moonrise=excluded.moonrise,moonset=excluded.moonset,moon_phase=excluded.moon_phase,"
                "moon_illumination=excluded.moon_illumination,solar_elevation=excluded.solar_elevation,"
                "solar_azimuth=excluded.solar_azimuth,solar_noon_elevation=excluded.solar_noon_elevation,"
                "solar_noon_at=excluded.solar_noon_at,updated_at=excluded.updated_at",
                (subscription.id, astronomy.date, astronomy.sunrise, astronomy.sunset,
                 astronomy.moonrise, astronomy.moonset, astronomy.moon_phase,
                 astronomy.moon_illumination, astronomy.solar_elevation,
                 astronomy.solar_azimuth, astronomy.solar_noon_elevation, astronomy.solar_noon_at, now),
            )

    def enqueue_weather_test(self, *, topic: str, click_url: str, now: int) -> bool:
        """Queue a clearly labelled snapshot without changing forecast state."""
        if not topic or now < 0:
            raise WeatherError("weather test metadata is invalid")
        with self.database.unit_of_work():
            subscription = self.get_weather_subscription()
            latest = self.get_weather_status()["latest"]
            if not isinstance(latest, dict):
                raise WeatherError("no weather snapshot is available yet")
            observed_at = latest.get("observed_at")
            if (not isinstance(observed_at, int) or not -900 <= now - observed_at <= 2 * 3600
                    or datetime.fromtimestamp(observed_at, ZoneInfo(subscription.timezone)).date()
                    != datetime.fromtimestamp(now, ZoneInfo(subscription.timezone)).date()):
                raise WeatherError("no fresh weather snapshot is available for today")
            message = "[测试通知] 今日天气快照\n"
            message += f"地点：{subscription.label}\n"
            message += (f"天气：{latest.get('condition', '暂无')}，当前 {latest.get('temperature_now', '—')}℃，"
                        f"今日 {latest.get('low', '—')}~{latest.get('high', '—')}℃\n")
            message += (f"降水 {latest.get('rain_mm', '—')} mm，最高概率 {latest.get('rain_probability', '—')}%，"
                        f"阵风 {latest.get('wind_gust_kmh', '—')} km/h\n")
            message += (f"湿度 {latest.get('humidity', '—')}%，风 {latest.get('wind_speed_kmh', '—')} km/h "
                        f"{latest.get('wind_direction_name', '—')}\n")
            aq = latest.get("air_quality")
            message += (air_quality_text(WeatherAirQuality(**aq)) if isinstance(aq, dict)
                        else "空气质量暂无数据") + "\n"
            message += (f"日出 {format_clock(latest.get('sunrise'), subscription.timezone)}，"
                        f"日落 {format_clock(latest.get('sunset'), subscription.timezone)}\n")
            uv = latest.get("uv_index_max")
            message += (f"今日最高紫外线指数 {float(uv):.1f}\n"
                        if isinstance(uv, (int, float)) else "今日最高紫外线指数暂无数据\n")
            message += astronomy_text(latest.get("astronomy"), subscription.timezone) + "\n"
            calendar = latest.get("calendar")
            if isinstance(calendar, dict):
                message += f"阳历 {datetime.fromtimestamp(now, ZoneInfo(subscription.timezone)).date().isoformat()}，{calendar.get('lunar', '')}。{calendar.get('festivals', '')}\n"
            message += "这是测试通知，不代表官方气象预警。"
            inserted = self.database._insert_alert(AlertCandidate(
                rule_id="weather.test", dedupe_key=f"weather:{subscription.id}:test:{now}",
                title=f"天气测试通知 · {subscription.label}", message=message,
                priority=3, tags=("test", "cloud"), click_url=click_url, topic=topic,
                confidence=1.0, evidence=("operator-requested weather snapshot",),
            ), None, now)
            return bool(inserted)

    def record_weather_failure(
        self, subscription: WeatherSubscription, error: BaseException, now: int,
        *, topic: str, click_url: str,
    ) -> None:
        with self.database.unit_of_work():
            current = self.get_weather_subscription()
            if current.revision != subscription.revision:
                return
            row = self.connection.execute(
                "SELECT consecutive_failures,outage_alerted,last_success_at FROM weather_state "
                "WHERE subscription_id=?", (subscription.id,),
            ).fetchone()
            assert row is not None
            failures = int(row["consecutive_failures"]) + 1
            alerted = bool(row["outage_alerted"])
            if failures >= 3 and not alerted:
                self.database._insert_alert(AlertCandidate(
                    rule_id="weather.provider_outage",
                    dedupe_key=f"weather:{subscription.id}:provider-outage:{subscription.revision}:{row['last_success_at'] or 0}",
                    title=f"本地天气数据连续获取失败 · {subscription.label}",
                    message=f"{subscription.label} 已连续 {failures} 次无法获取天气预报。天气提醒可能延迟，请检查后台。",
                    priority=4, tags=("warning",), click_url=click_url, topic=topic, confidence=1.0,
                ), None, now)
                alerted = True
            self.connection.execute(
                "UPDATE weather_state SET last_error=?,consecutive_failures=?,outage_alerted=? "
                "WHERE subscription_id=?",
                (sanitize_error(error)[:300], failures, int(alerted), subscription.id),
            )

    def _qweather_success(self, subscription: WeatherSubscription, kind: str,
                          now: int, topic: str, click_url: str) -> int:
        row = self.connection.execute(
            "SELECT outage_alerted FROM weather_provider_state WHERE subscription_id=? AND kind=?",
            (subscription.id, kind),
        ).fetchone()
        assert row is not None
        queued = 0
        if row["outage_alerted"]:
            queued = int(self.database._insert_alert(AlertCandidate(
                rule_id="weather.qweather_recovered",
                dedupe_key=f"weather:{subscription.id}:qweather:{kind}:recovered:{now // 3600}",
                title=f"和风天气数据恢复 · {subscription.label}", message=f"{subscription.label} 的{kind}数据获取已恢复。",
                priority=3, tags=("white_check_mark",), click_url=click_url,
                topic=topic, confidence=1.0,
            ), None, now))
        self.connection.execute(
            "UPDATE weather_provider_state SET last_success_at=?,last_error=NULL,"
            "consecutive_failures=0,outage_alerted=0 WHERE subscription_id=? AND kind=?",
            (now, subscription.id, kind),
        )
        return queued

    def record_weather_nowcast(
        self, subscription: WeatherSubscription, nowcast: WeatherNowcast,
        *, now: int, topic: str, click_url: str,
    ) -> int:
        validate_nowcast(nowcast, now)
        signal = nowcast_signal(nowcast, now)
        with self.database.unit_of_work():
            if self.get_weather_subscription().revision != subscription.revision:
                return 0
            row = self.connection.execute(
                "SELECT state_json FROM weather_provider_state WHERE subscription_id=? AND kind='minutely'",
                (subscription.id,),
            ).fetchone()
            assert row is not None
            state: dict[str, Any] = json.loads(row["state_json"])
            last_alert = int(state.get("last_alert_at", 0))
            last_signal = int(state.get("last_signal_at", 0))
            active = bool(state.get("active")) and now - last_signal < 1800
            queued = self._qweather_success(subscription, "minutely", now, topic, click_url)
            if signal is None:
                state["active"] = active
            else:
                phase, start = signal
                phase_changed = active and state.get("phase") != phase
                should_alert = subscription.alerts_enabled and (
                    phase_changed or (not active and now - last_alert >= 4 * 3600)
                )
                if should_alert:
                    label = "降雪" if phase == "snow" else "降雨"
                    minutes = max(0, round((start - now) / 300) * 5)
                    queued += int(self.database._insert_alert(AlertCandidate(
                        rule_id="weather.nowcast",
                        dedupe_key=f"weather:{subscription.id}:nowcast:{phase}:{now // (4 * 3600)}",
                        title=f"短时{label}预报 · {subscription.label}",
                        message=(f"和风天气临近预报显示未来两小时有{label}，最早约 {minutes} 分钟后开始。"
                                 "开始时间仍可能变化；此为预报，不代表实测。\n"
                                 "数据：QWeather https://developer.qweather.com/attribution.html"),
                        priority=4 if phase == "snow" else 3, tags=("cloud_with_rain",),
                        click_url=click_url, topic=topic, confidence=0.7,
                    ), None, now))
                    state["last_alert_at"] = now
                state.update({"active": True, "phase": phase, "last_signal_at": now})
            self.connection.execute(
                "UPDATE weather_provider_state SET state_json=? WHERE subscription_id=? AND kind='minutely'",
                (json.dumps(state), subscription.id),
            )
        return queued

    def record_official_weather_alerts(
        self, subscription: WeatherSubscription, alerts: tuple[OfficialWeatherAlert, ...],
        *, now: int, topic: str, click_url: str,
    ) -> int:
        if len(alerts) > 100:
            raise WeatherError("too many official weather alerts")
        queued = 0
        with self.database.unit_of_work():
            if self.get_weather_subscription().revision != subscription.revision:
                return 0
            row = self.connection.execute(
                "SELECT state_json FROM weather_provider_state WHERE subscription_id=? AND kind='alerts'",
                (subscription.id,),
            ).fetchone()
            assert row is not None
            state: dict[str, Any] = json.loads(row["state_json"])
            initialized = bool(state.get("initialized"))
            queued += self._qweather_success(subscription, "alerts", now, topic, click_url)
            for alert in sorted(alerts, key=lambda value: (value.issued_at, value.alert_id)):
                if (not alert.alert_id or len(alert.alert_id) > 128
                        or alert.issued_at > now + 900
                        or (alert.message_type != "cancel" and alert.expires_at <= now)
                        or (alert.message_type == "cancel" and alert.issued_at < now - 21600)):
                    continue
                seen = self.connection.execute(
                    "SELECT 1 FROM weather_warning_messages WHERE subscription_id=? AND warning_id=?",
                    (subscription.id, alert.alert_id),
                ).fetchone()
                if seen:
                    continue
                ancestors = []
                for previous_id in alert.supersedes:
                    previous = self.connection.execute(
                        "SELECT root_id,priority,level,announced FROM weather_warning_messages "
                        "WHERE subscription_id=? AND warning_id=?", (subscription.id, previous_id),
                    ).fetchone()
                    if previous:
                        ancestors.append(previous)
                prior = max(ancestors, key=lambda value: (int(value["announced"]), int(value["level"]))) if ancestors else None
                root_id = str(prior["root_id"]) if prior else alert.alert_id
                previous_priority = int(prior["priority"]) if prior else 0
                previous_level = int(prior["level"]) if prior else 0
                previous_announced = bool(prior["announced"]) if prior else False
                priority = max({"extreme": 5, "severe": 5, "moderate": 4}.get(alert.severity, 2),
                               {"red": 5, "orange": 5, "yellow": 4}.get(alert.color, 2))
                level = max({"extreme": 4, "severe": 3, "moderate": 2}.get(alert.severity, 1),
                            {"red": 4, "orange": 3, "yellow": 2, "blue": 1}.get(alert.color, 0))
                cancelled = alert.message_type == "cancel"
                should_alert = subscription.alerts_enabled and (
                    (cancelled and previous_announced)
                    or (not cancelled and priority >= 4 and (
                        (prior is None and (initialized or (priority == 5 and now - alert.issued_at <= 21600)))
                        or (prior is not None and (level > previous_level or
                            (not previous_announced and now - alert.issued_at <= 3600)))
                    ))
                )
                if should_alert:
                    message = (f"{alert.sender or '气象机构'}：{alert.description[:500]}\n"
                               + (f"防御建议：{alert.instruction[:300]}\n" if alert.instruction else "")
                               +
                               "经 QWeather 转发；数据可能延迟，请以发布机构最新公告为准。\n"
                               "https://developer.qweather.com/attribution.html")
                    queued += int(self.database._insert_alert(AlertCandidate(
                        rule_id="weather.official_warning",
                        dedupe_key=f"weather:{subscription.id}:official:{alert.alert_id}",
                        title=("官方天气预警取消 · " if cancelled else "官方天气预警 · ")
                              + (alert.headline or alert.kind or "天气") + f" · {subscription.label}",
                        message=message, priority=3 if cancelled else priority,
                        tags=("warning",), click_url=click_url, topic=topic, confidence=1.0,
                        evidence=(f"QWeather relayed alert {alert.alert_id}",),
                    ), None, now))
                self.connection.execute(
                    "INSERT INTO weather_warning_messages(subscription_id,warning_id,root_id,priority,level,"
                    "announced,message_type,seen_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (subscription.id, alert.alert_id, root_id, max(priority, previous_priority),
                     max(level, previous_level),
                     int(should_alert or (previous_announced and not cancelled)),
                     alert.message_type, now, alert.expires_at),
                )
            self.connection.execute(
                "UPDATE weather_provider_state SET state_json=? WHERE subscription_id=? AND kind='alerts'",
                (json.dumps({"initialized": True}), subscription.id),
            )
            self.connection.execute(
                "DELETE FROM weather_warning_messages WHERE subscription_id=? AND expires_at<?",
                (subscription.id, now - 30 * 86400),
            )
        return queued

    def record_qweather_failure(self, subscription: WeatherSubscription, kind: str,
                                error: BaseException, now: int, *, topic: str, click_url: str) -> None:
        if kind not in {"minutely", "alerts", "astronomy"}:
            raise WeatherError("unknown QWeather operation")
        with self.database.unit_of_work():
            if self.get_weather_subscription().revision != subscription.revision:
                return
            self.connection.execute(
                "INSERT OR IGNORE INTO weather_provider_state(subscription_id,kind) VALUES(?,?)",
                (subscription.id, kind),
            )
            row = self.connection.execute(
                "SELECT consecutive_failures,outage_alerted,last_success_at FROM weather_provider_state "
                "WHERE subscription_id=? AND kind=?", (subscription.id, kind),
            ).fetchone()
            assert row is not None
            failures = int(row["consecutive_failures"]) + 1
            alerted = bool(row["outage_alerted"])
            if failures >= 3 and not alerted:
                self.database._insert_alert(AlertCandidate(
                    rule_id="weather.qweather_outage",
                    dedupe_key=f"weather:{subscription.id}:qweather:{kind}:outage:{row['last_success_at'] or 0}",
                    title=f"和风天气数据连续获取失败 · {subscription.label}",
                    message=f"{subscription.label} 的{kind}接口已连续 {failures} 次失败；相应天气提醒可能延迟。",
                    priority=4, tags=("warning",), click_url=click_url, topic=topic, confidence=1.0,
                ), None, now)
                alerted = True
            self.connection.execute(
                "UPDATE weather_provider_state SET last_error=?,consecutive_failures=?,outage_alerted=? "
                "WHERE subscription_id=? AND kind=?",
                (sanitize_error(error)[:300], failures, int(alerted), subscription.id, kind),
            )

    def record_qweather_success(self, subscription: WeatherSubscription, kind: str, now: int,
                                *, topic: str, click_url: str) -> int:
        if kind not in {"minutely", "alerts", "astronomy"}:
            raise WeatherError("unknown QWeather operation")
        with self.database.unit_of_work():
            if self.get_weather_subscription().revision != subscription.revision:
                return 0
            self.connection.execute(
                "INSERT OR IGNORE INTO weather_provider_state(subscription_id,kind) VALUES(?,?)",
                (subscription.id, kind),
            )
            return self._qweather_success(subscription, kind, now, topic, click_url)
