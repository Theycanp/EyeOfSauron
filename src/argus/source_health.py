"""Read-only source diagnostics, separate from long-horizon quality ranking."""

from __future__ import annotations

from typing import Any, Protocol


class SourceHealthRepository(Protocol):
    def get_source_health(
        self, source_id: str, *, now: int | None = None
    ) -> dict[str, Any] | None:
        """Return counters and retained evidence; do not infer missing poll history.

        Poll counters are cumulative. Output and content coverage use the last
        seven days of *ingested*, deduplicated observations still retained in
        storage, not seven days of publication or polling history. Reading must
        not create state or change quality weights.
        """
        ...
