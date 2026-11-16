#!/usr/bin/env python3
"""
backfill_settlements.py -- fill in settlement results the recorder missed.

WHY THIS EXISTS: the recorder sweeps settlements 20 seconds after an event
closes, but Kalshi often hasn't settled by then, so `result` comes back
blank. Without settlements, tune_parameters.py has nothing to score against.

This script re-queries every event in the database that has no settled
result yet and fills them in. Safe to re-run as often as you like -- it only
touches rows that are still blank.

    python3 scripts/backfill_settlements.py
    python3 scripts/backfill_settlements.py --loop     # run every 30 min forever
"""

import argparse
import sqlite3
import time

import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"
S = requests.Session()
S.headers.update({"Accept": "application/json"})


def get(path, **params):
    delay = 1.0
    for _ in range(6):
        r = S.get(BASE + path, params=params, timeout=20)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(float(r.headers.get("Retry-After", delay)))
            delay = min(delay * 2, 30)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return r.json()


def markets_for(event_ticker):
    out, cursor = [], None
    while True:
        p = {"event_ticker": event_ticker, "limit": 1000}
        if cursor:
            p["cursor"] = cursor
        d = get("/markets", **p)
        out.extend(d.get("markets", []))
        cursor = d.get("cursor")
        if not cursor:
            break
    return out


def backfill(db):
    con = sqlite3.connect(db, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")

    events = [r[0] for r in con.execute(
        "SELECT DISTINCT event_ticker FROM ticks").fetchall()]

    done = {r[0] for r in con.execute(
        "SELECT DISTINCT event_ticker FROM settlements "
        "WHERE result IN ('yes','no')").fetchall()}

    todo = [e for e in events if e not in done]
    print(f"{len(events)} events in db, {len(done)} already settled, "
         f"{len(todo)} to check")

    filled = 0
    for et in todo:
        try:
            mkts = markets_for(et)
        except Exception as e:
            print(f"  {et}: fetch failed ({e})")
            continue

        rows = []
        for m in mkts:
            res = m.get("result")
            if res in ("yes", "no"):
                rows.append((et, m["ticker"], m.get("floor_strike"), res,
                            m.get("close_time"), time.time()))

        if rows:
            con.executemany(
                "INSERT OR REPLACE INTO settlements VALUES (?,?,?,?,?,?)", rows)
            con.commit()
            filled += 1
            print(f"  {et}: {len(rows)} markets settled")
        else:
            status = mkts[0].get("status") if mkts else "?"
            print(f"  {et}: not settled yet (status={status})")
        time.sleep(0.3)

    total = con.execute(
        "SELECT COUNT(*) FROM settlements WHERE result IN ('yes','no')").fetchone()[0]
    con.close()
    print(f"\nevents newly filled: {filled}")
    print(f"total settled market rows now: {total:,}")
    return filled


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/recorder.db")
    ap.add_argument("--loop", action="store_true",
                    help="keep running every 30 minutes")
    a = ap.parse_args()

    if not a.loop:
        backfill(a.db)
        return

    print("loop mode -- Ctrl-C to stop")
    while True:
        try:
            backfill(a.db)
        except Exception as e:
            print(f"backfill error: {e}")
        print("sleeping 30 min ...\n")
        time.sleep(1800)


if __name__ == "__main__":
    main()