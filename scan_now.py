import os
import yfinance as yf
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

client = TradingClient(
    api_key=os.getenv("ALPACA_API_KEY"),
    secret_key=os.getenv("ALPACA_SECRET_KEY"),
    paper=True,
)

WATCHLIST = [
    "AAPL","MSFT","NVDA","TSLA","AMZN","META","GOOGL","AMD",
    "NFLX","COIN","PLTR","SOFI","MARA","RIOT","ROKU","SNAP",
    "UBER","LYFT","SHOP","SQ","SMCI","ARM","IONQ","RKLB","HOOD"
]

print("Scanning for momentum plays...\n")
candidates = []
for symbol in WATCHLIST:
    try:
        t = yf.Ticker(symbol)
        hist = t.history(period="2d", interval="1d")
        if len(hist) < 2:
            continue
        prev_close = float(hist["Close"].iloc[-2])
        info = t.fast_info
        current = getattr(info, "last_price", None)
        if not current or not prev_close:
            continue
        change_pct = (current - prev_close) / prev_close * 100
        avg_vol = getattr(info, "three_month_average_volume", 1) or 1
        last_vol = float(hist["Volume"].iloc[-1])
        rel_vol = last_vol / avg_vol
        score = change_pct * rel_vol
        candidates.append({"symbol": symbol, "price": round(current, 2), "change_pct": round(change_pct, 2), "rel_vol": round(rel_vol, 2), "score": round(score, 3)})
        arrow = "^" if change_pct > 0 else "v"
        print(f"  {arrow} {symbol:6}  ${current:.2f}  {change_pct:+.2f}%  relvol {rel_vol:.1f}x")
    except Exception as e:
        print(f"  ? {symbol}: {e}")

candidates.sort(key=lambda x: x["score"], reverse=True)
top3 = [c for c in candidates if c["change_pct"] > 0][:3]

print("\n=== TOP PICKS ===")
for c in top3:
    print(f"  {c['symbol']:6}  ${c['price']}  {c['change_pct']:+.2f}%  relvol {c['rel_vol']}x  score {c['score']}")

if not top3:
    print("No qualifying momentum stocks right now (all negative or flat).")
    exit()

# Size positions: aim for $200 / 3 = ~$67 profit each on a ~1% move
acct = client.get_account()
buying_power = float(acct.buying_power)
DAILY_TARGET = 200
MAX_POSITIONS = 5

print("\n=== PLACING ORDERS ===")
for stock in top3:
    target_per_pos = DAILY_TARGET / MAX_POSITIONS
    shares_for_target = int(target_per_pos / (stock["price"] * 0.01))
    max_affordable = int((buying_power / MAX_POSITIONS) / stock["price"])
    qty = max(1, min(shares_for_target, max_affordable))
    try:
        order = client.submit_order(MarketOrderRequest(
            symbol=stock["symbol"],
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        ))
        print(f"  BOUGHT {qty}x {stock['symbol']} @ ~${stock['price']}  (order {str(order.id)[:8]}...)")
    except Exception as e:
        print(f"  FAILED {stock['symbol']}: {e}")

print("\nDone. Watching for +$200 daily target or -$100 loss limit.")
