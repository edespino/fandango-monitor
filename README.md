# fandango-monitor

[![Tests](https://github.com/edespino/fandango-monitor/actions/workflows/tests.yml/badge.svg)](https://github.com/edespino/fandango-monitor/actions/workflows/tests.yml)

Watches Fandango for a film's showtimes and tells you when two seats open up
in the part of the auditorium you actually want to sit in.

**Live status: https://edespino.github.io/fandango-monitor/** — rebuilt every
two hours, showing seats free in each row at both theaters and whether anything
currently matches.

Built for a specific problem: *The Odyssey* is showing on 70mm IMAX film at two
Bay Area theaters, every seat from the fourth row back is sold, and the only
way to get a decent pair is to catch the moment new dates go on sale or someone
cancels.

It reads Fandango's public JSON endpoints. It does not buy anything.

## What it checks

| Check | Requests |
| --- | --- |
| Dates published beyond the last known showtime | ~4 |
| Every seat map at every showtime | ~120 |

Runs a few times a day from a scheduled workflow, weighted toward the midweek
window when chains usually load the coming week. That is an estimate rather
than a documented publishing time, and the cron comments say so.

About 400 requests a day, spaced 1.5 seconds apart. Fandango's `robots.txt`
disallows `/napi/*`, so keep the frequency modest and expect no cooperation if
it is raised.

The first run is larger: it scans about 45 days at each theater to find where
the film's schedule currently ends.

## What counts as a match

Two seats, side by side, both in row F or G, both within the middle eight seats
of the row. Rows are lettered from the screen backward, so row A is the front
row and G is seven rows back.

Wheelchair spaces and companion seats are excluded. They report as available
but are not general seating, and there are enough of them at the edges of a row
to make an auditorium look far emptier than it is.

## Alerts

macOS notification with sound, repeated three times, written to `hits.log`,
with the ticketing link copied to the clipboard:

- new dates published
- a showtime added to a date already on sale
- a matching seat pair opened
- the run looks like it is ending
- **the monitor itself is broken** — after eight consecutive failed runs

That last one matters. Silence is the normal state here, so a monitor that has
quietly died looks exactly like a monitor with nothing to report.

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

Reports whether the scheduler is loaded and running on time, how long since
the seat maps were read, whether any run has failed, where each theater's
schedule currently ends, whether the published page is current, and whether
the two-hourly rebuild has actually been firing. Exits non-zero when something
needs attention, so it also works from another script.

It reads local state and the published page only — no requests to Fandango,
so it is cheap to run as often as you like.

### Installing the scheduler

```sh
sed "s|REPO_PATH|$PWD|g" com.user.fandango-monitor.plist \
  > ~/Library/LaunchAgents/com.user.fandango-monitor.plist
launchctl load ~/Library/LaunchAgents/com.user.fandango-monitor.plist
```

To stop it:

```sh
launchctl unload ~/Library/LaunchAgents/com.user.fandango-monitor.plist
```

### Status page

[`docs/index.html`](https://edespino.github.io/fandango-monitor/) shows where
things stand, with an Apple Maps link for each theater. It is served from
GitHub Pages off `main` and `/docs`, so it can be handed to whoever you are
seeing the film with. (On a fork, turn that on under Settings → Pages.)

The page has one owner, so the two schedulers do not fight over it:

- **GitHub Actions** (`.github/workflows/status.yml`) rebuilds and commits it
  every two hours. This runs whether or not your laptop is awake.
- **The launchd job** runs with `--no-report` and only sends notifications.
  Without that flag it would rewrite the file on every run and leave the
  working tree permanently dirty.

If you would rather not use Actions, delete that workflow, drop `--no-report`
from the plist, and point launchd at `publish.sh` instead — it regenerates the
page and pushes only when something changed. It needs a remote you can push to
without being prompted.

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
publish.sh            regenerate the page and push it
docs/index.html       generated status page
```

## Tests

The fixtures are captured Fandango responses, not invented ones, so the tests
run against the shapes the code actually meets — including a seat map whose
only free seats in a row are wheelchair and companion spaces.

## License

Apache License 2.0. See [LICENSE](LICENSE).
