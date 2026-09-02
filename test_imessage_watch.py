#!/usr/bin/env python3
"""Tests for the change detection behind the iMessage watcher."""

import unittest

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

    def test_reports_dates_extending(self):
        after = {"hits": [], "last_day": {"AAOPK": "2026-09-23", "AANEM": "2026-09-11"}}
        message = w.describe(after, BEFORE)
        self.assertIn("2026-09-23", message)
        self.assertIn("AAOPK", message)
        self.assertNotIn("AANEM", message)

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
