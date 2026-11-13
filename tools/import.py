#!/usr/bin/env python3
"""Import EiBi's schedule CSV and convert it into Havoc Radio's frequency guide table.

usage: import.py [--repo DIR] [--now ISO8601]

One conditional GET of http://www.eibispace.de/dx/sked-<season>.csv, the season chosen by the UTC date. On 304
nothing is downloaded. On 200 the CSV is converted with tools/guide_table.py and checked; when
every check passes, the CSV, v1/stations.bin, v1/manifest.json and state.json are committed together. When a check
fails, nothing is committed and the exit status is 1. When nothing is committed and the last commit is more than
30 days old, state.json is committed with the new check time, so that GitHub keeps the schedule enabled.

Only the Python standard library is used. The commit is made with the repository's configured git identity; the
workflow pushes it.
"""
import argparse
import datetime
import email.utils
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

BASE_URL = "http://www.eibispace.de/dx/"
USER_AGENT = "havoc-radio-guide (+https://github.com/hoverboardhavoc/havoc-radio-guide)"
MIN_ENTRIES, MAX_ENTRIES = 1000, 30000
KEEPALIVE = datetime.timedelta(days=30)
WORK_DIR = ".import"
STATE = "state.json"
TABLE = "v1/stations.bin"
MANIFEST = "v1/manifest.json"
CONVERTER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "guide_table.py")


class CheckFailed(Exception):
    pass


# ---- seasons ----

def last_sunday(year, month):
    """The last Sunday of a month (March or October)."""
    last = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)
    return last - datetime.timedelta(days=(last.weekday() - 6) % 7)


def season_for(day):
    """EiBi's season for a UTC date: Ayy from the last Sunday of March, Byy from the last Sunday of October. Byy
    runs into the next year with the starting year's yy."""
    y = day.year
    if day >= last_sunday(y, 10):
        return f"B{y % 100:02d}"
    if day >= last_sunday(y, 3):
        return f"A{y % 100:02d}"
    return f"B{(y - 1) % 100:02d}"


def csv_path(season):
    return f"eibi/sked-{season.lower()}.csv"


# ---- HTTP ----

def http_fetch(url, headers, timeout=60):
    """(status, headers with lower-case names, body) of one GET. The body is empty for anything but 200."""
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, b""


# ---- helpers ----

def iso(t):
    return t.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def load_state(repo):
    path = os.path.join(repo, STATE)
    if not os.path.exists(path):
        return {"season": None, "etag": None, "last_modified": None, "checked": None}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_state(repo, state):
    with open(os.path.join(repo, STATE), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def load_converter():
    spec = importlib.util.spec_from_file_location("guide_table", CONVERTER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_converter(*args):
    p = subprocess.run([sys.executable, CONVERTER, *args], capture_output=True, text=True)
    sys.stdout.write(p.stdout)
    sys.stderr.write(p.stderr)
    return p.returncode


# ---- import ----

def convert_and_commit(repo, state, season, headers, body, now, bounds):
    """Convert and check a downloaded CSV; when every check passes, commit it with the table."""
    work = os.path.join(repo, WORK_DIR)
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    try:
        new_csv = csv_path(season)
        staged = os.path.join(work, os.path.basename(new_csv))
        table = os.path.join(work, "stations.bin")
        manifest = os.path.join(work, "manifest.json")
        with open(staged, "wb") as f:
            f.write(body)
        modified = headers.get("last-modified") or email.utils.format_datetime(now, usegmt=True)
        if run_converter(staged, "-o", table, "--season", season, "--modified", modified,
                         "--manifest", manifest, "--csv-name", new_csv) != 0:
            raise CheckFailed("the converter reported errors")
        with open(manifest, encoding="utf-8") as f:
            entries = json.load(f)["entries"]
        if not bounds[0] <= entries <= bounds[1]:
            raise CheckFailed(f"{entries} entries, not {bounds[0]} to {bounds[1]}")
        if run_converter("--verify", table) != 0:
            raise CheckFailed("the table failed --verify")

        conv = load_converter()
        new_entries = set(conv.parse_csv(body)[0])
        old_csv = csv_path(state["season"]) if state.get("season") else None
        change = ""
        if old_csv and os.path.exists(os.path.join(repo, old_csv)):
            with open(os.path.join(repo, old_csv), "rb") as f:
                old_entries = set(conv.parse_csv(f.read())[0])
            change = f" (+{len(new_entries - old_entries):,}, -{len(old_entries - new_entries):,})"
            if old_csv != new_csv:
                git(repo, "mv", old_csv, new_csv)

        os.makedirs(os.path.join(repo, "eibi"), exist_ok=True)
        os.makedirs(os.path.join(repo, "v1"), exist_ok=True)
        with open(os.path.join(repo, new_csv), "wb") as f:
            f.write(body)
        shutil.copyfile(table, os.path.join(repo, TABLE))
        shutil.copyfile(manifest, os.path.join(repo, MANIFEST))
        write_state(repo, {"season": season, "etag": headers.get("etag"),
                           "last_modified": headers.get("last-modified"), "checked": iso(now)})
        git(repo, "add", "--", new_csv, TABLE, MANIFEST, STATE)
        date = email.utils.parsedate_to_datetime(modified)
        message = f"EiBi {season} of {date.day} {date:%b %Y}: {entries:,} entries{change}"
        git(repo, "commit", "-q", "-m", message)
        return message
    finally:
        shutil.rmtree(work, ignore_errors=True)


def keepalive(repo, state, now):
    """Commit state.json with the new check time when the last commit is more than 30 days old."""
    last = datetime.datetime.fromtimestamp(int(git(repo, "log", "-1", "--format=%ct")), datetime.timezone.utc)
    if now - last <= KEEPALIVE:
        return None
    state = dict(state, checked=iso(now))
    write_state(repo, state)
    git(repo, "add", "--", STATE)
    message = f"EiBi {state['season']} checked {iso(now)}: no change"
    git(repo, "commit", "-q", "-m", message)
    return message


def run(repo, now, fetch=http_fetch, bounds=(MIN_ENTRIES, MAX_ENTRIES)):
    """One import. Returns a line describing the outcome; raises CheckFailed when nothing may be committed."""
    state = load_state(repo)
    current = state.get("season")
    season = season_for(now.astimezone(datetime.timezone.utc).date())
    new_season = season != current
    headers = {"User-Agent": USER_AGENT}
    if not new_season:
        if state.get("etag"):
            headers["If-None-Match"] = state["etag"]
        if state.get("last_modified"):
            headers["If-Modified-Since"] = state["last_modified"]
    url = BASE_URL + f"sked-{season.lower()}.csv"
    status, resp_headers, body = fetch(url, headers)
    if status == 200 and body:
        return convert_and_commit(repo, state, season, resp_headers, body, now, bounds)
    if status == 304 and not new_season:
        outcome = f"EiBi {season}: not modified"
    elif status == 404 and new_season and current:
        outcome = f"EiBi {season} not published yet, {current} kept"
    else:
        raise CheckFailed(f"GET {url}: HTTP {status}, {len(body)} bytes")
    kept = keepalive(repo, state, now)
    return f"{outcome}; committed: {kept}" if kept else outcome


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--now", help="the time to act as, ISO 8601 (default: the current time)")
    a = ap.parse_args(argv)
    now = (datetime.datetime.fromisoformat(a.now.replace("Z", "+00:00")) if a.now
           else datetime.datetime.now(datetime.timezone.utc))
    try:
        print(run(a.repo, now))
    except CheckFailed as e:
        print(f"import failed, nothing committed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
