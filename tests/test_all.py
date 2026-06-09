"""
Regression suite for the trading bot.

Run:  python tests/test_all.py        (no pytest needed — stdlib unittest)

Every invariant discovered during code review lives here so a regression is
caught by running ONE command instead of being re-discovered by hand. Tests are
hermetic: no network, no broker calls, no real order placement. Anything that
would touch Alpaca or yfinance is mocked.
"""
import os
import sys
import json
import logging
import tempfile
import unittest
import datetime as dt
from unittest import mock

import pytz

# Importing bot configures logging to write to the REAL bot.log. Suppress all
# logging here so the suite never pollutes the production log (step_watch tails
# bot.log and would otherwise fire false toasts on test-generated lines).
logging.disable(logging.CRITICAL)

# Make the project root importable when run from anywhere.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

ET = pytz.timezone("America/New_York")

import trading_math as tm
from config import STOP_STEPS, PER_POSITION_TARGET, PER_POSITION_STOP, MAX_POSITIONS


# ──────────────────────────────────────────────────────────────────────────────
# 1. Pure trading math
# ──────────────────────────────────────────────────────────────────────────────
class TestPositionSize(unittest.TestCase):
    def test_zero_or_negative_price_returns_one(self):
        self.assertEqual(tm.position_size(0, 10000), 1)
        self.assertEqual(tm.position_size(-5, 10000), 1)

    def test_always_at_least_one(self):
        # Price larger than the whole budget -> still buy 1.
        self.assertEqual(tm.position_size(50000, 1000), 1)

    def test_affordability_cap(self):
        # $10 stock, $100 budget -> at most 10 shares (target wants 200).
        self.assertEqual(tm.position_size(10, 100), 10)

    def test_target_shares_when_affordable(self):
        # shares_for_target = 2000/price; budget is ample.
        self.assertEqual(tm.position_size(100, 1_000_000), int(2000 / 100))


class TestBracketPrices(unittest.TestCase):
    def test_invariant_sl_lt_price_lt_tp_across_range(self):
        for price in [0.40, 1.00, 5.0, 12.34, 50.0, 200.0, 1500.0]:
            for qty in [1, 5, 40, 1000, 5000]:
                tp, sl = tm.compute_bracket_prices(price, qty)
                self.assertLess(sl, price, f"SL not below price @ {price}/{qty}")
                self.assertLess(price, tp, f"TP not above price @ {price}/{qty}")
                # At least a 1-cent gap (allow tiny float slack).
                self.assertGreaterEqual(round(tp - price, 2), 0.01, f"TP gap @ {price}/{qty}")
                self.assertGreaterEqual(round(price - sl, 2), 0.01, f"SL gap @ {price}/{qty}")

    def test_qty_zero_does_not_crash(self):
        tp, sl = tm.compute_bracket_prices(10.0, 0)
        self.assertLess(sl, 10.0)
        self.assertLess(10.0, tp)

    def test_normal_dollar_targets(self):
        # qty 20 -> $1 each side.
        tp, sl = tm.compute_bracket_prices(100.0, 20)
        self.assertEqual(tp, 101.0)
        self.assertEqual(sl, 99.0)


class TestStopLadder(unittest.TestCase):
    def test_below_first_trigger_returns_none(self):
        self.assertIsNone(tm.select_stop_pl(4.99, -1))

    def test_first_step_breakeven(self):
        self.assertEqual(tm.select_stop_pl(5, -1), 0)

    def test_skips_to_highest_earned(self):
        # Jumped to +12 from start -> lock +5 (the $10 trigger), skipping breakeven.
        self.assertEqual(tm.select_stop_pl(12, -1), 5)

    def test_no_double_apply_same_level(self):
        # Already locked at +0; pl still only earns +0 -> nothing new.
        self.assertIsNone(tm.select_stop_pl(5, 0))

    def test_monotonic_progression(self):
        self.assertEqual(tm.select_stop_pl(20, 5), 10)
        self.assertIsNone(tm.select_stop_pl(20, 10))  # top of ladder already locked


class TestClassifyTrades(unittest.TestCase):
    def test_breakeven_trade_is_win(self):
        c = tm.classify_trades([{"symbol": "X", "pl": 0.0}], 0.0)
        self.assertEqual(c["wins"], 1)
        self.assertEqual(c["losses"], 0)

    def test_breakeven_day_is_win(self):
        self.assertEqual(tm.classify_trades([], 0.0)["result"], "WIN")

    def test_loss_day(self):
        self.assertEqual(tm.classify_trades([], -10.0)["result"], "LOSS")

    def test_mixed(self):
        trades = [{"symbol": "A", "pl": 10}, {"symbol": "B", "pl": -4}, {"symbol": "C", "pl": 0}]
        c = tm.classify_trades(trades, 6.0)
        self.assertEqual((c["wins"], c["losses"], c["trades"]), (2, 1, 3))
        self.assertEqual(c["best_trade"], 10)
        self.assertEqual(c["worst_trade"], -4)
        self.assertEqual(c["win_rate"], round(2 / 3 * 100, 1))


# ──────────────────────────────────────────────────────────────────────────────
# 2. bot.py behaviour (mocked broker; no network)
# ──────────────────────────────────────────────────────────────────────────────
def _fake_clock(is_open, next_open=None, next_close=None):
    m = mock.MagicMock()
    m.is_open = is_open
    m.next_open = next_open
    m.next_close = next_close
    return m


class TestSchedulesAreEasternPinned(unittest.TestCase):
    """The riskiest change: jobs must fire at the right ET wall-clock minute."""
    @classmethod
    def setUpClass(cls):
        import bot  # registers all schedule jobs at import
        import schedule
        cls.schedule = schedule
        cls.local_tz = dt.datetime.now().astimezone().tzinfo

    def _et_minutes_for(self, func_name):
        out = set()
        for j in self.schedule.jobs:
            f = getattr(j.job_func, "func", None)
            if f is not None and f.__name__ == func_name:
                nr_et = j.next_run.replace(tzinfo=self.local_tz).astimezone(ET)
                out.add((nr_et.hour, nr_et.minute))
        return out

    def test_entry_windows(self):
        self.assertEqual(self._et_minutes_for("open_positions"),
                         {(9, 32), (9, 48), (10, 3), (10, 18)})

    def test_reset_overnight_eod(self):
        self.assertEqual(self._et_minutes_for("daily_reset"), {(9, 29)})
        self.assertEqual(self._et_minutes_for("close_overnight"), {(9, 31)})
        self.assertEqual(self._et_minutes_for("eod_close"), {(15, 45)})


class TestEodWeekendGuard(unittest.TestCase):
    def test_skips_perf_log_when_market_closed(self):
        import bot
        with mock.patch.object(bot, "client") as c, \
             mock.patch.object(bot, "log_daily_performance") as perf, \
             mock.patch.object(bot, "close_all") as close_all, \
             mock.patch.object(bot, "sync_bracket_fills"), \
             mock.patch.object(bot, "persist_halted"):
            c.get_clock.return_value = _fake_clock(is_open=False)
            bot.trading_active = True
            bot.eod_close()
            perf.assert_not_called()

    def test_runs_full_eod_on_trading_day(self):
        import bot
        with mock.patch.object(bot, "client") as c, \
             mock.patch.object(bot, "log_daily_performance") as perf, \
             mock.patch.object(bot, "close_all") as close_all, \
             mock.patch.object(bot, "sync_bracket_fills"), \
             mock.patch.object(bot, "persist_halted"):
            c.get_clock.return_value = _fake_clock(is_open=True)
            c.get_all_positions.return_value = []
            bot.trading_active = True
            bot.eod_close()
            perf.assert_called_once()
            close_all.assert_called_once()

    def test_clock_failure_closes_but_skips_summary(self):
        # A clock-check blip must still force-close (safety) but must NOT write a
        # performance row (we can't confirm it's a trading day -> no spurious entry).
        import bot
        with mock.patch.object(bot, "client") as c, \
             mock.patch.object(bot, "log_daily_performance") as perf, \
             mock.patch.object(bot, "close_all") as close_all, \
             mock.patch.object(bot, "sync_bracket_fills"), \
             mock.patch.object(bot, "persist_halted"):
            c.get_clock.side_effect = RuntimeError("network")
            c.get_all_positions.return_value = []
            bot.trading_active = True
            bot.eod_close()
            close_all.assert_called_once()
            perf.assert_not_called()


class TestDailyPnlBaseline(unittest.TestCase):
    def test_missing_baseline_uses_current_equity_not_last(self):
        # No baseline -> P&L 0 (current equity), NOT a huge loss vs yesterday's
        # close, which would false-trip the kill switch.
        import bot
        acct = mock.MagicMock(equity="50000", last_equity="40000")
        with mock.patch.object(bot, "client") as c, \
             mock.patch.object(bot, "load_state", return_value={}):
            c.get_account.return_value = acct
            self.assertEqual(bot.get_daily_pnl(), 0.0)

    def test_uses_baseline_when_present(self):
        import bot
        acct = mock.MagicMock(equity="50200", last_equity="40000")
        with mock.patch.object(bot, "client") as c, \
             mock.patch.object(bot, "load_state", return_value={"session_baseline": 50000.0}):
            c.get_account.return_value = acct
            self.assertEqual(round(bot.get_daily_pnl(), 2), 200.0)


class TestCheckPnl(unittest.TestCase):
    def test_fetches_positions_once(self):
        # check_pnl must fetch get_all_positions ONCE and share it with the fill
        # sync + trailing-stop steps (it used to fetch 3x every 5s).
        import bot
        state = {"positions": ["AAA"], "sl_order_ids": {"AAA": "id1"},
                 "stop_steps_reached": {}, "trading_halted": False}
        pos = mock.MagicMock(symbol="AAA", unrealized_pl="1.0", qty="10", avg_entry_price="100")
        with mock.patch.object(bot, "client") as c, \
             mock.patch.object(bot, "is_market_open", return_value=True), \
             mock.patch.object(bot, "get_daily_pnl", return_value=0.0), \
             mock.patch.object(bot, "load_state", return_value=state), \
             mock.patch.object(bot, "save_state"), \
             mock.patch.object(bot, "record_trade"):
            c.get_all_positions.return_value = [pos]
            c.get_orders.return_value = []
            bot.trading_active = True
            bot.check_pnl()
            self.assertEqual(c.get_all_positions.call_count, 1)

    def test_loss_limit_still_checked_when_position_fetch_fails(self):
        # A position-fetch failure must NOT disable the kill switch (it's
        # equity-based via get_daily_pnl, independent of get_all_positions).
        import bot
        with mock.patch.object(bot, "client") as c, \
             mock.patch.object(bot, "is_market_open", return_value=True), \
             mock.patch.object(bot, "get_daily_pnl", return_value=-500.0), \
             mock.patch.object(bot, "load_state", return_value={"trading_halted": False}), \
             mock.patch.object(bot, "close_all") as close_all, \
             mock.patch.object(bot, "persist_halted"):
            c.get_all_positions.side_effect = RuntimeError("network")
            bot.trading_active = True
            bot.check_pnl()
            close_all.assert_called_once()   # kill switch fired despite no positions


class TestServeTail(unittest.TestCase):
    def test_returns_last_n_lines_without_reading_whole_file(self):
        import serve
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.log")
            with open(p, "w") as f:
                for i in range(5000):
                    f.write(f"line{i}\n")
            out = serve.tail(p, n=25)
        self.assertEqual(len(out), 25)
        self.assertEqual(out[-1], "line4999")
        self.assertEqual(out[0], "line4975")

    def test_handles_small_and_missing_files(self):
        import serve
        self.assertEqual(serve.tail("/nonexistent/none.log"), [])
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.log")
            with open(p, "w") as f:
                f.write("only\nthree\nlines\n")
            self.assertEqual(serve.tail(p, n=25), ["only", "three", "lines"])


class TestDailyResetWeekendGuard(unittest.TestCase):
    def test_no_reset_on_non_trading_day(self):
        import bot
        future = dt.datetime.now(ET) + dt.timedelta(days=3)
        with mock.patch.object(bot, "client") as c, \
             mock.patch.object(bot, "reset_session") as reset:
            c.get_clock.return_value = _fake_clock(is_open=False, next_open=future)
            bot.daily_reset()
            reset.assert_not_called()


class TestStateRoundtrip(unittest.TestCase):
    def test_save_load_atomic(self):
        import bot
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state.json")
            with mock.patch.object(bot, "STATE_FILE", path):
                data = {"a": 1, "positions": ["X"], "session_baseline": 100.5}
                bot.save_state(data)
                self.assertEqual(bot.load_state(), data)
                # No leftover temp file.
                self.assertFalse(os.path.exists(path + ".tmp"))

    def test_load_missing_returns_empty(self):
        import bot
        with mock.patch.object(bot, "STATE_FILE", "/nonexistent/xyz.json"):
            self.assertEqual(bot.load_state(), {})


class TestPerformanceLogDedup(unittest.TestCase):
    def test_replaces_same_day_entry_and_sorts(self):
        import bot
        with tempfile.TemporaryDirectory() as d:
            perf = os.path.join(d, "perf.json")
            state = os.path.join(d, "state.json")
            seed = [{"date": "2026-01-01", "daily_pnl": 5.0, "result": "WIN"}]
            with open(perf, "w") as f:
                json.dump(seed, f)
            today = str(dt.datetime.now(ET).date())
            with mock.patch.object(bot, "PERF_FILE", perf), \
                 mock.patch.object(bot, "STATE_FILE", state), \
                 mock.patch.object(bot, "get_daily_pnl", return_value=42.0):
                bot.save_state({"trades_today": [{"symbol": "A", "pl": 42.0}]})
                bot.log_daily_performance()
                bot.log_daily_performance()  # run twice — must not duplicate today
            with open(perf) as f:
                hist = json.load(f)
            dates = [h["date"] for h in hist]
            self.assertEqual(dates, sorted(dates))
            self.assertEqual(dates.count(today), 1)
            today_entry = next(h for h in hist if h["date"] == today)
            self.assertEqual(today_entry["result"], "WIN")
            self.assertEqual(today_entry["daily_pnl"], 42.0)


class TestSpyFilter(unittest.TestCase):
    def _patch_history(self, closes):
        import pandas as pd
        df = pd.DataFrame({"Close": closes})
        tk = mock.MagicMock()
        tk.history.return_value = df
        return tk

    def test_down_day_blocks(self):
        import bot
        with mock.patch.object(bot.yf, "Ticker", return_value=self._patch_history([100, 98])):
            self.assertFalse(bot.spy_is_positive())

    def test_up_day_allows(self):
        import bot
        with mock.patch.object(bot.yf, "Ticker", return_value=self._patch_history([100, 101])):
            self.assertTrue(bot.spy_is_positive())

    def test_insufficient_data_allows(self):
        import bot
        with mock.patch.object(bot.yf, "Ticker", return_value=self._patch_history([100])):
            self.assertTrue(bot.spy_is_positive())

    def test_exception_allows(self):
        import bot
        bad = mock.MagicMock()
        bad.history.side_effect = RuntimeError("network")
        with mock.patch.object(bot.yf, "Ticker", return_value=bad):
            self.assertTrue(bot.spy_is_positive())


# ──────────────────────────────────────────────────────────────────────────────
# 3. Screener scoring/filter (mocked yfinance)
# ──────────────────────────────────────────────────────────────────────────────
class TestScreener(unittest.TestCase):
    def _fake_download(self):
        import pandas as pd, numpy as np
        idx = pd.date_range("2026-04-20", periods=30, freq="D")
        fields = ["Open", "High", "Low", "Close", "Volume"]
        cols = pd.MultiIndex.from_product([["UP", "DOWN"], fields])
        raw = pd.DataFrame(index=idx, columns=cols, dtype=float)
        up_close = np.linspace(100, 140, 30)      # strong uptrend (+~1%/day at end)
        up_vol = np.linspace(1e6, 3e6, 30)        # rising volume
        dn_close = np.linspace(140, 100, 30)      # downtrend
        dn_vol = np.linspace(3e6, 1e6, 30)
        for f in fields:
            raw[("UP", f)] = up_close if f != "Volume" else up_vol
            raw[("DOWN", f)] = dn_close if f != "Volume" else dn_vol
        return raw

    def test_filters_direction_and_scores(self):
        import screener
        with mock.patch.object(screener, "get_universe", return_value=["UP", "DOWN"]), \
             mock.patch.object(screener.yf, "download", return_value=self._fake_download()):
            top = screener.get_top_momentum(50)
        syms = [c["symbol"] for c in top]
        self.assertIn("UP", syms)        # up + rising vol passes
        self.assertNotIn("DOWN", syms)   # down day filtered out
        # All returned candidates expose the keys bot.py logs.
        for c in top:
            for k in ("symbol", "price", "change_pct", "rel_vol", "vol_trend", "score"):
                self.assertIn(k, c)


# ──────────────────────────────────────────────────────────────────────────────
# 3b. Order placement: two-step entry (bare market buy -> OCO exit priced off the
#     REAL fill) with backfill on any failure.
#
# This replaces the old single-BRACKET path. A bracket's TP/SL legs were priced
# off our pre-trade quote and validated by Alpaca against ITS base_price; when the
# two disagreed by more than the ~1% leg offset (routine at the open) Alpaca
# rejected the WHOLE order, so only a few of MAX_POSITIONS filled (4/10 on 06/09).
# The two-step flow has no second price to disagree with: the exit straddles the
# live market, computed from the genuine fill.
# ──────────────────────────────────────────────────────────────────────────────
class TestTwoStepEntryAndBackfill(unittest.TestCase):
    def _candidates(self, n):
        return [{"symbol": f"S{i}", "price": 100.0, "change_pct": 1.0,
                 "rel_vol": 2.0, "vol_trend": 1.0} for i in range(n)]

    def _fake_buy(self, symbol):
        return mock.MagicMock(id=f"buy-{symbol}")

    def _filled(self, qty=10, price=100.0):
        # A fully-filled buy order as get_order_by_id would return it.
        return mock.MagicMock(status=mock.MagicMock(value="filled"),
                              filled_qty=str(qty), filled_avg_price=str(price))

    def _fake_oco(self, symbol):
        # Mirror a REAL Alpaca OCO response: the PARENT order is the take-profit
        # LIMIT; the stop-loss is a child leg whose order_type is the STOP enum
        # (NOT a plain "stop" string — that distinction hid the old dead-ladder bug).
        from alpaca.trading.enums import OrderType, OrderSide
        stop = mock.MagicMock(order_type=OrderType.STOP, id=f"sl-{symbol}", side=OrderSide.SELL)
        return mock.MagicMock(order_type=OrderType.LIMIT, id=f"tp-{symbol}", legs=[stop])

    def _wire(self, c, buy_ok=lambda s: True, oco_ok=lambda s: True):
        """Configure a mocked client for the two-step flow. buy_ok / oco_ok
        returning False raises on that step to simulate a rejection."""
        from alpaca.trading.enums import OrderSide

        def submit(req):
            if req.side == OrderSide.BUY:
                if not buy_ok(req.symbol):
                    raise RuntimeError("buy rejected")
                return self._fake_buy(req.symbol)
            if not oco_ok(req.symbol):           # SELL == the OCO exit
                raise RuntimeError("stop_loss.stop_price must be <= base_price - 0.01")
            return self._fake_oco(req.symbol)

        c.submit_order.side_effect = submit
        c.get_order_by_id.side_effect = lambda oid: self._filled()

    def test_captures_stop_loss_leg_id(self):
        # The STOP leg id (not the TP-limit parent) must be captured so
        # step_trailing_stops can ratchet it later.
        import bot
        with mock.patch.object(bot, "client") as c:
            self._wire(c)
            bought, sl_ids = bot._place_bracket_orders(self._candidates(1), 1_000_000)
        self.assertEqual(bought, ["S0"])
        self.assertEqual(sl_ids, {"S0": "sl-S0"})   # the STOP leg id, not "tp-S0"

    def test_exit_priced_off_real_fill_not_scan_price(self):
        # The core of the fix: TP/SL come from the ACTUAL fill (250.0), never the
        # scan price (100.0), and straddle it — so Alpaca can't reject them.
        import bot
        from alpaca.trading.enums import OrderSide
        captured = {}

        def submit(req):
            if req.side == OrderSide.BUY:
                return self._fake_buy(req.symbol)
            captured["tp"] = float(req.take_profit.limit_price)
            captured["sl"] = float(req.stop_loss.stop_price)
            return self._fake_oco(req.symbol)

        with mock.patch.object(bot, "client") as c:
            c.submit_order.side_effect = submit
            c.get_order_by_id.side_effect = lambda oid: self._filled(qty=10, price=250.0)
            bot._place_bracket_orders(self._candidates(1), 1_000_000)
        self.assertGreater(captured["tp"], 250.0)   # take-profit above the fill
        self.assertLess(captured["sl"], 250.0)      # stop below the fill

    def test_backfill_skips_rejected_buy(self):
        # Top pick's buy is rejected -> still fill MAX_POSITIONS off the spares.
        import bot
        cands = self._candidates(MAX_POSITIONS + 1)
        with mock.patch.object(bot, "client") as c:
            self._wire(c, buy_ok=lambda s: s != "S0")
            bought, sl_ids = bot._place_bracket_orders(cands, 1_000_000)
        self.assertEqual(len(bought), MAX_POSITIONS)   # not short despite S0 failing
        self.assertNotIn("S0", bought)
        self.assertEqual(set(sl_ids), set(bought))     # every fill tracked its SL leg

    def test_oco_rejection_closes_position_and_backfills(self):
        # If the protective exit is rejected we must NEVER carry the position
        # unprotected: close it, then backfill to the next candidate.
        import bot
        cands = self._candidates(MAX_POSITIONS + 1)
        with mock.patch.object(bot, "client") as c:
            self._wire(c, oco_ok=lambda s: s != "S0")
            bought, sl_ids = bot._place_bracket_orders(cands, 1_000_000)
        self.assertEqual(len(bought), MAX_POSITIONS)
        self.assertNotIn("S0", bought)
        c.close_position.assert_called_once_with("S0")   # flattened, never carried

    def test_unfilled_buy_is_cancelled_and_skipped(self):
        # A buy that never reaches 'filled' is cancelled and skipped (no orphan).
        import bot
        from alpaca.trading.enums import OrderSide

        def submit(req):
            if req.side == OrderSide.BUY:
                return self._fake_buy(req.symbol)
            return self._fake_oco(req.symbol)

        with mock.patch.object(bot, "client") as c:
            c.submit_order.side_effect = submit
            c.get_order_by_id.side_effect = lambda oid: mock.MagicMock(
                status=mock.MagicMock(value="rejected"), filled_qty="0", filled_avg_price=None)
            bought, sl_ids = bot._place_bracket_orders(self._candidates(1), 1_000_000)
        self.assertEqual(bought, [])
        c.cancel_order_by_id.assert_called_once()

    def test_caps_at_max_positions(self):
        import bot
        cands = self._candidates(MAX_POSITIONS * 2)
        with mock.patch.object(bot, "client") as c:
            self._wire(c)
            bought, _ = bot._place_bracket_orders(cands, 1_000_000)
        self.assertEqual(len(bought), MAX_POSITIONS)   # never overbuys past the cap

    def test_all_rejected_returns_empty(self):
        import bot
        with mock.patch.object(bot, "client") as c:
            self._wire(c, buy_ok=lambda s: False)
            bought, sl_ids = bot._place_bracket_orders(self._candidates(3), 1_000_000)
        self.assertEqual(bought, [])
        self.assertEqual(sl_ids, {})

    def test_scan_returns_backfill_candidates(self):
        import bot
        fake = [{"symbol": f"S{i}", "change_pct": i} for i in range(50)]
        with mock.patch.object(bot, "get_top_momentum", return_value=fake):
            out = bot.scan_momentum()
        self.assertEqual(out, fake[:MAX_POSITIONS * 2])  # ranked spares kept for backfill


# ──────────────────────────────────────────────────────────────────────────────
# 3c. Trade recording: a closed position must never be dropped from `positions`
#     until its P&L is actually recorded (the 06/09 lost-trades bug).
# ──────────────────────────────────────────────────────────────────────────────
class TestSyncBracketFillsRecording(unittest.TestCase):
    def _state_backed(self, store):
        import copy
        def load():
            return copy.deepcopy(store)
        def save(d):
            store.clear(); store.update(copy.deepcopy(d))
        return load, save

    def _order(self, side, price, qty):
        import datetime as dt, pytz
        ET = pytz.timezone("America/New_York")
        o = mock.MagicMock()
        o.side = mock.MagicMock(value=side)
        o.filled_avg_price = str(price)
        o.filled_qty = str(qty)
        o.submitted_at = dt.datetime.now(ET)   # today, ET
        return o

    def test_closed_position_recorded_on_retry_not_dropped(self):
        # Cycle 1: the position has vanished from get_all_positions, but the broker's
        # CLOSED-orders query hasn't surfaced the filled SELL yet (status lag). The
        # symbol must be RETAINED for retry, not silently dropped (which lost the
        # trade from the performance log on 06/09).
        import bot
        store = {"positions": ["AAA"], "trades_today": [], "sl_order_ids": {"AAA": "id1"}}
        load, save = self._state_backed(store)
        buy  = self._order("buy", 100.0, 10)
        sell = self._order("sell", 105.0, 10)
        with mock.patch.object(bot, "client") as c, \
             mock.patch.object(bot, "load_state", side_effect=load), \
             mock.patch.object(bot, "save_state", side_effect=save):
            c.get_orders.return_value = [buy]            # only the buy has settled
            bot.sync_bracket_fills(positions=[])
            self.assertEqual(store["trades_today"], [])  # nothing recorded yet
            self.assertEqual(store["positions"], ["AAA"])  # retained for retry, NOT dropped

            c.get_orders.return_value = [buy, sell]      # sell now visible
            bot.sync_bracket_fills(positions=[])
        self.assertEqual([t["symbol"] for t in store["trades_today"]], ["AAA"])
        self.assertEqual(store["trades_today"][0]["pl"], 50.0)   # (105-100)*10
        self.assertEqual(store["positions"], [])                 # dropped only after recording

    def test_recorded_symbol_is_dropped(self):
        # The happy path: buy+sell both settled on the first cycle -> record + drop.
        import bot
        store = {"positions": ["AAA"], "trades_today": [], "sl_order_ids": {}}
        load, save = self._state_backed(store)
        with mock.patch.object(bot, "client") as c, \
             mock.patch.object(bot, "load_state", side_effect=load), \
             mock.patch.object(bot, "save_state", side_effect=save):
            c.get_orders.return_value = [self._order("buy", 100.0, 10),
                                         self._order("sell", 95.0, 10)]
            bot.sync_bracket_fills(positions=[])
        self.assertEqual(len(store["trades_today"]), 1)
        self.assertEqual(store["trades_today"][0]["pl"], -50.0)
        self.assertEqual(store["positions"], [])


# ──────────────────────────────────────────────────────────────────────────────
# 4. Static guards — keep earlier fixes from silently regressing
# ──────────────────────────────────────────────────────────────────────────────
class TestSourceGuards(unittest.TestCase):
    def _read(self, name):
        with open(os.path.join(ROOT, name), encoding="utf-8", errors="replace") as f:
            return f.read()

    def test_serve_binds_localhost(self):
        src = self._read("serve.py")
        self.assertIn('("127.0.0.1", PORT)', src)
        self.assertNotIn('ThreadingHTTPServer(("", PORT)', src)

    def test_serve_blocks_sensitive_files(self):
        src = self._read("serve.py")
        for f in (".env", "bot_state.json", "performance_log.json"):
            self.assertIn(f, src)
        # Source/state files must also be blocked by extension (no serving code).
        self.assertIn("BLOCKED_EXT", src)
        self.assertIn('".py"', src)
        self.assertIn("endswith(BLOCKED_EXT)", src)

    def test_watchdog_hardened(self):
        src = self._read("watchdog.ps1")
        self.assertIn("try {", src)            # self-heal guard
        self.assertIn("Get-PidFile", src)      # pid-file tracking
        self.assertIn("Get-Process -Id", src)  # reliable liveness check
        self.assertIn(".watchdog.lock", src)   # singleton guard (no two watchdogs)

    def test_watchdog_path_independent(self):
        # The watchdog must derive its dir from its own location, not a hardcoded
        # OneDrive path (the repo was moved out of OneDrive to avoid sync corruption).
        src = self._read("watchdog.ps1")
        self.assertIn("$PSScriptRoot", src)
        self.assertNotIn("OneDrive", src)

    def test_startps1_kill_is_scoped(self):
        src = self._read("start.ps1")
        self.assertIn("bot\\.py|serve\\.py|step_watch\\.py", src)

    def test_startps1_delegates_launch_to_watchdog(self):
        # start.ps1 must NOT launch bot/serve itself — doing so spawned duplicate
        # processes under a different interpreter than the watchdog adopts. It
        # must only start the watchdog, which owns all spawning.
        src = self._read("start.ps1")
        self.assertIn("watchdog.ps1", src)
        self.assertNotIn('-ArgumentList "bot.py"', src)
        self.assertNotIn('-ArgumentList "serve.py"', src)

    def test_bat_files_delegate(self):
        for b in ("start.bat", "start_momentum.bat"):
            self.assertIn("start.ps1", self._read(b))

    def test_bot_log_rotation(self):
        self.assertIn("RotatingFileHandler", self._read("bot.py"))

    def test_requirements_are_pinned(self):
        # Every dependency must be pinned (==) so a fresh install can't pull a
        # breaking new major version. Unpinned deps are how "it worked yesterday"
        # environments silently break.
        for line in self._read("requirements.txt").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            self.assertIn("==", line, f"unpinned dependency: {line!r}")

    def test_no_duplicate_position_size(self):
        # position_size must live only in trading_math now (single source of truth).
        self.assertNotIn("def position_size", self._read("bot.py"))

    def test_mean_reversion_hardened(self):
        # The manual mean-reversion bot shares the account/state, so its bugs can
        # spill into the momentum bot. Keep the fixes locked in.
        src = self._read("_archived/mean_reversion.py")
        # Schedule pinned to ET, not the machine's local clock.
        self.assertIn('"09:29", TZ', src)
        self.assertIn('"11:00", TZ', src)
        self.assertNotIn('"11:00").do', src)
        # Must NOT write the momentum bot's shared bot_state.json (corruption risk);
        # mr_state writes must be atomic.
        self.assertNotIn('SHARED_STATE_FILE, "w"', src)
        self.assertIn("os.replace", src)
        # Shared sizing helper + protective bracket on every entry.
        self.assertIn("from trading_math import", src)
        self.assertNotIn("def position_size", src)
        self.assertIn("OrderClass.BRACKET", src)
        # Safe baseline: never fall back to yesterday's close.
        self.assertNotIn("last_equity", src)

    def test_shell_scripts_are_pure_ascii(self):
        # Windows PowerShell 5.1 / cmd.exe read a no-BOM script in the legacy
        # Windows-1252 codepage, so a single non-ASCII char (e.g. an em-dash
        # pasted into a string) silently corrupts parsing — the script dies at
        # launch with NO log line. This actually happened to watchdog.ps1. Keep
        # every .ps1 and .bat (including the archived launcher) pure ASCII.
        import glob
        paths = (glob.glob(os.path.join(ROOT, "*.ps1")) + glob.glob(os.path.join(ROOT, "*.bat"))
                 + glob.glob(os.path.join(ROOT, "_archived", "*.bat"))
                 + glob.glob(os.path.join(ROOT, "_archived", "*.ps1")))
        for path in paths:
            with open(path, "rb") as fh:
                data = fh.read()
            bad = [(i, b) for i, b in enumerate(data) if b > 127]
            self.assertEqual(bad, [], f"{os.path.basename(path)} has non-ASCII byte(s) at {bad[:3]}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
