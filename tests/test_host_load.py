from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from argus.config import SourceConfig
from argus.database import Database
from argus.host import HostHealthCollector
from argus.models import SourceState
from argus.rules import RuleSet


class HostLoadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = SourceConfig(
            id="host_test", kind="host", publisher="bk", section="Host", dedupe_scope="host",
            poll_interval_seconds=60, request_timeout_seconds=5, request_attempts=1,
            retry_base_seconds=1, max_response_bytes=1024, settings={"paths": [], "load1": 6},
        )
        self.state = SourceState("host_test", True, None, None, None, None, 0, False, None)

    def poll(self, seconds: int, load: float | None = 6.4):
        with patch("argus.host.datetime") as clock, \
                patch("argus.host._mem_percent", return_value=10), \
                patch("argus.host.os.getloadavg") as getload:
            clock.now.return_value = datetime.fromtimestamp(10000 + seconds, UTC)
            if load is None:
                getload.side_effect = OSError
            else:
                getload.return_value = (load, 0, 0)
            result = HostHealthCollector(self.source).fetch(self.state)
        self.state = replace(self.state, cursor=result.cursor, consecutive_failures=0)
        return result

    def test_short_spike_has_no_alert_or_recovery(self) -> None:
        for seconds, load in ((0, 6.4), (60, 6.5), (102, 2.0)):
            self.assertEqual((), self.poll(seconds, load).observations)
        self.assertNotIn("load_pending", json.loads(self.state.cursor))

    def test_sustained_overload_alerts_once_and_recovers_once(self) -> None:
        for seconds in range(0, 300, 60):
            self.assertEqual((), self.poll(seconds).observations)
        result = self.poll(300)
        self.assertEqual(1, len(result.observations))
        self.assertEqual("load", result.observations[0].attributes["check"])
        self.assertIn("300", result.observations[0].summary)
        self.assertFalse(result.observations[0].attributes.get("recovery"))
        self.assertEqual((), self.poll(360).observations)
        recovery = self.poll(420, 2)
        self.assertEqual(1, len(recovery.observations))
        self.assertTrue(recovery.observations[0].attributes["recovery"])
        self.assertEqual((), self.poll(480, 2).observations)

    def test_low_sample_resets_streak(self) -> None:
        for seconds in (0, 60, 120, 180, 240):
            self.poll(seconds)
        self.poll(299, 5.9)
        self.assertEqual((), self.poll(300).observations)
        self.assertEqual((), self.poll(360).observations)

    def test_unknown_sample_resets_pending_without_false_recovery(self) -> None:
        for seconds in (0, 60, 120, 180, 240):
            self.poll(seconds)
        unknown = self.poll(270, None)
        self.assertIn("unknown:load", unknown.warnings)
        self.assertNotIn("load_pending", json.loads(unknown.cursor))
        self.assertEqual((), self.poll(300).observations)
        self.state = replace(self.state, cursor='{"active":["load"]}')
        result = self.poll(360, None)
        self.assertEqual((), result.observations)
        self.assertIn("load", json.loads(result.cursor)["active"])

    def test_nonfinite_or_negative_load_is_unknown(self) -> None:
        for load in (float("nan"), float("inf"), -1):
            with self.subTest(load=load):
                self.state = replace(self.state, cursor='{"active":["load"]}')
                result = self.poll(0, load)
                self.assertEqual((), result.observations)
                self.assertIn("unknown:load", result.warnings)

    def test_poll_gap_and_clock_rollback_reset_pending(self) -> None:
        self.poll(0)
        self.poll(60)
        self.assertEqual((), self.poll(500).observations)
        self.assertEqual(10500, json.loads(self.state.cursor)["load_pending"]["since"])
        self.assertEqual((), self.poll(400).observations)
        self.assertEqual(10400, json.loads(self.state.cursor)["load_pending"]["since"])

    def test_collector_failure_resets_pending(self) -> None:
        for seconds in (0, 60, 120, 180, 240):
            self.poll(seconds)
        self.state = replace(self.state, consecutive_failures=1)
        self.assertEqual((), self.poll(300).observations)

    def test_threshold_or_duration_change_resets_pending(self) -> None:
        for field, value in (("load1", 6.1), ("load_sustain_seconds", 180)):
            with self.subTest(field=field):
                self.setUp()
                for seconds in (0, 60, 120, 180, 240):
                    self.poll(seconds)
                self.source = replace(self.source, settings={**self.source.settings, field: value})
                self.assertEqual((), self.poll(300).observations)

    def test_explicit_zero_duration_keeps_immediate_mode(self) -> None:
        self.source = replace(self.source, settings={**self.source.settings, "load_sustain_seconds": 0})
        self.assertEqual(1, len(self.poll(0).observations))

    def test_corrupt_cursor_starts_fresh(self) -> None:
        for cursor in ("invalid", "[]", '{"load_pending":{"since":true,"sampled_at":10000}}',
                       '{"load_pending":{"since":10000,"sampled_at":9999}}'):
            with self.subTest(cursor=cursor):
                self.state = replace(self.state, cursor=cursor)
                self.assertEqual((), self.poll(300).observations)

    def test_disk_memory_and_unit_checks_remain_immediate(self) -> None:
        self.source = replace(self.source, settings={**self.source.settings, "units": ["demo.service"], "memory_used_percent": 1})
        with patch("argus.host._unit_health", return_value={"demo.service": "unhealthy"}):
            result = self.poll(0)
        self.assertEqual({"memory", "unit:demo.service"}, {item.attributes["check"] for item in result.observations})

    def test_pending_cursor_is_persisted_without_observations_and_resumes(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        database = Database(Path(temporary.name) / "state.db")
        try:
            for seconds in range(0, 300, 60):
                result = self.poll(seconds)
                report = database.record_source_success(self.source.id, result, RuleSet(()), 10000 + seconds, "eos-news")
                self.assertEqual(0, report.inserted_observations)
                self.state = database.get_source_state(self.source.id)
            self.assertEqual(10000, json.loads(self.state.cursor)["load_pending"]["since"])
            self.assertEqual(1, len(self.poll(300).observations))
        finally:
            database.close()
