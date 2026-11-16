#!/usr/bin/env python3
"""
event_select.py -- one shared, tested implementation of "which event should
we be looking at right now?"

WHY THIS EXISTS
---------------
The KXBTCD series contains BOTH hourly contracts and longer-dated ones
(daily / multi-hour). A real observation from kalshi_discover:

    KXBTCD-26AUG1012   closes in    25 min   188 markets   <- hourly
    KXBTCD-26AUG1017   closes in   325 min    80 markets   <- NOT hourly
    KXBTCD-26AUG1417   closes in  6085 min    50 markets   <- NOT hourly

The old logic in each script picked "the nearest open event". That is right
most of the time, but wrong in exactly the moment that matters: when the
current hourly event closes and Kalshi has not yet listed the next one, the
nearest open event becomes a long-dated contract. The scripts would then
display, evaluate and potentially TRADE a daily contract using parameters
(tau, sigma) calibrated entirely for a one-hour window.

WHY time_to_close <= 3600 IS NOT ENOUGH ON ITS OWN
--------------------------------------------------
A daily contract in its final hour also satisfies time_to_close <= 3600.
Filtering on remaining time alone would still let a daily through, just
later in its life. The reliable discriminator is contract DURATION:

    hourly:  open 22:00Z, close 23:00Z  -> duration 3600s
    daily:   open 09:00Z, close 21:00Z  -> duration 43200s

So an event qualifies only if BOTH hold:
    1. duration  = close - open   <= MAX_DURATION (3600s + tolerance)
    2. remaining = close - now    is > 0 and <= MAX_REMAINING (3600s)

Selection among qualifying events is deterministic: soonest close, and on an
exact tie, the lexicographically smaller event_ticker.
"""

from datetime import datetime, timezone

# An hourly contract is exactly 3600s long. Allow a small tolerance for
# events that open a little early or carry odd rounding.
MAX_DURATION_SECONDS = 3600 + 300      # 1h + 5 min slack
MAX_REMAINING_SECONDS = 3600           # never target something >1h out
MIN_REMAINING_SECONDS = 0              # strictly in the future


def _parse(ts):
    """Parse a Kalshi ISO-8601 timestamp. Returns None if unusable."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def event_window(ev):
    """
    (open_dt, close_dt) for an event, from its nested markets.

    Uses the LATEST open and the EARLIEST close across the event's markets,
    which is the conservative reading: it yields the shortest duration the
    event could have, so a long-dated event can never be mistaken for hourly
    because one stray market opened late.
    """
    ms = ev.get("markets") or []
    closes = [_parse(m.get("close_time")) for m in ms]
    opens = [_parse(m.get("open_time")) for m in ms]
    closes = [c for c in closes if c]
    opens = [o for o in opens if o]
    if not closes:
        return None, None
    return (max(opens) if opens else None), min(closes)


def is_hourly(ev, max_duration=MAX_DURATION_SECONDS):
    """
    True if this event looks like a one-hour contract.

    If open_time is missing we cannot measure duration. In that case we do
    NOT assume hourly -- returning False keeps a long-dated contract from
    slipping through on missing data, which is the failure we are guarding
    against.
    """
    open_dt, close_dt = event_window(ev)
    if close_dt is None or open_dt is None:
        return False
    return 0 < (close_dt - open_dt).total_seconds() <= max_duration


def seconds_to_close(ev, now=None):
    """Seconds until this event closes. None if unknown."""
    _, close_dt = event_window(ev)
    if close_dt is None:
        return None
    now = now or datetime.now(timezone.utc)
    return (close_dt - now).total_seconds()


def select_hourly_event(events, now=None,
                        max_remaining=MAX_REMAINING_SECONDS,
                        max_duration=MAX_DURATION_SECONDS):
    """
    Pick the single nearest-expiring HOURLY event.

    Returns (event, close_dt) or (None, None) when nothing qualifies --
    which is a normal, expected state in the seconds after an hour closes
    and before Kalshi lists the next one. Callers must treat (None, None)
    as "wait and retry", never as "fall back to whatever is open".
    """
    now = now or datetime.now(timezone.utc)
    qualifying = []
    for ev in events or []:
        if not (ev.get("markets") or []):
            continue
        if not is_hourly(ev, max_duration=max_duration):
            continue
        _, close_dt = event_window(ev)
        remaining = (close_dt - now).total_seconds()
        if remaining <= MIN_REMAINING_SECONDS:
            continue
        if remaining > max_remaining:
            continue
        qualifying.append((close_dt, ev.get("event_ticker") or "", ev))

    if not qualifying:
        return None, None

    # deterministic: soonest close, then ticker as a stable tie-break
    qualifying.sort(key=lambda x: (x[0], x[1]))
    close_dt, _, ev = qualifying[0]
    return ev, close_dt


def describe_rejections(events, now=None,
                        max_remaining=MAX_REMAINING_SECONDS,
                        max_duration=MAX_DURATION_SECONDS):
    """Human-readable reason each event was skipped. For logging/debugging."""
    now = now or datetime.now(timezone.utc)
    out = []
    for ev in events or []:
        t = ev.get("event_ticker", "?")
        if not (ev.get("markets") or []):
            out.append((t, "no markets"))
            continue
        open_dt, close_dt = event_window(ev)
        if close_dt is None:
            out.append((t, "no close_time"))
            continue
        if open_dt is None:
            out.append((t, "no open_time (cannot verify hourly)"))
            continue
        dur = (close_dt - open_dt).total_seconds()
        rem = (close_dt - now).total_seconds()
        if dur > max_duration:
            out.append((t, f"NOT hourly (duration {dur/3600:.1f}h)"))
        elif rem <= 0:
            out.append((t, f"already closed ({rem:.0f}s)"))
        elif rem > max_remaining:
            out.append((t, f"too far out ({rem/60:.0f} min)"))
        else:
            out.append((t, f"OK (closes in {rem:.0f}s)"))
    return out