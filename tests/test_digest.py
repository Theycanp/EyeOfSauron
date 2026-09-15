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
    adaptive_digest_item_count,
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
    def _cluster(self, score: float, *, handling: str = "digest", key: str = "x") -> DigestCluster:
        return DigestCluster(
            cluster_key=f"{key}-{score}", title="主题", summary="摘要", score=score,
            importance=3, urgency=2, relevance=3, confidence=0.5,
            published_at=START, regions=("CN",), topics=("general",),
            source_ids=("source",), observation_ids=(1,), links=("https://example.test",),
            handling=handling,
        )

    def test_adaptive_item_count_scales_with_weighted_signal(self) -> None:
        quiet = adaptive_digest_item_count([self._cluster(2.0, key=str(i)) for i in range(4)], max_items=50)
        busy = adaptive_digest_item_count([self._cluster(5.5, key=str(i)) for i in range(20)], max_items=50)
        self.assertEqual(2, quiet)
        self.assertEqual(28, busy)

    def test_adaptive_item_count_keeps_immediate_events_and_hard_ceiling(self) -> None:
        clusters = [self._cluster(1.0, handling="immediate", key=str(i)) for i in range(8)]
        self.assertEqual(8, adaptive_digest_item_count(clusters, max_items=10))
        self.assertEqual(5, adaptive_digest_item_count(clusters, max_items=5))

    def test_cluster_observations_adaptive_mode_uses_score_based_count(self) -> None:
        rows = [
            _row(index, f"独立主题 {index}", source_id=f"source_{index}", topic=f"topic_{index}", importance=2, urgency=1)
            for index in range(1, 11)
        ]
        selected = cluster_observations(rows, max_items=50, adaptive=True)
        self.assertEqual(9, len(selected))

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

    def test_cluster_keeps_one_representative_link_per_source(self) -> None:
        rows = [
            _row(1, "熊本县发布雷电注意报", source_id="jma"),
            _row(2, "熊本县发布雷电注意报", source_id="jma"),
            _row(3, "熊本县发布雷电注意报", source_id="local_news"),
        ]

        cluster = cluster_observations(rows)[0]

        self.assertEqual(("jma", "local_news"), cluster.source_ids)
        self.assertEqual(3, len(cluster.observation_ids))
        self.assertEqual(
            ("https://example.test/2", "https://example.test/3"),
            cluster.links,
        )

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

    def test_digest_retry_state_and_three_day_failure_threshold_are_idempotent(self) -> None:
        first = self.database.start_digest_retry("daily:2026-09-01", 1, 100, 100 + 18000)
        self.assertEqual(1, first.attempts)
        self.assertEqual("pending", first.status)
        updated = self.database.record_digest_retry(
            first.digest_key, attempts=2, status="pending",
            next_attempt_at=4600, last_error="temporary", now=200,
        )
        self.assertEqual(2, updated.attempts)
        self.assertEqual(4600, updated.next_attempt_at)
        self.database.record_digest_retry(
            first.digest_key, attempts=5, status="failed",
            next_attempt_at=None, last_error="permanent", now=300,
        )
        self.assertEqual("failed", self.database.get_digest_retry(first.digest_key).status)
        self.assertEqual((1, False), self.database.record_digest_failure("daily:2026-09-01", now=301))
        self.assertEqual((2, False), self.database.record_digest_failure("daily:2026-09-02", now=302))
        self.assertEqual((3, True), self.database.record_digest_failure("daily:2026-09-03", now=303))
        self.assertEqual((3, False), self.database.record_digest_failure("daily:2026-09-03", now=304))
        self.assertEqual(3, self.database.status()["digest_ai"]["consecutive_failure_days"])
        self.assertIn("argus_digest_ai_failure_streak 3", self.database.metrics_prometheus())
        alerted = self.database.enqueue_digest_failure_notification(
            digest_key="daily:2026-09-03", streak=3, error="provider unavailable",
            topic="eos", click_url="https://eos.example.test/digests/daily%3A2026-09-03",
            now=304,
        )
        again = self.database.enqueue_digest_failure_notification(
            digest_key="daily:2026-09-03", streak=3, error="provider unavailable",
            topic="eos", click_url="https://eos.example.test/digests/daily%3A2026-09-03",
            now=305,
        )
        self.assertTrue(alerted)
        self.assertFalse(again)
        self.database.record_digest_success(now=305)
        self.assertEqual((1, False), self.database.record_digest_failure("daily:2026-09-05", now=306))

    def test_digest_api_budget_is_independent_and_persists_across_reopen(self) -> None:
        key = "daily:2026-09-06"
        self.assertTrue(self.database.reserve_digest_api_call(key, limit=5, now=100))
        self.assertTrue(self.database.reserve_analysis_api_call("2026-09-06", limit=1, now=100))
        self.assertFalse(self.database.reserve_analysis_api_call("2026-09-06", limit=1, now=101))
        self.database.close()
        self.database = Database(self.path)
        for _ in range(4):
            self.assertTrue(self.database.reserve_digest_api_call(key, limit=5, now=102))
        self.assertFalse(self.database.reserve_digest_api_call(key, limit=5, now=102))

    def test_schema_fourteen_migrates_to_durable_digest_retry_tables(self) -> None:
        self.database.connection.executescript(
            "DROP TABLE digest_retry_state; DROP TABLE digest_failure_state; "
            "DROP TABLE digest_api_usage; PRAGMA user_version=14;"
        )
        self.database.close()
        self.database = Database(self.path)
        self.assertEqual(SCHEMA_VERSION, self.database.status()["database_schema"])
        tables = {
            str(row[0]) for row in self.database.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertTrue(
            {"digest_retry_state", "digest_failure_state", "digest_api_usage"} <= tables
        )
        self.database.close()
        self.database = Database(self.path)
        self.assertTrue(self.database.reserve_digest_api_call("daily:2026-09-06", limit=5, now=100))

    def test_expired_digest_retry_can_be_discovered_after_downtime(self) -> None:
        retry = self.database.start_digest_retry("daily:2026-09-06", 1, 100, 18100)
        self.assertEqual([], self.database.list_expired_digest_retries(18100))
        self.database.close()
        self.database = Database(self.path)
        self.assertEqual([retry], self.database.list_expired_digest_retries(18101))


if __name__ == "__main__":
    unittest.main()
