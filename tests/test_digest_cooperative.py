from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from argus.config import DigestConfig
from argus.database import Database
from argus.digest import DigestScheduler
from argus.event_pool import EventPoolProjector


class DigestCooperativeTests(unittest.IsolatedAsyncioTestCase):
    async def test_backfill_yields_to_other_workers_between_small_batches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "state.db")
            self.addCleanup(database.close)
            scheduler = DigestScheduler(DigestConfig(enabled=True), database, topic="eos")
            heartbeats = []
            batches = []

            async def heartbeat() -> None:
                heartbeats.append(True)

            def project(**kwargs: int) -> int:
                self.assertEqual(50, kwargs["limit"])
                if batches:
                    self.assertTrue(heartbeats, "backfill must not starve heartbeat workers")
                batches.append(kwargs)
                return 50 if len(batches) < 3 else 0

            ticker = asyncio.create_task(heartbeat())
            with patch.object(EventPoolProjector, "project_pending", side_effect=project):
                await scheduler._project_events_async(1_789_000_000)
            await ticker
            self.assertEqual(3, len(batches))


if __name__ == "__main__":
    unittest.main()
