#!/usr/bin/env python3
"""
config_defaults.py -- every setting with a safe fallback value.

WHY THIS EXISTS
---------------
config.py lives on the trading box and is edited there, so it does not get
overwritten when new code is deployed. That protects your capital and size
settings -- but it means a NEW setting added in a later version is simply
absent from your server's config, and any code reading it directly raises:

    AttributeError: module 'config' has no attribute 'USE_SAFETY_RATIO_EXIT'

That is a crash at import time, before a single check runs. Losing the bot
because a new optional flag was missing is not an acceptable failure mode.

So the bot reads every setting through cfg() below. A missing key falls back
to the default here and prints a one-line notice, instead of crashing.

    from config_defaults import cfg
    stop = cfg("STOP_LOSS_PCT")

Run this file directly to see what your config is missing:

    python3 scripts/config_defaults.py
"""

import sys

# Minimum supported Python. The project once briefly required 3.12 because
# an automated edit produced PEP 701 nested-quote f-strings, which are a
# SyntaxError at IMPORT on 3.10/3.11 -- the bot would not start at all.
# That is fixed; this check makes a recurrence obvious rather than cryptic.
MIN_PYTHON = (3, 9)
if sys.version_info < MIN_PYTHON:
    raise SystemExit(
        f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required, "
        f"found {sys.version_info.major}.{sys.version_info.minor}. "
        f"Ubuntu 24.04 ships 3.12; 22.04 ships 3.10.")

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

try:
    import config as _user
except Exception:                                    # pragma: no cover
    _user = None


# Every setting the bot understands, with the value used when config.py
# does not define it. Keep this in sync with config.py.
DEFAULTS = {
    # capital
    "STARTING_CAPITAL": 100.00,
    "CONTRACTS_PER_TRADE": 3,
    "DAILY_LOSS_LIMIT_DOLLARS": 9.00,

    # entry window
    "TAU_MAX_SECONDS": 240,
    "TAU_MIN_SECONDS": 90,

    # price
    "MIN_ASK_CENTS": 99,
    "MAX_ASK_CENTS": None,

    # marked strike
    "MARKED_STRIKE_TAU_SECONDS": 90,

    # safety margin
    "GAP_SIGMAS": 3.0,
    "GAP_MIN_ABS_USD": 25,
    "REGIME_GAP_SIGMAS": {},
    "USE_REGIME_OVERRIDES": True,
    "REGIME_PARAMS": {},
    "REGIME_HOUR_GRANULARITY": 4,
    "REGIME_TIMEZONE": "America/New_York",

    # speed gates
    "TTS_SAFETY_MULTIPLIER": 3.0,
    "RECENT_MOVE_WINDOW_SECONDS": 10,
    "RECENT_MOVE_MAX_USD": 30.0,

    # exit / stop loss
    "STOP_LOSS_PCT": 0.10,
    "STOP_LOSS_DOLLARS": None,
    "STOP_CHECK_SECONDS": 1.0,
    "USE_SAFETY_RATIO_EXIT": False,
    "EXIT_SAFETY_RATIO": 0.4,

    # cooldown
    "COOLDOWN_AFTER_CONSECUTIVE_LOSSES": 2,
    "COOLDOWN_MINUTES": 180,
    "COOLDOWN_ESCALATION": 1.0,
    "COOLDOWN_MAX_MINUTES": 180,
    "COOLDOWN_AFTER_ANY_LOSS_MINUTES": 0,

    # fills
    "RETRY_UNTIL_FILL": True,
    "RETRY_INTERVAL_SECONDS": 1.0,
    "RETRY_STOP_BEFORE_CLOSE": 10,
    "ACCEPT_PARTIAL_FILLS": True,
    "MIN_DEPTH_CONTRACTS": None,

    # polling
    "POLL_SECONDS": 1.0,
    "POSITION_POLL_SECONDS": 0.5,

    # remote operation
    "CONFIRM_TIMEOUT_SECONDS": 20,
    "DECLINE_COOLDOWN_SECONDS": 15,

    # recording filter
    "RECORD_ALL": False,
    "RECORD_MIN_ASK_CENTS": 90,
    "RECORD_GAP_WINDOW_USD": 1500,
}

_warned = set()


def cfg(name, default=None):
    """
    Read a setting from config.py, falling back to DEFAULTS.

    Warns once per missing key so a stale config is visible without being
    noisy, and never raises.
    """
    if _user is not None and hasattr(_user, name):
        return getattr(_user, name)
    if name in DEFAULTS:
        if name not in _warned:
            _warned.add(name)
            print(f"  [config] '{name}' not in config.py -- using default "
                  f"{DEFAULTS[name]!r}")
        return DEFAULTS[name]
    return default


def missing_keys():
    """Settings the bot understands but config.py does not define."""
    if _user is None:
        return sorted(DEFAULTS)
    return sorted(k for k in DEFAULTS if not hasattr(_user, k))


def extra_keys():
    """Settings in config.py that this version does not know about."""
    if _user is None:
        return []
    return sorted(k for k in dir(_user)
                  if k.isupper() and not k.startswith("_")
                  and k not in DEFAULTS)


def report():
    print(f"python: {sys.version_info.major}.{sys.version_info.minor}."
          f"{sys.version_info.micro}  (minimum "
          f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]})\n")
    miss = missing_keys()
    extra = extra_keys()
    print(f"config.py defines {len(DEFAULTS) - len(miss)} of "
          f"{len(DEFAULTS)} known settings\n")
    if miss:
        print(f"MISSING ({len(miss)}) -- defaults will be used:")
        for k in miss:
            print(f"  {k:38} default {DEFAULTS[k]!r}")
        print("\n  These are safe (the bot falls back), but you cannot tune")
        print("  what is not there. Run:  python3 scripts/migrate_config.py")
    else:
        print("No missing settings. config.py is up to date.")
    if extra:
        print(f"\nUNKNOWN to this version ({len(extra)}) -- ignored:")
        for k in extra:
            print(f"  {k}")

    print("\nCurrent effective values:")
    for k in ["STARTING_CAPITAL", "CONTRACTS_PER_TRADE",
              "DAILY_LOSS_LIMIT_DOLLARS", "STOP_LOSS_PCT", "STOP_LOSS_DOLLARS",
              "USE_SAFETY_RATIO_EXIT", "COOLDOWN_AFTER_CONSECUTIVE_LOSSES",
              "COOLDOWN_MINUTES", "MIN_ASK_CENTS", "GAP_SIGMAS",
              "REGIME_TIMEZONE"]:
        src = "config.py" if (_user and hasattr(_user, k)) else "DEFAULT"
        print(f"  {k:38} {cfg(k)!r:28} ({src})")


if __name__ == "__main__":
    report()