from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from argus.database import Database
from argus.reminders import ReminderError, next_daily_occurrence, parse_reminder


NOW = int(datetime(2026, 9, 5, 0, 0, tzinfo=UTC).timestamp())


def reminder_payload(**overrides):  # type: ignore[no-untyped-def]
    payload = {
        "title": "喝水",
        "message": "起来活动一下并喝杯水。",
        "schedule_kind": "once",
        "run_at": NOW + 60,
        "timezone": "Asia/Shanghai",
        "enabled": True,
        "priority": 3,
        "tags": ["alarm_clock"],
    }
    payload.update(overrides)
    return payload


class ReminderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "state.db")

    def tearDown(self) -> None:
        self.database.close()
        self.temp.cleanup()

    def test_after_is_stored_as_an_absolute_one_time_schedule(self) -> None:
        spec = parse_reminder(
            reminder_payload(schedule_kind="after", delay_seconds=900, run_at=None),
            NOW,
        )
        self.assertEqual("once", spec.schedule_kind)
        self.assertEqual(NOW + 900, spec.run_at)

    def test_invalid_or_past_schedule_is_rejected(self) -> None:
        with self.assertRaises(ReminderError):
            parse_reminder(reminder_payload(run_at=NOW), NOW)
        with self.assertRaises(ReminderError):
            parse_reminder(reminder_payload(timezone="Not/AZone"), NOW)

    def test_daily_schedule_uses_named_timezone(self) -> None:
        occurrence = next_daily_occurrence("09:00", "Asia/Shanghai", NOW)
        local = datetime.fromtimestamp(occurrence, ZoneInfo("Asia/Shanghai"))
        self.assertEqual((2026, 9, 5, 9, 0), (local.year, local.month, local.day, local.hour, local.minute))

    def test_nonexistent_dst_time_moves_to_first_valid_minute(self) -> None:
        before = int(datetime(2026, 3, 8, 5, 0, tzinfo=UTC).timestamp())
        occurrence = next_daily_occurrence("02:30", "America/New_York", before)
        local = datetime.fromtimestamp(occurrence, ZoneInfo("America/New_York"))
        self.assertEqual((3, 0), (local.hour, local.minute))

    def test_one_time_reminder_enters_outbox_exactly_once(self) -> None:
        spec = parse_reminder(reminder_payload(), NOW)
        saved = self.database.upsert_reminder(spec, "tester", NOW)
        self.assertTrue(saved["enabled"])
        self.assertEqual(0, self.database.enqueue_due_reminders(NOW + 59, "eos"))
        self.assertEqual(1, self.database.enqueue_due_reminders(NOW + 60, "eos"))
        self.assertEqual(0, self.database.enqueue_due_reminders(NOW + 61, "eos"))
        completed = self.database.get_reminder(spec.id)
        assert completed is not None
        self.assertFalse(completed["enabled"])
        self.assertIsNotNone(completed["completed_at"])
        alert = self.database.claim_due_alert(NOW + 60, 30)
        self.assertIsNotNone(alert)
        assert alert is not None
        self.assertEqual("喝水", alert.title)
        self.database.mark_delivered(alert.id, NOW + 61)
        self.assertEqual(NOW + 61, self.database.get_reminder(spec.id)["last_delivered_at"])

    def test_daily_downtime_coalesces_missed_occurrences(self) -> None:
        spec = parse_reminder(
            reminder_payload(
                schedule_kind="daily",
                daily_time="09:00",
                run_at=None,
            ),
            NOW,
        )
        saved = self.database.upsert_reminder(spec, "tester", NOW)
        first_due = saved["next_run_at"]
        resumed_at = first_due + 3 * 86400
        self.assertEqual(1, self.database.enqueue_due_reminders(resumed_at, "eos"))
        self.assertEqual(0, self.database.enqueue_due_reminders(resumed_at, "eos"))
        current = self.database.get_reminder(spec.id)
        assert current is not None
        self.assertTrue(current["enabled"])
        self.assertGreater(current["next_run_at"], resumed_at)
        self.assertEqual(1, self.database.status()["outbox"]["pending"])

    def test_update_and_delete_cancel_pending_delivery(self) -> None:
        original = parse_reminder(reminder_payload(), NOW)
        self.database.upsert_reminder(original, "tester", NOW)
        self.database.enqueue_due_reminders(NOW + 60, "eos")
        replacement = parse_reminder(
            reminder_payload(id=original.id, run_at=NOW + 3600, message="新的内容"),
            NOW + 61,
        )
        saved = self.database.upsert_reminder(replacement, "tester", NOW + 61)
        self.assertEqual("新的内容", saved["message"])
        self.assertNotIn("pending", self.database.status()["outbox"])
        self.assertTrue(self.database.delete_reminder(original.id, "tester", NOW + 62))
        self.assertIsNone(self.database.get_reminder(original.id))

    def test_disabled_daily_reminder_can_be_enabled_without_editing(self) -> None:
        spec = parse_reminder(
            reminder_payload(
                schedule_kind="daily",
                daily_time="20:30",
                run_at=None,
                enabled=False,
            ),
            NOW,
        )
        saved = self.database.upsert_reminder(spec, "tester", NOW)
        self.assertFalse(saved["enabled"])
        self.assertIsNone(saved["next_run_at"])
        enabled = self.database.set_reminder_enabled(spec.id, True, "tester", NOW + 10)
        self.assertTrue(enabled["enabled"])
        self.assertGreater(enabled["next_run_at"], NOW + 10)
        disabled = self.database.set_reminder_enabled(spec.id, False, "tester", NOW + 20)
        self.assertFalse(disabled["enabled"])
        self.assertIsNone(disabled["next_run_at"])

    def test_reminder_survives_database_reopen(self) -> None:
        spec = parse_reminder(reminder_payload(), NOW)
        self.database.upsert_reminder(spec, "tester", NOW)
        path = self.database.path
        self.database.close()
        self.database = Database(path)
        restored = self.database.get_reminder(spec.id)
        self.assertIsNotNone(restored)
        self.assertEqual(NOW + 60, restored["next_run_at"])


if __name__ == "__main__":
    unittest.main()
