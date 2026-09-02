#!/usr/bin/env python3
"""Text yourself when the published status changes.

Reads the status.json that the scheduled workflow publishes and sends an
iMessage when something worth knowing has changed. It never touches
Fandango, so it adds nothing to the request budget the workflow is keeping
down: one request to GitHub Pages per run.

The number or Apple ID to message goes in `imessage.conf` beside this file,
which is not tracked. Put one handle on the first line.

    python3 imessage_watch.py            # send if something changed
    python3 imessage_watch.py --test     # send a test message now
    python3 imessage_watch.py --dry-run  # print, do not send
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATUS_URL = "https://edespino.github.io/fandango-monitor/status.json"
HANDLE_FILE = HERE / "imessage.conf"
SEEN_FILE = HERE / ".imessage-seen.json"


def handle() -> str:
    if not HANDLE_FILE.exists():
        sys.exit(f"No {HANDLE_FILE.name}. Put your phone number or Apple ID in it:\n"
                 f"  echo '+15551234567' > {HANDLE_FILE}")
    value = HANDLE_FILE.read_text().strip().splitlines()
    if not value or not value[0].strip():
        sys.exit(f"{HANDLE_FILE.name} is empty")
    return value[0].strip()


def send(text: str, to: str) -> bool:
    """Send one iMessage. Two dialects, because the working one varies."""
    literal = text.replace("\\", "\\\\").replace('"', '\\"')
    target = to.replace("\\", "\\\\").replace('"', '\\"')
    scripts = [
        f'tell application "Messages"\n'
        f'  set svc to 1st account whose service type = iMessage\n'
        f'  send "{literal}" to participant "{target}" of svc\n'
        f'end tell',
        f'tell application "Messages"\n'
        f'  set svc to 1st service whose service type = iMessage\n'
        f'  send "{literal}" to buddy "{target}" of svc\n'
        f'end tell',
    ]
    last = ""
    for script in scripts:
        result = subprocess.run(["osascript", "-e", script],
                                capture_output=True, text=True)
        if result.returncode == 0:
            return True
        last = result.stderr.strip()
    print(f"could not send: {last}", file=sys.stderr)
    if "Not authorized" in last or "-1743" in last:
        print("Grant permission under System Settings > Privacy & Security > "
              "Automation, then run again.", file=sys.stderr)
    return False


def fetch():
    try:
        with urllib.request.urlopen(STATUS_URL, timeout=25) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"could not read published status: {exc}", file=sys.stderr)
        return None


def interesting(status) -> dict:
    """The parts worth texting about, ignoring timestamps that always move."""
    return {
        "hits": [f"{h['theater']}|{h['day']}|{h['time']}|{'+'.join(h['seats'])}"
                 for h in status.get("hits") or []],
        "last_day": {t["code"]: t["last_day"] for t in status.get("theaters") or []},
    }


def describe(now: dict, before: dict) -> str | None:
    lines = []
    fresh = [h for h in now["hits"] if h not in (before.get("hits") or [])]
    for hit in fresh:
        theater, day, time, seats = hit.split("|")
        lines.append(f"SEATS {seats} — {theater}, {day} {time}")
    for code, day in now["last_day"].items():
        was = (before.get("last_day") or {}).get(code)
        if was and day > was:
            lines.append(f"New dates at {code}: now through {day} (was {was})")
    return "\n".join(lines) if lines else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", action="store_true", help="send a test message")
    parser.add_argument("--dry-run", action="store_true", help="print, do not send")
    args = parser.parse_args()

    to = handle()

    if args.test:
        text = "Odyssey seat watch: test message. Alerting works."
        print(text)
        return 0 if args.dry_run or send(text, to) else 1

    status = fetch()
    if status is None:
        return 1

    now = interesting(status)
    before = {}
    if SEEN_FILE.exists():
        try:
            before = json.loads(SEEN_FILE.read_text())
        except ValueError:
            pass

    message = describe(now, before)

    # Record first, so a send failure cannot cause the same alert forever.
    SEEN_FILE.write_text(json.dumps(now, indent=1, sort_keys=True))

    if not message:
        print("nothing new")
        return 0
    text = f"The Odyssey\n{message}\nhttps://edespino.github.io/fandango-monitor/"
    print(text)
    if args.dry_run:
        return 0
    return 0 if send(text, to) else 1


if __name__ == "__main__":
    sys.exit(main())
