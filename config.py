# Universe liquidity floor (momentum scan). Added 2026-06-17 after a week of losses
# traced to one cause: the screener had NO price/liquidity floor, so it kept picking
# thin sub-$50 mid/small-caps (CTKB $4, NSP $38, CXT $46, LZB $44) whose -$20 market
# stop slipped to -$24..-$49 on the violent first 5 minutes. On 06-17 those 4 thin
# names lost -$136 while every liquid large-cap (AMKR/MRVL/WDC/ASML) was a clean win;
# excluding them flips the day from -$95 to +$40. A -$20 stop only holds on names with
# a tight (penny) spread and real depth — so require both a price floor and a minimum
# average daily dollar-volume.
MIN_SHARE_PRICE      = 50.0          # skip anything trading below this
MIN_AVG_DOLLAR_VOL   = 50_000_000.0  # skip anything under $50M/day avg dollar-volume

DAILY_TARGET         = 200.0
# Kill switch. At 10 positions, worst case is 10 x $20 stop = $200 theoretical, which
# equals this -200 limit exactly — so there is ~$0 cushion for stop slippage / gap-
# throughs: the day force-halts right at theoretical max loss (user chose to keep -200
# at 10 positions on 2026-06-16 rather than restore the old -300 cushion).
# NOTE: as of 2026-06-30 each loss is also capped near -$28 by a marketable stop-LIMIT
# (STOP_LIMIT_SLIP below), so the realistic worst case is bounded ABOVE the old market-
# stop slippage that drove single losers to -$45..-$113 (06-02/05-20). This -200 limit
# now acts as a true backstop (it trips after ~7 capped stop-outs) rather than firing
# exactly at theoretical max.
DAILY_LOSS_LIMIT     = -200.0
PER_POSITION_STOP    = -20.0
MAX_POSITIONS        = 10

# Reward:risk. THE root cause of the slow daily bleed (analysis 2026-06-30): the system
# won 60-90% of the time yet still lost money because realized losers (-$45..-$113, from
# market-stop slippage) dwarfed winners that were hard-capped at +$20. With losses now
# capped near -$28 by a stop-LIMIT, the target is raised to +$40 so a winner is worth
# ~2x a loser. A high win rate only compounds if the average win >= the average loss;
# this restores that. +$40 on a ~$2000 position is a +2% move; -$20 is -1%.
PER_POSITION_TARGET  = 40.0

# Position sizing is DECOUPLED from the target (was int(TARGET/(price*1%)), which would
# have silently DOUBLED every position to ~$4000 when the target moved 20 -> 40). Size to
# a fixed notional instead so the dollar target/stop stay clean percent moves regardless
# of how the target is tuned. position_size buys int(POSITION_NOTIONAL / price) shares.
POSITION_NOTIONAL    = 2000.0

# Marketable stop-LIMIT slippage cap (dollars, whole-position). The protective stop is a
# stop-LIMIT, not a stop-MARKET: when the stop triggers, the sell is a limit no worse
# than STOP_LIMIT_SLIP below the trigger. A plain stop-market sold at WHATEVER the
# violent first 5 minutes offered, turning a -$20 stop into -$45..-$113 (06-02 worst
# -$68.53, 05-20 worst -$113.63) — that fat left tail is what made a 60%+ win rate
# unprofitable. $8 (~0.4% of a $2000 position) is wide enough that a liquid large-cap
# (we already gate on price + dollar-volume) fills in the normal case, but caps the loss
# near -$28 instead of letting it run. If price gaps clean THROUGH the limit the order
# rests unfilled — the daily loss-limit kill switch and the EOD force-close are the
# backstops for that rare case (never an unbounded overnight gap).
STOP_LIMIT_SLIP      = 8.0

# Stepped trailing stop: list of (trigger_pl, new_stop_pl)
# Once unrealized P&L crosses trigger, the stop order is moved to new_stop_pl above
# entry. Each position is sized to ~$2000 notional (POSITION_NOTIONAL), so these dollar
# levels are fixed PERCENT moves: the +$40 target == +2.0%.
#
# Two real failures shaped this ladder, both on 2026-06-12:
#   * The ORIGINAL [(5,0),(10,5),(15,10)] locked the first rung at exactly breakeven.
#     A triggered stop fills at MARKET and slips below its price, so the breakeven
#     lock turned negative — ARM sold for -$2.65. The fix for THAT was never to delay
#     the trigger; it was to lock a PROFIT big enough to clear slippage.
#   * Over-correcting to a +$10 first rung then gave winners back: ARM popped to +$8.30
#     and round-tripped to a loss because nothing locked below +$10.
# So: keep an EARLY trigger (so a real +$5 pop locks something) but make every lock a
# profit that covers typical (liquid-name) slippage. Rescaled 2026-06-30 to span the new
# +$40 target — the old ladder topped out at +$13, which (with a +$20 target) re-capped
# winners almost as tightly as the bug it replaced. The wider rungs let a real runner
# keep more of a +$22/+$32 move while still locking a profit the moment it reverses.
# The stop-LIMIT (STOP_LIMIT_SLIP) now bounds how far a triggered lock can slip, so a
# locked rung fills close to its level instead of at whatever market offered.
STOP_STEPS = [
    (5,  3),   # up $5  → lock +$3   (early; a small pop still ratchets to a profit)
    (12, 7),   # up $12 → lock +$7
    (22, 15),  # up $22 → lock +$15
    (32, 25),  # up $32 → lock +$25  (a strong runner keeps most of the move)
]
