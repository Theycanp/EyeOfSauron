from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from argus.database import Database
from test_weather import DAY, forecast


class WeatherPolicyRuntimeTests(unittest.TestCase):
    def test_midnight_resets_daily_usage_but_preserves_latest_request_after_restart(self) -> None:
        midnight = int(datetime(2026, 9, 28, tzinfo=UTC).timestamp())
        channels = (("qweather", "hourly"), ("open_meteo", "air_quality"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            database = Database(path)
            try:
                for provider, kind in channels:
                    self.assertTrue(database.reserve_weather_provider_request(provider, kind, now=midnight - 30))
                    database.record_weather_provider_error(provider, kind, "previous-day error", now=midnight - 30)
                database.close()
                database = Database(path)
                with patch("argus.sqlite_weather_policy.datetime") as clock:
                    clock.now.return_value = datetime.fromtimestamp(midnight + 30, UTC)
                    clock.fromtimestamp.side_effect = datetime.fromtimestamp
                    policies = database.list_weather_provider_policies()
                    for provider, kind in channels:
                        with self.subTest(provider=provider, kind=kind):
                            row = next(item for item in policies if (item["provider"], item["kind"]) == (provider, kind))
                            self.assertEqual("2026-09-28", row["budget_day"])
                            self.assertEqual(0, row["requests"])
                            self.assertIsNone(row["last_error"])
                            self.assertEqual(midnight - 30, row["last_request_at"])
                            self.assertTrue(database.reserve_weather_provider_request(provider, kind, now=midnight + 60))
                            database.record_weather_provider_error(provider, kind, "current-day error", now=midnight + 60)
                    updated = database.list_weather_provider_policies()
                    for provider, kind in channels:
                        row = next(item for item in updated if (item["provider"], item["kind"]) == (provider, kind))
                        self.assertEqual(1, row["requests"])
                        self.assertEqual("current-day error", row["last_error"])
                        self.assertEqual(midnight + 60, row["last_request_at"])
            finally:
                database.close()

    def test_provider_availability_is_visible_without_exposing_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "state.db")
            try:
                now = int(datetime.now(UTC).timestamp())
                database.record_weather_provider_availability("qweather", configured=False, now=now)
                policies = [row for row in database.list_weather_provider_policies() if row["provider"] == "qweather"]
                self.assertTrue(all(row["status"] == "not_configured" for row in policies))
                self.assertFalse(database.reserve_weather_provider_request("qweather", "hourly", now=now))
                database.record_weather_provider_availability("qweather", configured=True, now=now)
                hourly = next(row for row in database.list_weather_provider_policies() if row["kind"] == "hourly")
                self.assertEqual("conditional_standby", hourly["status"])
                self.assertEqual("ok", database.connection.execute("PRAGMA quick_check").fetchone()[0])
            finally:
                database.close()
    def test_forecast_product_switch_preserves_daily_and_rain_episode_dedupe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "state.db")
            try:
                subscription = database.get_weather_subscription()
                now = DAY + 10 * 3600
                def record(value, at):
                    database.record_weather_forecast(subscription, value, now=at, topic="eos", click_url="")
                record(forecast(now), now)
                record(replace(forecast(now + 3600, rain=1.2, probability=70), provider="QWeather",
                               conditions_basis="hourly_forecast"), now + 3600)
                database.close()
                database = Database(Path(directory) / "state.db")
                record(forecast(now + 7200, rain=1.2, probability=70), now + 7200)
                rules = [row[0] for row in database.connection.execute("SELECT rule_id FROM alerts ORDER BY id")]
                self.assertEqual(["weather.daily", "weather.rain_change"], rules)
                message = database.connection.execute("SELECT message FROM alerts WHERE rule_id='weather.rain_change'").fetchone()[0]
                self.assertIn("QWeather", message)
                self.assertNotIn("Open-Meteo", message)
                self.assertEqual("Open-Meteo", database.get_weather_status()["latest"]["provider"])
            finally:
                database.close()

    def test_usage_retention_removes_old_days_and_keeps_current_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "state.db")
            try:
                now = int(datetime.now(UTC).timestamp())
                database.reserve_weather_provider_request("qweather", "alerts", now=now - 91 * 86400)
                database.reserve_weather_provider_request("qweather", "alerts", now=now)
                database.cleanup(now - 90 * 86400)
                self.assertEqual(1, database.connection.execute("SELECT COUNT(*) FROM weather_provider_usage").fetchone()[0])
                row = next(item for item in database.list_weather_provider_policies() if item["kind"] == "alerts")
                self.assertEqual(1, row["requests"])
            finally:
                database.close()
