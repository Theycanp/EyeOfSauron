from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

from argus.model_analyzers import AnalyzerError, AnalyzerSettings, FailoverAnalyzer, LocalModelAnalyzer, OpenAICompatibleAnalyzer
from argus.prompts import PromptTemplate
from tests.helpers import observation


class _Response:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, _limit: int) -> bytes:
        return self.payload


def _completion(content: object) -> bytes:
    return json.dumps({"choices": [{"message": {"content": content}}]}).encode()


class ModelAnalyzerTests(unittest.TestCase):
    def setUp(self):
        self.settings = AnalyzerSettings("https://api.example.test/v1", "small-model")

    @patch("argus.model_analyzers.urllib.request.build_opener")
    def test_parses_structured_completion(self, build_opener):
        build_opener.return_value.open.return_value = _Response(_completion(json.dumps({
            "importance": 4, "urgency": 2, "relevance": 5,
            "confidence": 0.8, "region": "JP", "topic": "policy",
            "rationale": "official release",
        })))
        result = OpenAICompatibleAnalyzer(self.settings).analyze(observation("one", "headline"))
        self.assertEqual("JP", result["region"])
        self.assertEqual(4, result["importance"])
        self.assertEqual("_NoRedirect", type(build_opener.call_args.args[0]).__name__)

    @patch("argus.model_analyzers.urllib.request.build_opener")
    def test_invalid_model_json_fails_closed(self, build_opener):
        build_opener.return_value.open.return_value = _Response(_completion("not json"))
        with self.assertRaises(AnalyzerError):
            OpenAICompatibleAnalyzer(self.settings).analyze(observation("one", "headline"))

    @patch("argus.model_analyzers.urllib.request.build_opener")
    def test_uses_injected_versioned_prompt(self, build_opener):
        build_opener.return_value.open.return_value = _Response(_completion(json.dumps({
            "importance": 3, "urgency": 2, "relevance": 3, "confidence": 0.7,
        })))
        prompt = PromptTemplate("triage", 7, "configured prompt revision seven")
        OpenAICompatibleAnalyzer(self.settings, prompt=prompt).analyze(
            observation("one", "headline")
        )
        request = build_opener.return_value.open.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual("configured prompt revision seven", body["messages"][0]["content"])

    def test_local_analyzer_rejects_non_loopback(self):
        with self.assertRaises(ValueError):
            LocalModelAnalyzer(AnalyzerSettings("https://model.example.test", "small-model"))

    @patch("argus.model_analyzers.urllib.request.build_opener")
    def test_rejects_unknown_region_from_model(self, build_opener):
        build_opener.return_value.open.return_value = _Response(_completion(json.dumps({
            "importance": 3, "urgency": 2, "relevance": 3,
            "confidence": 0.8, "region": "instructions-from-article",
        })))
        with self.assertRaisesRegex(AnalyzerError, "region"):
            OpenAICompatibleAnalyzer(self.settings).analyze(observation("one", "headline"))

    def test_failover_uses_next_model_after_failure(self):
        first = OpenAICompatibleAnalyzer(AnalyzerSettings("https://api.example.test/v1", "first"))
        second = OpenAICompatibleAnalyzer(AnalyzerSettings("https://api.example.test/v1", "second"))
        first.analyze = Mock(side_effect=AnalyzerError("unavailable"))  # type: ignore[method-assign]
        second.analyze = Mock(return_value={"importance": 3, "urgency": 2, "relevance": 3, "confidence": 0.8})  # type: ignore[method-assign]
        result = FailoverAnalyzer((first, second)).analyze(observation("one", "headline"))
        self.assertEqual(3, result["importance"])
        first.analyze.assert_called_once()
        second.analyze.assert_called_once()

    def test_failover_reports_all_models_failed_without_secrets(self):
        first = OpenAICompatibleAnalyzer(AnalyzerSettings("https://api.example.test/v1", "first"))
        second = OpenAICompatibleAnalyzer(AnalyzerSettings("https://api.example.test/v1", "second"))
        first.analyze = Mock(side_effect=AnalyzerError("provider unavailable"))  # type: ignore[method-assign]
        second.analyze = Mock(side_effect=TimeoutError())  # type: ignore[method-assign]
        with self.assertRaisesRegex(AnalyzerError, "first:AnalyzerError, second:TimeoutError"):
            FailoverAnalyzer((first, second)).analyze(observation("one", "headline"))
