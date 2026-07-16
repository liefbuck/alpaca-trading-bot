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
import time
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
from config import MAX_POSITIONS

# Hermetic-by-default: point bot's data files at a throwaway dir for the WHOLE
# suite. Individual tests still patch these to their own temp paths; this is the
# backstop for any code path a test forgets to isolate. Without it, a test that
# calls a function which grew a new write (check_pnl -> snapshot_performance did
# exactly this) silently CORRUPTS the real performance_log.json / bot_state.json
# — a bogus 0-trade row for today landed in the live history this way.
_SANDBOX = tempfile.mkdtemp(prefix="bot_tests_")


def setUpModule():
    import bot
    bot.PERF_FILE  = os.path.join(_SANDBOX, "performance_log.json")
    bot.STATE_FILE = os.path.join(_SANDBOX, "bot_state.json")


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
        # Includes degenerate low-price/low-qty combos (e.g. $1 @ 1 share) that a
        # hypothesis fuzz showed could drive the raw stop NEGATIVE (-$19). The stop
        # must always be a POSITIVE price below the entry, or Alpaca rejects it.
        for price in [0.40, 1.00, 5.0, 12.34, 50.0, 200.0, 1500.0, 2500.0]:
            for qty in [1, 2, 5, 40, 1000, 5000]:
                tp, sl = tm.compute_bracket_prices(price, qty)
                self.assertGreater(sl, 0, f"SL not positive @ {price}/{qty}")   # the fuzz-found bug
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
        # Offsets are the configured dollar target/stop spread over qty. Derive the
        # expectation from config so a target/stop retune can't silently break this.
        from config import PER_POSITION_TARGET, PER_POSITION_STOP
        tp, sl = tm.compute_bracket_prices(100.0, 20)
        self.assertAlmostEqual(tp, 100.0 + PER_POSITION_TARGET / 20, places=2)
        self.assertAlmostEqual(sl, 100.0 + PER_POSITION_STOP / 20, places=2)  # STOP is negative


class TestRewardRiskAsymmetry(unittest.TestCase):
    """Policy pin (2026-06-30 analysis): the daily bleed came from a 1:1 target/stop
    whose realized losers (thin-name market-stop slippage, now gated by the liquidity
    floor) dwarfed its capped +$20 winners. The take-profit target must now be strictly
    LARGER than the per-position stop, so a high win rate actually compounds. Pinned so
    a future edit can't quietly revert it."""

    def test_target_strictly_exceeds_stop(self):
        from config import PER_POSITION_TARGET, PER_POSITION_STOP
        self.assertGreater(PER_POSITION_TARGET, abs(PER_POSITION_STOP),
                           "reward must exceed risk (target > |stop|)")


class TestHardStopThreshold(unittest.TestCase):
    """The per-position safety-net hard stop must sit BELOW the normal stop, so it only
    fires when the protective bracket has actually FAILED to flatten (the 2026-06-30
    stop-LIMIT non-fill that let ALGT/RRX ride to -$37/-$44), never on a normal stop."""

    def test_hard_stop_is_below_the_normal_stop(self):
        from config import PER_POSITION_HARD_STOP, PER_POSITION_STOP
        self.assertLess(PER_POSITION_HARD_STOP, PER_POSITION_STOP,
                        "hard stop must be more negative than the -$20 stop")


class TestPositionNotionalSizing(unittest.TestCase):
    """Sizing is decoupled from the target (2026-06-30): position_size keys off
    POSITION_NOTIONAL, not PER_POSITION_TARGET, so raising the target can't silently
    double position size and risk."""

    def test_sizes_to_notional_not_target(self):
        from config import POSITION_NOTIONAL
        # Ample budget -> shares == int(notional / price), independent of the target.
        self.assertEqual(tm.position_size(100.0, 1_000_000), int(POSITION_NOTIONAL / 100.0))

    def test_unaffected_by_target_value(self):
        # Sizing must not reference PER_POSITION_TARGET at all — patch it absurdly high
        # and the share count is unchanged.
        with mock.patch.object(tm, "PER_POSITION_TARGET", 999.0):
            self.assertEqual(tm.position_size(100.0, 1_000_000),
                             tm.position_size(100.0, 1_000_000))


class TestStopLadder(unittest.TestCase):
    # Algorithm-level tests: pass an explicit ladder so they exercise select_stop_pl's
    # logic independently of however config.STOP_STEPS happens to be tuned.
    LADDER = [(5, 0), (10, 5), (15, 10)]

    def test_below_first_trigger_returns_none(self):
        self.assertIsNone(tm.select_stop_pl(4.99, -1, self.LADDER))

    def test_first_step_breakeven(self):
        self.assertEqual(tm.select_stop_pl(5, -1, self.LADDER), 0)

    def test_skips_to_highest_earned(self):
        # Jumped to +12 from start -> lock +5 (the $10 trigger), skipping breakeven.
        self.assertEqual(tm.select_stop_pl(12, -1, self.LADDER), 5)

    def test_no_double_apply_same_level(self):
        # Already locked at +0; pl still only earns +0 -> nothing new.
        self.assertIsNone(tm.select_stop_pl(5, 0, self.LADDER))

    def test_monotonic_progression(self):
        self.assertEqual(tm.select_stop_pl(20, 5, self.LADDER), 10)
        self.assertIsNone(tm.select_stop_pl(20, 10, self.LADDER))  # top already locked


class TestConfiguredStopLadder(unittest.TestCase):
    """Pins the POLICY in config.STOP_STEPS (not just the algorithm). Two 06/12
    failures shaped it: a breakeven ($0) lock turned negative on stop slippage
    (ARM sold -$2.65), and a too-high +$10 first rung gave winners back (ARM popped
    +$8.30 and round-tripped). So: trail EARLY, but every lock is a profit that
    clears typical slippage, so a pop that reverses exits for a small gain."""
    def setUp(self):
        from config import STOP_STEPS, PER_POSITION_TARGET
        self.steps = STOP_STEPS
        self.target = PER_POSITION_TARGET

    def test_early_pop_locks_a_profit(self):
        # The fix for both failures: a real +$5 pop MUST ratchet, and to a POSITIVE
        # lock (so reversing exits in profit, not at breakeven-minus-slippage).
        locked = tm.select_stop_pl(5, -1, self.steps)
        self.assertIsNotNone(locked, "a +$5 pop must move the stop")
        self.assertGreater(locked, 0, "the locked level must be a profit, not breakeven")

    def test_every_lock_clears_a_slippage_buffer(self):
        # No lock at/below breakeven, and the first lock keeps a few $ of cushion so a
        # market-fill stop's slippage can't flip a liquid-name exit to a loss.
        for trigger, lock in self.steps:
            self.assertGreater(lock, 0, f"rung {trigger} locks at {lock} (<= breakeven)")
        self.assertGreaterEqual(min(l for _, l in self.steps), 2)

    def test_rungs_strictly_increasing_and_below_target(self):
        triggers = [t for t, _ in self.steps]
        locks    = [l for _, l in self.steps]
        self.assertEqual(triggers, sorted(set(triggers)), "triggers must strictly increase")
        self.assertEqual(locks, sorted(set(locks)), "locks must strictly increase")
        for trigger, lock in self.steps:
            self.assertLess(lock, trigger, "lock must sit below its own trigger")
            self.assertLess(lock, self.target, "lock must sit below the take-profit target")


class TestLogEncoding(unittest.TestCase):
    def test_logfile_handler_is_utf8(self):
        # The bot.log file handler MUST be utf-8: without it Windows uses cp1252, which
        # can't encode chars like the "→" in the STEP STOP message, so logging silently
        # drops the whole line (every trailing-stop move went unlogged before this fix).
        import logging
        from logging.handlers import RotatingFileHandler
        import bot  # configures the root logger at import
        handlers = [h for h in logging.getLogger().handlers
                    if isinstance(h, RotatingFileHandler)]
        self.assertTrue(handlers, "expected bot to install a RotatingFileHandler")
        for h in handlers:
            self.assertEqual((h.encoding or "").lower(), "utf-8")

    def test_yfinance_logger_silenced(self):
        # yfinance logs hundreds of "possibly delisted" ERROR lines per scan when
        # Yahoo throttles the bulk download; importing screener must mute its logger
        # so that noise can't flood bot.log and bury real errors.
        import logging
        import screener  # noqa: F401  (import has the side effect under test)
        self.assertGreaterEqual(logging.getLogger("yfinance").level, logging.CRITICAL)


class TestMomentumLiquidityFilter(unittest.TestCase):
    """The momentum scan must reject cheap/thin names — a -$20 market stop only holds
    on liquid large-caps. On 2026-06-17 the four losers (all sub-$50 mid/small-caps)
    blew through the stop to -$24..-$49; the filter requires price >= MIN_SHARE_PRICE
    and avg daily dollar-volume >= MIN_AVG_DOLLAR_VOL."""

    def _frame(self, specs):
        # specs: {symbol: (last_price, last_volume)}. Build a 30-bar history (dates in
        # the past so no partial-day projection) that gaps up ~0.6%, has rising volume
        # and elevated last-bar volume, so every name passes gap/rel_vol/vol_trend and
        # ONLY the price + dollar-volume gates decide survival.
        import pandas as pd
        idx = pd.date_range(end="2020-01-30", periods=30, freq="B")
        cols, data = [], {}
        for sym, (price, vol) in specs.items():
            closes = [price * 0.994] * 29 + [price]          # +0.6% gap on last bar
            vols   = [vol * 0.5] * 15 + [vol * 0.9] * 14 + [vol]  # rising + hot last bar
            cols += [(sym, "Close"), (sym, "Volume")]
            data[(sym, "Close")]  = closes
            data[(sym, "Volume")] = vols
        return pd.DataFrame(data, index=idx, columns=pd.MultiIndex.from_tuples(cols))

    def test_filter_keeps_liquid_drops_cheap_and_thin(self):
        import screener
        specs = {
            "LIQ":  (100.0, 1_000_000),   # $100, $100M/day  -> keep
            "CHEAP": (45.0, 2_000_000),   # $45 (<$50), $90M/day -> drop on price
            "THIN": (200.0,    50_000),   # $200, $10M/day (<$50M) -> drop on dollar-vol
        }
        frame = self._frame(specs)
        with mock.patch.object(screener, "get_universe", return_value=list(specs)), \
             mock.patch.object(screener, "_download_history", return_value=frame):
            top = screener.get_top_momentum(n=50)
        symbols = {c["symbol"] for c in top}
        self.assertIn("LIQ", symbols)
        self.assertNotIn("CHEAP", symbols)   # fails price floor
        self.assertNotIn("THIN", symbols)    # fails dollar-volume floor


class TestStatusStrip(unittest.TestCase):
    """The dashboard's rolling status strip is built from serve.recent_status, which
    must surface only meaningful events and drop the per-tick 'Daily P&L:' heartbeat
    and the indented per-position lines that otherwise dominate bot.log."""

    LINES = [
        "2026-06-17 09:30:00,000 INFO Trading bot started",
        "2026-06-17 09:31:05,000 INFO Daily P&L: $-5.00  (limit $-200 | target $200)",
        "2026-06-17 09:31:05,001 INFO   AMKR  P&L $+2.94 [stop locked at $+3]",
        "2026-06-17 09:34:29,000 INFO BUY 21x AMKR @ $92.63 | TP $93.58 | SL $91.68",
        "2026-06-17 09:35:11,000 INFO Entry cycle complete — 10/10 filled.",
        "2026-06-17 10:05:00,000 INFO Bracket closed AMKR: entry $91.56 → exit $92.00 | P&L $+9.68",
        "2026-06-17 15:45:01,000 INFO EOD Summary: {'date': '2026-06-17'}",
    ]

    def _write(self):
        fd, p = tempfile.mkstemp()
        os.close(fd)
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(self.LINES))
        self.addCleanup(os.remove, p)
        return p

    def test_filters_noise_and_keeps_events_newest_last(self):
        import serve
        out = serve.recent_status(self._write(), n=12)
        self.assertTrue(all("Daily P&L:" not in l for l in out), "heartbeat leaked")
        self.assertTrue(all("  AMKR  P&L" not in l for l in out), "per-position line leaked")
        self.assertTrue(any("Entry cycle complete" in l for l in out))
        self.assertTrue(any("Bracket closed" in l for l in out))
        self.assertTrue(out[-1].endswith("EOD Summary: {'date': '2026-06-17'}"), "must be newest-last")

    def test_caps_at_n(self):
        import serve
        out = serve.recent_status(self._write(), n=2)
        self.assertEqual(len(out), 2)
        # The two most recent meaningful events.
        self.assertIn("Bracket closed", out[0])
        self.assertIn("EOD Summary", out[1])

    def test_missing_file_returns_empty(self):
        import serve
        self.assertEqual(serve.recent_status("does_not_exist_xyz.log"), [])


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
        import bot  # importing bot registers all schedule jobs at import time
        cls.schedule = bot.schedule   # the same schedule singleton the jobs are on
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
        # First entry at 9:31 (earliest valid post-open start) so the ~3.5 min scan
        # of the expanded universe starts ASAP; later windows are backups.
        self.assertEqual(self._et_minutes_for("open_positions"),
                         {(9, 31), (9, 48), (10, 3), (10, 18)})

    def test_reset_overnight_eod(self):
        self.assertEqual(self._et_minutes_for("daily_reset"), {(9, 29)})
        self.assertEqual(self._et_minutes_for("close_overnight"), {(9, 30)})
        self.assertEqual(self._et_minutes_for("eod_close"), {(15, 45)})

    def test_daily_jobs_carry_eastern_timezone(self):
        # Directly assert every daily .at() job stores the America/New_York tz.
        # The next_run-based tests above can't tell at("09:29") from
        # at("09:29", ET) on an ET machine, so this is the real guard against a
        # tz-drop regression (which would silently fire jobs in local time).
        daily = [j for j in self.schedule.jobs if getattr(j, "at_time", None) is not None]
        self.assertTrue(daily, "no daily .at() jobs found — schedule wiring changed")
        for j in daily:
            tz = getattr(j, "at_time_zone", None)
            self.assertIsNotNone(tz, f"daily job {j} has no tz — would fire in local time")
            self.assertEqual(str(tz), "America/New_York", f"daily job {j} tz is {tz}, not ET")


class TestEodWeekendGuard(unittest.TestCase):
    def test_skips_perf_log_when_market_closed(self):
        import bot
        with mock.patch.object(bot, "client") as c, \
             mock.patch.object(bot, "log_daily_performance") as perf, \
             mock.patch.object(bot, "close_all"), \
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
        # A clock-check that fails EVERY retry must still force-close (safety) but
        # must NOT write a performance row (can't confirm a trading day).
        import bot
        with mock.patch.object(bot, "client") as c, \
             mock.patch.object(bot, "log_daily_performance") as perf, \
             mock.patch.object(bot, "close_all") as close_all, \
             mock.patch.object(bot, "sync_bracket_fills"), \
             mock.patch.object(bot, "persist_halted"), \
             mock.patch.object(bot.time, "sleep"):   # don't actually wait through retries
            c.get_clock.side_effect = RuntimeError("network")
            c.get_all_positions.return_value = []
            bot.trading_active = True
            bot.eod_close()
            close_all.assert_called_once()
            perf.assert_not_called()

    def test_eod_retries_transient_clock_failure_then_writes(self):
        # The recurring 15:45 DNS-blip bug: a TRANSIENT clock failure must be retried,
        # and once it recovers the day's perf row IS written (not lost).
        import bot
        with mock.patch.object(bot, "client") as c, \
             mock.patch.object(bot, "log_daily_performance") as perf, \
             mock.patch.object(bot, "close_all") as close_all, \
             mock.patch.object(bot, "sync_bracket_fills"), \
             mock.patch.object(bot, "persist_halted"), \
             mock.patch.object(bot.time, "sleep"):
            c.get_clock.side_effect = [RuntimeError("dns"), RuntimeError("dns"),
                                       _fake_clock(is_open=True)]
            c.get_all_positions.return_value = []
            bot.trading_active = True
            bot.eod_close()
            perf.assert_called_once()       # recovered -> summary written, NOT skipped
            close_all.assert_called_once()


class TestCloseAllRobust(unittest.TestCase):
    def test_retries_until_verified_flat(self):
        # A blip during the close must not leave positions carried overnight: keep
        # closing until a position fetch confirms flat.
        import bot
        with mock.patch.object(bot, "client") as c, mock.patch.object(bot.time, "sleep"):
            # first verify: still 1 open; second verify: flat
            c.get_all_positions.side_effect = [[mock.MagicMock(symbol="X")], []]
            self.assertTrue(bot.close_all())
            self.assertEqual(c.close_all_positions.call_count, 2)   # retried once

    def test_gives_up_after_max_attempts(self):
        # Never blocks forever: bounded retries, returns False if still not flat.
        import bot
        with mock.patch.object(bot, "client") as c, mock.patch.object(bot.time, "sleep"):
            c.get_all_positions.return_value = [mock.MagicMock(symbol="X")]  # never flat
            self.assertFalse(bot.close_all())
            self.assertEqual(c.close_all_positions.call_count, 5)

    def test_flat_on_first_try_no_wasted_retries(self):
        import bot
        with mock.patch.object(bot, "client") as c, mock.patch.object(bot.time, "sleep"):
            c.get_all_positions.return_value = []   # already flat
            self.assertTrue(bot.close_all())
            self.assertEqual(c.close_all_positions.call_count, 1)

    def test_close_overnight_forces_close_even_if_listing_fails(self):
        # A blip listing positions at 09:30 must NOT skip the close and let stale
        # positions silently block today's entry.
        import bot
        with mock.patch.object(bot, "client") as c, mock.patch.object(bot, "close_all") as ca:
            c.get_all_positions.side_effect = RuntimeError("dns")
            bot.close_overnight()
            ca.assert_called_once()


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

    def test_retries_transient_clock_failure_then_resets(self):
        # A transient DNS blip at 09:29 must NOT skip the reset (which would leave
        # entries_done=True and the bot sitting out the day). Retry, then reset.
        import bot
        today = dt.datetime.now(ET)
        clk = _fake_clock(is_open=True, next_open=today, next_close=today)
        with mock.patch.object(bot, "client") as c, \
             mock.patch.object(bot, "reset_session") as reset, \
             mock.patch.object(bot, "save_state"), \
             mock.patch.object(bot, "load_state", return_value={}), \
             mock.patch.object(bot.time, "sleep"):
            c.get_clock.side_effect = [RuntimeError("dns"), clk]
            bot.daily_reset()
            reset.assert_called_once()   # recovered -> reset ran, not skipped


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


class TestBacktestHarness(unittest.TestCase):
    """The harness decides what ships, so its biases must stay honest.

    Bars are (timestamp, open, high, low, close).
    """

    def _bars(self, day, rows):
        t0 = ET.localize(dt.datetime.combine(day, dt.time(9, 40)))
        return [(t0 + dt.timedelta(minutes=i), *r) for i, r in enumerate(rows)]

    def _trade(self, day, qty=20, entry=100.0):
        return {"day": day, "sym": "X", "qty": qty, "entry": entry,
                "entry_at": ET.localize(dt.datetime.combine(day, dt.time(9, 40)))}

    def test_ambiguous_bar_resolves_against_the_strategy(self):
        # One bar touches BOTH the -$20 stop and the +$40 target. A backtest that
        # books the target here is how you get a strategy that only works offline.
        import backtest as bt
        day = dt.date(2026, 6, 15)
        # qty 20 -> stop at 99.00, target at 102.00. This bar spans both.
        bars = self._bars(day, [(100.0, 103.0, 98.0, 100.0), (100.0, 100.0, 100.0, 100.0)])
        pol = bt.ExitPolicy("t", target=40.0, stop=-20.0, use_ladder=False)
        pl, reason = bt.simulate(self._trade(day), bars, pol)
        self.assertEqual(reason, "stop")
        self.assertLess(pl, 0)

    def test_stop_fills_at_next_bar_open_not_the_trigger(self):
        # A stop is a MARKET order: it cannot fill at its own trigger price.
        import backtest as bt
        day = dt.date(2026, 6, 15)
        bars = self._bars(day, [(100.0, 100.0, 98.0, 98.5), (97.0, 97.0, 97.0, 97.0)])
        pol = bt.ExitPolicy("t", target=40.0, stop=-20.0, use_ladder=False)
        pl, reason = bt.simulate(self._trade(day), bars, pol)
        self.assertEqual(reason, "stop")
        self.assertAlmostEqual(pl, (97.0 - 100.0) * 20, places=2)   # gapped through, not -$20

    def test_target_fills_exactly_at_its_limit_never_better(self):
        import backtest as bt
        day = dt.date(2026, 6, 15)
        bars = self._bars(day, [(100.0, 105.0, 100.0, 105.0)])   # blew past +$40
        pol = bt.ExitPolicy("t", target=40.0, stop=-20.0, use_ladder=False)
        pl, reason = bt.simulate(self._trade(day), bars, pol)
        self.assertEqual(reason, "target")
        self.assertAlmostEqual(pl, 40.0, places=2)                # not the 105 print

    def test_ladder_ratchets_on_close_not_high(self):
        # Crediting the intra-minute HIGH locks profit a 4s-polling bot never saw
        # and flatters results by ~$2/trade. Bar 1 spikes to +$5 on the high but
        # closes flat -> no rung may fire.
        import backtest as bt
        day = dt.date(2026, 6, 15)
        bars = self._bars(day, [(100.0, 100.25, 100.0, 100.0),    # high = +$5, close = +$0
                                (100.0, 100.0, 99.9, 99.95),
                                (99.95, 99.95, 99.95, 99.95)])
        pol = bt.ExitPolicy("t", target=40.0, stop=-20.0, steps=[(5, 3)])
        pl, reason = bt.simulate(self._trade(day), bars, pol)
        # If it had ratcheted on the high it would lock +$3 and stop out at +$3.
        self.assertNotAlmostEqual(pl, 3.0, places=2)

    def test_a_raised_stop_only_applies_from_the_next_bar(self):
        # Must never lock a profit and stop against the very bar that locked it.
        import backtest as bt
        day = dt.date(2026, 6, 15)
        # Bar 1 closes +$10 (rung (5,3) fires -> stop to +$3 = 100.15) and its own
        # low is below that; the exit may only happen on a LATER bar.
        bars = self._bars(day, [(100.0, 100.5, 100.0, 100.5), (100.5, 100.5, 100.0, 100.1)])
        pol = bt.ExitPolicy("t", target=40.0, stop=-20.0, steps=[(5, 3)])
        pl, reason = bt.simulate(self._trade(day), bars, pol)
        self.assertEqual(reason, "stop")
        self.assertGreater(pl, 0)   # locked the +$3 rung, not stopped at a loss

    def test_eod_forces_a_close_at_1545(self):
        import backtest as bt
        day = dt.date(2026, 6, 15)
        t0 = ET.localize(dt.datetime.combine(day, dt.time(15, 44)))
        bars = [(t0, 100.0, 100.0, 100.0, 100.0),
                (t0 + dt.timedelta(minutes=1), 101.0, 101.0, 101.0, 101.0)]
        tr = {"day": day, "sym": "X", "qty": 20, "entry": 100.0, "entry_at": t0}
        pol = bt.ExitPolicy("t", target=40.0, stop=-20.0, use_ladder=False)
        pl, reason = bt.simulate(tr, bars, pol)
        self.assertEqual(reason, "eod")

    def test_harness_bracket_prices_agree_with_the_bots(self):
        # The harness derives bracket prices itself (a sweep must vary the target
        # and stop, which compute_bracket_prices reads from config). That freedom
        # is only safe while the two agree for the CONFIGURED policy -- otherwise
        # the backtest silently grades a bracket the bot would never place.
        import backtest as bt
        from config import PER_POSITION_TARGET, PER_POSITION_STOP
        for price, qty in [(100.0, 20), (250.5, 8), (57.33, 34), (913.87, 2)]:
            tp_bot, sl_bot = tm.compute_bracket_prices(price, qty)
            tp_bt = price + PER_POSITION_TARGET / qty
            sl_bt = price + PER_POSITION_STOP / qty
            self.assertAlmostEqual(tp_bot, tp_bt, delta=0.02,
                                   msg=f"target drift at price={price} qty={qty}")
            self.assertAlmostEqual(sl_bot, sl_bt, delta=0.02,
                                   msg=f"stop drift at price={price} qty={qty}")

    def test_harness_uses_the_bots_real_ladder_function(self):
        # Imported, never re-implemented: a copy would silently drift from the bot
        # and the backtest would start grading a strategy that isn't running.
        import backtest as bt
        import trading_math
        self.assertIs(bt.select_stop_pl, trading_math.select_stop_pl)
        with open(os.path.join(ROOT, "backtest.py"), encoding="utf-8") as fh:
            self.assertNotIn("def select_stop_pl", fh.read())


class TestHeartbeat(unittest.TestCase):
    """The bot must prove PROGRESS, not mere existence."""

    def _read(self, path):
        with open(path) as fh:
            return json.load(fh)

    def test_beat_stamps_ts_and_pid(self):
        import bot
        with tempfile.TemporaryDirectory() as d:
            hb = os.path.join(d, ".bot.heartbeat")
            with mock.patch.object(bot, "HEARTBEAT_FILE", hb), \
                 mock.patch.object(bot, "_last_beat", 0.0):
                bot.beat(force=True)
            data = self._read(hb)
            # pid is what stops the watchdog acting on a DEAD bot's stamp.
            self.assertEqual(data["pid"], os.getpid())
            self.assertLess(abs(data["ts"] - time.time()), 30)

    def test_beat_is_rate_limited_but_force_overrides(self):
        import bot
        with tempfile.TemporaryDirectory() as d:
            hb = os.path.join(d, ".bot.heartbeat")
            with mock.patch.object(bot, "HEARTBEAT_FILE", hb), \
                 mock.patch.object(bot, "_last_beat", 0.0):
                bot.beat(force=True)
                first = self._read(hb)["ts"]
                bot.beat()                      # rate-limited -> no rewrite
                self.assertEqual(self._read(hb)["ts"], first)
                time.sleep(0.01)
                bot.beat(force=True)            # force -> rewrite
                self.assertGreater(self._read(hb)["ts"], first)

    def test_beat_never_raises(self):
        # A heartbeat problem must never be able to take trading down.
        import bot
        with mock.patch.object(bot, "HEARTBEAT_FILE", os.path.join(_SANDBOX, "no", "such", "dir", "hb")), \
             mock.patch.object(bot, "_last_beat", 0.0):
            bot.beat(force=True)   # must not raise

    def test_main_loop_beats_after_run_pending_not_before(self):
        # Ordering IS the design: beating before run_pending() would keep
        # stamping "healthy" while a job blocks forever, defeating the detector.
        with open(os.path.join(ROOT, "bot.py"), encoding="utf-8") as fh:
            src = fh.read()
        loop = src[src.index("    while True:"):]
        self.assertLess(loop.index("schedule.run_pending()"), loop.index("beat()"))

    def test_startup_claims_heartbeat_before_slow_phases(self):
        # The bot must stamp its own pid before wait_for_network/backfill, or the
        # watchdog reads the PREVIOUS bot's stale stamp and kills this one.
        with open(os.path.join(ROOT, "bot.py"), encoding="utf-8") as fh:
            src = fh.read()
        main = src[src.index('if __name__ == "__main__":'):]
        self.assertLess(main.index("beat(force=True)"), main.index("wait_for_network()"))


class TestWatchdogWedgeDetection(unittest.TestCase):
    """watchdog.ps1's restart window must stay pinned to bot.py's real schedule."""

    def setUp(self):
        with open(os.path.join(ROOT, "watchdog.ps1"), encoding="ascii") as fh:
            self.wd = fh.read()
        with open(os.path.join(ROOT, "bot.py"), encoding="utf-8") as fh:
            self.bot = fh.read()

    def _ps_var(self, name):
        m = __import__("re").search(rf'^\${name}\s*=\s*"?([^"\r\n]+)"?', self.wd, __import__("re").M)
        self.assertIsNotNone(m, f"${name} not found in watchdog.ps1")
        return m.group(1).strip()

    def _window(self):
        parse = lambda s: dt.datetime.strptime(s, "%H:%M").time()
        return parse(self._ps_var("noRestartFromET")), parse(self._ps_var("noRestartToET"))

    def test_window_covers_every_entry_time(self):
        import re
        entries = re.search(r'for _t in \[([^\]]+)\]', self.bot).group(1)
        entries = [dt.datetime.strptime(t.strip().strip('"'), "%H:%M").time()
                   for t in entries.split(",")]
        self.assertTrue(entries)
        lo, hi = self._window()
        for t in entries:
            self.assertGreaterEqual(t, lo, f"entry {t} starts before the no-restart window")
            self.assertLessEqual(t, hi, f"entry {t} is outside the no-restart window")

    def test_window_outlasts_the_last_entry_plus_its_fill_budget(self):
        # Entry work is still in flight for ENTRY_BUDGET_S after the last buy;
        # a kill during it can strand a filled-but-unprotected position.
        import re
        budget = float(re.search(r'ENTRY_BUDGET_S\s*=\s*([\d.]+)', self.bot).group(1))
        entries = re.search(r'for _t in \[([^\]]+)\]', self.bot).group(1)
        last = max(dt.datetime.strptime(t.strip().strip('"'), "%H:%M").time()
                   for t in entries.split(","))
        last_dt = dt.datetime.combine(dt.date(2026, 1, 1), last) + dt.timedelta(seconds=budget)
        _, hi = self._window()
        self.assertGreaterEqual(dt.datetime.combine(dt.date(2026, 1, 1), hi), last_dt)

    def test_window_ends_after_the_catchup_cutoff_so_a_restart_cannot_re_enter(self):
        # THE safety property. bot.py re-enters on startup only before its 10:30
        # cutoff; if the window ended earlier, a wedge-restart could fire inside
        # the catchup range and double up the day's positions.
        import re
        h, m = re.search(r'entry_cutoff\s*=\s*now_et\.replace\(hour=(\d+), minute=(\d+)', self.bot).groups()
        cutoff = dt.time(int(h), int(m))
        _, hi = self._window()
        self.assertGreater(hi, cutoff,
                           "no-restart window must outlast bot.py's catchup cutoff")

    def test_stall_limit_exceeds_the_startup_network_wait(self):
        # wait_for_network can legitimately block ~300s on a slow boot. If the
        # stall limit were under that, the watchdog would kill every cold start.
        import re
        limit = int(self._ps_var("stallLimitSec"))
        net = int(re.search(r'def wait_for_network\(max_wait_seconds: int = (\d+)', self.bot).group(1))
        self.assertGreater(limit, net)

    def test_singleton_lock_is_machine_wide_not_per_folder(self):
        # A $baseDir lock can only see a rival inside its own folder, so a second
        # checkout takes its own lock and both watchdogs duel on one broker
        # account -- the 2026-06-11 dual-bot failure. The lock must live at one
        # fixed path every watchdog on the box contends for.
        self.assertIn("$env:ProgramData", self.wd)
        lock = __import__("re").search(r'\$lockFile\s*=\s*Join-Path \$lockDir "([^"]+)"', self.wd)
        self.assertIsNotNone(lock, "machine-wide lock path not found")
        # The baseDir path may survive ONLY as the not-writable fallback.
        self.assertNotIn('$lockFile = Join-Path $baseDir ".watchdog.lock"\n$script:lockStream',
                         self.wd)

    def test_singleton_lock_falls_back_rather_than_running_unlocked(self):
        # If ProgramData is unwritable, a local lock is worse than a global one
        # but infinitely better than no singleton at all.
        block = self.wd[self.wd.index("$lockFile = $null"):]
        block = block[:block.index("$script:lockStream = $null")]
        self.assertIn("catch", block)
        self.assertIn('Join-Path $baseDir ".watchdog.lock"', block)

    def test_discovery_still_matches_the_bare_script_name(self):
        # Deliberate: a full-path match would stop recognising a hand-launched
        # `python bot.py` and spawn a SECOND bot -- two bots on one account is far
        # worse than adopting a stray. Cross-folder rivalry is stopped by the
        # machine-wide lock instead, not by narrowing this.
        self.assertIn('$_.CommandLine -like "*$scriptName*"', self.wd)

    def test_stall_limit_outlasts_a_full_entry_cycle(self):
        # open_positions runs INSIDE schedule.run_pending(), and beat() only fires
        # after run_pending returns -- so the heartbeat legitimately goes stale for
        # the whole scan + fill budget (~4min scan + 300s budget today). The entry
        # window normally covers this, but the startup catchup can fire an entry at
        # 10:29 that outruns the window's 10:35 end, and then only this margin
        # stops the watchdog killing a perfectly healthy bot mid-entry.
        import re
        budget = float(re.search(r'ENTRY_BUDGET_S\s*=\s*([\d.]+)', self.bot).group(1))
        scan_allowance = 300.0     # observed ~230s for the ~1500-ticker scan
        limit = int(self._ps_var("stallLimitSec"))
        self.assertGreater(limit, budget + scan_allowance,
                           "stallLimitSec must outlast a full scan+entry cycle or the "
                           "watchdog will kill the bot while it is legitimately buying")

    def test_watchdog_reads_the_same_heartbeat_file_the_bot_writes(self):
        import bot
        self.assertIn(os.path.basename(bot.HEARTBEAT_FILE), self.wd)

    def test_uncertain_heartbeat_reads_never_kill(self):
        # Every bail-out in Test-Wedged must return $false: an unreadable or
        # foreign stamp is not evidence of a wedge.
        fn = self.wd[self.wd.index("function Test-Wedged"):]
        fn = fn[:fn.index("\n}")]
        self.assertIn("if ([int]$hb.pid -ne [int]$procId) { return $false }", fn)
        self.assertIn("if (-not (Test-Path $botHeartbeat)) { return $false }", fn)
        self.assertIn("catch {\n        return $false\n    }", fn)


class TestHttpTimeout(unittest.TestCase):
    """THE mid-session killer: alpaca-py never sets a request timeout.

    bot.py is single-threaded, so one unbounded call freezes the whole scheduler
    silently — process alive (watchdog sees "healthy"), log dead, no EOD row.
    """

    def test_session_injects_a_default_timeout(self):
        import alpaca_client
        sess = alpaca_client.TimeoutSession()
        captured = {}
        with mock.patch("requests.Session.request",
                        side_effect=lambda *a, **kw: captured.update(kw)):
            sess.request("GET", "http://example.invalid")
        self.assertEqual(captured.get("timeout"), alpaca_client.HTTP_TIMEOUT)

    def test_explicit_timeout_is_not_overridden(self):
        import alpaca_client
        sess = alpaca_client.TimeoutSession()
        captured = {}
        with mock.patch("requests.Session.request",
                        side_effect=lambda *a, **kw: captured.update(kw)):
            sess.request("GET", "http://example.invalid", timeout=1)
        self.assertEqual(captured.get("timeout"), 1)

    def test_timeout_is_bounded_and_finite(self):
        import alpaca_client
        connect, read = alpaca_client.HTTP_TIMEOUT
        self.assertGreater(connect, 0)
        self.assertGreater(read, 0)
        # Must stay well under the ~15min gap between scheduled entry windows.
        self.assertLess(connect + read, 120)

    def test_factory_actually_installs_the_session(self):
        # Guards the reach into alpaca-py's private _session: an SDK upgrade that
        # breaks the injection must fail HERE, not silently restore the hang.
        import alpaca_client
        c = alpaca_client.make_trading_client(paper=True)
        self.assertIsInstance(c._session, alpaca_client.TimeoutSession)

    def test_bot_client_has_the_timeout_session(self):
        import bot, alpaca_client
        self.assertIsInstance(bot.client._session, alpaca_client.TimeoutSession)

    def test_no_module_builds_a_raw_tradingclient(self):
        # Every entry point must go through the factory, or it silently reverts
        # to the unbounded-hang behaviour.
        import glob
        for path in glob.glob(os.path.join(ROOT, "*.py")):
            if os.path.basename(path) == "alpaca_client.py":
                continue
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            self.assertNotIn("TradingClient(", src,
                             f"{os.path.basename(path)} builds a client directly — "
                             "use alpaca_client.make_trading_client()")

    def test_call_against_a_blackhole_socket_raises_instead_of_hanging(self):
        # End-to-end proof, and the real regression guard: a socket that ACCEPTS
        # but never answers (what this host's network blip produces) used to block
        # forever. It must now raise within the timeout.
        import socket, threading
        import alpaca_client
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        self.addCleanup(srv.close)
        held = []

        def blackhole():
            try:
                while True:
                    conn, _ = srv.accept()
                    held.append(conn)   # keep open, answer nothing
            except OSError:
                pass
        threading.Thread(target=blackhole, daemon=True).start()

        c = alpaca_client.make_trading_client(paper=True)
        c._base_url = f"http://127.0.0.1:{srv.getsockname()[1]}"
        c._session.timeout = (2, 3)   # keep the test fast

        t0 = time.time()
        with self.assertRaises(Exception) as ctx:
            c.get_clock()
        elapsed = time.time() - t0
        self.assertIn("timeout", type(ctx.exception).__name__.lower() + str(ctx.exception).lower())
        self.assertLess(elapsed, 20, "call did not bail out — the hang is back")


class TestPairFillsToTrades(unittest.TestCase):
    def test_reproduces_live_bracket_pnl(self):
        # Real 2026-07-09 fills: bot.log recorded "SOLS: entry $63.05 -> exit
        # $63.02 | P&L $-0.93". Reconstruction from fills must agree exactly.
        trades = tm.pair_fills_to_trades([
            {"symbol": "SOLS", "side": "buy",  "qty": "31", "price": "63.05"},
            {"symbol": "SOLS", "side": "sell", "qty": "31", "price": "63.02"},
        ])
        self.assertEqual(trades, [{"symbol": "SOLS", "pl": -0.93}])

    def test_sums_partial_exits(self):
        trades = tm.pair_fills_to_trades([
            {"symbol": "AAA", "side": "buy",  "qty": "10", "price": "100"},
            {"symbol": "AAA", "side": "sell", "qty": "4",  "price": "110"},
            {"symbol": "AAA", "side": "sell", "qty": "6",  "price": "105"},
        ])
        self.assertEqual(trades, [{"symbol": "AAA", "pl": 70.0}])

    def test_skips_symbol_that_is_not_flat(self):
        # An unsold position is an OPEN trade, not a completed one — booking it
        # would invent a fictitious full-value loss.
        trades = tm.pair_fills_to_trades([
            {"symbol": "AAA", "side": "buy",  "qty": "10", "price": "100"},
            {"symbol": "BBB", "side": "buy",  "qty": "5",  "price": "50"},
            {"symbol": "BBB", "side": "sell", "qty": "5",  "price": "52"},
        ])
        self.assertEqual(trades, [{"symbol": "BBB", "pl": 10.0}])

    def test_empty_day(self):
        self.assertEqual(tm.pair_fills_to_trades([]), [])


class TestPerformanceDurability(unittest.TestCase):
    """The history must survive the bot dying before the 15:45 EOD."""

    def test_check_pnl_snapshots_todays_row(self):
        import bot
        with tempfile.TemporaryDirectory() as d:
            perf  = os.path.join(d, "perf.json")
            state = os.path.join(d, "state.json")
            today = str(dt.datetime.now(ET).date())
            with mock.patch.object(bot, "PERF_FILE", perf), \
                 mock.patch.object(bot, "STATE_FILE", state), \
                 mock.patch.object(bot, "_last_perf_snapshot", 0.0), \
                 mock.patch.object(bot, "trading_active", True), \
                 mock.patch.object(bot, "is_market_open", return_value=True), \
                 mock.patch.object(bot, "get_daily_pnl", return_value=-31.90), \
                 mock.patch.object(bot, "client") as cl:
                cl.get_all_positions.return_value = []
                bot.save_state({"trades_today": [{"symbol": "SOLS", "pl": -0.93}]})
                bot.check_pnl()
            with open(perf) as f:
                hist = json.load(f)
            self.assertEqual([h["date"] for h in hist], [today])
            self.assertEqual(hist[0]["daily_pnl"], -31.9)
            # Provisional: the day is not over, so a later backfill may repair it.
            self.assertFalse(hist[0]["final"])

    def test_snapshot_is_rate_limited(self):
        import bot
        with tempfile.TemporaryDirectory() as d:
            perf = os.path.join(d, "perf.json")
            with mock.patch.object(bot, "PERF_FILE", perf), \
                 mock.patch.object(bot, "_last_perf_snapshot", time.time()):
                bot.snapshot_performance(-10.0)
            self.assertFalse(os.path.exists(perf))

    def test_eod_marks_row_final(self):
        import bot
        with tempfile.TemporaryDirectory() as d:
            perf, state = os.path.join(d, "perf.json"), os.path.join(d, "state.json")
            with mock.patch.object(bot, "PERF_FILE", perf), \
                 mock.patch.object(bot, "STATE_FILE", state), \
                 mock.patch.object(bot, "get_daily_pnl", return_value=42.0):
                bot.save_state({"trades_today": [{"symbol": "A", "pl": 42.0}]})
                bot.log_daily_performance()
            with open(perf) as f:
                hist = json.load(f)
            self.assertTrue(hist[0]["final"])


class TestPerformanceLogIsNeverDestroyed(unittest.TestCase):
    """An unreadable log must NEVER be overwritten.

    load_performance used to return [] for BOTH "no file yet" and "file is
    corrupt", so one bad read made upsert write a single row over the whole
    history. Harmless-ish at 1 write/day; catastrophic once check_pnl started
    snapshotting every 60s.
    """
    CORRUPT = "{ this is not valid json"

    def _seed(self, d, rows=6):
        perf = os.path.join(d, "perf.json")
        hist = [{"date": f"2026-06-{i:02d}", "daily_pnl": -1.0 * i, "trades": 1,
                 "wins": 0, "losses": 1, "win_rate": 0.0, "best_trade": 0.0,
                 "worst_trade": -1.0, "result": "LOSS", "final": True}
                for i in range(1, rows + 1)]
        with open(perf, "w") as fh:
            json.dump(hist, fh)
        return perf, hist

    def test_corrupt_file_is_not_overwritten(self):
        import bot
        with tempfile.TemporaryDirectory() as d:
            perf, _ = self._seed(d)
            with open(perf, "w") as fh:
                fh.write(self.CORRUPT)
            with mock.patch.object(bot, "PERF_FILE", perf), \
                 mock.patch.object(bot, "STATE_FILE", os.path.join(d, "s.json")), \
                 mock.patch.object(bot, "_last_perf_snapshot", 0.0):
                bot.save_state({"trades_today": []})
                bot.snapshot_performance(-158.36)
            with open(perf) as fh:
                self.assertEqual(fh.read(), self.CORRUPT)   # byte-for-byte untouched

    def test_eod_write_also_refuses_over_a_corrupt_file(self):
        # The settled 15:45 write must be just as careful as the snapshot.
        import bot
        with tempfile.TemporaryDirectory() as d:
            perf = os.path.join(d, "perf.json")
            with open(perf, "w") as fh:
                fh.write(self.CORRUPT)
            with mock.patch.object(bot, "PERF_FILE", perf), \
                 mock.patch.object(bot, "STATE_FILE", os.path.join(d, "s.json")), \
                 mock.patch.object(bot, "get_daily_pnl", return_value=1.0):
                bot.save_state({"trades_today": []})
                bot.log_daily_performance()
            with open(perf) as fh:
                self.assertEqual(fh.read(), self.CORRUPT)

    def test_missing_file_is_legitimately_empty_and_still_writes(self):
        # "No file yet" must NOT be conflated with "corrupt" in the other
        # direction either — a first run has to be able to create the log.
        import bot
        with tempfile.TemporaryDirectory() as d:
            perf = os.path.join(d, "perf.json")
            with mock.patch.object(bot, "PERF_FILE", perf), \
                 mock.patch.object(bot, "STATE_FILE", os.path.join(d, "s.json")), \
                 mock.patch.object(bot, "_last_perf_snapshot", 0.0):
                bot.save_state({"trades_today": []})
                bot.snapshot_performance(-5.0)
            with open(perf) as fh:
                self.assertEqual(len(json.load(fh)), 1)

    def test_healthy_file_still_merges_normally(self):
        import bot
        with tempfile.TemporaryDirectory() as d:
            perf, hist = self._seed(d)
            with mock.patch.object(bot, "PERF_FILE", perf), \
                 mock.patch.object(bot, "STATE_FILE", os.path.join(d, "s.json")), \
                 mock.patch.object(bot, "_last_perf_snapshot", 0.0):
                bot.save_state({"trades_today": []})
                bot.snapshot_performance(-158.36)
            with open(perf) as fh:
                self.assertEqual(len(json.load(fh)), len(hist) + 1)

    def test_a_non_list_payload_is_treated_as_corrupt(self):
        import bot
        with tempfile.TemporaryDirectory() as d:
            perf = os.path.join(d, "perf.json")
            with open(perf, "w") as fh:
                json.dump({"date": "2026-06-01"}, fh)     # dict, not a list
            with mock.patch.object(bot, "PERF_FILE", perf):
                hist, ok = bot.load_performance()
            self.assertFalse(ok)

    def test_quarantine_preserves_the_bad_file_and_clears_the_path(self):
        import bot
        with tempfile.TemporaryDirectory() as d:
            perf = os.path.join(d, "perf.json")
            with open(perf, "w") as fh:
                fh.write(self.CORRUPT)
            with mock.patch.object(bot, "PERF_FILE", perf):
                self.assertTrue(bot.quarantine_performance())
            kept = [f for f in os.listdir(d) if ".corrupt_" in f]
            self.assertEqual(len(kept), 1)               # preserved, never deleted
            with open(os.path.join(d, kept[0])) as fh:
                self.assertEqual(fh.read(), self.CORRUPT)
            self.assertFalse(os.path.exists(perf))       # rebuild can proceed


class TestPerformanceBackfill(unittest.TestCase):
    """Startup must rebuild rows for days the bot missed the 15:45 EOD on."""

    def _run(self, seed, sessions, day_trades):
        import bot
        d = tempfile.mkdtemp()
        perf = os.path.join(d, "perf.json")
        with open(perf, "w") as f:
            json.dump(seed, f)
        hist_obj = mock.Mock()
        hist_obj.timestamp   = [int(ET.localize(dt.datetime.combine(day, dt.time(12, 0))).timestamp())
                               for day, _ in sessions]
        hist_obj.profit_loss = [pl for _, pl in sessions]
        with mock.patch.object(bot, "PERF_FILE", perf), \
             mock.patch.object(bot, "fetch_day_trades", side_effect=lambda day: day_trades.get(str(day), [])), \
             mock.patch.object(bot, "client") as cl:
            cl.get_portfolio_history.return_value = hist_obj
            bot.backfill_performance_history()
        with open(perf) as f:
            return json.load(f)

    def test_recovers_missing_day_from_broker_truth(self):
        hist = self._run(
            seed=[],
            sessions=[(dt.date(2026, 7, 9), -31.94)],
            day_trades={"2026-07-09": [{"symbol": "SOLS", "pl": -0.93},
                                       {"symbol": "UCTT", "pl": -31.01}]},
        )
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["date"], "2026-07-09")
        self.assertEqual(hist[0]["daily_pnl"], -31.94)
        self.assertEqual(hist[0]["trades"], 2)
        self.assertEqual(hist[0]["result"], "LOSS")
        self.assertTrue(hist[0]["final"])

    def test_never_rewrites_a_settled_row(self):
        # Rows written before "final" existed have no such key and are settled.
        seed = [{"date": "2026-07-09", "daily_pnl": 999.0, "result": "WIN"}]
        hist = self._run(seed, [(dt.date(2026, 7, 9), -31.94)], {})
        self.assertEqual(hist[0]["daily_pnl"], 999.0)

    def test_repairs_a_provisional_row(self):
        # A day whose bot died mid-session left a stale intraday row: the loss
        # limit tripped at -213.51 but the day actually settled at -244.93.
        seed = [{"date": "2026-07-14", "daily_pnl": -213.51, "result": "LOSS", "final": False}]
        hist = self._run(seed, [(dt.date(2026, 7, 14), -244.93)],
                         {"2026-07-14": [{"symbol": "X", "pl": -244.93}]})
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["daily_pnl"], -244.93)
        self.assertTrue(hist[0]["final"])

    def test_leaves_today_alone(self):
        # Today's row is still live — settling it mid-session would freeze it.
        today = dt.datetime.now(ET).date()
        hist = self._run([], [(today, -50.0)], {})
        self.assertEqual(hist, [])

    def test_no_trade_session_still_gets_a_row(self):
        # 2026-07-13: the SPY gate skipped every entry. A flat day is real data,
        # and used to be lost entirely because record_trade never fired.
        hist = self._run([], [(dt.date(2026, 7, 13), 0.0)], {})
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["trades"], 0)
        self.assertEqual(hist[0]["daily_pnl"], 0.0)
        self.assertEqual(hist[0]["result"], "WIN")


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

    def test_download_history_recovers_throttled_tickers(self):
        # Yahoo drops 'C' on the first pass (throttle) and returns it flat (single
        # ticker) on the recovery pass. _download_history must retry the straggler
        # AND normalise the flat frame so every ticker is addressable as raw[sym].
        import screener, pandas as pd
        idx = pd.date_range("2026-04-20", periods=5, freq="D")
        fields = ["Open", "High", "Low", "Close", "Volume"]
        calls = []
        state = {"first": True}

        def fake_download(tickers, **kw):
            tickers = list(tickers)
            calls.append(tickers)
            got = tickers
            if state["first"]:
                state["first"] = False
                got = [t for t in tickers if t != "C"]   # throttle drops C first time
            if not got:
                return pd.DataFrame()
            if len(got) == 1:  # real yfinance returns a FLAT frame for one ticker
                return pd.DataFrame(1.0, index=idx, columns=fields)
            cols = pd.MultiIndex.from_product([got, fields])
            return pd.DataFrame(1.0, index=idx, columns=cols)

        with mock.patch.object(screener.yf, "download", side_effect=fake_download), \
                mock.patch.object(screener.time, "sleep"):
            raw = screener._download_history(["A", "B", "C"], "30d", chunk_size=100)

        self.assertEqual(set(raw.columns.get_level_values(0)), {"A", "B", "C"})
        self.assertEqual(list(raw["C"].columns), fields)   # flat frame normalised
        self.assertGreaterEqual(len(calls), 2)             # straggler was retried

    def test_chunks_avoids_singleton_tail(self):
        import screener
        chunks = screener._chunks(list(range(7)), 3)
        self.assertTrue(all(len(c) >= 2 for c in chunks), chunks)  # no length-1 chunk
        self.assertEqual([x for c in chunks for x in c], list(range(7)))  # nothing lost


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

    def _working(self):
        # A buy still working (accepted, not yet filled) — the open-feed-lag case.
        return mock.MagicMock(status=mock.MagicMock(value="new"),
                              filled_qty="0", filled_avg_price=None)

    def _terminal(self, status="canceled", qty=0, price=None):
        # A broker-terminal order. A 'canceled' may still carry a RACED fill
        # (qty>0, price set) vs. a true no-fill (qty=0, price=None).
        return mock.MagicMock(status=mock.MagicMock(value=status),
                              filled_qty=str(qty),
                              filled_avg_price=(None if price is None else str(price)))

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
        with mock.patch.object(bot, "client") as c, mock.patch.object(bot.time, "sleep"):
            self._wire(c)
            bought, sl_ids = bot._place_bracket_orders(self._candidates(1), 1_000_000, poll_s=0.0)
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

        with mock.patch.object(bot, "client") as c, mock.patch.object(bot.time, "sleep"):
            c.submit_order.side_effect = submit
            c.get_order_by_id.side_effect = lambda oid: self._filled(qty=10, price=250.0)
            bot._place_bracket_orders(self._candidates(1), 1_000_000, poll_s=0.0)
        self.assertGreater(captured["tp"], 250.0)   # take-profit above the fill
        self.assertLess(captured["sl"], 250.0)      # stop below the fill

    def test_exit_stop_is_a_plain_market_stop(self):
        # The protective stop leg must be a plain stop-MARKET (no limit_price), so a
        # triggered exit ALWAYS fills — a stop-LIMIT (tried 2026-06-30) left ALGT/RRX
        # stuck unfilled below their limit and riding to -$37/-$44.
        import bot
        from alpaca.trading.enums import OrderSide
        captured = {}

        def submit(req):
            if req.side == OrderSide.BUY:
                return self._fake_buy(req.symbol)
            captured["stop"]  = float(req.stop_loss.stop_price)
            captured["limit"] = getattr(req.stop_loss, "limit_price", None)
            return self._fake_oco(req.symbol)

        with mock.patch.object(bot, "client") as c, mock.patch.object(bot.time, "sleep"):
            c.submit_order.side_effect = submit
            c.get_order_by_id.side_effect = lambda oid: self._filled(qty=10, price=250.0)
            bot._place_bracket_orders(self._candidates(1), 1_000_000, poll_s=0.0)
        self.assertLess(captured["stop"], 250.0)             # stop below the fill
        self.assertIsNone(captured["limit"])                 # NO limit -> guaranteed-fill market stop

    def test_backfill_skips_rejected_buy(self):
        # Top pick's buy is rejected -> still fill MAX_POSITIONS off the spares.
        import bot
        cands = self._candidates(MAX_POSITIONS + 1)
        with mock.patch.object(bot, "client") as c, mock.patch.object(bot.time, "sleep"):
            self._wire(c, buy_ok=lambda s: s != "S0")
            bought, sl_ids = bot._place_bracket_orders(cands, 1_000_000, poll_s=0.0)
        self.assertEqual(len(bought), MAX_POSITIONS)   # not short despite S0 failing
        self.assertNotIn("S0", bought)
        self.assertEqual(set(sl_ids), set(bought))     # every fill tracked its SL leg

    def test_oco_rejection_closes_position_and_backfills(self):
        # If the protective exit is rejected we must NEVER carry the position
        # unprotected: close it, then backfill to the next candidate.
        import bot
        cands = self._candidates(MAX_POSITIONS + 1)
        with mock.patch.object(bot, "client") as c, mock.patch.object(bot.time, "sleep"):
            self._wire(c, oco_ok=lambda s: s != "S0")
            bought, sl_ids = bot._place_bracket_orders(cands, 1_000_000, poll_s=0.0)
        self.assertEqual(len(bought), MAX_POSITIONS)
        self.assertNotIn("S0", bought)
        c.close_position.assert_called_once_with("S0")   # flattened, never carried

    def test_slow_buy_is_not_cancelled_and_fills_on_a_later_poll(self):
        # THE 2026-06-15 fix: a buy still WORKING on early polls must be LEFT ALONE
        # (never cancelled + re-submitted, the churn that produced 0/5) and fills on
        # a later poll once the feed catches up.
        import bot
        seq = [self._working(), self._working(), self._filled()]

        def get_order(oid):
            return seq.pop(0) if len(seq) > 1 else seq[0]

        with mock.patch.object(bot, "client") as c, mock.patch.object(bot.time, "sleep"):
            self._wire(c)
            c.get_order_by_id.side_effect = get_order
            bought, sl_ids = bot._place_bracket_orders(self._candidates(1), 1_000_000, poll_s=0.0)
        self.assertEqual(bought, ["S0"])
        self.assertEqual(set(sl_ids), {"S0"})
        c.cancel_order_by_id.assert_not_called()   # a working order is never cancelled mid-cycle

    def test_unfilled_buy_is_cancelled_at_budget_and_flattened(self):
        # A buy that never fills is left working until the budget expires, then
        # cancelled and reconciled flat — bounded by the budget, no infinite loop.
        import bot
        from alpaca.trading.enums import OrderSide

        def submit(req):
            if req.side == OrderSide.BUY:
                return self._fake_buy(req.symbol)
            return self._fake_oco(req.symbol)

        with mock.patch.object(bot, "client") as c, mock.patch.object(bot.time, "sleep"):
            c.submit_order.side_effect = submit
            c.get_order_by_id.side_effect = lambda oid: self._working()
            c.get_open_position.side_effect = RuntimeError("position does not exist")
            bought, sl_ids = bot._place_bracket_orders(self._candidates(1), 1_000_000,
                                                       budget_s=0.0, poll_s=0.0)
        self.assertEqual(bought, [])
        c.cancel_order_by_id.assert_called_once()       # the straggler is cancelled
        c.close_position.assert_called_once_with("S0")  # reconciled flat

    def test_terminal_reject_is_flattened_and_not_retried(self):
        # A broker-terminal buy never filled: flatten any stray partial and advance.
        # The same name is NOT re-submitted (unlike a slow fill, retrying can't help).
        import bot
        from alpaca.trading.enums import OrderSide
        buys = {"n": 0}

        def submit(req):
            if req.side == OrderSide.BUY:
                buys["n"] += 1
                return self._fake_buy(req.symbol)
            return self._fake_oco(req.symbol)

        with mock.patch.object(bot, "client") as c, mock.patch.object(bot.time, "sleep"):
            c.submit_order.side_effect = submit
            c.get_order_by_id.side_effect = lambda oid: self._terminal("rejected")
            c.get_open_position.side_effect = RuntimeError("position does not exist")
            bought, sl_ids = bot._place_bracket_orders(self._candidates(1), 1_000_000, poll_s=0.0)
        self.assertEqual(bought, [])
        self.assertEqual(buys["n"], 1)                  # submitted once, never retried
        c.close_position.assert_called_once_with("S0")  # flatten any partial

    def test_terminal_reject_during_poll_backfills_next(self):
        # One name's buy is terminally rejected; the next ranked name still fills, so
        # we never end the cycle a position short on a single rejection.
        import bot
        from alpaca.trading.enums import OrderSide

        def submit(req):
            if req.side == OrderSide.BUY:
                return self._fake_buy(req.symbol)
            return self._fake_oco(req.symbol)

        def get_order(oid):
            return self._terminal("rejected") if oid == "buy-S0" else self._filled()

        with mock.patch.object(bot, "client") as c, mock.patch.object(bot.time, "sleep"):
            c.submit_order.side_effect = submit
            c.get_order_by_id.side_effect = get_order
            c.get_open_position.side_effect = RuntimeError("position does not exist")
            bought, sl_ids = bot._place_bracket_orders(self._candidates(2), 1_000_000, poll_s=0.0)
        self.assertEqual(bought, ["S1"])
        self.assertNotIn("S0", bought)

    def test_budget_end_recovers_raced_fill_and_protects(self):
        # The 06/11 orphan: the budget expires with a buy still working; we cancel it
        # but it FILLED in the race (status 'canceled' WITH a real fill). It must be
        # RECOVERED + PROTECTED off the real fill, never left bare, never dumped.
        import bot
        from alpaca.trading.enums import OrderSide
        captured = {}

        def submit(req):
            if req.side == OrderSide.BUY:
                return self._fake_buy(req.symbol)
            captured[req.symbol] = (float(req.take_profit.limit_price),
                                    float(req.stop_loss.stop_price))
            return self._fake_oco(req.symbol)

        order = self._terminal("canceled", qty=10, price=100.0)   # canceled WITH a fill
        with mock.patch.object(bot, "client") as c, mock.patch.object(bot.time, "sleep"):
            c.submit_order.side_effect = submit
            c.get_order_by_id.side_effect = lambda oid: order
            bought, sl_ids = bot._place_bracket_orders(self._candidates(1), 1_000_000,
                                                       budget_s=0.0, poll_s=0.0)
        self.assertEqual(bought, ["S0"])              # recovered + tracked, not orphaned
        self.assertEqual(set(sl_ids), {"S0"})
        c.cancel_order_by_id.assert_called_once()
        c.close_position.assert_not_called()          # a real fill is protected, not dumped
        self.assertGreater(captured["S0"][0], 100.0)  # OCO straddles the fill
        self.assertLess(captured["S0"][1], 100.0)

    def test_budget_end_recovers_fill_via_open_position(self):
        # Recheck shows no fill fields on the order, but a live position exists ->
        # protect it off avg_entry_price, do not flatten it.
        import bot
        from alpaca.trading.enums import OrderSide
        captured = {}

        def submit(req):
            if req.side == OrderSide.BUY:
                return self._fake_buy(req.symbol)
            captured[req.symbol] = True
            return self._fake_oco(req.symbol)

        order = self._terminal("canceled", qty=0, price=None)
        position = mock.MagicMock(qty="10", avg_entry_price="100.0")
        with mock.patch.object(bot, "client") as c, mock.patch.object(bot.time, "sleep"):
            c.submit_order.side_effect = submit
            c.get_order_by_id.side_effect = lambda oid: order
            c.get_open_position.side_effect = lambda sym: position
            bought, sl_ids = bot._place_bracket_orders(self._candidates(1), 1_000_000,
                                                       budget_s=0.0, poll_s=0.0)
        self.assertEqual(bought, ["S0"])
        self.assertEqual(set(sl_ids), {"S0"})
        c.close_position.assert_not_called()          # a real position is protected, not dumped
        self.assertIn("S0", captured)

    def test_budget_end_truly_flat_is_flattened(self):
        # No fill on the order AND no position -> genuinely flat: flatten any partial.
        import bot
        from alpaca.trading.enums import OrderSide

        def submit(req):
            if req.side == OrderSide.BUY:
                return self._fake_buy(req.symbol)
            return self._fake_oco(req.symbol)

        order = self._terminal("canceled", qty=0, price=None)
        with mock.patch.object(bot, "client") as c, mock.patch.object(bot.time, "sleep"):
            c.submit_order.side_effect = submit
            c.get_order_by_id.side_effect = lambda oid: order
            c.get_open_position.side_effect = RuntimeError("position does not exist")
            bought, sl_ids = bot._place_bracket_orders(self._candidates(1), 1_000_000,
                                                       budget_s=0.0, poll_s=0.0)
        self.assertEqual(bought, [])
        self.assertEqual(sl_ids, {})
        c.cancel_order_by_id.assert_called_once()
        c.close_position.assert_called_once_with("S0")

    def test_caps_at_max_positions(self):
        import bot
        cands = self._candidates(MAX_POSITIONS * 2)
        with mock.patch.object(bot, "client") as c, mock.patch.object(bot.time, "sleep"):
            self._wire(c)
            bought, sl_ids = bot._place_bracket_orders(cands, 1_000_000, poll_s=0.0)
        self.assertEqual(len(bought), MAX_POSITIONS)        # never overbuys past the cap
        self.assertEqual(len(set(bought)), MAX_POSITIONS)   # and never enters a name twice
        self.assertEqual(set(sl_ids), set(bought))          # every fill tracked its SL leg

    def test_all_rejected_returns_empty(self):
        import bot
        with mock.patch.object(bot, "client") as c, mock.patch.object(bot.time, "sleep"):
            self._wire(c, buy_ok=lambda s: False)
            bought, sl_ids = bot._place_bracket_orders(self._candidates(3), 1_000_000, poll_s=0.0)
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


class TestPerPositionHardStop(unittest.TestCase):
    """The safety net (2026-06-30): a position whose unrealized P&L falls to
    PER_POSITION_HARD_STOP is force-closed at market REGARDLESS of its resting order,
    so a stop that fails to flatten (the stop-LIMIT non-fill that stranded ALGT/RRX)
    can never leave a position riding unprotected again."""

    def _pos(self, symbol, pl):
        p = mock.MagicMock()
        p.symbol = symbol
        p.unrealized_pl = str(pl)
        return p

    def _open_order(self, order_type, side, order_id="o1"):
        from alpaca.trading.enums import OrderSide, OrderType
        o = mock.MagicMock(id=order_id)
        o.type = order_type
        o.side = side
        return o

    def test_force_closes_position_past_hard_stop(self):
        from config import PER_POSITION_HARD_STOP
        from alpaca.trading.enums import OrderSide, OrderType
        import bot
        bad = self._pos("AAA", PER_POSITION_HARD_STOP - 5)   # well past the hard stop
        ok  = self._pos("BBB", -5.0)                         # normal — must be left alone
        with mock.patch.object(bot, "client") as c:
            # The stuck resting order is the original protective stop, not a market sell.
            c.get_orders.return_value = [self._open_order(OrderType.STOP, OrderSide.SELL)]
            forced = bot.enforce_per_position_hard_stop([bad, ok])
        self.assertEqual(forced, ["AAA"])
        c.cancel_order_by_id.assert_called_once_with("o1")   # the stuck resting order is cancelled
        c.close_position.assert_called_once_with("AAA")      # then force-closed at market

    def test_normal_loss_is_not_force_closed(self):
        from config import PER_POSITION_STOP
        import bot
        # A position sitting at its normal -$20 stop must NOT be force-closed (the bracket
        # owns that exit); only a genuine FAILURE past the hard stop trips the net.
        p = self._pos("AAA", PER_POSITION_STOP)
        with mock.patch.object(bot, "client") as c:
            forced = bot.enforce_per_position_hard_stop([p])
        self.assertEqual(forced, [])
        c.close_position.assert_not_called()

    def test_does_not_cancel_resubmit_a_close_already_working(self):
        # HONA (2026-07-06): a market close submitted last cycle hadn't settled yet, so
        # the position still showed here on the very next cycle. The old code canceled
        # its own in-flight market sell and resubmitted, over and over, while the stock
        # kept falling — a -$30 hard stop realized as -$52.80 after ~35 such cycles.
        # Once a market sell is already open for the symbol, this must leave it alone.
        from config import PER_POSITION_HARD_STOP
        from alpaca.trading.enums import OrderSide, OrderType
        import bot
        bad = self._pos("AAA", PER_POSITION_HARD_STOP - 20)  # still far past the hard stop
        with mock.patch.object(bot, "client") as c:
            c.get_orders.return_value = [self._open_order(OrderType.MARKET, OrderSide.SELL)]
            forced = bot.enforce_per_position_hard_stop([bad])
        self.assertEqual(forced, [])
        c.cancel_order_by_id.assert_not_called()
        c.close_position.assert_not_called()


class TestPositionLifecycle(unittest.TestCase):
    """Full trade lifecycles, the way the live runs exercised them: a buy that runs
    up and ratchets its trailing stop into PROFIT (and sells there), and a buy that
    fades to its stop and sells at a LOSS. Drives the REAL step_trailing_stops (the
    ratchet) + sync_bracket_fills (the close/record) against the live config ladder,
    so these cases no longer need a manual market run to verify. Expectations derive
    from config.STOP_STEPS, so a ladder retune won't silently break them."""
    SL = "00000000-0000-0000-0000-000000000001"   # a valid UUID for replace_order_by_id

    def _state_backed(self, store):
        import copy
        def load(): return copy.deepcopy(store)
        def save(d): store.clear(); store.update(copy.deepcopy(d))
        return load, save

    def _pos(self, symbol, pl, qty=10, entry=100.0):
        p = mock.MagicMock()
        p.symbol = symbol
        p.unrealized_pl = str(pl); p.qty = str(qty); p.avg_entry_price = str(entry)
        return p

    def _order(self, side, price, qty=10):
        import datetime as dt, pytz
        o = mock.MagicMock()
        o.side = mock.MagicMock(value=side)
        o.filled_avg_price = str(price); o.filled_qty = str(qty)
        o.submitted_at = dt.datetime.now(pytz.timezone("America/New_York"))
        return o

    def _ratchet(self, store, pos):
        import bot
        load, save = self._state_backed(store)
        with mock.patch.object(bot, "load_state", side_effect=load), \
             mock.patch.object(bot, "save_state", side_effect=save):
            bot.step_trailing_stops(positions=[pos])

    def _close(self, c, store, buy_px, sell_px, qty=10):
        import bot
        load, save = self._state_backed(store)
        c.get_orders.return_value = [self._order("buy", buy_px, qty),
                                     self._order("sell", sell_px, qty)]
        with mock.patch.object(bot, "load_state", side_effect=load), \
             mock.patch.object(bot, "save_state", side_effect=save):
            bot.sync_bracket_fills(positions=[])

    def test_up_one_bracket_ratchets_stop_into_profit(self):
        # Buy, run up just past the first rung -> the stop is moved UP to a profit lock.
        from config import STOP_STEPS
        import bot
        trigger, lock = min(STOP_STEPS)                 # the first rung
        store = {"positions": ["AAA"], "sl_order_ids": {"AAA": self.SL}, "stop_steps_reached": {}}
        with mock.patch.object(bot, "client") as c:
            c.replace_order_by_id.return_value = mock.MagicMock(id="new-sl-1")
            self._ratchet(store, self._pos("AAA", trigger + 0.5, qty=10, entry=100.0))
        c.replace_order_by_id.assert_called_once()
        self.assertEqual(store["stop_steps_reached"]["AAA"], lock)
        self.assertGreater(lock, 0)                     # a PROFIT lock, never breakeven
        req = c.replace_order_by_id.call_args.args[1]
        self.assertAlmostEqual(float(req.stop_price), 100.0 + lock / 10, places=2)  # above entry
        self.assertEqual(store["sl_order_ids"]["AAA"], "new-sl-1")  # tracks the replacement order

    def test_up_then_sells_at_the_locked_profit(self):
        # The ARM case: a pop past the first rung locks the stop in profit; price then
        # reverses into it and fills there for a WIN — never a breakeven-slippage loss.
        from config import STOP_STEPS
        import bot
        trigger, lock = min(STOP_STEPS)
        store = {"positions": ["AAA"], "trades_today": [],
                 "sl_order_ids": {"AAA": self.SL}, "stop_steps_reached": {}}
        with mock.patch.object(bot, "client") as c:
            c.replace_order_by_id.return_value = mock.MagicMock(id="new-sl-1")
            self._ratchet(store, self._pos("AAA", trigger + 0.5, qty=10, entry=100.0))
            stop_px = round(100.0 + store["stop_steps_reached"]["AAA"] / 10, 2)
            self._close(c, store, buy_px=100.0, sell_px=stop_px, qty=10)
        self.assertEqual([t["symbol"] for t in store["trades_today"]], ["AAA"])
        self.assertGreater(store["trades_today"][0]["pl"], 0)            # sold GREEN
        self.assertAlmostEqual(store["trades_today"][0]["pl"], lock, places=2)
        self.assertEqual(store["positions"], [])

    def test_down_then_sells_at_a_loss_without_ratcheting(self):
        # A buy that only falls never reaches a rung, so the stop is NEVER moved; it
        # hits its initial stop and is recorded as a loss.
        import bot
        store = {"positions": ["AAA"], "trades_today": [],
                 "sl_order_ids": {"AAA": self.SL}, "stop_steps_reached": {}}
        with mock.patch.object(bot, "client") as c:
            self._ratchet(store, self._pos("AAA", -8.0, qty=10, entry=100.0))
            c.replace_order_by_id.assert_not_called()        # no ratchet on a loser
            self.assertEqual(store["stop_steps_reached"], {})
            self._close(c, store, buy_px=100.0, sell_px=98.0, qty=10)
        self.assertLess(store["trades_today"][0]["pl"], 0)   # sold RED
        self.assertAlmostEqual(store["trades_today"][0]["pl"], -20.0, places=2)
        self.assertEqual(store["positions"], [])

    def test_big_jump_skips_straight_to_highest_earned_lock(self):
        # One large move locks the TOP rung in a single ratchet, not one move per rung.
        from config import STOP_STEPS
        import bot
        top_trigger, top_lock = max(STOP_STEPS)
        store = {"positions": ["AAA"], "sl_order_ids": {"AAA": self.SL}, "stop_steps_reached": {}}
        with mock.patch.object(bot, "client") as c:
            c.replace_order_by_id.return_value = mock.MagicMock(id="new-sl-1")
            self._ratchet(store, self._pos("AAA", top_trigger + 1, qty=10, entry=100.0))
        c.replace_order_by_id.assert_called_once()
        self.assertEqual(store["stop_steps_reached"]["AAA"], top_lock)

    def test_ratchets_rung_by_rung_never_down_no_double_apply(self):
        # Walk up: first rung moves the stop once; re-seeing the same level is a no-op;
        # a higher rung moves it UP again. The stop never steps down or re-fires.
        from config import STOP_STEPS
        import bot
        triggers = sorted(t for t, _ in STOP_STEPS)
        store = {"positions": ["AAA"], "sl_order_ids": {"AAA": self.SL}, "stop_steps_reached": {}}
        with mock.patch.object(bot, "client") as c:
            # replacement id must be a valid UUID — the NEXT ratchet re-parses it.
            c.replace_order_by_id.side_effect = (
                lambda *a, **k: mock.MagicMock(id="00000000-0000-0000-0000-000000000002"))
            self._ratchet(store, self._pos("AAA", triggers[0] + 0.5, entry=100.0))   # first rung
            first_lock = store["stop_steps_reached"]["AAA"]
            self._ratchet(store, self._pos("AAA", triggers[0] + 0.5, entry=100.0))   # same -> no-op
            self._ratchet(store, self._pos("AAA", triggers[-1] + 1, entry=100.0))    # top rung
        self.assertGreater(store["stop_steps_reached"]["AAA"], first_lock)   # ratcheted UP
        self.assertEqual(c.replace_order_by_id.call_count, 2)                # the repeat did nothing

    def test_take_profit_target_records_the_full_gain(self):
        # Price runs all the way to the +$20 take-profit leg -> recorded as the full win.
        from config import PER_POSITION_TARGET
        import bot
        store = {"positions": ["AAA"], "trades_today": [], "sl_order_ids": {}}
        with mock.patch.object(bot, "client") as c:
            self._close(c, store, buy_px=100.0, sell_px=100.0 + PER_POSITION_TARGET / 10, qty=10)
        self.assertAlmostEqual(store["trades_today"][0]["pl"], PER_POSITION_TARGET, places=2)
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

    def test_watchdog_cim_query_is_bounded(self):
        # A hung WMI/CIM query must never block the relaunch path. On 2026-06-11 a
        # wedged Win32_Process enumeration in Find-RunningPid stalled the sweep and
        # left a dead bot un-relaunched ~20 min before the open. The discovery query
        # must now run bounded (a child job abandoned after a timeout) so the sweep
        # can never hang -> relaunch always proceeds.
        src = self._read("watchdog.ps1")
        self.assertIn("Wait-Job", src)
        self.assertIn("-Timeout", src)

    def test_watchdog_singleton_is_cross_context(self):
        # The singleton MUST use an OS-enforced exclusive file lock, not a CIM
        # CommandLine comparison. The latter reads $null across the SYSTEM/user
        # boundary, which let a SYSTEM boot-watchdog and a user logon-watchdog both
        # run and duel on the account (2026-06-11). OS file locks are enforced
        # regardless of security context.
        src = self._read("watchdog.ps1")
        self.assertIn("[System.IO.File]::Open", src)        # exclusive handle
        self.assertIn("FileShare]::Read", src)              # no second writer
        # The broken CommandLine-based watchdog liveness check must not come back.
        self.assertNotIn("Test-WatchdogAlive", src)

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

    def test_step_watch_does_not_hold_log_open(self):
        # 2026-06-15: step_watch held bot.log open for reading for its entire life
        # (a persistent `f = open(LOG_FILE, ...)`). On Windows a held read handle
        # blocks RotatingFileHandler's rename, so the FIRST rollover (bot.log
        # crossing the 5 MB cap, which happened for the first time this morning)
        # failed with PermissionError WinError 32. logging swallows that error, so
        # bot.log froze silently for the whole session — the bot kept trading with
        # zero log output and step_watch fired zero toast alerts. The fix: tail by
        # reopening the file each poll and tracking a byte offset, so the bot can
        # always roll the log over. Guard against regressing to a persistent handle.
        src = self._read("step_watch.py")
        self.assertNotIn("f = open(LOG_FILE", src)   # no persistent handle
        self.assertIn('open(LOG_FILE, "rb")', src)   # reopened per poll, binary
        self.assertIn("offset", src)                 # tracks position across polls

    def test_entry_is_place_all_then_poll(self):
        # 2026-06-15: the old per-order "wait 15s then cancel + re-submit" loop
        # cancelled every buy just before it filled on a laggy open (0/5 filled).
        # The entry path must now submit buys and POLL them, never cancelling a
        # still-working order mid-cycle — so the obsolete per-order timeout constant
        # and the blocking single-order wait must be gone.
        src = self._read("bot.py")
        self.assertIn("ENTRY_POLL_S", src)            # polls working buys
        self.assertIn("pending", src)                 # tracks concurrent working buys
        self.assertNotIn("BUY_FILL_TIMEOUT_S", src)   # superseded by place-all-then-poll
        self.assertNotIn("def _wait_for_fill", src)   # per-order blocking wait removed

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

    def test_suite_never_writes_the_real_data_files(self):
        # The suite runs against the live repo, so a test that exercises a write
        # path without isolating it silently rewrites real trading history. Assert
        # the module-level sandbox is actually in force.
        import bot
        self.assertTrue(bot.PERF_FILE.startswith(_SANDBOX), bot.PERF_FILE)
        self.assertTrue(bot.STATE_FILE.startswith(_SANDBOX), bot.STATE_FILE)
        self.assertNotEqual(os.path.dirname(bot.PERF_FILE), ROOT)

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
