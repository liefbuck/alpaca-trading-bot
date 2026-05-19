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
DAILY_LOSS_LIMIT = -300.0
PER_POSITION_LOSS_LIMIT = -150.0
MAX_POSITIONS = 10
STATE_FILE = "bot_state.json"
PERF_FILE  = "performance_log.json"

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
    state = load_state()
    baseline = state.get("session_baseline") or float(acct.last_equity)
    return float(acct.equity) - baseline


def reset_session():
    """Reset daily P&L tracking to current equity so today starts fresh."""
    acct = client.get_account()
    baseline = float(acct.equity)
    state = load_state()
    state["session_baseline"] = baseline
    save_state(state)
    log.info(f"Session reset — new baseline equity: ${baseline:.2f}")


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
    # Exclude positions with a pending close order (qty_available == 0)
    existing = [p for p in client.get_all_positions() if float(p.qty_available) > 0]
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
            log.info(f"BUY {qty}x {stock['symbol']} ~${stock['price']} | gap {stock['change_pct']}% | relvol {stock['rel_vol']}x | voltren {stock.get('vol_trend','?')}x")
            bought.append(stock["symbol"])
        except Exception as e:
            log.error(f"Order failed {stock['symbol']}: {e}")

    state = load_state()
    state["date"] = str(datetime.now(ET).date())
    state["positions"] = bought
    save_state(state)


def record_trade(symbol: str, pl: float):
    """Append a closed trade result to today's state for EOD summary."""
    state = load_state()
    trades = state.get("trades_today", [])
    trades.append({"symbol": symbol, "pl": round(pl, 2)})
    state["trades_today"] = trades
    save_state(state)


def close_position(symbol: str, pl: float = None):
    try:
        pos = client.get_open_position(symbol)
        entry_price = float(pos.avg_entry_price)
        qty = float(pos.qty)
        client.close_position(symbol)
        log.info(f"Closed position: {symbol}")
        # Wait briefly for the fill, then record actual realized P&L
        time.sleep(1)
        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            orders = client.get_orders(GetOrdersRequest(
                status=QueryOrderStatus.CLOSED,
                symbols=[symbol],
                limit=5,
            ))
            sell = next((o for o in orders if o.side.value == "sell" and o.filled_avg_price), None)
            if sell:
                actual_pl = (float(sell.filled_avg_price) - entry_price) * qty
                log.info(f"  {symbol} actual fill: ${float(sell.filled_avg_price):.4f} | realized P&L: ${actual_pl:.2f} (logged ${pl:.2f})")
                record_trade(symbol, actual_pl)
            elif pl is not None:
                record_trade(symbol, pl)
        except Exception:
            if pl is not None:
                record_trade(symbol, pl)
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

    per_position_target = DAILY_TARGET / MAX_POSITIONS      # ~$20

    daily_pnl = get_daily_pnl()
    log.info(f"Daily P&L: ${daily_pnl:+.2f}  (limit ${DAILY_LOSS_LIMIT:.0f} | target ${DAILY_TARGET:.0f})")

    positions = client.get_all_positions()
    for pos in positions:
        # Skip if a close order is already pending (shares held for orders)
        if float(pos.qty_available) <= 0:
            log.info(f"  {pos.symbol}  close order pending, skipping.")
            continue
        pl = float(pos.unrealized_pl)
        symbol = pos.symbol
        if pl >= per_position_target:
            log.info(f"TARGET HIT {symbol} +${pl:.2f} — closing.")
            close_position(symbol, pl)
        elif pl <= PER_POSITION_LOSS_LIMIT:
            log.info(f"LOSS LIMIT {symbol} ${pl:.2f} — closing.")
            close_position(symbol, pl)
        else:
            log.info(f"  {symbol}  P&L ${pl:+.2f}  (target ${per_position_target:.0f} | limit ${PER_POSITION_LOSS_LIMIT:.0f})")

    # Kill switch: stop trading for the day if total daily loss is too deep
    if daily_pnl <= DAILY_LOSS_LIMIT:
        log.info(f"DAILY LOSS LIMIT HIT ${daily_pnl:.2f} — halting for the day.")
        close_all()
        trading_active = False


def log_daily_performance():
    """Append today's summary to performance_log.json."""
    state = load_state()
    trades = state.get("trades_today", [])
    daily_pnl = get_daily_pnl()
    wins  = [t for t in trades if t["pl"] > 0]
    losses = [t for t in trades if t["pl"] <= 0]

    entry = {
        "date":        str(datetime.now(ET).date()),
        "daily_pnl":   round(daily_pnl, 2),
        "trades":      len(trades),
        "wins":        len(wins),
        "losses":      len(losses),
        "win_rate":    round(len(wins) / len(trades) * 100, 1) if trades else 0,
        "best_trade":  round(max((t["pl"] for t in trades), default=0), 2),
        "worst_trade": round(min((t["pl"] for t in trades), default=0), 2),
        "result":      "WIN" if daily_pnl > 0 else "LOSS",
    }

    log = logging.getLogger(__name__)
    log.info(f"EOD Summary: {entry}")

    history = []
    if os.path.exists(PERF_FILE):
        with open(PERF_FILE) as f:
            history = json.load(f)

    # Replace today's entry if it already exists, otherwise append
    history = [h for h in history if h["date"] != entry["date"]]
    history.append(entry)
    history.sort(key=lambda x: x["date"])

    with open(PERF_FILE, "w") as f:
        json.dump(history, f, indent=2)


def eod_close():
    log.info("EOD: Force-closing all positions.")
    close_all()
    log_daily_performance()
    global trading_active
    trading_active = False


def daily_reset():
    """Reset state at the start of each trading day."""
    global trading_active
    clock = client.get_clock()
    if not clock.is_open:
        return  # Only reset on actual trading days
    trading_active = True
    reset_session()
    state = load_state()
    state["date"] = str(datetime.now(ET).date())
    state["positions"] = []
    state["trades_today"] = []
    save_state(state)
    log.info("=" * 40)
    log.info(f"New trading day — state reset. Market closes: {clock.next_close}")
    log.info("=" * 40)


def close_overnight():
    """Close any positions held overnight before the fresh morning scan."""
    positions = client.get_all_positions()
    if not positions:
        return
    log.info(f"Closing {len(positions)} overnight position(s) before morning scan...")
    for pos in positions:
        pl = float(pos.unrealized_pl)
        log.info(f"  Closing overnight {pos.symbol}  P&L ${pl:+.2f}")
    close_all()


# ── Schedule ─────────────────────────────────────────────────────────────────
schedule.every().day.at("09:29").do(daily_reset)       # Reset at market pre-open
schedule.every().day.at("09:31").do(close_overnight)   # Close any overnight positions
schedule.every().day.at("09:35").do(open_positions)    # Enter 5 min after open
schedule.every(1).minutes.do(check_pnl)                 # P&L check every 1 min
schedule.every().day.at("15:45").do(eod_close)         # Force-close before EOD

if __name__ == "__main__":
    log.info("=" * 60)
    log.info("Trading bot started")
    log.info(f"Daily target: ${DAILY_TARGET} | Loss limit: ${DAILY_LOSS_LIMIT}")
    log.info("=" * 60)
    while True:
        schedule.run_pending()
        time.sleep(30)
