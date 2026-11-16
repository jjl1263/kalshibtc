#!/usr/bin/env python3
"""
tune_parameters.py -- turn recorded data into config.py numbers.

This replays your recorder database as if the bot had been running, across a
grid of parameter values, and reports what each setting would have done.
It is a backtest over YOUR market, not a simulation of an idealised one.

What it needs:
  data/recorder.db   with at least a few dozen SETTLED events
                     (the recorder writes settlements after each hour closes)

What it gives you:
  * for each gap_k value: how many trades fired, win rate, EV per contract
  * the same split by weekday vs weekend, and by UTC hour block
  * a recommended config.py block you can paste in

IMPORTANT LIMITATION, read before trusting the output:
At a 98-99% win rate, losses are RARE. With 168 events (one week) you might
see one or two losses total -- or zero. A backtest showing "100% win rate,
0 losses" across 40 trades does NOT mean the parameters are safe; it means
the sample is too small to have found the failure mode yet. The break-even
is 98.1% (98c) / 99.1% (99c), so you need hundreds of trades before a win
rate is distinguishable from break-even. Use this to REJECT settings that
already show losses, and to see trade frequency -- not to confirm that a
setting is profitable.

Usage:
    python3 tune_parameters.py
    python3 tune_parameters.py --db data/recorder.db --out out/tuning.xlsx
"""

import argparse
import os
import math
import sqlite3
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

# Grids to sweep. Edit if you want to explore different ranges.
# Much finer sweeps for optimisation. Every combination below is simulated.
SIGMA_GRID = [2.0, 2.25, 2.5, 2.75, 3.0, 3.25, 3.5, 3.75, 4.0, 4.5, 5.0]

TAU_WINDOWS = [
    (30, 120), (30, 180), (30, 240), (30, 300),
    (60, 180), (60, 240), (60, 300), (60, 420),
    (90, 240), (90, 300), (90, 420), (90, 600),
    (120, 300), (120, 420), (120, 600),
    (180, 420), (180, 600),
    (15, 90), (15, 120),
]

GAP_MIN_ABS_GRID = [0.0, 15.0, 25.0, 40.0, 60.0]
RECENT_MOVE_WINDOW = 10
RECENT_MOVE_MAX_GRID = [15.0, 30.0, 60.0, 1e9]   # 1e9 = gate effectively off

# HARD FLOOR on sigma. Documented in LAUNCH_GUIDE 3.4 and enforced here so
# the suggestion block cannot emit a value the guide forbids.
#
# 2.5 sigma is 1-in-161 under a NORMAL distribution. BTC's tails are fatter
# than normal -- assume the true breach rate is 3-5x worse -- so 2.5 is
# already thin against a 1-in-107 break-even. Anything below it is not
# supported by any amount of zero-loss data, because a zero-loss sample
# cannot measure a tail it did not sample.
SIGMA_FLOOR = 2.5

BREAKEVEN_98 = 0.9814
BREAKEVEN_99 = 0.9907

# the tau window used for the per-regime breakdown (matches config.py)
import config as _cfg
import regime_time as _rt
MIN_ASK = _cfg.MIN_ASK_CENTS
CFG_TAU_LO = _cfg.TAU_MIN_SECONDS
CFG_TAU_HI = _cfg.TAU_MAX_SECONDS
CFG_COUNT = _cfg.CONTRACTS_PER_TRADE
CFG_GAP_MIN = _cfg.GAP_MIN_ABS_USD
CFG_MOVE_MAX = _cfg.RECENT_MOVE_MAX_USD
if (CFG_TAU_LO, CFG_TAU_HI) not in TAU_WINDOWS:
    TAU_WINDOWS.append((CFG_TAU_LO, CFG_TAU_HI))


def taker_fee_cents(price_cents, count=1):
    """
    Kalshi taker fee for the WHOLE ORDER, in cents, rounded UP to the next
    cent. The round-up matters enormously at small sizes: a 1-contract order
    at 99c pays a full 1c fee against 1c of profit.
    """
    p = price_cents / 100.0
    return math.ceil(0.07 * count * p * (1 - p) * 100)


def sigma_tau(rv5, tau):
    """Parkinson sigma over the remaining tau seconds."""
    if rv5 is None or not np.isfinite(rv5) or tau <= 0:
        return None
    return (rv5 / 1.67) * math.sqrt(tau / 300.0)


def _p(msg, end="\n"):
    """Progress print that actually reaches the terminal immediately."""
    print(msg, end=end, flush=True)


def load_candidates(db, max_tau, count, require_depth=True,
                    want_margin=True, build_index=True):
    """
    Load ONLY what the sweep can possibly use, via two small indexed queries.

    The old version ran `SELECT * FROM ticks` into pandas -- the entire table,
    millions of rows, several GB, many minutes, and no output until it
    finished (which is why it looked stalled). But the sweep only ever varies
    sigmas / tau window / gap floor / speed limit. These filters are FIXED
    across every combination, so they belong in SQL:

        yes_ask >= MIN_ASK and < 100   (price band, fixed by config)
        tau <= widest tau window tested
        depth >= contracts             (can't fill otherwise)
        not the marked strike, unless tau <= 90

    On real data that is a few thousand rows out of millions.

    Returns (candidates_df, spot_df, results_dict). No data is dropped that
    could produce a trade at ANY parameter setting in the grid.
    """
    con = sqlite3.connect(db)
    # Index creation sorts the whole table. On a multi-GB database sitting on
    # a small cloud volume this is disk-bound, so give it more cache and keep
    # the sort scratch in memory.
    con.execute("PRAGMA cache_size=-256000")     # ~256MB
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("PRAGMA synchronous=OFF")

    try:
        _mb = os.path.getsize(db) / 1e6
        _p(f"  database: {_mb:,.0f} MB")
    except OSError:
        _mb = 0
    _p("  [1/6] indexing (one-time, see note below)")
    t0 = time.time()
    # Index order matters: yes_ask is FAR more selective than tau here
    # (only a few percent of rows are >=99c, while tau<=600 keeps most of a
    # 15-minute recording). Leading on yes_ask lets SQLite seek instead of
    # scanning.
    #
    # Each CREATE INDEX sorts the entire table. On a multi-GB database that
    # is minutes, not seconds -- and it used to print nothing at all until
    # every index finished, which looked identical to a hang. Progress is now
    # reported per index.
    idx = [("ix_tp_ask", "ticks(yes_ask, tau)", "candidate scan")]
    if want_margin:
        idx.append(("ix_tp_margin", "ticks(event_ticker, market_ticker, tau)",
                    "settlement margins"))
    idx.append(("ix_tp_ev", "ticks(event_ticker, ts)", "spot series"))

    if not build_index:
        _p("        --no-index given: skipping. Queries will be SLOW if the "
          "indexes are absent.")
        todo = []
        have = set()
    else:
        have = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        todo = [(n, spec, why) for n, spec, why in idx if n not in have]
    if not build_index:
        pass                      # message already printed above
    elif not todo:
        _p("        all indexes present, nothing to build")
    else:
        _p(f"        {len(todo)} index(es) to build. This is ONE-TIME -- "
          f"later runs skip it.")
        _p("        On a multi-GB database expect 1-5 minutes each. "
          "Do not interrupt.")
    for name, spec, why in todo:
        _p(f"        building {name} ({why}) ... ", end="")
        t0 = time.time()
        con.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {spec}")
        con.commit()
        _p(f"{time.time()-t0:.0f}s")

    _p("  [2/6] settlements ... ", end="")
    t0 = time.time()
    results = dict(con.execute(
        "SELECT market_ticker, result FROM settlements "
        "WHERE result IN ('yes','no')").fetchall())
    _p(f"{len(results):,} settled markets ({time.time()-t0:.1f}s)")
    if not results:
        con.close()
        raise SystemExit(
            "No settlements recorded yet. Run backfill_settlements.py first.")

    _p("  [3/6] candidate rows ... ", end="")
    t0 = time.time()
    depth_sql = "AND ask_size_at_ask >= ?" if require_depth else ""
    params = [max_tau, MIN_ASK]
    if require_depth:
        params.append(count)
    cand = pd.read_sql_query(f"""
        SELECT event_ticker, market_ticker, ts, tau, gap, yes_ask, is_marked,
               ask_size_at_ask
        FROM ticks
        WHERE tau <= ?
          AND yes_ask >= ? AND yes_ask < 100
          AND gap IS NOT NULL
          AND (is_marked = 0 OR tau <= 90)
          {depth_sql}
    """, con, params=params)
    _p(f"{len(cand):,} rows ({time.time()-t0:.1f}s)")

    if cand.empty:
        con.close()
        return cand, pd.DataFrame(), results, pd.DataFrame()

    _p("  [4/6] settlement margins ... ", end="")
    t0 = time.time()
    # How much room was left at the close, per market.
    #
    # PERFORMANCE: this MUST be restricted to the candidate markets and to a
    # narrow tau slice. The first version grouped over the whole ticks table
    # with no filter plus a self-join -- tens of millions of rows, and it hung
    # for 20 minutes. Candidate markets number in the thousands, and only the
    # last minute of each matters, so both dimensions collapse.
    #
    # WHY tau <= 60: Kalshi settles KXBTCD on the 60-second average of BRTI
    # before the hour. The tightest gap inside that window is the closest the
    # settlement price actually came to the strike, which is exactly the
    # measurement we want -- not an approximation of it.
    margins = pd.DataFrame(columns=["event_ticker", "market_ticker",
                                    "settle_gap", "min_gap_60",
                                    "min_gap_seen"])
    if not cand.empty and want_margin:
        pairs = cand[["event_ticker", "market_ticker"]].drop_duplicates()
        _p(f"({len(pairs):,} markets) ", end="")
        con.execute("DROP TABLE IF EXISTS _cand_mkts")
        con.execute("CREATE TEMP TABLE _cand_mkts "
                    "(event_ticker TEXT, market_ticker TEXT)")
        con.executemany("INSERT INTO _cand_mkts VALUES (?,?)",
                        pairs.itertuples(index=False, name=None))
        con.execute("CREATE INDEX _ix_cand ON _cand_mkts"
                    "(event_ticker, market_ticker)")
        con.commit()

        # THREE DISTINCT QUANTITIES. Conflating them was a real bug: the
        # previous version called MIN(gap) over the last 60s "final_gap" and
        # then counted final_gap < 0 as a LOSS. A market that dipped below the
        # strike at tau=45 and settled $200 above was reported as a loss by the
        # margin section while the P&L sweep (correctly, from the settlement
        # result) reported a win. The two sections disagreed because one of
        # them was measuring the wrong thing.
        #
        #   settle_gap    gap at the LAST recorded tick -> settlement proxy
        #   min_gap_60    lowest gap inside the 60s BRTI averaging window
        #   min_gap_seen  lowest gap at any point in the trading window
        #
        # ONLY the settlement RESULT decides a loss. The gap columns measure
        # how close it came, never whether it lost.
        margins = pd.read_sql_query(f"""
            SELECT t.event_ticker,
                   t.market_ticker,
                   MIN(CASE WHEN t.tau <= 60 THEN t.gap END) AS min_gap_60,
                   MIN(t.gap)                                AS min_gap_seen,
                   MIN(t.tau)                                AS last_tau
            FROM ticks t
            JOIN _cand_mkts c
              ON t.event_ticker = c.event_ticker
             AND t.market_ticker = c.market_ticker
            WHERE t.tau <= {float(max_tau)} AND t.gap IS NOT NULL
            GROUP BY t.event_ticker, t.market_ticker
        """, con)

        # True settlement gap: the gap on the final recorded tick.
        settle = pd.read_sql_query(f"""
            SELECT t.event_ticker, t.market_ticker, t.gap AS settle_gap
            FROM ticks t
            JOIN _cand_mkts c
              ON t.event_ticker = c.event_ticker
             AND t.market_ticker = c.market_ticker
            JOIN (SELECT tt.event_ticker, tt.market_ticker,
                         MIN(tt.tau) AS mt
                  FROM ticks tt
                  JOIN _cand_mkts cc
                    ON tt.event_ticker = cc.event_ticker
                   AND tt.market_ticker = cc.market_ticker
                  WHERE tt.tau <= {float(max_tau)} AND tt.gap IS NOT NULL
                  GROUP BY tt.event_ticker, tt.market_ticker) m
              ON t.event_ticker = m.event_ticker
             AND t.market_ticker = m.market_ticker
             AND t.tau = m.mt
            WHERE t.gap IS NOT NULL
        """, con)
        margins = margins.merge(settle, on=["event_ticker", "market_ticker"],
                                how="left")
        con.execute("DROP TABLE IF EXISTS _cand_mkts")
        con.commit()
    _p(f"{len(margins):,} markets ({time.time()-t0:.1f}s)")

    _p("  [5/6] spot series ... ", end="")
    t0 = time.time()
    evs = tuple(cand["event_ticker"].unique())
    ph = ",".join("?" * len(evs))
    spot = pd.read_sql_query(f"""
        SELECT event_ticker, ts, MAX(btc_spot) AS btc_spot
        FROM ticks
        WHERE btc_spot IS NOT NULL AND event_ticker IN ({ph})
        GROUP BY event_ticker, ts
    """, con, params=list(evs))
    con.close()
    _p(f"{len(spot):,} rows across {len(evs)} events ({time.time()-t0:.1f}s)")

    return cand, spot, results, margins


def enrich_fast(cand, spot, results, margins=None):
    """
    Attach rv5 / move_recent / sigma_tau / result to the candidate rows.

    Rolling stats are computed on the SPOT series (one value per second per
    event, ~900 rows/event) rather than on every strike-row, then joined.
    Previously this ran a rolling window plus a merge per event over all
    strike rows -- the same work repeated ~188 times per second of data.
    """
    _p("  [6/6] features ... ", end="")
    t0 = time.time()
    feats = []
    for ev, g in spot.groupby("event_ticker", sort=False):
        g = g.sort_values("ts")
        if len(g) < 20:
            continue
        sp = g.set_index(pd.to_datetime(g["ts"], unit="s"))["btc_spot"]
        rv5 = sp.rolling("300s").max() - sp.rolling("300s").min()
        mv = sp - sp.shift(RECENT_MOVE_WINDOW)
        feats.append(pd.DataFrame({
            "event_ticker": ev, "ts": g["ts"].values,
            "rv5": rv5.values, "move_recent": mv.values}))
    if not feats:
        _p("no usable spot data")
        return pd.DataFrame()

    feat = pd.concat(feats, ignore_index=True)
    df = cand.merge(feat, on=["event_ticker", "ts"], how="inner")
    df["sigma_tau"] = (df["rv5"] / 1.67) * np.sqrt(df["tau"].clip(lower=1) / 300.0)
    df["result"] = df["market_ticker"].map(results)
    df = df[df["result"].isin(["yes", "no"])]
    if margins is not None and len(margins):
        df = df.merge(margins[["event_ticker", "market_ticker",
                               "settle_gap", "min_gap_60",
                               "min_gap_seen"]],
                      on=["event_ticker", "market_ticker"], how="left")
    else:
        df["settle_gap"] = np.nan
        df["min_gap_60"] = np.nan
        df["min_gap_seen"] = np.nan
    _p(f"{len(df):,} scorable rows ({time.time()-t0:.1f}s)")
    return df


def simulate(df, sigmas, tau_lo, tau_hi,
             gap_min_abs=25.0, recent_move_max=30.0, count=14):
    """Vectorised replay. df must come from enrich() and carry 'result'."""
    if df.empty:
        return []
    req = np.maximum(gap_min_abs, sigmas * df["sigma_tau"])
    ok = (
        df["tau"].between(tau_lo, tau_hi)
        # ask==100 means NO OFFER on Kalshi (100 - no_bid with no_bid=0),
        # so it is excluded -- it is not a purchasable price.
        & (df["yes_ask"] >= MIN_ASK) & (df["yes_ask"] < 100)
        & ((df["is_marked"] == 0) | (df["tau"] <= 90))
        & df["gap"].notna() & (df["gap"] >= req)
        & df["sigma_tau"].notna()
        & (df["move_recent"].abs().fillna(0) <= recent_move_max)
        & df["result"].isin(["yes", "no"])
    )
    c = df[ok]
    if c.empty:
        return []
    # one trade per event: earliest qualifying tick, then deepest gap
    c = c.sort_values(["event_ticker", "ts", "gap"],
                      ascending=[True, True, False])
    first_ts = c.groupby("event_ticker")["ts"].transform("min")
    c = c[c["ts"] == first_ts]
    c = c.groupby("event_ticker", as_index=False).first()

    ask = c["yes_ask"].astype(float)
    fee = np.ceil(0.07 * count * (ask / 100) * (1 - ask / 100) * 100)
    win = c["result"].eq("yes")
    pnl = np.where(win, count * (100 - ask) - fee, -(count * ask + fee))

    out = []
    for i, row in enumerate(c.itertuples(index=False)):
        out.append({
            "event_ticker": row.event_ticker, "market_ticker": row.market_ticker,
            "ts": row.ts, "tau": row.tau, "gap": row.gap, "rv5": row.rv5,
            "sigma_tau": row.sigma_tau,
            "sigmas_actual": (row.gap / row.sigma_tau) if row.sigma_tau else None,
            "entry_ask": float(row.yes_ask), "result": row.result,
            "win": row.result == "yes", "pnl_cents": float(pnl[i]),
            "settle_gap": getattr(row, "settle_gap", float("nan")),
            "min_gap_60": getattr(row, "min_gap_60", float("nan")),
            "min_gap_seen": getattr(row, "min_gap_seen", float("nan")),
        })
    return out


def summarize(trades, sigmas, tau_lo, tau_hi, total_events, **extra):
    if not trades:
        return {"sigmas": sigmas, "tau_window": f"{tau_lo}-{tau_hi}", **extra,
                "trades": 0, "wins": 0, "losses": 0, "win_rate": np.nan,
                "ev_cents_per_contract": np.nan, "total_pnl_cents": 0,
                "trades_per_event": 0.0, "verdict": "no trades fired"}
    df = pd.DataFrame(trades)
    wins = int(df["win"].sum())
    losses = len(df) - wins
    wr = wins / len(df)
    ev = df["pnl_cents"].mean()
    avg_ask = df["entry_ask"].mean()
    be = BREAKEVEN_99 if avg_ask >= 98.5 else BREAKEVEN_98

    if losses == 0:
        verdict = f"0 losses in {len(df)} -- SAMPLE TOO SMALL to confirm"
    elif wr < be:
        verdict = f"BELOW break-even ({be:.2%}) -- reject"
    else:
        verdict = f"above break-even ({be:.2%}) but n={len(df)} is small"

    # MARGIN STATISTICS.
    #
    # LOSSES COME FROM THE SETTLEMENT RESULT AND NOTHING ELSE. The gap columns
    # say how close a trade came; they never decide whether it lost. Mixing
    # the two is what made this section contradict the P&L sweep.
    sg = [t.get("settle_gap") for t in trades
          if t.get("settle_gap") is not None
          and np.isfinite(t.get("settle_gap"))]
    g60 = [t.get("min_gap_60") for t in trades
           if t.get("min_gap_60") is not None
           and np.isfinite(t.get("min_gap_60"))]
    mg = [t.get("min_gap_seen") for t in trades
          if t.get("min_gap_seen") is not None
          and np.isfinite(t.get("min_gap_seen"))]

    margin = {}
    if sg:
        sga = np.array(sg, dtype=float)
        margin["min_settle_gap"] = float(sga.min())
        margin["p05_settle_gap"] = float(np.percentile(sga, 5))
        margin["median_settle_gap"] = float(np.median(sga))
        # Won, but with less than this much room. NOT losses.
        margin["near_miss_25"] = int((sga < 25).sum())
        margin["near_miss_50"] = int((sga < 50).sum())
        # Settled below the strike ACCORDING TO THE GAP. Cross-checked against
        # the settlement result below; a mismatch means the recording stopped
        # early or BRTI diverged from the Coinbase feed, and is reported.
        margin["gap_says_lost"] = int((sga < 0).sum())
    if g60:
        margin["breached_in_60s"] = int((np.array(g60, dtype=float) < 0).sum())
    if mg:
        mga = np.array(mg, dtype=float)
        margin["min_gap_ever"] = float(mga.min())
        margin["breached_intrahour"] = int((mga < 0).sum())

    # The authoritative loss count, and a consistency flag.
    _settled_losses = sum(1 for t in trades if not t["win"])
    margin["settled_losses"] = _settled_losses
    if "gap_says_lost" in margin:
        margin["gap_vs_settle_mismatch"] = abs(
            margin["gap_says_lost"] - _settled_losses)

    return {
        "sigmas": sigmas, "tau_window": f"{tau_lo}-{tau_hi}",
        **margin, **extra,
        "trades": len(df), "wins": wins, "losses": losses,
        "win_rate": wr, "avg_entry_ask": avg_ask,
        "ev_cents_per_contract": ev,
        "total_pnl_cents": df["pnl_cents"].sum(),
        "trades_per_event": len(df) / max(1, total_events),
        "breakeven_needed": be,
        "verdict": verdict,
    }


def regime_of(ts, per_day=False):
    """
    (day_key, hour_block_label) for a unix timestamp, bucketed in the
    REGIME TIMEZONE -- the same function the bot uses (regime_time.py).

    per_day=False -> 'weekday'/'weekend' (default; more data per bucket)
    per_day=True  -> 'mon'..'sun'        (7x more buckets, 7x less data each)

    This MUST match the bot. If the tuner fits parameters using UTC buckets
    while the bot applies them using Eastern buckets, the fitted values point
    at the wrong hours and the tuning is worthless.
    """
    day_type, h = _rt.regime_key_from_ts(ts)
    if per_day:
        day_type = _rt.day_name_from_ts(ts)
    if not per_day and day_type == "weekend":
        block = "00-24"
    elif 13 <= h < 17:
        block = "13-17"
    elif 17 <= h < 21:
        block = "17-21"
    elif 6 <= h < 13:
        block = "06-13"
    elif 21 <= h < 24:
        block = "21-24"
    else:
        block = "00-06"
    return day_type, block


def regime_report(all_trades, per_day=False, tau_key=None):
    """
    Per (regime, gap_k): trades, wins, losses. This is what you use to fill
    in config.REGIME_GAP_SIGMAS with measured numbers instead of seeds.
    """
    rows = []
    for key, trades in all_trades.items():
        # Must match the tau window the global recommendation uses, or the
        # two blocks describe different strategies. Previously hardcoded to
        # config's window while the global block reported the sweep winner.
        if tau_key and not key.endswith(tau_key):
            continue
        if not tau_key and not key.endswith(
                f"tau{int(CFG_TAU_LO)}-{int(CFG_TAU_HI)}"):
            continue
        gk = float(key.split("_")[0][1:])  # sigma value
        for t in trades:
            dtype, block = regime_of(t["ts"], per_day=per_day)
            rows.append({"day_type": dtype, "hour_block": block, "sigmas": gk,
                        "win": t["win"], "pnl_cents": t["pnl_cents"]})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    g = df.groupby(["day_type", "hour_block", "sigmas"], as_index=False).agg(
        trades=("win", "size"), wins=("win", "sum"),
        ev_cents=("pnl_cents", "mean"))
    g["losses"] = g["trades"] - g["wins"]
    g["win_rate"] = g["wins"] / g["trades"]
    return g.sort_values(["day_type", "hour_block", "sigmas"])


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
    ap.add_argument("--out", default="out/tuning.xlsx")
    ap.add_argument("--no-index", action="store_true",
                    help="do not create indexes. Only for a read-only copy; "
                        "everything will be much slower without them.")
    ap.add_argument("--no-margin", action="store_true",
                    help="skip the settlement-margin analysis. Faster, but "
                        "removes the only ranking signal when the sample has "
                        "no losses.")
    ap.add_argument("--per-day", action="store_true",
                    help="bucket regimes by SPECIFIC weekday (mon..sun) "
                        "instead of weekday/weekend. 7x more buckets, so it "
                        "needs roughly 7x the data before any bucket is "
                        "meaningful.")
    ap.add_argument("--no-depth", action="store_true",
                    help="don't require resting depth >= contract count. "
                        "Inflates results -- the bot DOES require depth.")
    ap.add_argument("--quick", action="store_true",
                    help="coarse grid (~5x faster). Full sweep is ~4 min on a "
                        "week of data; use --quick for a fast look.")
    a = ap.parse_args()
    _be_polite()

    global SIGMA_GRID, TAU_WINDOWS, GAP_MIN_ABS_GRID, RECENT_MOVE_MAX_GRID
    if a.quick:
        SIGMA_GRID = [2.0, 2.5, 3.0, 3.5, 4.0]
        TAU_WINDOWS = [(30, 180), (60, 240), (90, 240), (90, 420), (120, 600)]
        if (CFG_TAU_LO, CFG_TAU_HI) not in TAU_WINDOWS:
            TAU_WINDOWS.append((CFG_TAU_LO, CFG_TAU_HI))
        GAP_MIN_ABS_GRID = [0.0, 25.0, 50.0]
        RECENT_MOVE_MAX_GRID = [30.0, 1e9]
        _p("QUICK MODE: coarse grid. Omit --quick for the full sweep.\n")

    max_tau = max(hi for _, hi in TAU_WINDOWS)
    _p(f"loading (only rows the sweep can use, tau<={max_tau:.0f}, "
       f"ask>={MIN_ASK:.0f}c) ...")
    t_load = time.time()
    cand, spot, results, margins = load_candidates(
        a.db, max_tau, CFG_COUNT, require_depth=not a.no_depth,
        want_margin=not a.no_margin, build_index=not a.no_index)
    if cand.empty:
        raise SystemExit(
            "\nNo candidate rows at all.\n"
            "  Nothing was ever priced >= %.0fc with enough depth in the "
            "window.\n  Run: python3 scripts/audit.py" % MIN_ASK)

    df = enrich_fast(cand, spot, results, margins)
    total_events = int(spot["event_ticker"].nunique()) if len(spot) else 0
    _p(f"  load complete in {time.time()-t_load:.1f}s "
       f"({total_events} events, {len(df):,} scorable rows)\n")

    if df.empty:
        raise SystemExit("No scorable rows (candidates exist but are not "
                         "settled yet). Run backfill_settlements.py.")


    rows = []
    all_trades = {}
    count = CFG_COUNT

    total_combos = (len(TAU_WINDOWS) * len(SIGMA_GRID)
                   * len(GAP_MIN_ABS_GRID) * len(RECENT_MOVE_MAX_GRID))
    print(f"FULL cross-product sweep: {len(TAU_WINDOWS)} tau windows x "
         f"{len(SIGMA_GRID)} sigmas x {len(GAP_MIN_ABS_GRID)} gap floors x "
         f"{len(RECENT_MOVE_MAX_GRID)} speed limits")
    print(f"= {total_combos:,} combinations (N={count} contracts for P&L)\n")
    if total_combos > 4000:
        print("  (this may take a few minutes)\n")

    done = 0
    t_sweep = time.time()
    for tau_lo, tau_hi in TAU_WINDOWS:
        for sg in SIGMA_GRID:
            for gm in GAP_MIN_ABS_GRID:
                for rm in RECENT_MOVE_MAX_GRID:
                    tr = simulate(df, sg, tau_lo, tau_hi,
                                  gap_min_abs=gm, recent_move_max=rm,
                                  count=count)
                    # keep per-trade detail only for the default secondary
                    # gates, so the regime report stays comparable
                    if gm == CFG_GAP_MIN and rm == CFG_MOVE_MAX:
                        all_trades[f"s{sg}_tau{tau_lo}-{tau_hi}"] = tr
                    rows.append(summarize(tr, sg, tau_lo, tau_hi, total_events,
                                          gap_min_abs=gm, recent_move_max=rm))
                    done += 1
                    if done % 20 == 0 or done == total_combos:
                        el = time.time() - t_sweep
                        rate = done / el if el > 0 else 0
                        eta = (total_combos - done) / rate if rate > 0 else 0
                        pct = done / total_combos
                        bar = "#" * int(pct * 30) + "." * (30 - int(pct * 30))
                        _p(f"\r  [{bar}] {done:,}/{total_combos:,} "
                          f"({pct*100:.0f}%)  {rate:.0f}/s  ETA {eta:.0f}s   ",
                          end="")
    _p("")

    summary = pd.DataFrame(rows).sort_values(
        ["tau_window", "sigmas"]).reset_index(drop=True)

    # detail sheet for the current config's setting, if it fired anything
    import config as CFG
    cur_key = f"s{CFG.GAP_SIGMAS}_tau{int(CFG.TAU_MIN_SECONDS)}-{int(CFG.TAU_MAX_SECONDS)}"
    detail = pd.DataFrame(all_trades.get(cur_key, []))

    # weekday/weekend split at the current setting
    split = pd.DataFrame()
    if len(detail):
        detail["dt"] = pd.to_datetime(detail["ts"], unit="s", utc=True)
        detail["weekday"] = detail["dt"].dt.day_name()
        detail["is_weekend"] = detail["dt"].dt.dayofweek >= 5
        detail["utc_hour"] = detail["dt"].dt.hour
        split = detail.groupby("is_weekend", as_index=False).agg(
            trades=("win", "size"), wins=("win", "sum"),
            win_rate=("win", "mean"), ev_cents=("pnl_cents", "mean"))
        detail["dt"] = detail["dt"].dt.tz_localize(None)

    # Pin the regime breakdown to the same tau window as the global "best"
    # row, so the two suggestion blocks are mutually consistent.
    _best_tau_key = None
    _live0 = summary[summary["trades"] >= 10]
    if len(_live0):
        _b0 = _live0.sort_values(
            ["settled_losses", "trades"], ascending=[True, False]).iloc[0] \
            if "settled_losses" in _live0.columns else _live0.iloc[0]
        _best_tau_key = f"tau{_b0['tau_window']}"
    regimes = regime_report(all_trades, per_day=a.per_day,
                            tau_key=_best_tau_key)

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with pd.ExcelWriter(a.out, engine="openpyxl") as xw:
        summary.to_excel(xw, sheet_name="1_parameter_grid", index=False)
        if len(regimes):
            regimes.to_excel(xw, sheet_name="4_by_regime", index=False)
        if len(detail):
            detail.to_excel(xw, sheet_name="2_trades_current_config", index=False)
        if len(split):
            split.to_excel(xw, sheet_name="3_weekday_vs_weekend", index=False)
        from openpyxl.styles import Font
        for ws in xw.book.worksheets:
            for row in ws.iter_rows(max_row=min(ws.max_row, 3000)):
                for c in row:
                    c.font = Font(name="Arial", size=10, bold=(c.row == 1))
            for col in ws.columns:
                w = max((len(str(c.value)) for c in col[:150] if c.value is not None),
                       default=10)
                ws.column_dimensions[col[0].column_letter].width = min(max(w + 2, 10), 46)
            ws.freeze_panes = "A2"

    print(f"\nwrote {a.out}\n")
    print("=" * 78)
    print("PARAMETER GRID (sorted by trade count within each window)")
    print("=" * 78)
    show = summary[summary["trades"] > 0].copy()
    if len(show):
        show["win_rate"] = show["win_rate"].map(lambda v: f"{v:.1%}" if pd.notna(v) else "-")
        show["ev_cents_per_contract"] = show["ev_cents_per_contract"].round(2)
        show = show.sort_values(["losses", "trades"], ascending=[True, False])
        cols = ["sigmas", "tau_window", "gap_min_abs", "recent_move_max",
                "trades", "wins", "losses", "win_rate",
                "ev_cents_per_contract", "verdict"]
        cols = [c for c in cols if c in show.columns]
        print(show[cols].head(40).to_string(index=False))
        print(f"\n(showing top 40 of {len(show)} combinations that traded; "
             f"full list in the spreadsheet)")
    else:
        print("No parameter combination produced any trades.")
        print("Either the recorder needs more time, or every gap_k is too strict.")
        print("Try lowering GAP_K_GRID in this script and re-running.")

    # ---- ranked leaderboard: the actionable output of the full sweep ----
    live = summary[summary["trades"] >= 10].copy()
    if len(live):
        print("\n" + "=" * 78)
        print("TOP COMBINATIONS  (>=10 trades, ranked: 0 losses first, then")
        print("most trades, then best EV)")
        print("=" * 78)
        # RANKING. With losses present, more trades at zero losses is a
        # genuine result. With NO losses anywhere, "most trades" just picks
        # the loosest setting in the grid -- so fall back to margin, which
        # still varies.
        _has_losses = live["losses"].sum() > 0
        if _has_losses or "min_settle_gap" not in live.columns:
            live["rank_key"] = list(zip(live["losses"] == 0,
                                        live["trades"],
                                        live["ev_cents_per_contract"].fillna(-99)))
            live = live.sort_values("rank_key", ascending=False)
            _rank_basis = "0 losses, then most trades, then EV"
        else:
            # No losses anywhere: rank by how much room the WORST trade had,
            # then by trade count as a tiebreak. This prefers settings that
            # were never close, over settings that merely never lost.
            live = live.sort_values(
                ["min_settle_gap", "trades"], ascending=[False, False])
            _rank_basis = ("worst-case settlement margin (no losses exist "
                           "to rank on), then most trades")
        print(f"ranked by: {_rank_basis}")
        cols = ["tau_window", "sigmas", "gap_min_abs", "recent_move_max",
                "trades", "wins", "losses", "win_rate", "ev_cents_per_contract"]
        top = live.head(25)[cols].copy()
        top["win_rate"] = top["win_rate"].map(
            lambda v: f"{v:.2%}" if pd.notna(v) else "-")
        top["ev_cents_per_contract"] = top["ev_cents_per_contract"].round(3)
        top["recent_move_max"] = top["recent_move_max"].map(
            lambda v: "off" if v >= 1e8 else f"{v:.0f}")
        print(top.to_string(index=False))

        b = live.iloc[0]
        lo, hi = (int(x) for x in b["tau_window"].split("-"))
        print()
        print("-" * 78)
        print("BEST BY THIS RANKING -- paste into config.py:")
        print("-" * 78)
        print(f"TAU_MIN_SECONDS = {lo}")
        print(f"TAU_MAX_SECONDS = {hi}")
        _bs = max(float(b["sigmas"]), SIGMA_FLOOR)
        if float(b["sigmas"]) < SIGMA_FLOOR:
            print(f"# grid best was {b['sigmas']}, RAISED to the "
                 f"{SIGMA_FLOOR} floor (see SIGMA_FLOOR in this script)")
        print(f"GAP_SIGMAS = {_bs}")
        print(f"GAP_MIN_ABS_USD = {b['gap_min_abs']:.0f}")
        rmv = b["recent_move_max"]
        print(f"RECENT_MOVE_MAX_USD = "
             f"{'1e9  # gate off' if rmv >= 1e8 else f'{rmv:.0f}'}")
        print(f"# -> {int(b['trades'])} trades, {int(b['losses'])} losses "
             f"over the recorded sample")
        print()
        print("CAUTION: this is the best fit to data you ALREADY HAVE. With")
        print("losses this rare, the top row is partly luck. Prefer a setting")
        print("that is also sensible (not an isolated spike in the grid), and")
        print("sanity-check neighbours in sheet 1_parameter_grid.")
    else:
        print("\nNo combination produced >=10 trades. Let the recorder run")
        print("longer, or widen the grids at the top of this script.")

    # ---------------- MARGIN: the ranking signal with zero losses -----------
    _mcols = [c for c in ("min_settle_gap", "p05_settle_gap", "near_miss_25",
                          "settled_losses", "breached_in_60s",
                          "breached_intrahour") if c in summary.columns]
    if _mcols and len(live):
        print("\n" + "=" * 78)
        print("SETTLEMENT MARGIN -- HOW CLOSE IT CAME")
        print("=" * 78)
        print("""
LOSSES come from the settlement result, and only from there. The gap columns
below say how close a trade came; they never decide whether it lost.

  settled_losses      trades whose market settled against them. THE loss count.
  min_settle_gap      smallest $ between spot and strike on the final tick
  p05_settle_gap      5th percentile of that -- the routine bad case
  near_miss_25        WON with under $25 of room (not losses)
  breached_in_60s     spot dipped below the strike inside the 60s BRTI
                      averaging window, then recovered (not losses)
  breached_intrahour  dipped below at any point in the window (not losses)

A setting with more near-misses and breaches is closer to losing whether or
not it has lost yet. That is the early-warning signal.
""")
        show = live.sort_values("sigmas")
        cols = ["sigmas", "tau_window", "trades"] + _mcols
        t = show[cols].drop_duplicates(subset=["sigmas", "tau_window"]).copy()
        for c in ("min_settle_gap", "p05_settle_gap"):
            if c in t.columns:
                t[c] = t[c].map(lambda v: f"${v:,.0f}" if pd.notna(v) else "-")
        print(t.head(40).to_string(index=False))

        # Consistency check between the two sections.
        if "gap_vs_settle_mismatch" in summary.columns:
            _mm = summary["gap_vs_settle_mismatch"].fillna(0)
            if (_mm > 0).any():
                print()
                print("NOTE: on some settings the gap on the final recorded")
                print("tick disagrees with the settlement result. That means")
                print("the recorder stopped before the close, or BRTI diverged")
                print("from the Coinbase feed. The SETTLEMENT RESULT is")
                print("authoritative; settled_losses is the number to trust.")

        if "settled_losses" in live.columns and live["settled_losses"].sum() == 0:
            print()
            print("settled_losses is 0 across every setting shown. Any")
            print("breach counts above are near-misses that RECOVERED, not")
            print("losses -- do not compute a win rate from them.")

    if len(regimes):
        print("\n" + "=" * 78)
        print("PER-REGIME RESULTS  (tau window: %s)"
             % (_best_tau_key[3:] if _best_tau_key
                else f"{CFG_TAU_LO:.0f}-{CFG_TAU_HI:.0f}"))
        print("=" * 78)
        rr = regimes.copy()
        rr["win_rate"] = rr["win_rate"].map(lambda v: f"{v:.1%}")
        rr["ev_cents"] = rr["ev_cents"].round(2)
        print(rr.to_string(index=False))
        print()
        # Can this sample discriminate at all?
        _total_losses = int(regimes["losses"].sum()) if "losses" in regimes else 0
        _discriminating = _total_losses > 0

        if not _discriminating:
            print("!! NO LOSSES ANYWHERE IN THIS SAMPLE.")
            print("!!")
            print("!! Every sigma from the bottom to the top of the grid shows")
            print("!! a perfect record, so win/loss counts CANNOT rank them.")
            print("!! 'Lowest sigma with 0 losses' would just return the")
            print("!! smallest number tested -- an artifact of the grid, not a")
            print("!! finding. Ranking below therefore uses SETTLEMENT MARGIN")
            print("!! (how much room was left at the close), which is a real")
            print("!! measurement that differs between settings.")
            print()

        print(f"SUGGESTED config.REGIME_GAP_SIGMAS  (floor {SIGMA_FLOOR} enforced;")
        print("ordering follows session liquidity, deep sessions get less sigma).")
        print("-" * 78)
        print("REGIME_GAP_SIGMAS = {")
        for (dtype, block), g in regimes.groupby(["day_type", "hour_block"]):
            ok = g[(g["losses"] == 0) & (g["trades"] >= 10)]  # settled losses
            h0, h1 = block.split("-")
            if len(ok):
                raw = float(ok["sigmas"].min())
                k = max(raw, SIGMA_FLOOR)
                n = int(ok[ok["sigmas"] == ok["sigmas"].min()]["trades"].iloc[0])
                note = f"# {n} trades, 0 losses"
                if raw < SIGMA_FLOOR:
                    note += f" (grid said {raw}, RAISED to the {SIGMA_FLOOR} floor)"
                print(f'    ("{dtype}", ({int(h0)}, {int(h1)})): {k},'
                     f'   {note}')
            else:
                mx = int(g["trades"].max()) if len(g) else 0
                print(f'    # ("{dtype}", ({int(h0)}, {int(h1)})): '
                     f'insufficient data ({mx} trades max)')
        print("}")
        if not _discriminating:
            print()
            print("REMINDER: with 0 losses everywhere these values are the FLOOR,")
            print("not an optimum. They are the most aggressive setting the rules")
            print("allow, not the setting the data chose.")
        print()
        print()
        print("=" * 78)
        print("WHICH BLOCK TO USE -- THEY ARE ALTERNATIVES, NOT A SET")
        print("=" * 78)
        print("""
REGIME_PARAMS takes precedence over REGIME_GAP_SIGMAS, and both take
precedence over the global GAP_SIGMAS. Pasting all three does not combine
them -- only REGIME_PARAMS would be live, and the other two would be dead
config that still looks authoritative when you read it back.

PICK ONE:

  A) SIMPLEST -- global only. Paste the global block. Delete/comment
     REGIME_PARAMS and leave REGIME_GAP_SIGMAS as-is only if
     USE_REGIME_OVERRIDES = False.

  B) PER-SESSION sigma -- paste REGIME_GAP_SIGMAS, keep REGIME_PARAMS = {},
     set USE_REGIME_OVERRIDES = True. The global GAP_SIGMAS then only
     applies to hours no block covers.

  C) FULL per-regime -- paste REGIME_PARAMS only. It overrides everything
     it names.

All blocks below are computed on the SAME tau window, so they are
comparable. Whichever you pick, set TAU_MIN_SECONDS / TAU_MAX_SECONDS from
the global block -- the regime blocks do not carry a tau window unless you
add one per entry.
""")
        print("SUGGESTED config.REGIME_PARAMS  (option C -- overrides everything")
        print("it names). Only regimes with >=10 trades and 0 settled losses")
        print("are emitted; the rest are commented out.")
        print("-" * 78)
        print("REGIME_PARAMS = {")
        for (dtype, block), g in regimes.groupby(["day_type", "hour_block"]):
            ok = g[(g["losses"] == 0) & (g["trades"] >= 10)]  # settled losses
            h0, h1 = block.split("-")
            if len(ok):
                _raw = float(ok["sigmas"].min()) if "sigmas" in ok.columns else None
                k = max(_raw, SIGMA_FLOOR) if _raw is not None else None
                n = int(ok["trades"].max())
                kv = f'"sigmas": {k}, ' if k is not None else ""
                print(f'    ("{dtype}", ({int(h0)}, {int(h1)})): '
                     f'{{{kv}"enabled": True}},   # {n} trades, 0 losses')
            else:
                mx = int(g["trades"].max()) if len(g) else 0
                print(f'    # ("{dtype}", ({int(h0)}, {int(h1)})): '
                     f'insufficient data ({mx} trades)')
        print("}")
        print()
        print("REMINDER: 0 losses over tens of trades is NOT proof of safety.")
        print("Break-even at 99c is 99.07%. Use this to REJECT settings that")
        print("already show losses, not to confirm ones that do not.")

    print("\n" + "=" * 78)
    print("HOW TO READ THIS")
    print("=" * 78)
    print("""
1. Ignore any row with 0 losses and fewer than ~200 trades. It is not
   evidence of safety, only of a small sample. Break-even at 99c is
   99.07%, so a run of 40 wins is exactly what break-even looks like too.
2. REJECT any sigma setting that already shows losses at your trade
   count -- if losses appear this early, the true rate is well above
   break-even.
3. Among the survivors, prefer the LOWEST sigma that still shows zero
   losses AND produces enough trades. Lower sigma = more trades = faster
   learning. But do not go below 2.5: under a normal distribution 2.5
   sigma is already only 1-in-161, and BTC's fat tails make the real
   number materially worse than that.
4. Check sheet 4_by_regime: if weekend differs from weekday, use the
   suggested REGIME_GAP_SIGMAS block rather than one global number.
5. Secondary sweep rows vary gap_min_abs and recent_move_max while
   holding the best window fixed -- use them to see whether those gates
   are doing anything or just blocking trades.
6. Whatever you pick, put it in config.py. Remember N=1 CANNOT profit at
   99c (fee rounding); 14 is the efficient minimum size.
""")


if __name__ == "__main__":
    main()