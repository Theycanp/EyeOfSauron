from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from argus.database import Database
from argus.open_meteo import OpenMeteoProvider, WeatherProviderError
from argus.weather import (
    ForecastHour, NowcastSlot, OfficialWeatherAlert, WeatherAirQuality, WeatherAstronomy,
    WeatherNowcast, WeatherForecast, WeatherError, daily_due,
    remaining_day_rain, summarize_weather, validate_forecast, weather_signals,
)


TZ = ZoneInfo("Asia/Shanghai")
DAY = int(datetime(2026, 9, 26, tzinfo=TZ).timestamp())


def forecast(now: int, *, rain: float = 0, probability: float = 0,
             gust: float = 20, tomorrow_temperature: float = 20) -> WeatherForecast:
    start = int(datetime.fromtimestamp(now, TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    hours = tuple(ForecastHour(
        at=start + index * 3600,
        temperature=tomorrow_temperature if index >= 24 else 20,
        precipitation=rain if index == 17 else 0,
        rain_probability=probability if index == 17 else 0,
        wind_gust=gust,
    ) for index in range(72))
    return WeatherForecast(observed_at=now, temperature=20, weather_code=2, hours=hours)


class WeatherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "weather.db"
        self.database = Database(self.path)
        self.subscription = self.database.get_weather_subscription()

    def tearDown(self) -> None:
        self.database.close()
        self.temp.cleanup()

    def disable_daily(self) -> None:
        editable = {key: value for key, value in asdict(self.subscription).items() if key != "id"}
        self.subscription = self.database.update_weather_subscription(
            {**editable, "daily_enabled": False},
            actor="test", now=DAY,
        )

    def record(self, at: int, **values: float) -> int:
        return self.database.record_weather_forecast(
            self.subscription, forecast(at, **values), now=at,
            topic="eos", click_url="https://example.test/#/weather",
        )

    def alert_rules(self) -> list[str]:
        return [str(row[0]) for row in self.database.connection.execute(
            "SELECT rule_id FROM alerts ORDER BY id"
        )]

    def test_schema_seed_uses_verified_campus_location(self) -> None:
        self.assertEqual(24, self.database.connection.execute("PRAGMA user_version").fetchone()[0])
        self.assertEqual("北京邮电大学沙河校区", self.subscription.label)
        self.assertAlmostEqual(40.1561163, self.subscription.latitude)
        self.assertAlmostEqual(116.2835626, self.subscription.longitude)
        self.assertEqual("07:00", self.subscription.daily_time)

    def test_daily_is_once_per_local_day_and_late_run_does_not_send_old_report(self) -> None:
        seven = DAY + 7 * 3600 + 60
        self.assertEqual(1, self.record(seven))
        self.assertEqual(0, self.record(seven + 3600))
        self.assertEqual(["weather.daily"], self.alert_rules())
        self.assertIsNone(daily_due(self.subscription, None, DAY + 16 * 3600))
        self.assertIsNone(daily_due(self.subscription, None, DAY + 6 * 3600))

    def test_rain_change_uses_persisted_baseline_and_no_repeat_after_restart(self) -> None:
        self.disable_daily()
        ten = DAY + 10 * 3600
        self.assertEqual(0, self.record(ten))
        self.assertEqual(1, self.record(ten + 3600, rain=1.2, probability=70))
        self.database.close()
        self.database = Database(self.path)
        self.assertEqual(0, self.record(ten + 2 * 3600, rain=1.2, probability=70))
        self.assertEqual(0, self.record(ten + 3 * 3600))
        self.assertEqual(0, self.record(ten + 4 * 3600, rain=1.2, probability=70))
        self.assertEqual(["weather.rain_change"], self.alert_rules())
        self.assertTrue(self.database.get_weather_status()["rain_expected"])

    def test_heavy_rain_suppresses_duplicate_ordinary_change_and_wind_escalates(self) -> None:
        self.disable_daily()
        ten = DAY + 10 * 3600
        self.record(ten)
        self.assertEqual(2, self.record(ten + 3600, rain=10, probability=90, gust=60))
        self.assertEqual(0, self.record(ten + 2 * 3600, rain=10, probability=90, gust=60))
        self.assertEqual(1, self.record(ten + 3 * 3600, rain=10, probability=90, gust=90))
        self.assertEqual(["weather.rain", "weather.wind", "weather.wind"], self.alert_rules())
        self.assertEqual([4, 4, 5], [row[0] for row in self.database.connection.execute(
            "SELECT priority FROM alerts ORDER BY id"
        )])

    def test_three_failures_alert_once_and_recovery_notifies(self) -> None:
        self.disable_daily()
        ten = DAY + 10 * 3600
        for offset in range(4):
            self.database.record_weather_failure(
                self.subscription, RuntimeError("temporary outage"), ten + offset * 1200,
                topic="custom", click_url="https://example.test/#/weather",
            )
        self.assertEqual(["weather.provider_outage"], self.alert_rules())
        self.assertEqual("custom", self.database.connection.execute("SELECT topic FROM alerts").fetchone()[0])
        self.assertEqual(1, self.record(ten + 5000))
        self.assertEqual(0, self.database.get_weather_status()["consecutive_failures"])
        self.assertEqual(["weather.provider_outage", "weather.provider_recovered"], self.alert_rules())

    def test_location_update_resets_baseline_and_rejects_stale_revision(self) -> None:
        initial = {key: value for key, value in asdict(self.subscription).items() if key != "id"}
        updated = self.database.update_weather_subscription(
            {**initial, "label": "北京市天安门", "latitude": 39.905, "longitude": 116.397},
            actor="owner", now=DAY,
        )
        self.assertEqual(2, updated.revision)
        self.assertEqual(1, self.database.connection.execute(
            "SELECT COUNT(*) FROM weather_subscription_audit"
        ).fetchone()[0])
        with self.assertRaisesRegex(WeatherError, "changed"):
            self.database.update_weather_subscription(initial, actor="owner", now=DAY + 1)
        with self.assertRaisesRegex(WeatherError, "invalid"):
            self.database.update_weather_subscription(
                {**{key: value for key, value in asdict(updated).items() if key != "id"},
                 "latitude": float("nan")}, actor="owner", now=DAY + 1,
            )

    def test_schema_twenty_two_migration_is_additive(self) -> None:
        self.database.close()
        connection = sqlite3.connect(self.path)
        connection.execute("DROP TABLE weather_subscription_audit")
        connection.execute("DROP TABLE weather_state")
        connection.execute("DROP TABLE weather_provider_state")
        connection.execute("DROP TABLE weather_warning_messages")
        connection.execute("DROP TABLE weather_subscriptions")
        connection.execute("PRAGMA user_version=22")
        connection.commit()
        connection.close()
        self.database = Database(self.path)
        self.assertEqual(24, self.database.connection.execute("PRAGMA user_version").fetchone()[0])
        self.assertEqual("北京邮电大学沙河校区", self.database.get_weather_subscription().label)

    def test_schema_twenty_three_migration_preserves_existing_weather_state(self) -> None:
        now = DAY + 10 * 3600
        self.record(now)
        before = self.database.get_weather_status()
        self.database.close()
        connection = sqlite3.connect(self.path)
        connection.execute("DROP TABLE weather_air_quality")
        connection.execute("DROP TABLE weather_astronomy")
        connection.execute("PRAGMA user_version=23")
        connection.commit()
        connection.close()
        self.database = Database(self.path)
        after = self.database.get_weather_status()
        self.assertEqual(24, self.database.connection.execute("PRAGMA user_version").fetchone()[0])
        self.assertEqual(before["subscription"], after["subscription"])
        self.assertEqual(before["rain_expected"], after["rain_expected"])
        self.assertEqual(before["latest"]["observed_at"], after["latest"]["observed_at"])

    def test_air_quality_and_astronomy_use_typed_tables_and_survive_restart(self) -> None:
        now = DAY + 10 * 3600
        self.database.record_weather_air_quality(
            self.subscription, WeatherAirQuality(now, 12.5, 20, 28, 45), now=now,
        )
        self.database.record_weather_astronomy(
            self.subscription,
            WeatherAstronomy("20260926", now - 4 * 3600, now + 8 * 3600,
                             now + 7 * 3600, now - 3 * 3600, "盈凸月", 95, 45.2, 230.0),
            now=now,
        )
        self.record(now)
        self.database.close()
        self.database = Database(self.path)
        with patch("argus.sqlite_weather.time.time", return_value=now):
            latest = self.database.get_weather_status()["latest"]
        self.assertEqual(12.5, latest["air_quality"]["pm2_5"])
        self.assertEqual("盈凸月", latest["astronomy"]["moon_phase"])
        self.assertEqual(45.2, latest["astronomy"]["solar_elevation"])
        self.assertEqual(1, self.database.connection.execute(
            "SELECT COUNT(*) FROM weather_air_quality"
        ).fetchone()[0])

    def test_daily_message_and_test_snapshot_keep_real_data_separate(self) -> None:
        now = DAY + 7 * 3600 + 60
        self.database.record_weather_air_quality(
            self.subscription, WeatherAirQuality(now, 15, 25, 30, 50), now=now,
        )
        self.database.record_weather_astronomy(
            self.subscription,
            WeatherAstronomy("20260926", now - 3600, now + 11 * 3600,
                             now + 10 * 3600, now - 2 * 3600, "满月", 99, 22, 90),
            now=now,
        )
        self.assertEqual(1, self.record(now))
        daily = self.database.connection.execute(
            "SELECT message FROM alerts WHERE rule_id='weather.daily'"
        ).fetchone()[0]
        for expected in ("农历2026年8月16日", "PM2.5 15.0", "月相 满月", "太阳高度角 22.0"):
            self.assertIn(expected, daily)
        self.assertTrue(self.database.enqueue_weather_test(
            topic="eos", click_url="https://example.test/#/weather", now=now + 1,
        ))
        test = self.database.connection.execute(
            "SELECT message FROM alerts WHERE rule_id='weather.test'"
        ).fetchone()[0]
        self.assertIn("[测试通知]", test)
        self.assertIn("不代表官方气象预警", test)
        self.assertEqual("2026-09-26", str(datetime.fromtimestamp(now, TZ).date()))

    def test_stale_optional_weather_data_is_not_presented_as_current(self) -> None:
        now = DAY + 10 * 3600
        self.database.record_weather_air_quality(
            self.subscription, WeatherAirQuality(now, 12, 20, 25, 40), now=now,
        )
        self.database.record_weather_astronomy(
            self.subscription, WeatherAstronomy("20260925", None, None, None, None, "旧月相", 40), now=now,
        )
        self.record(now)
        with patch("argus.sqlite_weather.time.time", return_value=now + 7 * 3600):
            latest = self.database.get_weather_status()["latest"]
        self.assertIsNone(latest["air_quality"])
        self.assertIsNone(latest["astronomy"])
        with self.assertRaisesRegex(WeatherError, "fresh"):
            self.database.enqueue_weather_test(topic="eos", click_url="", now=now + 25 * 3600)

    def test_forecast_rules_are_conservative(self) -> None:
        now = DAY + 10 * 3600
        self.assertEqual((1.2, 70), remaining_day_rain(
            self.subscription, forecast(now, rain=1.2, probability=70), now
        ))
        self.assertEqual((), weather_signals(forecast(now, rain=1.2, probability=70), now))
        self.assertEqual(["wind"], [signal.kind for signal in weather_signals(
            forecast(now, gust=60), now
        )])

    def test_invalid_or_stale_forecast_never_changes_rain_baseline(self) -> None:
        self.disable_daily()
        now = DAY + 10 * 3600
        valid = forecast(now, rain=1.2, probability=70)
        broken = replace(valid, hours=tuple(
            replace(hour, rain_probability=None) if hour.at == now + 3600 else hour
            for hour in valid.hours
        ))
        for invalid in (replace(valid, observed_at=now - 7201), broken,
                        replace(valid, hours=valid.hours[:12])):
            with self.assertRaises(WeatherError):
                self.database.record_weather_forecast(
                    self.subscription, invalid, now=now, topic="eos", click_url="",
                )
        self.assertIsNone(self.database.get_weather_status()["rain_expected"])
        self.assertEqual([], self.alert_rules())
        validate_forecast(valid, now)

    def test_same_day_rain_flapping_has_one_alert_and_new_day_gets_new_baseline(self) -> None:
        self.disable_daily()
        ten = DAY + 10 * 3600
        self.record(ten)
        self.record(ten + 3600, rain=1.2, probability=70)
        self.record(ten + 2 * 3600)
        self.record(ten + 3 * 3600, rain=1.2, probability=70)
        self.assertEqual(["weather.rain_change"], self.alert_rules())
        next_day = DAY + 86400 + 10 * 3600
        self.database.record_weather_forecast(
            self.subscription, forecast(next_day, rain=1.2, probability=70),
            now=next_day, topic="eos", click_url="",
        )
        self.assertEqual(["weather.rain_change"], self.alert_rules())

    def test_nowcast_rain_is_once_per_episode_and_snow_is_a_material_change(self) -> None:
        self.disable_daily()
        now = DAY + 10 * 3600
        def put(at: int, phase: str) -> int:
            return self.database.record_weather_nowcast(
                self.subscription, WeatherNowcast(at, tuple(
                    NowcastSlot(at + index * 300, 0.1, phase) for index in range(24)
                )), now=at, topic="custom", click_url="",
            )
        self.assertEqual(1, put(now, "rain"))
        self.assertEqual(0, put(now + 600, "rain"))
        self.database.close()
        self.database = Database(self.path)
        self.assertEqual(0, put(now + 1200, "rain"))
        self.assertEqual(1, put(now + 1800, "snow"))
        self.assertEqual(["weather.nowcast", "weather.nowcast"], self.alert_rules())

    def test_official_warning_baseline_upgrade_cancel_and_dedupe(self) -> None:
        now = DAY + 10 * 3600
        initial = OfficialWeatherAlert(
            "initial", now - 7200, now + 86400, "alert", (), "moderate", "yellow",
            "雷电", "雷电黄色预警", "注意雷电", "昌平区气象台",
        )
        def put(at: int, *values: OfficialWeatherAlert) -> int:
            return self.database.record_official_weather_alerts(
                self.subscription, tuple(values), now=at, topic="custom", click_url="",
            )
        self.assertEqual(0, put(now, initial))
        self.assertEqual(0, put(now + 600, initial))
        upgraded = replace(initial, alert_id="upgrade", issued_at=now + 600,
                           message_type="update", supersedes=("initial",), severity="severe", color="orange")
        self.assertEqual(1, put(now + 601, upgraded))
        self.assertEqual(0, put(now + 602, upgraded))
        red = replace(upgraded, alert_id="red", issued_at=now + 900,
                      supersedes=("upgrade",), color="red")
        self.assertEqual(1, put(now + 901, red))
        self.assertEqual(0, put(now + 902, red))
        cancelled = replace(red, alert_id="cancel", issued_at=now + 1200,
                            expires_at=now + 1200,
                            message_type="cancel", supersedes=("red",))
        self.assertEqual(1, put(now + 1201, cancelled))
        self.assertEqual(0, put(now + 1202, cancelled))
        self.assertEqual(["weather.official_warning"] * 3, self.alert_rules())

    def test_qweather_failures_are_independent_and_recover_once(self) -> None:
        now = DAY + 10 * 3600
        for index in range(4):
            self.database.record_qweather_failure(
                self.subscription, "alerts", RuntimeError("unavailable"), now + index * 300,
                topic="custom", click_url="",
            )
        self.assertEqual(1, len(self.alert_rules()))
        self.assertEqual(0, self.database.get_weather_status()["qweather"]["minutely"]["consecutive_failures"])
        self.assertEqual(1, self.database.record_official_weather_alerts(
            self.subscription, (), now=now + 1500, topic="custom", click_url="",
        ))
        self.assertEqual(["weather.qweather_outage", "weather.qweather_recovered"], self.alert_rules())

    def test_warning_absence_is_not_cancellation_and_write_failure_is_atomic(self) -> None:
        now = DAY + 10 * 3600
        alert = OfficialWeatherAlert("severe", now, now + 86400, "alert", (),
                                     "severe", "orange", "wind", "Wind", "Careful", "Authority")
        with patch.object(self.database, "_insert_alert", side_effect=RuntimeError("write failed")):
            with self.assertRaisesRegex(RuntimeError, "write failed"):
                self.database.record_official_weather_alerts(
                    self.subscription, (alert,), now=now, topic="eos", click_url="",
                )
        self.assertIsNone(self.database.get_weather_status()["qweather"]["alerts"]["last_success_at"])
        self.assertEqual(0, self.database.connection.execute(
            "SELECT COUNT(*) FROM weather_warning_messages"
        ).fetchone()[0])
        self.assertEqual(1, self.database.record_official_weather_alerts(
            self.subscription, (alert,), now=now, topic="eos", click_url="",
        ))
        self.assertEqual(0, self.database.record_official_weather_alerts(
            self.subscription, (), now=now + 600, topic="eos", click_url="",
        ))
        self.assertEqual(1, len(self.alert_rules()))

    def test_provider_uses_fixed_hosts_and_rejects_bad_shape(self) -> None:
        times = [(datetime(2026, 9, 26, tzinfo=ZoneInfo("UTC")).replace(hour=hour % 24)
                  .timestamp() + hour // 24 * 86400) for hour in range(72)]
        iso = [datetime.fromtimestamp(at, ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M") for at in times]
        payload = {"current": {"time": iso[10], "temperature_2m": 20, "weather_code": 2},
                   "hourly": {"time": iso, "temperature_2m": [20] * 72,
                              "precipitation_probability": [0] * 72,
                              "precipitation": [0] * 72, "wind_gusts_10m": [20] * 72}}
        with patch("argus.open_meteo._request_json", return_value=payload) as request:
            result = OpenMeteoProvider().fetch(self.subscription)
        self.assertEqual(72, len(result.hours))
        self.assertEqual("https://api.open-meteo.com", request.call_args.args[0])
        with patch("argus.open_meteo._request_json", return_value={"current": payload["current"], "hourly": {"time": iso}}):
            with self.assertRaises(WeatherProviderError):
                OpenMeteoProvider().fetch(self.subscription)


if __name__ == "__main__":
    unittest.main()
