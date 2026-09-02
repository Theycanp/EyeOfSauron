from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from signalwatch.config import AppConfig, load_config
from signalwatch.models import Observation


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def production_config(tmp_path: Path) -> AppConfig:
    config = load_config(PROJECT_ROOT / "config" / "signalwatch.production.toml")
    return replace(
        config,
        service=replace(
            config.service,
            database_path=tmp_path / "state.db",
            lock_path=tmp_path / "service.lock",
        ),
    )


def observation(
    external_id: str,
    title: str,
    summary: str = "",
    source_id: str = "bloomberg_markets",
    timestamp: int = 1788361200,
) -> Observation:
    return Observation(
        source_id=source_id,
        publisher="Bloomberg",
        dedupe_scope="bloomberg",
        external_id=external_id,
        published_at=datetime.fromtimestamp(timestamp, UTC),
        title=title,
        summary=summary,
        url=f"https://www.bloomberg.com/news/articles/{external_id}",
        attributes={"section": "Markets"},
    )
