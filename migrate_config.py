#!/usr/bin/env python3
"""
migrate_config.py -- add new settings to config.py WITHOUT touching the
values you have already set.

THE PROBLEM THIS SOLVES
-----------------------
config.py is edited on the trading box and deliberately never overwritten by
a deploy, so your capital and size settings survive updates. But that means
a setting introduced in a later version is simply absent, and `sed` cannot
add it -- sed only rewrites lines that already exist. The result:

    AttributeError: module 'config' has no attribute 'USE_SAFETY_RATIO_EXIT'

This script appends only the MISSING keys, with their documented defaults,
in a clearly marked block at the end of the file. Every existing line is left
byte-for-byte alone.

    python3 scripts/migrate_config.py            # dry run -- shows what it would add
    python3 scripts/migrate_config.py --apply    # append them
    python3 scripts/migrate_config.py --apply --backup-only   # just make a backup

A timestamped backup is written before any change.
"""

import argparse
import os
import shutil
import sys
import time

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

from config_defaults import DEFAULTS, missing_keys, extra_keys  # noqa: E402

# One-line explanations for the settings most likely to be missing.
NOTES = {
    "USE_SAFETY_RATIO_EXIT": "exit on BTC spot moving toward the strike (off = your 3-brake spec)",
    "EXIT_SAFETY_RATIO": "threshold for the above, when enabled",
    "STOP_LOSS_DOLLARS": "absolute dollar stop; overrides STOP_LOSS_PCT when set",
    "STOP_CHECK_SECONDS": "how often the stop is evaluated",
    "POSITION_POLL_SECONDS": "position monitoring interval while holding",
    "REGIME_TIMEZONE": "timezone for regime blocks (Kalshi labels events in Eastern)",
    "REGIME_PARAMS": "per-regime overrides: sigmas/tau/depth/contracts/enabled",
    "REGIME_HOUR_GRANULARITY": "hour-block width used by the tuner",
    "MIN_DEPTH_CONTRACTS": "required resting size; None = follow order size",
    "COOLDOWN_ESCALATION": "1.0 = every cooldown is the same length",
    "COOLDOWN_MAX_MINUTES": "cap on cooldown length",
    "COOLDOWN_AFTER_ANY_LOSS_MINUTES": "pause after a single loss; 0 = off",
    "RECORD_ALL": "recorder: keep every row (large)",
    "RECORD_MIN_ASK_CENTS": "recorder: keep offers at or above this",
    "RECORD_GAP_WINDOW_USD": "recorder: keep strikes within this of spot",
    "CONFIRM_TIMEOUT_SECONDS": "y/N prompt timeout; entries fail NO, exits fail YES",
    "DECLINE_COOLDOWN_SECONDS": "wait before re-asking after a declined exit",
    "ACCEPT_PARTIAL_FILLS": "manage a partial fill rather than requiring full size",
    "RETRY_UNTIL_FILL": "retry every second until filled or the window closes",
    "RETRY_INTERVAL_SECONDS": "retry cadence",
    "RETRY_STOP_BEFORE_CLOSE": "stop retrying this many seconds before close",
}


def fmt(v):
    return repr(v)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="scripts/config.py")
    ap.add_argument("--apply", action="store_true",
                    help="actually append (default is a dry run)")
    ap.add_argument("--backup-only", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(a.config):
        raise SystemExit(f"No config at {a.config}")

    miss = missing_keys()
    extra = extra_keys()

    print(f"config: {a.config}")
    print(f"known settings: {len(DEFAULTS)}")
    print(f"already present: {len(DEFAULTS) - len(miss)}")
    print(f"missing: {len(miss)}\n")

    if extra:
        print(f"(this version does not use {len(extra)} setting(s) in your "
             f"config; they are left alone)\n")

    if not miss:
        print("Nothing to add -- config.py already has every known setting.")
        return

    block = ["", "", "# " + "=" * 74,
             "# ADDED BY migrate_config.py on "
             + time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
             "#",
             "# These settings exist in the current bot version but were not in",
             "# your config.py. Defaults are shown; edit freely. Nothing above",
             "# this line was modified.",
             "# " + "=" * 74, ""]
    for k in miss:
        note = NOTES.get(k)
        if note:
            block.append(f"# {note}")
        block.append(f"{k} = {fmt(DEFAULTS[k])}")
        block.append("")

    text = "\n".join(block)

    print("WOULD APPEND:")
    print("-" * 60)
    print(text)
    print("-" * 60)

    if not a.apply:
        print("\nDRY RUN -- nothing written. Re-run with --apply.")
        return

    backup = f"{a.config}.bak.{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy(a.config, backup)
    print(f"\nbackup written: {backup}")

    if a.backup_only:
        print("--backup-only: stopping here.")
        return

    with open(a.config, "a") as f:
        f.write(text)
    print(f"appended {len(miss)} setting(s) to {a.config}")

    # verify it still imports and now resolves
    import importlib
    try:
        import config
        importlib.reload(config)
        still = [k for k in DEFAULTS if not hasattr(config, k)]
        if still:
            print(f"!! still missing after append: {still}")
        else:
            print("verified: config.py now defines every known setting")
    except Exception as e:
        print(f"!! config.py failed to import after append: {e}")
        print(f"!! restore with:  cp {backup} {a.config}")


if __name__ == "__main__":
    main()