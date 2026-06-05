import os
import json
import time
import schedule
from datetime import datetime
from uuid import UUID
import pytz
import logging
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv

import yfinance as yf

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest, TakeProfitRequest, StopLossRequest, ReplaceOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus, OrderClass
from screener import get_top_momentum
from config import DAILY_TARGET, DAILY_LOSS_LIMIT, MAX_POSITIONS
from trading_math import position_size, compute_bracket_prices, select_stop_pl, classify_trades

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        # Rotate so bot.log can't grow without bound (it was 3.3 MB and climbing).
        # 5 MB x 3 backups = ~20 MB ceiling.
        RotatingFileHandler(
            os.path.join(BASE_DIR, "bot.log"),
            maxBytes=5_000_000, backupCount=3,
        ),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")
STATE_FILE = os.path.join(BASE_DIR, "bot_state.json")
PERF_FILE  = os.path.join(BASE_DIR, "performance_log.json")

client = TradingClient(
    api_key=os.getenv("ALPACA_API_KEY"),
    secret_key=os.getenv("ALPACA_SECRET_KEY"),
    paper=True,
)

trading_active = True
_last_api_error_log: float = 0.0       # epoch seconds — rate-limits repeated DNS error logs
_last_market_closed_log: float = 0.0   # epoch seconds — rate-limits "market closed" log to 1/hr


def save_state(data: dict):
    # Write to a temp file then replace atomically — prevents corrupt state on crash/kill.
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, STATE_FILE)


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception as e:
            log.warning(f"State file unreadable ({e}) — starting fresh.")
            return {}
    return {}


def persist_halted(halted: bool):
    """Persist the trading_active flag so a restart doesn't resume after a loss-limit halt."""
    state = load_state()
    state["trading_halted"] = halted
    save_state(state)


def get_daily_pnl() -> float:
    acct = client.get_account()
    state = load_state()
    baseline = state.get("session_baseline")
    if baseline is None:
        baseline = float(acct.last_equity)
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
    global _last_api_error_log, _last_market_closed_log
    try:
        clock = client.get_clock()
    except Exception as e:
        now_ts = time.time()
        if now_ts - _last_api_error_log > 300:  # log at most once every 5 minutes
            log.warning(f"Alpaca API unreachable (network issue?): {e}")
            _last_api_error_log = now_ts
        return False  # treat as market closed so nothing dangerous runs
    if not clock.is_open:
        now_ts = time.time()
        if now_ts - _last_market_closed_log > 3600:  # log at most once an hour, not every 5s
            now = datetime.now(ET)
            log.info(f"Market closed ({now.strftime('%A %H:%M ET')}). Next open: {clock.next_open}")
            _last_market_closed_log = now_ts
    return clock.is_open


SPY_DOWN_THRESHOLD = 0.0  # skip entries if SPY is down at all on the day

def spy_is_positive() -> bool:
    """Return True unless SPY is down more than SPY_DOWN_THRESHOLD on the day."""
    try:
        data = yf.Ticker("SPY").history(period="2d", interval="1d")
        if len(data) < 2:
            return True  # can't tell — allow entry
        prev  = float(data["Close"].iloc[-2])
        today = float(data["Close"].iloc[-1])
        change = (today - prev) / prev * 100
        log.info(f"SPY day change: {change:+.2f}% (threshold {SPY_DOWN_THRESHOLD:+.1f}%)")
        return change >= SPY_DOWN_THRESHOLD
    except Exception as e:
        log.warning(f"SPY check failed ({e}) — allowing entry")
        return True


def scan_momentum() -> list:
    """Scan S&P 500 + Nasdaq 100 + watchlist for top momentum plays."""
    top = get_top_momentum(n=50)
    log.info(f"Top movers: {[(c['symbol'], c['change_pct']) for c in top[:5]]}")
    return top[:MAX_POSITIONS]


def open_positions():
    global trading_active
    if not trading_active:
        log.info("Trading halted — skipping entry.")
        return
    if not is_market_open():
        log.info("Market not open, skipping entry.")
        return

    now_et = datetime.now(ET)
    entry_cutoff = now_et.replace(hour=10, minute=30, second=0, microsecond=0)
    if now_et >= entry_cutoff:
        log.info("Past 10:30 — entry window closed for today.")
        return

    # One entry cycle per day — once we've bought in, don't re-enter after closing out.
    state = load_state()
    if state.get("entries_done"):
        log.info("Entry cycle already complete for today — skipping re-entry.")
        return

    pnl = get_daily_pnl()
    if pnl >= DAILY_TARGET:
        log.info(f"Target already hit (${pnl:.2f}). Skipping new entries.")
        return
    if pnl <= DAILY_LOSS_LIMIT:
        log.info(f"Loss limit already hit (${pnl:.2f}). No new entries.")
        trading_active = False
        persist_halted(True)
        return

    # Market direction filter: skip momentum buys on down-market days
    if not spy_is_positive():
        log.info(f"SPY down more than {SPY_DOWN_THRESHOLD:+.1f}% — skipping momentum entry.")
        return

    # Check if we already have open positions — don't stack.
    # Count ALL held positions: under bracket orders every position reserves
    # its shares for the SL/TP legs (qty_available == 0), so this must not
    # filter on qty_available or the guard would never fire.
    existing = client.get_all_positions()
    if existing:
        log.info(f"Already holding {len(existing)} position(s), skipping entry.")
        return

    top = scan_momentum()
    if not top:
        log.info("No qualifying momentum stocks found today.")
        return

    # Fetch account once — don't call Alpaca once per stock in the loop
    acct = get_account()
    per_pos_buying_power = float(acct.buying_power) / MAX_POSITIONS

    bought = []
    sl_order_ids = {}

    for stock in top:
        qty = position_size(stock["price"], per_pos_buying_power)
        price = stock["price"]
        take_profit_price, stop_price = compute_bracket_prices(price, qty)
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
            # Capture the stop-loss leg ID so we can ratchet it later
            sl_leg = next(
                (leg for leg in (order.legs or [])
                 if str(getattr(leg, "order_type", "")).lower() in ("stop", "stop_limit")),
                None,
            )
            if sl_leg:
                sl_order_ids[stock["symbol"]] = str(sl_leg.id)
            else:
                log.warning(f"  {stock['symbol']}: bracket SL leg not returned — step stops disabled for this position")
            log.info(f"BUY {qty}x {stock['symbol']} ~${price} | TP ${take_profit_price} | SL ${stop_price} | gap {stock['change_pct']}% | relvol {stock['rel_vol']}x | voltren {stock.get('vol_trend','?')}x")
            bought.append(stock["symbol"])
        except Exception as e:
            log.error(f"Order failed {stock['symbol']}: {e}")

    if bought:
        state = load_state()
        state["date"] = str(datetime.now(ET).date())
        state["positions"] = bought
        state["sl_order_ids"] = sl_order_ids
        state["stop_steps_reached"] = {}   # symbol -> highest stop level locked in so far
        state["entries_done"] = True
        save_state(state)
        log.info("Entry cycle marked complete — no further entries today.")


def record_trade(symbol: str, pl: float):
    """Append a closed trade result to today's state for EOD summary."""
    state = load_state()
    trades = state.get("trades_today", [])
    trades.append({"symbol": symbol, "pl": round(pl, 2)})
    state["trades_today"] = trades
    save_state(state)



def close_all():
    try:
        client.close_all_positions(cancel_orders=True)
        log.info("All positions closed.")
    except Exception as e:
        log.error(f"Error closing all positions: {e}")


def step_trailing_stops():
    """Ratchet each position's stop-loss order up through the STOP_STEPS ladder."""
    state = load_state()
    sl_order_ids     = state.get("sl_order_ids", {})
    steps_reached    = state.get("stop_steps_reached", {})
    if not sl_order_ids:
        return

    updated = False
    try:
        positions = client.get_all_positions()
    except Exception:
        return

    for pos in positions:
        symbol      = pos.symbol
        sl_id       = sl_order_ids.get(symbol)
        if not sl_id:
            continue
        pl          = float(pos.unrealized_pl)
        qty         = float(pos.qty)
        entry_price = float(pos.avg_entry_price)
        current_step = steps_reached.get(symbol, -1)  # highest stop_pl level already applied

        # Find the highest step we've earned but not yet applied
        new_stop_pl = select_stop_pl(pl, current_step)
        if new_stop_pl is None:
            continue

        new_stop_price = round(entry_price + new_stop_pl / qty, 2)
        try:
            # replace_order cancels the existing order and returns a NEW order with a
            # new ID. Capture it so the next step targets the live order, not the
            # stale (now-replaced) one.
            new_order = client.replace_order_by_id(UUID(sl_id), ReplaceOrderRequest(stop_price=new_stop_price))
            sl_order_ids[symbol] = str(new_order.id)
            log.info(f"STEP STOP {symbol}: P&L ${pl:+.2f} → stop raised to ${new_stop_pl:+.0f} (${new_stop_price:.2f}/share)")
            steps_reached[symbol] = new_stop_pl
            updated = True
        except Exception as e:
            log.warning(f"step_trailing_stops: could not update {symbol} stop: {e}")

    if updated:
        state = load_state()
        state["stop_steps_reached"] = steps_reached
        state["sl_order_ids"] = sl_order_ids
        save_state(state)


def sync_bracket_fills():
    """Detect positions closed by bracket orders and record their P&L."""
    state = load_state()
    tracked = set(state.get("positions", []))
    if not tracked:
        return
    try:
        open_syms = {p.symbol for p in client.get_all_positions()}
    except Exception:
        return
    closed_syms = tracked - open_syms
    if not closed_syms:
        return

    today = datetime.now(ET).date()
    recorded = {t["symbol"] for t in state.get("trades_today", [])}

    for symbol in closed_syms:
        if symbol in recorded:
            continue
        try:
            orders = client.get_orders(GetOrdersRequest(
                status=QueryOrderStatus.CLOSED,
                symbols=[symbol],
                limit=10,
            ))
            buys  = [o for o in orders if o.side.value == "buy"  and o.filled_avg_price and o.submitted_at and o.submitted_at.astimezone(ET).date() == today]
            sells = [o for o in orders if o.side.value == "sell" and o.filled_avg_price and o.submitted_at and o.submitted_at.astimezone(ET).date() == today]
            if buys and sells:
                entry_price = float(buys[0].filled_avg_price)
                exit_price  = float(sells[0].filled_avg_price)
                qty         = float(sells[0].filled_qty)
                pl          = round((exit_price - entry_price) * qty, 2)
                log.info(f"Bracket closed {symbol}: entry ${entry_price:.2f} → exit ${exit_price:.2f} | P&L ${pl:+.2f}")
                record_trade(symbol, pl)
        except Exception as e:
            log.warning(f"sync_bracket_fills: could not record {symbol}: {e}")

    # Remove closed symbols from tracked positions in state
    state = load_state()
    state["positions"] = list(open_syms & tracked)
    save_state(state)


def check_pnl():
    """Monitor positions and enforce the daily loss limit kill switch.
    Per-position exits are handled by bracket orders submitted at entry."""
    global trading_active
    if trading_active:
        _state = load_state()
        if _state.get("trading_halted"):
            trading_active = False
            log.info("check_pnl: trading_halted flag detected in state — halting.")
    if not trading_active:
        return
    if not is_market_open():
        return

    sync_bracket_fills()
    step_trailing_stops()

    daily_pnl = get_daily_pnl()
    log.info(f"Daily P&L: ${daily_pnl:+.2f}  (limit ${DAILY_LOSS_LIMIT:.0f} | target ${DAILY_TARGET:.0f})")

    steps_reached = load_state().get("stop_steps_reached", {})
    for pos in client.get_all_positions():
        pl   = float(pos.unrealized_pl)
        step = steps_reached.get(pos.symbol)
        stop_note = f" [stop locked at ${step:+.0f}]" if step is not None else " [stop at initial]"
        log.info(f"  {pos.symbol}  P&L ${pl:+.2f}{stop_note}")

    if daily_pnl <= DAILY_LOSS_LIMIT:
        log.info(f"DAILY LOSS LIMIT HIT ${daily_pnl:.2f} — cancelling brackets and halting.")
        close_all()
        trading_active = False
        persist_halted(True)


def log_daily_performance():
    """Append today's summary to performance_log.json."""
    state = load_state()
    trades = state.get("trades_today", [])
    daily_pnl = get_daily_pnl()
    entry = {
        "date":      str(datetime.now(ET).date()),
        "daily_pnl": round(daily_pnl, 2),
        **classify_trades(trades, daily_pnl),
    }

    log.info(f"EOD Summary: {entry}")

    history = []
    if os.path.exists(PERF_FILE):
        try:
            with open(PERF_FILE) as f:
                history = json.load(f)
        except Exception as e:
            log.warning(f"performance_log.json unreadable ({e}) — starting fresh history.")
            history = []

    # Replace today's entry if it already exists, otherwise append
    history = [h for h in history if h["date"] != entry["date"]]
    history.append(entry)
    history.sort(key=lambda x: x["date"])

    # Atomic write — prevents corrupt file if process is killed mid-write
    tmp = PERF_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(history, f, indent=2)
    os.replace(tmp, PERF_FILE)


def eod_close():
    global trading_active
    # This fires every calendar day, but the market is only open on trading days.
    # At 15:45 ET a normal session is still open (closes 16:00), so is_open
    # cleanly identifies trading days. On weekends/holidays, skip the whole EOD
    # routine so we don't append an empty entry to performance_log.json.
    try:
        if not client.get_clock().is_open:
            log.info("EOD: non-trading day (weekend/holiday) — skipping summary.")
            persist_halted(False)
            trading_active = False
            return
    except Exception as e:
        log.warning(f"EOD: clock check failed ({e}) — proceeding with close.")

    log.info("EOD: Force-closing all positions.")
    # Catch any bracket orders that fired in the last few seconds before EOD.
    sync_bracket_fills()
    # Record each still-open position's unrealized P&L before the bulk close.
    try:
        for pos in client.get_all_positions():
            pl = float(pos.unrealized_pl)
            log.info(f"  EOD recording {pos.symbol} P&L ${pl:+.2f}")
            record_trade(pos.symbol, pl)
    except Exception as e:
        log.error(f"EOD trade recording error: {e}")
    close_all()
    try:
        log_daily_performance()
    except Exception as e:
        log.error(f"EOD: failed to write performance log ({e}) — continuing shutdown.")
    persist_halted(False)  # Reset for next day — write before flipping memory flag
    trading_active = False


def daily_reset():
    """Reset state at the start of each trading day."""
    global trading_active
    try:
        clock = client.get_clock()
    except Exception as e:
        log.error(f"daily_reset: could not reach Alpaca ({e}) — skipping reset.")
        return
    # Guard: skip weekends/holidays by checking next open is today
    now = datetime.now(ET)
    if clock.next_open.date() != now.date() and not clock.is_open:
        return
    trading_active = True
    reset_session()
    state = load_state()
    state["date"] = str(datetime.now(ET).date())
    state["positions"] = []
    state["trades_today"] = []
    state["trading_halted"] = False
    state["entries_done"] = False
    state["sl_order_ids"] = {}
    state["stop_steps_reached"] = {}
    save_state(state)
    log.info("=" * 40)
    log.info(f"New trading day — state reset. Market closes: {clock.next_close}")
    log.info("=" * 40)


def close_overnight():
    """Close any positions held overnight before the fresh morning scan."""
    try:
        positions = client.get_all_positions()
    except Exception as e:
        log.error(f"close_overnight: could not fetch positions ({e})")
        return
    if not positions:
        return
    log.info(f"Closing {len(positions)} overnight position(s) before morning scan...")
    for pos in positions:
        pl = float(pos.unrealized_pl)
        log.info(f"  Closing overnight {pos.symbol}  P&L ${pl:+.2f}")
        # Do NOT record_trade here — these positions belonged to yesterday's session.
        # EOD already logged them; recording again would pollute today's performance stats.
    close_all()


# ── Schedule ─────────────────────────────────────────────────────────────────
# All daily times are pinned to US Eastern explicitly. Without the tz argument
# `schedule` fires against the machine's LOCAL clock, so every entry/reset/close
# would silently misfire if this host's timezone ever differed from ET.
TZ = "America/New_York"
schedule.every().day.at("09:29", TZ).do(daily_reset)       # Reset at market pre-open
schedule.every().day.at("09:31", TZ).do(close_overnight)   # Close any overnight positions
schedule.every(5).seconds.do(check_pnl)                    # P&L check every 5 sec
schedule.every().day.at("15:45", TZ).do(eod_close)         # Force-close before EOD

# Entry attempts every 15 min from 9:32 to 10:18 — stops after 10:30 or once positions are open
for _t in ["09:32", "09:48", "10:03", "10:18"]:
    schedule.every().day.at(_t, TZ).do(open_positions)

def wait_for_network(max_wait_seconds: int = 300, interval: int = 15):
    """Block until Alpaca API is reachable. Handles slow network on morning boot."""
    deadline = time.time() + max_wait_seconds
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            client.get_clock()
            if attempt > 1:
                log.info(f"Network ready after {attempt} attempt(s).")
            return
        except Exception as e:
            remaining = int(deadline - time.time())
            log.warning(f"Network not ready (attempt {attempt}, {remaining}s remaining): {e}")
            time.sleep(interval)
    log.error("Alpaca API still unreachable after startup wait — continuing anyway.")


if __name__ == "__main__":
    log.info("=" * 60)
    log.info("Trading bot started")
    log.info(f"Daily target: ${DAILY_TARGET} | Loss limit: ${DAILY_LOSS_LIMIT}")
    log.info("=" * 60)
    wait_for_network()
    # On startup: if state file has no baseline for today, reset it now so
    # check_pnl doesn't immediately see a stale loss and kill trading.
    state = load_state()
    today = str(datetime.now(ET).date())
    if state.get("date") != today or not state.get("session_baseline"):
        log.info("Startup: no baseline for today — resetting session.")
        # Safety: if the bot was down across a prior EOD, the broker may still
        # hold yesterday's positions with no stop tracking in state. On a genuine
        # new-day startup, close any such leftovers before we begin so they
        # aren't carried untracked/unmanaged through the session.
        try:
            stale = client.get_all_positions()
            if stale:
                log.warning(f"Startup: closing {len(stale)} stale position(s) from a prior day: "
                            f"{[p.symbol for p in stale]}")
                close_all()
        except Exception as e:
            log.error(f"Startup: could not check/close stale positions ({e})")
        reset_session()
        state = load_state()
        state["date"] = today
        state["positions"] = []
        state["trades_today"] = []
        state["trading_halted"] = False
        state["entries_done"] = False
        state["sl_order_ids"] = {}
        state["stop_steps_reached"] = {}
        save_state(state)
    else:
        log.info(f"Startup: using existing baseline ${state['session_baseline']:.2f} for {today}")
        # Restore halted state — if the loss limit was hit before a restart, don't resume trading
        if state.get("trading_halted"):
            trading_active = False
            log.info("Startup: trading was halted (loss limit hit earlier today) — not resuming.")

    # Catchup: if we start/restart during the trading window (after 09:33, before EOD)
    # and hold no positions, run open_positions() immediately so a restart never
    # causes us to sit out the whole day.
    # Skip if trading was halted earlier today.
    now_et = datetime.now(ET)
    market_open_time = now_et.replace(hour=9, minute=32, second=0, microsecond=0)
    entry_cutoff     = now_et.replace(hour=10, minute=30, second=0, microsecond=0)
    if trading_active and market_open_time <= now_et < entry_cutoff:
        existing = client.get_all_positions()
        if not existing:
            log.info("Startup catchup: market is open, no positions held — running open_positions() now.")
            open_positions()
        else:
            log.info(f"Startup catchup: already holding {len(existing)} position(s), skipping entry.")

    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            log.error(f"Scheduler error (continuing): {e}")
        time.sleep(1)  # 1s granularity so check_pnl fires every ~5s
