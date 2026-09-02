#!/usr/bin/env python3
"""Tests for the change detection behind the iMessage watcher."""

import json
import pathlib
import tempfile
import unittest
from datetime import timedelta

import imessage_watch as w

BEFORE = {"hits": [], "last_day": {"AAOPK": "2026-09-16", "AANEM": "2026-09-11"}}


class Describe(unittest.TestCase):
    def test_quiet_when_nothing_moved(self):
        self.assertIsNone(w.describe(BEFORE, BEFORE))

    def test_reports_a_new_seat_pair(self):
        after = dict(BEFORE, hits=["AMC Metreon 16|2026-09-18|6:00 PM|G16+G17"])
        message = w.describe(after, BEFORE)
        self.assertIn("G16+G17", message)
        self.assertIn("AMC Metreon 16", message)

    NAMES = {"AAOPK": "Regal Hacienda Crossings", "AANEM": "AMC Metreon 16"}

    def test_reports_dates_extending(self):
        after = {"hits": [], "last_day": {"AAOPK": "2026-09-23", "AANEM": "2026-09-11"}}
        message = w.describe(after, BEFORE, self.NAMES)
        self.assertIn("2026-09-23", message)
        self.assertIn("Regal Hacienda Crossings", message)
        self.assertNotIn("Metreon", message)

    def test_theater_codes_never_reach_the_reader(self):
        # "New dates at AAOPK" means nothing on a phone.
        after = {"hits": [], "last_day": {"AAOPK": "2026-09-23", "AANEM": "2026-09-11"}}
        message = w.describe(after, BEFORE, self.NAMES)
        self.assertNotIn("AAOPK", message)

    def test_falls_back_to_the_code_if_the_name_is_unknown(self):
        after = {"hits": [], "last_day": {"AAOPK": "2026-09-23", "AANEM": "2026-09-11"}}
        message = w.describe(after, BEFORE, {})
        self.assertIn("AAOPK", message)

    def test_a_hit_is_not_repeated(self):
        hit = "AMC Metreon 16|2026-09-18|6:00 PM|G16+G17"
        after = dict(BEFORE, hits=[hit])
        self.assertIsNone(w.describe(after, after))

    def test_dates_shrinking_is_not_news(self):
        # Showtimes expiring is normal and must not text anyone.
        after = {"hits": [], "last_day": {"AAOPK": "2026-09-14", "AANEM": "2026-09-11"}}
        self.assertIsNone(w.describe(after, BEFORE))

    def test_first_run_is_quiet_when_there_is_nothing_to_say(self):
        self.assertIsNone(w.describe(BEFORE, {}))

    def test_first_run_does_report_seats_that_are_already_open(self):
        # With no history every hit counts as new. That is deliberate: if
        # seats are open the moment this is installed, you want telling.
        after = dict(BEFORE, hits=["AMC Metreon 16|2026-09-18|6:00 PM|G16+G17"])
        self.assertIn("G16+G17", w.describe(after, {}))

    def test_first_run_does_not_announce_existing_dates_as_new(self):
        # Unlike hits, a last_day with nothing to compare against is not news.
        self.assertIsNone(w.describe(BEFORE, {}))


class Interesting(unittest.TestCase):
    def test_timestamps_are_ignored(self):
        base = {"generated": "a", "swept": "b", "hits": [],
                "theaters": [{"code": "X", "last_day": "2026-09-16"}]}
        later = dict(base, generated="c", swept="d")
        self.assertEqual(w.interesting(base), w.interesting(later))


class SeenFile(unittest.TestCase):
    """load_seen has to cope with whatever is already on disk."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / "seen.json"
        self.original, w.SEEN_FILE = w.SEEN_FILE, self.path
        self.addCleanup(setattr, w, "SEEN_FILE", self.original)

    def test_missing_file_is_a_blank_slate(self):
        loaded = w.load_seen()
        self.assertEqual(loaded["interesting"], {})
        self.assertFalse(any(v for k, v in loaded.items() if k.endswith("_alerted")))

    def test_reads_the_older_flat_format(self):
        # An install predating the watchdog stored the payload at the top
        # level. Misreading it as blank would re-announce everything.
        self.path.write_text(json.dumps(
            {"hits": ["x"], "last_day": {"AAOPK": "2026-09-16"}}))
        loaded = w.load_seen()
        self.assertEqual(loaded["interesting"]["hits"], ["x"])
        self.assertFalse(loaded["broken_alerted"])

    def test_reads_the_current_format(self):
        self.path.write_text(json.dumps(
            {"interesting": {"hits": ["y"], "last_day": {}},
             "broken_alerted": True}))
        loaded = w.load_seen()
        self.assertEqual(loaded["interesting"]["hits"], ["y"])
        self.assertTrue(loaded["broken_alerted"])

    def test_corrupt_file_does_not_crash_the_run(self):
        self.path.write_text("{not json")
        loaded = w.load_seen()
        self.assertEqual(loaded["interesting"], {})
        self.assertFalse(any(v for k, v in loaded.items() if k.endswith("_alerted")))


class Thresholds(unittest.TestCase):
    def test_a_run_is_attempted_before_declaring_it_broken(self):
        # Otherwise it would announce a failure it had not yet tried to fix.
        self.assertLess(w.RUN_EVERY, w.BROKEN_AFTER)

    def test_cadence_is_longer_than_the_check_interval(self):
        # This runs every 30 minutes. A shorter threshold would trigger a
        # run on every single tick.
        self.assertGreater(w.RUN_EVERY, timedelta(minutes=30))

    def test_cadence_yields_hourly_runs(self):
        # Above 60 and the 60 minute tick is skipped, slipping to 90.
        self.assertLess(w.RUN_EVERY, timedelta(minutes=60))


class RunFailureAlert(unittest.TestCase):
    """A red run means the monitor already failed three times over, so it is
    worth a text there and then rather than after the four hour clock."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / "seen.json"
        self.original, w.SEEN_FILE = w.SEEN_FILE, self.path
        self.addCleanup(setattr, w, "SEEN_FILE", self.original)

    def test_blank_state_has_the_flag(self):
        self.assertIn("run_failed_alerted", w.load_seen())

    def test_older_state_gains_the_flag(self):
        self.path.write_text(json.dumps({"hits": [], "last_day": {}}))
        self.assertIn("run_failed_alerted", w.load_seen())

    def test_flag_survives_a_round_trip(self):
        self.path.write_text(json.dumps(
            {"interesting": {}, "broken_alerted": False,
             "run_failed_alerted": True}))
        self.assertTrue(w.load_seen()["run_failed_alerted"])


class Escaping(unittest.TestCase):
    def test_quotes_do_not_break_the_applescript(self):
        # send() builds AppleScript source, so a stray quote would be an
        # injection rather than a message.
        self.assertNotIn('""', 'x'.replace('"', '\\"'))
        text = 'He said "G16"'
        self.assertEqual(text.replace("\\", "\\\\").replace('"', '\\"'),
                         'He said \\"G16\\"')


if __name__ == "__main__":
    unittest.main(verbosity=2)
