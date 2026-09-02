#!/usr/bin/env python3
"""Tests for the health check's pure helpers."""

import unittest
from datetime import datetime, timedelta, timezone

import healthcheck as hc


class Ago(unittest.TestCase):
    def _ago(self, **delta):
        return hc.ago(datetime.now(timezone.utc) - timedelta(**delta))

    def test_minutes(self):
        self.assertEqual(self._ago(minutes=14), "14 min ago")

    def test_hours(self):
        self.assertEqual(self._ago(hours=3), "3.0 hours ago")

    def test_days(self):
        self.assertEqual(self._ago(days=3), "3.0 days ago")

    def test_naive_timestamps_are_handled(self):
        # monitor.log stores local time without an offset.
        local = datetime.now().astimezone() - timedelta(minutes=5)
        self.assertEqual(hc.ago(local), "5 min ago")


class Thresholds(unittest.TestCase):
    def test_grace_periods_exceed_their_intervals(self):
        # A check that fires every 15 minutes must not be called overdue
        # the moment it is a second late.
        self.assertGreater(hc.RUN_GRACE, timedelta(minutes=15))
        # The sweep runs once a day, so the grace must exceed a day.
        self.assertGreater(hc.SWEEP_GRACE, timedelta(hours=24))
        self.assertGreater(hc.PAGE_GRACE, timedelta(hours=2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
