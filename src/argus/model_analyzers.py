"""Optional local and OpenAI-compatible semantic analyzers.

These adapters are deliberately side-effect free from the service's point of
view. They return advisory JSON only; :mod:`argus.analysis` remains the
authority for routing and notification escalation.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from os import environ
from typing import Any, Mapping
from urllib.parse import urlsplit

from .models import Observation
from .prompts import PromptTemplate, get_prompt
from .util import truncate


class AnalyzerError(RuntimeError):
    """A semantic analyzer failed; callers should use deterministic fallback."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


@dataclass(frozen=True, slots=True)
class AnalyzerSettings:
    base_url: str
    model: str
    timeout_seconds: int = 20
    max_input_chars: int = 12000
    max_response_bytes: int = 65536
    max_tokens: int = 300
    prompt_id: str = "triage"
    prompt_version: int = 1

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if parsed.username or parsed.password or not parsed.hostname:
            raise ValueError("analyzer base URL must not contain credentials")
        if parsed.scheme not in {"https", "http"}:
            raise ValueError("analyzer base URL must use HTTP(S)")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("plain HTTP analyzers must be loopback-only")
        if not self.model or len(self.model) > 128:
            raise ValueError("analyzer model is invalid")
        if not 1 <= self.timeout_seconds <= 120:
            raise ValueError("analyzer timeout is out of range")
        if not 512 <= self.max_input_chars <= 100000:
            raise ValueError("analyzer input limit is out of range")
        if not 1024 <= self.max_response_bytes <= 4 * 1024 * 1024:
            raise ValueError("analyzer response limit is out of range")
        if not 1 <= self.max_tokens <= 4096:
            raise ValueError("analyzer token limit is out of range")
        if not self.prompt_id or len(self.prompt_id) > 64 or self.prompt_version < 1:
            raise ValueError("analyzer prompt reference is invalid")


def _observation_text(observation: Observation, limit: int) -> str:
    payload = {
        "title": observation.title,
        "summary": observation.summary,
        "publisher": observation.publisher,
        "region": observation.region,
        "topic": observation.topic,
        "source_tier": observation.source_tier,
        "information_type": observation.information_type,
        "url": observation.url,
    }
    # Article text is untrusted input. Delimit it clearly so it cannot masquerade
    # as instructions to the analyzer.
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return truncate(encoded, limit)


def _parse_advisory(payload: bytes, max_bytes: int) -> Mapping[str, Any]:
    if len(payload) > max_bytes:
        raise AnalyzerError("analyzer response exceeded the configured limit")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnalyzerError("analyzer returned invalid JSON") from exc
    if not isinstance(decoded, Mapping):
        raise AnalyzerError("analyzer response must be a JSON object")
    for name in ("importance", "urgency", "relevance"):
        value = decoded.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
            raise AnalyzerError(f"analyzer field {name} is invalid")
    confidence = decoded.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise AnalyzerError("analyzer field confidence is invalid")
    region = decoded.get("region")
    if region is not None and (
        not isinstance(region, str)
        or region.upper() not in {"CN", "JP", "US", "GLOBAL", "OTHER"}
    ):
        raise AnalyzerError("analyzer field region is invalid")
    for name in ("topic", "rationale"):
        value = decoded.get(name)
        if value is not None and (not isinstance(value, str) or len(value) > 500):
            raise AnalyzerError(f"analyzer field {name} is invalid")
    return dict(decoded)


class OpenAICompatibleAnalyzer:
    """JSON analyzer for OpenAI-compatible remote or local endpoints."""

    name = "api"

    def __init__(
        self,
        settings: AnalyzerSettings,
        *,
        api_key_env: str | None = None,
        prompt: PromptTemplate | None = None,
    ) -> None:
        self.settings = settings
        self.api_key_env = api_key_env
        self.prompt = prompt
        self._opener = urllib.request.build_opener(
            _NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )
        if api_key_env is not None and (not api_key_env.isidentifier() or api_key_env.upper() != api_key_env):
            raise ValueError("analyzer credential environment variable is invalid")

    def analyze(self, observation: Observation) -> Mapping[str, Any]:
        endpoint = self.settings.base_url.rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key_env:
            token = environ.get(self.api_key_env, "")
            if not token:
                raise AnalyzerError("analyzer credential is not configured")
            headers["Authorization"] = f"Bearer {token}"
        request_body = json.dumps({
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": (self.prompt or get_prompt(self.settings.prompt_id, self.settings.prompt_version)).system_text},
                {"role": "user", "content": _observation_text(observation, self.settings.max_input_chars)},
            ],
            "temperature": 0,
            "max_tokens": self.settings.max_tokens,
        }, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(endpoint, data=request_body, headers=headers, method="POST")
        try:
            with self._opener.open(request, timeout=self.settings.timeout_seconds) as response:
                raw = response.read(self.settings.max_response_bytes + 1)
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise AnalyzerError(f"analyzer request failed: {type(exc).__name__}") from exc
        try:
            envelope = json.loads(raw.decode("utf-8"))
            content = envelope["choices"][0]["message"]["content"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise AnalyzerError("analyzer response has an invalid completion shape") from exc
        if not isinstance(content, str):
            raise AnalyzerError("analyzer completion is not text")
        return _parse_advisory(content.encode("utf-8"), self.settings.max_response_bytes)


class LocalModelAnalyzer(OpenAICompatibleAnalyzer):
    """Loopback-only local model adapter (for Ollama/vLLM-compatible APIs)."""

    name = "local"

    def __init__(self, settings: AnalyzerSettings, *, prompt: PromptTemplate | None = None) -> None:
        parsed = urlsplit(settings.base_url)
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("local analyzer must use a loopback host")
        super().__init__(settings, prompt=prompt)
