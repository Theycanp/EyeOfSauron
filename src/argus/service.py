from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import random
import socket
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from . import __version__
from .adapters import build_collector
from .analysis_orchestrator import AnalysisOrchestrator
from .content import ContentFetchError, PublicDocumentFetcher
from .digest import DigestScheduler
from .config import AppConfig, parse_source_config
from .persistence import RuntimeRepository
from .models import FeedFetchResult, SourceState
from .notifier import Notifier, delivery_error_details
from .rules import RuleSet
from .util import now_epoch, sanitize_error


LOGGER = logging.getLogger("argus")


def _sd_notify(message: str) -> None:
    """Send a best-effort systemd readiness/watchdog datagram."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
            client.settimeout(1.0)
            client.connect(address)
            client.sendall(message.encode("utf-8"))
    except OSError as exc:
        LOGGER.debug("systemd_notify_failed error=%s", sanitize_error(exc))



class AlreadyRunningError(RuntimeError):
    pass


class ProcessLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> "ProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self.fd)
            self.fd = None
            raise AlreadyRunningError("another Argus process holds the service lock") from exc
        os.ftruncate(self.fd, 0)
        os.write(self.fd, f"{os.getpid()}\n".encode("ascii"))
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:  # type: ignore[no-untyped-def]
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


def retry_delay(attempts: int, maximum: int) -> int:
    exponent = min(max(attempts - 1, 0), 10)
    return min(maximum, 5 * (2**exponent))


class ArgusService:
    def __init__(
        self,
        config: AppConfig,
        database: RuntimeRepository,
        collectors: dict[str, object],
        rules: RuleSet,
        notifier: Notifier | None,
        config_revision: int | None = None,
        analysis_orchestrator: AnalysisOrchestrator | None = None,
        digest_scheduler: DigestScheduler | None = None,
        content_fetcher: PublicDocumentFetcher | None = None,
    ) -> None:
        self.config = config
        self.database = database
        self.collectors = collectors
        self.rules = rules
        self.notifier = notifier
        self.stop_event = asyncio.Event()
        self.config_revision = config_revision
        self.analysis_orchestrator = analysis_orchestrator
        self.digest_scheduler = digest_scheduler
        self.content_fetcher = content_fetcher
        self.instance_id = uuid.uuid4().hex
        self._source_slots = asyncio.Semaphore(config.service.max_source_concurrency)
        self._jitter = random.SystemRandom()
        self.reload_requested = False

    async def poll_source_once(self, source_id: str) -> bool:
        collector = self.collectors[source_id]
        source_config = next(source for source in self.config.sources if source.id == source_id)
        state = self.database.get_source_state(source_id)
        now = now_epoch()
        started = time.monotonic()
        self.database.mark_source_runtime(
            source_id, "degraded" if state.consecutive_failures else "active", now
        )
        result: FeedFetchResult | None = None
        last_error: Exception | None = None
        for attempt in range(1, source_config.request_attempts + 1):
            try:
                async with self._source_slots:
                    result = await asyncio.to_thread(collector.fetch, state)  # type: ignore[attr-defined]
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt < source_config.request_attempts:
                    delay = source_config.retry_base_seconds * (2 ** (attempt - 1))
                    LOGGER.debug(
                        "source_poll_retry source=%s attempt=%d delay_seconds=%d error=%s",
                        source_id,
                        attempt,
                        delay,
                        sanitize_error(exc),
                    )
                    await asyncio.sleep(delay)

        if result is None:
            assert last_error is not None
            alert_queued = self.database.record_source_failure(
                source_id,
                last_error,
                self.config.service.source_failure_alert_after,
                self.config.ntfy.default_topic,
                now,
                duration_ms=int((time.monotonic() - started) * 1000),
                error_kind=type(last_error).__name__,
            )
            self.database.mark_source_runtime(source_id, "degraded", now_epoch(), last_error)
            LOGGER.warning(
                "source_poll_failed source=%s alert_queued=%s error=%s",
                source_id,
                alert_queued,
                sanitize_error(last_error),
            )
            return False

        # Attach source provenance once at the service boundary so every
        # collector (RSS, market, mail, and future adapters) shares one
        # information-classification contract.
        annotated = tuple(
            replace(
                item,
                attributes={
                    "region": source_config.region,
                    "source_tier": source_config.source_tier,
                    "importance": source_config.default_importance,
                    "topic": str(source_config.settings.get("topic", item.topic)),
                    **item.attributes,
                },
            )
            for item in result.observations
        )
        result = replace(result, observations=annotated)
        report = self.database.record_source_success(
            source_id,
            result,
            self.rules,
            now,
            self.config.ntfy.default_topic,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

        self.database.mark_source_runtime(
            source_id,
            "degraded" if result.warnings else "active",
            now_epoch(),
            "; ".join(result.warnings) if result.warnings else None,
        )
        if result.warnings:
            LOGGER.warning(
                "source_poll_degraded source=%s warnings=%s",
                source_id,
                ",".join(result.warnings),
            )

        if report.baseline_created:
            LOGGER.info(
                "source_baseline_created source=%s observations=%d",
                source_id,
                report.inserted_observations,
            )
        elif report.inserted_observations or report.queued_alerts:
            LOGGER.info(
                "source_updated source=%s observations=%d alerts=%d",
                source_id,
                report.inserted_observations,
                report.queued_alerts,
            )
        else:
            LOGGER.debug("source_unchanged source=%s", source_id)
        return True

    async def deliver_one(self) -> bool:
        if self.notifier is None:
            return False
        now = now_epoch()
        alert = self.database.claim_due_alert(now, self.config.service.delivery_lease_seconds)
        if alert is None:
            return False
        try:
            await asyncio.to_thread(self.notifier.publish, alert)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            delay = retry_delay(alert.attempts, self.config.service.max_delivery_retry_seconds)
            failed_at = now_epoch()
            retryable, failure_kind = delivery_error_details(exc)
            state = self.database.mark_delivery_failure(
                alert.id,
                failed_at,
                failed_at + delay,
                exc,
                retryable=retryable,
                failure_kind=failure_kind,
                max_attempts=self.config.service.max_delivery_attempts,
                max_age_seconds=self.config.service.max_delivery_age_seconds,
            )
            log = LOGGER.error if state == "dead" else LOGGER.warning
            log(
                "notification_failed alert_id=%d attempts=%d state=%s retry_seconds=%d kind=%s error=%s",
                alert.id, alert.attempts, state, delay, failure_kind, sanitize_error(exc),
            )
            return False
        self.database.mark_delivered(alert.id, now_epoch())
        LOGGER.info("notification_delivered alert_id=%d topic=%s attempts=%d", alert.id, alert.topic, alert.attempts)
        return True

    def process_reminders_once(self) -> int:
        now = now_epoch()
        queued = self.database.enqueue_due_reminders(now, self.config.ntfy.default_topic)
        if queued:
            LOGGER.info("reminders_queued count=%d", queued)
        return queued

    async def process_content_fetch_once(self) -> bool:
        if self.content_fetcher is None:
            return False
        item = self.database.claim_content_fetch(now_epoch(), lease_seconds=180)
        if item is None:
            return False
        try:
            document = await asyncio.to_thread(self.content_fetcher.fetch, item)
        except asyncio.CancelledError:
            raise
        except ContentFetchError as exc:
            failed_at = now_epoch()
            state = self.database.fail_content_fetch(
                item,
                exc,
                failed_at,
                failed_at + retry_delay(item.attempts, 3600),
                retryable=exc.retryable,
                failure_kind=exc.kind,
                max_attempts=5,
            )
            log = LOGGER.error if state == "dead" else LOGGER.warning
            log(
                "content_fetch_failed job_id=%d observation_id=%d attempts=%d state=%s "
                "kind=%s error=%s",
                item.id,
                item.observation_id,
                item.attempts,
                state,
                exc.kind,
                sanitize_error(exc),
            )
            return True
        except Exception as exc:
            failed_at = now_epoch()
            state = self.database.fail_content_fetch(
                item,
                exc,
                failed_at,
                failed_at + retry_delay(item.attempts, 3600),
                retryable=True,
                failure_kind=type(exc).__name__.lower(),
                max_attempts=5,
            )
            LOGGER.warning(
                "content_fetch_failed job_id=%d observation_id=%d attempts=%d state=%s "
                "kind=unexpected error=%s",
                item.id,
                item.observation_id,
                item.attempts,
                state,
                sanitize_error(exc),
            )
            return True
        if self.database.complete_content_fetch(item, document, now_epoch()):
            LOGGER.info(
                "content_fetch_completed job_id=%d observation_id=%d level=%s",
                item.id,
                item.observation_id,
                document.level.value,
            )
        else:
            LOGGER.warning(
                "content_fetch_lease_lost job_id=%d observation_id=%d",
                item.id,
                item.observation_id,
            )
        return True

    async def _source_loop(
        self, source_id: str, interval: int, initial_delay: float = 0.0
    ) -> None:
        if initial_delay > 0:
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=initial_delay)
            except TimeoutError:
                pass
        while not self.stop_event.is_set():
            try:
                await self.poll_source_once(source_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.database.record_source_failure(
                    source_id, exc, self.config.service.source_failure_alert_after,
                    self.config.ntfy.default_topic, now_epoch(),
                    error_kind=type(exc).__name__,
                )
                self.database.mark_source_runtime(source_id, "degraded", now_epoch(), exc)
                LOGGER.error(
                    "source_supervisor_caught source=%s error=%s",
                    source_id,
                    sanitize_error(exc),
                )
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=interval)
            except TimeoutError:
                pass

    async def _delivery_loop(self) -> None:
        while not self.stop_event.is_set():
            delivered = await self.deliver_one()
            if delivered:
                continue
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=1.0)
            except TimeoutError:
                pass

    async def _reminder_loop(self) -> None:
        while not self.stop_event.is_set():
            self.process_reminders_once()
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=1.0)
            except TimeoutError:
                pass

    async def _content_fetch_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                processed = await self.process_content_fetch_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.error("content_fetch_worker_failed error=%s", sanitize_error(exc))
                processed = False
            if processed:
                continue
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=2.0)
            except TimeoutError:
                pass

    async def _analysis_loop(self) -> None:
        assert self.analysis_orchestrator is not None
        while not self.stop_event.is_set():
            processed = await self.analysis_orchestrator.process_once()
            if processed:
                LOGGER.info("analysis_batch_processed count=%d", processed)
                continue
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=2.0)
            except TimeoutError:
                pass

    async def _digest_loop(self) -> None:
        assert self.digest_scheduler is not None
        while not self.stop_event.is_set():
            try:
                published = await self.digest_scheduler.process_once_async(now_epoch())
                if published is not None:
                    LOGGER.debug(
                        "digest_checked key=%s version=%d", published.digest_key, published.version
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.error("digest_generation_failed error=%s", sanitize_error(exc))
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=60.0)
            except TimeoutError:
                pass

    async def _maintenance_loop(self) -> None:
        while not self.stop_event.is_set():
            now = now_epoch()
            cutoff = now - self.config.service.retention_days * 86400
            deleted_alerts, deleted_observations = self.database.cleanup(
                cutoff,
                incident_cutoff=now - self.config.service.incident_retention_days * 86400,
                audit_cutoff=now - self.config.service.audit_retention_days * 86400,
                dead_cutoff=now - self.config.service.dead_letter_retention_days * 86400,
            )
            if deleted_alerts or deleted_observations:
                LOGGER.info(
                    "retention_cleanup alerts=%d observations=%d",
                    deleted_alerts,
                    deleted_observations,
                )
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=86400)
            except TimeoutError:
                pass

    async def _heartbeat_loop(self) -> None:
        interval = self.config.service.heartbeat_interval_seconds
        while not self.stop_event.is_set():
            now = now_epoch()
            state = "running" if len(self.collectors) == sum(s.enabled for s in self.config.sources) else "degraded"
            self.database.heartbeat_engine(self.instance_id, now, state=state)
            _sd_notify(f"WATCHDOG=1\nSTATUS=Argus running; heartbeat={now}")
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=interval)
            except TimeoutError:
                pass

    async def _config_revision_loop(self) -> None:
        """Request a controlled process restart when the desired revision changes."""
        while not self.stop_event.is_set():
            active = self.database.get_active_config_revision()
            desired_revision = int(active["revision"]) if active is not None else None
            if desired_revision != self.config_revision:
                self.reload_requested = True
                LOGGER.info(
                    "configuration_reload_requested applied_revision=%s desired_revision=%s",
                    self.config_revision,
                    desired_revision,
                )
                _sd_notify(
                    "RELOADING=1\n"
                    f"STATUS=Argus is applying configuration revision {desired_revision}"
                )
                self.stop_event.set()
                return
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=5.0)
            except TimeoutError:
                pass

    async def _admin_job_loop(self) -> None:
        while not self.stop_event.is_set():
            job = self.database.claim_admin_job("test_source", now_epoch())
            if job is None:
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=1.0)
                except TimeoutError:
                    pass
                continue
            job_id = str(job["id"])
            try:
                request = job.get("request")
                if not isinstance(request, Mapping) or not isinstance(request.get("source"), Mapping):
                    raise ValueError("test-source job requires a source object")
                raw_source: dict[str, Any] = dict(request["source"])
                raw_source["enabled"] = True
                source = parse_source_config(raw_source)
                settings = dict(source.settings)
                if source.kind == "imap":
                    settings["batch_size"] = 1
                elif source.kind == "x":
                    settings["max_pages_per_poll"] = 1
                elif source.kind == "market":
                    settings["symbols"] = settings["symbols"][:1]
                source = replace(source, settings=settings, request_timeout_seconds=min(15, source.request_timeout_seconds))
                collector = build_collector(source)
                if collector is None:
                    raise RuntimeError("source has no active collector")
                started = time.monotonic()
                result = await asyncio.to_thread(
                    collector.fetch,
                    SourceState(source.id, False, None, None, None, None, 0, False, None),
                )
                self.database.finish_admin_job(
                    job_id,
                    now_epoch(),
                    result={
                        "observations": len(result.observations),
                        "elapsed_ms": int((time.monotonic() - started) * 1000),
                        "not_modified": result.not_modified,
                        "warnings": list(result.warnings),
                    },
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.database.finish_admin_job(job_id, now_epoch(), error=exc)
                LOGGER.warning(
                    "admin_job_failed job_id=%s kind=test_source error=%s",
                    job_id,
                    sanitize_error(exc),
                )

    async def run_forever(self) -> bool:
        enabled_sources = [source for source in self.config.sources if source.enabled]
        runtime_rows = [
            (source.id, source.kind, source.enabled, source.poll_interval_seconds)
            for source in self.config.sources
        ]
        self.database.sync_source_runtime(
            runtime_rows,
            set(self.collectors),
            config_revision=self.config_revision,
            now=now_epoch(),
        )
        self.database.register_engine(
            self.instance_id,
            started_at=now_epoch(),
            applied_revision=self.config_revision,
            code_version=__version__,
            configured_sources=len(enabled_sources),
            active_sources=len(self.collectors),
        )
        _sd_notify("READY=1\nSTATUS=Argus watcher engine is ready")
        LOGGER.info(
            "service_started sources=%d revision=%s instance=%s",
            len(self.collectors),
            self.config_revision,
            self.instance_id,
        )
        terminal_error: BaseException | None = None
        try:
            async with asyncio.TaskGroup() as group:
                for source in self.config.sources:
                    if source.id in self.collectors:
                        jitter = self._jitter.uniform(
                            0, self.config.service.source_start_jitter_seconds
                        )
                        group.create_task(
                            self._source_loop(source.id, source.poll_interval_seconds, jitter),
                            name=f"source:{source.id}",
                        )
                group.create_task(self._delivery_loop(), name="delivery")
                group.create_task(self._reminder_loop(), name="reminders")
                if self.content_fetcher is not None:
                    group.create_task(self._content_fetch_loop(), name="content-fetch")
                if self.analysis_orchestrator is not None:
                    group.create_task(self._analysis_loop(), name="analysis")
                if self.digest_scheduler is not None:
                    group.create_task(self._digest_loop(), name="digest")
                group.create_task(self._maintenance_loop(), name="maintenance")
                group.create_task(self._heartbeat_loop(), name="heartbeat")
                group.create_task(self._admin_job_loop(), name="admin-jobs")
                group.create_task(self._config_revision_loop(), name="config-revision")
                await self.stop_event.wait()
        except BaseException as exc:
            terminal_error = exc
            raise
        finally:
            _sd_notify("STOPPING=1\nSTATUS=Argus watcher engine is stopping")
            self.database.stop_engine(
                self.instance_id,
                now_epoch(),
                error=sanitize_error(terminal_error) if terminal_error is not None else None,
            )
            LOGGER.info("service_stopped")
        return self.reload_requested

    async def run_once(self, deliver: bool = False) -> None:
        await asyncio.gather(*(self.poll_source_once(source.id) for source in self.config.sources if source.id in self.collectors))
        if self.analysis_orchestrator is not None:
            await self.analysis_orchestrator.process_once()
        while await self.process_content_fetch_once():
            pass
        if self.digest_scheduler is not None:
            await self.digest_scheduler.process_once_async(now_epoch())
        self.process_reminders_once()
        if deliver:
            while await self.deliver_one():
                pass

    def request_stop(self) -> None:
        self.stop_event.set()
