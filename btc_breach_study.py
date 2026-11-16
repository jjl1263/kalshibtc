#!/usr/bin/env python3
"""
btc_breach_study.py  --  Track A: the go/no-go analysis.

Question it answers:
    Given how BTC has been moving recently, and given N seconds left in the hour,
    how big a gap (spot above strike) do I need so that the chance of ending
    BELOW the strike is under 1-in-107 (the 99c break-even) or 1-in-53 (the 98c
    break-even)?

It uses free Binance 1-second kline data. No Kalshi account needed, no money at
risk. Every second in the file is a valid sample, so a few months gives millions
of observations -- enough to measure a 1-in-107 tail directly instead of
assuming a bell curve.

Two different breach types are measured, and they mean different things:
    TERMINAL  price at the END of the horizon is below the strike
              -> this is what settles your contract. THE important one.
    MAE       price DIPPED below the strike at any point in the horizon
              -> this is what trips your stop loss. Always >= terminal.

Usage:
    python3 btc_breach_study.py --start 2025-01 --end 2025-03
    python3 btc_breach_study.py --start 2024-01 --end 2025-12 --stride 5
    python3 btc_breach_study.py --start 2025-01 --end 2025-03 --keep-cache

Output: out/btc_breach_study.xlsx
"""

import argparse
import io
import shutil
import sys
import urllib.request
import urllib.error
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

BINANCE_URL = ("https://data.binance.vision/data/spot/monthly/klines/"
               "{sym}/1s/{sym}-1s-{ym}.zip")

KLINE_COLS = ["open_time", "open", "high", "low", "close", "volume",
              "close_time", "quote_volume", "count", "taker_buy_volume",
              "taker_buy_quote_volume", "ignore"]

# Seconds remaining in the hour that we evaluate.
HORIZONS = [30, 60, 90, 120, 180, 300, 600]

# Gap sizes tested, in basis points of spot.
# 1 bp = 0.01%. At BTC = $100,000, 1 bp = $10.
GAPS_BP = [1, 2, 3, 4, 5, 6, 8, 10, 15, 20, 30, 40, 60, 80, 120]

# Recent-volatility buckets: trailing 5-minute high-low range, in bp of spot.
# This is the "how fast has it been moving" conditioning variable.
RV5_EDGES_BP = [0, 5, 10, 20, 40, 80, np.inf]
RV5_LABELS = ["0-5bp (dead calm)", "5-10bp (calm)", "10-20bp (normal)",
              "20-40bp (active)", "40-80bp (fast)", "80bp+ (violent)"]

# Trailing 30-second drift buckets, in bp. Negative = falling = adverse for a
# YES-above position.
V30_EDGES_BP = [-np.inf, -3, 3, np.inf]
V30_LABELS = ["falling (adverse)", "flat", "rising (favourable)"]

# Break-even loss rates from the fee math.
BREAKEVEN = {"99c (1-in-107)": 1.0 / 107.0, "98c (1-in-53)": 1.0 / 53.0}

# Reference BTC price used only to add a dollar column next to the bp columns.
REF_PRICE = 100_000

LOOKBACK = 3600  # seconds of history needed before a sample is valid


# --------------------------------------------------------------------------
# Data loading
# --------------------------------------------------------------------------

def month_list(start, end):
    """['2025-01', '2025-02', ...] inclusive."""
    sy, sm = (int(x) for x in start.split("-"))
    ey, em = (int(x) for x in end.split("-"))
    out, y, m = [], sy, sm
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def fetch_month(sym, ym, cache_dir):
    """Download one month of 1s klines, return DataFrame(ts, high, low, close)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    zpath = cache_dir / f"{sym}-1s-{ym}.zip"

    if not zpath.exists():
        url = BINANCE_URL.format(sym=sym, ym=ym)
        print(f"  downloading {url}")
        try:
            with urllib.request.urlopen(url, timeout=300) as r, open(zpath, "wb") as f:
                shutil.copyfileobj(r, f)
        except urllib.error.HTTPError as e:
            print(f"  !! {ym} unavailable (HTTP {e.code}) -- skipping")
            if zpath.exists():
                zpath.unlink()
            return None
    else:
        print(f"  using cached {zpath.name}")

    with zipfile.ZipFile(zpath) as z:
        name = z.namelist()[0]
        with z.open(name) as fh:
            head = fh.read(200).decode("utf-8", "replace")
        has_header = "open_time" in head or "open" in head.split(",")[1:2]
        with z.open(name) as fh:
            df = pd.read_csv(
                io.BytesIO(fh.read()),
                header=0 if has_header else None,
                names=None if has_header else KLINE_COLS,
                usecols=[0, 2, 3, 4],
            )

    df.columns = ["open_time", "high", "low", "close"]
    # Binance switched from ms to us timestamps partway through 2025.
    if df["open_time"].iloc[0] > 1e15:
        df["open_time"] //= 1000
    df["ts"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.sort_values("ts").reset_index(drop=True)
    return df[["ts", "high", "low", "close"]].astype(
        {"high": "float64", "low": "float64", "close": "float64"})


# --------------------------------------------------------------------------
# Feature computation
# --------------------------------------------------------------------------

def add_features(df):
    """Backward-looking conditioning variables, all in bp of current spot."""
    close = df["close"]
    high = df["high"]
    low = df["low"]

    rv5 = high.rolling(300, min_periods=300).max() - low.rolling(300, min_periods=300).min()
    r60 = high.rolling(LOOKBACK, min_periods=LOOKBACK).max() - low.rolling(LOOKBACK, min_periods=LOOKBACK).min()
    burst_win = (high.rolling(300, min_periods=300).max()
                 - low.rolling(300, min_periods=300).min())
    burst_max = burst_win.rolling(LOOKBACK, min_periods=LOOKBACK).max()

    df["rv5_bp"] = rv5 / close * 1e4
    df["r60_bp"] = r60 / close * 1e4
    df["burst"] = (burst_max / r60).clip(0, 1)
    df["v30_bp"] = (close - close.shift(30)) / close * 1e4
    return df


def forward_moves(df, horizon):
    """
    Adverse (downward) move over the next `horizon` seconds, in bp.
      term_bp: spot now minus spot at horizon end   -> settlement risk
      mae_bp:  spot now minus lowest low in between -> stop-loss risk
    """
    close = df["close"]
    low = df["low"]

    fwd_close = close.shift(-horizon)
    term_bp = (close - fwd_close) / close * 1e4

    # min of low over [t+1, t+horizon]
    rev = low.shift(-1).iloc[::-1]
    fwd_min = rev.rolling(horizon, min_periods=horizon).min().iloc[::-1]
    mae_bp = (close - fwd_min) / close * 1e4

    return term_bp.to_numpy(), mae_bp.to_numpy()


# --------------------------------------------------------------------------
# Accumulation
# --------------------------------------------------------------------------

class Counts:
    """Accumulates breach counts across months without holding all data."""

    def __init__(self, years):
        self.years = sorted(years)
        self.yi = {y: i for i, y in enumerate(self.years)}
        shape_n = (len(years), len(RV5_LABELS), len(V30_LABELS), len(HORIZONS))
        shape_b = shape_n + (len(GAPS_BP),)
        self.n = np.zeros(shape_n, dtype=np.int64)
        self.b_term = np.zeros(shape_b, dtype=np.int64)
        self.b_mae = np.zeros(shape_b, dtype=np.int64)
        self.daily = {}  # date -> [samples, breaches] for the clustering sheet

    def add(self, year, rv_idx, v_idx, h_idx, term_bp, mae_bp, dates):
        yi = self.yi[year]
        valid = np.isfinite(term_bp) & np.isfinite(mae_bp) & (rv_idx >= 0) & (v_idx >= 0)
        if not valid.any():
            return
        rv_v, v_v = rv_idx[valid], v_idx[valid]
        t_v, m_v = term_bp[valid], mae_bp[valid]

        np.add.at(self.n, (yi, rv_v, v_v, h_idx), 1)
        for gi, g in enumerate(GAPS_BP):
            bt = t_v >= g
            if bt.any():
                np.add.at(self.b_term, (yi, rv_v[bt], v_v[bt], h_idx, gi), 1)
            bm = m_v >= g
            if bm.any():
                np.add.at(self.b_mae, (yi, rv_v[bm], v_v[bm], h_idx, gi), 1)

        # clustering reference cell: horizon 60s, gap 10bp
        if HORIZONS[h_idx] == 60:
            gi = GAPS_BP.index(10)
            d_v = dates[valid]
            br = t_v >= GAPS_BP[gi]
            for d, cnt in zip(*np.unique(d_v, return_counts=True)):
                rec = self.daily.setdefault(d, [0, 0])
                rec[0] += int(cnt)
            if br.any():
                for d, cnt in zip(*np.unique(d_v[br], return_counts=True)):
                    rec = self.daily.setdefault(d, [0, 0])
                    rec[1] += int(cnt)


def process_month(df, counts, year, stride):
    df = add_features(df)

    rv_idx_full = np.digitize(df["rv5_bp"].to_numpy(), RV5_EDGES_BP[1:-1])
    v_idx_full = np.digitize(df["v30_bp"].to_numpy(), V30_EDGES_BP[1:-1])
    rv_idx_full = np.where(np.isfinite(df["rv5_bp"].to_numpy()), rv_idx_full, -1)
    v_idx_full = np.where(np.isfinite(df["v30_bp"].to_numpy()), v_idx_full, -1)
    dates_full = df["ts"].dt.date.to_numpy()

    for h_idx, h in enumerate(HORIZONS):
        term, mae = forward_moves(df, h)
        sl = slice(LOOKBACK, len(df) - h, stride)
        counts.add(year, rv_idx_full[sl], v_idx_full[sl], h_idx,
                   term[sl], mae[sl], dates_full[sl])


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def build_frames(counts):
    rows = []
    for yi, year in enumerate(counts.years):
        for ri, rv in enumerate(RV5_LABELS):
            for vi, vl in enumerate(V30_LABELS):
                for hi, h in enumerate(HORIZONS):
                    n = counts.n[yi, ri, vi, hi]
                    if n == 0:
                        continue
                    for gi, g in enumerate(GAPS_BP):
                        bt = counts.b_term[yi, ri, vi, hi, gi]
                        bm = counts.b_mae[yi, ri, vi, hi, gi]
                        rows.append({
                            "year": year,
                            "recent_vol_5min": rv,
                            "drift_30s": vl,
                            "seconds_left": h,
                            "gap_bp": g,
                            f"gap_usd_at_{REF_PRICE//1000}k": round(g / 1e4 * REF_PRICE, 2),
                            "samples": int(n),
                            "settle_breaches": int(bt),
                            "settle_breach_rate": bt / n,
                            "settle_1_in": (n / bt) if bt else np.inf,
                            "stop_breaches": int(bm),
                            "stop_breach_rate": bm / n,
                            "beats_99c": bool(bt / n < BREAKEVEN["99c (1-in-107)"]),
                            "beats_98c": bool(bt / n < BREAKEVEN["98c (1-in-53)"]),
                        })
    return pd.DataFrame(rows)


def required_gap_table(full, label, mask_col=None):
    """Smallest gap (bp) whose settlement breach rate clears each break-even."""
    out = []
    grp = full.groupby(["recent_vol_5min", "seconds_left"], sort=False)
    for (rv, h), g in grp:
        g = g.sort_values("gap_bp")
        row = {"recent_vol_5min": rv, "seconds_left": h,
               "samples": int(g["samples"].max())}
        for name, thresh in BREAKEVEN.items():
            ok = g[g["settle_breach_rate"] < thresh]
            if len(ok):
                bp = float(ok["gap_bp"].iloc[0])
                row[f"gap_bp_{name.split()[0]}"] = bp
                row[f"gap_usd_{name.split()[0]}"] = round(bp / 1e4 * REF_PRICE, 0)
            else:
                row[f"gap_bp_{name.split()[0]}"] = np.nan
                row[f"gap_usd_{name.split()[0]}"] = np.nan
        out.append(row)
    df = pd.DataFrame(out)
    return df.sort_values(["recent_vol_5min", "seconds_left"])


def write_workbook(counts, full, path, args):
    pooled = (full.groupby(["recent_vol_5min", "drift_30s", "seconds_left", "gap_bp"],
                           as_index=False)
              .agg(samples=("samples", "sum"),
                   settle_breaches=("settle_breaches", "sum"),
                   stop_breaches=("stop_breaches", "sum")))
    pooled["settle_breach_rate"] = pooled["settle_breaches"] / pooled["samples"]
    pooled["stop_breach_rate"] = pooled["stop_breaches"] / pooled["samples"]
    pooled["settle_1_in"] = np.where(pooled["settle_breaches"] > 0,
                                     pooled["samples"] / pooled["settle_breaches"].replace(0, np.nan),
                                     np.inf)
    pooled[f"gap_usd_at_{REF_PRICE//1000}k"] = (pooled["gap_bp"] / 1e4 * REF_PRICE).round(0)

    all_drift = (pooled.groupby(["recent_vol_5min", "seconds_left", "gap_bp"], as_index=False)
                 .agg(samples=("samples", "sum"),
                      settle_breaches=("settle_breaches", "sum"),
                      stop_breaches=("stop_breaches", "sum")))
    all_drift["settle_breach_rate"] = all_drift["settle_breaches"] / all_drift["samples"]
    all_drift["stop_breach_rate"] = all_drift["stop_breaches"] / all_drift["samples"]

    req = required_gap_table(all_drift, "all")

    by_year = []
    for y in counts.years:
        sub = full[full["year"] == y]
        agg = (sub.groupby(["recent_vol_5min", "seconds_left", "gap_bp"], as_index=False)
               .agg(samples=("samples", "sum"), settle_breaches=("settle_breaches", "sum")))
        agg["settle_breach_rate"] = agg["settle_breaches"] / agg["samples"]
        t = required_gap_table(agg, str(y))
        t.insert(0, "year", y)
        by_year.append(t)
    by_year = pd.concat(by_year, ignore_index=True) if by_year else pd.DataFrame()

    daily = pd.DataFrame(
        [{"date": d, "samples": v[0], "breaches": v[1]} for d, v in sorted(counts.daily.items())])
    if len(daily):
        daily["breach_rate"] = daily["breaches"] / daily["samples"].replace(0, np.nan)

    readme = pd.DataFrame({
        "field": [
            "recent_vol_5min", "drift_30s", "seconds_left", "gap_bp", "gap_usd",
            "settle_breach_rate", "stop_breach_rate", "settle_1_in",
            "beats_99c", "beats_98c", "", "SOURCE", "PERIOD", "STRIDE", "SAMPLES",
        ],
        "meaning": [
            "Trailing 5-minute high-low range, in bp of spot. The 'how fast is it moving' bucket.",
            "Trailing 30-second price change. 'falling' is adverse for a YES-above position.",
            "Seconds remaining until the hour closes.",
            "Distance from spot down to the strike, in basis points (1bp = 0.01%).",
            f"Same gap in dollars, assuming BTC = ${REF_PRICE:,}. Scale for other prices.",
            "Fraction of samples where price ENDED below the strike. This settles your contract.",
            "Fraction where price DIPPED below the strike at some point. This trips your stop.",
            "1 / settle_breach_rate. Compare against 107 (99c) and 53 (98c).",
            "TRUE if settlement breach rate is under 1-in-107, i.e. a 99c buy is +EV.",
            "TRUE if settlement breach rate is under 1-in-53, i.e. a 98c buy is +EV.",
            "",
            "Binance BTCUSDT 1-second klines (data.binance.vision)",
            f"{args.start} to {args.end}",
            f"every {args.stride} seconds",
            "See 'samples' column in each sheet",
        ],
    })

    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        req.to_excel(xw, sheet_name="1_required_gap", index=False)
        all_drift.to_excel(xw, sheet_name="2_breach_all", index=False)
        pooled.to_excel(xw, sheet_name="3_breach_by_drift", index=False)
        if len(by_year):
            by_year.to_excel(xw, sheet_name="4_by_year", index=False)
        if len(daily):
            daily.to_excel(xw, sheet_name="5_daily_clustering", index=False)
        readme.to_excel(xw, sheet_name="0_readme", index=False)

        from openpyxl.styles import Font
        for ws in xw.book.worksheets:
            for row in ws.iter_rows():
                for c in row:
                    c.font = Font(name="Arial", size=10,
                                  bold=(c.row == 1))
            for col in ws.columns:
                width = max((len(str(c.value)) for c in col if c.value is not None), default=10)
                ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 10), 42)
            ws.freeze_panes = "A2"

    return req


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="first month, e.g. 2025-01")
    ap.add_argument("--end", required=True, help="last month, e.g. 2025-03")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--stride", type=int, default=5,
                    help="sample every Nth second (default 5; overlapping windows "
                         "are highly correlated so this loses little)")
    ap.add_argument("--cache", default="data/binance")
    ap.add_argument("--out", default="out/btc_breach_study.xlsx")
    ap.add_argument("--keep-cache", action="store_true",
                    help="keep downloaded zips (default deletes to save disk)")
    args = ap.parse_args()

    months = month_list(args.start, args.end)
    years = sorted({int(m[:4]) for m in months})
    counts = Counts(years)
    cache = Path(args.cache)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)

    processed = 0
    for ym in months:
        print(f"[{ym}]")
        df = fetch_month(args.symbol, ym, cache)
        if df is None or len(df) < LOOKBACK * 2:
            continue
        print(f"  {len(df):,} rows -> computing")
        process_month(df, counts, int(ym[:4]), args.stride)
        processed += 1
        del df
        if not args.keep_cache:
            zp = cache / f"{args.symbol}-1s-{ym}.zip"
            if zp.exists():
                zp.unlink()

    if processed == 0:
        print("No data processed. Check the month range.")
        sys.exit(1)

    print("building workbook...")
    full = build_frames(counts)
    req = write_workbook(counts, full, args.out, args)

    print(f"\nWrote {args.out}\n")
    print("=== HEADLINE: smallest gap that clears each break-even ===")
    print("(gap in USD assumes BTC = $%s)\n" % f"{REF_PRICE:,}")
    show = req[req["seconds_left"].isin([30, 60, 120])]
    with pd.option_context("display.width", 160, "display.max_rows", 200):
        print(show.to_string(index=False))
    print("\nNaN means NO tested gap was safe enough in that bucket.")
    print("Open sheet '1_required_gap' for the full table.")


if __name__ == "__main__":
    main()