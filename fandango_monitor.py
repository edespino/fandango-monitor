#!/usr/bin/env python3
"""Watch Fandango for a film's showtimes and report well-placed seat pairs.

The monitor tracks one or more theaters and answers two questions on a
schedule: has the theater published showtimes beyond the dates it had last
time, and are there adjacent seats free in the rows we actually want.

It reads Fandango's public JSON endpoints. It never purchases anything.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# What to watch
# ---------------------------------------------------------------------------

MOVIE_ID = 241283
MOVIE_TITLE = "The Odyssey"

THEATERS = {
    "AAOPK": "Regal Hacienda Crossings",
    "AANEM": "AMC Metreon 16",
}

# Only auditoriums whose amenity string contains this token are considered.
# "IMAX" selects the 70MM presentations at both venues and leaves out the
# standard laser prints. Set to "" to accept every format.
FORMAT_MATCH = "IMAX"

# Shown in the footer of the status page. Blank to leave it out.
REPO_URL = "https://github.com/edespino/fandango-monitor"
LICENSE_NAME = "Apache 2.0"
LICENSE_URL = REPO_URL + "/blob/main/LICENSE" if REPO_URL else ""

# Rows are lettered from the screen backward, so row A is the front row.
TARGET_ROWS = ("F", "G")
CENTRE_SEATS = 8      # how many seats around the row's midpoint count as centre
PARTY_SIZE = 2        # seats needed, side by side

# ---------------------------------------------------------------------------
# How often
# ---------------------------------------------------------------------------

# The two checks cost very different amounts and are worth very different
# things, so they run at different rates.
#
# Looking for newly published dates costs about 8 requests and is the signal
# that matters: when a theater loads its next week the whole auditorium opens
# at once. That happens on every invocation.
#
# Reading every seat map costs about 120 requests and only finds
# cancellations in rows that are already fully sold, so it gates itself to a
# few times a day.
SEAT_SWEEP_INTERVAL = 8 * 3600

DATE_PROBE_NEAR = 3     # dates past the frontier checked on every run
DATE_PROBE_WIDE = 14    # dates past the frontier checked on a seat-sweep run
BOOTSTRAP_DAYS = 45     # how far ahead the very first run looks
RUN_END_WARN_DAYS = 2   # warn when the last known date is this close

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

BASE = "https://www.fandango.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
REQUEST_DELAY = 1.5      # seconds between requests
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
FAILURE_ALERT_THRESHOLD = 8   # consecutive failed runs before shouting

HERE = Path(__file__).resolve().parent
STATE_PATH = HERE / "state.json"
LOG_PATH = HERE / "monitor.log"
HITS_PATH = HERE / "hits.log"
LOCK_PATH = HERE / ".lock"


class MonitorError(RuntimeError):
    """A request failed or a response did not look the way it should."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class Fandango:
    """Minimal JSON client with a fixed delay between requests."""

    def __init__(self, delay: float = REQUEST_DELAY):
        self.delay = delay
        self._last = 0.0
        self.requests = 0

    def get(self, path: str):
        url = path if path.startswith("http") else BASE + path
        gap = self.delay - (time.monotonic() - self._last)
        if gap > 0:
            time.sleep(gap)

        last_error = None
        for attempt in range(MAX_RETRIES):
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "en-US,en;q=0.9",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": BASE + "/",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                    payload = response.read()
                self._last = time.monotonic()
                self.requests += 1
                return json.loads(payload)
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    # An expired showtime; not a failure worth retrying.
                    self._last = time.monotonic()
                    self.requests += 1
                    return None
                last_error = exc
            except Exception as exc:  # network, timeout, malformed JSON
                last_error = exc
            time.sleep(2 ** attempt)

        self._last = time.monotonic()
        raise MonitorError(f"GET {url} failed: {last_error}")

    def calendar(self, theater: str):
        data = self.get(f"/napi/theaterCalendar/{theater}")
        if not isinstance(data, dict) or "showtimeDates" not in data:
            raise MonitorError(f"calendar for {theater} missing showtimeDates")
        return data.get("showtimeDates") or []

    def showtimes(self, theater: str, day: str | None = None):
        path = f"/napi/theaterMovieShowtimes/{theater}"
        if day:
            path += "?startDate=" + urllib.parse.quote(day)
        data = self.get(path)
        if not isinstance(data, dict):
            raise MonitorError(f"showtimes for {theater} returned no object")
        # A date the theater has not published comes back as {"date": null}.
        return data.get("viewModel")

    def seatmap(self, showtime_hash: str):
        data = self.get(f"/napi/seatMap/{showtime_hash}")
        if data is None:
            return None
        if "seats" not in data:
            raise MonitorError("seat map missing seats")
        return data


# ---------------------------------------------------------------------------
# Reading responses
# ---------------------------------------------------------------------------

SEAT_ID = re.compile(r"^([A-Za-z]+)(\d+)$")


def seat_row(seat) -> str | None:
    match = SEAT_ID.match(str(seat.get("id", "")))
    return match.group(1).upper() if match else None


def bookable_row(seatmap, row: str):
    """Standard seats in one row, left to right.

    Wheelchair spaces and companion seats are dropped. They report as
    available but are reserved for people who need them.
    """
    seats = [
        s for s in seatmap.get("seats", [])
        if s.get("type") == "standard" and seat_row(s) == row.upper()
    ]
    return sorted(seats, key=lambda s: s["x"])


def centre_window(seats, size: int):
    """Ids of the `size` seats nearest the middle of the row."""
    if not seats:
        return set()
    midpoint = (seats[0]["x"] + seats[-1]["x"]) / 2.0
    ranked = sorted(seats, key=lambda s: abs(s["x"] - midpoint))
    return {s["id"] for s in ranked[:size]}


def adjacent_runs(seats):
    """Split a row into groups of physically neighbouring seats.

    A gap wider than about one and a half seats means an aisle, and seats
    either side of it are not next to each other.
    """
    runs, current = [], []
    for seat in seats:
        if current and seat["x"] - current[-1]["x"] > current[-1]["width"] * 1.6:
            runs.append(current)
            current = []
        current.append(seat)
    if current:
        runs.append(current)
    return runs


def find_groups(seatmap, rows=TARGET_ROWS, centre=CENTRE_SEATS, party=PARTY_SIZE):
    """Best available block of `party` seats in each wanted row.

    Returns the most central match per row, closest to the middle first.
    """
    results = []
    for row in rows:
        seats = bookable_row(seatmap, row)
        if len(seats) < party:
            continue
        midpoint = (seats[0]["x"] + seats[-1]["x"]) / 2.0
        window = centre_window(seats, centre)
        best = None
        for run in adjacent_runs(seats):
            for start in range(len(run) - party + 1):
                block = run[start:start + party]
                if any(s["status"] != "A" for s in block):
                    continue
                if any(s["id"] not in window for s in block):
                    continue
                offset = abs(sum(s["x"] for s in block) / party - midpoint)
                if best is None or offset < best[0]:
                    best = (offset, [s["id"] for s in block])
        if best:
            results.append({"row": row, "seats": best[1], "offset": round(best[0], 1)})
    results.sort(key=lambda group: group["offset"])
    return results


def extract_theater(view_model):
    """Name, address and map link for the theater a response came from."""
    details = ((view_model or {}).get("theater") or {}).get("details") or {}
    if not details.get("name"):
        return None
    geo = details.get("geo") or {}
    entry = {
        "name": details["name"],
        "address": details.get("fullAddress") or "",
        "lat": geo.get("latitude"),
        "lon": geo.get("longitude"),
    }
    query = urllib.parse.urlencode(
        {k: v for k, v in (
            ("ll", f"{entry['lat']},{entry['lon']}"
                   if entry["lat"] is not None and entry["lon"] is not None else None),
            ("q", entry["name"]),
            ("address", entry["address"] or None),
        ) if v}
    )
    entry["map"] = f"https://maps.apple.com/?{query}"
    return entry


def extract_poster(view_model, width: str = "400"):
    """Poster art for the target film, if the response carries any."""
    for movie in (view_model or {}).get("movies") or []:
        if movie.get("id") != MOVIE_ID:
            continue
        sizes = ((movie.get("poster") or {}).get("size")) or {}
        url = sizes.get(width) or sizes.get("500") or sizes.get("full")
        if url:
            return {"url": url, "wide": sizes.get("500") or url}
    return None


def extract_screenings(view_model):
    """Showtimes for the target film in a matching format, on one date."""
    if view_model is None:
        return []
    if "movies" not in view_model:
        raise MonitorError("showtimes response missing 'movies'")

    screenings = []
    for movie in view_model.get("movies") or []:
        if movie.get("id") != MOVIE_ID:
            continue
        for variant in movie.get("variants") or []:
            for group in variant.get("amenityGroups") or []:
                amenity = group.get("amenityString") or ""
                if FORMAT_MATCH and FORMAT_MATCH.upper() not in amenity.upper():
                    continue
                fmt = amenity.split(",")[0].strip()
                for show in group.get("showtimes") or []:
                    screenings.append({
                        "format": fmt,
                        "time": show.get("screenReaderTime") or show.get("date") or "?",
                        "hash": show.get("showtimeHashCode"),
                        "sold_out": bool(show.get("isSoldOut")),
                        "url": show.get("ticketingJumpPageURL") or "",
                    })
    return screenings


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

EMPTY_STATE = {
    "frontier": {},        # theater -> last date the film is known to play
    "movie_dates": {},     # theater -> [dates the film plays]
    "showtimes": {},       # theater -> {date: [times]}
    "matches": {},         # "theater|date|time|row" -> [seat ids]
    "availability": {},    # theater -> {date: {time: {...}}} for the report
    "poster": None,        # artwork for the status page
    "places": {},          # theater -> address and map link
    "rows": {},            # theater -> per-row seat counts, screen first
    "capacity": {},        # theater -> standard seats in the auditorium
    "last_seat_sweep": 0.0,
    "last_report": {},     # summary numbers for the status page
    "failures": 0,
    "failure_alerted": False,
    "run_end_alerted": {},
}


def load_state(path: Path):
    if not path.exists():
        return json.loads(json.dumps(EMPTY_STATE))
    try:
        stored = json.loads(path.read_text())
    except (OSError, ValueError):
        log("state file unreadable, starting fresh")
        return json.loads(json.dumps(EMPTY_STATE))
    state = json.loads(json.dumps(EMPTY_STATE))
    state.update(stored)
    return state


def save_state(path: Path, state):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    tmp.replace(path)


def log(message: str):
    line = f"{datetime.now().isoformat(timespec='seconds')}  {message}"
    print(line)
    try:
        with LOG_PATH.open("a") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


def applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(title: str, body: str, sound: str = "Glass", repeat: int = 3):
    script = (
        f"display notification {applescript_string(body)} "
        f"with title {applescript_string(title)} sound name \"{sound}\""
    )
    for index in range(repeat):
        try:
            subprocess.run(["osascript", "-e", script],
                           capture_output=True, check=False)
        except OSError:
            # Not a Mac, or osascript is missing; the log still has the alert.
            log(f"could not post notification: {title}")
            return
        if index < repeat - 1:
            time.sleep(1.2)


def copy_to_clipboard(text: str):
    try:
        subprocess.run(["pbcopy"], input=text.encode(), check=False)
    except (OSError, FileNotFoundError):
        pass


def deliver(alerts, dry_run: bool):
    if not alerts:
        return
    for alert in alerts:
        entry = (
            f"{datetime.now().isoformat(timespec='seconds')}  [{alert['kind']}] "
            f"{alert['title']} :: {alert['body']}"
        )
        if alert.get("url"):
            entry += f"\n    {alert['url']}"
        log(entry)
        if not dry_run:
            try:
                with HITS_PATH.open("a") as handle:
                    handle.write(entry + "\n")
            except OSError:
                pass

    if dry_run:
        log(f"dry run: {len(alerts)} alert(s) not sent")
        return

    for alert in alerts:
        notify(alert["title"], alert["body"])
    for alert in alerts:
        if alert.get("url"):
            copy_to_clipboard(alert["url"])
            break


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def describe_screenings(screenings):
    times = [s["time"] + (" [sold out]" if s["sold_out"] else "") for s in screenings]
    return ", ".join(times)


def bootstrap_theater(api, state, theater, calendar, alerts):
    """First run: find every date the film currently plays."""
    today = date.today()
    horizon = [
        day for day in calendar
        if (date.fromisoformat(day) - today).days <= BOOTSTRAP_DAYS
    ]
    log(f"{theater}: first run, scanning {len(horizon)} dates")
    playing = []
    for day in horizon:
        if extract_screenings(api.showtimes(theater, day)):
            playing.append(day)
    state["movie_dates"][theater] = playing
    state["frontier"][theater] = playing[-1] if playing else today.isoformat()
    log(f"{theater}: plays on {len(playing)} dates, through {state['frontier'][theater]}")


def check_dates(api, state, alerts, wide: bool) -> bool:
    """Look for showtimes published past what we already knew about.

    Returns True if any theater gained dates, which is the cue to read the
    seat maps immediately rather than waiting for the next interval.
    """
    anything_new = False
    today = date.today()
    for theater, name in THEATERS.items():
        calendar = [d for d in api.calendar(theater) if d >= today.isoformat()]
        if not calendar:
            raise MonitorError(f"{theater}: calendar came back empty")

        if theater not in state["frontier"]:
            bootstrap_theater(api, state, theater, calendar, alerts)
            anything_new = True
            continue

        frontier = state["frontier"][theater]
        limit = DATE_PROBE_WIDE if wide else DATE_PROBE_NEAR
        candidates = [d for d in calendar if d > frontier][:limit]

        found = []
        for day in candidates:
            screenings = extract_screenings(api.showtimes(theater, day))
            if screenings:
                found.append((day, screenings))

        # A new week arrives as a block; map the rest of it.
        if found:
            remaining = [d for d in calendar if d > found[-1][0]]
            for day in remaining:
                screenings = extract_screenings(api.showtimes(theater, day))
                if not screenings:
                    break
                found.append((day, screenings))

        if found:
            anything_new = True
            days = [day for day, _ in found]
            state["movie_dates"].setdefault(theater, [])
            state["movie_dates"][theater] = sorted(
                set(state["movie_dates"][theater]) | set(days)
            )
            state["frontier"][theater] = max(days)
            state["run_end_alerted"][theater] = False
            first_day, first_shows = found[0]
            alerts.append({
                "kind": "new-dates",
                "title": f"{MOVIE_TITLE}: new dates at {name}",
                "body": f"{len(days)} new date(s) from {first_day}. {describe_screenings(first_shows)}",
                "url": first_shows[0]["url"],
            })
        else:
            days_left = (date.fromisoformat(frontier) - today).days
            if days_left <= RUN_END_WARN_DAYS and not state["run_end_alerted"].get(theater):
                state["run_end_alerted"][theater] = True
                alerts.append({
                    "kind": "run-ending",
                    "title": f"{MOVIE_TITLE}: run may be ending at {name}",
                    "body": f"Last showtime is {frontier} and nothing new has been published.",
                    "url": "",
                })

        # Drop dates that have passed.
        state["movie_dates"][theater] = [
            d for d in state["movie_dates"].get(theater, []) if d >= today.isoformat()
        ]

    return anything_new


def check_seats(api, state, alerts):
    today = date.today().isoformat()
    seen_matches = {}
    availability = {}
    row_tally = {}

    for theater, name in THEATERS.items():
        availability[theater] = {}
        for day in sorted(state["movie_dates"].get(theater, [])):
            if day < today:
                continue
            view_model = api.showtimes(theater, day)
            screenings = extract_screenings(view_model)
            if not screenings:
                continue

            if not state.get("poster"):
                poster = extract_poster(view_model)
                if poster:
                    state["poster"] = poster
            if theater not in state.get("places", {}):
                place = extract_theater(view_model)
                if place:
                    state.setdefault("places", {})[theater] = place

            times = sorted({s["time"] for s in screenings})
            previous = state["showtimes"].get(theater, {}).get(day)
            if previous is not None:
                added = [t for t in times if t not in previous]
                if added:
                    alerts.append({
                        "kind": "new-showtime",
                        "title": f"{MOVIE_TITLE}: showtime added at {name}",
                        "body": f"{day}: {', '.join(added)}",
                        "url": screenings[0]["url"],
                    })
            state["showtimes"].setdefault(theater, {})[day] = times

            availability[theater][day] = {}
            for show in screenings:
                record = {"sold_out": show["sold_out"], "free": 0, "match": None}
                if show["sold_out"] or not show["hash"]:
                    availability[theater][day][show["time"]] = record
                    continue

                seatmap = api.seatmap(show["hash"])
                if seatmap is None:
                    availability[theater][day][show["time"]] = record
                    continue

                record["free"] = sum(
                    1 for s in seatmap.get("seats", [])
                    if s.get("type") == "standard" and s.get("status") == "A"
                )

                # Every showtime here is the same auditorium, so its
                # standard-seat count is the capacity to measure against.
                state.setdefault("capacity", {})[theater] = sum(
                    1 for s in seatmap.get("seats", []) if s.get("type") == "standard"
                )

                # Per-row totals, so the page can show where the empty
                # seats actually are rather than just how many there are.
                tally = row_tally.setdefault(theater, {})
                for entry in seatmap.get("seats", []):
                    if entry.get("type") != "standard":
                        continue
                    row = seat_row(entry)
                    if not row:
                        continue
                    slot = tally.setdefault(row, {"y": entry.get("y", 0),
                                                  "free": 0, "total": 0})
                    slot["total"] += 1
                    if entry.get("status") == "A":
                        slot["free"] += 1
                groups = find_groups(seatmap)
                if groups:
                    record["match"] = groups[0]["seats"]
                for group in groups:
                    key = f"{theater}|{day}|{show['time']}|{group['row']}"
                    seen_matches[key] = group["seats"]
                    if key not in state["matches"]:
                        alerts.append({
                            "kind": "seats",
                            "title": f"{MOVIE_TITLE}: row {group['row']} seats at {name}",
                            "body": f"{' + '.join(group['seats'])} on {day} {show['time']}",
                            "url": show["url"],
                        })
                availability[theater][day][show["time"]] = record

    state["matches"] = seen_matches
    state["availability"] = availability
    state["rows"] = {
        theater: [
            {"row": row, "free": counts["free"], "total": counts["total"]}
            for row, counts in sorted(rows.items(), key=lambda kv: kv[1]["y"])
        ]
        for theater, rows in row_tally.items()
    }
    state["last_seat_sweep"] = time.time()


# ---------------------------------------------------------------------------
# Status page
# ---------------------------------------------------------------------------

TEMPLATE_PATH = HERE / "template.html"


def escape(text) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def summarise(state):
    """Numbers the status page needs, derived from the last sweep."""
    today = date.today().isoformat()
    theaters, hits = [], []
    for theater, name in THEATERS.items():
        availability = state.get("availability", {}).get(theater, {})
        shows = free = matched = 0
        for day, times in sorted(availability.items()):
            if day < today:
                continue
            for slot, record in sorted(times.items()):
                shows += 1
                free += record.get("free", 0)
                if record.get("match"):
                    matched += 1
                    hits.append({
                        "theater": name,
                        "day": day,
                        "time": slot,
                        "seats": record["match"],
                    })
        place = state.get("places", {}).get(theater, {})
        capacity = state.get("capacity", {}).get(theater, 0)
        per_show = round(free / shows) if shows else 0
        theaters.append({
            "capacity": capacity,
            "per_show": per_show,
            "code": theater,
            "name": name,
            "address": place.get("address", ""),
            "map": place.get("map", ""),
            "last_day": state.get("frontier", {}).get(theater, "unknown"),
            "shows": shows,
            "free": free,
            "matched": matched,
        })
    return {"theaters": theaters, "hits": hits}


def render_rowmap(state) -> str:
    """A row-by-row picture of where the empty seats are, screen at the top."""
    blocks = []
    for theater, name in THEATERS.items():
        rows = state.get("rows", {}).get(theater) or []
        if not rows:
            continue
        peak = max((row["free"] for row in rows), default=0) or 1
        items = []
        for row in rows:
            width = round(100 * row["free"] / peak, 1)
            classes = ["rowline"]
            if row["row"] in TARGET_ROWS:
                classes.append("target")
            if not row["free"]:
                classes.append("empty")
            items.append(
                '<li class="{cls}"><span class="rl">{row}</span>'
                '<span class="bar"><i style="width:{width:g}%"></i></span>'
                '<span class="rn">{free}</span></li>'.format(
                    cls=" ".join(classes), row=escape(row["row"]),
                    width=width, free=row["free"])
            )
        blocks.append(
            '<figure class="rowmap"><figcaption>{name}</figcaption>'
            '<p class="screen">screen</p><ol class="rows">{items}</ol></figure>'.format(
                name=escape(name), items="".join(items))
        )
    return "".join(blocks)


def render_report(state) -> str:
    if not TEMPLATE_PATH.exists():
        raise MonitorError(f"missing template at {TEMPLATE_PATH}")
    summary = summarise(state)
    swept = state.get("last_seat_sweep") or 0

    rows = []
    for entry in summary["theaters"]:
        flag = "hit" if entry["matched"] else "gone"
        if entry.get("map"):
            label = '<a href="{map}">{name}</a>'.format(
                map=escape(entry["map"]), name=escape(entry["name"]))
        else:
            label = escape(entry["name"])
        if entry.get("address"):
            label += '<span class="addr">{}</span>'.format(escape(entry["address"]))
        rows.append(
            "<tr><th>{name}</th>"
            "<td class=\"num\">{shows}</td>"
            "<td class=\"num\">{last}</td>"
            "<td class=\"num\">{free}</td>"
            "<td class=\"num {flag}\">{matched}</td></tr>".format(
                name=label,
                shows=entry["shows"],
                last=escape(entry["last_day"]),
                free=("{} of {}".format(entry["per_show"], entry["capacity"])
                      if entry["capacity"] else str(entry["per_show"])),
                matched=entry["matched"],
                flag=flag,
            )
        )

    if summary["hits"]:
        headline = "Seats are open right now"
        headline_class = "hit"
        items = "".join(
            "<li><strong>{seats}</strong> &mdash; {theater}, {day} at {time}</li>".format(
                seats=escape(" + ".join(hit["seats"])),
                theater=escape(hit["theater"]),
                day=escape(hit["day"]),
                time=escape(hit["time"]),
            )
            for hit in summary["hits"]
        )
        detail = f"<ul class=\"hits\">{items}</ul>"
    else:
        headline = "Nothing matching yet"
        headline_class = "waiting"
        detail = (
            "<p>Every seat in rows {rows} is taken across all "
            "{shows} remaining showtimes. The monitor keeps checking.</p>".format(
                rows=" and ".join(TARGET_ROWS),
                shows=sum(t["shows"] for t in summary["theaters"]),
            )
        )

    now = datetime.now().astimezone()
    if swept:
        swept_at = datetime.fromtimestamp(swept).astimezone()
        swept_text = swept_at.strftime("%b %-d, %-I:%M %p %Z")
        swept_iso = swept_at.isoformat(timespec="seconds")
    else:
        swept_text, swept_iso = "not yet", ""

    poster = state.get("poster") or {}
    if poster.get("url"):
        poster_html = (
            '<img class="poster" src="{url}" srcset="{url} 1x, {wide} 2x" '
            'width="400" height="600" loading="eager" decoding="async" '
            'alt="{movie} poster">'
        ).format(
            url=escape(poster["url"]),
            wide=escape(poster.get("wide") or poster["url"]),
            movie=escape(MOVIE_TITLE),
        )
    else:
        poster_html = ""

    parts = []
    if REPO_URL:
        parts.append('Source and setup at <a href="{url}">{label}</a>'.format(
            url=escape(REPO_URL),
            label=escape(REPO_URL.replace("https://", "")),
        ))
    if LICENSE_URL and LICENSE_NAME:
        parts.append('licensed <a href="{url}">{name}</a>'.format(
            url=escape(LICENSE_URL), name=escape(LICENSE_NAME)))
    elif LICENSE_NAME:
        parts.append("licensed " + escape(LICENSE_NAME))
    repo_html = "<span>{}</span>".format(", ".join(parts)) if parts else ""

    html = TEMPLATE_PATH.read_text()
    replacements = {
        "{{TITLE}}": escape(f"{MOVIE_TITLE} Seat Watch"),
        "{{MOVIE}}": escape(MOVIE_TITLE),
        "{{POSTER}}": poster_html,
        "{{HEADLINE}}": escape(headline),
        "{{HEADLINE_CLASS}}": headline_class,
        "{{DETAIL}}": detail,
        "{{ROWS}}": "".join(rows),
        "{{ROWMAP}}": render_rowmap(state),
        "{{TARGET}}": escape(
            f"row {' or '.join(TARGET_ROWS)}, middle {CENTRE_SEATS} seats, "
            f"{PARTY_SIZE} together"
        ),
        "{{UPDATED}}": escape(now.strftime("%b %-d, %Y at %-I:%M %p %Z")),
        "{{UPDATED_ISO}}": escape(now.isoformat(timespec="seconds")),
        "{{SWEPT}}": escape(swept_text),
        "{{SWEPT_ISO}}": escape(swept_iso),
        "{{REPO}}": repo_html,
    }
    for token, value in replacements.items():
        html = html.replace(token, value)
    return html


def write_report(state, destination: Path):
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_report(state))

    # A machine-readable twin of the page, so the health check can see the
    # published state without parsing HTML.
    summary = summarise(state)
    summary["generated"] = datetime.now().astimezone().isoformat(timespec="seconds")
    swept = state.get("last_seat_sweep") or 0
    summary["swept"] = (
        datetime.fromtimestamp(swept).astimezone().isoformat(timespec="seconds")
        if swept else None
    )
    summary["movie"] = MOVIE_TITLE
    summary["target"] = {"rows": list(TARGET_ROWS), "centre": CENTRE_SEATS,
                         "party": PARTY_SIZE}
    (destination.parent / "status.json").write_text(
        json.dumps(summary, indent=1, sort_keys=True))
    log(f"status page written to {destination}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_checks(api, state, force: bool):
    alerts = []
    now = time.time()
    sweep_due = force or (now - state.get("last_seat_sweep", 0)) >= SEAT_SWEEP_INTERVAL

    # Newly published dates mean a whole auditorium just opened. That is the
    # one moment the wanted rows are actually free, so read the seat maps now
    # instead of waiting up to SEAT_SWEEP_INTERVAL for the next pass.
    found_new = check_dates(api, state, alerts, wide=sweep_due)
    if sweep_due or found_new:
        if found_new and not sweep_due:
            log("new dates found, reading seat maps now")
        check_seats(api, state, alerts)
    return alerts


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=f"Watch Fandango for well-placed seats to {MOVIE_TITLE}."
    )
    parser.add_argument("--once", action="store_true",
                        help="run every check now, ignoring the usual intervals")
    parser.add_argument("--dry-run", action="store_true",
                        help="log what would be sent without notifying")
    parser.add_argument("--status", action="store_true",
                        help="print the last known state and exit")
    parser.add_argument("--report", metavar="PATH", default=str(HERE / "docs" / "index.html"),
                        help="where to write the status page")
    parser.add_argument("--no-report", action="store_true",
                        help="skip writing the status page")
    parser.add_argument("--alerts-out", metavar="PATH",
                        help="write this run's alerts as JSON, for a notifier "
                             "to pick up (always written, even when empty)")
    args = parser.parse_args(argv)

    state = load_state(STATE_PATH)

    if args.status:
        summary = summarise(state)
        for entry in summary["theaters"]:
            print(f"{entry['name']}: through {entry['last_day']}, "
                  f"{entry['shows']} showtimes, {entry['free']} standard seats free, "
                  f"{entry['matched']} matching")
        for hit in summary["hits"]:
            print(f"  MATCH {' + '.join(hit['seats'])} {hit['day']} {hit['time']} "
                  f"{hit['theater']}")
        print(f"consecutive failures: {state.get('failures', 0)}")
        return 0

    lock = LOCK_PATH.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another run is still going, skipping")
        return 0

    api = Fandango()
    alerts = []
    try:
        alerts = run_checks(api, state, force=args.once)
    except MonitorError as exc:
        state["failures"] = state.get("failures", 0) + 1
        log(f"check failed ({state['failures']} in a row): {exc}")
        if state["failures"] >= FAILURE_ALERT_THRESHOLD and not state.get("failure_alerted"):
            state["failure_alerted"] = True
            alerts.append({
                "kind": "broken",
                "title": "Seat monitor is not working",
                "body": f"{state['failures']} failed runs in a row. Last error: {exc}",
                "url": "",
            })
    else:
        if state.get("failures"):
            log(f"recovered after {state['failures']} failed run(s)")
        state["failures"] = 0
        state["failure_alerted"] = False

    deliver(alerts, args.dry_run)

    if args.alerts_out:
        # Always written, so a notifier can tell "no alerts" from "the run
        # never got this far".
        Path(args.alerts_out).write_text(json.dumps(alerts, indent=1))
        log(f"{len(alerts)} alert(s) written to {args.alerts_out}")

    if not args.no_report:
        try:
            write_report(state, Path(args.report))
        except MonitorError as exc:
            log(f"could not write status page: {exc}")

    save_state(STATE_PATH, state)
    log(f"done, {api.requests} request(s), {len(alerts)} alert(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
