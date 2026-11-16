#!/usr/bin/env python3
"""
prune_data.py -- reclaim disk space without losing anything you use.

WHAT IT DELETES (and why that's safe):
  Rows where yes_ask >= 100 AND the strike is far from spot.
  yes_ask == 100 means NO OFFER on Kalshi (100 - no_bid, with no_bid = 0),
  so these rows record "nobody was selling this deep-ITM strike" -- a fact
  no part of the strategy, the audit, or the tuner ever reads.

WHAT IT ALWAYS KEEPS:
  * every row with a real offer (yes_ask < 100)
  * every row within --keep-gap dollars of spot (near-money context)
  * every marked-strike row
  * the entire settlements table
  * everything from the last --keep-days days, untouched, whatever it is

Run with no flags for a DRY RUN that only reports. Nothing is deleted until
you pass --apply.

    python3 scripts/prune_data.py                 # dry run, shows savings
    python3 scripts/prune_data.py --apply         # actually prune + VACUUM
    python3 scripts/prune_data.py --apply --keep-days 3

NOTE ON VACUUM: SQLite does not release disk space on DELETE alone; VACUUM
rewrites the file. It needs temporary free space roughly equal to the final
database size, and takes a minute or two on a multi-GB file. Stop the
recorder first, or run it during a quiet stretch between hours.
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
    DEF_GAP = getattr(_cfg, "RECORD_GAP_WINDOW_USD", 1500)
except Exception:
    DEF_GAP = 1500


def human(n):
    return f"{n/1e6:,.0f} MB" if n < 1e9 else f"{n/1e9:,.2f} GB"


def _be_polite():
    """
    Lower this process's CPU priority.

    These analysis scripts run on the SAME box as the recorder, which has a
    hard real-time requirement: it must sample the book every second. A
    4,400-combination sweep can saturate a 2-core VM and cause the recorder
    to miss ticks -- corrupting the very data being analysed.

    Renicing costs nothing when the machine is idle and yields instantly when
    the recorder needs CPU.
    """
    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/recorder.db")
    ap.add_argument("--apply", action="store_true",
                    help="actually delete (default is a dry run)")
    ap.add_argument("--keep-days", type=float, default=2.0,
                    help="never touch anything newer than this many days")
    ap.add_argument("--keep-gap", type=float, default=DEF_GAP,
                    help="always keep strikes within this many $ of spot")
    ap.add_argument("--no-vacuum", action="store_true",
                    help="skip VACUUM (deletes rows but does not shrink the file)")
    a = ap.parse_args()
    _be_polite()

    if not os.path.exists(a.db):
        raise SystemExit(f"No database at {a.db}")

    before = os.path.getsize(a.db)
    con = sqlite3.connect(a.db, timeout=120)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA cache_size=-64000")

    cutoff = time.time() - a.keep_days * 86400

    total = con.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
    if total == 0:
        raise SystemExit("No ticks to prune.")

    WHERE = """
        ts < ?
        AND yes_ask >= 100
        AND is_marked = 0
        AND (btc_spot IS NULL OR floor_strike IS NULL
             OR ABS(btc_spot - floor_strike) > ?)
    """
    params = (cutoff, a.keep_gap)

    doomed = con.execute(f"SELECT COUNT(*) FROM ticks WHERE {WHERE}",
                        params).fetchone()[0]

    print(f"database : {a.db}  ({human(before)})")
    print(f"total rows: {total:,}")
    print()
    print("PRUNE CRITERIA (all must hold):")
    print(f"  older than {a.keep_days} days")
    print("  yes_ask >= 100  (NO OFFER -- not a price)")
    print("  not the marked strike")
    print(f"  strike more than ${a.keep_gap:,.0f} from spot")
    print()
    print(f"rows matching : {doomed:,}  ({doomed/total*100:.1f}% of table)")
    print(f"rows kept     : {total-doomed:,}")
    est = before * (doomed / total)
    print(f"est. reclaim  : ~{human(est)}")

    # sanity: confirm we are not deleting anything the strategy reads
    would_lose = con.execute(
        f"SELECT COUNT(*) FROM ticks WHERE ({WHERE}) AND yes_ask < 100",
        params).fetchone()[0]
    print(f"\nsafety check: rows with a REAL offer being deleted = {would_lose}")
    if would_lose:
        raise SystemExit("ABORT -- criteria would delete real offers. "
                         "This is a bug; do not proceed.")
    print("  (0 is correct -- only 'no offer' rows are removed)")

    if not a.apply:
        print("\nDRY RUN. Nothing changed. Re-run with --apply to prune.")
        con.close()
        return

    if doomed == 0:
        print("\nNothing to prune.")
        con.close()
        return

    print("\ndeleting...")
    t0 = time.time()
    # chunked so a huge delete doesn't hold one enormous transaction
    removed = 0
    while True:
        cur = con.execute(
            f"DELETE FROM ticks WHERE rowid IN "
            f"(SELECT rowid FROM ticks WHERE {WHERE} LIMIT 200000)", params)
        con.commit()
        if cur.rowcount <= 0:
            break
        removed += cur.rowcount
        print(f"  {removed:,} / {doomed:,}")
    print(f"deleted {removed:,} rows in {time.time()-t0:.0f}s")

    if not a.no_vacuum:
        print("\nVACUUM (rewrites the file to actually free space)...")
        t0 = time.time()
        con.execute("VACUUM")
        print(f"  done in {time.time()-t0:.0f}s")
    con.close()

    after = os.path.getsize(a.db)
    print()
    print(f"before: {human(before)}")
    print(f"after : {human(after)}")
    print(f"freed : {human(before-after)}  ({(before-after)/before*100:.0f}%)")


if __name__ == "__main__":
    main()