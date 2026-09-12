from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from .config import SourceConfig
from .models import FeedFetchResult, Observation, SourceState


class HostHealthError(RuntimeError):
    pass


def _mem_percent() -> float | None:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            key, _, value = line.partition(":")
            if value.strip().endswith(" kB"):
                values[key] = int(value.strip()[:-3])
    except (OSError, ValueError):
        return None
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if not total or available is None:
        return None
    return max(0.0, min(100.0, (total - available) / total * 100.0))


def _unit_health(units: list[str]) -> dict[str, str]:
    health: dict[str, str] = {}
    for unit in units:
        try:
            result = subprocess.run(
                ["/usr/bin/systemctl", "is-active", "--quiet", "--", unit],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=3,
            )
        except (OSError, subprocess.SubprocessError):
            health[unit] = "unknown"
            continue
        health[unit] = "healthy" if result.returncode == 0 else "unhealthy"
    return health


def _listen_ports() -> set[tuple[str, str, int]] | None:
    """Return protocol, local address and port for listening TCP/UDP sockets."""
    try:
        result = subprocess.run(
            ["/usr/bin/ss", "-H", "-lntu"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    sockets: set[tuple[str, str, int]] = set()
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        protocol = fields[0].lower()
        endpoint = fields[4]
        if endpoint.startswith("[") and "]:" in endpoint:
            address, raw_port = endpoint[1:].rsplit("]:", 1)
        else:
            address, separator, raw_port = endpoint.rpartition(":")
            if not separator:
                continue
        if raw_port.isdigit():
            sockets.add((protocol, address or "*", int(raw_port)))
    return sockets


def _family(address: str) -> str:
    if address == "*":
        return "any"
    return "ipv6" if ":" in address else "ipv4"


def _port_spec(value: Any) -> tuple[str, str, int] | None:
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535:
        return "any", "any", value
    if not isinstance(value, str):
        return None
    pieces = value.lower().split(":")
    if len(pieces) == 2 and pieces[1].isdigit():
        protocol, family, port = pieces[0], "any", int(pieces[1])
    elif len(pieces) == 3 and pieces[2].isdigit():
        protocol, family, port = pieces[0], pieces[1], int(pieces[2])
    else:
        return None
    if protocol not in {"tcp", "udp", "any"} or family not in {"ipv4", "ipv6", "any"}:
        return None
    return (protocol, family, port) if 1 <= port <= 65535 else None


def _matches(spec: tuple[str, str, int], socket: tuple[str, str, int]) -> bool:
    protocol, family, port = spec
    actual_protocol, address, actual_port = socket
    return (
        port == actual_port
        and protocol in {"any", actual_protocol}
        and family in {"any", _family(address)}
    )


class HostHealthCollector:
    """Low-frequency tri-state local checks; unknown never means recovered."""

    def __init__(self, config: SourceConfig) -> None:
        self.config = config
        settings = config.settings or {}
        self.paths = tuple(str(path) for path in settings.get("paths", ["/"]))
        self.units = tuple(str(unit) for unit in settings.get("units", []))
        self.disk_threshold = float(settings.get("disk_used_percent", 90.0))
        self.inode_threshold = float(settings.get("inode_used_percent", 90.0))
        self.memory_threshold = float(settings.get("memory_used_percent", 90.0))
        self.load_threshold = float(settings.get("load1", max(1.0, os.cpu_count() or 1)))
        self.port_allowlist = tuple(
            spec for value in settings.get("allowed_listen_ports", [])
            if (spec := _port_spec(value)) is not None
        )
        self.required_ports = tuple(
            spec for value in settings.get("required_listen_ports", [])
            if (spec := _port_spec(value)) is not None
        )

    def fetch(self, state: SourceState) -> FeedFetchResult:
        now = datetime.now(UTC)
        active: dict[str, str] = {}
        unknown_prefixes: set[str] = set()
        for path in self.paths:
            try:
                usage = shutil.disk_usage(path)
                disk_used = usage.used / usage.total * 100.0 if usage.total else 0.0
                stat = os.statvfs(path)
                inode_used = ((stat.f_files - stat.f_favail) / stat.f_files * 100.0) if stat.f_files else 0.0
            except OSError as exc:
                active[f"path:{path}"] = f"无法检查 {path}: {type(exc).__name__}"
                unknown_prefixes.update({f"disk:{path}", f"inode:{path}"})
                continue
            if disk_used >= self.disk_threshold:
                active[f"disk:{path}"] = f"{path} 磁盘已使用 {disk_used:.1f}%"
            if inode_used >= self.inode_threshold:
                active[f"inode:{path}"] = f"{path} inode 已使用 {inode_used:.1f}%"

        memory = _mem_percent()
        if memory is None:
            unknown_prefixes.add("memory")
        elif memory >= self.memory_threshold:
            active["memory"] = f"内存已使用 {memory:.1f}%"
        try:
            load1 = os.getloadavg()[0]
        except OSError:
            unknown_prefixes.add("load")
        else:
            if load1 >= self.load_threshold:
                active["load"] = f"1 分钟 load 为 {load1:.2f}"

        for unit, health in _unit_health(list(self.units)).items():
            identity = f"unit:{unit}"
            if health == "unhealthy":
                active[identity] = f"systemd 单元 {unit} 未处于 active"
            elif health == "unknown":
                unknown_prefixes.add(identity)

        if self.port_allowlist or self.required_ports:
            listening = _listen_ports()
            if listening is None:
                unknown_prefixes.add("listen:")
            else:
                for socket_info in sorted(listening):
                    protocol, address, port = socket_info
                    if self.port_allowlist and not any(
                        _matches(spec, socket_info) for spec in self.port_allowlist
                    ):
                        identity = f"listen:{protocol}:{_family(address)}:{address}:{port}"
                        active[identity] = (
                            f"发现未列入白名单的 {protocol.upper()} {_family(address)} "
                            f"监听 {address}:{port}"
                        )
                for protocol, family, port in self.required_ports:
                    if not any(_matches((protocol, family, port), item) for item in listening):
                        identity = f"listen:missing:{protocol}:{family}:{port}"
                        active[identity] = f"要求的 {protocol.upper()} {family} 端口 {port} 当前未监听"

        try:
            previous = json.loads(state.cursor or "{}")
        except (TypeError, ValueError):
            previous = {}
        if not isinstance(previous, dict):
            previous = {}
        previous_active = set(previous.get("active", [])) if isinstance(previous.get("active"), list) else set()
        preserved_unknown = {
            identity
            for identity in previous_active
            if any(identity == prefix or identity.startswith(prefix) for prefix in unknown_prefixes)
        }
        current_active = set(active) | preserved_unknown
        observations: list[Observation] = []
        for identity, message in active.items():
            if identity in previous_active:
                continue
            observations.append(Observation(
                source_id=self.config.id,
                publisher=self.config.publisher,
                dedupe_scope=self.config.dedupe_scope,
                external_id=f"{identity}:{int(now.timestamp())}",
                published_at=now,
                title=f"主机异常：{identity}",
                summary=message,
                url="",
                attributes={
                    "section": self.config.section,
                    "check": identity,
                    "incident_key": f"host:{identity}",
                    "stateful": True,
                    "live_state": True,
                },
            ))
        for identity in sorted(previous_active - current_active):
            observations.append(Observation(
                source_id=self.config.id,
                publisher=self.config.publisher,
                dedupe_scope=self.config.dedupe_scope,
                external_id=f"{identity}:recovery:{int(now.timestamp())}",
                published_at=now,
                title=f"主机异常已恢复：{identity}",
                summary="检查项已回到阈值以内。",
                url="",
                attributes={
                    "section": self.config.section,
                    "check": identity,
                    "incident_key": f"host:{identity}",
                    "stateful": True,
                    "recovery": True,
                },
            ))
        cursor = json.dumps(
            {"active": sorted(current_active), "updated_at": int(now.timestamp())},
            sort_keys=True,
        )
        return FeedFetchResult(
            tuple(observations),
            None,
            None,
            not_modified=not observations,
            cursor=cursor,
            warnings=tuple(sorted(f"unknown:{prefix}" for prefix in unknown_prefixes)),
        )
