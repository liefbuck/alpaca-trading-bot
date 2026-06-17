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
DAILY_LOSS_LIMIT     = -200.0
PER_POSITION_STOP    = -20.0
MAX_POSITIONS        = 10
PER_POSITION_TARGET  = 20.0

# Stepped trailing stop: list of (trigger_pl, new_stop_pl)
# Once unrealized P&L crosses trigger, the stop order is moved to new_stop_pl above
# entry. Each position is sized to ~$2000 notional (position_size buys int($2000/price)
# shares), so these dollar levels are fixed PERCENT moves: +$20 target == +1.0%.
#
# Two real failures shaped this ladder, both on 2026-06-12:
#   * The ORIGINAL [(5,0),(10,5),(15,10)] locked the first rung at exactly breakeven.
#     A triggered stop fills at MARKET and slips below its price, so the breakeven
#     lock turned negative — ARM sold for -$2.65. The fix for THAT was never to delay
#     the trigger; it was to lock a PROFIT big enough to clear slippage.
#   * Over-correcting to a +$10 first rung then gave winners back: ARM popped to +$8.30
#     and round-tripped to a loss because nothing locked below +$10.
# So: keep an EARLY trigger (so a real +$5–8 pop locks something) but make every lock
# a profit that covers typical (liquid-name) stop slippage (~$2–3). Now a pop that
# reverses exits for a small GAIN instead of giving it all back or scratching a loss.
# (Very thin names can still slip past a $3 buffer — that's an IEX/market-order issue,
# not the ladder's; see the SIP-feed / marketable-limit note in the open-feed memory.)
STOP_STEPS = [
    (5,  3),   # up $5  → lock +$3   (early; the lock clears typical slippage)
    (8,  5),   # up $8  → lock +$5
    (12, 9),   # up $12 → lock +$9
    (16, 13),  # up $16 → lock +$13
]
