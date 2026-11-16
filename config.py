"""
config.py -- every tunable number in one place. Edit, save, rerun. No other
file needs to change for day-to-day iteration.

Values marked UNVALIDATED are your own eyeballed heuristics, not measured
from data. They're starting points, not truth -- tighten or loosen them as
today's live_trades.csv and the recorder's data come in.

Values marked OBSERVED are set from what you actually did today, which is
better evidence than anything invented.
"""

# ============================== CAPITAL ==================================
# Fixed dollars, not a percentage of anything. Change this number directly
# when you want to size up or down -- nothing else in the bot derives from
# a percentage of balance.

STARTING_CAPITAL = 500.00

# Contracts per trade.
#
# CRITICAL -- Kalshi rounds fees UP to the next cent, PER ORDER. At 99c the
# raw fee is 0.0693c/contract, so a 1-contract order pays a full 1c fee --
# which is exactly your entire 1c profit. N=1 at 99c wins NOTHING and loses
# 100c. It is not "safe", it is unwinnable.
#
#   C=1  -> net win  0c, net loss 100c   IMPOSSIBLE
#   C=10 -> net win  9c, net loss 991c   breakeven 99.10%
#   C=14 -> net win 13c, net loss 1387c  breakeven 99.07%  <- efficient min
#   C=28 -> net win 26c, net loss 2786c  breakeven 99.07%
#   C=100-> net win 93c, net loss 9907c  breakeven 99.07%
#
# Sweet spots sit just under a fee-rounding boundary: 14, 28, 43, 57, 72, 86, 100.
# Your real per-trade risk is ~$0.99 x C. At C=14 a loss costs $13.87.
CONTRACTS_PER_TRADE = 14

# Halt for the rest of the UTC day once realized losses reach this many
# dollars. Resets at UTC midnight.
# SIZE THIS AGAINST ONE LOSS, not against your gut. A losing trade costs
# roughly CONTRACTS_PER_TRADE x 99c plus the rounded fee:
#
#     3 contracts  -> $2.98 per loss
#    14 contracts  -> $13.87 per loss
#
# If this limit is smaller than a single loss, the FIRST loss halts the day.
# The bot warns at startup when that is true. Set it deliberately -- a good
# rule of thumb is 2-3 losses' worth.
DAILY_LOSS_LIMIT_DOLLARS = 9.00


# ========================== ENTRY WINDOW ==================================
# tau = seconds remaining until the hour closes.
# OBSERVED today: you were NOT trading at the 10-minute mark. You waited
# until roughly 2-4 minutes remained, then bought once something looked
# certain. That's tau between ~120 and ~240.

TAU_MAX_SECONDS = 240   # don't even look earlier than this (was 180)
TAU_MIN_SECONDS = 90    # don't enter this close to the buzzer -- leaves
                         # room to actually manage/exit the position


# ============================== PRICE ======================================
# Kalshi quotes contracts in WHOLE CENTS, 1 through 99. There is no 99.5c or
# 99.99c -- 99c is the highest price that exists, and 100c would mean the
# market is already resolved. So "99c to 99.99c" is exactly one price: 99.
#
# At 99c: risk 99.07c (incl. fee) to make 0.93c. Break-even = 99.07%.
# Set both to 98 if you ever want to include 98c fills, which have a lower
# break-even (98.14%) and roughly double the profit per trade.

MIN_ASK_CENTS = 99

# No upper bound: set to None to accept any ask at or above MIN_ASK_CENTS.
# In practice Kalshi quotes 1-99, and 100c is auto-rejected by the bot because
# paying 100c for something worth 100c is a guaranteed loss after fees.
MAX_ASK_CENTS = None


# ========================= MARKED STRIKE ===================================
# The strike Kalshi would label "Current:" (nearest strike below spot) is
# excluded until this close to the end -- it's the one strike the market
# hasn't finished pricing yet.

MARKED_STRIKE_TAU_SECONDS = 90


# ===================== SAFETY MARGIN (the gap requirement) =================
# The gap required between BTC spot and the strike, expressed in STANDARD
# DEVIATIONS of expected remaining movement. This replaces the old
# "gap_k x 5min range" rule, which was accidentally demanding 7-12 sigma and
# therefore almost never fired.
#
#   sigma_5min   = rv5 / 1.67                  (Parkinson range estimator)
#   sigma_tau    = sigma_5min * sqrt(tau/300)  (scale to time remaining)
#   required_gap = max(GAP_MIN_ABS_USD, GAP_SIGMAS * sigma_tau)
#
# Breach odds under a normal distribution (BTC's fat tails make these
# meaningfully worse in reality -- assume 3-5x):
#     2.5 sigma -> 1 in 161      too thin once tails are considered
#     3.0 sigma -> 1 in 741      DEFAULT: clears the 1-in-107 bar with room
#     3.5 sigma -> 1 in 4,299    very safe, fires much less often
#     4.0 sigma -> 1 in 31,574   effectively never trades
#
# You need better than 1-in-107 (the 99c break-even). Lower = more trades.

GAP_SIGMAS = 3.0
GAP_MIN_ABS_USD = 25       # hard dollar floor regardless of how calm it is


# ================= REGIME-AWARE OVERRIDES (time of day / week) ==============
# BTC behaves differently by session and weekday vs weekend, so one number is
# not one-size-fits-all. Any (day_type, hour_block) here overrides GAP_SIGMAS;
# anything unlisted falls back to it.
#
#   day key:  "mon".."sun"          a SPECIFIC weekday  (highest precedence)
#             "weekday"/"weekend"   Mon-Fri / Sat-Sun
#             "all"                 any day             (lowest precedence)
#   hour block: (start_inclusive, end_exclusive) in UTC
#
# UNVALIDATED SEEDS based on general BTC liquidity: deep books during the
# US/Europe overlap, thin overnight and weekends. Thinner liquidity -> demand
# more sigma. tune_parameters.py prints a MEASURED replacement for this block
# once you have enough settled events. Replace these; don't trust them.

REGIME_GAP_SIGMAS = {
    ("weekday", (13, 17)): 2.8,   # US/Europe overlap -- deepest books
    ("weekday", (17, 21)): 3.0,   # US afternoon
    ("weekday", (6, 13)):  3.0,   # Europe morning
    ("weekday", (21, 24)): 3.3,   # US evening, thinning
    ("weekday", (0, 6)):   3.3,   # Asia overnight -- thinnest weekday
    ("weekend", (0, 24)):  3.5,   # weekends thin all day
}

USE_REGIME_OVERRIDES = True


# ============ FULL PER-REGIME PARAMETER TABLE (optional, advanced) ==========
# REGIME_GAP_SIGMAS above overrides ONLY the sigma requirement. This table
# overrides ANY parameter per regime, and takes precedence over it.
#
# Key:   (day_type, (start_hour, end_hour))   day_type = "weekday"|"weekend"
# Value: dict of any of:
#          "sigmas"     -> GAP_SIGMAS
#          "tau_min"    -> TAU_MIN_SECONDS
#          "tau_max"    -> TAU_MAX_SECONDS
#          "min_depth"  -> MIN_DEPTH_CONTRACTS
#          "contracts"  -> CONTRACTS_PER_TRADE
#          "enabled"    -> False to skip that regime entirely
#
# Anything not specified falls back to the global value. Example of the shape
# (commented out -- these are NOT recommendations):
#
# REGIME_PARAMS = {
#     ("weekday", (13, 17)): {"sigmas": 2.8, "tau_min": 60,  "contracts": 14},
#     ("weekday", (0, 6)):   {"sigmas": 3.5, "tau_min": 120, "contracts": 3},
#     ("weekend", (0, 24)):  {"enabled": False},   # don't trade weekends
# }
#
# tune_parameters.py prints a MEASURED version of this block once enough
# settled events exist per regime. Start empty and let data fill it in --
# hand-written values here are guesses wearing a precise-looking costume.

REGIME_PARAMS = {}

# DAY-OF-WEEK + TIME-OF-DAY EXAMPLES (all commented; these are shapes, not
# recommendations). Keys resolve most-specific-first, so a "fri" block beats
# a "weekday" block on a Friday and neither needs to know about the other:
#
# REGIME_PARAMS = {
#     # --- specific days ---
#     ("fri", (17, 24)):     {"sigmas": 3.6, "contracts": 3},
#                            # Friday close: thinning into the weekend
#     ("mon", (0, 6)):       {"enabled": False},
#                            # Monday pre-dawn: weekend repositioning
#     ("sun", (0, 24)):      {"enabled": False},
#                            # skip Sundays entirely
#
#     # --- whole weekday / weekend ---
#     ("weekday", (13, 17)): {"sigmas": 2.8, "tau_min": 60, "contracts": 14},
#                            # US/Europe overlap: deepest books
#     ("weekday", (0, 6)):   {"sigmas": 3.5, "contracts": 3},
#                            # Asia overnight: thinnest weekday
#     ("weekend", (0, 24)):  {"sigmas": 3.6, "contracts": 3},
#
#     # --- catch-all ---
#     ("all", (0, 24)):      {"min_depth": 5},
# }
#
# Hours are in REGIME_TIMEZONE (below), NOT UTC, and blocks are
# [start, end) -- (13, 17) covers 13:00:00 through 16:59:59.

REGIME_HOUR_GRANULARITY = 4

# ---- TIMEZONE for all regime blocks ----
# Kalshi labels its hourly BTC events in EASTERN TIME (verified: ticker
# ...26AUG1319 closes 2026-08-13T23:00:00Z = 19:00 EDT). Eastern Time
# observes DST, so the same ET hour is 23:00 UTC in August and 00:00 UTC in
# December. Regime blocks written in UTC therefore drift by one hour twice a
# year, silently pointing at a different session than they were fitted to.
#
# BTC liquidity is driven by US session hours (ETF flows, US desks, Kalshi's
# own user base), which follow ET. So regimes are defined in ET and DST is
# handled automatically.
#
# The bot, tune_parameters.py and audit.py ALL read this value, so tuning and
# execution always bucket time the same way. Changing it invalidates any
# previously tuned REGIME_* values -- re-run the tuner after changing it.
#
# Set to "UTC" for fixed UTC hours with no DST adjustment.
REGIME_TIMEZONE = "America/New_York"


# ============================ SPEED GATES ===================================
# Two separate checks, because you described two separate things:

# (1) FORWARD-PROJECTED: "at this rate, would price even reach my strike
#     before time runs out?" -- gap divided by current adverse drift rate,
#     compared against tau times this safety multiplier.
TTS_SAFETY_MULTIPLIER = 3.0

# (2) RAW "is it moving too fast right now" -- literally your eyeballing:
#     "if it's $10 in 10 seconds, I wait." You said this specific number
#     was made up, so it starts loose (permissive) rather than tight.
#     Tighten it once you've watched it against a few real hours.
RECENT_MOVE_WINDOW_SECONDS = 10
RECENT_MOVE_MAX_USD = 30.0


# ================================ EXIT ======================================
# You asked for exactly THREE brakes and nothing else:
#   1. a percentage drop in the value of the open position   (below)
#   2. a cooldown after 2 consecutive losses                 (next section)
#   3. a fixed daily dollar loss cap                         (CAPITAL section)
# Everything else is off by default.

# ---- 1. POSITION-VALUE STOP ----
#   cost_basis   = contracts x entry_price
#   current_val  = contracts x current_bid
#   drawdown_pct = (cost_basis - current_val) / cost_basis
#   EXIT when drawdown_pct >= STOP_LOSS_PCT
#
# Dollar value at 99c entry (the bot prints this at startup for YOUR size):
#   contracts   10% stop fires at   loss incl. both fees
#        3            bid <= 89c          ~$0.32
#       14            bid <= 89c          ~$1.42
#       28            bid <= 89c          ~$2.83
#
# GRANULARITY: Kalshi quotes WHOLE CENTS, so this percentage is really a
# cent-selector. At a 99c entry each 1c step is ~1.01%, so 0.095 and 0.100
# both exit at 89c -- nothing between those values behaves differently. If
# you would rather think in dollars, set STOP_LOSS_DOLLARS below.
STOP_LOSS_PCT = 0.10

# Optional absolute stop, in DOLLARS of position value lost. When set (not
# None) this TAKES PRECEDENCE over STOP_LOSS_PCT. Often the more natural way
# to express the rule, because it is the number you actually care about.
#   e.g. STOP_LOSS_DOLLARS = 0.50 -> exit once the position is down 50c,
#        whatever contract count that corresponds to.
STOP_LOSS_DOLLARS = None

# Check the stop this often while holding (see POSITION_POLL_SECONDS).
STOP_CHECK_SECONDS = 1.0

# ---- OPTIONAL: spot-based exit (OFF by default, per your instruction) ----
# Fires when BTC spot moves back toward your strike far enough that
# gap / required_gap falls below EXIT_SAFETY_RATIO -- regardless of what the
# contract is bid at.
#
# It is OFF because you asked for only the three brakes above. Be aware of
# what that trades away: this exit watches the UNDERLYING, which is deep and
# instant, while the position-value stop watches the Kalshi book, which is
# thin and can gap straight past your stop price with no bid to sell into.
# In a fast breach this is usually the one that would have gotten you out.
#
# Set True to re-enable if you later decide the protection is worth the
# occasional early exit.
USE_SAFETY_RATIO_EXIT = False
EXIT_SAFETY_RATIO = 0.4


# ============================ LOSS COOLDOWN =================================
# Brake 2 of 3: after this many consecutive losses, stop trading for
# COOLDOWN_MINUTES. A WIN RESETS THE COUNTER TO ZERO.
#
# "Consecutive" means consecutive TRADES, not consecutive hours -- two losses
# separated by six untraded hours still trigger it. That is deliberate:
# losses on this strategy cluster because they are caused by BTC regime
# shifts, and the shift does not care whether you happened to trade the
# hours in between.
COOLDOWN_AFTER_CONSECUTIVE_LOSSES = 2
COOLDOWN_MINUTES = 180          # 3 hours

# Escalation is DISABLED (multiplier 1.0) so the pause is always exactly
# COOLDOWN_MINUTES, per your spec. Raise above 1.0 to lengthen each repeat.
COOLDOWN_ESCALATION = 1.0
COOLDOWN_MAX_MINUTES = 180

# No pause after a single isolated loss -- only the 2-in-a-row rule fires.
COOLDOWN_AFTER_ANY_LOSS_MINUTES = 0


# ========================= FILL RETRY BEHAVIOUR =============================
# If the gates say GO but the order doesn't fill (no liquidity at 99c, or an
# IOC came back with zero fills), keep retrying once per second until either
# we fill, the gates stop passing, or the event closes.
RETRY_UNTIL_FILL = True
RETRY_INTERVAL_SECONDS = 1.0

# Stop retrying this many seconds before close.
#
# IMPORTANT -- this is a FLOOR, not the whole story. Every retry attempt
# re-validates all entry gates, including the tau window, so the bot will not
# fill below TAU_MIN_SECONDS regardless of what you set here. The effective
# retry deadline is:
#
#     max(RETRY_STOP_BEFORE_CLOSE, effective TAU_MIN_SECONDS)
#
# With TAU_MIN_SECONDS = 90 the retry window is [90, tau_max] and this value
# does nothing. It only binds if you lower TAU_MIN_SECONDS below it. The bot
# prints the resolved floor when it gives up, so the real number is visible.
RETRY_STOP_BEFORE_CLOSE = 10

# Accept a partial fill and manage it, rather than requiring the full size.
ACCEPT_PARTIAL_FILLS = True


# ============================ DEPTH GATE ====================================
# Minimum contracts resting at the ask before the bot will fire. Set to None
# to default to CONTRACTS_PER_TRADE (i.e. only trade when the full order can
# fill). Lower it if you would rather take partial fills than skip the hour.
MIN_DEPTH_CONTRACTS = None


# ========================== RECORDING FILTER ================================
# Storage control. Without a filter the recorder writes all ~188 strikes every
# second: 188 x 900s x 24h = 4.06M rows/day = ~1 GB/day. The overwhelming
# majority are useless -- far-OTM strikes parked at 1c, and deep-ITM strikes
# reporting ask=100 (which means NO OFFER, not a price).
#
# A row is KEPT if EITHER condition holds:
#   * it has a real offer at or above RECORD_MIN_ASK_CENTS (and below 100), OR
#   * its strike is within RECORD_GAP_WINDOW_USD of spot (near-money context)
#
# This preserves everything the strategy and the tuner actually read -- the
# 99c band plus surrounding context -- while dropping dead strikes. Expect
# roughly a 10-20x reduction (~50-150 MB/day).
#
# Set RECORD_ALL = True to disable filtering and record everything.
RECORD_ALL = False
RECORD_MIN_ASK_CENTS = 90     # keep any real offer at/above this
RECORD_GAP_WINDOW_USD = 1500  # keep strikes within this many $ of spot


# ============================== POLLING =====================================
# How often to re-evaluate entry gates while hunting for a trade.
POLL_SECONDS = 1.0

# How often to re-check an OPEN position (stop loss, z-exit). Separate from
# POLL_SECONDS because it is far more time-critical: it decides how fast you
# notice a position going wrong.
#
#   1.0s -> average 500ms to detect a breach
#   0.5s -> average 250ms
#
# 0.5s costs 2 API reads/second against a ~20/s limit, so it is cheap. It is
# NOT a magic number: the real limit on a stop is whether a bid exists at
# your stop price, not how fast you notice. Polling faster narrows detection
# latency; it cannot conjure liquidity. See POSITION_POLL note in the docs.
POSITION_POLL_SECONDS = 0.5


# ====================== REMOTE OPERATION SAFETY =============================
# The bot runs on a remote box (AWS Lightsail), so a detached tmux session or
# a dropped SSH connection must never leave a position unmanaged waiting for
# a keypress that never comes.
#
# In confirm mode, prompts time out after this many seconds. Failure defaults
# are deliberately asymmetric:
#     entry prompt times out -> NO   (never open a position unattended)
#     exit  prompt times out -> YES  (always protect capital unattended)
CONFIRM_TIMEOUT_SECONDS = 20

# If you decline an exit, wait this long before asking again rather than
# re-prompting every poll cycle.
DECLINE_COOLDOWN_SECONDS = 15