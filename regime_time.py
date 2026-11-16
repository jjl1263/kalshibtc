#!/usr/bin/env python3
"""
regime_time.py -- one shared definition of "which regime is it right now?"

WHY THIS EXISTS
---------------
Kalshi labels its hourly BTC events in EASTERN TIME. Verified against real
data:

    ticker     KXBTCD-26AUG1319       <- suffix '19' = 19:00 ET
    close_time 2026-08-13T23:00:00Z   <- 23:00 UTC = 19:00 EDT

Eastern Time observes daylight saving. The same ET hour maps to different
UTC hours depending on the season:

    19:00 ET in August   (EDT, UTC-4) = 23:00 UTC
    19:00 ET in December (EST, UTC-5) = 00:00 UTC

So a regime block written in UTC silently drifts by one hour twice a year.
A "US/Europe overlap, deepest books" block tuned in summer as 13-17 UTC is
actually 14-18 UTC in winter -- the parameters stop pointing at the session
they were fitted to.

The drivers of BTC liquidity are US session hours (ETF flows, US desks,
Kalshi's own user base), which follow ET, not UTC. So regimes are defined in
ET by default and DST is handled automatically.

CRITICAL: the bot, tune_parameters.py and audit.py must all bucket time the
SAME way. If the tuner fits parameters using UTC buckets and the bot applies
them using ET buckets, the tuning is worthless. That is why this logic lives
in one module that all three import.

Set REGIME_TIMEZONE = "UTC" in config.py to opt out and use fixed UTC hours.
"""

from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
    _HAVE_ZONEINFO = True
except ImportError:                                    # Python < 3.9
    _HAVE_ZONEINFO = False

try:
    import config as _cfg
    TZ_NAME = getattr(_cfg, "REGIME_TIMEZONE", "America/New_York")
except Exception:
    TZ_NAME = "America/New_York"


def get_tz():
    """The tz regimes are expressed in. Falls back to UTC if unavailable."""
    if TZ_NAME.upper() == "UTC" or not _HAVE_ZONEINFO:
        return timezone.utc
    try:
        return ZoneInfo(TZ_NAME)
    except Exception:
        return timezone.utc


TZ = get_tz()


def tz_label():
    return "UTC" if TZ is timezone.utc else TZ_NAME


def to_regime_tz(dt):
    """Convert an aware datetime into the regime timezone."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ)


def from_timestamp(ts):
    """Unix timestamp -> datetime in the regime timezone."""
    return datetime.fromtimestamp(ts, TZ)


def regime_key(dt):
    """
    (day_type, hour) for a datetime, evaluated in the regime timezone.

    day_type is 'weekend' for Sat/Sun IN THAT TIMEZONE -- which is what you
    want, since "weekend liquidity" is a property of the local trading week,
    not of UTC.
    """
    local = to_regime_tz(dt)
    return ("weekend" if local.weekday() >= 5 else "weekday"), local.hour


def regime_key_from_ts(ts):
    """Same, from a unix timestamp. Used by the analysis scripts."""
    local = from_timestamp(ts)
    return ("weekend" if local.weekday() >= 5 else "weekday"), local.hour


def hour_block(hour, granularity=4):
    """
    (start, end) of the block containing `hour`, aligned to `granularity`.
    granularity=4 -> 00-04, 04-08, ... ; granularity=1 -> every hour.
    """
    g = max(1, int(granularity))
    start = (hour // g) * g
    return (start, min(start + g, 24))


def describe(dt=None):
    """Human-readable current regime, for logs and headers."""
    dt = dt or datetime.now(timezone.utc)
    local = to_regime_tz(dt)
    day_type, hour = regime_key(dt)
    return (f"{day_type} {hour:02d}:00 {local:%Z} "
            f"({local:%a %Y-%m-%d %H:%M})")


# SQL fragment so audit.py buckets identically to the Python code above.
# SQLite has no timezone database, so a fixed-offset regime tz cannot be
# expressed exactly. Analysis scripts therefore convert in Python instead;
# this constant exists to make the limitation explicit rather than silently
# producing UTC buckets that disagree with the bot.
SQL_TZ_NOTE = (
    "SQLite cannot do timezone conversion. Analysis scripts convert "
    "timestamps in Python via regime_key_from_ts() so buckets match the bot."
)


if __name__ == "__main__":
    now = datetime.now(timezone.utc)
    print(f"regime timezone : {tz_label()}")
    print(f"now (UTC)       : {now:%Y-%m-%d %H:%M}")
    print(f"now (regime tz) : {to_regime_tz(now):%Y-%m-%d %H:%M %Z}")
    print(f"regime key      : {regime_key(now)}")
    print(f"described       : {describe(now)}")
    print()
    print("DST demonstration -- same ET hour, different UTC hour:")
    for m, lbl in [(8, "August"), (12, "December")]:
        d = to_regime_tz(datetime(2026, m, 13, 19, 0, tzinfo=timezone.utc))
        u = datetime(2026, m, 13, 19, 0, tzinfo=TZ).astimezone(timezone.utc)
        print(f"  19:00 {tz_label()} in {lbl:9} = {u:%H:%M} UTC")


# ---------------------------------------------------------------- day keys

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def day_keys(dt):
    """
    Every day-key that matches this datetime, MOST SPECIFIC FIRST.

    Regime tables may key on:
        "mon".."sun"   a specific weekday          (highest precedence)
        "weekday"      Mon-Fri
        "weekend"      Sat-Sun
        "all"          any day                     (lowest precedence)

    Returning them in precedence order lets a lookup take the first match,
    so a ("fri", (13,17)) block beats a ("weekday", (13,17)) block on a
    Friday afternoon without either needing to know about the other.

    Evaluated in the REGIME TIMEZONE, so "friday" means the local trading
    day, not the UTC one.
    """
    local = to_regime_tz(dt)
    wd = local.weekday()
    return [DAY_NAMES[wd],
            "weekend" if wd >= 5 else "weekday",
            "all"]


def day_keys_from_ts(ts):
    """Same, from a unix timestamp."""
    return day_keys(datetime.fromtimestamp(ts, timezone.utc))


def day_name(dt):
    """'mon'..'sun' in the regime timezone."""
    return DAY_NAMES[to_regime_tz(dt).weekday()]


def day_name_from_ts(ts):
    return DAY_NAMES[from_timestamp(ts).weekday()]


def match_regime(table, dt, granularity=None):
    """
    Look up `dt` in a regime table keyed by (day_key, (h0, h1)).

    Tries day keys in precedence order: specific day, then weekday/weekend,
    then "all". Within a day key, the first hour block containing the local
    hour wins.

    Returns (label, value) or (None, None).
    """
    if not table:
        return None, None
    local = to_regime_tz(dt)
    hour = local.hour
    for dk in day_keys(dt):
        for key, val in table.items():
            try:
                k_day, (h0, h1) = key
            except (TypeError, ValueError):
                continue
            if str(k_day).lower() != dk:
                continue
            if h0 <= hour < h1:
                return f"{dk} {h0:02d}-{h1:02d} {local:%Z}", val
    return None, None