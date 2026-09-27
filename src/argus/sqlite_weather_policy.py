"""SQLite request quotas and operator controls for independent weather channels."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .util import sanitize_error

if TYPE_CHECKING:
    from .database import Database


CHANNELS = frozenset({
    ("open_meteo", "forecast"), ("open_meteo", "air_quality"),
    ("qweather", "minutely"), ("qweather", "alerts"),
    ("qweather", "astronomy"), ("qweather", "hourly"),
})


class SQLiteWeatherPolicy:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.connection = database.connection

    def list(self, subscription_id: str = "home") -> list[dict[str, Any]]:
        if not subscription_id or len(subscription_id) > 64:
            raise ValueError("weather subscription ID is invalid")
        today = datetime.now(UTC).date().isoformat()
        rows = self.connection.execute(
            "SELECT p.provider,p.kind,p.enabled,p.interval_seconds,p.daily_budget,p.updated_at,p.updated_by,"
            "p.configured,p.configuration_checked_at,"
            "COALESCE(u.requests,0) requests,u.last_error,"
            "(SELECT MAX(history.last_request_at) FROM weather_provider_usage history "
            "WHERE history.subscription_id=p.subscription_id AND history.provider=p.provider "
            "AND history.kind=p.kind) last_request_at "
            "FROM weather_provider_policy p LEFT JOIN weather_provider_usage u "
            "ON u.subscription_id=p.subscription_id AND u.provider=p.provider AND u.kind=p.kind "
            "AND u.budget_day=? WHERE p.subscription_id=? ORDER BY p.provider,p.kind",
            (today, subscription_id),
        ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["budget_day"] = today
            item["status"] = (
                "disabled" if not item["enabled"] else
                "not_configured" if item["configured"] == 0 else
                "budget_exhausted" if item["requests"] >= item["daily_budget"] else
                "error" if item["last_error"] else
                "awaiting_engine" if item["configured"] is None else
                "conditional_standby" if item["kind"] == "hourly" else "enabled"
            )
            results.append(item)
        return results

    def record_availability(self, provider: str, *, configured: bool, now: int) -> None:
        if provider not in {"open_meteo", "qweather"} or type(configured) is not bool or now < 0:
            raise ValueError("weather provider availability is invalid")
        with self.database.unit_of_work():
            self.connection.execute(
                "UPDATE weather_provider_policy SET configured=?,configuration_checked_at=? WHERE provider=?",
                (int(configured), now, provider),
            )

    def update(self, provider: str, kind: str, *, enabled: bool, interval_seconds: int,
               daily_budget: int, actor: str, now: int, subscription_id: str = "home") -> dict[str, Any]:
        if (provider, kind) not in CHANNELS:
            raise ValueError("unknown weather provider channel")
        if type(enabled) is not bool or type(interval_seconds) is not int or not 60 <= interval_seconds <= 86400:
            raise ValueError("weather provider interval is invalid")
        if type(daily_budget) is not int or not 1 <= daily_budget <= 10000:
            raise ValueError("weather provider budget is invalid")
        if not actor or len(actor) > 128 or now < 0:
            raise ValueError("weather policy audit metadata is invalid")
        with self.database.unit_of_work():
            changed = self.connection.execute(
                "UPDATE weather_provider_policy SET enabled=?,interval_seconds=?,daily_budget=?,"
                "updated_at=?,updated_by=? WHERE subscription_id=? AND provider=? AND kind=?",
                (int(enabled), interval_seconds, daily_budget, now, actor, subscription_id, provider, kind),
            )
            if changed.rowcount != 1:
                raise ValueError("weather provider policy is missing")
            self.connection.execute(
                "INSERT INTO admin_auth_audit(user_id,action,actor,details_json,created_at) "
                "VALUES(NULL,'weather_policy_updated',?,?,?)",
                (actor, json.dumps({"provider": provider, "kind": kind, "enabled": enabled,
                                    "interval_seconds": interval_seconds, "daily_budget": daily_budget},
                                   ensure_ascii=False, sort_keys=True), now),
            )
        return next(item for item in self.list(subscription_id)
                    if item["provider"] == provider and item["kind"] == kind)

    def reserve(self, provider: str, kind: str, *, now: int, subscription_id: str = "home") -> bool:
        if now < 0:
            raise ValueError("weather request time is invalid")
        day = datetime.fromtimestamp(now, UTC).date().isoformat()
        with self.database.unit_of_work():
            policy = self.connection.execute(
                "SELECT enabled,daily_budget,configured FROM weather_provider_policy "
                "WHERE subscription_id=? AND provider=? AND kind=?", (subscription_id, provider, kind),
            ).fetchone()
            if policy is None or not bool(policy["enabled"]) or policy["configured"] == 0:
                return False
            self.connection.execute(
                "INSERT OR IGNORE INTO weather_provider_usage(subscription_id,provider,kind,budget_day) "
                "VALUES(?,?,?,?)", (subscription_id, provider, kind, day),
            )
            changed = self.connection.execute(
                "UPDATE weather_provider_usage SET requests=requests+1,last_request_at=?,last_error=NULL "
                "WHERE subscription_id=? AND provider=? AND kind=? AND budget_day=? AND requests < ?",
                (now, subscription_id, provider, kind, day, int(policy["daily_budget"])),
            )
            return changed.rowcount == 1

    def record_error(self, provider: str, kind: str, error: BaseException | str, *, now: int,
                     subscription_id: str = "home") -> None:
        day = datetime.fromtimestamp(now, UTC).date().isoformat()
        with self.database.unit_of_work():
            self.connection.execute(
                "INSERT OR IGNORE INTO weather_provider_usage(subscription_id,provider,kind,budget_day) "
                "VALUES(?,?,?,?)", (subscription_id, provider, kind, day),
            )
            self.connection.execute(
                "UPDATE weather_provider_usage SET last_error=? WHERE subscription_id=? AND provider=? "
                "AND kind=? AND budget_day=?",
                (sanitize_error(error), subscription_id, provider, kind, day),
            )

    def cleanup(self, cutoff: int) -> None:
        day = datetime.fromtimestamp(cutoff, UTC).date().isoformat()
        self.connection.execute("DELETE FROM weather_provider_usage WHERE budget_day < ?", (day,))
