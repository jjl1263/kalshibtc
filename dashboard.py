#!/usr/bin/env python3
"""
dashboard.py -- simulate the strategy over recorded data and render a
self-contained HTML report.

WHAT IT DOES
    Replays every recorded hour applying your CURRENT config.py gates
    (price band, sigma requirement, tau window, depth, marked-strike rule),
    simulates fills, applies fees, stop loss and the loss cooldown, and
    tracks a running balance from STARTING_CAPITAL.

WHAT IT IS NOT
    Not a prediction. It is "what would have happened on the hours I have
    recorded, under these settings". With losses this rare, a clean equity
    curve over a few days is the EXPECTED look of a break-even strategy too.
    The report states its own confidence interval for that reason.

    Fills are optimistic in one specific way: it assumes you get filled at
    the displayed ask whenever depth >= your size. Real fills can miss.

Usage:
    python3 scripts/dashboard.py
    python3 scripts/dashboard.py --balance 20 --contracts 3 --open
"""

import argparse
import math
import os
import sqlite3
import sys
import webbrowser
from datetime import datetime, timezone

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")
try:
    import config as CFG
except Exception:
    CFG = None


def cfgv(name, default):
    return getattr(CFG, name, default) if CFG else default


# ---------------------------------------------------------------- fees

def order_fee_cents(count, price_cents):
    """Kalshi taker fee, rounded UP to the next cent, charged per order."""
    p = price_cents / 100.0
    return math.ceil(0.07 * count * p * (1 - p) * 100)


# ---------------------------------------------------------------- sim

def simulate(db, params):
    con = sqlite3.connect(db)
    con.execute("PRAGMA cache_size=-64000")

    # settled results
    res = {}
    try:
        for mt, r in con.execute("SELECT market_ticker, result FROM settlements "
                                 "WHERE result IN ('yes','no')"):
            res[mt] = r
    except sqlite3.Error:
        pass

    # Pull only candidate rows -- indexed, small, never the whole table.
    sql = """
        SELECT ts, event_ticker, market_ticker, floor_strike, tau, btc_spot,
               gap, yes_ask, yes_bid, ask_size_at_ask, is_marked
        FROM ticks
        WHERE tau <= ? AND tau >= ?
          AND yes_ask IS NOT NULL AND yes_ask < 100 AND yes_ask >= ?
          AND is_marked = 0
          AND ask_size_at_ask >= ?
        ORDER BY event_ticker, ts
    """
    rows = con.execute(sql, (params["tau_max"], params["tau_min"],
                            params["min_ask"],
                            params.get("min_depth") or params["contracts"])
                       ).fetchall()

    # rolling 5-min BTC range per event, for the sigma gate
    spots = {}
    spot_rows = con.execute(
        "SELECT event_ticker, ts, MAX(btc_spot) FROM ticks "
        "WHERE btc_spot IS NOT NULL GROUP BY event_ticker, ts "
        "ORDER BY event_ticker, ts").fetchall()
    for ev, ts, sp in spot_rows:
        spots.setdefault(ev, []).append((ts, sp))
    con.close()

    def move_at(ev, ts, window):
        """Signed spot change over the trailing `window` seconds."""
        arr = spots.get(ev)
        if not arr:
            return None
        prior = [p for t, p in arr if t <= ts - window]
        cur = [p for t, p in arr if t <= ts]
        if not prior or not cur:
            return None
        return cur[-1] - prior[-1]

    def rv5_at(ev, ts):
        arr = spots.get(ev)
        if not arr:
            return None
        lo = ts - 300
        vals = [s for t, s in arr if lo <= t <= ts]
        if len(vals) < 30:
            return None
        return max(vals) - min(vals)

    balance = params["balance"]
    start_balance = balance
    trades = []
    equity = [(None, balance)]
    consec = 0
    cooldown_until = 0.0
    trig = 0
    daily = {}
    traded_events = set()

    for (ts, ev, mt, strike, tau, spot, gap, ask, bid, depth, marked) in rows:
        if ev in traded_events:
            continue
        if ts < cooldown_until:
            continue
        day = datetime.fromtimestamp(ts, timezone.utc).date()
        if daily.get(day, 0.0) <= -abs(params["daily_limit"]):
            continue
        if gap is None or spot is None:
            continue

        rv5 = rv5_at(ev, ts)
        if rv5 is None:
            continue
        sigma5 = rv5 / 1.67
        sigma_t = sigma5 * math.sqrt(max(tau, 1) / 300.0)
        need = max(params["gap_min_abs"], params["sigmas"] * sigma_t)
        if gap < need:
            continue

        # g6 -- the raw "is it moving too fast right now" check. The bot and
        # tune_parameters both apply this; the dashboard did not, so the same
        # settings produced different trade counts in the two tools.
        if params.get("recent_move_max") is not None:
            mv = move_at(ev, ts, params.get("recent_move_window", 10))
            if mv is not None and abs(mv) > params["recent_move_max"]:
                continue

        # ---- take the trade ----
        n = params["contracts"]
        fee_in = order_fee_cents(n, ask)
        cost = n * ask + fee_in
        if cost / 100.0 > balance:
            continue

        result = res.get(mt)
        if result is None:
            continue

        won = (result == "yes")
        if won:
            pnl_c = n * 100 - cost
            exit_reason = "settled_win"
        else:
            pnl_c = -cost
            exit_reason = "settled_loss"

        pnl = pnl_c / 100.0
        balance += pnl
        daily[day] = daily.get(day, 0.0) + pnl
        traded_events.add(ev)

        if pnl > 0:
            consec = 0
            trig = 0
        else:
            consec += 1
            if consec >= params["cooldown_losses"]:
                trig += 1
                mins = min(params["cooldown_min"] *
                          (params["cooldown_esc"] ** (trig - 1)),
                          params["cooldown_max"])
                cooldown_until = ts + mins * 60

        dt = datetime.fromtimestamp(ts, timezone.utc)
        trades.append({
            "ts": ts, "dt": dt, "event": ev, "market": mt,
            "strike": strike, "tau": tau, "spot": spot, "gap": gap,
            "required_gap": need, "sigma_tau": sigma_t,
            "ask": ask, "depth": depth, "contracts": n,
            "fee": fee_in, "cost": cost, "won": won,
            "pnl": pnl, "balance": balance, "exit": exit_reason,
            "weekday": dt.strftime("%a"), "hour": dt.hour,
            "is_weekend": dt.weekday() >= 5,
        })
        equity.append((dt, balance))

    return trades, equity, start_balance


# ---------------------------------------------------------------- stats

def wilson(wins, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def compute_stats(trades, equity, start_balance, params):
    n = len(trades)
    wins = sum(1 for t in trades if t["won"])
    losses = n - wins
    pnl = sum(t["pnl"] for t in trades)
    fees = sum(t["fee"] for t in trades) / 100.0
    deployed = sum(t["cost"] for t in trades) / 100.0
    wr = wins / n if n else 0.0
    lo, hi = wilson(wins, n)

    peak = start_balance
    max_dd = 0.0
    max_dd_pct = 0.0
    for _, b in equity:
        peak = max(peak, b)
        dd = peak - b
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = dd / peak * 100 if peak else 0

    # break-even at the traded price / size
    be = None
    if n:
        avg_ask = sum(t["ask"] for t in trades) / n
        c = params["contracts"]
        f = order_fee_cents(c, avg_ask)
        win_c = c * 100 - (c * avg_ask + f)
        loss_c = c * avg_ask + f
        be = loss_c / (win_c + loss_c) if (win_c + loss_c) else None

    streak = best = 0
    for t in trades:
        if not t["won"]:
            streak += 1
            best = max(best, streak)
        else:
            streak = 0

    return {
        "trades": n, "wins": wins, "losses": losses,
        "win_rate": wr, "ci_lo": lo, "ci_hi": hi,
        "pnl": pnl, "fees": fees, "deployed": deployed,
        "roi": (pnl / start_balance * 100) if start_balance else 0,
        "end_balance": start_balance + pnl,
        "max_dd": max_dd, "max_dd_pct": max_dd_pct,
        "breakeven": be, "worst_streak": best,
        "avg_gap": sum(t["gap"] for t in trades) / n if n else 0,
        "avg_tau": sum(t["tau"] for t in trades) / n if n else 0,
    }


def by_bucket(trades, keyfn):
    out = {}
    for t in trades:
        k = keyfn(t)
        d = out.setdefault(k, {"n": 0, "w": 0, "pnl": 0.0})
        d["n"] += 1
        d["w"] += 1 if t["won"] else 0
        d["pnl"] += t["pnl"]
    return out


# ---------------------------------------------------------------- html

CSS = """
:root{--bg:#0f1117;--panel:#171a23;--line:#262b38;--txt:#e6e9ef;--dim:#8b93a7;
--pos:#3ecf8e;--neg:#ff6b6b;--acc:#5b8def;--warn:#f5a623}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font:14px/1.5 -apple-system,
BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;padding:24px}
h1{font-size:22px;margin-bottom:4px}
h2{font-size:15px;margin:28px 0 12px;color:var(--dim);text-transform:uppercase;
letter-spacing:.08em;font-weight:600}
.sub{color:var(--dim);font-size:13px;margin-bottom:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:14px 16px}
.card .label{color:var(--dim);font-size:11px;text-transform:uppercase;
letter-spacing:.06em;margin-bottom:6px}
.card .val{font-size:24px;font-weight:600;font-variant-numeric:tabular-nums}
.card .note{color:var(--dim);font-size:11px;margin-top:4px}
.pos{color:var(--pos)}.neg{color:var(--neg)}.acc{color:var(--acc)}
table{width:100%;border-collapse:collapse;background:var(--panel);
border:1px solid var(--line);border-radius:10px;overflow:hidden}
th{background:#1c202b;color:var(--dim);font-size:11px;text-transform:uppercase;
letter-spacing:.05em;text-align:left;padding:10px 12px}
td{padding:9px 12px;border-top:1px solid var(--line);font-variant-numeric:tabular-nums}
tr:hover td{background:#1b1f2a}
.banner{background:#2a1f1a;border:1px solid #5a3a28;border-radius:10px;
padding:14px 16px;margin-bottom:20px;font-size:13px}
.banner b{color:#f5a623}
svg{background:var(--panel);border:1px solid var(--line);border-radius:10px}
.bar{height:7px;background:#232838;border-radius:4px;overflow:hidden;margin-top:6px}
.bar span{display:block;height:100%;background:var(--acc)}
.small{font-size:12px;color:var(--dim)}
"""


def equity_svg(equity, w=1000, h=220, pad=34):
    if len(equity) < 2:
        return "<p class='small'>Not enough trades to plot.</p>"
    vals = [b for _, b in equity]
    lo, hi = min(vals), max(vals)
    if hi == lo:
        hi = lo + 1
    n = len(vals)
    pts = []
    for i, v in enumerate(vals):
        x = pad + (w - 2 * pad) * i / (n - 1)
        y = h - pad - (h - 2 * pad) * (v - lo) / (hi - lo)
        pts.append(f"{x:.1f},{y:.1f}")
    color = "var(--pos)" if vals[-1] >= vals[0] else "var(--neg)"
    base_y = h - pad - (h - 2 * pad) * (vals[0] - lo) / (hi - lo)
    return f"""<svg viewBox="0 0 {w} {h}" width="100%" height="{h}">
<line x1="{pad}" y1="{base_y:.1f}" x2="{w-pad}" y2="{base_y:.1f}"
 stroke="var(--line)" stroke-dasharray="4 4"/>
<polyline points="{' '.join(pts)}" fill="none" stroke="{color}" stroke-width="2"/>
<text x="{pad}" y="{pad-12}" fill="var(--dim)" font-size="11">${hi:,.2f}</text>
<text x="{pad}" y="{h-pad+16}" fill="var(--dim)" font-size="11">${lo:,.2f}</text>
</svg>"""


def card(label, val, cls="", note=""):
    return (f'<div class="card"><div class="label">{label}</div>'
            f'<div class="val {cls}">{val}</div>'
            f'{f"<div class=note>{note}</div>" if note else ""}</div>')


def render(trades, equity, start_balance, stats, params, db, span):
    n = stats["trades"]
    pcls = "pos" if stats["pnl"] >= 0 else "neg"
    be_txt = f"{stats['breakeven']*100:.2f}%" if stats["breakeven"] else "n/a"

    # honesty banner
    # Trades needed for the Wilson lower bound to clear break-even, assuming
    # the observed rate holds. Solved numerically: the closed-form normal
    # approximation divides by zero at a 100% observed rate, which is exactly
    # the case this strategy produces early on.
    need = "n/a"
    if stats["breakeven"] and stats["win_rate"] > stats["breakeven"]:
        p_obs = stats["win_rate"]
        b = stats["breakeven"]
        found = None
        for m in range(10, 200001, 10):
            lo_m, _ = wilson(int(round(p_obs * m)), m)
            if lo_m > b:
                found = m
                break
        need = f"{found:,}" if found else ">200,000"

    # THREE distinct verdicts. The old text said "still includes break-even"
    # whenever the LOWER bound was below it -- which mislabels the case where
    # the ENTIRE interval sits below break-even. That is not inconclusive,
    # it is conclusively losing, and it is the one case you most need stated
    # plainly.
    be = stats["breakeven"]
    if not be or n == 0:
        verdict = ""
        need_line = ""
    elif stats["ci_hi"] < be:
        verdict = ("— the entire interval is <b>BELOW</b> break-even. On this "
                   "sample these settings lose money; this is not a "
                   "small-sample artifact.")
        need_line = ("No trade count fixes this — the settings themselves "
                     "need to change.")
    elif stats["ci_lo"] > be:
        verdict = ("— the lower bound clears break-even, so this sample is "
                   "consistent with a profitable edge.")
        need_line = (f"Approx. <b>{need}</b> trades are needed for statistical "
                     f"confidence at this edge.")
    else:
        verdict = ("— which still includes break-even, so this sample cannot "
                   "yet distinguish a profitable strategy from a losing one.")
        need_line = (f"Approx. <b>{need}</b> trades are needed for statistical "
                     f"confidence at this edge.")

    banner = f"""<div class="banner">
<b>Read this before trusting the numbers.</b> This is a replay of {n} trades over
{span:.1f} days of recorded data using your current config — not a forecast.
Break-even at this price and size is <b>{be_txt}</b>. The observed win rate of
{stats['win_rate']*100:.2f}% has a 95% confidence interval of
<b>{stats['ci_lo']*100:.1f}% – {stats['ci_hi']*100:.1f}%</b>
{verdict}
{need_line}
Fills are assumed whenever depth ≥ size, which is optimistic.
</div>"""

    dow = by_bucket(trades, lambda t: t["weekday"])
    hod = by_bucket(trades, lambda t: t["hour"])
    wk = by_bucket(trades, lambda t: "weekend" if t["is_weekend"] else "weekday")

    def bucket_rows(d, label, sort_key=None):
        keys = sorted(d.keys(), key=sort_key) if sort_key else sorted(d.keys())
        out = ""
        for k in keys:
            v = d[k]
            wr = v["w"] / v["n"] * 100 if v["n"] else 0
            cls = "pos" if v["pnl"] >= 0 else "neg"
            out += (f"<tr><td>{k}</td><td>{v['n']}</td><td>{v['w']}</td>"
                   f"<td>{v['n']-v['w']}</td><td>{wr:.1f}%</td>"
                   f"<td class='{cls}'>${v['pnl']:+.2f}</td></tr>")
        return out

    trade_rows = ""
    for t in reversed(trades[-100:]):
        cls = "pos" if t["won"] else "neg"
        trade_rows += (
            f"<tr><td>{t['dt']:%Y-%m-%d %H:%M:%S}</td>"
            f"<td>{t['weekday']}</td>"
            f"<td>${t['strike']:,.0f}</td>"
            f"<td>${t['spot']:,.0f}</td>"
            f"<td>${t['gap']:,.0f}</td>"
            f"<td>${t['required_gap']:,.0f}</td>"
            f"<td>{t['tau']:.0f}s</td>"
            f"<td>{t['ask']:.0f}c</td>"
            f"<td>{t['contracts']}</td>"
            f"<td class='{cls}'>{'WIN' if t['won'] else 'LOSS'}</td>"
            f"<td class='{cls}'>${t['pnl']:+.2f}</td>"
            f"<td>${t['balance']:,.2f}</td></tr>")

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Kalshi BTC — Strategy Simulation</title><style>{CSS}</style></head><body>
<h1>Kalshi BTC Hourly — Strategy Simulation</h1>
<div class="sub">{db} &nbsp;·&nbsp; {span:.1f} days of recorded data &nbsp;·&nbsp;
generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC</div>
{banner}

<h2>Result</h2>
<div class="grid">
{card("Starting balance", f"${start_balance:,.2f}")}
{card("Ending balance", f"${stats['end_balance']:,.2f}", pcls)}
{card("Net P&amp;L", f"${stats['pnl']:+,.2f}", pcls)}
{card("Return", f"{stats['roi']:+.2f}%", pcls)}
{card("Trades", f"{n}")}
{card("Win rate", f"{stats['win_rate']*100:.2f}%",
      "pos" if stats["breakeven"] and stats["win_rate"] > stats["breakeven"] else "neg",
      f"break-even {be_txt}")}
</div>

<h2>Equity curve</h2>
{equity_svg(equity)}

<h2>Risk</h2>
<div class="grid">
{card("Max drawdown", f"${stats['max_dd']:,.2f}", "neg",
      f"{stats['max_dd_pct']:.1f}% of peak")}
{card("Worst losing streak", f"{stats['worst_streak']}")}
{card("Losses", f"{stats['losses']}", "neg")}
{card("Fees paid", f"${stats['fees']:,.2f}", "",
      f"{stats['fees']/abs(stats['pnl'])*100:.0f}% of net P&amp;L" if stats['pnl'] else "")}
{card("Capital deployed", f"${stats['deployed']:,.2f}")}
{card("95% CI on win rate",
      f"{stats['ci_lo']*100:.1f}–{stats['ci_hi']*100:.1f}%", "acc")}
</div>

<h2>Entry profile</h2>
<div class="grid">
{card("Avg gap at entry", f"${stats['avg_gap']:,.0f}")}
{card("Avg seconds left", f"{stats['avg_tau']:.0f}s")}
{card("Contracts / trade", f"{params['contracts']}")}
{card("Sigma requirement", f"{params['sigmas']}")}
{card("Tau window", f"{params['tau_min']:.0f}–{params['tau_max']:.0f}s")}
{card("Min ask", f"{params['min_ask']:.0f}c")}
</div>

<h2>By day of week</h2>
<table><tr><th>Day</th><th>Trades</th><th>Wins</th><th>Losses</th>
<th>Win rate</th><th>P&amp;L</th></tr>
{bucket_rows(dow, "day", lambda k: ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"].index(k)
             if k in ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"] else 9)}
</table>

<h2>Weekday vs weekend</h2>
<table><tr><th>Type</th><th>Trades</th><th>Wins</th><th>Losses</th>
<th>Win rate</th><th>P&amp;L</th></tr>
{bucket_rows(wk, "type")}
</table>

<h2>By UTC hour</h2>
<table><tr><th>Hour</th><th>Trades</th><th>Wins</th><th>Losses</th>
<th>Win rate</th><th>P&amp;L</th></tr>
{bucket_rows(hod, "hour")}
</table>

<h2>Trades (most recent 100)</h2>
<table><tr><th>Time (UTC)</th><th>Day</th><th>Strike</th><th>Spot</th>
<th>Gap</th><th>Required</th><th>Tau</th><th>Ask</th><th>Qty</th>
<th>Result</th><th>P&amp;L</th><th>Balance</th></tr>
{trade_rows}
</table>

<p class="small" style="margin-top:24px">
Simulation applies: price ≥ {params['min_ask']:.0f}c and &lt;100c, depth ≥ size,
not the marked strike, gap ≥ {params['sigmas']}σ scaled to time remaining,
tau {params['tau_min']:.0f}–{params['tau_max']:.0f}s, one trade per event,
Kalshi fees rounded up per order, cooldown after
{params['cooldown_losses']} consecutive losses, daily loss limit
${params['daily_limit']:.2f}.
</p>
</body></html>"""


# ---------------------------------------------------------------- main

def _be_polite():
    """
    Lower this process's CPU priority.

    These analysis scripts run on the SAME box as the recorder, which has a
    hard real-time requirement: it must sample the book every second. A
    4,400-combination sweep can saturate a 2-core VM and cause the recorder
    to miss ticks -- corrupting the very data being analysed.

    Renicing costs nothing when the machine is idle and yields instantly when
    the recorder needs CPU.
    """
    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/recorder.db")
    ap.add_argument("--out", default="out/dashboard.html")
    ap.add_argument("--balance", type=float,
                    default=cfgv("STARTING_CAPITAL", 100.0))
    ap.add_argument("--contracts", type=int,
                    default=cfgv("CONTRACTS_PER_TRADE", 3))
    ap.add_argument("--sigmas", type=float, default=cfgv("GAP_SIGMAS", 3.0))
    ap.add_argument("--tau-min", type=float, default=cfgv("TAU_MIN_SECONDS", 90))
    ap.add_argument("--tau-max", type=float, default=cfgv("TAU_MAX_SECONDS", 240))
    ap.add_argument("--min-ask", type=float, default=cfgv("MIN_ASK_CENTS", 99))
    ap.add_argument("--gap-min-abs", type=float,
                    default=cfgv("GAP_MIN_ABS_USD", 25))
    ap.add_argument("--recent-move-max", type=float,
                    default=cfgv("RECENT_MOVE_MAX_USD", 30.0),
                    help="max $ move in the recent window and still enter")
    ap.add_argument("--recent-move-window", type=float,
                    default=cfgv("RECENT_MOVE_WINDOW_SECONDS", 10),
                    help="seconds for the recent-move check")
    ap.add_argument("--min-depth", type=float, default=None,
                    help="required resting size. Defaults to --contracts.")
    ap.add_argument("--open", action="store_true", help="open in a browser")
    a = ap.parse_args()
    _be_polite()

    if not os.path.exists(a.db):
        raise SystemExit(f"No database at {a.db}")

    params = {
        "balance": a.balance, "contracts": a.contracts, "sigmas": a.sigmas,
        "recent_move_max": a.recent_move_max,
        "recent_move_window": a.recent_move_window,
        "min_depth": (a.min_depth if a.min_depth is not None else a.contracts),
        "tau_min": a.tau_min, "tau_max": a.tau_max, "min_ask": a.min_ask,
        "gap_min_abs": a.gap_min_abs,
        "daily_limit": cfgv("DAILY_LOSS_LIMIT_DOLLARS", 10.0),
        "cooldown_losses": cfgv("COOLDOWN_AFTER_CONSECUTIVE_LOSSES", 2),
        "cooldown_min": cfgv("COOLDOWN_MINUTES", 60),
        "cooldown_esc": cfgv("COOLDOWN_ESCALATION", 2.0),
        "cooldown_max": cfgv("COOLDOWN_MAX_MINUTES", 240),
    }

    print(f"simulating over {a.db} ...")
    trades, equity, start = simulate(a.db, params)

    con = sqlite3.connect(a.db)
    span = (con.execute("SELECT (MAX(ts)-MIN(ts))/86400.0 FROM ticks")
            .fetchone()[0] or 0.01)
    con.close()

    if not trades:
        print("\nNo trades were generated.")
        print("  Either no settled events yet (run backfill_settlements.py),")
        print("  or the gates never passed. Check: python3 scripts/audit.py")
        return

    stats = compute_stats(trades, equity, start, params)
    html = render(trades, equity, start, stats, params, a.db, span)

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w") as f:
        f.write(html)

    print(f"\n  trades      {stats['trades']}")
    print(f"  wins/losses {stats['wins']}/{stats['losses']}")
    print(f"  win rate    {stats['win_rate']*100:.2f}%  "
         f"(95% CI {stats['ci_lo']*100:.1f}-{stats['ci_hi']*100:.1f}%)")
    if stats["breakeven"]:
        print(f"  break-even  {stats['breakeven']*100:.2f}%")
    print(f"  net P&L     ${stats['pnl']:+,.2f}")
    print(f"  balance     ${start:,.2f} -> ${stats['end_balance']:,.2f}")
    print(f"  max DD      ${stats['max_dd']:,.2f} ({stats['max_dd_pct']:.1f}%)")
    print(f"\nwrote {a.out}")

    if a.open:
        webbrowser.open("file://" + os.path.abspath(a.out))


if __name__ == "__main__":
    main()