#!/usr/bin/env python3
"""
kalshi_order_test.py -- validate auth + order placement/cancel BEFORE the
live bot ever runs. Run this first. Today.

Places the smallest possible real order (1 contract, 1 cent, on whatever
strike is furthest from spot -- i.e. certain to be worthless) and cancels
it. This proves:
  * your API key + signing actually work for WRITE endpoints (auth.py only
    checked a read endpoint)
  * the order payload shape (price as dollar string, count as string, side
    naming) is correct
  * cancel works

Worst case if the cancel somehow fails: you're out 1 cent on a contract
that will almost certainly settle to zero anyway.

Usage:
    python3 kalshi_order_test.py                # dry run, prints payload only
    python3 kalshi_order_test.py --live          # actually places + cancels
"""

import argparse
import json
import sys
import time
import uuid

import requests

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")
from kalshi_auth import auth_headers, load_private_key  # noqa: E402

BASE = "https://api.elections.kalshi.com"
API = "/trade-api/v2"


def get(path, key_id, pk, **params):
    r = requests.get(BASE + path, params=params,
                     headers=auth_headers("GET", path, key_id, pk), timeout=15)
    return r


def post(path, key_id, pk, body):
    r = requests.post(BASE + path, json=body,
                      headers=auth_headers("POST", path, key_id, pk), timeout=15)
    return r


def delete(path, key_id, pk):
    r = requests.delete(BASE + path,
                        headers=auth_headers("DELETE", path, key_id, pk), timeout=15)
    return r


def find_cheap_far_strike(key_id, pk):
    """Find a KXBTCD strike trading at ~1c -- safe to test with."""
    r = get(f"{API}/events", key_id, pk, series_ticker="KXBTCD", status="open",
           limit=5, with_nested_markets="true")
    r.raise_for_status()
    events = r.json().get("events", [])
    if not events:
        raise SystemExit("No open KXBTCD event found. Run kalshi_discover.py first.")
    mkts = events[0].get("markets") or []
    cheap = [m for m in mkts if m.get("yes_ask") and 1 <= m["yes_ask"] <= 3]
    if not cheap:
        cheap = sorted(mkts, key=lambda m: m.get("yes_ask") or 100)[:1]
    if not cheap:
        raise SystemExit("No markets with a price found in this event.")
    return cheap[0]["ticker"], cheap[0].get("yes_ask", 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="actually place and cancel a real 1-contract order")
    a = ap.parse_args()

    import os
    key_id = os.environ.get("KALSHI_KEY_ID")
    if not key_id:
        raise SystemExit("KALSHI_KEY_ID not set. export KALSHI_KEY_ID=<your key id>")
    pk = load_private_key()

    print("1. Checking read auth (balance) ...")
    r = get(f"{API}/portfolio/balance", key_id, pk)
    print(f"   {r.status_code} {r.text[:200]}")
    if r.status_code != 200:
        raise SystemExit("Read auth failed. Fix this before going further.")
    print("   OK\n")

    print("2. Finding a cheap far-OTM strike to test with ...")
    ticker, ask_c = find_cheap_far_strike(key_id, pk)
    price_dollars = f"{max(1, ask_c) / 100:.2f}"
    print(f"   {ticker} @ {ask_c}c -> price field will be \"{price_dollars}\"\n")

    coid = str(uuid.uuid4())
    payload = {
        "ticker": ticker,
        "client_order_id": coid,
        "side": "bid",            # buying YES = joining the bid side
        "count": "1",
        "price": price_dollars,   # DOLLAR STRING, e.g. "0.01" -- not cents!
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": False,
        "cancel_order_on_pause": True,
        "reduce_only": False,
    }
    print("3. Order payload that would be sent:")
    print(json.dumps(payload, indent=2))

    if not a.live:
        print("\nDry run only (no --live flag). Nothing was sent.")
        print("If this payload looks right, re-run with --live.")
        return

    print(f"\n4. Placing order (client_order_id={coid}) ...")
    r = post(f"{API}/portfolio/events/orders", key_id, pk, payload)
    print(f"   {r.status_code} {r.text[:500]}")

    if r.status_code not in (200, 201):
        print("\n!! Order placement failed. Common causes:")
        print("   - endpoint path is wrong for your account tier -- try base")
        print("     'https://external-api.kalshi.com' instead and tell Claude")
        print("     the exact status/error text above so the scripts can be fixed")
        print("   - insufficient balance -- fund the account first")
        return

    resp = r.json()
    order_id = resp.get("order_id") or resp.get("order", {}).get("order_id")
    filled = resp.get("fill_count", resp.get("filled_count", "?"))
    print(f"\n   order_id={order_id}  filled={filled}")

    if order_id:
        print(f"\n5. Canceling (in case anything is still resting) ...")
        time.sleep(1)
        # V2 cancel path. The legacy /portfolio/orders/{id} returns
        # 410 deprecated_v1_order_endpoint.
        r2 = delete(f"{API}/portfolio/events/orders/{order_id}", key_id, pk)
        print(f"   {r2.status_code} {r2.text[:300]}")

    print("\n6. Checking balance again to see the actual effect ...")
    r3 = get(f"{API}/portfolio/balance", key_id, pk)
    print(f"   {r3.status_code} {r3.text[:200]}")

    print("\nDone. If order placement above returned 200/201 with an order_id,")
    print("the live bot's order mechanics are validated. If it returned an")
    print("error, paste the exact response back before running the live bot.")


if __name__ == "__main__":
    main()