DAILY_TARGET         = 200.0
# Kill switch. 5 positions x $20 stop = $100 theoretical worst case; -200 leaves a
# ~$100 cushion for stop slippage / gap-throughs before the day is force-halted
# (a middle ground: tighter than the old -300, looser than -140).
DAILY_LOSS_LIMIT     = -200.0
PER_POSITION_STOP    = -20.0
MAX_POSITIONS        = 5
PER_POSITION_TARGET  = 20.0

# Stepped trailing stop: list of (trigger_pl, new_stop_pl)
# Once unrealized P&L crosses trigger, the stop order is moved to new_stop_pl above
# entry. Each position is sized to ~$2000 notional (position_size buys int($2000/price)
# shares), so these dollar levels are fixed PERCENT moves: +$20 target == +1.0%.
#
# The first rung waits for +$10 (~0.5%) instead of the old +$5 (~0.25%): a quarter-
# percent fires inside ordinary first-minute noise on gappers, scratching winners
# (ARM/OXM/IBKR all locked breakeven then stopped out for a small loss on 2026-06-12).
# And no rung locks at exactly $0: a triggered stop fills at MARKET and slips below
# its price, so a breakeven lock turns negative. Locking a few dollars above entry
# covers typical (liquid-name) slippage; very thin names can still slip more, but
# starting the lock in profit is strictly better than at zero.
STOP_STEPS = [
    (10, 3),   # up $10 (~0.5%)  → lock +$3  (above entry, absorbs typical slippage)
    (15, 8),   # up $15 (~0.75%) → lock +$8
    (18, 13),  # up $18 (~0.9%)  → lock +$13
]
