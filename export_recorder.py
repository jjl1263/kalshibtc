#!/usr/bin/env python3
"""
export_recorder.py -- turn recorder.db into a spreadsheet you can actually read.

You never need to open the SQLite file. Run this whenever you want a look.

    python3 export_recorder.py
    python3 export_recorder.py --hours 48 --out out/week1.xlsx
    python3 export_recorder.py --csv          # also dump full raw to CSV

Sheets:
    0_readme            what every column means
    1_opportunity       per event: was a tradeable contract available, and for how long
    2_by_hour           availability + book-death time by weekday x UTC hour
    3_book_death        seconds-before-close that depth at >=98c ran out
    4_price_ladder      how contract price relates to gap and time left
    5_settlements       what each event actually settled at
    6_raw_recent        raw ticks for the last N hours (Excel row limit applies)
"""

import argparse
import os
import sqlite3
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")
try:
    import config as _cfg
    MIN_ASK_DEFAULT = _cfg.MIN_ASK_CENTS
except Exception:
    MIN_ASK_DEFAULT = 99

XLSX_ROW_LIMIT = 1_000_000


def load(db, hours):
    con = sqlite3.connect(db)
    ticks = pd.read_sql_query("SELECT * FROM ticks", con)
    setl = pd.read_sql_query("SELECT * FROM settlements", con)
    con.close()
    if ticks.empty:
        raise SystemExit("No ticks recorded yet. Let the recorder run through an hour close.")
    ticks["dt"] = pd.to_datetime(ticks["ts"], unit="s", utc=True)
    ticks["weekday"] = ticks["dt"].dt.day_name()
    ticks["utc_hour"] = ticks["dt"].dt.hour
    return ticks, setl


def opportunity(ticks, min_size, min_ask):
    """
    Per event: did a tradeable contract exist at >= min_ask, and for how long.

    IMPORTANT -- yes_ask == 100 means NO OFFER, not "priced at 100c".
    Kalshi derives yes_ask = 100 - no_bid. When nobody bids NO, yes_ask
    reports 100 and yes_ask_size_fp is 0. Those rows are empty books and must
    never be counted as opportunities, which is why every filter below
    requires yes_ask < 100 AND depth > 0.
    """
    t = ticks.copy()
    tradeable = ((t["yes_ask"] >= min_ask) & (t["yes_ask"] < 100)
                 & (t["ask_size_at_ask"].fillna(0) >= min_size)
                 & (t["is_marked"] == 0))
    t["tradeable98"] = tradeable          # column name kept for compatibility
    t["tradeable99"] = tradeable

    rows = []
    for ev, g in t.groupby("event_ticker"):
        tr = g[g["tradeable98"]]
        rows.append({
            "event_ticker": ev,
            "first_seen_utc": g["dt"].min(),
            "weekday": g["weekday"].iloc[0],
            "utc_hour": g["utc_hour"].iloc[0],
            "ticks_recorded": g["ts"].nunique(),
            "strikes_seen": g["market_ticker"].nunique(),
            "any_tradeable_98": bool(len(tr)),
            "seconds_tradeable": tr["ts"].nunique(),
            "earliest_tau_tradeable": tr["tau"].max() if len(tr) else np.nan,
            "latest_tau_tradeable": tr["tau"].min() if len(tr) else np.nan,
            "best_gap_usd": tr["gap"].max() if len(tr) else np.nan,
            "max_ask_size": tr["ask_size_at_ask"].max() if len(tr) else np.nan,
            "n_99c_opportunities": int(g["tradeable99"].sum()),
        })
    df = pd.DataFrame(rows).sort_values("first_seen_utc")
    df["first_seen_utc"] = df["first_seen_utc"].dt.tz_localize(None)
    return df


def by_hour(opp):
    g = opp.groupby(["weekday", "utc_hour"], as_index=False).agg(
        events=("event_ticker", "count"),
        pct_with_opportunity=("any_tradeable_98", "mean"),
        median_seconds_tradeable=("seconds_tradeable", "median"),
        median_earliest_tau=("earliest_tau_tradeable", "median"),
        median_latest_tau=("latest_tau_tradeable", "median"),
        median_best_gap_usd=("best_gap_usd", "median"),
    )
    g["pct_with_opportunity"] = (g["pct_with_opportunity"] * 100).round(1)
    return g.sort_values(["weekday", "utc_hour"])


def book_death(ticks, min_size, min_ask):
    """Last tau at which depth >= min_size existed at 98c or better."""
    t = ticks[(ticks["yes_ask"] >= min_ask) & (ticks["yes_ask"] < 100)
              & (ticks["ask_size_at_ask"].fillna(0) >= min_size)]
    if t.empty:
        return pd.DataFrame()
    r = t.groupby(["event_ticker", "weekday", "utc_hour"], as_index=False).agg(
        book_death_tau=("tau", "min"),
        deepest_size=("ask_size_at_ask", "max"))
    r["book_death_tau"] = r["book_death_tau"].round(0)
    return r.sort_values(["weekday", "utc_hour"])


def price_ladder(ticks):
    t = ticks[ticks["yes_ask"].notna() & ticks["gap"].notna()].copy()
    if t.empty:
        return pd.DataFrame()
    t["tau_bucket"] = pd.cut(t["tau"], [-1, 30, 60, 120, 180, 300, 600, 1e9],
                             labels=["0-30s", "30-60s", "60-120s", "120-180s",
                                     "180-300s", "300-600s", "600s+"])
    t["gap_bucket"] = pd.cut(t["gap"], [-1e9, 0, 25, 50, 100, 200, 400, 1e9],
                             labels=["below strike", "$0-25", "$25-50", "$50-100",
                                     "$100-200", "$200-400", "$400+"])
    r = t.groupby(["gap_bucket", "tau_bucket"], as_index=False, observed=True).agg(
        n=("yes_ask", "size"),
        median_ask=("yes_ask", "median"),
        pct_tradeable=("yes_ask", lambda s: round(
            ((s >= MIN_ASK_DEFAULT) & (s < 100)).mean() * 100, 1)),
        median_ask_size=("ask_size_at_ask", "median"))
    return r


def readme_frame(min_size):
    return pd.DataFrame({
        "field": ["tau", "gap", "yes_ask", "ask_size_at_ask", "is_marked",
                  "any_tradeable_98", "seconds_tradeable", "earliest_tau_tradeable",
                  "book_death_tau", "pct_with_opportunity",
                  "btc_spot", "btc_spot_binance", "abs_diff_usd", "", "MIN_SIZE", "NOTE"],
        "meaning": [
            "Seconds remaining until the event closes.",
            "BTC spot minus this market's strike, in dollars. Positive = in the money.",
            "Cost in cents to buy YES right now.",
            "Contracts available at that ask price.",
            "1 if this is the strike Kalshi labels 'Current:' (nearest in-the-money).",
            f"TRUE if some strike was buyable at 98-99c with >= {min_size} contracts of depth, "
            "excluding the marked strike.",
            "How many distinct seconds such an opportunity existed during the event.",
            "Earliest point (seconds before close) an opportunity appeared.",
            "Last moment depth at >=98c still existed. This is what sets your entry deadline.",
            "Share of events in that weekday/hour cell that had any opportunity at all.",
            "Coinbase spot -- a real BRTI constituent exchange. Used for all trading decisions.",
            "Binance spot -- NOT a BRTI constituent. Logged only for comparison, never for "
            "trading. This is what the offline breach study (btc_breach_study.py) uses as its "
            "historical proxy for BRTI, since it's free at 1-second resolution and Coinbase's "
            "free API is not.",
            "abs(btc_spot_binance - btc_spot). See sheet 6_feed_divergence: if this stays small "
            "and stable, trust the breach study's numbers as-is. If it widens during fast moves, "
            "add that amount as extra margin on top of the study's required-gap thresholds.",
            "",
            str(min_size),
            "Availability is NOT profitability. This file says what you could have "
            "bought, not whether buying it was wise. That is the BTC breach study.",
        ]})


def feed_divergence(ticks):
    """
    How far Binance (used in the offline breach study) strays from Coinbase
    (a real BRTI constituent, used for all live trading decisions).
    Small/stable divergence -> trust the study's numbers.
    Divergence that widens with recent volatility -> the study understates
    tail risk exactly where it matters most, and gap thresholds should carry
    an extra safety margin sized to this number.
    """
    t = ticks[ticks["btc_spot"].notna() & ticks["btc_spot_binance"].notna()].copy()
    if t.empty:
        return pd.DataFrame(), pd.DataFrame()
    t = t.drop_duplicates(subset=["ts"])
    t["diff_usd"] = t["btc_spot_binance"] - t["btc_spot"]
    t["abs_diff_usd"] = t["diff_usd"].abs()
    t["tau_bucket"] = pd.cut(t["tau"], [-1, 30, 60, 120, 300, 600, 1e9],
                             labels=["0-30s", "30-60s", "60-120s", "120-300s",
                                     "300-600s", "600s+"])
    summary = pd.DataFrame({
        "metric": ["samples", "mean_abs_diff_usd", "median_abs_diff_usd",
                   "p95_abs_diff_usd", "p99_abs_diff_usd", "max_abs_diff_usd",
                   "mean_diff_usd_signed"],
        "value": [len(t), t["abs_diff_usd"].mean(), t["abs_diff_usd"].median(),
                 t["abs_diff_usd"].quantile(0.95), t["abs_diff_usd"].quantile(0.99),
                 t["abs_diff_usd"].max(), t["diff_usd"].mean()],
    })
    by_tau = t.groupby("tau_bucket", as_index=False, observed=True).agg(
        samples=("abs_diff_usd", "size"),
        mean_abs_diff_usd=("abs_diff_usd", "mean"),
        p95_abs_diff_usd=("abs_diff_usd", lambda s: s.quantile(0.95)),
        max_abs_diff_usd=("abs_diff_usd", "max"))
    return summary, by_tau


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/recorder.db")
    ap.add_argument("--out", default="out/recorder_report.xlsx")
    ap.add_argument("--hours", type=float, default=48,
                    help="how many hours of raw ticks to include in sheet 6")
    ap.add_argument("--min-ask", type=float, default=MIN_ASK_DEFAULT,
                    help="minimum ask in cents to count as tradeable "
                        "(from config.MIN_ASK_CENTS)")
    ap.add_argument("--min-size", type=int, default=1,
                    help="contracts of depth required to call it 'tradeable'")
    ap.add_argument("--csv", action="store_true", help="also dump full raw ticks to CSV")
    a = ap.parse_args()

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    ticks, setl = load(a.db, a.hours)
    print(f"loaded {len(ticks):,} tick rows across "
          f"{ticks['event_ticker'].nunique()} events")

    opp = opportunity(ticks, a.min_size, a.min_ask)
    hourly = by_hour(opp)
    death = book_death(ticks, a.min_size, a.min_ask)
    ladder = price_ladder(ticks)
    div_summary, div_by_tau = feed_divergence(ticks)

    cutoff = ticks["ts"].max() - a.hours * 3600
    raw = ticks[ticks["ts"] >= cutoff].drop(columns=["dt"])
    if len(raw) > XLSX_ROW_LIMIT:
        raw = raw.tail(XLSX_ROW_LIMIT)

    with pd.ExcelWriter(a.out, engine="openpyxl") as xw:
        readme_frame(a.min_size).to_excel(xw, sheet_name="0_readme", index=False)
        opp.to_excel(xw, sheet_name="1_opportunity", index=False)
        hourly.to_excel(xw, sheet_name="2_by_hour", index=False)
        if len(death):
            death.to_excel(xw, sheet_name="3_book_death", index=False)
        if len(ladder):
            ladder.to_excel(xw, sheet_name="4_price_ladder", index=False)
        if len(setl):
            setl.to_excel(xw, sheet_name="5_settlements", index=False)
        if len(div_summary):
            div_summary.to_excel(xw, sheet_name="6_feed_divergence", index=False)
            div_by_tau.to_excel(xw, sheet_name="6_feed_divergence", index=False,
                                startrow=len(div_summary) + 3)
        raw.to_excel(xw, sheet_name="7_raw_recent", index=False)

        from openpyxl.styles import Font
        for ws in xw.book.worksheets:
            for row in ws.iter_rows(max_row=min(ws.max_row, 5000)):
                for c in row:
                    c.font = Font(name="Arial", size=10, bold=(c.row == 1))
            for col in ws.columns:
                w = max((len(str(c.value)) for c in col[:200] if c.value is not None),
                        default=10)
                ws.column_dimensions[col[0].column_letter].width = min(max(w + 2, 10), 44)
            ws.freeze_panes = "A2"

    if a.csv:
        cpath = a.out.replace(".xlsx", "_raw.csv")
        ticks.drop(columns=["dt"]).to_csv(cpath, index=False)
        print(f"wrote {cpath}")

    print(f"\nwrote {a.out}")
    if len(opp):
        pct = opp["any_tradeable_98"].mean() * 100
        per_day = opp["any_tradeable_98"].sum() / max(1, opp["ticks_recorded"].count() / 24)
        print(f"\n=== HEADLINE ===")
        print(f"events recorded          : {len(opp)}")
        print(f"had a tradeable >={a.min_ask:.0f}c    : {opp['any_tradeable_98'].sum()} ({pct:.0f}%)")
        print(f"implied opportunities/day: {per_day:.1f}")
        print(f"median seconds available : "
              f"{opp.loc[opp['any_tradeable_98'], 'seconds_tradeable'].median():.0f}")
        print("\nKill criterion: fewer than ~4 opportunities/day means there is not")
        print("enough to trade, regardless of what the breach study says.")


if __name__ == "__main__":
    main()