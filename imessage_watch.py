#!/usr/bin/env python3
"""Watch the published status from this Mac and text about it over iMessage.

Three jobs, all from a machine that is always on:

  1. Text when the published status changes - a seat pair appeared, or the
     dates extended.
  2. Run the workflow. It has no schedule of its own - GitHub's cron fired
     nothing across eight slots here - so this is the clock. Actions still
     does every Fandango request; this only presses the button.
  3. Say so when the pipeline is dead. Silence otherwise looks exactly like
     "no seats yet", which is the failure that matters.

Never contacts Fandango. Reads the published page and the GitHub API only.

The number or Apple ID to message goes in `imessage.conf` beside this file,
which is not tracked. One handle on the first line.

    python3 imessage_watch.py            # the scheduled behaviour
    python3 imessage_watch.py --test     # send a test message now
    python3 imessage_watch.py --dry-run  # report, send and trigger nothing
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = "edespino/fandango-monitor"
WORKFLOW = "status.yml"
STATUS_URL = "https://edespino.github.io/fandango-monitor/status.json"
PAGE_URL = "https://edespino.github.io/fandango-monitor/"

HANDLE_FILE = HERE / "imessage.conf"
SEEN_FILE = HERE / ".imessage-seen.json"

# This is the scheduler. The workflow has no cron of its own, because
# GitHub's fired nothing across eight slots here, so nothing runs unless
# this triggers it.
#
# Expressed as "how stale before running again" rather than a wall clock,
# which makes it self correcting: a missed tick, a failed run or a sleeping
# machine is caught at the next opportunity instead of waiting a full cycle.
# It also means a GitHub schedule could be added back tomorrow without
# double running, since a recent success suppresses this.
#
# Checked every 30 minutes by launchd, so 55 gives an hourly cadence: the
# tick at 60 minutes triggers, the one at 30 does not. Must stay above 30,
# or every tick would fire.
RUN_EVERY = timedelta(minutes=55)

# Shout when nothing has succeeded this long despite being triggered.
BROKEN_AFTER = timedelta(hours=4)


def gh_path() -> str | None:
    return shutil.which("gh") or next(
        (p for p in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh") if Path(p).exists()),
        None,
    )


def gh(*args, timeout=45):
    binary = gh_path()
    if not binary:
        return None
    result = subprocess.run([binary, *args], capture_output=True, text=True,
                            timeout=timeout)
    if result.returncode != 0:
        print(f"gh {' '.join(args)}: {result.stderr.strip()}", file=sys.stderr)
        return None
    return result.stdout


def handle() -> str:
    if not HANDLE_FILE.exists():
        sys.exit(f"No {HANDLE_FILE.name}. Put your phone number or Apple ID in it:\n"
                 f"  echo '+15551234567' > {HANDLE_FILE}")
    lines = [l.strip() for l in HANDLE_FILE.read_text().splitlines() if l.strip()]
    if not lines:
        sys.exit(f"{HANDLE_FILE.name} is empty")
    return lines[0]


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
        print("Grant access under System Settings > Privacy & Security > "
              "Automation, then run again.", file=sys.stderr)
    return False


def fetch_status():
    try:
        with urllib.request.urlopen(STATUS_URL, timeout=25) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"could not read published status: {exc}", file=sys.stderr)
        return None


def last_success() -> datetime | None:
    """When the workflow last completed successfully."""
    raw = gh("run", "list", "--repo", REPO, "--workflow", WORKFLOW,
             "--limit", "20", "--json", "conclusion,createdAt,status")
    if not raw:
        return None
    try:
        runs = json.loads(raw)
    except ValueError:
        return None
    done = [r["createdAt"] for r in runs if r.get("conclusion") == "success"]
    if not done:
        return None
    return datetime.fromisoformat(max(done).replace("Z", "+00:00"))


def anything_running() -> bool:
    raw = gh("run", "list", "--repo", REPO, "--workflow", WORKFLOW,
             "--limit", "5", "--json", "status")
    if not raw:
        return False
    try:
        return any(r.get("status") in ("queued", "in_progress")
                   for r in json.loads(raw))
    except ValueError:
        return False


def interesting(status) -> dict:
    """The parts worth texting about, ignoring timestamps that always move."""
    return {
        "hits": [f"{h['theater']}|{h['day']}|{h['time']}|{'+'.join(h['seats'])}"
                 for h in status.get("hits") or []],
        "last_day": {t["code"]: t["last_day"] for t in status.get("theaters") or []},
    }


def describe(now: dict, before: dict, names: dict | None = None) -> str | None:
    """Compose the text. Theater codes never appear: they mean nothing to
    someone reading this on a phone."""
    names = names or {}
    lines = []
    for hit in [h for h in now["hits"] if h not in (before.get("hits") or [])]:
        theater, day, time, seats = hit.split("|")
        lines.append(f"SEATS {seats} — {theater}, {day} {time}")
    for code, day in now["last_day"].items():
        was = (before.get("last_day") or {}).get(code)
        if was and day > was:
            lines.append(f"New dates at {names.get(code, code)}: "
                         f"now through {day} (was {was})")
    return "\n".join(lines) if lines else None


def load_seen() -> dict:
    if SEEN_FILE.exists():
        try:
            stored = json.loads(SEEN_FILE.read_text())
            if "interesting" in stored:
                return stored
            # Earlier format kept the payload at the top level.
            return {"interesting": stored, "broken_alerted": False}
        except ValueError:
            pass
    return {"interesting": {}, "broken_alerted": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", action="store_true", help="send a test message")
    parser.add_argument("--dry-run", action="store_true",
                        help="report only; send nothing and trigger nothing")
    args = parser.parse_args()

    to = handle()

    if args.test:
        text = "Odyssey seat watch: test message. Alerting works."
        print(text)
        return 0 if args.dry_run or send(text, to) else 1

    seen = load_seen()
    messages = []

    # --- keep the workflow alive -------------------------------------------
    success = last_success()
    now = datetime.now(timezone.utc)
    if success is None:
        print("no successful workflow run found")
    else:
        age = now - success
        print(f"last successful run {int(age.total_seconds() // 60)} min ago")
        if age > RUN_EVERY and not anything_running():
            if args.dry_run:
                print("would trigger a run")
            elif gh("workflow", "run", WORKFLOW, "--repo", REPO) is not None:
                print("triggered a run")
            else:
                print("could not trigger a run", file=sys.stderr)
        if age > BROKEN_AFTER and not seen.get("broken_alerted"):
            messages.append(
                f"Seat monitor has not run successfully in "
                f"{int(age.total_seconds() // 3600)}h. It may be stuck.")
            seen["broken_alerted"] = True
        elif age <= BROKEN_AFTER:
            seen["broken_alerted"] = False

    # --- report real news ---------------------------------------------------
    status = fetch_status()
    if status is not None:
        current = interesting(status)
        names = {t["code"]: t["name"] for t in status.get("theaters") or []}
        change = describe(current, seen.get("interesting") or {}, names)
        if change:
            messages.append(f"The Odyssey\n{change}\n{PAGE_URL}")
        seen["interesting"] = current

    # Record before sending, so a failed send cannot repeat forever.
    if not args.dry_run:
        SEEN_FILE.write_text(json.dumps(seen, indent=1, sort_keys=True))

    if not messages:
        print("nothing to say")
        return 0

    ok = True
    for text in messages:
        print(text)
        if not args.dry_run:
            ok = send(text, to) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
