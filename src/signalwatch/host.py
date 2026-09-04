from __future__ import annotations

import json
import os
import shutil
import socket
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


def _failed_units(units: list[str]) -> list[str]:
    failed: list[str] = []
    for unit in units:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", "--quiet", unit],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=3,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode != 0:
            failed.append(unit)
    return failed


class HostHealthCollector:
    """Low-frequency local checks; high-rate telemetry belongs in Prometheus."""

    def __init__(self, config: SourceConfig) -> None:
        self.config = config
        settings = config.settings or {}
        self.paths = tuple(str(path) for path in settings.get("paths", ["/"]))
        self.units = tuple(str(unit) for unit in settings.get("units", []))
        self.disk_threshold = float(settings.get("disk_used_percent", 90.0))
        self.inode_threshold = float(settings.get("inode_used_percent", 90.0))
        self.memory_threshold = float(settings.get("memory_used_percent", 90.0))
        self.load_threshold = float(settings.get("load1", max(1.0, os.cpu_count() or 1)))
        self.port_allowlist = {int(port) for port in settings.get("allowed_listen_ports", []) if str(port).isdigit()}

    def fetch(self, state: SourceState) -> FeedFetchResult:
        now = datetime.now(UTC)
        active: dict[str, str] = {}
        for path in self.paths:
            try:
                usage = shutil.disk_usage(path)
                disk_used = usage.used / usage.total * 100.0 if usage.total else 0.0
                stat = os.statvfs(path)
                inode_used = ((stat.f_files - stat.f_favail) / stat.f_files * 100.0) if stat.f_files else 0.0
            except OSError as exc:
                active[f"path:{path}"] = f"无法检查 {path}: {type(exc).__name__}"
                continue
            if disk_used >= self.disk_threshold:
                active[f"disk:{path}"] = f"{path} 磁盘已使用 {disk_used:.1f}%"
            if inode_used >= self.inode_threshold:
                active[f"inode:{path}"] = f"{path} inode 已使用 {inode_used:.1f}%"
        memory = _mem_percent()
        if memory is not None and memory >= self.memory_threshold:
            active["memory"] = f"内存已使用 {memory:.1f}%"
        try:
            load1 = os.getloadavg()[0]
            if load1 >= self.load_threshold:
                active["load"] = f"1 分钟 load 为 {load1:.2f}"
        except OSError:
            pass
        for unit in _failed_units(list(self.units)):
            active[f"unit:{unit}"] = f"systemd 单元 {unit} 未处于 active"

        previous: dict[str, Any]
        try:
            previous = json.loads(state.cursor or "{}")
        except (TypeError, ValueError):
            previous = {}
        if not isinstance(previous, dict):
            previous = {}
        previous_active = set(previous.get("active", [])) if isinstance(previous.get("active", []), list) else set()
        observations: list[Observation] = []
        for identity, message in active.items():
            if identity in previous_active:
                continue
            observations.append(Observation(
                source_id=self.config.id, publisher=self.config.publisher,
                dedupe_scope=self.config.dedupe_scope, external_id=f"{identity}:{int(now.timestamp())}",
                published_at=now, title=f"主机异常：{identity}", summary=message, url="",
                attributes={"section": self.config.section, "check": identity},
            ))
        for identity in sorted(previous_active - set(active)):
            observations.append(Observation(
                source_id=self.config.id, publisher=self.config.publisher,
                dedupe_scope=self.config.dedupe_scope, external_id=f"{identity}:recovery:{int(now.timestamp())}",
                published_at=now, title=f"主机异常已恢复：{identity}", summary="检查项已回到阈值以内。", url="",
                attributes={"section": self.config.section, "check": identity, "recovery": True},
            ))
        cursor = json.dumps({"active": sorted(active), "updated_at": int(now.timestamp())}, sort_keys=True)
        return FeedFetchResult(tuple(observations), None, None, not observations, cursor=cursor)
