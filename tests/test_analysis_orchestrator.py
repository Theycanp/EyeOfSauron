from __future__ import annotations

import unittest
from dataclasses import replace
from tempfile import TemporaryDirectory
from pathlib import Path

from argus.analysis_orchestrator import AnalysisOrchestrator
from argus.cli import _build_service
from argus.config import AnalysisConfig, ConfigError
from argus.database import Database
from argus.models import AnalysisWorkItem
from tests.helpers import observation, production_config


class _Analyzer:
    def __init__(self, name: str, result=None, error: Exception | None = None):
        self.name = name
        self.result = result
        self.error = error
        self.calls = 0

    def analyze(self, _observation):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


class _Repository:
    def __init__(self, item: AnalysisWorkItem, budget: bool = True):
        self.item = item
        self.budget = budget
        self.saved = []
        self.reservations = []
        self.claimed = False

    def claim_analysis_observations(self, now, *, limit, lease_seconds):
        self.claim_args = (now, limit, lease_seconds)
        if self.claimed:
            return []
        self.claimed = True
        return [self.item]

    def reserve_analysis_api_call(self, budget_day, *, limit, now):
        self.reservations.append((budget_day, limit, now))
        return self.budget

    def save_analysis_result(
        self, observation_id, lease_token, analysis, attempts, *, processing_state, now, error=None
    ):
        self.saved.append((observation_id, lease_token, analysis, attempts, processing_state, error))


def _item(region: str = "JP", importance: int = 3) -> AnalysisWorkItem:
    source = observation("analysis", "Policy update")
    source = replace(
        source,
        attributes={
            **source.attributes,
            "region": region,
            "importance": importance,
            "relevance": 2,
        },
    )
    return AnalysisWorkItem(17, source, "lease-17", 1788652800)


class AnalysisOrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_region_policy_and_shadow_results_are_persisted(self):
        repository = _Repository(_item())
        local = _Analyzer("local", {
            "importance": 5,
            "urgency": 5,
            "relevance": 5,
            "confidence": 0.9,
            "region": "JP",
            "topic": "geopolitics",
            "rationale": "model opinion",
        })
        config = AnalysisConfig(enabled=True, local_enabled=True, local_model="tiny", shadow_mode=True)
        processed = await AnalysisOrchestrator(config, repository, local_analyzer=local).process_once(
            1788652800
        )
        self.assertEqual(1, processed)
        _, _, result, attempts, state, error = repository.saved[0]
        self.assertEqual(4, result.relevance)
        self.assertEqual(3, result.importance)
        self.assertEqual("analyzed", state)
        self.assertIsNone(error)
        self.assertEqual(["deterministic", "local"], [attempt.analyzer for attempt in attempts])
        self.assertTrue(attempts[1].shadow)

    async def test_non_shadow_advisory_is_conservatively_merged(self):
        repository = _Repository(_item())
        local = _Analyzer("local", {
            "importance": 5,
            "urgency": 5,
            "relevance": 5,
            "confidence": 0.9,
            "region": "JP",
            "topic": "policy",
            "rationale": "material policy change",
        })
        config = AnalysisConfig(enabled=True, local_enabled=True, local_model="tiny", shadow_mode=False)
        await AnalysisOrchestrator(config, repository, local_analyzer=local).process_once(1788652800)
        result = repository.saved[0][2]
        self.assertEqual(4, result.importance)
        self.assertNotEqual("immediate", result.handling)

    async def test_api_is_budgeted_and_only_used_for_high_interest_digest(self):
        repository = _Repository(_item(region="CN"), budget=False)
        api = _Analyzer("api", {})
        config = AnalysisConfig(
            enabled=True,
            api_enabled=True, api_triage_enabled=True,
            api_base_url="https://api.example.test/v1",
            api_model="small",
            api_key_env="API_TOKEN",
            daily_api_budget=2,
        )
        await AnalysisOrchestrator(config, repository, api_analyzer=api).process_once(1788652800)
        self.assertEqual(0, api.calls)
        self.assertEqual(1, len(repository.reservations))
        attempts = repository.saved[0][3]
        self.assertEqual("budget_exhausted", attempts[-1].outcome)

    async def test_api_success_is_merged_and_historical_backlog_is_skipped(self):
        advisory = {
            "importance": 5,
            "urgency": 3,
            "relevance": 5,
            "confidence": 0.7,
            "region": "CN",
            "topic": "policy",
            "rationale": "official policy change",
        }
        config = AnalysisConfig(
            enabled=True,
            api_enabled=True, api_triage_enabled=True,
            api_base_url="https://api.example.test/v1",
            api_model="small",
            api_key_env="API_TOKEN",
            shadow_mode=False,
        )
        repository = _Repository(_item(region="CN"))
        api = _Analyzer("api", advisory)
        await AnalysisOrchestrator(config, repository, api_analyzer=api).process_once(1788652800)
        self.assertEqual(1, api.calls)
        self.assertEqual(4, repository.saved[0][2].importance)

        historical = _item(region="CN")
        historical = replace(historical, fetched_at=1788652800 - 86401)
        old_repository = _Repository(historical)
        old_api = _Analyzer("api", advisory)
        await AnalysisOrchestrator(
            config, old_repository, api_analyzer=old_api
        ).process_once(1788652800)
        self.assertEqual(0, old_api.calls)
        self.assertEqual([], old_repository.reservations)

    async def test_model_failure_falls_back_to_deterministic_result(self):
        repository = _Repository(_item())
        local = _Analyzer("local", error=RuntimeError("secret token=do-not-store"))
        config = AnalysisConfig(enabled=True, local_enabled=True, local_model="tiny")
        await AnalysisOrchestrator(config, repository, local_analyzer=local).process_once(1788652800)
        _, _, result, attempts, state, error = repository.saved[0]
        self.assertEqual(3, result.importance)
        self.assertEqual("degraded", state)
        self.assertEqual("failed", attempts[-1].outcome)
        self.assertNotIn("do-not-store", error)

    async def test_disabled_pipeline_does_not_claim_work(self):
        repository = _Repository(_item())
        processed = await AnalysisOrchestrator(AnalysisConfig(), repository).process_once()
        self.assertEqual(0, processed)
        self.assertFalse(repository.claimed)


class AnalysisCompositionTests(unittest.TestCase):
    def test_cli_uses_database_selected_prompt(self):
        with TemporaryDirectory() as directory:
            config = production_config(Path(directory))
            config = replace(
                config,
                ntfy=replace(config.ntfy, enabled=False),
                analysis=replace(
                    config.analysis,
                    enabled=True,
                    local_enabled=True,
                    local_model="tiny",
                ),
            )
            database = Database(config.service.database_path)
            try:
                database.save_prompt("triage", 1, "Database-selected prompt", "test", 1)
                service = _build_service(config, database)
                assert service.analysis_orchestrator is not None
                analyzer = service.analysis_orchestrator.local_analyzer
                self.assertIsNotNone(analyzer)
                self.assertEqual("Database-selected prompt", analyzer.prompt.system_text)
            finally:
                database.close()

    def test_cli_fails_closed_when_prompt_version_is_missing(self):
        with TemporaryDirectory() as directory:
            config = production_config(Path(directory))
            config = replace(
                config,
                ntfy=replace(config.ntfy, enabled=False),
                analysis=replace(config.analysis, enabled=True, prompt_version=999),
            )
            database = Database(config.service.database_path)
            try:
                with self.assertRaises(ConfigError):
                    _build_service(config, database)
            finally:
                database.close()


if __name__ == "__main__":
    unittest.main()
