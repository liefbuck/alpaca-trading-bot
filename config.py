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

# Per-position HARD stop (safety net). The protective exit is a stop-MARKET order, which
# always fills — but a server-side stop can still, rarely, fail to flatten (a partial
# fill on a fast gap, an order that never triggers). If that happens the position rides
# unprotected past its -$20 stop. So check_pnl independently force-closes ANY held
# position whose unrealized P&L falls to this level, guaranteeing an exit no matter what
# the broker order does. Set well BELOW the -$20 stop so it only fires on a genuine stop
# FAILURE, never on a normal stop fill.
#
# THIS REPLACES the 2026-06-30 stop-LIMIT experiment, which backfired live the same day:
# a stop-LIMIT caps slippage but DOESN'T FILL when a volatile name gaps through the limit
# — ALGT and RRX rode to -$37 and -$44 (vs the -$20 stop) with their limit sells stuck
# unfilled BELOW the market, and AVAV round-tripped +$21 -> +$0.11. On a momentum name a
# guaranteed exit (stop-MARKET) beats a capped-but-maybe-unfilled one. Catastrophic
# market-stop slippage (-$113, 05-20) was a THIN-name problem already fixed by the
# liquidity floor above; on the $50+/$50M-a-day universe a market stop slips only a few $.
PER_POSITION_HARD_STOP = -30.0

# Reward:risk. THE root cause of the slow daily bleed (analysis 2026-06-30): the system
# won 60-90% of the time yet still lost money because realized losers (-$45..-$113, from
# thin-name market-stop slippage, now gated out by the liquidity floor) dwarfed winners
# hard-capped at +$20. The target is raised to +$40 so a winner is worth ~2x a loser. A
# high win rate only compounds if the average win >= the average loss; this restores it.
# +$40 on a ~$2000 position is a +2% move; -$20 is -1%.
PER_POSITION_TARGET  = 40.0

# Position sizing is DECOUPLED from the target (was int(TARGET/(price*1%)), which would
# have silently DOUBLED every position to ~$4000 when the target moved 20 -> 40). Size to
# a fixed notional instead so the dollar target/stop stay clean percent moves regardless
# of how the target is tuned. position_size buys int(POSITION_NOTIONAL / price) shares.
POSITION_NOTIONAL    = 2000.0

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
# Each lock keeps a few $ of cushion so a triggered MARKET stop's slippage can't flip a
# locked-profit exit to a loss (the original reason locks are never set at breakeven).
STOP_STEPS = [
    (5,  3),   # up $5  → lock +$3   (early; a small pop still ratchets to a profit)
    (12, 7),   # up $12 → lock +$7
    (22, 15),  # up $22 → lock +$15
    (32, 25),  # up $32 → lock +$25  (a strong runner keeps most of the move)
]
