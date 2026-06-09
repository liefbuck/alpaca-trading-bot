DAILY_TARGET         = 200.0
# Kill switch. 5 positions x $20 stop = $100 theoretical worst case; -200 leaves a
# ~$100 cushion for stop slippage / gap-throughs before the day is force-halted
# (a middle ground: tighter than the old -300, looser than -140).
DAILY_LOSS_LIMIT     = -200.0
PER_POSITION_STOP    = -20.0
MAX_POSITIONS        = 5
PER_POSITION_TARGET  = 20.0

# Stepped trailing stop: list of (trigger_pl, new_stop_pl)
# Once unrealized P&L crosses trigger, stop order is moved to new_stop_pl above entry.
STOP_STEPS = [
    (5,  0),   # up $5  → stop at breakeven ($0)
    (10, 5),   # up $10 → stop at $5
    (15, 10),  # up $15 → stop at $10
]
