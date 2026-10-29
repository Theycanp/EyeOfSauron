"""Offline rollout checks; never access a live feed or production database."""
from __future__ import annotations

from contextlib import closing, redirect_stderr, redirect_stdout
import copy
from dataclasses import replace
from datetime import UTC, datetime
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from argus.config import load_config, parse_source_config
from argus.database import Database
from argus.models import FeedFetchResult
from argus.rules import RuleSet
from argus.rss import parse_feed

from helpers import PROJECT_ROOT, production_config


SPEC = importlib.util.spec_from_file_location(
    "activate_tibo_reset_monitor",
    PROJECT_ROOT / "scripts/operations/activate_tibo_reset_monitor.py",
)
assert SPEC is not None and SPEC.loader is not None
operations = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(operations)


class TiboResetOperationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.base = production_config(self.directory)
        self.base = replace(self.base, service=replace(
            self.base.service, managed_sources_path=self.directory / "managed.json",
        ))
        self.planned = operations.plan_migration({})
        self.effective = load_config(
            PROJECT_ROOT / "config/argus.production.toml", managed_override=self.planned,
        )
        self.source = parse_source_config(operations.SOURCE)
        self.items = parse_feed(
            (PROJECT_ROOT / "tests/fixtures/tibo-reset-announcements.atom").read_bytes(),
            self.source,
        )

    def run_command(self, *arguments: str) -> None:
        with patch("sys.argv", ["activate_tibo_reset_monitor.py", "--config", "/test/config.toml",
                                *arguments]), redirect_stdout(io.StringIO()), \
                patch.object(operations, "load_config", return_value=self.base), \
                patch.object(operations, "probe", return_value={"rule_replay": "passed"}):
            operations.main()

    def test_planner_is_idempotent_and_preserves_other_configuration(self) -> None:
        original = {"sources": [{"id": "unrelated", "enabled": False}],
                    "rules": [], "digest": {"enabled": False},
                    "notifications": {"routes": {"news": None}}}
        before = copy.deepcopy(original)
        planned = operations.plan_migration(original)
        self.assertEqual(before, original)
        self.assertEqual(planned, operations.plan_migration(planned))
        self.assertEqual(original["sources"][0], planned["sources"][0])
        self.assertEqual(original["digest"], planned["digest"])
        self.assertEqual(original["notifications"], planned["notifications"])
        planned["sources"][-1]["allowed_hosts"].append("example.test")
        self.assertNotIn("example.test", operations.SOURCE["allowed_hosts"])

    def test_operator_edits_are_never_overwritten(self) -> None:
        for key, field, value in (("sources", "enabled", False), ("rules", "priority", 2)):
            with self.subTest(key=key):
                current = copy.deepcopy(self.planned)
                current[key][0][field] = value
                before = copy.deepcopy(current)
                with self.assertRaisesRegex(ValueError, "preserve operator"):
                    operations.plan_migration(current)
                self.assertEqual(before, current)

    def test_snapshot_keeps_ids_credit_and_announcement_semantics(self) -> None:
        self.assertEqual(2, len(self.items))
        self.assertEqual("https://x.com/i/status/2115273064481218829", self.items[-1].external_id)
        rules = RuleSet.from_config(self.effective.rules, "eos-news")
        for item in self.items:
            matches = [match for match in rules.evaluate(item, int(item.published_at.timestamp()) + 1)
                       if match.rule_id == operations.RULE["id"]]
            self.assertEqual(1, len(matches))
            self.assertEqual(4, matches[0].priority)
            self.assertIn("以原帖为准", matches[0].title)
            self.assertIn("Data: tokenusage.dev", matches[0].message)
            self.assertIn("not proof", matches[0].message)

    def test_unrelated_stale_and_future_messages_do_not_notify(self) -> None:
        rules = RuleSet.from_config(
            tuple(rule for rule in self.effective.rules if rule.id == operations.RULE["id"]),
            "eos-news",
        )
        item = self.items[-1]
        now = int(item.published_at.timestamp())
        for changed, timestamp in (
            (replace(item, source_id="other_source"), now),
            (replace(item, title="Ordinary product update"), now),
            (item, now + 172801), (item, now - 3601),
        ):
            with self.subTest(timestamp=timestamp, title=changed.title):
                self.assertFalse(rules.evaluate(changed, timestamp))

    def test_real_parser_probe_needs_no_recent_reset_to_claim_monitor_health(self) -> None:
        collector = Mock()
        collector.fetch.return_value = FeedFetchResult(self.items, None, None, False)
        with patch.object(operations, "build_collector", return_value=collector), \
                patch.object(operations, "datetime") as clock:
            clock.now.return_value = datetime(2026, 11, 30, tzinfo=UTC)
            result = operations.probe(self.effective)
        self.assertEqual(2, result["items"])
        self.assertGreater(result["latest_age_seconds"], 7 * 86400)
        self.assertEqual("not_checked", result["upstream_poll_freshness"])

    def test_probe_rejects_empty_unrelated_links_and_inferred_dates(self) -> None:
        item = self.items[-1]
        cases = [(), (replace(item, url="https://x.com/other/status/2115273064481218829"),),
                 (replace(item, attributes={**item.attributes, "published_at_inferred": True}),)]
        for observations in cases:
            with self.subTest(observations=observations):
                collector = Mock()
                collector.fetch.return_value = FeedFetchResult(observations, None, None, False)
                with patch.object(operations, "build_collector", return_value=collector), \
                        self.assertRaises(RuntimeError):
                    operations.probe(self.effective)

    def test_preview_does_not_create_configuration_or_alerts(self) -> None:
        self.run_command("--expect-revision", "0")
        with closing(Database(self.base.service.database_path)) as database:
            self.assertIsNone(database.get_active_config_revision())
            self.assertFalse(database.list_alerts())

    def test_apply_creates_one_revision_and_second_apply_is_noop(self) -> None:
        self.run_command("--expect-revision", "0", "--apply")
        with closing(Database(self.base.service.database_path)) as database:
            active = database.get_active_config_revision()
            self.assertIsNotNone(active)
            self.assertEqual(self.planned, active["payload"])
            revision = active["revision"]
        self.run_command("--expect-revision", str(revision), "--apply")
        with closing(Database(self.base.service.database_path)) as database:
            self.assertEqual(revision, database.get_active_config_revision()["revision"])
            self.assertFalse(database.list_alerts())

    def test_revision_mismatch_and_probe_failure_never_write(self) -> None:
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.run_command("--expect-revision", "4", "--apply")
        with patch.object(operations, "probe", side_effect=RuntimeError("probe failed")):
            # run_command normally supplies a successful probe, so invoke directly here.
            with patch("sys.argv", ["activate_tibo_reset_monitor.py", "--config", "/test/config.toml",
                                    "--expect-revision", "0", "--apply"]), \
                    patch.object(operations, "load_config", return_value=self.base), \
                    self.assertRaisesRegex(RuntimeError, "probe failed"):
                operations.main()
        with closing(Database(self.base.service.database_path)) as database:
            self.assertIsNone(database.get_active_config_revision())


if __name__ == "__main__":
    unittest.main()
