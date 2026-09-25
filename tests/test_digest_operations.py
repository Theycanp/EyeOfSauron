from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock

from argus.auth import AdminAuth
from argus.database import Database
from argus.digest import DigestCluster, DigestDocument, SourceCoverage, with_api_summary
from argus.digest_analysis import ApiDigestSummarizer
from argus.model_analyzers import AnalyzerSettings, OpenAICompatibleAnalyzer


NOW = 1_789_214_400


def digest() -> DigestDocument:
    return DigestDocument(
        digest_key="daily:2026-09-12", version=0,
        period_start=NOW - 86400, period_end=NOW,
        timezone="Asia/Shanghai", title="每日情报摘要", summary="算法摘要",
        generation_kind="algorithm",
        items=(DigestCluster(
            cluster_key="cluster-1", title="政策更新", summary="官方摘要",
            score=4.5, importance=4, urgency=3, relevance=4, confidence=0.9,
            published_at=NOW - 100, regions=("EAST_ASIA",), topics=("policy",),
            source_ids=("official",), observation_ids=(1,), links=("https://example.test/1",),
        ),),
        coverage=(SourceCoverage("official", "covered", 1, NOW - 100, NOW - 100, 0),),
        created_at=NOW,
    )


class DigestRunRepositoryTests(unittest.TestCase):
    def test_run_uses_latest_published_ai_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "state.db")
            try:
                algorithm = database.save_digest(digest())
                algorithm = database.publish_digest(algorithm.digest_key, algorithm.version, NOW)
                ai = database.save_digest(with_api_summary(algorithm, "AI summary", created_at=NOW + 1))
                database.publish_digest(ai.digest_key, ai.version, NOW + 1)

                run = database.get_digest_run(ai.digest_key, now=NOW + 2)
                assert run is not None
                self.assertEqual(("ai_published", ai.version, "api"), (
                    run["state"], run["published_version"], run["generation_kind"],
                ))

                # A damaged legacy index can leave two published rows. Keep the
                # read model deterministic until that data is repaired.
                with database.unit_of_work():
                    database.connection.execute("DROP INDEX digests_one_published_idx")
                    database.connection.execute(
                        "UPDATE digests SET status='published' WHERE digest_key=? AND version=?",
                        (algorithm.digest_key, algorithm.version),
                    )
                run = database.get_digest_run(ai.digest_key, now=NOW + 2)
                assert run is not None
                self.assertEqual(("ai_published", ai.version, "api"), (
                    run["state"], run["published_version"], run["generation_kind"],
                ))
            finally:
                database.close()

    def test_running_attempt_is_interrupted_once_after_restart_and_history_is_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            database = Database(path)
            attempt = database.start_digest_attempt("daily:2026-09-12", now=NOW)
            database.close()
            reopened = Database(path)
            try:
                # register_engine is the restart boundary used by the service.
                reopened.register_engine(
                    "new-instance", started_at=NOW + 10, applied_revision=19,
                    code_version="test", configured_sources=0, active_sources=0,
                )
                run = reopened.get_digest_run("daily:2026-09-12", now=NOW + 10)
                assert run is not None
                self.assertEqual("interrupted", run["attempts"][0]["status"])
                self.assertEqual("unpublished", run["state"])
                reopened.register_engine(
                    "second-instance", started_at=NOW + 20, applied_revision=19,
                    code_version="test", configured_sources=0, active_sources=0,
                )
                run_again = reopened.get_digest_run("daily:2026-09-12", now=NOW + 20)
                assert run_again is not None
                self.assertEqual(1, len(run_again["attempts"]))
                self.assertEqual(attempt, run_again["attempts"][0]["id"])
            finally:
                reopened.close()

    def test_provider_trace_is_allowlisted_and_sanitized(self) -> None:
        first = OpenAICompatibleAnalyzer(AnalyzerSettings("https://api.example.test/v1", "first"))
        second = OpenAICompatibleAnalyzer(AnalyzerSettings("https://api.example.test/v1", "second"))
        first.complete = Mock(side_effect=RuntimeError(
            "Bearer TOPSECRET token=https://user:password@example.test/x?api_key=SECRET"))  # type: ignore[method-assign]
        second.complete = Mock(return_value=json.dumps({
            "summary": "这是一段足够长的摘要，用于验证备用模型成功并且引用能够和证据编号严格对应。[1]", "citations": [1],
        }))  # type: ignore[method-assign]
        summarizer = ApiDigestSummarizer((first, second))
        self.assertIn("备用模型成功", summarizer.summarize(digest()))
        self.assertEqual(2, len(summarizer.last_attempts))
        trace = summarizer.last_attempts[0]
        self.assertEqual({"provider", "model", "prompt_id", "prompt_version", "prompt_hash", "status", "error", "elapsed_ms"}, set(trace))
        self.assertNotIn("TOPSECRET", json.dumps(trace))
        self.assertNotIn("password", json.dumps(trace))
        self.assertNotIn("SECRET", json.dumps(trace))
        self.assertEqual("failed", trace["status"])
        self.assertEqual("succeeded", summarizer.last_attempts[1]["status"])

    def test_corrupt_provider_trace_does_not_break_the_read_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "state.db")
            try:
                attempt_id = database.start_digest_attempt("daily:corrupt", now=NOW)
                second_attempt_id = database.start_digest_attempt("daily:corrupt", now=NOW + 1)
                database.connection.execute(
                    "UPDATE digest_generation_attempts SET providers_json=? WHERE id=?",
                    ("not-json", attempt_id),
                )
                database.connection.execute(
                    "UPDATE digest_generation_attempts SET providers_json=? WHERE id=?",
                    ('[{"provider":"' + ("x" * 10000) + '"}]', second_attempt_id),
                )
                database.connection.commit()
                run = database.get_digest_run("daily:corrupt", now=NOW + 1)
                assert run is not None
                self.assertEqual(500, len(run["attempts"][0]["providers"][0]["provider"]))
                self.assertEqual([], run["attempts"][1]["providers"])
            finally:
                database.close()

    def test_retry_budget_and_window_are_enforced_without_reopening_exhausted_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "state.db")
            try:
                saved = database.save_digest(digest())
                database.publish_digest(saved.digest_key, saved.version, NOW)
                retry = database.start_digest_retry(saved.digest_key, saved.version, NOW, NOW + 18_000)
                self.assertEqual(1, retry.attempts)
                first_request = database.request_digest_retry_now(
                    saved.digest_key, actor="owner", request_id="req-idempotent", now=NOW + 1,
                )
                second_request = database.request_digest_retry_now(
                    saved.digest_key, actor="owner", request_id="req-idempotent", now=NOW + 2,
                )
                self.assertEqual(first_request["id"], second_request["id"])
                self.assertEqual(first_request["result"], second_request["result"])
                database.record_digest_retry(saved.digest_key, attempts=5, status="pending",
                                             next_attempt_at=NOW + 100, last_error="limit", now=NOW + 1)
                run = database.get_digest_run(saved.digest_key, now=NOW + 2)
                assert run is not None
                self.assertFalse(run["can_retry_now"])
                with self.assertRaises(ValueError):
                    database.request_digest_retry_now(saved.digest_key, actor="owner", request_id="req-1", now=NOW + 2)
                database.record_digest_retry(saved.digest_key, attempts=5, status="failed",
                                             next_attempt_at=None, last_error="exhausted", now=NOW + 3)
                run = database.get_digest_run(saved.digest_key, now=NOW + 4)
                assert run is not None
                self.assertEqual("retry_exhausted", run["state"])
                self.assertFalse(run["can_retry_now"])
            finally:
                database.close()


class DigestRetryAuthTests(unittest.TestCase):
    def test_operator_permissions_and_csrf_contract_for_manual_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "state.db")
            try:
                auth = AdminAuth(database)
                database.create_admin_user("operator", "Operator", auth.hash_password("operator-password-value"), "operator", "bootstrap", NOW)
                database.create_admin_user("reader", "Reader", auth.hash_password("reader-password-value"), "viewer", "bootstrap", NOW)
                operator, session, csrf = auth.login("operator", "operator-password-value", "127.0.0.1", now=NOW)
                reader, reader_session, reader_csrf = auth.login("reader", "reader-password-value", "127.0.0.1", now=NOW)
                self.assertTrue(operator.allows("operations:write"))
                self.assertFalse(reader.allows("operations:write"))
                self.assertTrue(auth.validate_csrf(operator, {"Cookie": f"__Host-eos_session={session}; __Host-eos_csrf={csrf}", "X-CSRF-Token": csrf}))
                self.assertFalse(auth.validate_csrf(operator, {"Cookie": f"__Host-eos_session={session}; __Host-eos_csrf={csrf}"}))
                self.assertTrue(auth.validate_csrf(reader, {"Cookie": f"__Host-eos_session={reader_session}; __Host-eos_csrf={reader_csrf}", "X-CSRF-Token": reader_csrf}))
            finally:
                database.close()


if __name__ == "__main__":
    unittest.main()
