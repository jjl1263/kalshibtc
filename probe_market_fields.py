#!/usr/bin/env python3
"""
probe_market_fields.py -- dump what Kalshi ACTUALLY returns for KXBTCD.

Kalshi migrated to fixed-point "_dollars" price strings. Integer-cent fields
like `yes_ask` can now be null, which silently broke the recorder (every
tick logged with no price).

This prints the raw JSON of a few markets plus the market's price grid, so
we can see the real field names and the real tick size instead of guessing.

    python3 scripts/probe_market_fields.py
    python3 scripts/probe_market_fields.py --series KXBTCD
"""

import argparse
import json
import time

import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"
S = requests.Session()
S.headers.update({"Accept": "application/json"})


def get(path, **params):
    delay = 1.0
    for _ in range(5):
        r = S.get(BASE + path, params=params, timeout=20)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(float(r.headers.get("Retry-After", delay)))
            delay = min(delay * 2, 20)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", default="KXBTCD")
    a = ap.parse_args()

    evs = get("/events", series_ticker=a.series, status="open", limit=5,
              with_nested_markets="true").get("events", [])
    if not evs:
        raise SystemExit("no open events")

    # nearest event
    ev = evs[0]
    et = ev["event_ticker"]
    print(f"event: {et}\n")

    mkts = get("/markets", event_ticker=et, limit=1000).get("markets", [])
    print(f"{len(mkts)} markets\n")

    print("=" * 70)
    print("ALL FIELD NAMES on a market object")
    print("=" * 70)
    keys = sorted(mkts[0].keys())
    for k in keys:
        print(f"  {k}")

    print()
    print("=" * 70)
    print("PRICE-RELATED FIELDS (anything with price/bid/ask/dollar)")
    print("=" * 70)
    price_keys = [k for k in keys
                  if any(w in k.lower()
                        for w in ("price", "bid", "ask", "dollar"))]
    for k in price_keys:
        print(f"  {k}")

    print()
    print("=" * 70)
    print("PRICE GRID  <-- decides whether sub-cent prices exist")
    print("=" * 70)
    m0 = mkts[0]
    print(f"  price_level_structure: {m0.get('price_level_structure')}")
    print(f"  price_ranges         : {json.dumps(m0.get('price_ranges'))}")
    pls = m0.get("price_level_structure")
    if pls == "linear_cent":
        print("\n  -> WHOLE CENTS ONLY. 99c is the top price; there is no 99.5c.")
    elif pls:
        print("\n  -> SUB-CENT TICKS EXIST. Prices between 99c and 100c are")
        print("     real (e.g. 0.995, 0.999). Your '99c to 99.99c' was literal.")

    print()
    print("=" * 70)
    print("3 MARKETS NEAREST THE MONEY -- full raw JSON")
    print("=" * 70)

    def ask_of(m):
        for k in ("yes_ask_dollars", "yes_ask"):
            v = m.get(k)
            if v is not None:
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    continue
                return v if k.endswith("dollars") else v / 100.0
        return None

    live = [m for m in mkts if ask_of(m) is not None]
    print(f"markets with a readable ask: {len(live)} of {len(mkts)}\n")
    live.sort(key=lambda m: -(ask_of(m) or 0))
    for m in live[:3]:
        print(json.dumps(m, indent=2)[:1600])
        print("-" * 70)

    if not live:
        print("!! NO market had a readable ask under EITHER naming.")
        print("   Dumping one raw market so we can see what is actually there:")
        print(json.dumps(mkts[0], indent=2)[:2000])

    print()
    print("=" * 70)
    print("ORDERBOOK shape")
    print("=" * 70)
    t = (live[0] if live else mkts[0])["ticker"]
    ob = get(f"/markets/{t}/orderbook", depth=5).get("orderbook", {})
    print(f"ticker: {t}")
    print(json.dumps(ob, indent=2)[:1200])

    print()
    print("=" * 70)
    print("SEND THIS WHOLE OUTPUT BACK so the scripts can be fixed exactly.")
    print("=" * 70)


if __name__ == "__main__":
    main()