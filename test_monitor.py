#!/usr/bin/env python3
"""Tests for the seat matching and response parsing.

The fixtures are real responses captured from Fandango, so these exercise
the shapes the monitor actually meets rather than invented ones.
"""

import json
import re
import unittest
from pathlib import Path

import fandango_monitor as fm

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text())


def seat(seat_id, x, status="R", seat_type="standard", width=32.7):
    return {"id": seat_id, "x": x, "width": width, "status": status, "type": seat_type}


class AccessibleSeats(unittest.TestCase):
    """Wheelchair spaces and companion seats must never count as ours."""

    def setUp(self):
        self.seatmap = load("seatmap_hacienda_g.json")

    def test_row_h_has_available_accessible_seats(self):
        # Guards the premise of the next test: if Fandango ever stops
        # marking these, the exclusion test below would pass vacuously.
        rows_h = [s for s in self.seatmap["seats"] if re.match(r"H\d", s["id"])]
        special = [s for s in rows_h if s["type"] != "standard"]
        self.assertTrue(special, "fixture should contain accessible seats in row H")
        self.assertTrue(
            any(s["status"] == "A" for s in special),
            "fixture should have accessible seats showing as available",
        )

    def test_accessible_seats_are_not_bookable(self):
        bookable = fm.bookable_row(self.seatmap, "H")
        self.assertTrue(all(s["type"] == "standard" for s in bookable))

    def test_row_h_yields_no_match_despite_free_seats(self):
        groups = fm.find_groups(self.seatmap, rows=("H",))
        self.assertEqual(groups, [])


class RealSeatMaps(unittest.TestCase):
    def test_hacienda_target_rows_are_sold_out(self):
        self.assertEqual(fm.find_groups(load("seatmap_hacienda_g.json")), [])

    def test_metreon_target_rows_are_sold_out(self):
        self.assertEqual(fm.find_groups(load("seatmap_metreon_g.json")), [])

    def test_pair_is_found_when_centre_seats_open_up(self):
        seatmap = load("seatmap_hacienda_g.json")
        for entry in seatmap["seats"]:
            if entry["id"] in ("G16", "G17"):
                entry["status"] = "A"
        groups = fm.find_groups(seatmap)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["row"], "G")
        self.assertEqual(sorted(groups[0]["seats"]), ["G16", "G17"])

    def test_edge_seats_are_ignored(self):
        seatmap = load("seatmap_hacienda_g.json")
        for entry in seatmap["seats"]:
            if entry["id"] in ("G1", "G2"):
                entry["status"] = "A"
        self.assertEqual(fm.find_groups(seatmap), [])

    def test_most_central_pair_wins(self):
        seatmap = load("seatmap_hacienda_g.json")
        for entry in seatmap["seats"]:
            if entry["id"] in ("G13", "G14", "G16", "G17"):
                entry["status"] = "A"
        groups = fm.find_groups(seatmap)
        self.assertEqual(sorted(groups[0]["seats"]), ["G16", "G17"])


class Geometry(unittest.TestCase):
    def test_aisle_breaks_adjacency(self):
        seats = [seat("G3", 0.0, "A"), seat("G2", 40.0, "A"), seat("G1", 400.0, "A")]
        runs = fm.adjacent_runs(seats)
        self.assertEqual([len(run) for run in runs], [2, 1])

    def test_seats_across_an_aisle_do_not_pair(self):
        seatmap = {"seats": [
            seat("G2", 0.0, "A"), seat("G1", 400.0, "A"),
        ]}
        self.assertEqual(fm.find_groups(seatmap, rows=("G",), centre=8), [])

    def test_centre_window_picks_middle_seats(self):
        seats = [seat(f"G{i}", i * 40.0) for i in range(10)]
        window = fm.centre_window(seats, 4)
        self.assertEqual(window, {"G3", "G4", "G5", "G6"})

    def test_row_letters_are_read_from_seat_ids(self):
        self.assertEqual(fm.seat_row({"id": "G16"}), "G")
        self.assertEqual(fm.seat_row({"id": "WC1"}), "WC")
        self.assertIsNone(fm.seat_row({"id": "??"}))


class Screenings(unittest.TestCase):
    def test_finds_the_film_at_hacienda(self):
        view_model = load("showtimes_hacienda_2026-09-14.json")["viewModel"]
        screenings = fm.extract_screenings(view_model)
        self.assertEqual(len(screenings), 4)
        self.assertTrue(all("70MM" in s["format"] for s in screenings))
        self.assertTrue(all(s["hash"] for s in screenings))
        self.assertTrue(all(s["url"].startswith("https://") for s in screenings))

    def test_non_imax_prints_are_filtered_out(self):
        view_model = load("showtimes_metreon_2026-09-05.json")["viewModel"]
        screenings = fm.extract_screenings(view_model)
        self.assertTrue(screenings)
        self.assertTrue(all("IMAX" in s["format"].upper() for s in screenings))

        movie = [m for m in view_model["movies"] if m["id"] == fm.MOVIE_ID][0]
        every_group = [
            group
            for variant in movie["variants"]
            for group in variant["amenityGroups"]
        ]
        self.assertGreater(len(every_group), 1,
                           "fixture should carry a second, non-IMAX print")

    def test_unpublished_date_yields_nothing(self):
        self.assertEqual(fm.extract_screenings(None), [])

    def test_unexpected_shape_is_an_error(self):
        with self.assertRaises(fm.MonitorError):
            fm.extract_screenings({"theater": {}})


class Reporting(unittest.TestCase):
    def test_summary_counts_matches(self):
        state = json.loads(json.dumps(fm.EMPTY_STATE))
        theater = next(iter(fm.THEATERS))
        far = "2099-01-01"
        state["frontier"][theater] = far
        state["availability"][theater] = {
            far: {
                "6:10 PM": {"sold_out": False, "free": 12, "match": ["G16", "G17"]},
                "10:10 PM": {"sold_out": False, "free": 3, "match": None},
            }
        }
        summary = fm.summarise(state)
        row = [t for t in summary["theaters"] if t["code"] == theater][0]
        self.assertEqual(row["shows"], 2)
        self.assertEqual(row["free"], 15)
        self.assertEqual(row["matched"], 1)
        self.assertEqual(summary["hits"][0]["seats"], ["G16", "G17"])

    def test_report_renders_with_no_matches(self):
        state = json.loads(json.dumps(fm.EMPTY_STATE))
        html = fm.render_report(state)
        self.assertIn("Nothing matching yet", html)
        self.assertNotIn("{{", html)

    def test_report_carries_machine_readable_timestamps(self):
        state = json.loads(json.dumps(fm.EMPTY_STATE))
        state["last_seat_sweep"] = 1756771200.0
        html = fm.render_report(state)
        stamps = re.findall(r'<time class="ts mono" datetime="([^"]*)"', html)
        self.assertEqual(len(stamps), 2)
        for stamp in stamps:
            parsed = fm.datetime.fromisoformat(stamp)
            self.assertIsNotNone(parsed.tzinfo,
                                 "timestamp needs an offset to localise correctly")

    def test_report_timestamp_is_blank_before_first_sweep(self):
        state = json.loads(json.dumps(fm.EMPTY_STATE))
        html = fm.render_report(state)
        self.assertIn('datetime=""', html)
        self.assertIn("not yet", html)

    def test_report_renders_matches(self):
        state = json.loads(json.dumps(fm.EMPTY_STATE))
        theater = next(iter(fm.THEATERS))
        state["availability"][theater] = {
            "2099-01-01": {"6:10 PM": {"sold_out": False, "free": 9,
                                       "match": ["G16", "G17"]}}
        }
        html = fm.render_report(state)
        self.assertIn("Seats are open right now", html)
        self.assertIn("G16 + G17", html)
        self.assertNotIn("{{", html)


class Poster(unittest.TestCase):
    def test_poster_is_read_from_the_response(self):
        view_model = load("showtimes_hacienda_2026-09-14.json")["viewModel"]
        poster = fm.extract_poster(view_model)
        self.assertIsNotNone(poster)
        self.assertTrue(poster["url"].startswith("https://"))

    def test_missing_poster_is_not_an_error(self):
        self.assertIsNone(fm.extract_poster({"movies": []}))
        self.assertIsNone(fm.extract_poster(None))

    def test_report_includes_the_poster(self):
        state = json.loads(json.dumps(fm.EMPTY_STATE))
        state["poster"] = {"url": "https://example.test/a.jpg",
                           "wide": "https://example.test/b.jpg"}
        html = fm.render_report(state)
        self.assertIn('class="poster"', html)
        self.assertIn("https://example.test/a.jpg", html)

    def test_report_omits_poster_when_unknown(self):
        html = fm.render_report(json.loads(json.dumps(fm.EMPTY_STATE)))
        self.assertNotIn('class="poster"', html)
        self.assertNotIn("{{", html)


class TheaterDetails(unittest.TestCase):
    def test_address_and_map_link_are_read(self):
        view_model = load("showtimes_hacienda_2026-09-14.json")["viewModel"]
        place = fm.extract_theater(view_model)
        self.assertIn("Dublin", place["address"])
        self.assertTrue(place["map"].startswith("https://maps.apple.com/?"))
        self.assertIn("ll=37.7062%2C-121.8866", place["map"])

    def test_metreon_address_is_read(self):
        view_model = load("showtimes_metreon_2026-09-05.json")["viewModel"]
        place = fm.extract_theater(view_model)
        self.assertIn("San Francisco", place["address"])
        self.assertTrue(place["map"].startswith("https://maps.apple.com/?"))

    def test_missing_theater_details_are_tolerated(self):
        self.assertIsNone(fm.extract_theater({"theater": {}}))
        self.assertIsNone(fm.extract_theater(None))

    def test_map_link_reaches_the_page(self):
        state = json.loads(json.dumps(fm.EMPTY_STATE))
        code = next(iter(fm.THEATERS))
        state["places"] = {code: {"name": "A Theater",
                                  "address": "1 Example St, Town, CA 90001",
                                  "map": "https://maps.apple.com/?q=A+Theater"}}
        html = fm.render_report(state)
        self.assertIn('href="https://maps.apple.com/?q=A+Theater"', html)
        self.assertIn("1 Example St, Town, CA 90001", html)

    def test_page_renders_without_addresses(self):
        html = fm.render_report(json.loads(json.dumps(fm.EMPTY_STATE)))
        self.assertNotIn("maps.apple.com", html)
        self.assertNotIn("{{", html)


class AppleScriptQuoting(unittest.TestCase):
    def test_quotes_and_backslashes_are_escaped(self):
        self.assertEqual(fm.applescript_string('a "b" \\c'), '"a \\"b\\" \\\\c"')


if __name__ == "__main__":
    unittest.main(verbosity=2)
