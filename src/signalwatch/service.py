from __future__ import annotations

import asyncio
import fcntl
import logging
import os
from pathlib import Path

from .config import AppConfig
from .database import Database
from .models import FeedFetchResult
from .notifier import Notifier
from .rss import RssCollector
from .rules import RuleSet
from .util import now_epoch, sanitize_error


LOGGER = logging.getLogger("signalwatch")


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
            raise AlreadyRunningError("another SignalWatch process holds the service lock") from exc
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


class SignalWatchService:
    def __init__(
        self,
        config: AppConfig,
        database: Database,
        collectors: dict[str, object],
        rules: RuleSet,
        notifier: Notifier | None,
    ) -> None:
        self.config = config
        self.database = database
        self.collectors = collectors
        self.rules = rules
        self.notifier = notifier
        self.stop_event = asyncio.Event()

    async def poll_source_once(self, source_id: str) -> bool:
        collector = self.collectors[source_id]
        source_config = next(source for source in self.config.sources if source.id == source_id)
        state = self.database.get_source_state(source_id)
        now = now_epoch()
        result: FeedFetchResult | None = None
        last_error: Exception | None = None
        for attempt in range(1, source_config.request_attempts + 1):
            try:
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
            )
            LOGGER.warning(
                "source_poll_failed source=%s alert_queued=%s error=%s",
                source_id,
                alert_queued,
                sanitize_error(last_error),
            )
            return False

        report = self.database.record_source_success(
            source_id,
            result,
            self.rules,
            now,
            self.config.ntfy.default_topic,
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
            self.database.mark_retry(alert.id, now_epoch() + delay, exc)
            LOGGER.warning(
                "notification_failed alert_id=%d attempts=%d retry_seconds=%d error=%s",
                alert.id,
                alert.attempts,
                delay,
                sanitize_error(exc),
            )
            return False
        self.database.mark_delivered(alert.id, now_epoch())
        LOGGER.info("notification_delivered alert_id=%d topic=%s", alert.id, alert.topic)
        return True

    async def _source_loop(self, source_id: str, interval: int) -> None:
        while not self.stop_event.is_set():
            await self.poll_source_once(source_id)
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

    async def _maintenance_loop(self) -> None:
        while not self.stop_event.is_set():
            cutoff = now_epoch() - self.config.service.retention_days * 86400
            deleted_alerts, deleted_observations = self.database.cleanup(cutoff)
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

    async def run_forever(self) -> None:
        LOGGER.info("service_started sources=%d", len(self.collectors))
        try:
            async with asyncio.TaskGroup() as group:
                for source in self.config.sources:
                    if source.id in self.collectors:
                        group.create_task(
                            self._source_loop(source.id, source.poll_interval_seconds),
                            name=f"source:{source.id}",
                        )
                group.create_task(self._delivery_loop(), name="delivery")
                group.create_task(self._maintenance_loop(), name="maintenance")
                await self.stop_event.wait()
        finally:
            LOGGER.info("service_stopped")

    async def run_once(self, deliver: bool = False) -> None:
        await asyncio.gather(*(self.poll_source_once(source.id) for source in self.config.sources if source.id in self.collectors))
        if deliver:
            while await self.deliver_one():
                pass

    def request_stop(self) -> None:
        self.stop_event.set()
