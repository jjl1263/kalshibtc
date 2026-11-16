#!/usr/bin/env python3
"""
verify_deploy.py -- prove which files on THIS box are current.

WHY THIS EXISTS
---------------
Deployment has been file-by-file ("scp these three"), and the two boxes each
received a different subset each round. Version drift is the predictable
result: pyflakes on the bot box reported three issues that were already fixed
locally, which meant those files were stale -- and one of them
(kalshi_discover.py) still carried the fixed-point price bug.

Unused imports are cosmetic. A stale file is not: it is a fix you believe you
deployed and did not.

This computes a SHA-256 for every script and compares it against a manifest,
so "is this box current?" is one command instead of an inference.

    # on the machine holding the good copies:
    python3 scripts/verify_deploy.py --write-manifest

    # on each deployed box, after copying the manifest across:
    python3 scripts/verify_deploy.py

Exit code is 0 when everything matches, 1 when anything is stale or missing,
so it can gate a deploy script.
"""

import argparse
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MANIFEST = os.path.join(HERE, "manifest.json")

# Files each box actually needs. config.py is deliberately excluded -- it is
# edited per-box and is expected to differ.
BOT_FILES = [
    "config_defaults.py", "migrate_config.py", "regime_time.py",
    "event_select.py", "kalshi_live_bot.py", "live_view.py",
    "kalshi_auth.py", "kalshi_order_test.py", "kalshi_discover.py",
]
RECORDER_FILES = [
    "config_defaults.py", "migrate_config.py", "regime_time.py",
    "event_select.py", "kalshi_recorder.py", "kalshi_discover.py",
    "audit.py", "diagnose.py", "dashboard.py", "tune_parameters.py",
    "backfill_settlements.py", "prune_data.py", "probe_market_fields.py",
    "export_recorder.py", "btc_breach_study.py", "kalshi_auth.py",
]
EXCLUDE = {"config.py", "manifest.json", "verify_deploy.py"}


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def scan(directory):
    out = {}
    for fn in sorted(os.listdir(directory)):
        if not fn.endswith(".py") or fn in EXCLUDE:
            continue
        out[fn] = sha(os.path.join(directory, fn))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=HERE)
    ap.add_argument("--manifest", default=DEFAULT_MANIFEST)
    ap.add_argument("--write-manifest", action="store_true")
    ap.add_argument("--role", choices=["bot", "recorder", "all"], default="all",
                    help="only check the files this box needs")
    a = ap.parse_args()

    current = scan(a.dir)

    # F5: the checker excludes itself from the manifest (its hash would change
    # every time the manifest is rewritten), so it cannot detect that IT is
    # stale. Printing its own hash lets you compare boxes by eye -- an old
    # copy with an outdated BOT_FILES/RECORDER_FILES list would otherwise
    # silently skip a newly added module.
    _self = sha(os.path.abspath(__file__))
    print(f"verify_deploy.py self-hash: {_self[:12]}")

    if a.write_manifest:
        with open(a.manifest, "w") as f:
            json.dump(current, f, indent=2, sort_keys=True)
        print(f"wrote {a.manifest} ({len(current)} files)")
        for fn in sorted(current):
            print(f"  {current[fn][:12]}  {fn}")
        return 0

    if not os.path.exists(a.manifest):
        print(f"No manifest at {a.manifest}")
        print("Generate one on the machine with the good copies:")
        print("  python3 scripts/verify_deploy.py --write-manifest")
        print("then scp it to this box alongside the scripts.")
        return 1

    with open(a.manifest) as f:
        expected = json.load(f)

    needed = {"bot": BOT_FILES, "recorder": RECORDER_FILES,
              "all": list(expected)}[a.role]
    needed = [f for f in needed if f in expected]

    stale, missing, ok = [], [], []
    for fn in needed:
        if fn not in current:
            missing.append(fn)
        elif current[fn] != expected[fn]:
            stale.append(fn)
        else:
            ok.append(fn)

    extra = [f for f in current if f not in expected]

    print(f"role: {a.role}   checking {len(needed)} file(s)\n")
    if ok:
        print(f"CURRENT ({len(ok)}):")
        for fn in ok:
            print(f"  ok    {fn}")
    if stale:
        print(f"\nSTALE ({len(stale)}) -- these are OLD versions:")
        for fn in stale:
            print(f"  STALE {fn}")
            print(f"          have {current[fn][:12]}  want {expected[fn][:12]}")
    if missing:
        print(f"\nMISSING ({len(missing)}):")
        for fn in missing:
            print(f"  MISS  {fn}")
    if extra:
        print(f"\nnot in manifest ({len(extra)}, ignored):")
        for fn in extra:
            print(f"        {fn}")

    if stale or missing:
        print("\n" + "=" * 62)
        print("THIS BOX IS NOT CURRENT. Copy these from the source machine:")
        print("=" * 62)
        files = " ".join(f"scripts/{f}" for f in sorted(stale + missing))
        print(f"  scp -i <key> {files} \\\n      ubuntu@<host>:~/kalshi-btc/scripts/")
        print("\nThen re-run this check.")
        return 1

    print("\nAll checked files are current.")
    return 0


if __name__ == "__main__":
    sys.exit(main())