from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from argus.config import DigestConfig
from argus.database import SCHEMA_VERSION, Database
from argus.digest import (
    DigestBuilder,
    DigestCluster,
    DigestDocument,
    SourceCoverage,
    cluster_observations,
    with_api_summary,
    DigestScheduler,
)


START = 1_788_307_200
END = START + 86_400


def _row(
    identifier: int,
    title: str,
    *,
    source_id: str,
    region: str = "CN",
    topic: str = "policy",
    importance: int = 4,
    urgency: int = 3,
) -> dict[str, Any]:
    return {
        "id": identifier,
        "source_id": source_id,
        "publisher": source_id,
        "published_at": START + identifier,
        "title": title,
        "summary": f"{title}的详细信息",
        "url": f"https://example.test/{identifier}",
        "importance": importance,
        "urgency": urgency,
        "relevance": 4,
        "confidence": 0.8,
        "region": region,
        "topic": topic,
        "source_tier": "primary",
        "information_type": "announcement",
        "handling": "digest",
        "processing_state": "analyzed",
    }


class FakeDigestRepository:
    def __init__(self, observations: list[dict[str, Any]]) -> None:
        self.observations = observations
        self.saved: list[DigestDocument] = []

    def list_digest_observations(
        self, since: int, until: int, *, source_ids: Sequence[str] | None = None,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        return [item for item in self.observations
                if source_ids is None or item['source_id'] in source_ids][:limit]

    def list_observations(
        self,
        since: int,
        until: int,
        *,
        handling: str | None = None,
        region: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        return self.observations[:limit]

    def list_source_coverage(
        self,
        since: int,
        until: int,
        *,
        source_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        return [
            {"source_id": source_id, "status": "covered", "observation_count": 1}
            for source_id in (source_ids or ("gov_cn", "news_cn"))
        ]

    def save_digest(self, digest: DigestDocument) -> DigestDocument:
        saved = DigestDocument(
            digest_key=digest.digest_key,
            version=len(self.saved) + 1,
            period_start=digest.period_start,
            period_end=digest.period_end,
            timezone=digest.timezone,
            title=digest.title,
            summary=digest.summary,
            generation_kind=digest.generation_kind,
            items=digest.items,
            coverage=digest.coverage,
            created_at=digest.created_at,
        )
        self.saved.append(saved)
        return saved

    def publish_digest(self, digest_key: str, version: int, now: int) -> DigestDocument:
        raise NotImplementedError

    def get_digest(
        self,
        digest_key: str,
        version: int | None = None,
        *,
        published_only: bool = False,
    ) -> DigestDocument | None:
        return None

    def list_digests(
        self, *, status: str | None = None, limit: int = 30
    ) -> list[DigestDocument]:
        return self.saved[:limit]


class DigestDomainTests(unittest.TestCase):
    def test_clusters_duplicate_reports_and_ranks_regional_interest(self) -> None:
        rows = [
            _row(1, "财政部将向八家金融央企增资3600亿元", source_id="mof_cn"),
            _row(2, "财政部向8家中央金融企业增资3600亿元", source_id="xinhua_cn"),
            _row(
                3,
                "Bank of Japan publishes routine statistics",
                source_id="boj_jp",
                region="JP",
                topic="economy",
                importance=2,
                urgency=1,
            ),
        ]
        clusters = cluster_observations(rows)
        self.assertEqual(2, len(clusters))
        self.assertEqual((1, 2), clusters[0].observation_ids)
        self.assertEqual(("mof_cn", "xinhua_cn"), clusters[0].source_ids)
        self.assertGreater(clusters[0].score, clusters[1].score)

    def test_builder_uses_repository_ports_and_reports_coverage(self) -> None:
        repository = FakeDigestRepository(
            [_row(1, "日本央行维持政策利率不变", source_id="boj_jp", region="JP")]
        )
        digest = DigestBuilder(repository).build_and_save(
            digest_key="daily:2026-09-06",
            period_start=START,
            period_end=END,
            timezone="Asia/Shanghai",
            created_at=END,
            source_ids=("boj_jp", "mof_cn"),
        )
        self.assertEqual(1, digest.version)
        self.assertEqual(("boj_jp", "mof_cn"), tuple(item.source_id for item in digest.coverage))
        self.assertIn("2/2", digest.summary)

    def test_api_polish_creates_new_draft_payload(self) -> None:
        original = DigestBuilder(FakeDigestRepository([])).build(
            digest_key="daily:2026-09-06",
            period_start=START,
            period_end=END,
            timezone="Asia/Shanghai",
            created_at=END,
        )
        polished = with_api_summary(original, "  API 生成的摘要  ", created_at=END + 1)
        self.assertEqual("algorithm", original.generation_kind)
        self.assertEqual("api", polished.generation_kind)
        self.assertEqual("API 生成的摘要", polished.summary)
        self.assertEqual(0, polished.version)


class DigestDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.db"
        self.database = Database(self.path)

    def tearDown(self) -> None:
        self.database.close()
        self.temp.cleanup()

    def _digest(self, *, created_at: int = END, summary: str = "算法摘要") -> DigestDocument:
        return DigestDocument(
            digest_key="daily:2026-09-06",
            version=0,
            period_start=START,
            period_end=END,
            timezone="Asia/Shanghai",
            title="每日情报摘要",
            summary=summary,
            generation_kind="algorithm",
            items=(
                DigestCluster(
                    cluster_key="cluster-1",
                    title="重要政策公告",
                    summary="公告摘要",
                    score=4.75,
                    importance=5,
                    urgency=3,
                    relevance=5,
                    confidence=0.9,
                    published_at=START + 100,
                    regions=("CN",),
                    topics=("policy",),
                    source_ids=("mof_cn",),
                    observation_ids=(42,),
                    links=("https://example.test/42",),
                ),
            ),
            coverage=(
                SourceCoverage("mof_cn", "covered", 1, END - 10, END - 10, 0),
            ),
            created_at=created_at,
        )

    def test_versions_and_publication_are_atomic_and_reversible(self) -> None:
        first = self.database.save_digest(self._digest())
        second = self.database.save_digest(self._digest(created_at=END + 1, summary="修订摘要"))
        self.assertEqual((1, 2), (first.version, second.version))
        published_first = self.database.publish_digest(first.digest_key, first.version, END + 2)
        self.assertEqual("published", published_first.status)
        self.database.publish_digest(second.digest_key, second.version, END + 3)
        previous = self.database.get_digest(first.digest_key, 1)
        self.assertIsNotNone(previous)
        assert previous is not None
        self.assertEqual("superseded", previous.status)
        current = self.database.get_digest(first.digest_key, published_only=True)
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(2, current.version)
        self.assertEqual("修订摘要", current.summary)
        self.assertEqual((42,), current.items[0].observation_ids)
        self.assertEqual("covered", current.coverage[0].status)

    def test_source_coverage_distinguishes_quiet_degraded_and_unknown(self) -> None:
        self.database.sync_source_runtime(
            [("quiet", "rss", True, 300), ("failed", "rss", True, 300)],
            {"quiet", "failed"},
            config_revision=1,
            now=START,
        )
        with self.database.connection:
            self.database.connection.execute(
                "UPDATE collector_state SET last_success_at = ? WHERE source_id = 'quiet'",
                (START + 100,),
            )
            self.database.connection.execute(
                "UPDATE collector_state SET consecutive_failures = 2 WHERE source_id = 'failed'"
            )
        coverage = {
            item["source_id"]: item["status"]
            for item in self.database.list_source_coverage(
                START, END, source_ids=("quiet", "failed", "missing")
            )
        }
        self.assertEqual(
            {"quiet": "quiet", "failed": "degraded", "missing": "unknown"}, coverage
        )

    def test_schema_nine_migrates_to_digest_schema(self) -> None:
        self.database.close()
        connection = sqlite3.connect(self.path)
        connection.executescript(
            "DROP TABLE digest_source_coverage; DROP TABLE digest_items; DROP TABLE digests; "
            "PRAGMA user_version=9;"
        )
        connection.close()
        self.database = Database(self.path)
        version = int(self.database.connection.execute("PRAGMA user_version").fetchone()[0])
        tables = {
            str(row[0])
            for row in self.database.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertEqual(SCHEMA_VERSION, version)
        self.assertTrue({"digests", "digest_items", "digest_source_coverage"} <= tables)

    def test_daily_scheduler_publishes_and_notifies_idempotently(self) -> None:
        config = DigestConfig(
            enabled=True,
            timezone="Asia/Shanghai",
            daily_time="20:00",
            notify=True,
            public_base_url="https://eos.example.test",
        )
        scheduler = DigestScheduler(config, self.database, topic="eos")
        before = int(datetime(2026, 9, 6, 19, 59, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
        due = int(datetime(2026, 9, 6, 20, 1, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
        catchup = scheduler.process_once(before)
        self.assertEqual("daily:2026-09-05", catchup.digest_key)
        first = scheduler.process_once(due)
        second = scheduler.process_once(due + 60)
        self.assertIsNotNone(first)
        self.assertEqual(first, second)
        self.assertEqual(2, len(self.database.list_digests(status="published")))
        alerts = self.database.list_alerts()
        self.assertEqual(2, len(alerts))
        self.assertIn("daily%3A2026-09-06", alerts[0]["click_url"])


if __name__ == "__main__":
    unittest.main()
