# fandango-monitor

[![Tests](https://github.com/edespino/fandango-monitor/actions/workflows/tests.yml/badge.svg)](https://github.com/edespino/fandango-monitor/actions/workflows/tests.yml)

Watches Fandango for a film's showtimes and tells you when two seats open up
in the part of the auditorium you actually want to sit in.

**Live status: https://edespino.github.io/fandango-monitor/** — rebuilt hourly,
showing seats free in each row at both theaters and whether anything currently
matches.

Built for a specific problem: *The Odyssey* is showing on 70mm IMAX film at two
Bay Area theaters, every seat from the fourth row back is sold, and the only
way to get a decent pair is to catch the moment new dates go on sale or someone
cancels.

It reads Fandango's public JSON endpoints. It does not buy anything.

## What it checks

The two checks cost very different amounts and are worth very different
things, so they run at different rates.

| Check | How often | Requests | Finds |
| --- | --- | --- | --- |
| Dates published past the last known showtime | hourly | ~8 | A whole week going on sale at once |
| Every seat map at every showtime | once a day at 8am PT, **and immediately whenever new dates appear** | ~150 | A cancellation in a sold-out row |

New dates trigger a seat sweep in the same run. A week going on sale is the
one moment the wanted rows are actually empty, and waiting hours for the next
scheduled pass would miss it — so that single alert can name the dates and the
seats together.

The first is the one that matters. When a theater loads its next week the
entire auditorium opens at once, and that window is short — so it is worth
checking often precisely because it is cheap. The second only turns up
cancellations in rows that are already fully sold, which is rare enough not to
justify the cost of running it at the same rate.

That works out to roughly 340 requests a day, spaced 1.5 seconds apart, with
new dates found within about an hour.

The sweep is on a wall clock rather than an interval, because the runner is UTC
and "once a day at 8am" should mean the reader's 8am. `SWEEP_HOUR` and
`SWEEP_TZ` set it. A day missed because the machine was off is swept on the
next run rather than skipped.

**The workflow has no schedule.** GitHub's cron produced nothing across eight
slots in eight hours here, at two different minutes, and the documentation is
explicit that scheduled runs may be dropped under load with no guarantee
offered. Rather than depend on that, the clock lives on a machine that is
always on: `imessage_watch.py` triggers a run whenever the last successful one
is over 55 minutes old. The seat sweep still gates itself on
`SWEEP_HOUR` regardless of how often a run is triggered.

The trade is plain. If that machine is off, nothing runs at all. Restoring a
`schedule:` block costs nothing and would act as a backstop if GitHub's
scheduler ever becomes reliable for this repo.

Fandango's `robots.txt` disallows `/napi/*`, so keep the frequency modest and
expect no cooperation if it is raised.

The first run is larger: it scans about 45 days at each theater to find where
the film's schedule currently ends.

## What counts as a match

Two seats side by side, both within the middle eight seats of the row, in a row
that theater's config asks for. Rows are lettered from the screen backward, so
row A is the front row.

| Theater | Rows | Wanted |
| --- | --- | --- |
| Regal Hacienda Crossings | A-I (9 rows) | F, G |
| AMC Metreon 16 | A-N, no row I (13 rows) | J, K, L, M |

The same letter is a different seat in each room, which is why the target is
per theater rather than global. A test asserts every configured letter actually
exists in that auditorium — a row that is not in the room can never match, and
nothing else would ever mention it.

Wheelchair spaces and companion seats are excluded. They report as available
but are not general seating, and there are enough of them at the edges of a row
to make an auditorium look far emptier than it is.

## Getting told

The scheduled run writes `alerts.json` and a workflow step sends it on, but
only if you give it somewhere to send. With no secrets set the step does
nothing, which is why a fresh clone is quiet.

**Never put a phone number in the repo.** These go in Settings → Secrets and
variables → Actions.

**If you configure nothing, it still tells you.** A find opens an issue on the
repo, which GitHub emails you and pushes to the GitHub mobile app. That needs
no account and no secrets. Everything below is an upgrade on it.

**Free text message, via your carrier's email gateway** — if your carrier
still runs one. Set `SMS_EMAIL` to your number at the carrier's domain, plus
`SMTP_USER` and `SMTP_PASS` for the mailbox to send from. Gmail needs an app
password rather than your login. `SMTP_HOST` defaults to `smtp.gmail.com`.

Carriers are shutting these down. Checked 2026-09-01:

| Carrier | Domain | State |
| --- | --- | --- |
| AT&T | `txt.att.net`, `mms.att.net` | **Gone** — no DNS records |
| Verizon | `vtext.com` | MX records present |
| T-Mobile | `tmomail.net` | MX records present |

Confirm yours resolves before relying on it — `dig +short MX <domain>` is
enough. A failed send warns and leaves the issue fallback to carry it.

**A real iMessage, from your Mac.** `imessage_watch.py` does three things
every 30 minutes, and never contacts Fandango:

1. Texts you when the published status changes — a seat pair appeared, or the
   dates extended.
2. **Runs the workflow.** It has no schedule of its own, so this is the clock:
   a run is triggered whenever the last successful one is over 55 minutes old.
   Actions still does every Fandango request; this only presses the button.

   Written as "how stale before running again" rather than a wall clock, so a
   missed tick, a failed run or a sleeping machine is caught at the next
   opportunity. A GitHub `schedule:` could be added back tomorrow without
   double running, since a recent success suppresses this.
3. Texts you when the pipeline has been dead for four hours. Silence otherwise
   looks exactly like "no seats yet", which is the failure that matters.

```sh
echo '+15551234567' > imessage.conf     # not tracked
python3 imessage_watch.py --test        # grant Automation access when asked
sed "s|REPO_PATH|$PWD|g" com.user.odyssey-imessage.plist \
  > ~/Library/LaunchAgents/com.user.odyssey-imessage.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.odyssey-imessage.plist
```

Runs every 30 minutes. Needs the Mac awake and Messages signed in, so pair it
with something cloud-side rather than relying on it alone. Remove it with
`launchctl bootout gui/$(id -u)/com.user.odyssey-imessage`.

**Pushcut**, for a native iPhone notification. Create a notification in the
app, then set either `PUSHCUT_URL` to its webhook URL, or `PUSHCUT_KEY` to
your API key with `PUSHCUT_NOTIFICATION` as its name.

The seat details ride in the JSON body, which Pushcut only applies on a Pro
subscription. On the free tier the notification still fires, worded however
you configured it in the app — so treat it as the nudge and read the details
in the issue it opens alongside.

**Free push, no account.** Install [ntfy](https://ntfy.sh), subscribe to a
topic nobody would guess, set `NTFY_TOPIC`. Anyone who knows a topic name can
read it, so treat it as unlisted rather than private.

**Slack.** Set `SLACK_WEBHOOK` to an incoming webhook URL.

**Paid SMS, via Twilio.** `TWILIO_SID`, `TWILIO_TOKEN`, `TWILIO_FROM` and
`ALERT_TO`. The most reliable option and the only one that costs money.

Any combination can be set at once; each configured provider gets a copy.
None are required for the status page.

## Alerts

Every alert is written to `hits.log`, sent onward by the workflow if a
provider is configured, and — when the monitor is run locally rather than from
the workflow — raised as a macOS notification with the ticketing link copied to
the clipboard:

- new dates published
- a showtime added to a date already on sale
- a matching seat pair opened
- the run looks like it is ending
- **the monitor itself is broken** — after eight consecutive failed runs

That last one matters. Silence is the normal state here, so a monitor that has
quietly died looks exactly like a monitor with nothing to report.

### If Fandango blocks us

`robots.txt` disallows `/napi/*`, and Akamai sits in front, so a block would
arrive as opaque 403s rather than a warning. Three things then happen, in this
order:

1. **The run exits non-zero** after three consecutive failures, so the workflow
   goes red and GitHub mails about it. One bad run is a blip and stays green.
2. **The page stops being republished.** A fresh timestamp over stale figures
   is worse than an old timestamp, because it makes a dead monitor look
   healthy. The page keeps its honest last-good time.
3. **The alert still goes out.** The notify and issue steps run on failure too,
   which is exactly when they matter.

The Mac notices independently, and texts rather than waiting:

- **A red run is texted straight away.** The monitor only exits non-zero after
  three consecutive failures, so red already means a sustained problem rather
  than one bad request. No need to wait out a timer.
- **Four hours with nothing succeeding** is texted too. That covers the other
  shape of failure, where runs stop happening at all rather than failing.

Both are sent once and reset on the next successful run, so a problem that
persists for a day does not text every thirty minutes.

## Running it

```sh
python3 fandango_monitor.py --once --dry-run   # full check, nothing sent
python3 fandango_monitor.py --status           # last known state
python3 fandango_monitor.py                    # one scheduled pass
python3 -m unittest discover -v                # tests
```

### Checking on it later

```sh
python3 healthcheck.py
```

Reports whether the driver is loaded, when a run last succeeded, whether the
published page is current, and where each theater's schedule ends. Exits
non-zero when something needs attention, so it also works from another script.

It reads local state and the published page only — no requests to Fandango,
so it is cheap to run as often as you like.

### How it is wired

One machine drives everything. The workflow has no schedule of its own, and
nothing runs unless the Mac triggers it.

```
launchd, every 30 min
  └─ imessage_watch.py
       ├─ last success over 55 min old?  → gh workflow run status.yml
       ├─ published status changed?      → iMessage
       └─ nothing succeeded in 4 hours?  → iMessage

GitHub Actions (triggered only)
  └─ status.yml → Fandango → docs/ → committed → Pages
```

Every Fandango request happens on the runner. The Mac only reads the published
page and the GitHub API, so it adds nothing to the request budget.

Install the driver:

```sh
echo '+15551234567' > imessage.conf     # not tracked
python3 imessage_watch.py --test        # grant Automation access when asked
sed "s|REPO_PATH|$PWD|g" com.user.odyssey-imessage.plist \
  > ~/Library/LaunchAgents/com.user.odyssey-imessage.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.odyssey-imessage.plist
```

Stop it with `launchctl bootout gui/$(id -u)/com.user.odyssey-imessage`.

**The obvious weakness:** if that machine is off, nothing runs at all, and the
thing that would tell you is on the same machine. That was a deliberate trade.
A `schedule:` block in `status.yml` would be a free backstop and could not
double-run, since a recent success suppresses the Mac's trigger — but GitHub's
scheduler fired nothing across eight slots here, so it was not worth pretending
otherwise.

### Status page

[`docs/index.html`](https://edespino.github.io/fandango-monitor/) shows where
things stand, with an Apple Maps link for each theater and a row-by-row chart
of where the empty seats actually are.

A find is drawn rather than listed: the matched pair appears inside its own
row, taken and free seats distinguished, placement given in seats either side
of centre, with a direct link to buy. The header's sprocket holes start moving
on a find and are still otherwise, so the state reads from across a room.
`preview.py` renders all of it without waiting for a theater to oblige. It is served from
GitHub Pages off `main` and `/docs`, so it can be handed to whoever you are
seeing the film with. (On a fork, turn that on under Settings → Pages.)

The workflow owns that file. Running `fandango_monitor.py` by hand rewrites it
too, so pass `--no-report` locally unless you mean to replace it.

### Looking at the page in states it is rarely in

```sh
python3 preview.py
```

The page is almost always in one state — nothing found — which makes the
others hard to check without waiting for a theater to do something. This
renders them all through the real template from the real seat-map fixtures,
into `docs/preview/`, published alongside the live page:

**https://edespino.github.io/fandango-monitor/preview/**

Every pair shown is one the monitor would genuinely alert on; the generator
refuses to place a pair outside the centre window rather than illustrate
something that could never happen. The pages are synthetic and say so.

### Running everything on one machine instead

To skip Actions entirely: delete `.github/workflows/status.yml`, install
`com.user.fandango-monitor.plist` in place of the driver above, and point it at
`publish.sh`, which regenerates the page and pushes when something changed. It
needs a remote you can push to without being prompted, and it puts all the
Fandango traffic on your own address.

## Watching something else

The configuration block at the top of `fandango_monitor.py` holds the film id,
the theaters, the format filter, and the seat preferences. Add a theater by
putting its code in `THEATERS`; the monitor watches exactly what is listed
there and nothing else.

Theater codes and film ids are visible in Fandango's own URLs — a theater page
ends in its code (`.../amc-metreon-16-aanem/theater-page` is `AANEM`), and a
film's id is the number in its page URL. To confirm a film id, fetch
`/napi/theaterMovieShowtimes/<THEATER>` and read the `movies` array.

## Layout

```
fandango_monitor.py   the monitor
template.html         status page template
test_monitor.py       tests
fixtures/             real API responses, used by the tests
healthcheck.py        is any of this actually running?
preview.py            render every page state for looking at
imessage_watch.py     the driver: runs the workflow, texts you
publish.sh            all-on-one-machine alternative to the workflow
docs/index.html       generated status page
```

## Tests

The fixtures are captured Fandango responses, not invented ones, so the tests
run against the shapes the code actually meets — including a seat map whose
only free seats in a row are wheelchair and companion spaces.

## License

Apache License 2.0. See [LICENSE](LICENSE).
