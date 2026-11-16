#!/usr/bin/env python3
"""
kalshi_live_bot.py -- the live bot, running TODAY on your own manually-
validated heuristics instead of the empirically calibrated table the full
spec calls for. That table doesn't exist yet -- it can't, no matter how
fast this gets built, because it comes from data that takes days to
accumulate. This is not a shortcut around that; it's using the one dataset
you already have (your own hand-trading judgment) as the starting numbers,
at trivial size, while the recorder keeps calibrating in the background.

SAFETY MODEL FOR TODAY:
  * count is fixed at 1 contract. Do not raise this until you have trusted
    fills across many hours -- see --count if you insist, but don't.
  * default mode requires you to type 'y' before every order, in AND out.
    Pass --auto only after you've watched it propose good trades for a
    while.
  * one trade per hourly event, hard cap.
  * daily breaker halts after 3 losses.
  * every gate value is printed every tick so you can see exactly why it
    did or didn't act -- this IS your live diff-log against your own
    judgment, the thing the spec always wanted you keeping anyway.

RUN kalshi_order_test.py FIRST. This assumes that already passed.

QUICK ITERATION: every threshold lives in config.py. Edit that file, save,
rerun -- no need to touch this script or remember flag names. CLI flags
below override config.py for one-off experiments without editing the file.

Usage:
    python3 kalshi_live_bot.py --series KXBTCD                  # confirm mode
    python3 kalshi_live_bot.py --series KXBTCD --dry-run         # no orders at all
    python3 kalshi_live_bot.py --series KXBTCD --auto            # no confirmation
"""

import argparse
import csv
import math
import os
import select
import signal
import sys
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")
from kalshi_auth import auth_headers, load_private_key  # noqa: E402
import config as CFG  # noqa: E402
from config_defaults import cfg as _cfgv  # noqa: E402
from event_select import select_hourly_event, describe_rejections  # noqa: E402
import regime_time as _rt  # noqa: E402

BASE = "https://api.elections.kalshi.com"
API = "/trade-api/v2"
SPOT_URL = "https://api.exchange.coinbase.com/products/BTC-USD/ticker"

S = requests.Session()
S.headers.update({"Accept": "application/json"})

RUNNING = True


def stop(*_):
    global RUNNING
    RUNNING = False
    print("\nstopping after current tick ...")


signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)


# ---------------------------------------------------------------- api

def get(path, key_id, pk, **params):
    for attempt in range(4):
        r = S.get(BASE + path, params=params,
                 headers=auth_headers("GET", path, key_id, pk), timeout=15)
        if r.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        return r
    return r


def post(path, key_id, pk, body, retries=3):
    """
    POST with 429 backoff, matching get() and get_public().

    Previously bare. During the retry loop this fires ~once a second, and a
    429 came back as a generic "order error" that was immediately retried --
    feeding the rate limiter exactly when it was already complaining.

    Raises on network faults; every caller wraps it (see safe_order).
    """
    delay = 1.0
    last = None
    for attempt in range(retries + 1):
        r = S.post(BASE + path, json=body,
                   headers=auth_headers("POST", path, key_id, pk), timeout=15)
        last = r
        if r.status_code == 429:
            wait = float(r.headers.get("Retry-After", delay))
            print(f"  429 rate limited on order, waiting {wait:.1f}s")
            time.sleep(wait)
            delay = min(delay * 2, 20)
            continue
        return r
    return last


def json_or_none(r):
    """
    Parse a response body, tolerating non-JSON.

    A gateway or Cloudflare error page arrives with HTTP 200 more often than
    you would like, so a status check is not enough -- r.json() then raises
    JSONDecodeError on the next line. Returns None instead.
    """
    if r is None or not getattr(r, "text", ""):
        return None
    try:
        return r.json()
    except ValueError:
        body = (r.text or "")[:120].replace("\n", " ")
        print(f"  response was not JSON (status {r.status_code}): {body}")
        return None


def safe_order(fn, *a, **kw):
    """
    Run an order call, converting a network fault into (None, None, None).

    Order calls were the ONLY unprotected network path in the bot. An
    uncaught ConnectionError during an exit propagated
    manage_position -> run -> main and killed the process while contracts
    were still open -- the stop never executed and the only trace was the
    OPEN row. Callers treat a None response as "did not happen, retry".
    """
    try:
        return fn(*a, **kw)
    except Exception as e:
        print(f"  ORDER CALL FAILED ({type(e).__name__}: {e}) -- "
              "treating as NOT SENT, will retry")
        return None, None, None


def get_public(path, **params):
    for attempt in range(4):
        r = S.get(BASE + path, params=params, timeout=15)
        if r.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        return r
    return r


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


def to_tick(price_cents_val):
    """
    Snap a price to a whole cent Kalshi will accept, always rounding DOWN.

    Order payloads are formatted as f"{price/100:.2f}", which ROUNDS. A
    sub-cent ask of 99.75 became "1.00" -- an order to pay 100c for a 100c
    payout, a guaranteed loss before fees. A bid of 88.6 became a sell limit
    of 0.89, above the bid, which would never cross.

    Down is correct on both sides: on a buy it can only lower what we pay
    (and if it no longer crosses, the retry loop simply waits -- we never
    intended to pay above MIN_ASK_CENTS); on a protective sell it makes the
    limit more aggressive, so the exit still crosses the bid.
    """
    try:
        v = float(price_cents_val)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return max(1, min(99, int(math.floor(v))))


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


def current_event(series, verbose=False):
    """
    The nearest-expiring HOURLY event, or (None, None).

    Selection lives in event_select.py. The KXBTCD series also contains
    longer-dated contracts, and picking "nearest open event" would target one
    of those whenever the current hour has closed and the next is not yet
    listed. (None, None) means "wait and retry" -- never fall back to a
    long-dated contract.
    """
    try:
        r = get_public(f"{API}/events", series_ticker=series, status="open",
                       limit=200, with_nested_markets="true")
        r.raise_for_status()
        data = json_or_none(r)
    except Exception as e:
        # This is the most frequent call in the loop -- roughly every 15s
        # while idle, thousands of times a day. One timeout used to end the
        # session. Return "no event" and let the caller wait and retry.
        print(f"  event fetch failed ({type(e).__name__}: {e}) -- retrying")
        return None, None
    if data is None:
        return None, None
    events = data.get("events", [])
    ev, close_dt = select_hourly_event(events)
    if ev is None and verbose:
        for t, reason in describe_rejections(events):
            print(f"    skip {t}: {reason}")
    return ev, close_dt


def markets_for(event_ticker):
    r = get_public(f"{API}/markets", event_ticker=event_ticker, limit=1000)
    r.raise_for_status()
    return r.json().get("markets", [])


def single_market(ticker):
    """
    Fetch ONE market. Used by the position monitor.

    Previously manage_position() called markets_for(), pulling all ~188
    markets in the event and parsing them just to read one bid. That is a
    large response every poll and it is what made sub-second polling
    expensive. This endpoint returns a single object.
    """
    r = get_public(f"{API}/markets/{ticker}")
    r.raise_for_status()
    return r.json().get("market", {})


# ---------------------------------------------------------------- gates

class SpotHistory:
    """Rolling in-memory buffer of (timestamp, price), ~5 min of 1s samples."""

    def __init__(self, seconds=300):
        self.seconds = seconds
        self.buf = deque()

    def add(self, ts, price):
        self.buf.append((ts, price))
        cutoff = ts - self.seconds
        while self.buf and self.buf[0][0] < cutoff:
            self.buf.popleft()

    def range_5min(self):
        if len(self.buf) < 30:
            return None
        prices = [p for _, p in self.buf]
        return max(prices) - min(prices)

    def drift_30s(self, now_ts, now_price):
        cutoff = now_ts - 30
        past = [p for t, p in self.buf if t <= cutoff]
        if not past:
            return None
        return now_price - past[-1]  # positive = rising

    def move_over(self, now_ts, now_price, seconds):
        """Signed price change over the last `seconds`. None if not enough history."""
        cutoff = now_ts - seconds
        past = [p for t, p in self.buf if t <= cutoff]
        if not past:
            return None
        return now_price - past[-1]


def regime_key(dt_utc):
    """
    (day_type, hour) evaluated in the REGIME TIMEZONE (config.REGIME_TIMEZONE,
    default Eastern, matching how Kalshi labels its hourly events).

    Shared with tune_parameters.py and audit.py via regime_time.py so tuning
    and execution always bucket time identically.
    """
    return _rt.regime_key(dt_utc)


def resolve_regime_params(dt_utc):
    """
    Full parameter set for this moment.

    Precedence, highest first:
        1. _cfgv('REGIME_PARAMS')       -- any parameter, per regime
        2. _cfgv('REGIME_GAP_SIGMAS')   -- sigma only, per regime
        3. global CFG values

    Within each table, day keys resolve MOST SPECIFIC FIRST:
        "mon".."sun"  ->  "weekday"/"weekend"  ->  "all"

    So ("fri", (13, 17)) beats ("weekday", (13, 17)) on a Friday afternoon,
    and neither needs to know about the other. All hours are in the REGIME
    TIMEZONE (config.REGIME_TIMEZONE, default Eastern to match how Kalshi
    labels its events), so DST is handled automatically.

    Returns (label, params_dict). params_dict always has every key, so
    callers never have to handle a missing one.
    """
    params = {
        "sigmas": _cfgv('GAP_SIGMAS'),
        "tau_min": _cfgv('TAU_MIN_SECONDS'),
        "tau_max": _cfgv('TAU_MAX_SECONDS'),
        "min_depth": _cfgv('MIN_DEPTH_CONTRACTS'),
        "contracts": _cfgv('CONTRACTS_PER_TRADE'),
        "enabled": True,
    }
    label = f"{_rt.day_name(dt_utc)} (global)"

    if _cfgv('USE_REGIME_OVERRIDES'):
        lbl, sig = _rt.match_regime(_cfgv('REGIME_GAP_SIGMAS'), dt_utc)
        if sig is not None:
            params["sigmas"] = sig
            label = lbl

    lbl2, over = _rt.match_regime(_cfgv('REGIME_PARAMS'), dt_utc)
    if isinstance(over, dict):
        params.update(over)
        label = lbl2 + "*"          # * = from REGIME_PARAMS

    return label, params


def resolve_regime(dt_utc):
    """Back-compat: (label, sigmas)."""
    label, p = resolve_regime_params(dt_utc)
    return label, p["sigmas"]


def sigma_tau(rv5, tau):
    """
    Expected stdev of BTC movement over the remaining tau seconds.
    Parkinson: sigma over a window ~= range / 1.67. rv5 is a 5-minute (300s)
    range, so convert then scale by sqrt(time).
    """
    if rv5 is None or tau is None or tau <= 0:
        return None
    return (rv5 / 1.67) * math.sqrt(tau / 300.0)


def required_gap(rv5, gap_min_abs, sigmas, tau):
    """
    Required dollar gap = max(hard floor, sigmas x sigma_tau).

    Expressed in sigmas so the number is directly interpretable and scales
    correctly as the clock runs down: less time left -> less possible
    movement -> smaller required gap.
    """
    st = sigma_tau(rv5, tau)
    if st is None:
        return None
    return max(gap_min_abs, sigmas * st)


# Gates that reflect a momentary book condition rather than the trade getting
# worse. The retry loop waits these out instead of abandoning the event.
_TRANSIENT_GATES = {"g6_not_too_fast_now", "g7_enough_depth"}


def evaluate(now, tau, spot, strike, ask, is_marked, hist, args,
             ask_size=None, _reg_now=None):
    """Returns (ok: bool, reasons: dict) -- reasons always filled for logging."""
    r = {}
    r["tau"] = round(tau, 1)
    r["spot"] = round(spot, 2)
    r["strike"] = strike
    r["gap"] = round(spot - strike, 2)
    r["ask"] = ask
    r["is_marked"] = is_marked

    # L7: resolved ONCE per evaluate() call and reused. This previously ran
    # twice per strike -- ~376 timezone resolutions/second across 188 strikes.
    _reg_label0, _reg0 = _reg_now or resolve_regime_params(datetime.now(timezone.utc))
    # Regime tau window applies only when the user did not set one explicitly
    # on the CLI -- a regime block that never mentions tau must not clobber
    # an explicit --tau-min/--tau-max.
    tau_lo = (_reg0["tau_min"] if getattr(args, "tau_min_from_cli", False) is False
              else args.tau_min)
    tau_hi = (_reg0["tau_max"] if getattr(args, "tau_max_from_cli", False) is False
              else args.tau_max)
    r["tau_window"] = f"{tau_lo:.0f}-{tau_hi:.0f}"

    ok = True
    if not (tau_lo <= tau <= tau_hi):
        ok = False
    r["g1_tau_window"] = ok

    # Price: minimum only, no upper bound unless one is configured.
    # 100c is always rejected -- paying 100c for a 100c payout loses the fee.
    if ask is None:
        g2 = False
    elif ask < args.min_ask or ask >= 100:
        g2 = False
    elif args.max_ask is not None and ask > args.max_ask:
        g2 = False
    else:
        g2 = True
    r["g2_price_band"] = g2
    ok = ok and g2

    g3 = (not is_marked) or (tau <= args.marked_tau)
    r["g3_not_marked"] = g3
    ok = ok and g3

    rv5 = hist.range_5min()
    r["rv5_usd"] = round(rv5, 1) if rv5 is not None else None

    # Full regime resolution. Previously only "sigmas" was read and the rest
    # of REGIME_PARAMS -- tau_min, tau_max, min_depth, contracts, enabled --
    # was computed and silently discarded. Writing {"enabled": False} gave no
    # error and traded anyway.
    reg_label, reg = _reg_label0, _reg0
    if getattr(args, "gap_sigmas_override", None) is not None:
        regime_label, sigmas = "cli-override", args.gap_sigmas_override
    else:
        regime_label, sigmas = reg_label, reg["sigmas"]
    r["regime"] = regime_label
    r["gap_sigmas"] = sigmas

    # g0: is this regime enabled at all?
    g0 = bool(reg.get("enabled", True))
    r["g0_regime_enabled"] = g0
    ok = ok and g0
    st = sigma_tau(rv5, tau)
    r["sigma_tau_usd"] = round(st, 1) if st is not None else None
    req_gap = required_gap(rv5, args.gap_min_abs, sigmas, tau)
    r["actual_sigmas"] = round(r["gap"] / st, 2) if st and st > 0 else None
    r["required_gap"] = round(req_gap, 1) if req_gap is not None else None
    g4 = req_gap is not None and r["gap"] >= req_gap
    r["g4_gap_sufficient"] = g4
    ok = ok and g4

    drift = hist.drift_30s(now, spot)
    r["drift_30s_usd"] = round(drift, 2) if drift is not None else None
    if drift is not None and drift < 0:
        tts = r["gap"] / abs(drift) * 30  # seconds-equivalent at this rate
    else:
        tts = float("inf")
    r["tts_seconds"] = round(tts, 1) if tts != float("inf") else "inf"
    g5 = tts >= args.tts_mult * tau
    r["g5_velocity"] = g5
    ok = ok and g5

    recent_move = hist.move_over(now, spot, args.recent_move_window)
    r["recent_move_usd"] = round(recent_move, 2) if recent_move is not None else None
    g6 = recent_move is None or abs(recent_move) <= args.recent_move_max
    r["g6_not_too_fast_now"] = g6
    ok = ok and g6

    # G7: is there actually enough size to fill our order? Without this the
    # bot fires at a strike showing 99c with 2 contracts behind it and gets a
    # partial (or zero) fill.
    r["ask_size"] = ask_size
    # Depth must track the size ACTUALLY being traded, at every level.
    # P2 fixed this for --count; a regime {"contracts": 3} reintroduced it
    # one level up -- trade_count became 3 while eff_depth stayed at the
    # config default, demanding 14 resting contracts to trade 3.
    #
    # Precedence: explicit --min-depth > regime min_depth > regime contracts
    #             > args.min_depth (which already follows --count).
    _reg_depth = _reg0.get("min_depth")
    _reg_count = _reg0.get("contracts")
    if getattr(args, "min_depth_from_cli", False):
        eff_depth = args.min_depth                  # explicit CLI wins
    elif _reg_depth:
        eff_depth = _reg_depth                      # regime said so
    elif _reg_count and not getattr(args, "count_from_cli", False):
        eff_depth = _reg_count                      # follow the regime's size
    else:
        eff_depth = args.min_depth
    r["min_depth"] = eff_depth
    if ask_size is None:
        g7 = True                      # unknown depth -> don't block
    else:
        g7 = ask_size >= eff_depth
    r["g7_enough_depth"] = g7
    ok = ok and g7

    r["safety_ratio"] = round(r["gap"] / req_gap, 2) if req_gap else None
    return ok, r


# ---------------------------------------------------------------- orders

def place_ioc_buy(ticker, price_cents, count, key_id, pk):
    coid = str(uuid.uuid4())
    px = to_tick(price_cents)
    if px is None:
        raise ValueError(f"unusable buy price: {price_cents!r}")
    payload = {
        "ticker": ticker, "client_order_id": coid, "side": "bid",
        "count": str(count), "price": f"{px / 100:.2f}",
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": False, "cancel_order_on_pause": True, "reduce_only": False,
    }
    r = post(f"{API}/portfolio/events/orders", key_id, pk, payload)
    return r, payload, px


def place_ioc_sell(ticker, price_cents, count, key_id, pk):
    coid = str(uuid.uuid4())
    px = to_tick(price_cents)
    if px is None:
        raise ValueError(f"unusable sell price: {price_cents!r}")
    payload = {
        "ticker": ticker, "client_order_id": coid, "side": "ask",
        "count": str(count), "price": f"{px / 100:.2f}",
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": False, "cancel_order_on_pause": True, "reduce_only": True,
    }
    r = post(f"{API}/portfolio/events/orders", key_id, pk, payload)
    return r, payload, px


class CooldownState:
    """
    Sit out after losses.

    Rationale: losses on this strategy are NOT independent. They are caused
    by BTC regime shifts -- a liquidation cascade, a news jump -- which
    persist for minutes to hours. Trading straight through one is how a
    single bad regime becomes several losses instead of one.

    A win resets the consecutive counter. Repeated triggers escalate the
    cooldown length, capped at COOLDOWN_MAX_MINUTES.
    """

    def __init__(self, cfg):
        self.consec_losses = 0
        self.trigger_count = 0
        self.until_ts = 0.0
        self.reason = ""
        # M3: these MUST go through _cfgv so a config missing a key gets the
        # documented default. Inline fallbacks here disagreed with DEFAULTS
        # (60 vs 180 minutes, escalation 2.0 vs 1.0), so config_defaults would
        # report one effective value while the bot used another.
        self.n_needed = _cfgv('COOLDOWN_AFTER_CONSECUTIVE_LOSSES')
        self.base_min = _cfgv('COOLDOWN_MINUTES')
        self.escalation = _cfgv('COOLDOWN_ESCALATION')
        self.max_min = _cfgv('COOLDOWN_MAX_MINUTES')
        self.any_loss_min = _cfgv('COOLDOWN_AFTER_ANY_LOSS_MINUTES')

    def record_result(self, pnl):
        """Call after every closed trade. pnl in dollars."""
        if pnl is None:
            return
        # M-B: exactly 0.0 is break-even, not a loss. Treating it as a loss
        # meant a flat trade plus one real loss engaged a 3-hour cooldown.
        if pnl >= 0:
            if self.consec_losses:
                print("  [cooldown] win -- consecutive losses reset "
                     f"({self.consec_losses} -> 0)")
            self.consec_losses = 0
            self.trigger_count = 0
            return

        self.consec_losses += 1
        print(f"  [cooldown] loss #{self.consec_losses} in a row "
             f"(trigger at {self.n_needed})")

        if self.consec_losses >= self.n_needed:
            self.trigger_count += 1
            mins = min(self.base_min * (self.escalation ** (self.trigger_count - 1)),
                      self.max_min)
            self.until_ts = time.time() + mins * 60
            self.reason = (f"{self.consec_losses} consecutive losses "
                          f"(trigger #{self.trigger_count})")
            print(f"  [cooldown] ENGAGED for {mins:.0f} min -- {self.reason}")
        elif self.any_loss_min > 0:
            self.until_ts = max(self.until_ts,
                               time.time() + self.any_loss_min * 60)
            self.reason = "single loss pause"
            print(f"  [cooldown] short pause {self.any_loss_min:.0f} min")

    def active(self):
        return time.time() < self.until_ts

    def remaining_min(self):
        return max(0.0, (self.until_ts - time.time()) / 60.0)


def order_fee_cents(count, price_cents_val):
    """
    Kalshi taker fee for one order, rounded UP to the next cent.

        fee = ceil(0.07 * C * P * (1-P))   charged on the WHOLE order

    Rounding up per order is why 1 contract at 99c cannot profit: the 1c
    rounded fee equals the entire 1c gain.
    """
    p = price_cents_val / 100.0
    return math.ceil(0.07 * count * p * (1 - p) * 100)


def wait_for_settlement(ticker, key_id, pk, timeout=1800, poll=20):
    """
    Poll a closed market until Kalshi publishes its result.

    Holding to settlement is the NORMAL exit for a 99c buy, so this is the
    path that decides most outcomes. Previously manage_position() returned
    None here, which meant the P&L accumulator, the daily loss limit and the
    cooldown all silently ignored the majority of trades -- every brake in
    config.py was decorative.

    Returns 'yes' | 'no' | None (None = still unsettled after `timeout`).
    """
    deadline = time.time() + timeout
    while RUNNING and time.time() < deadline:
        try:
            m = single_market(ticker)
            res = (m or {}).get("result")
            if res in ("yes", "no"):
                return res
        except Exception as e:
            print(f"  settlement poll error: {e}")
        time.sleep(poll)
    return None


def confirm(prompt, auto, timeout=None, default_on_timeout=False):
    """
    Ask y/N, but never block forever.

    This matters because the bot runs on a remote box: if you detach the tmux
    session (or your SSH drops) while a position is open, a plain input() would
    wait for a keypress that never comes -- leaving the position unmanaged.

    Defaults are chosen so that a timeout always fails SAFE:
      * entries  -> default NO  (never open a position unattended)
      * exits    -> default YES (always protect capital unattended)
    """
    if auto:
        print(f"[auto] {prompt} -> YES")
        return True

    # No terminal attached (systemd, detached pipe, nohup): can't ask at all.
    if not sys.stdin.isatty():
        print(f"[no tty] {prompt} -> {'YES' if default_on_timeout else 'NO'}")
        return default_on_timeout

    if timeout is None:
        ans = input(f"{prompt} [y/N] ").strip().lower()
        return ans == "y"

    fallback = "YES" if default_on_timeout else "NO"
    print(f"{prompt} [y/N]  (auto-{fallback} in {timeout:.0f}s): ", end="", flush=True)
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if ready:
        ans = sys.stdin.readline().strip().lower()
        return ans == "y"
    print(f"\n  [no response -> {fallback}]")
    return default_on_timeout


# ---------------------------------------------------------------- logging

def log_row(path, row, header):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        if new:
            w.writeheader()
        w.writerow(row)


TRADE_HEADER = ["ts", "event_ticker", "ticker", "strike", "entry_reasons",
                "entry_price_c", "count", "entry_order_id",
                "exit_ts", "exit_reason", "exit_price_c", "exit_order_id",
                "pnl_dollars"]


# ---------------------------------------------------------------- main

def run(args):
    key_id = os.environ.get("KALSHI_KEY_ID")
    if not key_id and not args.dry_run:
        raise SystemExit("KALSHI_KEY_ID not set. export it first, or use --dry-run.")
    pk = load_private_key() if key_id else None

    _clock_ok, _skew, _clock_msg = check_clock(max_skew=args.max_clock_skew)
    print(f"clock: {_clock_msg}")
    if not _clock_ok:
        print("  !! REFUSING TO START. Override with --ignore-clock if you "
             "are certain.")
        if not args.ignore_clock:
            return
        print("  --ignore-clock given: continuing.")
    print()

    if not args.dry_run:
        if not reconcile_on_startup(args, key_id, pk):
            return

    hist = SpotHistory()
    realized_pnl_today = 0.0
    running_balance = args.starting_capital
    day = datetime.now(timezone.utc).date()
    seen_events = set()
    last_wait_msg = 0.0
    cooldown = CooldownState(CFG)
    last_cooldown_msg = 0.0
    wait_started = 0.0

    # ---- startup sanity checks ----
    # A daily limit smaller than one maximum loss means the first losing
    # trade halts the day. That may be intended, but it should be a choice,
    # not an accident of leaving a default in place.
    max_single_loss = (args.count * _cfgv('MIN_ASK_CENTS')
                       + order_fee_cents(args.count, _cfgv('MIN_ASK_CENTS'))) / 100.0
    print(f"max loss on ONE trade: ${max_single_loss:.2f} "
         f"({args.count} contracts)")
    if args.daily_loss_dollars < max_single_loss:
        print(f"  !! DAILY_LOSS_LIMIT_DOLLARS (${args.daily_loss_dollars:.2f}) is "
             f"LESS than one losing trade (${max_single_loss:.2f}).")
        print("  !! The first loss will halt trading for the rest of the day.")
        print(f"  !! Set it to a multiple of ${max_single_loss:.2f} "
             f"(e.g. ${max_single_loss*3:.2f} for 3 losses) if that is not "
             "what you want.")
    else:
        print(f"  daily limit ${args.daily_loss_dollars:.2f} "
             f"= {args.daily_loss_dollars/max_single_loss:.1f} losing trades")
    if args.count == 1 and _cfgv('MIN_ASK_CENTS') >= 99:
        print("  !! CONTRACTS_PER_TRADE = 1 at 99c CANNOT PROFIT: the fee "
             "rounds up to 1c, exactly the 1c gain. Use 3+ (14 is efficient).")
    print()

    # ---- stop loss, expressed in the dollars you actually risk ----
    _entry = _cfgv('MIN_ASK_CENTS')
    if args.stop_dollars is not None:
        print(f"stop loss: ${args.stop_dollars:.2f} of position value "
             "(absolute, overrides percentage)")
    else:
        _cost = args.count * _entry
        _trigger = None
        for _b in range(int(_entry), 0, -1):
            if (_cost - args.count * _b) / _cost >= args.stop_pct:
                _trigger = _b
                break
        if _trigger is not None:
            _loss_c = (_cost - args.count * _trigger
                       + order_fee_cents(args.count, _entry)
                       + order_fee_cents(args.count, _trigger))
            print(f"stop loss: {args.stop_pct*100:.1f}% of position value")
            print(f"  {args.count} contracts @ {_entry:.0f}c = "
                 f"${_cost/100:.2f} committed")
            print(f"  fires when bid <= {_trigger}c  ->  "
                 f"loss ~${_loss_c/100:.2f} (incl. both fees)")
    print("spot-based exit (safety ratio): "
         f"{'ON' if args.use_safety_ratio else 'OFF'}")
    print(f"cooldown: {_cfgv('COOLDOWN_AFTER_CONSECUTIVE_LOSSES')} consecutive "
         f"losses -> {_cfgv('COOLDOWN_MINUTES'):.0f} min pause")

    # ---- announce any regime that is switched off ----
    # L5: a typo'd key must report a bad config, not crash at startup.
    _disabled = []
    for _k, _v in (_cfgv('REGIME_PARAMS') or {}).items():
        try:
            _d, (_h0, _h1) = _k
            if isinstance(_v, dict) and _v.get("enabled") is False:
                _disabled.append((_d, (_h0, _h1)))
        except (TypeError, ValueError):
            print(f"  !! REGIME_PARAMS has a malformed key: {_k!r} -- expected "
                 '("weekday"|"weekend"|"mon".."sun"|"all", (start_hour, end_hour)). '
                 "This block will be IGNORED.")
    if _disabled:
        print("regimes DISABLED (no trading in these blocks):")
        for d, (h0, h1) in _disabled:
            print(f"    {d} {h0:02d}:00-{h1:02d}:00 {_rt.tz_label()}")
    print()

    print(f"mode: {'DRY-RUN (no orders)' if args.dry_run else ('AUTO' if args.auto else 'CONFIRM')}")
    print(f"starting_capital=${args.starting_capital:.2f}  count={args.count}  "
         f"tau=[{args.tau_min},{args.tau_max}]  price=[{args.min_ask},{args.max_ask}]c  "
         f"daily_loss_limit=${args.daily_loss_dollars:.2f}")
    if args.gap_sigmas_override is not None:
        print(f"gap: {args.gap_sigmas_override} sigma (CLI override, all regimes)")
    elif _cfgv('USE_REGIME_OVERRIDES'):
        print("gap: regime-aware sigmas (config.REGIME_GAP_SIGMAS)")
        for (dtk,(h0,h1)),sg in sorted(_cfgv('REGIME_GAP_SIGMAS').items()):
            print(f"    {dtk:<8} {h0:02d}-{h1:02d}z  {sg} sigma")
    else:
        print(f"gap: {_cfgv('GAP_SIGMAS')} sigma (flat)")
    print(f"price: >={args.min_ask}c"
         + (f" and <={args.max_ask}c" if args.max_ask is not None else " (no max)"))
    retry_on = _cfgv('RETRY_UNTIL_FILL') and not args.no_retry
    print(f"retry until fill: {'ON' if retry_on else 'OFF'}"
         + (f" (every {_cfgv('RETRY_INTERVAL_SECONDS')}s, stops "
            f"{_cfgv('RETRY_STOP_BEFORE_CLOSE')}s before close)" if retry_on else ""))
    # fee-rounding sanity check
    # L1: derive from the CONFIGURED entry price, not a hardcoded 99c.
    # Setting MIN_ASK_CENTS = 98 (which the config invites) previously left
    # this banner reporting the wrong win, loss and break-even while
    # max_single_loss above it used the real value.
    _ep = _cfgv('MIN_ASK_CENTS')
    _f = order_fee_cents(args.count, _ep)
    _net = args.count * (100 - _ep) - _f
    if _net <= 0:
        print(f"\n!! WARNING: at {args.count} contracts the 99c fee rounds to "
             f"{_f}c, leaving {_net}c profit on a WIN.")
        print("!! This size cannot make money. Raise CONTRACTS_PER_TRADE "
             "(14 is the efficient minimum).")
    else:
        _loss = args.count * _ep + _f
        print(f"economics @ {_ep:.0f}c: win +{_net}c, loss -{_loss}c "
             f"(fee {_f}c), breakeven "
             f"{_loss/(_net+_loss):.2%}")
    print()

    while RUNNING:
        today = datetime.now(timezone.utc).date()
        if today != day:
            day, realized_pnl_today = today, 0.0
            print(f"\n=== new UTC day. balance=${running_balance:.2f} ===\n")

        # cooldown gate -- checked before looking for an event at all
        if cooldown.active():
            if time.time() - last_cooldown_msg > 60:
                print(f"[{datetime.now(timezone.utc):%H:%M:%S}] COOLDOWN active: "
                     f"{cooldown.remaining_min():.0f} min left ({cooldown.reason})")
                last_cooldown_msg = time.time()
            time.sleep(15)
            continue

        if realized_pnl_today <= -abs(args.daily_loss_dollars):
            print(f"daily loss limit hit (${realized_pnl_today:.2f}). "
                 "Halted until restart or UTC midnight.")
            time.sleep(60)
            continue

        ev, close_dt = current_event(args.series)
        if not ev:
            if not wait_started:
                wait_started = time.time()
            # Normal right after an hour closes. Never fall back to a
            # long-dated contract -- just wait for the next hourly.
            if time.time() - last_wait_msg > 120:
                # L8: after ~5 minutes with nothing, show WHY each open event
                # was rejected. Otherwise a permanent stall is invisible
                # without editing code.
                if wait_started and time.time() - wait_started > 300:
                    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] still waiting "
                         f"({(time.time()-wait_started)/60:.0f} min) -- "
                         "open events in this series:")
                    try:
                        current_event(args.series, verbose=True)
                    except Exception as e:
                        print(f"    (could not list: {e})")
                else:
                    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] waiting for the "
                         "next hourly event (long-dated contracts ignored)")
                last_wait_msg = time.time()
            time.sleep(15)
            continue
        wait_started = 0.0
        et = ev["event_ticker"]
        if et in seen_events:
            time.sleep(10)
            continue

        traded_this_event = False

        while RUNNING and not traded_this_event:
            now = time.time()
            tau = (close_dt - datetime.now(timezone.utc)).total_seconds()
            if tau < -5:
                break
            # The outer loop must use the SAME tau window evaluate() will
            # apply, otherwise a regime that WIDENS the window (e.g.
            # tau_max: 600) never gets a chance -- the loop would sleep
            # through tau=500 on the global 240 and evaluate() would never
            # see the candidate. A regime could narrow but never widen.
            _rl0, _rp0 = resolve_regime_params(datetime.now(timezone.utc))
            eff_tau_max = (args.tau_max if args.tau_max_from_cli
                           else _rp0["tau_max"])
            if tau > eff_tau_max + 30:
                time.sleep(min(20, tau - eff_tau_max))
                continue

            try:
                spot = btc_spot()
            except Exception as e:
                print(f"spot fetch failed: {e}")
                time.sleep(1)
                continue
            hist.add(now, spot)

            try:
                mkts = markets_for(et)
            except Exception as e:
                print(f"markets fetch failed: {e}")
                time.sleep(1)
                continue

            below = [m for m in mkts if m.get("floor_strike") is not None
                    and m["floor_strike"] <= spot]
            marked_ticker = max(below, key=lambda m: m["floor_strike"])["ticker"] if below else None

            candidates = []
            for m in mkts:
                fs = m.get("floor_strike")
                ask = price_cents(m, "yes_ask")
                if fs is None or ask is None:
                    continue
                is_marked = (m["ticker"] == marked_ticker)
                ok, reasons = evaluate(now, tau, spot, fs, ask, is_marked,
                                       hist, args, ask_size=size_fp(m, "yes_ask"),
                                       _reg_now=(_rl0, _rp0))
                if ok:
                    candidates.append((m, reasons))

            if candidates:
                candidates.sort(key=lambda c: -c[1]["gap"])
                m, reasons = candidates[0]
                # regime may override size, unless --count was typed
                _rl, _rp = resolve_regime_params(datetime.now(timezone.utc))
                trade_count = (_rp["contracts"]
                               if (_rp.get("contracts") and
                                   not args.count_from_cli)
                               else args.count)
                if trade_count != args.count:
                    print(f"  regime {_rl} sets size {trade_count} "
                         f"(config default {args.count})")
                print(f"\n[{datetime.now(timezone.utc):%H:%M:%S}] CANDIDATE "
                     f"{m['ticker']}  {reasons}")

                if confirm(f"BUY {trade_count}x {m['ticker']} @ {price_cents(m, 'yes_ask')}c?",
                           args.auto, timeout=args.confirm_timeout,
                           default_on_timeout=False):
                    if args.dry_run:
                        print("  [dry-run] would place order here")
                        # price_cents(), not m["yes_ask"] -- the legacy
                        # integer field is null after Kalshi's fixed-point
                        # migration, which crashed dry-run with a TypeError
                        # on `count * None` the moment a candidate appeared.
                        entry_price, order_id = price_cents(m, "yes_ask"), "DRYRUN"
                        actual_count = trade_count
                    else:
                        # RETRY UNTIL FILL: an IOC can return zero fills when
                        # liquidity vanishes between our price read and the
                        # order landing. Rather than abandoning the hour, keep
                        # trying once per second while the gates still pass and
                        # there is still time.
                        retry_on = _cfgv('RETRY_UNTIL_FILL') and not args.no_retry

                        # F2: the effective retry deadline is the LATER of
                        # RETRY_STOP_BEFORE_CLOSE and the regime's tau_min.
                        # g1 is re-validated on every attempt, so retrying
                        # below tau_min could never actually place an order --
                        # RETRY_STOP_BEFORE_CLOSE alone was unreachable config.
                        # Computing it explicitly makes the real deadline
                        # visible instead of an emergent side effect.
                        _eff_tau_min = (args.tau_min if args.tau_min_from_cli
                                        else _rp0["tau_min"])
                        _retry_floor = max(_cfgv('RETRY_STOP_BEFORE_CLOSE'),
                                           _eff_tau_min)

                        got, attempts = None, 0
                        while RUNNING:
                            attempts += 1
                            tau_now = (close_dt - datetime.now(timezone.utc)
                                      ).total_seconds()
                            if tau_now <= _retry_floor:
                                print(f"  giving up: {tau_now:.0f}s left "
                                     f"(retry floor {_retry_floor:.0f}s = "
                                     f"max(stop {_cfgv('RETRY_STOP_BEFORE_CLOSE')}s, "
                                     f"tau_min {_eff_tau_min:.0f}s)), "
                                     f"{attempts-1} attempts")
                                break

                            # re-read the live book each attempt
                            try:
                                live = markets_for(et)
                                mk = next((x for x in live
                                          if x["ticker"] == m["ticker"]), None)
                            except Exception as e:
                                print(f"  refetch failed: {e}")
                                time.sleep(_cfgv('RETRY_INTERVAL_SECONDS'))
                                continue
                            if mk is None:
                                break
                            # MUST use price_cents(): Kalshi's fixed-point
                            # migration left the legacy integer `yes_ask`
                            # null on many markets. Reading it raw here (while
                            # evaluate() used price_cents) meant the retry loop
                            # saw None, printed "price left the band", and
                            # never placed an order.
                            ask_now = price_cents(mk, "yes_ask")
                            if ask_now is None or not math.isfinite(ask_now) \
                               or ask_now < args.min_ask \
                               or ask_now >= 100 \
                               or (args.max_ask is not None
                                   and ask_now > args.max_ask):
                                print(f"  price left the band (ask={ask_now}) "
                                     f"after {attempts-1} attempts")
                                break

                            # H6: re-run the FULL gate set, not just the price
                            # band. With TAU_MIN_SECONDS=90 and
                            # RETRY_STOP_BEFORE_CLOSE=10 this loop can fire for
                            # ~80 seconds. BTC can travel a long way toward the
                            # strike in 80s while the ask sits at 99c the whole
                            # time -- and the old check would still buy, because
                            # 99c is still 99c. This turns "keep buying at this
                            # price" into "keep buying while it is still the
                            # trade you approved".
                            try:
                                _spot_now = btc_spot()
                            except Exception:
                                time.sleep(_cfgv('RETRY_INTERVAL_SECONDS'))
                                continue
                            hist.add(time.time(), _spot_now)
                            _tau_now = (close_dt - datetime.now(timezone.utc)
                                        ).total_seconds()

                            # F3: recompute the marked strike from the CURRENT
                            # spot. It was frozen at the scan snapshot, so
                            # spot could move onto a different strike while
                            # is_marked stayed False -- g3, whose whole job is
                            # to stop you buying the strike spot sits on, was
                            # evaluating stale data. `live` is already in hand.
                            _below_now = [x for x in live
                                          if x.get("floor_strike") is not None
                                          and x["floor_strike"] <= _spot_now]
                            _marked_now = (max(_below_now,
                                               key=lambda x: x["floor_strike"]
                                               )["ticker"]
                                           if _below_now else None)

                            _still_ok, _why = evaluate(
                                time.time(), _tau_now, _spot_now,
                                m["floor_strike"], ask_now,
                                m["ticker"] == _marked_now, hist, args,
                                ask_size=size_fp(mk, "yes_ask"))
                            if not _still_ok:
                                _failed = [k for k, v in _why.items()
                                           if k.startswith("g") and v is False]
                                # F1: not all gate failures mean the same thing.
                                #
                                #   g6 (recent move) and g7 (depth) are
                                #   microstructure noise that clears in
                                #   seconds. g7 in particular is the very
                                #   reason RETRY_UNTIL_FILL exists -- an IOC
                                #   returns zero fills precisely BECAUSE
                                #   displayed depth vanished, so standing down
                                #   on it made retry-until-fill survive only
                                #   the narrow case where the book was full
                                #   and we lost a race.
                                #
                                #   g0/g1/g3/g4/g5 mean the TRADE has genuinely
                                #   deteriorated -- wrong regime, out of window,
                                #   spot moved onto the strike, gap collapsed,
                                #   or price is running at us. Those stand down.
                                if _failed and set(_failed) <= _TRANSIENT_GATES:
                                    if attempts == 1 or attempts % 10 == 0:
                                        print("  waiting on "
                                             f"{', '.join(_failed)} "
                                             f"(attempt {attempts}, "
                                             f"{_tau_now:.0f}s left)")
                                    time.sleep(_cfgv('RETRY_INTERVAL_SECONDS'))
                                    continue
                                print("  gates no longer pass "
                                     f"({', '.join(_failed) or 'unknown'}) after "
                                     f"{attempts-1} attempts -- standing down")
                                break

                            # F4: record the price ACTUALLY SENT. to_tick()
                            # snaps 99.75 -> 99, but the caller used to log
                            # 99.75, overstating cost basis on sub-cent ticks.
                            r, payload, sent_px = safe_order(
                                place_ioc_buy, m["ticker"], ask_now,
                                trade_count, key_id, pk)
                            if r is None:
                                # network fault -- the order may or may not
                                # have reached Kalshi. client_order_id makes a
                                # duplicate harmless, so retrying is safe.
                                time.sleep(_cfgv('RETRY_INTERVAL_SECONDS'))
                                continue
                            if r.status_code not in (200, 201):
                                print(f"  order error {r.status_code}: "
                                     f"{r.text[:200]}")
                                if not retry_on:
                                    break
                                time.sleep(_cfgv('RETRY_INTERVAL_SECONDS'))
                                continue

                            resp = json_or_none(r)
                            if resp is None:
                                print("  unparseable order response -- cannot "
                                     "confirm the fill. Verify on Kalshi "
                                     "before this event settles.")
                                break
                            order = resp.get("order", resp)
                            oid = order.get("order_id", resp.get("order_id", "?"))
                            filled = order.get("fill_count",
                                               order.get("filled_count",
                                                         resp.get("fill_count")))
                            try:
                                filled = (int(float(filled))
                                         if filled is not None else None)
                            except (TypeError, ValueError):
                                filled = None

                            if filled and filled > 0:
                                got = (filled, sent_px, oid)
                                if filled < trade_count:
                                    if not _cfgv('ACCEPT_PARTIAL_FILLS'):
                                        print("  NOTE: ACCEPT_PARTIAL_FILLS is "
                                             "False, but an IOC that filled "
                                             "cannot be undone. Managing the "
                                             f"{filled} contracts now held.")
                                    print(f"  partial fill {filled}/"
                                         f"{trade_count} @ {sent_px}c "
                                         f"(attempt {attempts})")
                                else:
                                    print(f"  FILLED {filled} @ {sent_px}c "
                                         f"(attempt {attempts})")
                                break

                            if not retry_on:
                                print("  no fill, retry disabled -- moving on")
                                break
                            if attempts == 1 or attempts % 10 == 0:
                                print(f"  no liquidity at {sent_px}c, retrying "
                                     f"(attempt {attempts}, {tau_now:.0f}s left)")
                            time.sleep(_cfgv('RETRY_INTERVAL_SECONDS'))

                        if got is None:
                            print("  never filled this event")
                            traded_this_event = True
                            seen_events.add(et)
                            break
                        actual_count, entry_price, order_id = got

                    traded_this_event = True
                    seen_events.add(et)
                    entry_row = {
                        "ts": now, "event_ticker": et, "ticker": m["ticker"],
                        "strike": m["floor_strike"], "entry_reasons": reasons,
                        "entry_price_c": entry_price, "count": actual_count,
                        "entry_order_id": order_id,
                    }

                    # M1: write an OPEN row NOW. Between entry and close --
                    # up to an hour plus 30 minutes of settlement polling --
                    # the only record of a live position was process memory.
                    # A Ctrl-C, crash or instance loss left contracts held
                    # with nothing on disk saying so. Reconcile on
                    # entry_order_id and ignore an OPEN row when a later row
                    # for the same order exists.
                    log_row(args.log, dict(entry_row, **{
                        "exit_ts": None, "exit_reason": "OPEN",
                        "exit_price_c": None, "exit_order_id": None,
                        "pnl_dollars": None}), TRADE_HEADER)

                    pnl = manage_position(m["ticker"], m["floor_strike"], et,
                                          close_dt, entry_price, actual_count,
                                          hist, args, key_id, pk, entry_row)
                    cooldown.record_result(pnl)
                    if pnl is not None:
                        realized_pnl_today += pnl
                        running_balance += pnl
                        print(f"  today's realized pnl: ${realized_pnl_today:+.2f}   "
                             f"balance: ${running_balance:.2f}")
                else:
                    # H3: without this the same candidate is re-evaluated one
                    # second later and you are asked again -- and since a
                    # confirm timeout is itself a NO, an unattended terminal
                    # churns prompts for the rest of the window.
                    print(f"  skipped -- not re-asking for {et} this event")
                    traded_this_event = True
                    seen_events.add(et)
                    break

            time.sleep(args.poll)

        seen_events.add(et)
        time.sleep(2)


def manage_position(ticker, strike, event_ticker, close_dt, entry_price,
                    count, hist, args, key_id, pk, entry_row):
    """Watch the position until exit (z-collapse / stop / settlement)."""
    print(f"  monitoring position: {count}x {ticker} @ {entry_price}c")

    # C1: P&L realised on PARTIAL exits must be carried, not printed and
    # dropped. Previously a partial loss was discarded and only the surviving
    # remainder was returned -- so a net-losing trade could be reported as a
    # win, resetting the consecutive-loss counter and defeating the very
    # cooldown that exists for the fast moves which cause messy fills.
    realized_c = 0.0
    partial_n = 0
    exit_attempts = 0            # H2: backoff on repeated zero-fill exits

    # M2: Kalshi bills ONE rounded fee for the whole entry order. Charging
    # order_fee_cents(sold, entry) on every slice pays that rounding several
    # times over. Compute it once and amortise by the fraction exited.
    original_count = count
    entry_fee_total = order_fee_cents(original_count, entry_price)

    def _entry_fee_share(n):
        if not original_count:
            return 0
        return entry_fee_total * (n / original_count)

    while RUNNING:
        tau = (close_dt - datetime.now(timezone.utc)).total_seconds()
        if tau <= 0:
            print("  event closed -- waiting for settlement ...")
            entry_fee = _entry_fee_share(count)
            cost_c = count * entry_price + entry_fee
            res = wait_for_settlement(ticker, key_id, pk)

            if res is None:
                # Unknown after the timeout. Do NOT invent a number -- log it
                # and return None so nothing false enters the accumulators.
                print("  !! settlement not published within timeout. "
                     "Reconcile manually, then check data/live_trades.csv")
                entry_row.update({"count": count, "exit_ts": time.time(),
                                 "exit_reason": "settlement_unknown",
                                 "exit_price_c": None, "exit_order_id": None,
                                 "pnl_dollars": None})
                log_row(args.log, entry_row, TRADE_HEADER)
                # Carry anything already booked on partials, even though the
                # remainder is unknown.
                return (realized_c / 100.0) if realized_c else None

            won = (res == "yes")
            pnl_c = (count * 100 - cost_c) if won else -cost_c
            pnl = pnl_c / 100.0
            print(f"  SETTLED {res.upper()}  "
                 f"{count} x {entry_price:.0f}c + {entry_fee:.1f}c fee = {cost_c:.0f}c"
                 f"  ->  pnl ${pnl:+.2f}")
            total = pnl + realized_c / 100.0
            entry_row.update({"count": count, "exit_ts": time.time(),
                             "exit_reason": f"settled_{res}",
                             "exit_price_c": 100 if won else 0,
                             "exit_order_id": None,
                             "pnl_dollars": round(pnl, 4)})
            log_row(args.log, entry_row, TRADE_HEADER)
            if realized_c:
                print(f"  trade total incl. {partial_n} partial exit(s): "
                     f"${total:+.2f}")
            return total

        try:
            spot = btc_spot()
        except Exception:
            time.sleep(1)
            continue
        hist.add(time.time(), spot)
        gap = spot - strike
        rv5 = hist.range_5min()
        if args.gap_sigmas_override is not None:
            _sig = args.gap_sigmas_override
        else:
            _sig = resolve_regime(datetime.now(timezone.utc))[1]
        req_gap = required_gap(rv5, args.gap_min_abs, _sig, max(tau, 1))
        ratio = (gap / req_gap) if req_gap else None

        try:
            m = single_market(ticker)
            bid = price_cents(m, "yes_bid") if m else None
        except Exception:
            bid = None

        # ---- STOP LOSS on POSITION VALUE ----
        # cost_basis  = contracts x entry price
        # current_val = contracts x current bid
        # Fire the moment the position has lost args.stop_pct of its
        # committed capital. Computed on value rather than on price alone so
        # it means the same thing at any contract count.
        cost_basis = count * entry_price
        current_val = (count * bid) if bid is not None else None
        loss_cents = ((cost_basis - current_val)
                      if current_val is not None else None)
        drawdown_pct = (loss_cents / cost_basis
                        if (loss_cents is not None and cost_basis > 0) else None)

        should_exit = False
        reason = None

        # Brake 1: position-value stop. A dollar limit takes precedence over
        # the percentage when configured.
        # B2: the dollar stop applies to the TRADE, not to whatever survives
        # of it. realized_c is negative on a losing partial, so subtracting it
        # adds that loss back in. Measuring the remainder alone let a partial
        # exit shrink the position below the threshold and DISARM the stop --
        # the rest then rode to settlement, which is precisely what the stop
        # existed to prevent. ($1.00 stop -> $8.58 realised loss, measured.)
        #
        # Percentage mode is unaffected: (entry-bid)/entry is scale-invariant,
        # so count cancels and it keeps firing at any remaining size.
        total_loss_c = (loss_cents - realized_c) if loss_cents is not None else None

        if total_loss_c is not None and args.stop_dollars is not None:
            if total_loss_c >= args.stop_dollars * 100.0:
                should_exit, reason = True, "stop_loss_dollars"
        elif drawdown_pct is not None and drawdown_pct >= args.stop_pct:
            should_exit, reason = True, "stop_loss_position_value"

        # Optional spot-based exit. OFF by default -- see USE_SAFETY_RATIO_EXIT
        # in config.py for what turning it off trades away.
        if (not should_exit and args.use_safety_ratio
                and ratio is not None and ratio < args.exit_ratio):
            should_exit, reason = True, "safety_ratio_collapsed"

        # NOTE: the old `price_stop_backstop` branch was removed. It tested
        # bid <= entry*(1-p), which is algebraically IDENTICAL to the
        # drawdown test above -- `count` cancels out of the fraction, so
        # (count*e - count*b)/(count*e) = (e-b)/e. It was two names for one
        # condition and could never fire independently.

        if should_exit:
            dd = f"{drawdown_pct*100:.1f}%" if drawdown_pct is not None else "?"
            if args.stop_dollars is not None and total_loss_c is not None:
                lost = f"${total_loss_c/100:.2f} (trade total)"
            else:
                lost = f"${loss_cents/100:.2f}" if loss_cents is not None else "$?"
            print(f"  EXIT signal: {reason}")
            print(f"    position: {count} x {entry_price:.0f}c = "
                 f"{cost_basis:.0f}c  ->  bid {bid}c = "
                 f"{current_val if current_val is not None else 0:.0f}c "
                 f"(down {lost} = {dd}, limit "
                 f"{('$%.2f' % args.stop_dollars) if args.stop_dollars is not None else ('%.0f%%' % (args.stop_pct*100))})")
            print(f"    gap={gap:.1f} ratio={ratio}")
            # Exits default to YES on timeout: if nobody is watching, protecting
            # the position is the safe failure mode.
            if confirm(f"SELL {count}x {ticker} now?", args.auto,
                       timeout=args.confirm_timeout, default_on_timeout=True):
                if bid is None or bid <= 0:
                    print("  no bid to sell into -- cannot exit. Still holding; "
                         "will retry next poll.")
                    time.sleep(args.position_poll)
                    continue

                # M-A: snap here, not inside the live branch. Dry-run never
                # computed sent_px, so it booked the raw float and reported
                # exits ~0.2% better than live would on a sub-cent bid --
                # in the mode you use to validate the system.
                exit_price = to_tick(bid)
                if exit_price is None:
                    print("  unusable bid, cannot exit -- still holding")
                    time.sleep(args.position_poll)
                    continue
                if args.dry_run:
                    print("  [dry-run] would sell here")
                    order_id, sold = "DRYRUN", count
                else:
                    r, _, sent_px = safe_order(place_ioc_sell, ticker,
                                               exit_price, count, key_id, pk)
                    if r is None:
                        # STILL HOLDING. Back off and try again -- never fall
                        # through to booking a P&L we did not realise.
                        exit_attempts += 1
                        wait = min(30.0, args.position_poll * (2 ** exit_attempts))
                        print("  exit order did not send -- STILL HOLDING, "
                             f"retrying in {wait:.1f}s")
                        time.sleep(wait)
                        continue
                    # F4: book the snapped price we actually sent.
                    exit_price = sent_px
                    print(f"  sell response {r.status_code}: {r.text[:300]}")
                    if r.status_code not in (200, 201):
                        print("  SELL REJECTED -- still holding, retrying.")
                        time.sleep(args.position_poll)
                        continue
                    resp = json_or_none(r)
                    if resp is None:
                        # 200 with a gateway error page slips past the status
                        # check above; treat it as UNKNOWN, never as filled.
                        exit_attempts += 1
                        wait = min(30.0, args.position_poll * (2 ** exit_attempts))
                        print("  unparseable sell response -- treating as "
                             f"UNKNOWN. STILL HOLDING, retrying in {wait:.1f}s")
                        time.sleep(wait)
                        continue
                    order = resp.get("order", resp)
                    order_id = order.get("order_id", resp.get("order_id", "?"))
                    # An IOC into a book with no bid returns 200 with ZERO
                    # fills. Booking a P&L here would record a fake exit,
                    # stop monitoring, and leave the position open with the
                    # bot believing it was flat.
                    raw = order.get("fill_count", order.get("filled_count",
                                                           resp.get("fill_count")))
                    try:
                        sold = int(float(raw)) if raw is not None else None
                    except (TypeError, ValueError):
                        sold = None

                    # C2: a missing fill_count means UNKNOWN, not "all sold".
                    # The buy path already treats None as no-fill; the sell
                    # path assuming a full exit was the unsafe reading of the
                    # same ambiguity -- it books P&L and stops monitoring
                    # while the position is still open.
                    if sold is None:
                        # H5: back off here too. A zero fill is transient
                        # (liquidity returns); a response-shape mismatch is
                        # permanent for the session, so EVERY attempt would
                        # land here and hammer the order endpoint -- the exact
                        # behaviour H2 was written to stop.
                        exit_attempts += 1
                        wait = min(30.0, args.position_poll * (2 ** exit_attempts))
                        print("  sell response has NO fill_count -- treating as "
                             "UNKNOWN, not as filled. STILL HOLDING; verify the "
                             f"position on Kalshi. Retrying in {wait:.1f}s.")
                        time.sleep(wait)
                        continue

                    if sold == 0:
                        # H2: exponential backoff. A breach with no bid is
                        # exactly when this fires, and hammering the order
                        # endpoint twice a second for minutes is the worst
                        # possible moment to get rate-limited. post() has no
                        # 429 handling of its own.
                        exit_attempts += 1
                        wait = min(30.0, args.position_poll * (2 ** exit_attempts))
                        print(f"  IOC FILLED 0 of {count} -- no liquidity at "
                             f"{exit_price:.0f}c (attempt {exit_attempts}). "
                             f"Holding, retrying in {wait:.1f}s.")
                        time.sleep(wait)
                        continue

                    exit_attempts = 0          # any fill resets the backoff

                    if sold < count:
                        exit_fee = order_fee_cents(sold, exit_price)
                        part_c = (sold * (exit_price - entry_price)
                                  - _entry_fee_share(sold) - exit_fee)
                        realized_c += part_c
                        partial_n += 1
                        count -= sold
                        log_row(args.log, dict(entry_row, **{
                            "count": sold, "exit_ts": time.time(),
                            "exit_reason": f"{reason}_partial{partial_n}",
                            "exit_price_c": exit_price,
                            "exit_order_id": order_id,
                            "pnl_dollars": round(part_c / 100.0, 4)}),
                            TRADE_HEADER)
                        print(f"  PARTIAL exit: sold {sold} @ {exit_price:.0f}c "
                             f"-> ${part_c/100:+.2f} booked. Still holding "
                             f"{count}.")
                        time.sleep(args.position_poll)
                        continue

                # full exit -- P&L NET OF BOTH FEES
                entry_fee = _entry_fee_share(sold)
                exit_fee = 0 if args.dry_run else order_fee_cents(sold, exit_price)
                pnl_c = sold * (exit_price - entry_price) - entry_fee - exit_fee
                total = (pnl_c + realized_c) / 100.0
                entry_row.update({"count": sold, "exit_ts": time.time(),
                                 "exit_reason": reason,
                                 "exit_price_c": exit_price, "exit_order_id": order_id,
                                 "pnl_dollars": round(pnl_c / 100.0, 4)})
                log_row(args.log, entry_row, TRADE_HEADER)
                print(f"  CLOSED {sold} @ {exit_price:.0f}c  "
                     f"(fees {entry_fee:.1f}+{exit_fee}c)  "
                     f"pnl=${pnl_c/100:+.2f}")
                if realized_c:
                    print(f"  trade total incl. {partial_n} partial exit(s): "
                         f"${total:+.2f}")
                return total
            else:
                # Declined. Don't re-ask every single poll -- wait before the
                # next prompt, but keep monitoring in case it gets worse.
                print("  exit declined -- will re-check in "
                     f"{args.decline_cooldown:.0f}s")
                time.sleep(args.decline_cooldown)
                continue
        time.sleep(args.position_poll)

    # Loop exited without a clean close (Ctrl-C, shutdown). Return whatever
    # was actually booked on partials rather than silently dropping it.
    return (realized_c / 100.0) if realized_c else None


def open_positions(log_path):
    """
    Any entry_order_id with an OPEN row and no later closing row.

    The OPEN row (M1) was written but never read back. On restart the bot had
    no idea a position was live: seen_events was empty so the same event was
    eligible again, realized_pnl_today reset, and any cooldown was cleared.
    """
    if not os.path.exists(log_path):
        return []
    try:
        with open(log_path, newline="") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return []
    latest = {}
    for row in rows:
        oid = row.get("entry_order_id") or ""
        if not oid:
            continue
        latest.setdefault(oid, []).append(row)
    out = []
    for oid, group in latest.items():
        reasons = [(g.get("exit_reason") or "") for g in group]
        if any(r and r != "OPEN" for r in reasons):
            continue          # a closing row exists
        if "OPEN" in reasons:
            out.append(group[-1])
    return out


def check_clock(max_skew=2.0):
    """
    Compare local time against Kalshi's Date header.

    Two things depend on the clock being right and nothing checked either:

      1. AUTH. auth_headers() signs int(time.time()*1000). Kalshi rejects a
         timestamp too far from server time, and it surfaces as a generic
         401 in the retry loop -- which reads like a bad API key, not a bad
         clock. That is hours of debugging the wrong thing.

      2. THE ENTRY WINDOW. tau = close_dt - now. The window is 150s wide, so
         30s of drift distorts it by 20% and shifts every gate that scales
         with tau, including sigma_tau.

    Returns (ok, skew_seconds, message).
    """
    try:
        r = S.get(BASE + f"{API}/exchange/status", timeout=10)
        date_hdr = r.headers.get("Date")
        if not date_hdr:
            return True, 0.0, "no Date header returned; clock unverified"
        server = parsedate_to_datetime(date_hdr)
        if server.tzinfo is None:
            server = server.replace(tzinfo=timezone.utc)
        skew = (datetime.now(timezone.utc) - server).total_seconds()
        if abs(skew) > max_skew:
            return False, skew, (
                f"local clock is {skew:+.1f}s from Kalshi. Orders may be "
                "rejected and the tau window is distorted. Fix with: "
                "sudo systemctl restart chrony && chronyc tracking")
        return True, skew, f"clock OK ({skew:+.2f}s from Kalshi)"
    except Exception as e:
        return True, 0.0, f"clock check skipped ({type(e).__name__}: {e})"


def kalshi_positions(key_id, pk):
    """
    Actual open positions from Kalshi. The bot never asked the exchange what
    it owned, on any path -- the CSV was the only source of truth, and the
    CSV is written by the process that just crashed.

    Returns a list of {ticker, position} with non-zero size, or None if the
    query fails (which must NOT be read as "flat").
    """
    try:
        r = get(f"{API}/portfolio/positions", key_id, pk, limit=200)
        if r.status_code != 200:
            return None
        data = json_or_none(r)
        if data is None:
            return None
        out = []
        for p in data.get("market_positions", []) or []:
            try:
                qty = int(float(p.get("position", 0) or 0))
            except (TypeError, ValueError):
                qty = 0
            if qty:
                out.append({"ticker": p.get("ticker"), "position": qty})
        return out
    except Exception as e:
        print(f"  position query failed ({type(e).__name__}: {e})")
        return None


def reconcile_on_startup(args, key_id, pk):
    """
    Refuse to trade while something is unaccounted for.

    Critical 1 and Critical 2 compound: a crash during an exit leaves a
    position open, and a restart would clear seen_events, realized_pnl_today
    and the cooldown -- so the bot could re-enter the SAME event while the
    first position was still live, with the daily limit and cooldown wiped.

    Returns True to continue, False to stop.
    """
    logged = open_positions(args.log)
    live_pos = kalshi_positions(key_id, pk) if (key_id and pk) else None

    if live_pos is None and not logged:
        print("startup reconcile: could not query Kalshi positions; "
             "log shows nothing open. Continuing.")
        return True

    if not logged and live_pos == []:
        print("startup reconcile: flat on both the log and Kalshi. OK.")
        return True

    print("\n" + "=" * 66)
    print("STARTUP RECONCILE -- UNRESOLVED POSITION")
    print("=" * 66)
    if logged:
        print(f"log has {len(logged)} OPEN row(s) with no closing row:")
        for r in logged:
            print(f"  {r.get('ticker')}  {r.get('count')} @ "
                 f"{r.get('entry_price_c')}c  order {r.get('entry_order_id')}")
    if live_pos:
        print(f"Kalshi reports {len(live_pos)} open position(s):")
        for p in live_pos:
            print(f"  {p['ticker']}  {p['position']} contracts")
    elif live_pos == []:
        print("Kalshi reports NO open positions -- the log row is probably "
             "stale (closed manually, or settled).")
    else:
        print("Could not reach Kalshi to confirm.")

    print("\nThe bot will NOT trade until this is resolved. Either close or")
    print("let the position settle on Kalshi, then re-run with --ack-open to")
    print("acknowledge and continue.")
    print("=" * 66)

    if args.ack_open:
        print("--ack-open given: continuing anyway.\n")
        return True
    return False


def _report_open_position(args):
    """Print anything the log says is still open. Called on any exit path."""
    try:
        rows = open_positions(args.log)
    except Exception:
        return
    if not rows:
        print("\nno open position recorded in the log.")
        return
    print("\n" + "!" * 66)
    print("OPEN POSITION(S) RECORDED -- VERIFY ON KALSHI IMMEDIATELY")
    print("!" * 66)
    for r in rows:
        print(f"  ticker : {r.get('ticker')}")
        print(f"  count  : {r.get('count')}")
        print(f"  entry  : {r.get('entry_price_c')}c")
        print(f"  order  : {r.get('entry_order_id')}")
        print(f"  event  : {r.get('event_ticker')}")
    print("!" * 66)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", default="KXBTCD")
    ap.add_argument("--starting-capital", type=float, default=_cfgv('STARTING_CAPITAL'),
                    help="fixed dollars. Not a percentage of anything.")
    ap.add_argument("--count", type=int, default=_cfgv('CONTRACTS_PER_TRADE'),
                    help="contracts per trade. Leave at 1 today.")
    ap.add_argument("--tau-min", type=float, default=_cfgv('TAU_MIN_SECONDS'))
    ap.add_argument("--tau-max", type=float, default=_cfgv('TAU_MAX_SECONDS'),
                    help="seconds before close to start looking")
    ap.add_argument("--min-ask", type=int, default=_cfgv('MIN_ASK_CENTS'))
    ap.add_argument("--max-ask", type=int, default=_cfgv('MAX_ASK_CENTS'),
                    help="upper price bound; omit/None for no maximum")
    ap.add_argument("--marked-tau", type=float, default=_cfgv('MARKED_STRIKE_TAU_SECONDS'))
    ap.add_argument("--gap-min-abs", type=float, default=_cfgv('GAP_MIN_ABS_USD'),
                    help="hard dollar floor on the gap, regardless of vol")
    ap.add_argument("--gap-sigmas", dest="gap_sigmas_override", type=float,
                    default=None,
                    help="override required sigmas for ALL regimes (testing "
                        "only). Omit to use the regime table in config.py.")
    ap.add_argument("--no-retry", action="store_true",
                    help="disable retry-until-fill; try once and move on")
    ap.add_argument("--tts-mult", type=float, default=_cfgv('TTS_SAFETY_MULTIPLIER'))
    ap.add_argument("--recent-move-window", type=float,
                    default=_cfgv('RECENT_MOVE_WINDOW_SECONDS'),
                    help="window in seconds for the raw speed check")
    ap.add_argument("--recent-move-max", type=float, default=_cfgv('RECENT_MOVE_MAX_USD'),
                    help="max $ movement allowed within that window to still enter")
    ap.add_argument("--exit-ratio", type=float, default=_cfgv('EXIT_SAFETY_RATIO'),
                    help="exit if gap/required_gap falls below this")
    ap.add_argument("--stop-pct", type=float, default=_cfgv('STOP_LOSS_PCT'),
                    help="exit when the position loses this fraction of its value")
    ap.add_argument("--stop-dollars", type=float,
                    default=_cfgv('STOP_LOSS_DOLLARS'),
                    help="absolute dollar stop; overrides --stop-pct when set")
    # NOTE ON ORDER: argparse applies the default of the FIRST action
    # registered for a dest. store_false carries an implicit default of True,
    # so listing --no-safety-ratio first silently forced the exit ON
    # regardless of config. The enabling flag must come first, and the
    # disabling flag must SUPPRESS its default so it only acts when typed.
    ap.add_argument("--use-safety-ratio", action="store_true",
                    dest="use_safety_ratio",
                    default=_cfgv('USE_SAFETY_RATIO_EXIT'),
                    help="also exit on BTC spot moving back toward the strike")
    ap.add_argument("--no-safety-ratio", action="store_false",
                    dest="use_safety_ratio", default=argparse.SUPPRESS,
                    help="disable the spot-based exit even if config enables it")
    ap.add_argument("--daily-loss-dollars", type=float,
                    default=_cfgv('DAILY_LOSS_LIMIT_DOLLARS'),
                    help="halt for the day once realized losses reach this many dollars")
    ap.add_argument("--min-depth", type=float, default=None,
                    help="minimum contracts resting at the ask before we will "
                        "fire. Defaults to the ACTUAL order size in use "
                        "(--count or regime override), not the config value -- "
                        "otherwise --count 3 would still demand "
                        "CONTRACTS_PER_TRADE resting contracts.")
    ap.add_argument("--poll", type=float, default=_cfgv('POLL_SECONDS'),
                    help="entry-scan interval")
    ap.add_argument("--position-poll", type=float,
                    default=_cfgv('POSITION_POLL_SECONDS'),
                    help="stop-loss check interval while holding a position")
    ap.add_argument("--confirm-timeout", type=float,
                    default=_cfgv('CONFIRM_TIMEOUT_SECONDS'),
                    help="seconds to wait at a y/N prompt before falling back "
                        "(entries default NO, exits default YES)")
    ap.add_argument("--decline-cooldown", type=float,
                    default=_cfgv('DECLINE_COOLDOWN_SECONDS'),
                    help="seconds to wait before re-asking after a declined exit")
    ap.add_argument("--log", default="data/live_trades.csv")
    ap.add_argument("--max-clock-skew", type=float, default=2.0,
                    help="max seconds of drift from Kalshi before refusing "
                        "to start")
    ap.add_argument("--ignore-clock", action="store_true",
                    help="start even if the clock check fails")
    ap.add_argument("--ack-open", action="store_true",
                    help="acknowledge an unresolved open position and trade "
                        "anyway. Only after verifying on Kalshi.")
    ap.add_argument("--dry-run", action="store_true",
                    help="never sends a real order, just prints what it would do")
    ap.add_argument("--auto", action="store_true",
                    help="skip the y/N confirmation. Don't use this until "
                        "you've watched confirm mode for a while.")
    args = ap.parse_args()

    # Which options did the user actually type? A regime block that never
    # mentions tau or depth must not override an explicit CLI flag.
    _typed = set()
    for tok in sys.argv[1:]:
        if tok.startswith("--"):
            _typed.add(tok.split("=")[0])
    args.tau_min_from_cli = "--tau-min" in _typed
    args.tau_max_from_cli = "--tau-max" in _typed
    args.min_depth_from_cli = "--min-depth" in _typed
    args.count_from_cli = "--count" in _typed

    # P2: the depth gate must track the size actually being traded. Config's
    # MIN_DEPTH_CONTRACTS is an explicit override; otherwise follow --count.
    if args.min_depth is None:
        cfg_depth = _cfgv('MIN_DEPTH_CONTRACTS')
        args.min_depth = cfg_depth if cfg_depth else args.count

    os.makedirs(os.path.dirname(args.log) or ".", exist_ok=True)

    # Last-resort guard. Everything below the order paths is now wrapped, but
    # if anything ever does fall through, the one thing that must not happen
    # is dying silently while holding contracts. Print the position first.
    try:
        run(args)
    except KeyboardInterrupt:
        print("\ninterrupted by user")
        _report_open_position(args)
    except Exception as e:
        import traceback
        print("\n" + "=" * 66)
        print(f"UNCAUGHT {type(e).__name__}: {e}")
        print("=" * 66)
        traceback.print_exc()
        _report_open_position(args)
        raise


if __name__ == "__main__":
    main()