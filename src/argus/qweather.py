"""QWeather JWT adapter for Chinese near-term precipitation and relayed official alerts."""

from __future__ import annotations

import base64
import gzip
import io
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from .weather import (
    NowcastSlot, OfficialWeatherAlert, WeatherAstronomy, WeatherNowcast, WeatherSubscription,
    validate_nowcast,
)


_HOST = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)*\.qweatherapi\.com$")
_ID = re.compile(r"^[A-Z0-9]{10}$")
_MAX_BODY = 512_000


class QWeatherError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        raise QWeatherError("QWeather redirect is not allowed")


def _timestamp(value: Any) -> int:
    if not isinstance(value, str):
        raise QWeatherError("QWeather timestamp is missing")
    try:
        instant = datetime.fromisoformat(value)
    except ValueError as exc:
        raise QWeatherError("QWeather timestamp is invalid") from exc
    if instant.tzinfo is None:
        raise QWeatherError("QWeather timestamp has no timezone")
    return int(instant.timestamp())


def _optional_timestamp(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return _timestamp(value)


def _text(value: Any, limit: int = 200) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise QWeatherError("QWeather precipitation is invalid") from exc
    if not math.isfinite(number) or not 0 <= number <= 100:
        raise QWeatherError("QWeather precipitation is out of range")
    return number


def _b64(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")


class QWeatherProvider:
    def __init__(self, host: str, developer_id: str, project_id: str,
                 credential_id: str, private_key_file: str) -> None:
        if not _HOST.fullmatch(host) or any(not _ID.fullmatch(value) for value in
                                        (developer_id, project_id, credential_id)):
            raise QWeatherError("QWeather host or JWT identifiers are invalid")
        key_path = Path(private_key_file)
        if not key_path.is_absolute() or key_path.is_symlink() or not key_path.is_file():
            raise QWeatherError("QWeather private-key path is invalid")
        if key_path.stat().st_mode & 0o007 or key_path.stat().st_mode & 0o030:
            raise QWeatherError("QWeather private-key permissions must not allow public or group writes")
        key = load_pem_private_key(key_path.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise QWeatherError("QWeather private key must be Ed25519")
        self.host = host
        self.developer_id = developer_id
        self.project_id = project_id
        self.credential_id = credential_id
        self.key = key
        self.opener = urllib.request.build_opener(_NoRedirect)

    @classmethod
    def from_environment(cls) -> QWeatherProvider | None:
        names = ("QWEATHER_API_HOST", "QWEATHER_DEVELOPER_ID", "QWEATHER_PROJECT_ID",
                 "QWEATHER_CREDENTIAL_ID", "QWEATHER_PRIVATE_KEY_FILE")
        values = tuple(os.environ.get(name, "") for name in names)
        if not any(values):
            return None
        if not all(values):
            raise QWeatherError("QWeather JWT configuration is incomplete")
        return cls(*values)

    def _token(self) -> str:
        now = int(time.time())
        header = _b64(json.dumps({"alg": "EdDSA", "kid": self.credential_id},
                                 separators=(",", ":")).encode())
        payload = _b64(json.dumps({"iss": self.developer_id, "sub": self.project_id,
                                  "iat": now, "exp": now + 900}, separators=(",", ":")).encode())
        signing_input = header + b"." + payload
        return (signing_input + b"." + _b64(self.key.sign(signing_input))).decode("ascii")

    def _request(self, path: str) -> dict[str, Any]:
        request = urllib.request.Request(
            f"https://{self.host}{path}",
            headers={"Authorization": "Bearer " + self._token(), "Accept": "application/json",
                     "Accept-Encoding": "gzip", "User-Agent": "EyeOfSauron/0.24"},
        )
        try:
            with self.opener.open(request, timeout=12) as response:
                if response.status != 200 or not response.headers.get("Content-Type", "").lower().startswith("application/json"):
                    raise QWeatherError("QWeather returned an invalid HTTP response")
                raw = response.read(_MAX_BODY + 1)
                if len(raw) > _MAX_BODY:
                    raise QWeatherError("QWeather response is too large")
                if response.headers.get("Content-Encoding", "").lower() == "gzip":
                    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                        raw = stream.read(_MAX_BODY + 1)
                    if len(raw) > _MAX_BODY:
                        raise QWeatherError("QWeather decompressed response is too large")
                result = json.loads(raw)
        except urllib.error.HTTPError as exc:
            raise QWeatherError(f"QWeather returned HTTP {exc.code}") from exc
        except (OSError, ValueError) as exc:
            raise QWeatherError(f"QWeather request failed: {type(exc).__name__}") from exc
        if not isinstance(result, dict) or result.get("code", "200") != "200":
            raise QWeatherError("QWeather returned an invalid payload")
        return result

    def fetch_minutely(self, subscription: WeatherSubscription) -> WeatherNowcast:
        location = urllib.parse.quote(f"{subscription.longitude:.2f},{subscription.latitude:.2f}")
        payload = self._request(f"/v7/minutely/5m?location={location}")
        values = payload.get("minutely")
        if not isinstance(values, list) or not 1 <= len(values) <= 48:
            raise QWeatherError("QWeather minutely series is missing")
        slots = []
        for item in values:
            if not isinstance(item, dict) or item.get("type") not in {"rain", "snow"}:
                raise QWeatherError("QWeather minutely item is invalid")
            slots.append(NowcastSlot(_timestamp(item.get("fxTime")),
                                     _number(item.get("precip")), item["type"]))
        if any(right.at <= left.at for left, right in zip(slots, slots[1:])):
            raise QWeatherError("QWeather minutely times are unordered")
        issued_at = _timestamp(payload.get("updateTime"))
        now = int(time.time())
        if not now - 1800 <= issued_at <= now + 900 or slots[-1].at < now + 1800:
            raise QWeatherError("QWeather minutely forecast is stale")
        result = WeatherNowcast(issued_at=issued_at, slots=tuple(slots))
        validate_nowcast(result, now)
        return result

    def fetch_alerts(self, subscription: WeatherSubscription) -> tuple[OfficialWeatherAlert, ...]:
        payload = self._request(
            f"/weatheralert/v1/current/{subscription.latitude:.2f}/{subscription.longitude:.2f}"
        )
        values = payload.get("alerts")
        if not isinstance(values, list) or len(values) > 100:
            raise QWeatherError("QWeather alert list is invalid")
        alerts = []
        for item in values:
            if not isinstance(item, dict) or not _text(item.get("id"), 128):
                raise QWeatherError("QWeather alert item is invalid")
            message_type = item.get("messageType") or {}
            event_type = item.get("eventType") or {}
            color = item.get("color") or {}
            if not isinstance(message_type, dict) or not isinstance(event_type, dict) or not isinstance(color, dict):
                raise QWeatherError("QWeather alert details are invalid")
            if message_type.get("code") not in {"alert", "update", "cancel"}:
                raise QWeatherError("QWeather alert message type is invalid")
            supersedes = message_type.get("supersedes") or []
            if not isinstance(supersedes, list) or not all(isinstance(value, str) for value in supersedes):
                raise QWeatherError("QWeather supersedes list is invalid")
            alerts.append(OfficialWeatherAlert(
                alert_id=_text(item["id"], 128), issued_at=_timestamp(item.get("issuedTime")),
                expires_at=_timestamp(item.get("expireTime")),
                message_type=_text(message_type.get("code"), 16),
                supersedes=tuple(value[:128] for value in supersedes[:20]),
                severity=_text(item.get("severity"), 16), color=_text(color.get("code"), 16),
                kind=_text(event_type.get("name"), 80), headline=_text(item.get("headline"), 200),
                description=_text(item.get("description"), 1200),
                sender=_text(item.get("senderName"), 100),
                instruction=_text(item.get("instruction"), 1000),
            ))
        return tuple(alerts)

    def fetch_astronomy(self, subscription: WeatherSubscription, date: str) -> WeatherAstronomy:
        if not re.fullmatch(r"\d{8}", date):
            raise QWeatherError("QWeather astronomy date is invalid")
        location = urllib.parse.quote(f"{subscription.longitude:.2f},{subscription.latitude:.2f}")
        sun = self._request(f"/v7/astronomy/sun?location={location}&date={date}")
        moon = self._request(f"/v7/astronomy/moon?location={location}&date={date}")
        phases = moon.get("moonPhase")
        if not isinstance(phases, list):
            phases = []
        phase = next((item for item in phases if isinstance(item, dict)
                      and isinstance(item.get("fxTime"), str)
                      and item["fxTime"][11:13] == "12" and item.get("name")), None)
        if phase is None:
            phase = next((item for item in phases if isinstance(item, dict) and item.get("name")), None)
        illumination: float | None = None
        if isinstance(phase, dict):
            try:
                illumination = float(str(phase.get("illumination")))
            except (TypeError, ValueError):
                illumination = None
        local_now = datetime.now(ZoneInfo(subscription.timezone))
        offset = local_now.utcoffset() or UTC.utcoffset(local_now)
        tz = f"{int(offset.total_seconds() // 3600):+03d}00"
        angle_payload: dict[str, Any] = {}
        if local_now.strftime("%Y%m%d") == date:
            try:
                angle_payload = self._request(
                    f"/v7/astronomy/solar-elevation-angle?location={location}&date={date}"
                    f"&time={local_now:%H%M}&tz={tz}"
                )
            except QWeatherError:
                pass  # Optional angle data must not hide valid sun/moon times.
        def optional_number(value: Any, low: float, high: float) -> float | None:
            try:
                number = float(value)
            except (TypeError, ValueError):
                return None
            return number if math.isfinite(number) and low <= number <= high else None
        return WeatherAstronomy(
            date=date,
            sunrise=_optional_timestamp(sun.get("sunrise")),
            sunset=_optional_timestamp(sun.get("sunset")),
            moonrise=_optional_timestamp(moon.get("moonrise")),
            moonset=_optional_timestamp(moon.get("moonset")),
            moon_phase=str(phase["name"])[:80] if isinstance(phase, dict) else None,
            moon_illumination=illumination,
            solar_elevation=optional_number(angle_payload.get("solarElevationAngle"), -90, 90),
            solar_azimuth=optional_number(angle_payload.get("solarAzimuthAngle"), 0, 360),
        )
