#!/usr/bin/env python3
"""
audit.py -- recorder health + opportunity verdict.

PERFORMANCE: every number is computed by SQLite with COUNT/GROUP BY over an
index. Nothing loads the tick table into memory. The previous version ran
`SELECT * FROM ticks` into a DataFrame -- fine at 26k rows, fatal at 30M
(it exhausted RAM). This version uses roughly constant memory regardless of
database size.

Run it ON the recorder box. There is no need to download the database.

THE ONE THING THIS ANSWERS
    In the last N seconds of each hour, how often is there a strike with a
    REAL offer at >= MIN_ASK_CENTS, with enough depth to fill your order,
    that isn't the marked strike?

CRITICAL CONCEPT -- yes_ask == 100 is NOT a price.
    Kalshi computes yes_ask = 100 - no_bid. When nobody bids NO (normal for
    a deep in-the-money strike) yes_ask reports 100 and size is 0. Those
    rows mean "no offer", not "costs 100c". All counts exclude them.

Usage:
    python3 scripts/audit.py
    python3 scripts/audit.py --size 3 --window 600
    python3 scripts/audit.py --no-index
"""

import argparse
import os
import sqlite3
import sys
import time

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")
try:
    import config as _cfg
    MIN_ASK = _cfg.MIN_ASK_CENTS
    DEF_SIZE = _cfg.CONTRACTS_PER_TRADE
except Exception:
    # L6: 1 contract at 99c is unwinnable (the rounded fee equals
    # the gain), so a failed config import must not silently audit
    # a size you would never trade.
    MIN_ASK, DEF_SIZE = 99, 14
import regime_time as _rt


def connect(db):
    if not os.path.exists(db):
        raise SystemExit(f"No database at {db}")
    con = sqlite3.connect(db, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA cache_size=-64000")   # ~64MB ceiling, predictable
    con.execute("PRAGMA temp_store=MEMORY")
    return con


def ensure_indexes(con, quiet=False):
    """Without these every query is a full scan. Created once; safe to re-run."""
    for name, spec in [
        ("ix_ticks_ask", "ticks(yes_ask)"),
        ("ix_ticks_tau", "ticks(tau)"),
        ("ix_ticks_ev", "ticks(event_ticker)"),
        ("ix_ticks_scan", "ticks(tau, yes_ask, is_marked)"),
    ]:
        t0 = time.time()
        con.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {spec}")
        dt = time.time() - t0
        if not quiet and dt > 1.0:
            print(f"  built {name} ({dt:.1f}s)")
    con.commit()


def one(con, sql, params=()):
    row = con.execute(sql, params).fetchone()
    return row[0] if row and row[0] is not None else 0


def rows(con, sql, params=()):
    return con.execute(sql, params).fetchall()


def fmt(n):
    return f"{n:,}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/recorder.db")
    ap.add_argument("--size", type=float, default=DEF_SIZE)
    ap.add_argument("--min-ask", type=float, default=MIN_ASK)
    ap.add_argument("--window", type=float, default=600)
    ap.add_argument("--no-index", action="store_true")
    a = ap.parse_args()

    con = connect(a.db)
    size_mb = os.path.getsize(a.db) / 1e6
    print(f"database: {a.db}  ({size_mb:,.0f} MB)")
    if not a.no_index:
        print("ensuring indexes (one-time cost, makes everything after fast)...")
        ensure_indexes(con)
    print()
    t_start = time.time()

    print("=" * 74)
    print("SECTION 1 -- IS THE RECORDER HEALTHY?")
    print("=" * 74)
    total = one(con, "SELECT COUNT(*) FROM ticks")
    if total == 0:
        raise SystemExit("No ticks. Is the recorder running?")
    n_ev = one(con, "SELECT COUNT(DISTINCT event_ticker) FROM ticks")
    n_price = one(con, "SELECT COUNT(*) FROM ticks WHERE yes_ask IS NOT NULL")
    n_depth = one(con, "SELECT COUNT(*) FROM ticks WHERE ask_size_at_ask > 0")
    n_spot = one(con, "SELECT COUNT(*) FROM ticks WHERE btc_spot IS NOT NULL")
    print(f"  tick rows                  {fmt(total):>14}")
    print(f"  events                     {fmt(n_ev):>14}")
    print(f"  rows with a yes_ask        {fmt(n_price):>14}  ({n_price/total*100:.1f}%)")
    print(f"  rows with depth > 0        {fmt(n_depth):>14}  ({n_depth/total*100:.1f}%)")
    print(f"  rows with BTC spot         {fmt(n_spot):>14}  ({n_spot/total*100:.1f}%)")

    problems = []
    if n_price / total < 0.5:
        problems.append("Most rows have NO PRICE -- recorder broken.")
    if n_depth == 0:
        problems.append("NO row has depth -- recorder not reading yes_ask_size_fp.")
    if n_spot / total < 0.9:
        problems.append("BTC spot missing on many rows -- Coinbase fetch failing.")
    thin = one(con, """SELECT COUNT(*) FROM (SELECT event_ticker FROM ticks
                       GROUP BY event_ticker HAVING COUNT(DISTINCT ts) < 300)""")
    if thin:
        problems.append(f"{thin} event(s) have <300s recorded -- restarts/dropouts.")
    long_ev = rows(con, """SELECT event_ticker, MAX(tau) FROM ticks
                           GROUP BY event_ticker HAVING MAX(tau) > 1200""")
    if long_ev:
        problems.append(f"{len(long_ev)} event(s) have tau>1200s -- NON-HOURLY "
                        "contracts, will skew opportunity stats.")
    if problems:
        print("\n  PROBLEMS FOUND:")
        for p in problems:
            print(f"    !! {p}")
        for t, mx in long_ev[:8]:
            print(f"       non-hourly: {t}  max tau={mx:.0f}s")
    else:
        print("\n  No structural problems detected.")

    print()
    print("=" * 74)
    print("SECTION 2 -- WHAT DOES 'ask == 100' MEAN?  (sanity check)")
    print("=" * 74)
    at100 = one(con, "SELECT COUNT(*) FROM ticks WHERE yes_ask >= 100")
    at100d = one(con, "SELECT COUNT(*) FROM ticks WHERE yes_ask >= 100 AND ask_size_at_ask > 0")
    below = one(con, "SELECT COUNT(*) FROM ticks WHERE yes_ask IS NOT NULL AND yes_ask < 100")
    print(f"  rows with ask >= 100c      {fmt(at100):>14}")
    print(f"     of those, depth > 0     {fmt(at100d):>14}")
    print("     -> ~0 confirms ask==100 means 'NO OFFER'")
    print(f"  rows with a real offer     {fmt(below):>14}")

    print()
    print("=" * 74)
    print(f"SECTION 3 -- OPPORTUNITY: real offer >= {a.min_ask:.0f}c, "
          f"depth >= {a.size:g}, not marked")
    print("=" * 74)
    p = (a.window,)
    f1 = one(con, "SELECT COUNT(*) FROM ticks WHERE tau <= ?", p)
    f2 = one(con, "SELECT COUNT(*) FROM ticks WHERE tau <= ? AND yes_ask IS NOT NULL", p)
    f3 = one(con, "SELECT COUNT(*) FROM ticks WHERE tau <= ? AND yes_ask < 100", p)
    f4 = one(con, "SELECT COUNT(*) FROM ticks WHERE tau <= ? AND yes_ask < 100 AND yes_ask >= ?",
             p + (a.min_ask,))
    f5 = one(con, "SELECT COUNT(*) FROM ticks WHERE tau <= ? AND yes_ask < 100 "
                  "AND yes_ask >= ? AND is_marked = 0", p + (a.min_ask,))
    f6 = one(con, "SELECT COUNT(*) FROM ticks WHERE tau <= ? AND yes_ask < 100 "
                  "AND yes_ask >= ? AND is_marked = 0 AND ask_size_at_ask >= ?",
             p + (a.min_ask, a.size))
    print(f"  (last {a.window:.0f}s of each hour)")
    print(f"  rows in window             {fmt(f1):>14}")
    print(f"  ...with a price            {fmt(f2):>14}")
    print(f"  ...real offer (<100c)      {fmt(f3):>14}")
    print(f"  ...>= {a.min_ask:.0f}c                 {fmt(f4):>14}")
    print(f"  ...not the marked strike   {fmt(f5):>14}")
    print(f"  ...depth >= {a.size:g}             {fmt(f6):>14}   <-- TRADEABLE")

    TR = ("tau <= ? AND yes_ask < 100 AND yes_ask >= ? AND is_marked = 0 "
          "AND ask_size_at_ask >= ?")
    tp = (a.window, a.min_ask, a.size)

    ev_with = 0
    if f6 == 0:
        print("\n  >> No tradeable moments in this sample.")
        if n_depth == 0:
            print("     Depth missing everywhere -> RECORDER BUG, not a market fact.")
        else:
            print("     Depth IS recorded, so this is a real observation.")
            print("     Try --min-ask 98 to see if the band is above the market.")
    else:
        ev_with = one(con, f"SELECT COUNT(DISTINCT event_ticker) FROM ticks WHERE {TR}", tp)
        r = rows(con, f"""SELECT MIN(c), MAX(c), AVG(c) FROM (
                            SELECT COUNT(DISTINCT ts) c FROM ticks
                            WHERE {TR} GROUP BY event_ticker)""", tp)
        mn, mx, av = r[0] if r else (0, 0, 0)
        print(f"\n  events with >=1 tradeable moment: {ev_with} of {n_ev} "
              f"({ev_with/max(n_ev,1)*100:.0f}%)")
        print(f"  seconds tradeable per event: avg {av or 0:.0f}, "
              f"min {mn or 0:.0f}, max {mx or 0:.0f}")
        print(f"  implied opportunities/day: {ev_with/max(n_ev,1)*24:.1f}")

    print()
    print("=" * 74)
    print("SECTION 4 -- WHEN in the window do offers appear?")
    print("=" * 74)
    if f6:
        r = rows(con, f"""SELECT CAST(tau/60 AS INT) m, COUNT(*) n,
                                 COUNT(DISTINCT ts) secs,
                                 COUNT(DISTINCT event_ticker) evs, AVG(gap) g
                          FROM ticks WHERE {TR} GROUP BY m ORDER BY m""", tp)
        print(f"  {'min_before_close':>17} {'rows':>9} {'seconds':>9} {'events':>7} {'avg_gap':>9}")
        for m, n, secs_, evs, g in r:
            print(f"  {m:>17} {n:>9,} {secs_:>9,} {evs:>7} {g or 0:>9.0f}")
        print("\n  Use this to set TAU_MIN_SECONDS / TAU_MAX_SECONDS.")
    else:
        print("  (no tradeable rows to bucket)")

    print()
    print("=" * 74)
    print("SECTION 5 -- BY HOUR / WEEKDAY vs WEEKEND")
    print("=" * 74)
    if f6:
        # Bucketed in the REGIME TIMEZONE via regime_time.py -- the same
        # function the bot and tuner use. SQLite has no timezone database, so
        # this conversion happens in Python. Only two small aggregates are
        # pulled (event + timestamp), so memory stays flat.
        opp = rows(con, f"""SELECT DISTINCT event_ticker, MIN(ts) FROM ticks
                            WHERE {TR} GROUP BY event_ticker""", tp)
        allev = rows(con, """SELECT DISTINCT event_ticker, MIN(ts) FROM ticks
                             WHERE tau <= ? GROUP BY event_ticker""", (a.window,))
        have = {}
        for ev, ts in opp:
            have[_rt.regime_key_from_ts(ts)] = have.get(
                _rt.regime_key_from_ts(ts), 0) + 1
        tot = {}
        for ev, ts in allev:
            k = _rt.regime_key_from_ts(ts)
            tot[k] = tot.get(k, 0) + 1
        print(f"  (bucketed in {_rt.tz_label()})")
        print(f"  {'day':>8} {'hour':>6} {'recorded':>9} {'with_opp':>9} {'pct':>6}")
        for k in sorted(tot, key=lambda x: (x[0], x[1])):
            e = tot[k]
            w = have.get(k, 0)
            print(f"  {k[0]:>8} {k[1]:>6} {e:>9} {w:>9} "
                 f"{(w/e*100 if e else 0):>5.0f}%")
    else:
        print("  (no tradeable rows)")

    print()
    print("=" * 74)
    print("SECTION 6 -- SETTLEMENTS (needed for tuning)")
    print("=" * 74)
    try:
        s_good = one(con, "SELECT COUNT(*) FROM settlements WHERE result IN ('yes','no')")
        s_ev = one(con, "SELECT COUNT(DISTINCT event_ticker) FROM settlements "
                        "WHERE result IN ('yes','no')")
        print(f"  settled market rows        {fmt(s_good):>14}")
        print(f"  events fully settled       {fmt(s_ev):>14} of {n_ev}")
        if s_ev < n_ev:
            print("  -> run: python3 scripts/backfill_settlements.py")
    except sqlite3.Error:
        print("  settlements table missing")

    print()
    print("=" * 74)
    print("STORAGE")
    print("=" * 74)
    span = max(one(con, "SELECT (MAX(ts)-MIN(ts))/86400.0 FROM ticks"), 0.01)
    print(f"  db size                    {size_mb:>10,.0f} MB")
    print(f"  days of data               {span:>10,.2f}")
    print(f"  growth rate                {size_mb/span:>10,.0f} MB/day")
    print(f"  projected 30 days          {size_mb/span*30/1000:>10,.1f} GB")
    if total:
        print(f"\n  {at100/total*100:.0f}% of rows are 'no offer' (ask>=100) and carry")
        print("  no information for this strategy.")
    print("\n  To control growth:")
    print("    1. config.py: RECORD_MIN_ASK_CENTS / RECORD_GAP_WINDOW_USD")
    print("       make the recorder skip dead strikes at write time")
    print("    2. python3 scripts/prune_data.py --apply    (prune + VACUUM)")

    print()
    print("=" * 74)
    print("VERDICT")
    print("=" * 74)
    if problems:
        print("  RECORDER NOT HEALTHY. Fix section 1 first; everything below")
        print("  section 1 is unreliable until then.")
    elif f6 == 0:
        print("  Recorder healthy, but zero tradeable moments found.")
    else:
        rate = ev_with / max(n_ev, 1)
        print(f"  Recorder healthy. {rate*100:.0f}% of events had a tradeable")
        print(f"  moment at >= {a.min_ask:.0f}c with depth >= {a.size:g}.")
        if rate * 24 < 4:
            print("  WARNING: under 4 opportunities/day. Availability, not")
            print("  safety thresholds, is your bottleneck.")
        else:
            print(f"  ~{rate*24:.0f} opportunities/day. Enough to proceed to tuning.")
        if n_ev < 24:
            print(f"  NOTE: only {n_ev} events -- collect a full day first.")

    print(f"\n(audit completed in {time.time()-t_start:.1f}s)")
    con.close()


if __name__ == "__main__":
    main()