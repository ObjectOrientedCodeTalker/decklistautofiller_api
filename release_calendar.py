#!/usr/bin/env python3
"""
Decide when ygo_thumbs.py needs to run, based on TCG product release dates.

    python release_calendar.py fetch   # 1 request to Yugipedia -> release_calendar.json
    python release_calendar.py check   # offline: is today within a release window?
    python release_calendar.py info    # offline: write latest released set to info.json

`fetch` runs monthly. `check` and `info` make no web requests; they only read
release_calendar.json from the repo. In GitHub Actions, `check` writes
run=true/false to $GITHUB_OUTPUT.

Release dates come from Yugipedia (the earliest English release of each TCG
set), which lists products as soon as they're announced. YGOPRODeck only lists
a set once it has added some of its cards.

Standard library only (Python 3.8+).
"""

import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request

YUGIPEDIA_API = "https://yugipedia.com/api.php"
USER_AGENT = "ygo-thumb-downloader/1.0 (personal deckbuilder project)"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CALENDAR_PATH = os.path.join(SCRIPT_DIR, "release_calendar.json")
INFO_PATH = os.path.join(SCRIPT_DIR, "info.json")

DAYS_BEFORE = 5  # start updating this many days before a release...
DAYS_AFTER = 5   # ...and keep going this many days after it
# Keep a year of past releases so `info` can always name the latest one,
# even across long gaps between products.
HISTORY_DAYS = 365
STALE_AFTER_DAYS = 45  # warn if the monthly fetch seems to have stopped working


def today():
    return dt.datetime.now(dt.timezone.utc).date()


def load_calendar():
    with open(CALENDAR_PATH, encoding="utf-8") as f:
        return json.load(f)


def fetch():
    since = today() - dt.timedelta(days=HISTORY_DAYS)
    releases, offset = [], 0
    while True:
        query = (f"[[Page type::Set page]][[Medium::TCG]]"
                 f"[[English release date::>{since.isoformat()}]]"
                 f"|?English release date|?English set prefix"
                 f"|sort=English release date|limit=500|offset={offset}")
        url = YUGIPEDIA_API + "?" + urllib.parse.urlencode(
            {"action": "ask", "format": "json", "api_version": "3", "query": query})
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.load(resp)
        if "error" in data:
            raise RuntimeError(f"Yugipedia API error: {data['error']}")

        for row in data.get("query", {}).get("results", []):
            (name, page), = row.items()
            p = page["printouts"]
            if not p.get("English release date"):
                continue
            ts = int(p["English release date"][0]["timestamp"])
            date = dt.datetime.fromtimestamp(ts, dt.timezone.utc).date()
            prefixes = p.get("English set prefix") or []
            releases.append({"date": date.isoformat(),
                             "set_code": prefixes[0] if prefixes else None,
                             "set_name": name})

        nxt = data.get("query-continue-offset")
        if not nxt or nxt <= offset:
            break
        offset = nxt

    releases.sort(key=lambda r: (r["date"], r["set_name"]))
    calendar = {"fetched": today().isoformat(), "releases": releases}
    with open(CALENDAR_PATH, "w", encoding="utf-8") as f:
        json.dump(calendar, f, ensure_ascii=False, indent=1)
        f.write("\n")

    now = today()
    upcoming = [r for r in releases if dt.date.fromisoformat(r["date"]) >= now - dt.timedelta(days=DAYS_AFTER)]
    print(f"{len(releases)} TCG releases saved to {CALENDAR_PATH}; upcoming/recent:")
    for r in upcoming:
        print(f"  {r['date']}  {r['set_code'] or '-':6}  {r['set_name']}")


def check():
    calendar = load_calendar()
    now = today()
    age = (now - dt.date.fromisoformat(calendar["fetched"])).days
    if age > STALE_AFTER_DAYS:
        # ::warning:: shows up as an annotation on the GitHub Actions run.
        print(f"::warning::release_calendar.json is {age} days old; "
              f"the monthly calendar job may be failing.")

    active = []
    for r in calendar["releases"]:
        date = dt.date.fromisoformat(r["date"])
        if date - dt.timedelta(days=DAYS_BEFORE) <= now <= date + dt.timedelta(days=DAYS_AFTER):
            active.append(r)

    if active:
        for r in active:
            print(f"In release window: {r['date']}  {r['set_name']}")
    else:
        upcoming = [r for r in calendar["releases"] if dt.date.fromisoformat(r["date"]) > now]
        nxt = upcoming[0] if upcoming else None
        print("No release window today." + (
            f" Next: {nxt['set_name']} on {nxt['date']} (updates start "
            f"{dt.date.fromisoformat(nxt['date']) - dt.timedelta(days=DAYS_BEFORE)})."
            if nxt else ""))

    run = "true" if active else "false"
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"run={run}\n")
    return run


def latest_released(calendar, on_date):
    """Most recent set released on or before on_date that has a set code
    (skips promos like "...Premiere! promotional card", which have none)."""
    released = [r for r in calendar["releases"]
                if r["set_code"] and dt.date.fromisoformat(r["date"]) <= on_date]
    return released[-1] if released else None  # releases are sorted by date


def info():
    now = today()
    latest = latest_released(load_calendar(), now)
    if not latest:
        print("::warning::No released set with a set code in release_calendar.json; "
              "info.json left unchanged.")
        return

    try:
        with open(INFO_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    data["latest_set"] = f"{latest['set_code']} (Updated {now:%m/%d/%y})"
    with open(INFO_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"info.json latest_set = {data['latest_set']!r} ({latest['set_name']}, {latest['date']})")


if __name__ == "__main__":
    commands = {"fetch": fetch, "check": check, "info": info}
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in commands:
        sys.exit(__doc__)
    commands[cmd]()
