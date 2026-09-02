#!/usr/bin/env python3
"""Report whether the monitor is actually running and the page is current.

Reads local state and, where the tools are available, the scheduled job and
the published page. Makes no requests to Fandango, so it is cheap to run as
often as you like.

    python3 healthcheck.py
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fandango_monitor as fm  # noqa: E402

# This setup runs entirely from the scheduled workflow. Set to True if you
# also install the launchd agent, and the local checks below switch back on.
LOCAL_SCHEDULER = False

LABEL = "com.user.fandango-monitor"
REPO = fm.REPO_URL.replace("https://github.com/", "") if fm.REPO_URL else ""
PAGE = "https://edespino.github.io/fandango-monitor/"
STATUS_JSON = PAGE + "status.json"

# How late something has to be before it is worth mentioning.
RUN_GRACE = timedelta(minutes=35)     # runs every 15 min
SWEEP_GRACE = timedelta(hours=10)     # a few scheduled runs a day
PAGE_GRACE = timedelta(hours=10)      # a few runs a day, cron drifts

OK, WARN, BAD, INFO = "  ok  ", " warn ", " FAIL ", "      "
problems = []


def line(mark, text, detail=""):
    print(f"[{mark}] {text}" + (f"\n{INFO}  {detail}" if detail else ""))
    if mark in (WARN, BAD):
        problems.append(text)


def run(cmd, **kw):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=25, **kw)
    except (OSError, subprocess.SubprocessError):
        return None


def ago(when: datetime) -> str:
    delta = datetime.now(timezone.utc) - when.astimezone(timezone.utc)
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes / 60
    return f"{hours:.1f} hours ago" if hours < 48 else f"{hours / 24:.1f} days ago"


def check_agent():
    if not shutil.which("launchctl"):
        return
    uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
    result = run(["launchctl", "print", f"gui/{uid}/{LABEL}"])
    if not result or result.returncode != 0:
        line(BAD, "Launch agent is not loaded",
             f"launchctl bootstrap gui/{uid} ~/Library/LaunchAgents/{LABEL}.plist")
        return
    text = result.stdout
    runs = re.search(r"runs = (\d+)", text)
    code = re.search(r"last exit code = (\S+)", text)
    runs = int(runs.group(1)) if runs else 0
    code = code.group(1) if code else "?"
    if code not in ("0", "(never", "(never exited)"):
        line(BAD, f"Last run exited {code}", "check launchd.err.log")
    else:
        line(OK, f"Launch agent loaded, {runs} run(s), last exit {code}")


def check_recent_run():
    if not fm.LOG_PATH.exists():
        line(WARN, "No monitor.log yet — has it ever run?")
        return
    stamps = [
        datetime.fromisoformat(m.group(1)).astimezone()
        for m in re.finditer(r"^(\S+)\s+done,", fm.LOG_PATH.read_text(), re.M)
    ]
    if not stamps:
        line(WARN, "monitor.log has no completed runs")
        return
    last = stamps[-1]
    late = datetime.now(timezone.utc) - last.astimezone(timezone.utc) > RUN_GRACE
    line(WARN if late else OK,
         f"Last check {ago(last)}" + (" — overdue, expected every 15 min" if late else ""))


def check_state():
    if not fm.STATE_PATH.exists():
        line(WARN, "No state.json yet — the first run has not finished")
        return
    state = fm.load_state(fm.STATE_PATH)

    failures = state.get("failures", 0)
    if failures >= fm.FAILURE_ALERT_THRESHOLD:
        line(BAD, f"{failures} failed runs in a row — the monitor is broken")
    elif failures:
        line(WARN, f"{failures} failed run(s) in a row")
    else:
        line(OK, "No failed runs")

    swept = state.get("last_seat_sweep") or 0
    if swept:
        when = datetime.fromtimestamp(swept).astimezone()
        stale = datetime.now(timezone.utc) - when.astimezone(timezone.utc) > SWEEP_GRACE
        line(WARN if stale else OK, f"Seat maps last read {ago(when)}")
    else:
        line(WARN, "Seat maps have never been read")

    summary = fm.summarise(state)
    for entry in summary["theaters"]:
        line(INFO, f"{entry['name']}: through {entry['last_day']}, "
                   f"{entry['shows']} showtimes, "
                   f"{entry['per_show']} of {entry['capacity']} free per show")

    if summary["hits"]:
        print()
        for hit in summary["hits"]:
            line(OK, f"SEATS: {' + '.join(hit['seats'])} — "
                     f"{hit['theater']}, {hit['day']} {hit['time']}")
    else:
        line(INFO, f"No pair matching rows {'/'.join(fm.TARGET_ROWS)} yet")


def check_schedule():
    if not REPO or not shutil.which("gh"):
        return
    result = run(["gh", "run", "list", "--repo", REPO, "--workflow", "status.yml",
                  "--limit", "5", "--json", "event,conclusion,createdAt"])
    if not result or result.returncode != 0:
        line(INFO, "Could not read workflow runs (gh not authenticated?)")
        return
    try:
        runs = json.loads(result.stdout)
    except ValueError:
        return
    scheduled = [r for r in runs if r.get("event") == "schedule"]
    if not scheduled:
        line(WARN, "The scheduled workflow has never fired on its own",
             "only manual runs so far; if this persists the page will go stale")
        return
    newest = max(scheduled, key=lambda r: r["createdAt"])
    when = datetime.fromisoformat(newest["createdAt"].replace("Z", "+00:00"))
    bad = newest.get("conclusion") not in (None, "success")
    line(BAD if bad else OK,
         f"Scheduled page rebuild {ago(when)} ({newest.get('conclusion')})")


def check_published_state():
    """Read the summary the workflow publishes beside the page."""
    try:
        with urllib.request.urlopen(STATUS_JSON, timeout=20) as response:
            data = json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        line(WARN, f"Could not read published status.json: {exc}")
        return
    for entry in data.get("theaters", []):
        free = f"{entry['per_show']}"
        if entry.get("capacity"):
            free += f" of {entry['capacity']}"
        line(INFO, f"{entry['name']}: through {entry['last_day']}, "
                   f"{entry['shows']} showtimes, {free} free per show")
    hits = data.get("hits") or []
    if hits:
        print()
        for hit in hits:
            line(OK, f"SEATS: {' + '.join(hit['seats'])} — "
                     f"{hit['theater']}, {hit['day']} {hit['time']}")
    else:
        target = data.get("target", {})
        rows = "/".join(target.get("rows", []))
        line(INFO, f"No pair matching rows {rows} yet")


def check_page():
    try:
        with urllib.request.urlopen(PAGE, timeout=20) as response:
            html = response.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        line(BAD, f"Status page unreachable: {exc}")
        return
    found = re.search(r'<time class="ts mono" datetime="([^"]+)"', html)
    if not found:
        line(WARN, "Status page has no timestamp — is it the right page?")
        return
    when = datetime.fromisoformat(found.group(1))
    stale = datetime.now(timezone.utc) - when.astimezone(timezone.utc) > PAGE_GRACE
    line(WARN if stale else OK,
         f"Published page built {ago(when)}" + (" — stale" if stale else ""),
         PAGE if stale else "")


def main():
    print(f"{fm.MOVIE_TITLE} seat monitor — {datetime.now().strftime('%b %-d, %-I:%M %p')}\n")
    if LOCAL_SCHEDULER:
        print("Local")
        check_agent()
        check_recent_run()
        check_state()
        print("\nPublished")
    else:
        print("Published (this setup runs from the workflow only)")
    check_schedule()
    check_page()
    check_published_state()

    print()
    if problems:
        print(f"{len(problems)} thing(s) need attention:")
        for item in problems:
            print(f"  - {item}")
        return 1
    print("Everything healthy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
