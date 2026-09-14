"""Model-assisted digest synthesis over bounded, already selected evidence."""
from __future__ import annotations

import json
import re
from typing import Protocol, Sequence

from .digest import DigestDocument
from .model_analyzers import AnalyzerError, OpenAICompatibleAnalyzer


class DigestContentRepository(Protocol):
    def get_analysis_text(self, observation_id: int, *, max_chars: int) -> str: ...


class ApiDigestSummarizer:
    """Transport adapter; neither model output nor input can change routing."""

    def __init__(self, clients: OpenAICompatibleAnalyzer | Sequence[OpenAICompatibleAnalyzer]) -> None:
        if hasattr(clients, "analyzers"):
            self.clients = tuple(clients.analyzers)  # type: ignore[attr-defined]
        elif hasattr(clients, "complete") and hasattr(clients, "settings"):
            self.clients = (clients,)  # type: ignore[assignment]
        else:
            self.clients = tuple(clients)
        if not self.clients:
            raise ValueError("at least one digest analyzer is required")

    def summarize(self, digest: DigestDocument) -> str:
        if len(self.clients) == 1:
            return self._summarize_with(self.clients[0], digest)
        failures: list[str] = []
        for client in self.clients:
            try:
                return self._summarize_with(client, digest)
            except Exception as exc:
                failures.append(f"{client.settings.model}:{type(exc).__name__}")
        raise AnalyzerError("all configured digest analyzers failed: " + ", ".join(failures))

    @staticmethod
    def _summarize_with(client: OpenAICompatibleAnalyzer, digest: DigestDocument) -> str:
        limit = client.settings.max_input_chars
        # Distribute the input budget across all selected topics, rather than
        # truncate a JSON string halfway through (or omit its last sources).
        per_item = max(40, (limit - 1000) // max(1, len(digest.items)) - 200)
        items = [{"id": index, "title": item.title[:min(300, per_item // 2)],
                  "summary": item.summary[:per_item // 2], "regions": item.regions}
                 for index, item in enumerate(digest.items, 1)]
        payload = json.dumps({"items": items}, ensure_ascii=False)
        if len(payload) > limit:
            raise AnalyzerError("digest input budget is too small for all selected topics")
        raw = client.complete(payload)
        try:
            result = json.loads(raw)
        except ValueError as exc:
            raise AnalyzerError("digest response is not JSON") from exc
        if not isinstance(result, dict):
            raise AnalyzerError("digest response must be an object")
        summary, citations = result.get("summary"), result.get("citations")
        if not isinstance(summary, str) or not 40 <= len(summary.strip()) <= 16000:
            raise AnalyzerError("digest summary length is invalid")
        if not isinstance(citations, list) or not citations or any(
            type(number) is not int or not 1 <= number <= len(items) for number in citations
        ):
            raise AnalyzerError("digest citations are invalid")
        references = {int(number) for number in re.findall(r"\[(\d+)\]", summary)}
        if references != set(citations):
            raise AnalyzerError("digest text references do not match its evidence")
        return (summary.strip() + "\n\nAI 辅助整理，请结合下方编号条目核对。"
                + f"模型：{client.settings.model}；Prompt：{client.settings.prompt_id}@{client.settings.prompt_version}。")
