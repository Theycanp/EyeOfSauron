"""Digest run diagnostics independent of storage and model transports."""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence, runtime_checkable


@runtime_checkable
class DigestRunRepository(Protocol):
    def start_digest_attempt(self, digest_key: str, *, now: int) -> int: ...

    def finish_digest_attempt(
        self, attempt_id: int, *, status: str, now: int,
        error: str | None = None, providers: Sequence[Mapping[str, Any]] = (),
    ) -> None: ...

    def get_digest_run(self, digest_key: str, *, now: int) -> dict[str, Any] | None: ...

    def list_digest_runs(self, *, now: int, limit: int = 30) -> list[dict[str, Any]]: ...

    def request_digest_retry_now(
        self, digest_key: str, *, actor: str, request_id: str, now: int
    ) -> dict[str, Any]: ...


def digest_run_state(
    *, generation_kind: str | None, retry: Mapping[str, Any] | None,
    latest_attempt: Mapping[str, Any] | None, now: int,
) -> str:
    """Derive display state; never maintain a second publication state machine."""
    if generation_kind == "api":
        return "ai_published"
    if latest_attempt and latest_attempt.get("status") == "running":
        return "generating"
    if retry:
        if retry["status"] == "failed" or now > int(retry["retry_deadline_at"]):
            return "retry_exhausted"
        if retry["status"] == "pending":
            return "ai_retrying"
    return "algorithm_published" if generation_kind == "algorithm" else "unpublished"
