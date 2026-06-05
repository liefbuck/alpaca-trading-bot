import os
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from screener import get_top_momentum
from config import (
    DAILY_TARGET, DAILY_LOSS_LIMIT, MAX_POSITIONS, PER_POSITION_TARGET,
    PER_POSITION_STOP,
)

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
for stock in picks:
    price             = stock["price"]
    shares_for_target = int(PER_POSITION_TARGET / (price * 0.01))
    max_affordable    = int((buying_power / MAX_POSITIONS) / price)
    qty = max(1, min(shares_for_target, max_affordable))
    # Same protective bracket the bot uses, with a 1-cent minimum leg distance so
    # the TP/SL never round to the base price (which Alpaca rejects).
    tp_offset = max(0.01, round(PER_POSITION_TARGET / qty, 2))
    sl_offset = max(0.01, round(-PER_POSITION_STOP / qty, 2))
    take_profit_price = round(price + tp_offset, 2)
    stop_price        = round(price - sl_offset, 2)
    try:
        order = client.submit_order(MarketOrderRequest(
            symbol=stock["symbol"],
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=take_profit_price),
            stop_loss=StopLossRequest(stop_price=stop_price),
        ))
        print(f"  BOUGHT {qty}x {stock['symbol']} @ ~${price}  | TP ${take_profit_price} SL ${stop_price}  (order {str(order.id)[:8]}...)")
    except Exception as e:
        print(f"  FAILED {stock['symbol']}: {e}")

print(f"\nDone. Target +${DAILY_TARGET:.0f} | Loss limit ${DAILY_LOSS_LIMIT:.0f}")
