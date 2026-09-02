#!/usr/bin/env python3
"""Render the status page in every state, for looking at.

The page is normally in one state - nothing found - which makes the other
states hard to check without waiting for a theater to do something. This
builds them all from the real seat-map fixtures and the real template, so
what you see is what the live page will do.

    python3 preview.py      # writes docs/preview/

Everything here is synthetic. Each page says so.
"""

from __future__ import annotations

import copy
import json
import random
from datetime import date, timedelta
from pathlib import Path

import fandango_monitor as fm

HERE = Path(__file__).resolve().parent
OUT = HERE / "docs" / "preview"
FIXTURES = HERE / "fixtures"

BANNER = (
    '<p style="margin:0;padding:.55rem 1rem;background:#96331F;color:#F2EDE3;'
    'font:600 .78rem/1.3 ui-monospace,monospace;letter-spacing:.08em;'
    'text-transform:uppercase;text-align:center">'
    'Preview &mdash; synthetic data, not real availability</p>'
)


def base_state():
    state = json.loads(json.dumps(fm.EMPTY_STATE))
    live = HERE / "state.json"
    if live.exists():
        state.update(json.loads(live.read_text()))
    state["availability"] = {}
    return state


def seat_row_with(fixture, row, indices, extra_free=0, seed=7):
    """A seat map where `indices` are free and the rest of the row is taken."""
    seatmap = json.loads((FIXTURES / fixture).read_text())
    seats = fm.bookable_row(seatmap, row)
    for seat in seats:
        seat["status"] = "R"
    for i in indices:
        seats[i]["status"] = "A"
    if extra_free:
        random.seed(seed)
        for seat in random.sample(seats, extra_free):
            if seat["status"] != "A":
                seat["status"] = "A"
    return seatmap, seats


def hit(state, theater, fixture, row, index, when, time, extra_free=6):
    seatmap, _ = seat_row_with(fixture, row, (index, index + 1), extra_free)
    groups = fm.find_groups(seatmap, (row,), centre=fm.CENTRE_SEATS)
    if not groups:
        raise SystemExit(
            f"{row}[{index}] is outside the centre-{fm.CENTRE_SEATS} window, so "
            "the monitor would never alert on it. Pick a seat it would match.")
    group = groups[0]
    state["availability"].setdefault(theater, {})[when] = {time: {
        "sold_out": False,
        "free": sum(1 for s in seatmap["seats"]
                    if s["type"] == "standard" and s["status"] == "A"),
        "match": group["seats"],
        "url": "https://tickets.fandango.com/transaction/ticketing/mobile/"
               "jump.aspx?preview=1",
        "strip": fm.row_strip(seatmap, group),
    }}
    return state


def write(name, state, title):
    html = fm.render_report(state).replace("<body>", "<body>" + BANNER, 1)
    html = html.replace("<title>", f"<title>{title} — ", 1)
    (OUT / name).write_text(html)
    return name


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    soon = (date.today() + timedelta(days=16)).isoformat()
    pages = []

    pages.append(("Waiting — nothing found",
                  write("waiting.html", base_state(), "Waiting")))

    s = hit(base_state(), "AANEM", "seatmap_metreon_g.json", "K", 16, soon, "6:00 PM")
    pages.append(("One pair, dead centre", write("hit-centre.html", s, "Centre")))

    # Still inside the centre window - anything wider would never alert.
    s = hit(base_state(), "AANEM", "seatmap_metreon_g.json", "L", 13, soon, "9:15 PM")
    pages.append(("One pair, off centre but still matching",
                  write("hit-edge.html", s, "Off centre")))

    s = base_state()
    s = hit(s, "AANEM", "seatmap_metreon_g.json", "K", 16, soon, "6:00 PM")
    s = hit(s, "AAOPK", "seatmap_hacienda_g.json", "G", 15, soon, "6:10 PM")
    pages.append(("Two pairs, both theaters", write("hit-multiple.html", s, "Two pairs")))

    s = base_state()
    s["rows"] = {}
    s["capacity"] = {}
    pages.append(("Before the first sweep — no row data",
                  write("cold.html", s, "Cold start")))

    frames = "".join(
        '<section><h2>{label}</h2>'
        '<iframe src="{src}" title="{label}" loading="lazy"></iframe>'
        '<p><a href="{src}">open on its own</a></p></section>'.format(
            label=label, src=src)
        for label, src in pages
    )
    (OUT / "index.html").write_text(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Status page states</title>
<style>
  :root{{color-scheme:light dark}}
  body{{margin:0;padding:2rem 1.25rem 4rem;background:#14110E;color:#E9E3D7;
       font:15px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}}
  .wrap{{max-width:70rem;margin:0 auto;display:flex;flex-direction:column;gap:2.5rem}}
  h1{{font-size:1.2rem;letter-spacing:.12em;text-transform:uppercase;margin:0}}
  .note{{color:#97A0A3;margin:.4rem 0 0;max-width:44rem;font-size:.85rem}}
  h2{{font-size:.72rem;letter-spacing:.14em;text-transform:uppercase;
     color:#E0A055;margin:0 0 .6rem}}
  iframe{{width:100%;height:44rem;border:1px solid #332C26;background:#F2EDE3}}
  a{{color:#E0A055}}
  p{{margin:.5rem 0 0;font-size:.8rem}}
</style></head>
<body><div class="wrap">
<header>
  <h1>Status page states</h1>
  <p class="note">Every state the page can be in, built from the real seat-map
  fixtures through the real template. Synthetic throughout: no theater has
  these seats. Regenerate with <code>python3 preview.py</code>.</p>
</header>
{frames}
</div></body></html>""")
    print(f"wrote {len(pages) + 1} files to {OUT}")
    for label, src in pages:
        print(f"  {src:<20} {label}")


if __name__ == "__main__":
    main()
