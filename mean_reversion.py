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
DAILY_LOSS_LIMIT  = -100.0
MAX_POSITIONS     = 5
PER_TARGET        = DAILY_TARGET / MAX_POSITIONS   # $40 per position
PER_STOP          = DAILY_LOSS_LIMIT / MAX_POSITIONS  # -$20 per position

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


def get_daily_pnl() -> float:
    acct = client.get_account()
    return float(acct.equity) - float(acct.last_equity)


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

    existing = client.get_all_positions()
    held = {p.symbol for p in existing}
    slots = MAX_POSITIONS - len(existing)
    if slots <= 0:
        log.info("All position slots filled.")
        return

    log.info("Scanning for oversold mean-reversion setups...")
    signals = scan_mean_reversion()
    signals = [s for s in signals if s["symbol"] not in held][:slots]

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
        except Exception as e:
            log.error(f"Order failed {stock['symbol']}: {e}")


def check_exits():
    """Close positions that hit their +$40 target, -$20 stop, or RSI has recovered."""
    global trading_active
    if not trading_active or not is_market_open():
        return

    # Daily kill switch
    daily_pnl = get_daily_pnl()
    if daily_pnl <= DAILY_LOSS_LIMIT:
        log.info(f"DAILY LOSS LIMIT ${daily_pnl:.2f} — closing everything.")
        client.close_all_positions(cancel_orders=True)
        trading_active = False
        return

    positions = client.get_all_positions()
    for pos in positions:
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

        if pl >= PER_TARGET:
            log.info(f"TARGET HIT {symbol} +${pl:.2f} — closing.")
            client.close_position(symbol)
        elif pl <= PER_STOP:
            log.info(f"STOP HIT {symbol} ${pl:.2f} — closing.")
            client.close_position(symbol)
        elif rsi_recovered:
            log.info(f"RSI RECOVERED {symbol} RSI>={RSI_EXIT}, P&L=${pl:.2f} — closing.")
            client.close_position(symbol)
        else:
            log.info(f"  {symbol}  P&L ${pl:+.2f}  (target +${PER_TARGET:.0f} | stop ${PER_STOP:.0f})")


def eod_close():
    log.info("EOD: Closing all mean-reversion positions.")
    try:
        client.close_all_positions(cancel_orders=True)
    except Exception as e:
        log.error(f"EOD close error: {e}")
    global trading_active
    trading_active = False


def daily_reset():
    """Reset state at the start of each trading day."""
    global trading_active
    clock = client.get_clock()
    if not clock.is_open:
        return
    trading_active = True
    log.info("=" * 40)
    log.info(f"New trading day — state reset. Market closes: {clock.next_close}")
    log.info("=" * 40)


# ── Schedule ──────────────────────────────────────────────────────────────────
schedule.every().day.at("09:29").do(daily_reset)       # Reset at market pre-open
schedule.every().day.at("11:00").do(open_positions)    # First scan
schedule.every().day.at("13:00").do(open_positions)    # Second scan if slots remain
schedule.every(3).minutes.do(check_exits)              # P&L check every 3 min
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
