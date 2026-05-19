"""
Mean Reversion Bot
Strategy: buy stocks that are oversold (RSI < 30) and trading below their
20-day SMA. Waits for the morning volatility to settle then runs 11 AM - 2:30 PM ET.
Each position targets +$40 profit, stops at -$20. Max 5 positions per day.
Complements the momentum bot which runs at the open.
"""
import os
import json
import time
import schedule
import pandas as pd
import yfinance as yf
from datetime import datetime
import pytz
import logging
from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from screener import get_top_mean_reversion

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(BASE_DIR, "mean_reversion.log")),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")

DAILY_TARGET      = 200.0
DAILY_LOSS_LIMIT  = -300.0
MAX_POSITIONS     = 10
PER_TARGET        = DAILY_TARGET / MAX_POSITIONS   # $40 per position
PER_STOP          = -150.0                            # per-position stop, independent of daily limit

RSI_PERIOD    = 14
RSI_OVERSOLD  = 30   # entry threshold
RSI_EXIT      = 50   # RSI recovery exit
SMA_PERIOD    = 20
MIN_PCT_BELOW = 1.0  # price must be at least 1% below SMA to qualify


client = TradingClient(
    api_key=os.getenv("ALPACA_API_KEY"),
    secret_key=os.getenv("ALPACA_SECRET_KEY"),
    paper=True,
)

trading_active = True


def calc_rsi(closes: pd.Series, period: int = RSI_PERIOD) -> float:
    delta = closes.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss
    rsi   = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])


def is_market_open() -> bool:
    clock = client.get_clock()
    if not clock.is_open:
        now = datetime.now(ET)
        if now.minute == 0:
            log.info(f"Market closed ({now.strftime('%A %H:%M ET')}). Next open: {clock.next_open}")
    return clock.is_open


# Shared with the momentum bot for session baseline / daily P&L tracking.
SHARED_STATE_FILE = os.path.join(BASE_DIR, "bot_state.json")
# Own state file tracking only positions this bot opened.
MR_STATE_FILE = os.path.join(BASE_DIR, "mr_state.json")


def load_state() -> dict:
    if os.path.exists(SHARED_STATE_FILE):
        with open(SHARED_STATE_FILE) as f:
            return json.load(f)
    return {}


def load_mr_state() -> dict:
    if os.path.exists(MR_STATE_FILE):
        with open(MR_STATE_FILE) as f:
            return json.load(f)
    return {"positions": []}


def save_mr_state(data: dict):
    with open(MR_STATE_FILE, "w") as f:
        json.dump(data, f)


def get_daily_pnl() -> float:
    acct = client.get_account()
    state = load_state()
    baseline = state.get("session_baseline") or float(acct.last_equity)
    return float(acct.equity) - baseline


def scan_mean_reversion() -> list:
    """Scan the full S&P 500 for oversold stocks below their 20-day SMA."""
    results = get_top_mean_reversion(n=50)
    for s in results[:5]:
        log.info(f"  SIGNAL {s['symbol']}  ${s['price']}  RSI={s['rsi']}  {s['pct_below']}% below SMA")
    return results[:MAX_POSITIONS]


def position_size(price: float) -> int:
    acct = client.get_account()
    buying_power = float(acct.buying_power) / MAX_POSITIONS
    shares_for_target = int(PER_TARGET / (price * 0.01))
    max_affordable    = int(buying_power / price)
    return max(1, min(shares_for_target, max_affordable))


def open_positions():
    global trading_active
    if not is_market_open():
        return

    pnl = get_daily_pnl()
    if pnl >= DAILY_TARGET:
        log.info(f"Daily target already hit (${pnl:.2f}). No new entries.")
        return
    if pnl <= DAILY_LOSS_LIMIT:
        log.info(f"Daily loss limit hit (${pnl:.2f}). Stopping.")
        trading_active = False
        return

    mr_state = load_mr_state()
    owned = set(mr_state.get("positions", []))
    slots = MAX_POSITIONS - len(owned)
    if slots <= 0:
        log.info("All mean-reversion slots filled.")
        return

    # Also avoid symbols already held by the momentum bot
    all_held = {p.symbol for p in client.get_all_positions()}

    log.info("Scanning for oversold mean-reversion setups...")
    signals = scan_mean_reversion()
    signals = [s for s in signals if s["symbol"] not in all_held][:slots]

    if not signals:
        log.info("No qualifying oversold stocks found.")
        return

    for stock in signals:
        qty = position_size(stock["price"])
        try:
            client.submit_order(MarketOrderRequest(
                symbol=stock["symbol"],
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            ))
            log.info(
                f"BUY {qty}x {stock['symbol']} @ ~${stock['price']} "
                f"| RSI={stock['rsi']} | {stock['pct_below']}% below SMA"
            )
            owned.add(stock["symbol"])
        except Exception as e:
            log.error(f"Order failed {stock['symbol']}: {e}")

    mr_state["positions"] = list(owned)
    save_mr_state(mr_state)


def check_exits():
    """Close positions that hit their +$40 target, -$20 stop, or RSI has recovered."""
    global trading_active
    if not trading_active or not is_market_open():
        return

    # Daily kill switch
    daily_pnl = get_daily_pnl()
    log.info(f"Daily P&L: ${daily_pnl:+.2f}  (limit ${DAILY_LOSS_LIMIT:.0f} | target ${DAILY_TARGET:.0f})")
    if daily_pnl <= DAILY_LOSS_LIMIT:
        log.info(f"DAILY LOSS LIMIT ${daily_pnl:.2f} — closing everything.")
        client.close_all_positions(cancel_orders=True)
        trading_active = False
        return

    mr_state = load_mr_state()
    owned = set(mr_state.get("positions", []))

    positions = [p for p in client.get_all_positions() if p.symbol in owned]
    for pos in positions:
        # Skip if a close order is already pending (shares held for orders)
        if float(pos.qty_available) <= 0:
            log.info(f"  {pos.symbol}  close order pending, skipping.")
            continue
        pl     = float(pos.unrealized_pl)
        symbol = pos.symbol

        # Check RSI recovery as an additional exit signal
        rsi_recovered = False
        try:
            hist = yf.Ticker(symbol).history(period="30d", interval="1d")
            rsi  = calc_rsi(hist["Close"])
            rsi_recovered = rsi >= RSI_EXIT
        except Exception:
            pass

        closed = False
        if pl >= PER_TARGET:
            log.info(f"TARGET HIT {symbol} +${pl:.2f} — closing.")
            client.close_position(symbol)
            closed = True
        elif pl <= PER_STOP:
            log.info(f"STOP HIT {symbol} ${pl:.2f} — closing.")
            client.close_position(symbol)
            closed = True
        elif rsi_recovered:
            log.info(f"RSI RECOVERED {symbol} RSI>={RSI_EXIT}, P&L=${pl:.2f} — closing.")
            client.close_position(symbol)
            closed = True
        else:
            log.info(f"  {symbol}  P&L ${pl:+.2f}  (target +${PER_TARGET:.0f} | stop ${PER_STOP:.0f})")

        if closed:
            owned.discard(symbol)

    mr_state["positions"] = list(owned)
    save_mr_state(mr_state)


def eod_close():
    log.info("EOD: Closing mean-reversion positions.")
    mr_state = load_mr_state()
    for symbol in mr_state.get("positions", []):
        try:
            client.close_position(symbol)
            log.info(f"EOD closed {symbol}")
        except Exception as e:
            log.error(f"EOD close error {symbol}: {e}")
    mr_state["positions"] = []
    save_mr_state(mr_state)
    global trading_active
    trading_active = False


def daily_reset():
    """Reset state at the start of each trading day."""
    global trading_active
    clock = client.get_clock()
    if not clock.is_open:
        return
    trading_active = True
    # Refresh session_baseline so daily P&L starts from today's open equity
    acct = client.get_account()
    state = load_state()
    state["session_baseline"] = float(acct.equity)
    state["date"] = str(datetime.now(ET).date())
    state["trades_today"] = []
    with open(SHARED_STATE_FILE, "w") as f:
        json.dump(state, f)
    save_mr_state({"positions": []})
    log.info("=" * 40)
    log.info(f"New trading day — state reset. Market closes: {clock.next_close}")
    log.info("=" * 40)


# ── Schedule ──────────────────────────────────────────────────────────────────
schedule.every().day.at("09:29").do(daily_reset)       # Reset at market pre-open
schedule.every().day.at("11:00").do(open_positions)    # First scan
schedule.every().day.at("13:00").do(open_positions)    # Second scan if slots remain
schedule.every(1).minutes.do(check_exits)               # P&L check every 1 min
schedule.every().day.at("15:45").do(eod_close)         # Force-close before EOD

if __name__ == "__main__":
    log.info("=" * 60)
    log.info("Mean Reversion Bot started")
    log.info(f"Entry: RSI < {RSI_OVERSOLD} + price > {MIN_PCT_BELOW}% below {SMA_PERIOD}-day SMA")
    log.info(f"Exit:  +${PER_TARGET:.0f} target | ${PER_STOP:.0f} stop | RSI >= {RSI_EXIT} recovery")
    log.info("=" * 60)
    while True:
        schedule.run_pending()
        time.sleep(30)
