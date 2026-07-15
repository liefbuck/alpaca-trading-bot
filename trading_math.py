"""
Pure trading-math helpers — no I/O, no network, no broker calls.

Centralised here so bot.py and scan_now.py share ONE implementation (previously
the bracket math was duplicated and could drift), and so the regression suite in
tests/ can exercise every rule deterministically.
"""
from config import (
    PER_POSITION_TARGET, PER_POSITION_STOP, STOP_STEPS, POSITION_NOTIONAL,
)


def position_size(price: float, buying_power: float) -> int:
    """
    Share quantity for one position. `buying_power` is the per-position budget.

    Sizes to a fixed POSITION_NOTIONAL (~$2000), capped by what the per-position
    budget can afford. This is DELIBERATELY independent of PER_POSITION_TARGET:
    keying the size off the target (the old int(TARGET/(price*1%))) silently scaled
    every position with the target, so raising the target 20 -> 40 would have doubled
    notional and risk. With POSITION_NOTIONAL=2000 this returns exactly the same share
    counts the old target=20 formula did, but a target retune no longer moves them.

    Returns at least 1. Guards price <= 0 (would otherwise divide by zero) by
    returning 1 — screener candidates are always > 0, this is pure defence.
    """
    if price <= 0:
        return 1
    shares_for_notional = int(POSITION_NOTIONAL / price)
    max_affordable = int(buying_power / price)
    return max(1, min(shares_for_notional, max_affordable))


def compute_bracket_prices(price: float, qty: int) -> tuple:
    """
    (take_profit_price, stop_price) for a long bracket.

    Each leg is held at least 1 cent away from the base price so Alpaca never
    rejects the order for TP <= base or SL >= base (which could happen on a
    very low-priced / high-qty fill where the raw offset rounds to $0.00).
    Guarantees 0 < stop_price < price < take_profit_price.
    """
    if qty <= 0:
        qty = 1
    tp_offset = max(0.01, round(PER_POSITION_TARGET / qty, 2))
    sl_offset = max(0.01, round(-PER_POSITION_STOP / qty, 2))  # PER_POSITION_STOP is negative
    take_profit_price = round(price + tp_offset, 2)
    stop_price        = round(price - sl_offset, 2)
    # A degenerate low-price / low-qty combo (e.g. a sub-$20 stock sized to 1 share)
    # drives the raw stop to zero or negative, which Alpaca rejects. Not reachable
    # for the S&P/Nasdaq universe (position_size scales qty UP for cheap names, and
    # qty=1 only happens at price >= ~$2000 where the stop stays well positive), but
    # floor it strictly between $0 and price so a config/universe change can never
    # produce an invalid stop and silently kill an entry.
    if stop_price <= 0:
        stop_price = round(price / 2, 2) or round(price / 2, 4)
    return take_profit_price, stop_price


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


def pair_fills_to_trades(fills: list) -> list:
    """
    Rebuild a day's {'symbol','pl'} trade list from raw broker fills.

    Used to RECONSTRUCT a performance row for a day the bot did not survive to
    15:45 (see backfill_performance_history in bot.py) — the live path records
    trades into bot_state.json as they close, but that state is wiped by the next
    daily_reset, so a missed EOD can only be recovered from broker fills.

    `fills` is a list of {'symbol','side','qty','price'} ('side' is 'buy'/'sell').
    P&L per symbol is proceeds - cost, which reproduces the live bracket math
    (exit - entry) * qty exactly, and also handles a partially-filled exit that
    closed across several orders.

    A symbol whose buy and sell quantities do not match is NOT flat — that is an
    open position, not a completed trade — so it is skipped rather than booked at
    a bogus P&L.
    """
    by_symbol: dict = {}
    for f in fills:
        acc = by_symbol.setdefault(f["symbol"], {"buy_qty": 0.0, "cost": 0.0,
                                                 "sell_qty": 0.0, "proceeds": 0.0})
        qty, price = float(f["qty"]), float(f["price"])
        if str(f["side"]).lower().endswith("buy"):
            acc["buy_qty"] += qty
            acc["cost"]    += qty * price
        else:
            acc["sell_qty"] += qty
            acc["proceeds"] += qty * price

    trades = []
    for symbol, acc in sorted(by_symbol.items()):
        if acc["buy_qty"] <= 0 or acc["buy_qty"] != acc["sell_qty"]:
            continue
        trades.append({"symbol": symbol, "pl": round(acc["proceeds"] - acc["cost"], 2)})
    return trades


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
