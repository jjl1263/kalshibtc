#!/usr/bin/env python3
"""
kalshi_recorder.py -- Track B: record what was actually available to trade.

Deliberately dumb. No signal logic, no thresholds, no orders. It watches the
last N minutes of every hourly BTC event and writes down what the book looked
like, once per second, plus BTC spot at the same instant.

This exists because Kalshi does not publish historical order books. If you do
not record it, it is gone forever.

Answers:
  * how often does a >=99c contract with real depth actually exist?
  * when does the book die, by hour of week?
  * what did each event settle at?

Usage:
    python3 kalshi_recorder.py --series KXBTCD
    python3 kalshi_recorder.py --series KXBTCD --window 15 --db data/recorder.db

Stop with Ctrl-C. Safe to restart at any time; it appends.
"""

import argparse
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone

import requests

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")
from event_select import select_hourly_event  # noqa: E402
try:
    import config as _cfg
    RECORD_ALL = getattr(_cfg, "RECORD_ALL", False)
    RECORD_MIN_ASK = getattr(_cfg, "RECORD_MIN_ASK_CENTS", 90)
    RECORD_GAP_WINDOW = getattr(_cfg, "RECORD_GAP_WINDOW_USD", 1500)
except Exception:
    RECORD_ALL, RECORD_MIN_ASK, RECORD_GAP_WINDOW = False, 90, 1500

BASE = "https://api.elections.kalshi.com/trade-api/v2"
SPOT_URL = "https://api.exchange.coinbase.com/products/BTC-USD/ticker"
BINANCE_SPOT_URL = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"

S = requests.Session()
S.headers.update({"Accept": "application/json", "User-Agent": "kalshi-recorder/1.0"})

RUNNING = True


def stop(*_):
    global RUNNING
    RUNNING = False
    print("\nshutting down after this tick ...")


signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)


# ---------------------------------------------------------------- storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS ticks (
    ts              REAL,
    event_ticker    TEXT,
    market_ticker   TEXT,
    floor_strike    REAL,
    cap_strike      REAL,
    tau             REAL,
    btc_spot        REAL,
    btc_spot_binance REAL,
    gap             REAL,
    yes_bid         INTEGER,
    yes_ask         INTEGER,
    no_bid          INTEGER,
    no_ask          INTEGER,
    last_price      INTEGER,
    volume          INTEGER,
    open_interest   INTEGER,
    ask_size_at_ask INTEGER,
    ask_size_98_100 INTEGER,
    bid_size_at_bid INTEGER,
    is_marked       INTEGER
);
CREATE INDEX IF NOT EXISTS ix_ticks_ev ON ticks(event_ticker);
CREATE INDEX IF NOT EXISTS ix_ticks_ts ON ticks(ts);

CREATE TABLE IF NOT EXISTS settlements (
    event_ticker  TEXT,
    market_ticker TEXT,
    floor_strike  REAL,
    result        TEXT,
    close_time    TEXT,
    recorded_at   REAL,
    UNIQUE(market_ticker)
);

CREATE TABLE IF NOT EXISTS runlog (
    ts REAL, level TEXT, msg TEXT
);
"""


def open_db(path):
    con = sqlite3.connect(path, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    con.commit()
    return con


def log(con, level, msg):
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp}] {level}: {msg}", flush=True)
    try:
        con.execute("INSERT INTO runlog VALUES (?,?,?)", (time.time(), level, msg))
        con.commit()
    except Exception:
        pass


# ---------------------------------------------------------------- api

def get(path, **params):
    """GET with retry/backoff on 429 and transient 5xx.

    Runs unattended for a week, so it must survive rate limits on its own
    rather than crashing the process.
    """
    delay = 1.0
    for attempt in range(6):
        r = S.get(BASE + path, params=params, timeout=15)
        if r.status_code == 429 or r.status_code >= 500:
            wait = float(r.headers.get("Retry-After", delay))
            time.sleep(wait)
            delay = min(delay * 2, 30)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return r.json()


def btc_spot():
    """Coinbase mid -- a real BRTI constituent exchange. This is the feed
    used for all trading decisions."""
    r = S.get(SPOT_URL, timeout=8)
    r.raise_for_status()
    return float(r.json()["price"])


def btc_spot_binance():
    """Binance -- NOT a BRTI constituent. Logged only to measure how much it
    diverges from the Coinbase feed above, since the offline breach study
    uses Binance history (free, 1s resolution) as a proxy for BRTI. If these
    two stay close, the study's numbers can be trusted; if they diverge
    during fast moves, that's the error band on the study's tail estimates.
    Never used for trading decisions -- comparison only."""
    try:
        r = S.get(BINANCE_SPOT_URL, timeout=5)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception:
        return None


def current_event(series):
    """
    Nearest-expiring HOURLY event only.

    Recording a long-dated contract would pollute the dataset: audit.py and
    tune_parameters.py count events and measure opportunity per event, so a
    12-hour contract mixed in would skew every availability statistic.
    """
    data = get("/events", series_ticker=series, status="open", limit=200,
               with_nested_markets="true")
    return select_hourly_event(data.get("events", []))


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


def orderbook(ticker, depth=12):
    try:
        return get(f"/markets/{ticker}/orderbook", depth=depth).get("orderbook", {})
    except Exception:
        return {}


def size_at(levels, price):
    """orderbook levels are [[price_cents, size], ...]"""
    if not levels:
        return 0
    for p, s in levels:
        if p == price:
            return s
    return 0


def _num(v):
    """Parse a value that may be a number or a fixed-point string."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def size_fp(m, base):
    """
    Contracts available at top of book, from the new '_size_fp' fixed-point
    fields on the market object (e.g. yes_ask_size_fp).

    This replaces the separate orderbook call: /markets/{t}/orderbook returns
    {} for many markets, and the market object already carries the size at
    best bid/ask, which is all the strategy needs.
    """
    v = _num(m.get(f"{base}_size_fp"))
    return v if v is not None else 0.0


def price_cents(m, base):
    """
    Read a price in CENTS from a market object, tolerating both the old
    integer-cent fields and the new fixed-point '_dollars' strings.

    Kalshi migrated to fixed-point dollar strings; integer-cent fields can be
    null on markets with sub-cent ticks, which silently produced price-less
    rows in the recorder. Returns a float so sub-cent prices survive.

        base = "yes_ask" -> tries yes_ask_dollars, then yes_ask
    """
    d = _num(m.get(f"{base}_dollars"))
    if d is not None:
        return d * 100.0
    c = _num(m.get(base))
    return c


def size_between(levels, lo, hi):
    if not levels:
        return 0
    return sum(s for p, s in levels if lo <= p <= hi)


# ---------------------------------------------------------------- main loop

def record_event(con, ev, close_dt, window_s, poll, max_books):
    et = ev["event_ticker"]
    mkts = markets_for(et)
    log(con, "INFO", f"event {et} closes {close_dt.isoformat()} "
                     f"({len(mkts)} markets)")

    strike_of = {m["ticker"]: m.get("floor_strike") for m in mkts}
    cap_of = {m["ticker"]: m.get("cap_strike") for m in mkts}
    ticks = 0

    while RUNNING:
        now = datetime.now(timezone.utc)
        tau = (close_dt - now).total_seconds()
        if tau < -30:
            break
        if tau > window_s:
            time.sleep(min(20, tau - window_s))
            continue

        t0 = time.time()
        try:
            spot = btc_spot()
        except Exception as e:
            log(con, "WARN", f"spot fetch failed: {e}")
            spot = None
        spot_bn = btc_spot_binance()

        try:
            live = markets_for(et)
        except Exception as e:
            log(con, "WARN", f"markets fetch failed: {e}")
            time.sleep(poll)
            continue

        # the strike Kalshi labels "Current:" = highest strike below spot
        marked = None
        if spot is not None:
            below = [m["ticker"] for m in live
                     if m.get("floor_strike") is not None and m["floor_strike"] <= spot]
            if below:
                marked = max(below, key=lambda t: strike_of.get(t) or -1e18)

        # candidates worth pulling depth on: anything priced near certainty,
        # plus anything close to spot
        def interesting(m):
            ask = price_cents(m, "yes_ask") or 0
            bid = price_cents(m, "yes_bid") or 0
            if ask >= 90 or bid >= 90:
                return True
            if spot and m.get("floor_strike") is not None:
                return abs(spot - m["floor_strike"]) <= 600
            return False

        # Depth now comes straight off the market object via *_size_fp.
        # The old per-strike /orderbook calls returned {} for most markets
        # AND cost one API request each, which capped us at --max-books
        # strikes per tick. Reading size_fp gives depth for ALL 188 strikes
        # every tick, at zero extra requests.
        rows = []
        ts = time.time()
        for m in live:
            ask = price_cents(m, "yes_ask")
            bid = price_cents(m, "yes_bid")
            ask_sz = size_fp(m, "yes_ask")
            bid_sz = size_fp(m, "yes_bid")
            if not ask and not bid:
                continue  # no quote at all
            fs = strike_of.get(m["ticker"])

            # Storage filter -- see config.RECORD_* . Keep a row only if it
            # carries information: a real offer in the band we trade, OR a
            # strike near spot (context the tuner needs). Everything else is
            # a far-OTM 1c strike or a deep-ITM ask=100 "no offer" row.
            if not RECORD_ALL:
                real_offer = (ask is not None and RECORD_MIN_ASK <= ask < 100)
                near_money = (spot is not None and fs is not None
                              and abs(spot - fs) <= RECORD_GAP_WINDOW)
                is_marked_row = (m["ticker"] == marked)
                if not (real_offer or near_money or is_marked_row):
                    continue
            rows.append((
                ts, et, m["ticker"], fs, cap_of.get(m["ticker"]), tau, spot, spot_bn,
                (spot - fs) if (spot is not None and fs is not None) else None,
                bid, ask, price_cents(m, "no_bid"), price_cents(m, "no_ask"),
                price_cents(m, "last_price"), _num(m.get("volume_fp")),
                _num(m.get("open_interest_fp")),
                ask_sz,
                ask_sz,
                bid_sz,
                1 if m["ticker"] == marked else 0,
            ))

        if rows:
            con.executemany(
                "INSERT INTO ticks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            con.commit()
            ticks += 1

        if ticks % 30 == 1:
            # count REAL offers at/above the trading floor (ask==100 means
            # no offer, so it is excluded)
            near = sum(1 for m in live
                      if 99 <= (price_cents(m, "yes_ask") or 0) < 100
                      and size_fp(m, "yes_ask") > 0)
            log(con, "TICK", f"tau={tau:6.0f}s spot={spot} "
                             f"real_offers_at_99c={near}")

        time.sleep(max(0.0, poll - (time.time() - t0)))

    # Settlement sweep. Kalshi does NOT settle instantly at close -- it can
    # take minutes. So retry with backoff, and only ever write real results;
    # never overwrite a good result with a blank.
    for attempt, wait in enumerate([60, 120, 300, 600], 1):
        if not RUNNING:
            break
        time.sleep(wait)
        try:
            rows = []
            for m in markets_for(et):
                res = m.get("result")
                if res in ("yes", "no"):
                    rows.append((et, m["ticker"], m.get("floor_strike"), res,
                                m.get("close_time"), time.time()))
            if rows:
                con.executemany(
                    "INSERT OR REPLACE INTO settlements VALUES (?,?,?,?,?,?)", rows)
                con.commit()
                log(con, "INFO", f"settled {et}: {len(rows)} markets "
                                 f"(attempt {attempt})")
                break
            log(con, "INFO", f"{et} not settled yet (attempt {attempt})")
        except Exception as e:
            log(con, "WARN", f"settlement sweep failed for {et}: {e}")
    else:
        log(con, "WARN", f"{et} never settled during sweep -- run "
                         f"backfill_settlements.py later")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", required=True,
                    help="hourly BTC series ticker from kalshi_discover.py")
    ap.add_argument("--db", default="data/recorder.db")
    ap.add_argument("--window", type=float, default=15,
                    help="minutes before close to start recording")
    ap.add_argument("--poll", type=float, default=1.0, help="seconds between ticks")
    ap.add_argument("--max-books", type=int, default=20,
                    help="how many strikes to pull full depth on each tick")
    a = ap.parse_args()

    import os
    os.makedirs(os.path.dirname(a.db) or ".", exist_ok=True)
    con = open_db(a.db)
    log(con, "INFO", f"recorder started series={a.series} window={a.window}min")

    seen = set()
    while RUNNING:
        try:
            ev, close_dt = current_event(a.series)
            if not ev:
                log(con, "INFO", "no HOURLY event open right now "
                                 "(long-dated contracts ignored); retry in 30s")
                time.sleep(30)
                continue
            if ev["event_ticker"] in seen:
                time.sleep(15)
                continue
            seen.add(ev["event_ticker"])
            record_event(con, ev, close_dt, a.window * 60, a.poll, a.max_books)
        except requests.HTTPError as e:
            log(con, "ERROR", f"http {e}")
            time.sleep(20)
        except Exception as e:
            log(con, "ERROR", f"{type(e).__name__}: {e}")
            time.sleep(20)

    n = con.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
    log(con, "INFO", f"stopped. {n:,} tick rows in {a.db}")
    con.close()


if __name__ == "__main__":
    main()