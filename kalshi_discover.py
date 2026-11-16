#!/usr/bin/env python3
"""
kalshi_discover.py -- find the exact hourly BTC series and inspect its shape.

Run this FIRST. It answers the open questions the spec flagged:
  * what is the hourly BTC series ticker?
  * are the markets threshold ("$X or above", floor_strike only) or range
    buckets (floor_strike AND cap_strike)?  -- changes the whole risk model
  * what is the strike spacing?
  * how many strikes actually have a live book right now?

No API key needed -- market data is public.

Usage:
    python3 kalshi_discover.py
    python3 kalshi_discover.py --series KXBTCD     # inspect one series closely
"""

import argparse
import json
import time
from collections import Counter
from datetime import datetime, timezone

import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"
S = requests.Session()
S.headers.update({"Accept": "application/json", "User-Agent": "kalshi-recorder/1.0"})


def get(path, **params):
    """GET with retry/backoff on 429 and transient 5xx.

    Kalshi's public endpoints are rate limited; hammering /events across many
    pages with no delay trips it quickly. Respect Retry-After when present,
    otherwise back off exponentially.
    """
    delay = 1.0
    for attempt in range(6):
        r = S.get(BASE + path, params=params, timeout=20)
        if r.status_code == 429 or r.status_code >= 500:
            wait = float(r.headers.get("Retry-After", delay))
            print(f"  ...{r.status_code}, waiting {wait:.1f}s (attempt {attempt+1}/6)")
            time.sleep(wait)
            delay = min(delay * 2, 30)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return r.json()


CANDIDATE_TICKERS = [
    "KXBTCD",      # hourly strike-ladder ("$X or above"), best current guess
    "KXBTC",       # seen in older docs/examples
    "KXBTCH",
    "KXBTC1H",
    "KXBTCHOURLY",
]


def probe_ticker(st):
    """Does this exact series ticker have an open event right now?"""
    try:
        data = get("/events", series_ticker=st, status="open", limit=5,
                   with_nested_markets="true")
    except Exception:
        return None
    evs = data.get("events", [])
    if not evs:
        return None
    return evs[0]


def find_btc_series():
    """
    Two passes:
      1. Probe known/likely tickers directly -- fast, no pagination, can't be
         drowned out by unrelated sports/politics volume.
      2. If none hit, fall back to scanning open events (bounded, with
         backoff) as a last resort.
    """
    print("Pass 1: probing known BTC series tickers directly ...\n")
    hits = {}
    for st in CANDIDATE_TICKERS:
        ev = probe_ticker(st)
        if ev:
            mkts = ev.get("markets") or []
            print(f"  HIT  {st:<14} open now: {ev.get('event_ticker')} "
                 f"({len(mkts)} markets) -- {ev.get('title','')[:50]}")
            hits[st] = ev
        else:
            print(f"  ---  {st:<14} no open event")
        time.sleep(0.2)

    if hits:
        print(f"\nFound {len(hits)} candidate(s) above. The strike-ladder one "
             "(many strikes, not just up/down) is your target.")
        print("Confirm with:  python3 kalshi_discover.py --series <TICKER>\n")
        return hits

    print("\nNo direct hits. Pass 2: scanning open events (this may take a "
         "minute and can hit rate limits on a large exchange) ...\n")
    series = {}
    cursor = None
    pages = 0
    while pages < 40:
        params = {"status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = get("/events", **params)
        for ev in data.get("events", []):
            blob = f"{ev.get('title','')} {ev.get('event_ticker','')} {ev.get('series_ticker','')}".upper()
            if "BTC" in blob or "BITCOIN" in blob:
                st = ev.get("series_ticker") or ev.get("event_ticker", "").split("-")[0]
                rec = series.setdefault(st, {"title": ev.get("title", ""), "events": []})
                rec["events"].append(ev.get("event_ticker"))
        cursor = data.get("cursor")
        pages += 1
        if pages % 10 == 0:
            print(f"  ...scanned {pages*200:,} events, {len(series)} BTC series so far")
        if not cursor:
            break
        time.sleep(0.3)

    if not series:
        print("\nStill nothing. Kalshi's naming has likely changed. Open a BTC")
        print("hourly market on kalshi.com in a browser, look at the URL --")
        print("it's the segment right after /markets/, e.g.")
        print("  kalshi.com/markets/kxbtcd/bitcoin-price/kxbtcd-26aug100200")
        print("                     ^^^^^^ this is your series ticker")
        return {}

    print(f"\n{'SERIES':<20} {'EVENTS':>7}  TITLE")
    print("-" * 80)
    for st, rec in sorted(series.items(), key=lambda kv: -len(kv[1]["events"])):
        print(f"{st:<20} {len(rec['events']):>7}  {rec['title'][:52]}")
    print("\nNote: the hourly series may show only 1 open event, same as")
    print("everything else -- Kalshi opens one hour at a time. Don't rely on")
    print("event count to pick it; check the title/strikes with --series.")
    print("Confirm with:  python3 kalshi_discover.py --series <TICKER>\n")
    return series


def inspect(series_ticker):
    print(f"Inspecting series {series_ticker}\n" + "=" * 70)

    data = get("/events", series_ticker=series_ticker, status="open",
               limit=200, with_nested_markets="true")
    events = data.get("events", [])
    if not events:
        print("No open events. Wrong ticker, or the series is closed right now.")
        return

    def close_of(ev):
        ms = ev.get("markets") or []
        return min((m.get("close_time", "") for m in ms), default="")

    events.sort(key=close_of)
    now = datetime.now(timezone.utc)

    print(f"{len(events)} open events\n")
    for ev in events[:4]:
        ct = close_of(ev)
        try:
            dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
            mins = (dt - now).total_seconds() / 60
            when = f"{mins:+.1f} min"
        except Exception:
            when = "?"
        print(f"  {ev.get('event_ticker'):<34} closes {ct}  ({when})  "
              f"{len(ev.get('markets') or [])} markets")

    ev = events[0]
    markets = ev.get("markets") or []
    if not markets:
        markets = get("/markets", event_ticker=ev["event_ticker"], limit=1000).get("markets", [])

    print("\n" + "=" * 70)
    print(f"NEAREST EVENT: {ev.get('event_ticker')}   ({len(markets)} markets)")
    print("=" * 70)

    has_floor = sum(1 for m in markets if m.get("floor_strike") is not None)
    has_cap = sum(1 for m in markets if m.get("cap_strike") is not None)
    print(f"\nmarkets with floor_strike: {has_floor}")
    print(f"markets with cap_strike  : {has_cap}")
    if has_cap == 0:
        print(">> THRESHOLD contracts ('$X or above'). Risk model in the spec applies as written.")
    elif has_cap == has_floor:
        print(">> RANGE BUCKETS ('$X to $Y'). WARNING: a bucket can be exited on EITHER")
        print("   side, so the one-sided gap logic does NOT apply. Tell Claude before trading.")
    else:
        print(">> MIXED. Inspect individually.")

    strikes = sorted(m["floor_strike"] for m in markets if m.get("floor_strike") is not None)
    if len(strikes) <= 2:
        print("\n>> Only 1-2 strikes in this event. This looks like an UP/DOWN BINARY")
        print("   (e.g. KXBTC15M), not the strike-ladder market from your screenshot.")
        print("   Your strategy needs the ladder market -- try another candidate ticker.")
    elif len(strikes) > 2:
        diffs = Counter(round(b - a, 2) for a, b in zip(strikes, strikes[1:]))
        print(f"\nstrike range   : ${strikes[0]:,.0f} to ${strikes[-1]:,.0f}")
        print(f"strike spacing : {dict(diffs.most_common(4))}")

    live = []
    def _ask_cents(mk):
        """Ask in cents, tolerating the fixed-point '_dollars' migration."""
        d = mk.get("yes_ask_dollars")
        if d is not None:
            try:
                return float(d) * 100.0
            except (TypeError, ValueError):
                pass
        v = mk.get("yes_ask")
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    for m in markets:
        ask = _ask_cents(m)
        if ask and 0 < ask < 100:
            live.append(m)
    print(f"\nmarkets with a live yes_ask: {len(live)} of {len(markets)}")

    band = [m for m in live if (_ask_cents(m) or 0) >= 90]
    print(f"markets with yes_ask >= 90c: {len(band)}")
    print(f"\n{'TICKER':<40} {'STRIKE':>10} {'BID':>5} {'ASK':>5} {'VOL':>7}")
    print("-" * 74)
    for m in sorted(live, key=lambda x: -(_ask_cents(x) or 0))[:18]:
        _b = m.get("yes_bid_dollars")
        _b = (float(_b) * 100.0) if _b is not None else (m.get("yes_bid") or 0)
        _vol = m.get("volume_fp", m.get("volume", 0))
        print(f"{m['ticker']:<40} {m.get('floor_strike', 0):>10,.0f} "
              f"{_b:>5.0f} {(_ask_cents(m) or 0):>5.0f} {_vol:>7}")

    if live:
        t = sorted(live, key=lambda x: -(_ask_cents(x) or 0))[0]["ticker"]
        try:
            ob = get(f"/markets/{t}/orderbook", depth=10).get("orderbook", {})
            print(f"\norderbook sample for {t}:")
            print(json.dumps(ob, indent=2)[:700])
        except Exception as e:
            print(f"\norderbook fetch failed: {e}")

    print("\n" + "=" * 70)
    print("WRITE THIS DOWN for the recorder:")
    print(f"  --series {series_ticker}")
    print("Also check the series rules page for the SETTLEMENT PRICE SOURCE.")
    print("=" * 70)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", help="inspect one series in detail")
    a = ap.parse_args()
    if a.series:
        inspect(a.series)
    else:
        find_btc_series()