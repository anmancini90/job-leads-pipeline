"""Synthetic time-zone checks; no scheduler is created."""

import importlib.util
import unittest
from datetime import date
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_schedule.py"
SPEC = importlib.util.spec_from_file_location("check_schedule", SCRIPT)
schedule = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(schedule)


class ScheduleTests(unittest.TestCase):
    def test_morning_time_stays_at_same_wall_clock_across_dst(self):
        result = schedule.check_schedule("America/New_York", "06:30", start_date=date(2026, 1, 1))
        self.assertEqual("stable", result["status"])
        self.assertEqual([], result["unstable_dates"])

    def test_missing_and_repeated_times_are_rejected(self):
        spring = schedule.check_schedule("America/New_York", "02:30", start_date=date(2026, 1, 1))
        fall = schedule.check_schedule("America/New_York", "01:30", start_date=date(2026, 1, 1))
        self.assertIn("missing local time", [item["reason"] for item in spring["unstable_dates"]])
        self.assertIn("repeated local time", [item["reason"] for item in fall["unstable_dates"]])

    def test_weekday_only_avoids_sunday_dst_gap(self):
        result = schedule.check_schedule("America/New_York", "02:30", weekdays_only=True,
                                         start_date=date(2026, 1, 1))
        self.assertEqual("stable", result["status"])

    def test_bad_time_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "24-hour"):
            schedule.check_schedule("America/New_York", "25:99")


if __name__ == "__main__":
    unittest.main()
