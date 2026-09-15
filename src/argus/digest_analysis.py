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
        if len(digest.items) > 12:
            return ApiDigestSummarizer._summarize_large_digest(client, digest)
        return ApiDigestSummarizer._synthesize(client, digest, digest.items)

    @staticmethod
    def _summarize_large_digest(client: OpenAICompatibleAnalyzer, digest: DigestDocument) -> str:
        """Use a map/select pass before spending context on the final synthesis."""
        limit = client.settings.max_input_chars
        index_budget = max(12000, min(40000, limit // 2))
        per_item = max(80, (index_budget - 1000) // max(1, len(digest.items)) - 80)
        items = [{"id": index, "title": item.title[:min(300, per_item // 2)],
                  "summary": item.summary[:per_item // 2], "regions": item.regions}
                 for index, item in enumerate(digest.items, 1)]
        index_payload = json.dumps({
            "stage": "index",
            "instruction": "浏览全部主题，返回需要深入阅读的主题编号。只输出 JSON：{\"expand_topics\":[整数]}。优先选择重大变化、跨来源关联、官方公告、信源分歧和可能影响用户关注地区或市场的主题。最多选择 12 个。",
            "items": items,
        }, ensure_ascii=False)
        if len(index_payload) > limit:
            raise AnalyzerError("digest index input budget is too small for all selected topics")
        selected_ids: list[int] = []
        try:
            index_result = json.loads(client.complete(index_payload))
            raw_ids = index_result.get("expand_topics") if isinstance(index_result, dict) else None
            if isinstance(raw_ids, list):
                selected_ids = [number for number in raw_ids if type(number) is int and 1 <= number <= len(items)][:12]
        except (AnalyzerError, ValueError, TypeError):
            selected_ids = []
        if not selected_ids:
            selected_ids = list(range(1, min(12, len(items)) + 1))
        selected = tuple(
            item if not item.observation_ids else item
            for item in (digest.items[number - 1] for number in selected_ids)
        )
        return ApiDigestSummarizer._synthesize(client, digest, selected, index_ids=selected_ids)

    @staticmethod
    def _synthesize(
        client: OpenAICompatibleAnalyzer,
        digest: DigestDocument,
        evidence_items: Sequence,
        *,
        index_ids: Sequence[int] | None = None,
    ) -> str:
        limit = client.settings.max_input_chars
        per_item = max(80, (limit - 1200) // max(1, len(evidence_items)) - 120)
        evidence_ids = tuple(index_ids or range(1, len(evidence_items) + 1))
        if len(evidence_ids) != len(evidence_items):
            raise AnalyzerError("digest evidence identifiers are inconsistent")
        items = [{"id": evidence_id, "title": item.title[:min(300, per_item // 2)],
                  "summary": item.summary[:per_item], "regions": item.regions}
                 for evidence_id, item in zip(evidence_ids, evidence_items, strict=True)]
        payload = json.dumps({
            "stage": "synthesis",
            "instruction": "基于证据完成最终日报。输出 JSON：{\"summary\":\"中文摘要\",\"citations\":[1,2]}。摘要必须覆盖最重要变化、主题关联、影响、不确定性和后续观察；每个事实段落使用 [编号] 引用。",
            "selected_topic_ids": list(evidence_ids),
            "items": items,
        }, ensure_ascii=False)
        if len(payload) > limit:
            raise AnalyzerError("digest evidence input budget is too small")
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
            type(number) is not int or number not in evidence_ids for number in citations
        ):
            raise AnalyzerError("digest citations are invalid")
        references = {int(number) for number in re.findall(r"\[(\d+)\]", summary)}
        if references != set(citations):
            raise AnalyzerError("digest text references do not match its evidence")
        return (summary.strip() + "\n\nAI 辅助整理，请结合下方编号条目核对。"
                + f"模型：{client.settings.model}；Prompt：{client.settings.prompt_id}@{client.settings.prompt_version}。")
