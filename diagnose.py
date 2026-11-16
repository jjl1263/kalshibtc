#!/usr/bin/env python3
"""
diagnose.py -- explain WHY candidate strikes are being lost.

Run this whenever audit.py reports zero tradeable moments and you want to
know which specific filter killed the candidates -- and, critically, whether
a zero result is a RECORDER BUG or a REAL MARKET OBSERVATION.

PERFORMANCE: every figure is computed by SQLite with COUNT / GROUP BY over an
index. Nothing loads the tick table into memory. The previous version ran
`SELECT * FROM ticks` into a DataFrame, which on a multi-million-row database
built a multi-gigabyte frame before printing anything -- so it looked frozen
rather than slow.

CRITICAL CONCEPT -- yes_ask == 100 is NOT a price.
    Kalshi computes yes_ask = 100 - no_bid. When nobody bids NO (the normal
    state for a deep in-the-money strike), yes_ask reports 100 and
    yes_ask_size_fp is 0. Those rows mean "no offer", not "costs 100c".

    Counting them as priced markets is what made an early diagnosis read
    "nothing was ever priced 98-99c" when the truth was different. They are
    separated out explicitly below.

Usage:
    python3 scripts/diagnose.py
    python3 scripts/diagnose.py --min-ask 98 --size 3
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


def one(con, sql, params=()):
    row = con.execute(sql, params).fetchone()
    return row[0] if row and row[0] is not None else 0


def rows(con, sql, params=()):
    return con.execute(sql, params).fetchall()


def f(n):
    return f"{n:,}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/recorder.db")
    ap.add_argument("--min-ask", type=float, default=MIN_ASK)
    ap.add_argument("--size", type=float, default=DEF_SIZE,
                    help="contracts you need to fill")
    ap.add_argument("--window", type=float, default=600,
                    help="seconds before close you actually trade")
    ap.add_argument("--no-index", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(a.db):
        raise SystemExit(f"No database at {a.db}")

    con = sqlite3.connect(a.db, timeout=60)
    con.execute("PRAGMA cache_size=-64000")
    con.execute("PRAGMA temp_store=MEMORY")

    size_mb = os.path.getsize(a.db) / 1e6
    print(f"database: {a.db}  ({size_mb:,.0f} MB)", flush=True)

    if not a.no_index:
        print("ensuring indexes (one-time) ... ", end="", flush=True)
        t0 = time.time()
        for name, spec in [("ix_dg_ask", "ticks(yes_ask)"),
                           ("ix_dg_scan", "ticks(tau, yes_ask, is_marked)")]:
            con.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {spec}")
        con.commit()
        print(f"{time.time()-t0:.1f}s", flush=True)

    t_start = time.time()
    total = one(con, "SELECT COUNT(*) FROM ticks")
    if total == 0:
        raise SystemExit("No ticks at all. Is the recorder running?")

    n_ev = one(con, "SELECT COUNT(DISTINCT event_ticker) FROM ticks")
    print(f"\nticks: {f(total)}   events: {n_ev}", flush=True)

    try:
        s_tot = one(con, "SELECT COUNT(*) FROM settlements")
        s_res = rows(con, "SELECT COALESCE(result,'(blank)'), COUNT(*) "
                          "FROM settlements GROUP BY result")
        print(f"settlement rows: {f(s_tot)}")
        if s_res:
            print("  results seen:", {r: c for r, c in s_res})
    except sqlite3.Error:
        print("settlements table missing")

    # ------------------------------------------------------------ funnel
    print()
    print("=" * 66)
    print("FUNNEL -- where candidate strikes are being lost")
    print("=" * 66)

    W = "tau <= ?"
    p = (a.window,)

    n_win = one(con, f"SELECT COUNT(*) FROM ticks WHERE {W}", p)
    n_priced = one(con, f"SELECT COUNT(*) FROM ticks WHERE {W} "
                        f"AND yes_ask IS NOT NULL", p)
    n_noffer = one(con, f"SELECT COUNT(*) FROM ticks WHERE {W} "
                        f"AND yes_ask >= 100", p)
    n_real = one(con, f"SELECT COUNT(*) FROM ticks WHERE {W} "
                      f"AND yes_ask IS NOT NULL AND yes_ask < 100", p)
    n_band = one(con, f"SELECT COUNT(*) FROM ticks WHERE {W} "
                      f"AND yes_ask < 100 AND yes_ask >= ?", p + (a.min_ask,))
    n_unmark = one(con, f"SELECT COUNT(*) FROM ticks WHERE {W} "
                        f"AND yes_ask < 100 AND yes_ask >= ? AND is_marked = 0",
                   p + (a.min_ask,))
    n_depth = one(con, f"SELECT COUNT(*) FROM ticks WHERE {W} "
                       f"AND yes_ask < 100 AND yes_ask >= ? AND is_marked = 0 "
                       f"AND ask_size_at_ask >= ?", p + (a.min_ask, a.size))
    any_depth = one(con, "SELECT COUNT(*) FROM ticks WHERE ask_size_at_ask > 0")

    print(f"all tick-rows                        {f(total):>12}")
    print(f"  in the last {a.window:.0f}s of an hour     {f(n_win):>12}")
    print(f"  with a yes_ask value               {f(n_priced):>12}")
    print(f"  of which ask==100 (NO OFFER)       {f(n_noffer):>12}")
    print(f"  with a REAL offer (<100c)          {f(n_real):>12}")
    print(f"  priced >={a.min_ask:.0f}c and <100c            {f(n_band):>12}")
    print(f"  and not the marked strike          {f(n_unmark):>12}")
    print(f"  and depth >= {a.size:g}                    {f(n_depth):>12}   <-- TRADEABLE")

    # ------------------------------------------------------------ diagnosis
    print()
    if n_priced == 0:
        print(">> DIAGNOSIS: NO tick has a price at all. RECORDING BUG.")
        print("   Kalshi migrated to fixed-point '_dollars' price strings;")
        print("   the legacy integer fields are null. Update kalshi_recorder.py,")
        print("   restart, then run: python3 scripts/probe_market_fields.py")
    elif any_depth == 0:
        print(">> DIAGNOSIS: NO row anywhere has depth. RECORDING BUG.")
        print("   The recorder is not reading yes_ask_size_fp. Update")
        print("   kalshi_recorder.py and restart the service.")
        print("   (Depth comes off the market object now, not /orderbook,")
        print("    which returns {} for most markets.)")
    elif n_band == 0:
        print(f">> DIAGNOSIS: nothing had a REAL offer at >={a.min_ask:.0f}c.")
        print("   Depth is being recorded elsewhere, so this is a genuine")
        print("   market observation, not a bug.")
        print(f"   Try --min-ask 98 to see whether the band sits above what")
        print("   the market actually offers.")
    elif n_unmark == 0:
        print(">> DIAGNOSIS: every qualifying offer WAS the marked strike.")
        print("   The marked strike is excluded until tau <= 90s because spot")
        print("   sits right on it. Widen MARKED_STRIKE_TAU_SECONDS only if")
        print("   you accept that risk.")
    elif n_depth == 0:
        print(f">> DIAGNOSIS: offers existed at >={a.min_ask:.0f}c but never with")
        print(f"   {a.size:g}+ contracts resting. Availability is thin.")
        print("   Options: lower --size, or lower MIN_DEPTH_CONTRACTS and")
        print("   accept partial fills.")
    else:
        secs = one(con, f"SELECT COUNT(DISTINCT ts) FROM ticks WHERE {W} "
                        f"AND yes_ask < 100 AND yes_ask >= ? AND is_marked = 0 "
                        f"AND ask_size_at_ask >= ?", p + (a.min_ask, a.size))
        evs = one(con, f"SELECT COUNT(DISTINCT event_ticker) FROM ticks WHERE {W} "
                       f"AND yes_ask < 100 AND yes_ask >= ? AND is_marked = 0 "
                       f"AND ask_size_at_ask >= ?", p + (a.min_ask, a.size))
        print(f">> Tradeable moments FOUND: {f(n_depth)} rows across {evs} events")
        print(f"   ({secs:,} distinct seconds). If the bot still isn't trading,")
        print("   the sigma / speed gates are the cause, not availability.")
        print("   Check: python3 scripts/dashboard.py")

    # ------------------------------------------------------------ price dist
    print()
    print("=" * 66)
    print("PRICE DISTRIBUTION of non-marked strikes (in window)")
    print("=" * 66)
    dist = rows(con, f"""
        SELECT CASE
                 WHEN yes_ask >= 100 THEN 'ask=100 (NO OFFER)'
                 WHEN yes_ask >= 99  THEN '99c'
                 WHEN yes_ask >= 98  THEN '98c'
                 WHEN yes_ask >= 95  THEN '95-97c'
                 WHEN yes_ask >= 90  THEN '90-94c'
                 WHEN yes_ask >= 50  THEN '50-89c'
                 ELSE '<50c' END AS band,
               COUNT(*)
        FROM ticks
        WHERE {W} AND yes_ask IS NOT NULL AND is_marked = 0
        GROUP BY band""", p)
    order = ['<50c', '50-89c', '90-94c', '95-97c', '98c', '99c',
             'ask=100 (NO OFFER)']
    d = dict(dist)
    for b in order:
        if b in d:
            print(f"  {b:22} {f(d[b]):>12}")

    # ------------------------------------------------------------ per event
    print()
    print("=" * 66)
    print("PER-EVENT COVERAGE (most recent 12)")
    print("=" * 66)
    ev = rows(con, f"""
        SELECT event_ticker,
               COUNT(DISTINCT ts)                              AS secs,
               COUNT(DISTINCT market_ticker)                   AS strikes,
               SUM(CASE WHEN ask_size_at_ask > 0 THEN 1 ELSE 0 END) AS with_depth,
               MAX(CASE WHEN yes_ask < 100 THEN yes_ask END)   AS max_real_ask,
               MAX(tau)                                        AS max_tau
        FROM ticks GROUP BY event_ticker
        ORDER BY MIN(ts) DESC LIMIT 12""")
    print(f"  {'event':<24} {'secs':>6} {'strikes':>8} {'w/depth':>9} "
         f"{'max_ask':>8} {'max_tau':>8}")
    for et, secs, strikes, wd, mx, mt in ev:
        flag = "  <-- NOT HOURLY" if (mt or 0) > 1200 else ""
        print(f"  {et:<24} {secs:>6} {strikes:>8} {wd or 0:>9} "
             f"{(mx or 0):>8.0f} {(mt or 0):>8.0f}{flag}")

    print(f"\n(diagnose completed in {time.time()-t_start:.1f}s)")
    con.close()


if __name__ == "__main__":
    main()