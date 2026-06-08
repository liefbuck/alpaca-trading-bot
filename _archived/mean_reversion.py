"""
Mean Reversion Bot  (MANUAL / standalone — NOT auto-started by the watchdog).

Strategy: buy oversold stocks (RSI < 30 and >1% below their 20-day SMA) between
11:00 and 13:00 ET. Each entry gets a protective BRACKET (server-side take-profit
and stop-loss) so positions stay protected even if this process dies; an RSI
recovery (>= 50) is an additional early exit. Targets/limits come from config.py.
Complements the momentum bot.

Run it by hand:  python _archived/mean_reversion.py   (or start_meanreversion.bat)
"""
import os
import sys
import json
import time
import schedule
import pandas as pd
import yfinance as yf
from datetime import datetime
import pytz
import logging
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv

# This file lives in _archived/ but imports the project's shared modules from the
# repo root, so put the root on sys.path before importing them.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from screener import get_top_mean_reversion
from trading_math import position_size, compute_bracket_prices
from config import (
    DAILY_TARGET, DAILY_LOSS_LIMIT,
    PER_POSITION_STOP as PER_STOP, MAX_POSITIONS, PER_POSITION_TARGET as PER_TARGET,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env")) or load_dotenv(os.path.join(ROOT, ".env"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        # Rotate so mean_reversion.log can't grow without bound.
        RotatingFileHandler(
            os.path.join(BASE_DIR, "mean_reversion.log"),
            maxBytes=5_000_000, backupCount=3,
        ),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")
TZ = "America/New_York"   # pin schedule jobs to ET, not the machine's local clock

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


# The momentum bot OWNS bot_state.json. We only READ its session_baseline (never
# write it — a non-atomic shared write there could corrupt the momentum bot's
# state). Our own positions/baseline live in mr_state.json.
SHARED_STATE_FILE = os.path.join(ROOT, "bot_state.json")
MR_STATE_FILE     = os.path.join(BASE_DIR, "mr_state.json")


def load_state() -> dict:
    """Read-only view of the momentum bot's shared state (for session_baseline)."""
    if os.path.exists(SHARED_STATE_FILE):
        try:
            with open(SHARED_STATE_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def load_mr_state() -> dict:
    if os.path.exists(MR_STATE_FILE):
        try:
            with open(MR_STATE_FILE) as f:
                return json.load(f)
        except Exception:
            return {"positions": []}
    return {"positions": []}


def save_mr_state(data: dict):
    # Atomic write so a crash mid-write can't corrupt mr_state.json.
    tmp = MR_STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, MR_STATE_FILE)


def get_daily_pnl() -> float:
    acct = client.get_account()
    # Prefer the momentum bot's shared baseline so dashboard P&L stays consistent;
    # else our own baseline; else CURRENT equity (P&L 0 — never a false loss-limit
    # trip). Never fall back to yesterday's close, which would read the overnight
    # move as today's loss.
    baseline = load_state().get("session_baseline")
    if baseline is None:
        baseline = load_mr_state().get("mr_baseline")
    if baseline is None:
        baseline = float(acct.equity)
    return float(acct.equity) - baseline


def scan_mean_reversion() -> list:
    """Scan the full S&P 500 for oversold stocks below their 20-day SMA."""
    results = get_top_mean_reversion(n=50)
    for s in results[:5]:
        log.info(f"  SIGNAL {s['symbol']}  ${s['price']}  RSI={s['rsi']}  {s['pct_below']}% below SMA")
    return results[:MAX_POSITIONS]


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

    # Avoid symbols already held (by us or the momentum bot)
    all_held = {p.symbol for p in client.get_all_positions()}

    log.info("Scanning for oversold mean-reversion setups...")
    signals = scan_mean_reversion()
    signals = [s for s in signals if s["symbol"] not in all_held][:slots]

    if not signals:
        log.info("No qualifying oversold stocks found.")
        return

    acct = client.get_account()
    per_pos_bp = float(acct.buying_power) / MAX_POSITIONS

    for stock in signals:
        price = stock["price"]
        qty = position_size(price, per_pos_bp)   # shared sizing helper
        tp_price, sl_price = compute_bracket_prices(price, qty)
        try:
            # BRACKET: broker-side take-profit + stop-loss protect the position
            # even if this process dies (the old version placed naked market buys).
            client.submit_order(MarketOrderRequest(
                symbol=stock["symbol"],
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                order_class=OrderClass.BRACKET,
                take_profit=TakeProfitRequest(limit_price=tp_price),
                stop_loss=StopLossRequest(stop_price=sl_price),
            ))
            log.info(
                f"BUY {qty}x {stock['symbol']} @ ~${price} | TP ${tp_price} SL ${sl_price} "
                f"| RSI={stock['rsi']} | {stock['pct_below']}% below SMA"
            )
            owned.add(stock["symbol"])
        except Exception as e:
            log.error(f"Order failed {stock['symbol']}: {e}")

    mr_state["positions"] = list(owned)
    save_mr_state(mr_state)


def check_exits():
    """Brackets own the target/stop (server-side). This handles the RSI-recovery
    early exit, the account-wide daily kill switch, and pruning closed names."""
    global trading_active
    if not trading_active or not is_market_open():
        return

    daily_pnl = get_daily_pnl()
    log.info(f"Daily P&L: ${daily_pnl:+.2f}  (limit ${DAILY_LOSS_LIMIT:.0f} | target ${DAILY_TARGET:.0f})")
    if daily_pnl <= DAILY_LOSS_LIMIT:
        log.info(f"DAILY LOSS LIMIT ${daily_pnl:.2f} - closing mean-reversion positions.")
        mr_state = load_mr_state()
        for symbol in mr_state.get("positions", []):
            try:
                client.close_position(symbol)
            except Exception as e:
                log.error(f"Loss limit close error {symbol}: {e}")
        save_mr_state({"positions": [], "mr_baseline": mr_state.get("mr_baseline")})
        trading_active = False
        return

    mr_state = load_mr_state()
    owned = set(mr_state.get("positions", []))
    if not owned:
        return

    open_syms = {p.symbol for p in client.get_all_positions()}
    # Prune names the bracket already closed (target/stop hit)
    for sym in list(owned):
        if sym not in open_syms:
            log.info(f"  {sym} closed by bracket (target/stop hit).")
            owned.discard(sym)

    for pos in [p for p in client.get_all_positions() if p.symbol in owned]:
        if float(pos.qty_available) <= 0:
            continue  # a close order is already pending
        symbol = pos.symbol
        try:
            hist = yf.Ticker(symbol).history(period="30d", interval="1d")
            rsi  = calc_rsi(hist["Close"])
        except Exception as e:
            log.warning(f"RSI check failed for {symbol}: {e}")
            continue
        if rsi >= RSI_EXIT:
            log.info(f"RSI RECOVERED {symbol} RSI>={RSI_EXIT}, P&L=${float(pos.unrealized_pl):+.2f} - closing.")
            try:
                client.close_position(symbol)   # also cancels the bracket OCO
                owned.discard(symbol)
            except Exception as e:
                log.error(f"Error closing {symbol}: {e}")
        else:
            log.info(f"  {symbol}  P&L ${float(pos.unrealized_pl):+.2f}  RSI={rsi:.0f}  "
                     f"(bracket +${PER_TARGET:.0f}/${PER_STOP:.0f})")

    mr_state["positions"] = list(owned)
    save_mr_state(mr_state)


def eod_close():
    global trading_active
    log.info("EOD: Closing mean-reversion positions.")
    mr_state = load_mr_state()
    for symbol in mr_state.get("positions", []):
        try:
            client.close_position(symbol)
            log.info(f"EOD closed {symbol}")
        except Exception as e:
            log.error(f"EOD close error {symbol}: {e}")
    save_mr_state({"positions": []})
    trading_active = False


def daily_reset():
    """Reset state at the start of each trading day."""
    global trading_active
    clock = client.get_clock()
    now = datetime.now(ET)
    if clock.next_open.date() != now.date() and not clock.is_open:
        return
    trading_active = True
    # Store OUR OWN baseline in mr_state.json. Do NOT write the momentum bot's
    # bot_state.json (a non-atomic shared write there could corrupt it).
    acct = client.get_account()
    save_mr_state({"positions": [], "mr_baseline": float(acct.equity)})
    log.info("=" * 40)
    log.info(f"New trading day - mean-reversion state reset. Market closes: {clock.next_close}")
    log.info("=" * 40)


# -- Schedule (all wall-clock times pinned to ET, not the machine's local tz) ----
schedule.every().day.at("09:29", TZ).do(daily_reset)       # Reset at market pre-open
schedule.every().day.at("11:00", TZ).do(open_positions)    # First scan
schedule.every().day.at("13:00", TZ).do(open_positions)    # Second scan if slots remain
schedule.every(1).minutes.do(check_exits)                  # exit/kill-switch check every 1 min
schedule.every().day.at("15:45", TZ).do(eod_close)         # Force-close before EOD

if __name__ == "__main__":
    log.info("=" * 60)
    log.info("Mean Reversion Bot started")
    log.info(f"Entry: RSI < {RSI_OVERSOLD} + price > {MIN_PCT_BELOW}% below {SMA_PERIOD}-day SMA")
    log.info(f"Exit:  bracket +${PER_TARGET:.0f}/${PER_STOP:.0f} | RSI >= {RSI_EXIT} recovery")
    log.info("=" * 60)
    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            log.error(f"Scheduler error (continuing): {e}")
        time.sleep(30)
