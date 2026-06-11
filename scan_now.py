import os
import time
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from screener import get_top_momentum
from config import DAILY_TARGET, DAILY_LOSS_LIMIT, MAX_POSITIONS
from trading_math import position_size, compute_bracket_prices

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

client = TradingClient(
    api_key=os.getenv("ALPACA_API_KEY"),
    secret_key=os.getenv("ALPACA_SECRET_KEY"),
    paper=True,
)

clock = client.get_clock()
if not clock.is_open:
    print(f"Market is closed. Next open: {clock.next_open}")
    exit()

print("Scanning S&P 500 + Nasdaq 100 for momentum plays...\n")
top = get_top_momentum(n=50)

if not top:
    print("No qualifying momentum stocks right now.")
    exit()

picks = top[:MAX_POSITIONS]
print("=== TOP PICKS ===")
for c in picks:
    print(f"  {c['symbol']:6}  ${c['price']}  {c['change_pct']:+.2f}%  relvol {c['rel_vol']}x  score {c['score']}")

acct = client.get_account()
buying_power = float(acct.buying_power)

print("\n=== PLACING ORDERS ===")
print("  NOTE: manual tool. These positions are protected by their OCO TP/SL")
print("  and are force-closed at EOD, but are NOT written to bot_state.json, so the")
print("  running bot will NOT ladder their trailing stops or log them per-trade.\n")


def _wait_for_fill(order_id, timeout=15.0, poll=0.3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            o = client.get_order_by_id(order_id)
        except Exception:
            time.sleep(poll); continue
        status = getattr(o.status, "value", o.status)
        if status == "filled" and o.filled_avg_price and float(o.filled_qty or 0) > 0:
            return float(o.filled_qty), float(o.filled_avg_price)
        if status in ("canceled", "rejected", "expired"):
            return None, None
        time.sleep(poll)
    return None, None


def _recheck_fill(order_id, symbol, settle=1.0):
    """After a timeout+cancel, confirm whether the buy ACTUALLY filled (the cancel
    can race a late fill -> 'canceled' WITH a real fill, or a live position). Mirrors
    bot.py so this manual tool never leaves an unprotected orphan position either."""
    time.sleep(settle)
    try:
        o = client.get_order_by_id(order_id)
        if o.filled_avg_price and float(o.filled_qty or 0) > 0:
            return float(o.filled_qty), float(o.filled_avg_price)
    except Exception:
        pass
    try:
        pos = client.get_open_position(symbol)
        q = float(getattr(pos, "qty", 0) or 0)
        p = float(getattr(pos, "avg_entry_price", 0) or 0)
        if q > 0 and p > 0:
            return q, p
    except Exception:
        pass
    return None, None


for stock in picks:
    price = stock["price"]
    qty = position_size(price, buying_power / MAX_POSITIONS)
    # Two-step entry: bare market buy, then an OCO exit priced off the REAL fill —
    # same approach as the bot. A single BRACKET order priced off a pre-trade quote
    # gets rejected when Alpaca's base_price disagrees with it (TP/SL outside the
    # 1-cent tolerance), which silently dropped positions in the morning scan.
    try:
        buy = client.submit_order(MarketOrderRequest(
            symbol=stock["symbol"], qty=qty, side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        ))
    except Exception as e:
        print(f"  FAILED {stock['symbol']} (buy): {e}")
        continue
    fill_qty, fill_price = _wait_for_fill(buy.id)
    if not fill_price:
        # Timed out: cancel, then RE-CHECK -- the cancel can race a late fill, which
        # would otherwise leave an unprotected orphan. Recover it instead of dropping.
        try: client.cancel_order_by_id(buy.id)
        except Exception: pass
        fill_qty, fill_price = _recheck_fill(buy.id, stock["symbol"])
        if not fill_price:
            print(f"  FAILED {stock['symbol']}: buy did not fill (confirmed flat) — skipping")
            try: client.close_position(stock["symbol"])
            except Exception: pass
            continue
        print(f"  RECOVERED {stock['symbol']}: filled despite timeout — protecting it")
    take_profit_price, stop_price = compute_bracket_prices(fill_price, fill_qty)
    try:
        client.submit_order(LimitOrderRequest(
            symbol=stock["symbol"], qty=int(fill_qty), side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY, order_class=OrderClass.OCO,
            limit_price=take_profit_price,
            take_profit=TakeProfitRequest(limit_price=take_profit_price),
            stop_loss=StopLossRequest(stop_price=stop_price),
        ))
        print(f"  BOUGHT {int(fill_qty)}x {stock['symbol']} @ ${fill_price:.2f}  | TP ${take_profit_price} SL ${stop_price}")
    except Exception as e:
        print(f"  FAILED {stock['symbol']} (OCO exit rejected) — closing position: {e}")
        try: client.close_position(stock["symbol"])
        except Exception as ce: print(f"    FAILED to close {stock['symbol']}: {ce}")

print(f"\nDone. Target +${DAILY_TARGET:.0f} | Loss limit ${DAILY_LOSS_LIMIT:.0f}")
