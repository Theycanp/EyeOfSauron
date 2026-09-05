from __future__ import annotations

import sqlite3
from types import TracebackType
from typing import Any, Mapping, Protocol, runtime_checkable

from .models import FeedFetchResult, IngestReport, OutboxMessage, SourceState
from .reminders import ReminderSpec
from .rules import RuleSet


class RevisionConflictError(RuntimeError):
    """The managed configuration changed after a client read it."""


class SQLiteUnitOfWork:
    """Transaction boundary shared by SQLite repositories."""

    def __init__(self, connection: sqlite3.Connection, *, immediate: bool = True) -> None:
        self.connection = connection
        self.immediate = immediate
        self._finished = False

    def __enter__(self) -> "SQLiteUnitOfWork":
        self.connection.execute("BEGIN IMMEDIATE" if self.immediate else "BEGIN")
        return self

    def commit(self) -> None:
        if not self._finished:
            self.connection.commit()
            self._finished = True

    def rollback(self) -> None:
        if not self._finished:
            self.connection.rollback()
            self._finished = True

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()


@runtime_checkable
class RuntimeRepository(Protocol):
    """Persistence port used by the always-on application service."""

    def get_source_state(self, source_id: str) -> SourceState: ...

    def mark_source_runtime(
        self,
        source_id: str,
        status: str,
        now: int,
        error: BaseException | str | None = None,
    ) -> None: ...

    def sync_source_runtime(
        self,
        sources: list[tuple[str, str, bool, int]],
        active_source_ids: set[str],
        *,
        config_revision: int | None,
        now: int,
    ) -> None: ...

    def register_engine(
        self,
        instance_id: str,
        *,
        started_at: int,
        applied_revision: int | None,
        code_version: str,
        configured_sources: int,
        active_sources: int,
    ) -> None: ...

    def heartbeat_engine(self, instance_id: str, now: int, *, state: str = "running") -> bool: ...
    def stop_engine(self, instance_id: str, now: int, *, error: str | None = None) -> None: ...

    def record_source_success(
        self,
        source_id: str,
        result: FeedFetchResult,
        rules: RuleSet,
        now: int,
        default_topic: str,
        duration_ms: int | None = None,
    ) -> IngestReport: ...

    def record_source_failure(
        self,
        source_id: str,
        error: BaseException | str,
        threshold: int,
        default_topic: str,
        now: int,
        duration_ms: int | None = None,
        error_kind: str | None = None,
        http_status: int | None = None,
    ) -> bool: ...

    def enqueue_due_reminders(self, now: int, topic: str, limit: int = 100) -> int: ...
    def claim_due_alert(self, now: int, lease_seconds: int) -> OutboxMessage | None: ...
    def mark_delivered(self, alert_id: int, now: int) -> None: ...
    def mark_retry(self, alert_id: int, next_attempt_at: int, error: BaseException | str) -> None: ...
    def cleanup(
        self,
        cutoff: int,
        *,
        incident_cutoff: int | None = None,
        audit_cutoff: int | None = None,
        dead_cutoff: int | None = None,
    ) -> tuple[int, int]: ...
    def mark_delivery_failure(
        self,
        alert_id: int,
        now: int,
        next_attempt_at: int,
        error: BaseException | str,
        *,
        retryable: bool,
        failure_kind: str,
        max_attempts: int,
        max_age_seconds: int,
    ) -> str: ...

    def get_active_config_revision(self) -> dict[str, Any] | None: ...
    def claim_admin_job(self, kind: str, now: int) -> dict[str, Any] | None: ...
    def finish_admin_job(
        self,
        job_id: str,
        now: int,
        *,
        result: Mapping[str, Any] | None = None,
        error: BaseException | str | None = None,
    ) -> bool: ...


@runtime_checkable
class ManagedConfigRepository(Protocol):
    """Persistence port for immutable desired-configuration revisions."""

    def get_active_config_revision(self) -> dict[str, Any] | None: ...
    def get_config_revision(self, revision: int) -> dict[str, Any] | None: ...
    def record_config_revision(
        self,
        revision: int,
        payload: Mapping[str, Any],
        actor: str = "admin",
        reason: str = "configuration update",
        active: bool = True,
        *,
        only_if_empty: bool = False,
    ) -> int: ...
    def save_managed_config(
        self,
        payload: Mapping[str, Any],
        actor: str,
        reason: str,
        *,
        expected_revision: int | None = None,
    ) -> int: ...


@runtime_checkable
class ControlPlaneRepository(ManagedConfigRepository, Protocol):
    """Narrow persistence port used by the local administration API."""

    def status(self) -> dict[str, Any]: ...
    def metrics_prometheus(self) -> str: ...
    def list_config_revisions(self, limit: int = 50) -> list[dict[str, Any]]: ...
    def list_incidents(
        self, limit: int = 100, status: str | None = None
    ) -> list[dict[str, Any]]: ...
    def get_reminder(self, reminder_id: str) -> dict[str, Any] | None: ...
    def list_reminders(self, limit: int = 500) -> list[dict[str, Any]]: ...
    def upsert_reminder(
        self, reminder: ReminderSpec, actor: str, now: int
    ) -> dict[str, Any]: ...
    def set_reminder_enabled(
        self, reminder_id: str, enabled: bool, actor: str, now: int
    ) -> dict[str, Any]: ...
    def delete_reminder(self, reminder_id: str, actor: str, now: int) -> bool: ...
    def create_admin_job(
        self,
        kind: str,
        request: Mapping[str, Any],
        actor: str,
        now: int,
        *,
        ttl_seconds: int = 900,
    ) -> dict[str, Any]: ...
    def get_admin_job(self, job_id: str) -> dict[str, Any] | None: ...
    def cancel_admin_job(self, job_id: str, now: int) -> bool: ...
    def list_alerts(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
        before_id: int | None = None,
    ) -> list[dict[str, Any]]: ...
    def get_alert(self, alert_id: int) -> dict[str, Any] | None: ...
    def retry_alert(self, alert_id: int, now: int) -> bool: ...
    def cancel_alert(self, alert_id: int, now: int) -> bool: ...
    def discard_alert(self, alert_id: int) -> bool: ...
