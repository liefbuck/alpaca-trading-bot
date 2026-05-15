import os
import json
import time
import schedule
from datetime import datetime
import pytz
import logging
from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from screener import get_top_momentum

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")
DAILY_TARGET = 200.0
DAILY_LOSS_LIMIT = -100.0
MAX_POSITIONS = 5
STATE_FILE = "bot_state.json"

client = TradingClient(
    api_key=os.getenv("ALPACA_API_KEY"),
    secret_key=os.getenv("ALPACA_SECRET_KEY"),
    paper=True,
)

trading_active = True


def save_state(data: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(data, f)


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def get_daily_pnl() -> float:
    acct = client.get_account()
    return float(acct.equity) - float(acct.last_equity)


def get_account():
    return client.get_account()


def is_market_open() -> bool:
    clock = client.get_clock()
    if not clock.is_open:
        now = datetime.now(ET)
        # Suppress noisy weekend logging — only log once per hour
        if now.minute == 0:
            log.info(f"Market closed ({now.strftime('%A %H:%M ET')}). Next open: {clock.next_open}")
    return clock.is_open


def scan_momentum() -> list:
    """Scan the full S&P 500 for top momentum plays."""
    top = get_top_momentum(n=50)
    log.info(f"Top movers: {[(c['symbol'], c['change_pct']) for c in top[:5]]}")
    return top[:MAX_POSITIONS]


def position_size(price: float) -> int:
    """Size each position so a 1% move hits ~$67 profit (1/3 of $200 target)."""
    acct = get_account()
    buying_power = float(acct.buying_power) / MAX_POSITIONS
    target_per_pos = DAILY_TARGET / MAX_POSITIONS
    shares_for_target = int(target_per_pos / (price * 0.01))
    max_affordable = int(buying_power / price)
    return max(1, min(shares_for_target, max_affordable))


def open_positions():
    global trading_active
    if not is_market_open():
        log.info("Market not open, skipping entry.")
        return

    pnl = get_daily_pnl()
    if pnl >= DAILY_TARGET:
        log.info(f"Target already hit (${pnl:.2f}). Skipping new entries.")
        return
    if pnl <= DAILY_LOSS_LIMIT:
        log.info(f"Loss limit already hit (${pnl:.2f}). No new entries.")
        trading_active = False
        return

    # Check if we already have open positions — don't stack
    existing = client.get_all_positions()
    if existing:
        log.info(f"Already holding {len(existing)} position(s), skipping entry.")
        return

    top = scan_momentum()
    if not top:
        log.info("No qualifying momentum stocks found today.")
        return

    bought = []
    for stock in top:
        qty = position_size(stock["price"])
        try:
            order = client.submit_order(MarketOrderRequest(
                symbol=stock["symbol"],
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            ))
            log.info(f"BUY {qty}x {stock['symbol']} ~${stock['price']} | gap {stock['change_pct']}% | relvol {stock['rel_volume']}x")
            bought.append(stock["symbol"])
        except Exception as e:
            log.error(f"Order failed {stock['symbol']}: {e}")

    save_state({"date": str(datetime.now(ET).date()), "positions": bought})


def close_position(symbol: str):
    try:
        client.close_position(symbol)
        log.info(f"Closed position: {symbol}")
    except Exception as e:
        log.error(f"Error closing {symbol}: {e}")


def close_all():
    try:
        client.close_all_positions(cancel_orders=True)
        log.info("All positions closed.")
    except Exception as e:
        log.error(f"Error closing all positions: {e}")


def check_pnl():
    """Close each position individually when it hits its target or loss limit.
    Also close everything if the daily loss limit is breached."""
    global trading_active
    if not trading_active:
        return
    if not is_market_open():
        return

    per_position_target = DAILY_TARGET / MAX_POSITIONS      # ~$67
    per_position_limit  = DAILY_LOSS_LIMIT / MAX_POSITIONS  # ~-$33

    positions = client.get_all_positions()
    for pos in positions:
        pl = float(pos.unrealized_pl)
        symbol = pos.symbol
        if pl >= per_position_target:
            log.info(f"TARGET HIT {symbol} +${pl:.2f} — closing.")
            close_position(symbol)
        elif pl <= per_position_limit:
            log.info(f"LOSS LIMIT {symbol} ${pl:.2f} — closing.")
            close_position(symbol)
        else:
            log.info(f"  {symbol}  P&L ${pl:+.2f}  (target ${per_position_target:.0f} | limit ${per_position_limit:.0f})")

    # Kill switch: stop trading for the day if total daily loss is too deep
    daily_pnl = get_daily_pnl()
    if daily_pnl <= DAILY_LOSS_LIMIT:
        log.info(f"DAILY LOSS LIMIT HIT ${daily_pnl:.2f} — halting for the day.")
        close_all()
        trading_active = False


def eod_close():
    log.info("EOD: Force-closing all positions.")
    close_all()
    global trading_active
    trading_active = False


def daily_reset():
    """Reset state at the start of each trading day."""
    global trading_active
    clock = client.get_clock()
    if not clock.is_open:
        return  # Only reset on actual trading days
    trading_active = True
    log.info("=" * 40)
    log.info(f"New trading day — state reset. Market closes: {clock.next_close}")
    log.info("=" * 40)


# ── Schedule ─────────────────────────────────────────────────────────────────
schedule.every().day.at("09:29").do(daily_reset)       # Reset at market pre-open
schedule.every().day.at("09:35").do(open_positions)    # Enter 5 min after open
schedule.every(3).minutes.do(check_pnl)                # P&L check every 3 min
schedule.every().day.at("15:45").do(eod_close)         # Force-close before EOD

if __name__ == "__main__":
    log.info("=" * 60)
    log.info("Trading bot started")
    log.info(f"Daily target: ${DAILY_TARGET} | Loss limit: ${DAILY_LOSS_LIMIT}")
    log.info("=" * 60)
    while True:
        schedule.run_pending()
        time.sleep(30)
