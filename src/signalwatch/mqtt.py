from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Mapping, Protocol


class MqttError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SensorEvent:
    device_id: str
    kind: str
    value: Any
    unit: str | None
    observed_at: int
    topic: str
    raw: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class CommandRequest:
    device_id: str
    action: str
    parameters: Mapping[str, Any]
    idempotency_key: str
    expires_at: int


class MqttTransport(Protocol):
    def publish(self, topic: str, payload: bytes, qos: int = 1, retain: bool = False) -> None:
        ...


class SensorNormalizer:
    """Normalize ESPHome/Zigbee2MQTT JSON without storing high-frequency telemetry."""

    def parse(self, topic: str, payload: bytes, observed_at: int | None = None) -> SensorEvent:
        try:
            value = json.loads(payload)
        except (TypeError, ValueError) as exc:
            raise MqttError("sensor payload is not valid JSON") from exc
        if not isinstance(value, Mapping):
            raise MqttError("sensor payload must be an object")
        device_id = str(value.get("device_id") or self._device_from_topic(topic)).strip()
        kind = str(value.get("kind") or value.get("type") or "state").strip()
        if not device_id or not kind:
            raise MqttError("sensor event requires device_id and kind")
        unit = value.get("unit")
        return SensorEvent(device_id, kind, value.get("value", value.get("state")), str(unit) if unit else None, observed_at or int(time.time()), topic, value)

    @staticmethod
    def _device_from_topic(topic: str) -> str:
        pieces = [piece for piece in topic.split("/") if piece]
        return pieces[-2] if len(pieces) >= 2 else ""


class CommandPolicy:
    def __init__(self, devices: Mapping[str, Mapping[str, Mapping[str, Any]]], max_ttl_seconds: int = 300) -> None:
        self.devices = devices
        self.max_ttl_seconds = max_ttl_seconds

    def validate(self, request: CommandRequest, now: int | None = None) -> str:
        now = int(time.time()) if now is None else now
        actions = self.devices.get(request.device_id)
        if not actions or request.action not in actions:
            raise MqttError("device or action is not allowlisted")
        if not request.idempotency_key or len(request.idempotency_key) > 128:
            raise MqttError("invalid idempotency key")
        if request.expires_at < now or request.expires_at > now + self.max_ttl_seconds:
            raise MqttError("command expiry is outside the allowed window")
        constraints = actions[request.action]
        if not isinstance(request.parameters, Mapping):
            raise MqttError("command parameters must be an object")
        for name in constraints.get("required", []):
            if name not in request.parameters:
                raise MqttError(f"missing command parameter: {name}")
        return hashlib.sha256(f"{request.device_id}\x1f{request.action}\x1f{request.idempotency_key}".encode()).hexdigest()
