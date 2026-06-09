"""
Pure trading-math helpers — no I/O, no network, no broker calls.

Centralised here so bot.py and scan_now.py share ONE implementation (previously
the bracket math was duplicated and could drift), and so the regression suite in
tests/ can exercise every rule deterministically.
"""
from config import PER_POSITION_TARGET, PER_POSITION_STOP, STOP_STEPS


def position_size(price: float, buying_power: float) -> int:
    """
    Share quantity for one position. `buying_power` is the per-position budget.

    Returns at least 1. Guards price <= 0 (would otherwise divide by zero) by
    returning 1 — screener candidates are always > 0, this is pure defence.
    """
    if price <= 0:
        return 1
    shares_for_target = int(PER_POSITION_TARGET / (price * 0.01))
    max_affordable = int(buying_power / price)
    return max(1, min(shares_for_target, max_affordable))


def compute_bracket_prices(price: float, qty: int) -> tuple:
    """
    (take_profit_price, stop_price) for a long bracket.

    Each leg is held at least 1 cent away from the base price so Alpaca never
    rejects the order for TP <= base or SL >= base (which could happen on a
    very low-priced / high-qty fill where the raw offset rounds to $0.00).
    Guarantees stop_price < price < take_profit_price.
    """
    if qty <= 0:
        qty = 1
    tp_offset = max(0.01, round(PER_POSITION_TARGET / qty, 2))
    sl_offset = max(0.01, round(-PER_POSITION_STOP / qty, 2))  # PER_POSITION_STOP is negative
    return round(price + tp_offset, 2), round(price - sl_offset, 2)


def select_stop_pl(pl: float, current_step: float, steps=None):
    """
    Highest stepped-stop level earned by unrealized P&L `pl` that is strictly
    above `current_step` (the level already locked in). Returns the new stop_pl
    (dollars above entry) or None if no higher step is earned yet.
    """
    # Default to the configured ladder. Bound at call time (not as a default arg)
    # to avoid the mutable-default-argument gotcha.
    if steps is None:
        steps = STOP_STEPS
    for trigger, stop_pl in sorted(steps, reverse=True):
        if pl >= trigger and stop_pl > current_step:
            return stop_pl
    return None


def classify_trades(trades: list, daily_pnl: float) -> dict:
    """
    Aggregate a list of {'symbol','pl'} trades into the daily performance record.
    Break-even ($0.00) trades count as wins, and a break-even *day* is a WIN.
    """
    wins   = [t for t in trades if t["pl"] >= 0]
    losses = [t for t in trades if t["pl"] < 0]
    return {
        "trades":      len(trades),
        "wins":        len(wins),
        "losses":      len(losses),
        "win_rate":    round(len(wins) / len(trades) * 100, 1) if trades else 0,
        "best_trade":  round(max((t["pl"] for t in trades), default=0), 2),
        "worst_trade": round(min((t["pl"] for t in trades), default=0), 2),
        "result":      "WIN" if daily_pnl >= 0 else "LOSS",
    }
