#!/usr/bin/env python3
"""
live_view.py -- what you were screenshotting, on screen continuously instead.

No API keys needed -- pure market data. Safe to run in a tmux pane next to
kalshi_live_bot.py, or entirely on its own while you trade by hand and just
want the numbers in front of you instead of eyeballing two browser tabs.

Shows, refreshed once a second:
  * current BTC spot and this event's countdown
  * 1h high/low/range -- the thing you were reading off Kalshi's chart
  * 5-min range and recent speed -- the "how fast did it happen" read
  * the marked ("Current:") strike
  * a ladder of nearby strikes with bid/ask, gap, and which gates they'd
    pass right now under config.py's thresholds -- so you can see what the
    bot sees, live, whether or not it's actually running

Usage:
    python3 live_view.py --series KXBTCD
"""

import argparse
import os
import sys
import time
from datetime import datetime, timezone

import requests

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")
from config_defaults import cfg as _cfgv  # noqa: E402
from event_select import select_hourly_event, describe_rejections  # noqa: E402
from kalshi_live_bot import (SpotHistory, required_gap,  # noqa: E402
                            resolve_regime_params, sigma_tau)

BASE = "https://api.elections.kalshi.com/trade-api/v2"
SPOT_URL = "https://api.exchange.coinbase.com/products/BTC-USD/ticker"

S = requests.Session()


def get(path, **params):
    r = S.get(BASE + path, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def _num(v):
    """Parse a value that may be a number or a fixed-point string."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def size_fp(m, base):
    """Contracts at top of book from the new '_size_fp' fixed-point fields."""
    v = _num(m.get(f"{base}_size_fp"))
    return v if v is not None else 0.0


def price_cents(m, base):
    """
    Price in CENTS, tolerating both legacy integer-cent fields and the new
    fixed-point '_dollars' strings. Returns a float so sub-cent ticks (which
    exist above $0.90 on several Kalshi price structures) are preserved.
    """
    d = _num(m.get(f"{base}_dollars"))
    if d is not None:
        return d * 100.0
    return _num(m.get(base))


def btc_spot():
    r = S.get(SPOT_URL, timeout=8)
    r.raise_for_status()
    return float(r.json()["price"])


def current_event(series):
    """Nearest-expiring HOURLY event only. See event_select.py."""
    data = get("/events", series_ticker=series, status="open", limit=200,
               with_nested_markets="true")
    return select_hourly_event(data.get("events", []))


def markets_for(event_ticker):
    return get("/markets", event_ticker=event_ticker, limit=1000).get("markets", [])


def clear():
    os.system("clear" if os.name != "nt" else "cls")


def fmt(v, w=8, d=1):
    return f"{v:>{w}.{d}f}" if v is not None else " " * (w - 1) + "-"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", default="KXBTCD")
    ap.add_argument("--poll", type=float, default=1.0)
    a = ap.parse_args()

    hist_1h = SpotHistory(seconds=3600)
    hist_5m = SpotHistory(seconds=300)
    ev, close_dt = None, None

    while True:
        now = time.time()
        try:
            spot = btc_spot()
            hist_1h.add(now, spot)
            hist_5m.add(now, spot)
        except Exception as e:
            spot = None
            print(f"spot fetch failed: {e}")

        if ev is None or (close_dt and datetime.now(timezone.utc) >= close_dt):
            try:
                ev, close_dt = current_event(a.series)
            except Exception as e:
                print(f"event fetch failed: {e}")
                time.sleep(2)
                continue

        # No hourly event right now -- normal for a few seconds after an hour
        # closes, before Kalshi lists the next one. Show a waiting screen
        # rather than an empty ladder, and never fall back to a long-dated
        # contract.
        if ev is None:
            clear()
            print(f"=== {a.series} live view ===  "
                 f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC\n")
            if spot is not None:
                print(f"BTC spot (Coinbase): ${spot:,.2f}\n")
            print("WAITING -- no hourly event open right now.")
            print("(The next hourly contract has not been listed yet.")
            print(" Long-dated KXBTCD contracts are deliberately ignored.)\n")
            try:
                data = get("/events", series_ticker=a.series, status="open",
                          limit=200, with_nested_markets="true")
                rej = describe_rejections(data.get("events", []))
                if rej:
                    print("open events in this series:")
                    for t, reason in rej[:8]:
                        print(f"    {t:24} {reason}")
            except Exception:
                pass
            print("\nCtrl-C to quit")
            time.sleep(max(a.poll, 2.0))
            continue

        tau = (close_dt - datetime.now(timezone.utc)).total_seconds() if close_dt else None

        try:
            mkts = markets_for(ev["event_ticker"]) if ev else []
        except Exception:
            mkts = []

        clear()
        print(f"=== {a.series} live view ===  "
             f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC\n")

        if spot is not None:
            print(f"BTC spot (Coinbase): ${spot:,.2f}")
        if ev:
            print(f"event: {ev['event_ticker']}   "
                 f"closes in: {tau:6.0f}s" if tau is not None else "")

        prices_1h = [p for _, p in hist_1h.buf]
        rng_1h = (max(prices_1h) - min(prices_1h)) if len(prices_1h) >= 30 else None
        rng_5m = hist_5m.range_5min()
        move10 = hist_5m.move_over(now, spot, _cfgv('RECENT_MOVE_WINDOW_SECONDS')) if spot else None

        print(f"\n1h range (this session so far): {fmt(rng_1h, 8, 0)}  "
             f"| 5m range: {fmt(rng_5m, 8, 0)}  "
             f"| last {_cfgv('RECENT_MOVE_WINDOW_SECONDS'):.0f}s move: {fmt(move10, 8, 1)}")
        # What the BOT will actually use this tick, not the globals. The M4
        # display change referenced these without ever computing them, which
        # was a NameError on the first strike row -- live_view never rendered.
        regime_label, _rp = resolve_regime_params(datetime.now(timezone.utc))
        sigmas = _rp["sigmas"]
        eff_tau_min = _rp["tau_min"]
        eff_tau_max = _rp["tau_max"]
        eff_count = int(_rp["contracts"])
        eff_depth = _rp["min_depth"] or eff_count
        regime_on = bool(_rp.get("enabled", True))
        st = sigma_tau(rng_5m, tau) if (rng_5m and tau) else None
        if rng_5m and tau:
            req = required_gap(rng_5m, _cfgv('GAP_MIN_ABS_USD'), sigmas, tau)
            print(f"regime: {regime_label}   required: {sigmas} sigma"
                 f"   sigma_tau=${st:,.0f}   -> need gap >= ${req:,.0f}")

        below = [m for m in mkts if m.get("floor_strike") is not None
                and spot and m["floor_strike"] <= spot]
        marked = max(below, key=lambda m: m["floor_strike"])["ticker"] if below else None

        print(f"\n{'STRIKE':>10} {'GAP':>8} {'SIGMAS':>7} {'BID':>4} {'ASK':>4} "
             f"{'MARKED':>7}  {'WINDOW':>7} {'PRICE':>6} {'GAP':>5} "
             f"{'SIZE':>6} {'DEPTH':>5}")
        print("-" * 74)
        near = [m for m in mkts if m.get("floor_strike") is not None
               and spot and abs(spot - m["floor_strike"]) <= 1000]
        near.sort(key=lambda m: m["floor_strike"])
        for m in near:
            fs = m["floor_strike"]
            gap = spot - fs if spot else None
            ask = price_cents(m, "yes_ask")
            bid = price_cents(m, "yes_bid")
            is_marked = "<<<" if m["ticker"] == marked else ""
            in_window = (tau is not None and eff_tau_min <= tau <= eff_tau_max)
            price_ok = (ask is not None and ask >= _cfgv('MIN_ASK_CENTS')
                       and ask < 100
                       and (_cfgv('MAX_ASK_CENTS') is None or ask <= _cfgv('MAX_ASK_CENTS')))
            req = (required_gap(rng_5m, _cfgv('GAP_MIN_ABS_USD'), sigmas, tau)
                  if (rng_5m and tau) else None)
            gap_ok = req is not None and gap is not None and gap >= req
            nsig = (gap / st) if (st and st > 0 and gap is not None) else None
            print(f"{fs:>10,.0f} {fmt(gap,8,0)} {fmt(nsig,7,1)} "
                 f"{(f'{bid:.0f}' if bid else '-'):>4} "
                 f"{(f'{ask:.0f}' if ask else '-'):>4} "
                 f"{is_marked:>7}  {'yes' if in_window else 'no':>7} "
                 f"{'yes' if price_ok else 'no':>6} {'yes' if gap_ok else 'no':>5} """
                 f"{(size_fp(m, 'yes_ask') or 0):>6.0f} "
                 f"{'yes' if (size_fp(m, 'yes_ask') or 0) >= eff_depth else 'no':>5}")

        _maxs = "none" if _cfgv('MAX_ASK_CENTS') is None else f"{_cfgv('MAX_ASK_CENTS')}c"
        _off = "" if regime_on else "   *** REGIME DISABLED -- BOT WILL NOT TRADE ***"
        print(f"\n(effective: tau=[{eff_tau_min:.0f},{eff_tau_max:.0f}]  "
             f"price>={_cfgv('MIN_ASK_CENTS')}c max={_maxs}  "
             f"{sigmas} sigma ({regime_label})  N={eff_count}  "
             f"depth>={eff_depth} -- edit config.py){_off}")
        print("\nCtrl-C to quit")

        time.sleep(a.poll)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass