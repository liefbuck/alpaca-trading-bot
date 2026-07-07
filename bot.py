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
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest, GetOrdersRequest, TakeProfitRequest, StopLossRequest, ReplaceOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus, OrderClass, OrderType
from screener import get_top_momentum
from config import DAILY_TARGET, DAILY_LOSS_LIMIT, MAX_POSITIONS, PER_POSITION_HARD_STOP
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
            # Without an explicit encoding the handler uses the Windows locale
            # (cp1252), which can't encode characters like the "→" in the STEP STOP
            # message — logging then swallows the UnicodeEncodeError and DROPS the
            # whole line. That silently hid every trailing-stop move from bot.log
            # (and from step_watch.py's toast alerts, which tail this file).
            encoding="utf-8",
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
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, STATE_FILE)


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
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
        # No baseline in state (fresh boot, or a corrupted/half-synced state
        # file). Measuring against last_equity (yesterday's CLOSE) would read
        # the whole overnight+intraday move as "today's" P&L — on a gap-down day
        # that can blow past the loss limit and FALSE-TRIP the kill switch, closing
        # everything. Fall back to CURRENT equity instead → P&L 0 for this tick;
        # daily_reset / startup restores a real baseline immediately after.
        log.warning("get_daily_pnl: no session_baseline in state — using current "
                    "equity (P&L 0 this tick) to avoid a false loss-limit halt.")
        baseline = float(acct.equity)
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
    """Scan S&P 500 + Nasdaq 100 + watchlist for top momentum plays.

    Returns up to 2x MAX_POSITIONS ranked candidates: the extra names are
    backfill, so if a top pick's order is rejected the buy loop can fall through
    to the next-best candidate instead of ending the day a position short.
    """
    top = get_top_momentum(n=50)
    log.info(f"Top movers: {[(c['symbol'], c['change_pct']) for c in top[:5]]}")
    return top[:MAX_POSITIONS * 2]


def _recheck_fill(order_id, symbol: str, settle: float = 1.0) -> tuple:
    """After a buy is cancelled (it was still working when the entry budget ran
    out, or the broker reported it terminal), decide whether it ACTUALLY filled.
    The cancel can race a late fill, so a market buy may report status='canceled'
    WITH a real filled_avg_price/filled_qty — which used to be dropped, leaving an
    unprotected, untracked orphan position (MRVL, 06/11).

    Returns (qty, price) if the shares are genuinely held, else (None, None).
    Two independent sources are checked because order-status and position
    propagation each lag a cycle right after a fill:
      1. the order's own fill fields (a 'canceled' order can still carry a fill);
      2. the live open position (the surest proof shares are actually held).
    """
    time.sleep(settle)  # let the fill/cancel cross settle on the broker side
    try:
        o = client.get_order_by_id(order_id)
        if o.filled_avg_price and float(o.filled_qty or 0) > 0:
            return float(o.filled_qty), float(o.filled_avg_price)
    except Exception:
        pass
    try:
        pos = client.get_open_position(symbol)
        qty   = float(getattr(pos, "qty", 0) or 0)
        price = float(getattr(pos, "avg_entry_price", 0) or 0)
        if qty > 0 and price > 0:
            return qty, price
    except Exception:
        # get_open_position raises when there is no position — that IS the flat case.
        pass
    return None, None


# Entry-cycle pacing (module-level so tests can shrink them). We submit bare market
# BUYs for up to MAX_POSITIONS names at once and POLL them for fills, attaching each
# OCO exit as its buy fills — rather than waiting on one order at a time. A market
# DAY order fills on its own as soon as the IEX feed catches up, so a still-working
# order is NEVER cancelled before the whole-cycle budget runs out (the 2026-06-15
# 0/5 open: every buy was cancelled at 15s and re-submitted, so none ever lived long
# enough to fill). The budget keeps the cycle well short of the 10:30 entry cutoff.
ENTRY_BUDGET_S = 300.0
# Poll every 2s, not faster: up to MAX_POSITIONS buys are polled concurrently, so at
# 1s we'd hit ~MAX*60/min get_order calls and risk Alpaca's ~200 req/min limit during
# a sustained lag. 2s keeps us comfortably under it; a couple seconds' latency to spot
# a fill and attach its (server-side) OCO is immaterial.
ENTRY_POLL_S   = 2.0


def _place_bracket_orders(candidates: list, per_pos_buying_power: float,
                          budget_s: float = ENTRY_BUDGET_S,
                          poll_s: float = ENTRY_POLL_S) -> tuple:
    """Enter up to MAX_POSITIONS positions, each protected by an OCO exit.

    TWO-STEP ENTRY — this is the fix for the recurring "only N of MAX filled"
    problem. We used to submit a single BRACKET market order whose TP/SL legs
    were priced off OUR quote, and let Alpaca validate those legs against ITS
    own base_price at submit time. When the two prices disagreed by more than the
    (tiny, ~1%) leg offset — routine in the volatile first minute — Alpaca
    rejected the WHOLE order:
      * our price below base -> TP < base+0.01  (the 06/08 MRVL rejection)
      * our price above base -> SL > base-0.01  (the 06/09 UNP/GS/SATS/EXPD/AVGO/JPM rejections)
    Six of ten candidates rejected on 06/09, so only four positions filled.

    Now we (1) submit a BARE market buy — no legs, so nothing for Alpaca to
    reject — (2) read the ACTUAL fill price, then (3) attach an OCO
    take-profit/stop-loss exit priced off that real fill. The exit legs straddle
    the live market (TP above, SL below) so they are always valid; there is no
    second price left to disagree with. Risk per position stays exactly
    PER_POSITION_TARGET / PER_POSITION_STOP because the legs are computed from the
    genuine fill, not a guess.

    Backfill is preserved: a candidate whose buy is rejected at submit, fills but
    can't be protected, or is terminally cancelled by the broker is dropped and we
    advance to the next ranked name until MAX_POSITIONS fill or the list runs out.

    Returns (bought_symbols, sl_order_ids) where sl_order_ids[symbol] is the STOP
    leg id so step_trailing_stops can ratchet it later.
    """
    bought = []
    sl_order_ids = {}
    pending = {}              # working buy order_id -> (symbol, stock)
    next_idx = 0              # index of the next candidate to submit
    deadline = time.time() + budget_s

    def _protect(symbol, stock, fill_qty, fill_price) -> bool:
        """Attach an OCO exit priced off the REAL fill (TP above, SL below, so it
        always straddles the market and can't be rejected for leg pricing) and
        record the STOP leg id for the trailing-stop ratchet. Returns True if the
        position is protected and tracked; False if the exit was rejected — in which
        case we CLOSE the position rather than ever carry it unprotected, freeing the
        slot for backfill."""
        take_profit_price, stop_price = compute_bracket_prices(fill_price, fill_qty)
        # Plain stop-MARKET (not stop-LIMIT): a market stop ALWAYS fills, so the position
        # can't ride unprotected past its stop. A stop-LIMIT (tried 2026-06-30) caps
        # slippage but DOESN'T fill when a volatile name gaps through its limit — ALGT/RRX
        # stuck at -$37/-$44 with limit sells resting below the market. The liquidity floor
        # already bounds market-stop slippage to a few $; check_pnl's hard-stop is the
        # backstop if a server-side stop still fails to flatten.
        try:
            exit_order = client.submit_order(LimitOrderRequest(
                symbol=symbol,
                qty=int(fill_qty),
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                order_class=OrderClass.OCO,
                limit_price=take_profit_price,
                take_profit=TakeProfitRequest(limit_price=take_profit_price),
                stop_loss=StopLossRequest(stop_price=stop_price),
            ))
        except Exception as e:
            log.error(f"Order failed {symbol}: OCO exit rejected ({e}) — closing position for safety and advancing")
            try:
                client.close_position(symbol)
            except Exception as ce:
                log.error(f"  {symbol}: FAILED to close unprotected position: {ce}")
            return False
        # The STOP leg (not the TP-limit parent) is what step_trailing_stops ratchets.
        # order_type is an OrderType enum; OrderType is a (str, Enum) so == matches
        # whether the API hands back the enum or a plain "stop" string.
        sl_leg = next(
            (o for o in [exit_order, *(exit_order.legs or [])]
             if getattr(o, "order_type", None) in (OrderType.STOP, OrderType.STOP_LIMIT)),
            None,
        )
        if sl_leg:
            sl_order_ids[symbol] = str(sl_leg.id)
        else:
            log.warning(f"  {symbol}: OCO stop leg not returned — step stops disabled for this position")
        log.info(f"BUY {int(fill_qty)}x {symbol} @ ${fill_price:.2f} | TP ${take_profit_price} | SL ${stop_price} | gap {stock['change_pct']}% | relvol {stock['rel_vol']}x | voltren {stock.get('vol_trend','?')}x")
        bought.append(symbol)
        return True

    def _fill_open_slots():
        """Submit BARE market buys (no legs -> nothing for Alpaca to reject) until
        filled + still-working == MAX_POSITIONS, or the candidate list runs out. A
        submit-time rejection is skipped so the next ranked name backfills its slot.
        Capping at MAX_POSITIONS means we never overbuy even though buys work
        concurrently."""
        nonlocal next_idx
        held = set(bought) | {s for s, _ in pending.values()}
        while next_idx < len(candidates) and (len(bought) + len(pending)) < MAX_POSITIONS:
            stock = candidates[next_idx]
            next_idx += 1
            symbol = stock["symbol"]
            if symbol in held:
                continue
            qty = position_size(stock["price"], per_pos_buying_power)
            try:
                buy = client.submit_order(MarketOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                ))
            except Exception as e:
                log.error(f"Order failed {symbol}: {e} — advancing to next candidate")
                continue
            pending[buy.id] = (symbol, stock)
            held.add(symbol)

    _fill_open_slots()
    # Poll the working buys; protect each as it fills and backfill any slot a
    # terminal rejection frees. A still-working order is LEFT ALONE — it fills on
    # its own once the feed catches up — never cancelled mid-cycle.
    while pending and len(bought) < MAX_POSITIONS and time.time() < deadline:
        time.sleep(poll_s)
        for oid, (symbol, stock) in list(pending.items()):
            try:
                o = client.get_order_by_id(oid)
            except Exception:
                continue   # transient read error — check again next poll
            status = getattr(o.status, "value", o.status)
            if status == "filled" and o.filled_avg_price and float(o.filled_qty or 0) > 0:
                del pending[oid]
                _protect(symbol, stock, float(o.filled_qty), float(o.filled_avg_price))
            elif status in ("canceled", "rejected", "expired"):
                # Terminal non-fill. A 'canceled' can still carry a late fill, so
                # RECHECK before giving up (the 06/11 orphan); otherwise flatten any
                # stray partial and let the freed slot backfill below.
                del pending[oid]
                fill_qty, fill_price = _recheck_fill(oid, symbol)
                if fill_price:
                    _protect(symbol, stock, fill_qty, fill_price)
                else:
                    log.warning(f"{symbol}: buy {status} without filling — advancing to next candidate")
                    try:
                        client.close_position(symbol)
                    except Exception:
                        pass
            # else: still working — leave it; a market DAY order fills on its own.
        _fill_open_slots()   # backfill slots freed by terminal rejections

    # Budget reached (or candidates exhausted) with buys still working: cancel each
    # straggler, then RE-CHECK for a fill that raced the cancel so we never strand an
    # unprotected orphan. Protect a recovered fill only while we are under the cap;
    # otherwise (or if it never filled) close it to stay flat.
    if pending:
        log.warning(f"Entry cycle ending with {len(pending)} buy(s) still unfilled — cancelling and reconciling.")
    for oid, (symbol, stock) in list(pending.items()):
        del pending[oid]
        try:
            client.cancel_order_by_id(oid)
        except Exception:
            pass
        fill_qty, fill_price = _recheck_fill(oid, symbol)
        if fill_price and len(bought) < MAX_POSITIONS:
            _protect(symbol, stock, fill_qty, fill_price)
        else:
            try:
                client.close_position(symbol)
            except Exception:
                pass

    log.info(f"Entry cycle complete — {len(bought)}/{MAX_POSITIONS} filled.")
    return bought, sl_order_ids


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

    bought, sl_order_ids = _place_bracket_orders(top, per_pos_buying_power)

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



def close_all(max_attempts: int = 5):
    """Close ALL positions and VERIFY flat, retrying on transient failure.

    A single close that hits a network blip (e.g. the recurring 15:45 DNS one)
    would leave positions carried overnight with EXPIRED day-OCOs — i.e. an
    unprotected overnight gap. Never carry overnight: retry the close until a
    position fetch confirms flat. Returns True if confirmed flat."""
    for attempt in range(max_attempts):
        try:
            client.close_all_positions(cancel_orders=True)
        except Exception as e:
            log.warning(f"close_all attempt {attempt + 1}/{max_attempts}: {e}")
        try:
            remaining = client.get_all_positions()
        except Exception:
            remaining = None  # couldn't verify this attempt
        if remaining is not None and len(remaining) == 0:
            log.info("All positions closed (verified flat).")
            return True
        if attempt < max_attempts - 1:
            time.sleep(5)  # let the close propagate / let a transient blip clear
    log.error("close_all: could not confirm flat after retries — positions may remain.")
    return False


def step_trailing_stops(positions=None):
    """Ratchet each position's stop-loss order up through the STOP_STEPS ladder.
    `positions` may be supplied by the caller (check_pnl) to avoid re-fetching."""
    state = load_state()
    sl_order_ids     = state.get("sl_order_ids", {})
    steps_reached    = state.get("stop_steps_reached", {})
    if not sl_order_ids:
        return

    updated = False
    if positions is None:
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


def enforce_per_position_hard_stop(positions):
    """Safety net: market-close any held position whose unrealized P&L has fallen to
    PER_POSITION_HARD_STOP. The protective bracket is a stop-MARKET that normally fills
    near the -$20 stop, but a server-side stop can rarely fail to flatten — a partial
    fill on a fast gap, or one that never triggers — leaving the position riding
    unprotected (ALGT/RRX hit -$37/-$44 on 2026-06-30 when the stop-LIMIT experiment's
    limit sells couldn't fill). This is broker-order-independent: it acts purely on the
    live unrealized P&L, so an exit is guaranteed no matter what the resting order does.

    Cancels the symbol's open orders first (so the now-stale OCO can't later try to sell
    shares we no longer hold), then closes at market. sync_bracket_fills records the P&L
    on a later cycle once the close settles. Returns the list of symbols force-closed.

    GOTCHA (HONA, 2026-07-06): this runs every ~4s cycle. A market close doesn't settle
    instantly, so the position can still show here (still P&L <= hard stop) on the very
    next cycle while its close order is still working. Canceling that live order to
    "retry" just re-submits against a falling stock at a worse price every cycle — HONA's
    -$30 hard stop realized as -$52.80 after ~35 cancel/resubmit cycles over 3 minutes.
    So: if a market sell is already open for the symbol, leave it alone and wait."""
    closed = []
    for pos in positions:
        try:
            pl = float(pos.unrealized_pl)
        except (TypeError, ValueError):
            continue
        if pl > PER_POSITION_HARD_STOP:
            continue
        symbol = pos.symbol
        try:
            open_orders = list(client.get_orders(GetOrdersRequest(
                    status=QueryOrderStatus.OPEN, symbols=[symbol], limit=20)))
        except Exception as e:
            log.warning(f"  {symbol}: could not list open orders ({e}) — skipping this cycle.")
            continue
        if any(o.type == OrderType.MARKET and o.side == OrderSide.SELL for o in open_orders):
            log.info(f"  {symbol}: hard-stop close already working — waiting for it to fill.")
            continue
        log.warning(f"HARD STOP {symbol}: P&L ${pl:+.2f} <= ${PER_POSITION_HARD_STOP:.0f} "
                    f"— protective stop failed to flatten; force-closing at market.")
        # Cancel this symbol's resting (stuck) sell orders, then market-close.
        for o in open_orders:
            try:
                client.cancel_order_by_id(o.id)
            except Exception:
                pass
        try:
            client.close_position(symbol)
            closed.append(symbol)
        except Exception as e:
            log.error(f"  {symbol}: HARD STOP close failed: {e}")
    return closed


def sync_bracket_fills(positions=None):
    """Detect positions closed by bracket/OCO orders and record their P&L.
    `positions` may be supplied by the caller (check_pnl) to avoid re-fetching."""
    state = load_state()
    tracked = set(state.get("positions", []))
    if not tracked:
        return
    if positions is None:
        try:
            positions = client.get_all_positions()
        except Exception:
            return
    open_syms = {p.symbol for p in positions}
    closed_syms = tracked - open_syms
    if not closed_syms:
        return

    today = datetime.now(ET).date()
    recorded = {t["symbol"] for t in state.get("trades_today", [])}

    newly_recorded = set()
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
                newly_recorded.add(symbol)
            else:
                # The position is gone but the broker's CLOSED-orders query hasn't
                # yet returned BOTH a filled buy and a filled sell — order-status
                # propagation lags the position disappearing by a cycle or two right
                # after a fill. Do NOT drop the symbol here: leaving it in
                # `positions` lets the next 5s cycle retry. Unconditionally dropping
                # every closed symbol (recorded or not) was the bug that silently
                # lost trades from the daily performance log.
                log.info(f"sync_bracket_fills: {symbol} closed but fills not settled yet — retrying next cycle")
        except Exception as e:
            log.warning(f"sync_bracket_fills: could not record {symbol}: {e}")

    # Drop a symbol from `positions` only once its trade is actually recorded (now
    # or on an earlier cycle). Still-open symbols and closed-but-unrecorded symbols
    # both stay tracked, so nothing is lost. (Any genuinely unmatchable straggler is
    # cleared by the EOD close / next-day daily_reset, so this can't leak forever.)
    done = recorded | newly_recorded
    state = load_state()
    state["positions"] = [s for s in state.get("positions", []) if s not in done]
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

    # Fetch open positions ONCE and share the snapshot with the fill-sync and
    # trailing-stop steps (they each used to re-fetch — 3 calls every 5s). On a
    # fetch failure, skip those steps but STILL run the equity-based loss-limit
    # check below — the kill switch must not depend on the positions call.
    try:
        positions = client.get_all_positions()
    except Exception as e:
        log.warning(f"check_pnl: position fetch failed ({e}) — stop/fill sync skipped this cycle.")
        positions = None

    if positions is not None:
        sync_bracket_fills(positions)
        # Safety net BEFORE ratcheting: if any position has blown past its stop (the
        # broker's protective order failed to flatten), force-close it now rather than
        # ratchet/monitor a position that should already be gone.
        forced = enforce_per_position_hard_stop(positions)
        if forced:
            positions = [p for p in positions if p.symbol not in forced]
        step_trailing_stops(positions)

    daily_pnl = get_daily_pnl()
    log.info(f"Daily P&L: ${daily_pnl:+.2f}  (limit ${DAILY_LOSS_LIMIT:.0f} | target ${DAILY_TARGET:.0f})")

    if positions is not None:
        steps_reached = load_state().get("stop_steps_reached", {})
        for pos in positions:
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
            with open(PERF_FILE, encoding="utf-8") as f:
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
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)
    os.replace(tmp, PERF_FILE)


def eod_close():
    global trading_active
    # This fires every calendar day, but the market is only open on trading days.
    # At 15:45 ET a normal session is still open (closes 16:00), so is_open cleanly
    # identifies trading days. RETRY the clock check: a transient DNS blip recurs on
    # this host at ~15:45:00 (06/05, 06/10, 06/11 all failed identically), and a
    # single attempt lost the day's perf row AND skipped the safety close. DNS
    # recovers within ~60s, so retry for a couple of minutes before giving up.
    clock = None
    for attempt in range(8):
        try:
            clock = client.get_clock()
            break
        except Exception as e:
            log.warning(f"EOD: clock check failed (attempt {attempt + 1}/8) — retrying in 15s: {e}")
            time.sleep(15)
    market_confirmed_open = False
    if clock is not None:
        if not clock.is_open:
            log.info("EOD: non-trading day (weekend/holiday) — skipping summary.")
            persist_halted(False)
            trading_active = False
            return
        market_confirmed_open = True
    else:
        # Still unreachable after retries. Close positions for safety (never carry
        # overnight), but DON'T write a performance row — a weekend/holiday blip
        # would append a spurious 0-trade "WIN" for a non-trading day.
        log.warning("EOD: clock unreachable after retries — closing for safety, skipping summary.")

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
    if market_confirmed_open:
        try:
            log_daily_performance()
        except Exception as e:
            log.error(f"EOD: failed to write performance log ({e}) — continuing shutdown.")
    persist_halted(False)  # Reset for next day — write before flipping memory flag
    trading_active = False


def daily_reset():
    """Reset state at the start of each trading day."""
    global trading_active
    # RETRY the clock check: the same transient DNS blip that hits eod_close (see
    # there) could land at 09:29 and skip the reset, leaving entries_done=True from
    # yesterday so the bot never enters that day. DNS recovers within ~60s.
    clock = None
    for attempt in range(8):
        try:
            clock = client.get_clock()
            break
        except Exception as e:
            log.warning(f"daily_reset: clock check failed (attempt {attempt + 1}/8) — retrying in 15s: {e}")
            time.sleep(15)
    if clock is None:
        log.error("daily_reset: could not reach Alpaca after retries — skipping reset.")
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
        # Couldn't list them, but a blip here must NOT mean we skip the close and let
        # stale positions block today's entry. Force a (verified) close anyway.
        log.warning(f"close_overnight: could not list positions ({e}) — forcing close anyway.")
        close_all()
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
schedule.every().day.at("09:30", TZ).do(close_overnight)   # Close any overnight positions (at the open, before entry)
schedule.every(4).seconds.do(check_pnl)                    # P&L check every 4 sec
schedule.every().day.at("15:45", TZ).do(eod_close)         # Force-close before EOD

# First entry fires at 9:31 — the earliest the market is reliably open with real
# gap data — so the ~3.5 min scan (now ~1,521 tickers) starts ASAP and fills land
# ~9:34:30 instead of ~9:35:30. The scan can't run pre-open (orders are rejected
# before 9:30 and there's no opening-gap data yet), so 9:31 is the earliest valid
# start. Later windows are backups: they no-op once positions are held or after 10:30.
for _t in ["09:31", "09:48", "10:03", "10:18"]:
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

    # Catchup: if we start/restart during the trading window (after the 09:31 first
    # entry, before EOD) and hold no positions, run open_positions() immediately so a
    # restart never causes us to sit out the whole day.
    # Skip if trading was halted earlier today.
    now_et = datetime.now(ET)
    market_open_time = now_et.replace(hour=9, minute=31, second=0, microsecond=0)
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
        time.sleep(1)  # 1s granularity so check_pnl fires every ~4s
