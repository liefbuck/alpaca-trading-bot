"""
Offline replay harness for the exit rules.

WHY THIS EXISTS
---------------
Every trading change in this repo's history was shipped LIVE on a hunch and then
judged on ~10 days of noise -- which is how the stop ladder ended up tuned in
OPPOSITE directions twice (8cc853c loosened the first rung, 3f1686a put it back),
and how a month of paper losses bought information a laptop could have produced
in seconds. Ten days of this strategy is statistically indistinguishable from
nothing; you cannot learn anything from it.

This replays the REAL ladder rule (trading_math.select_stop_pl -- imported, never
re-implemented, so the harness cannot drift from the bot) against REAL minute
bars, over REAL historical entries taken from broker fills. It answers "what
would this bracket have done" in seconds.

Bracket prices are derived here from the ExitPolicy under test rather than via
compute_bracket_prices(), because that function reads the CONFIGURED target/stop
from config.py -- which is exactly what a sweep needs to vary. The two must stay
consistent: entry +/- (target|stop)/qty, which is what compute_bracket_prices
does modulo its penny-rounding and its degenerate-price floors.

SCOPE -- READ THIS BEFORE BELIEVING ANY NUMBER
----------------------------------------------
It replays EXITS over the entries the screener actually took. It does NOT re-run
the screener, so it can only answer "would a different bracket have done better
on these picks" -- never "would a different signal work".

Two data traps it now handles, both of which produced badly wrong answers first
time round and would silently do so again:
  * the account's pre-2026-06-11 history is a DIFFERENT strategy (naked market
    in/out, order_class=simple, no stop, no target). Replaying stop rules over
    entries that never had a stop is meaningless. Only bracket-era days count.
  * a symbol can be round-tripped twice in a day, so trades are paired FIFO.
    Averaging a day's buys against its sells invents a trade never held.

FIDELITY
--------
Every ambiguity is resolved AGAINST the strategy, so a policy has to be good
rather than lucky:
  * a STOP is a market order: it triggers when the bar's low crosses it and
    fills at the NEXT bar's open -- never at the trigger price.
  * a TARGET is a limit order: it fills exactly at its price, never better.
  * if one bar could have touched both, the STOP wins.
  * the ladder ratchets on the bar CLOSE, not its high: the live bot only sees
    prices at ~4s poll instants and cannot catch a one-second spike. Ratcheting
    on the high locks profit the real bot would have missed, which flatters the
    result by ~$2/trade.
  * a newly raised stop only applies from the NEXT bar, so it can never lock a
    profit and stop out against the very bar it locked on.

Those choices were each made on principle, not fitted -- and picking the
conservative option in every dimension independently is what lands closest to
reality (17% error vs 25-69% for the alternatives), which is corroboration
rather than curve-fitting.

KNOWN RESIDUAL: even calibrated, the sim comes out ~17% KINDER than the account
actually did. So treat every number here as an OPTIMISTIC UPPER BOUND -- if a
policy loses in here, it loses harder in real life. Some of that gap is real
history the sim deliberately does not model: the daily loss-limit close_all, the
per-position hard stop, the 15:45 EOD force-close, the reverted stop-limit era,
and the stretches where the live bot was WEDGED and stopped ratcheting at all.
--validate therefore scores only trades the BRACKET itself exited (oco/stop,
oco/limit) and reports the rest separately as out of scope.

ALWAYS run --validate before trusting a sweep. A harness that cannot reproduce
reality is worse than none, because it produces confident nonsense.

Usage:
    python backtest.py --validate          # reproduce actual results (trust check)
    python backtest.py --sweep             # compare candidate exit policies
    python backtest.py --days 60           # how much history to pull
"""
import os
import sys
import pickle
import argparse
import datetime
import collections
import statistics as st
from dataclasses import dataclass, field

import pytz
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

from alpaca_client import make_trading_client
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# The REAL rules. Imported, never copied: if the bot's ladder logic changes, the
# harness changes with it, and a stale backtest can't quietly lie to us.
from trading_math import select_stop_pl
from config import PER_POSITION_TARGET, PER_POSITION_STOP, STOP_STEPS

ET = pytz.timezone("America/New_York")
CACHE_DIR = os.path.join(BASE_DIR, ".backtest_cache")
EOD_HHMM = (15, 45)   # bot.py force-closes here


@dataclass
class ExitPolicy:
    """A candidate bracket. `steps` is a STOP_STEPS-shaped ladder."""
    name: str
    target: float = PER_POSITION_TARGET
    stop: float = PER_POSITION_STOP          # negative
    steps: list = field(default_factory=lambda: list(STOP_STEPS))
    use_ladder: bool = True


# ---------------------------------------------------------------- data loading
def _cache(path, build):
    os.makedirs(CACHE_DIR, exist_ok=True)
    full = os.path.join(CACHE_DIR, path)
    if os.path.exists(full):
        with open(full, "rb") as fh:
            return pickle.load(fh)
    val = build()
    with open(full, "wb") as fh:
        pickle.dump(val, fh)
    return val


def load_trades(days_back: int):
    """Rebuild real round-trip trades from broker fills.

    Two correctness traps, both of which produced badly wrong numbers before:

    1. FIFO PAIRING, not aggregate-by-symbol-per-day. A symbol can be traded
       more than once in a day (HPE on 05-29 was bought 09:33 AND 10:19).
       Averaging all of a day's buys against all its sells invents a single
       fictitious trade at a blended price that was never held.

    2. ONLY the bracket bot. The account's history before 2026-06-11 is a
       DIFFERENT strategy: naked market in / market out, order_class=simple,
       no stop and no target at all. Replaying stop rules over entries that
       never had a stop is meaningless, and mixing the two eras corrupts every
       aggregate. We keep only days where the OCO bracket was actually used.
    """
    def build():
        c = make_trading_client(paper=True)
        fills, until = [], datetime.datetime.now(ET)
        floor = datetime.datetime.now(ET) - datetime.timedelta(days=days_back)
        for _ in range(60):
            batch = c.get_orders(GetOrdersRequest(status=QueryOrderStatus.CLOSED, until=until,
                                                  limit=500, direction="desc", nested=False))
            if not batch:
                break
            fills += [o for o in batch
                      if o.filled_qty and float(o.filled_qty) > 0 and o.filled_avg_price]
            oldest = min(o.submitted_at for o in batch)
            until = oldest - datetime.timedelta(seconds=1)
            if len(batch) < 500 or oldest.astimezone(ET) < floor:
                break

        rows = [{"sym": o.symbol, "side": o.side.value, "cls": o.order_class.value,
                 "type": o.order_type.value, "qty": float(o.filled_qty),
                 "px": float(o.filled_avg_price), "at": o.filled_at.astimezone(ET)}
                for o in fills]

        # The bracket bot always attaches an OCO exit; the pre-06-11 strategy never did.
        bracket_days = {r["at"].date() for r in rows if r["cls"] == "oco"}

        byday = collections.defaultdict(list)
        for r in rows:
            byday[(r["sym"], r["at"].date())].append(r)

        trades = []
        for (sym, day), os_ in byday.items():
            if day not in bracket_days:
                continue
            os_.sort(key=lambda x: x["at"])
            lots = []
            for o in os_:
                if o["side"] == "buy":
                    lots.append([o["qty"], o["px"], o["at"]])
                    continue
                q = o["qty"]
                while q > 1e-9 and lots:
                    lot = lots[0]
                    take = min(q, lot[0])
                    trades.append({"day": day, "sym": sym, "qty": take,
                                   "entry": lot[1], "entry_at": lot[2],
                                   "exit_cls": o["cls"], "exit_type": o["type"],
                                   "actual_pl": (o["px"] - lot[1]) * take})
                    lot[0] -= take
                    q -= take
                    if lot[0] <= 1e-9:
                        lots.pop(0)
        trades.sort(key=lambda t: (t["day"], t["entry_at"]))
        return trades
    return _cache(f"trades_fifo_bracket_{days_back}d.pkl", build)


def load_bars(trades):
    """Minute bars per (symbol, day), entry-day session only."""
    def build():
        dc = StockHistoricalDataClient(os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY"))
        byday = collections.defaultdict(set)
        for t in trades:
            byday[t["day"]].add(t["sym"])
        out = {}
        for day, syms in sorted(byday.items()):
            start = ET.localize(datetime.datetime.combine(day, datetime.time(9, 30)))
            end = ET.localize(datetime.datetime.combine(day, datetime.time(16, 0)))
            try:
                df = dc.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=sorted(syms), timeframe=TimeFrame.Minute,
                    start=start, end=end)).df
            except Exception as e:
                print(f"  bars failed {day}: {e}", file=sys.stderr)
                continue
            if df is None or len(df) == 0:
                continue
            for sym in sorted(syms):
                try:
                    sub = df.loc[sym]
                except KeyError:
                    continue
                rows = [(ts.tz_convert(ET), float(r.open), float(r.high),
                         float(r.low), float(r.close))
                        for ts, r in sub.iterrows()]
                out[(sym, day)] = sorted(rows)
            print(f"  bars {day}: {len(syms)} symbols", file=sys.stderr)
        return out
    return _cache(f"bars_fifo_{len(trades)}.pkl", build)


# ------------------------------------------------------------------ simulation
def simulate(trade, bars, pol: ExitPolicy):
    """Replay one trade's exit under `pol`. Returns (pl, reason)."""
    qty, entry = trade["qty"], trade["entry"]
    series = [b for b in bars if b[0] >= trade["entry_at"]]
    if not series:
        return None, "no-bars"

    target_price = entry + pol.target / qty
    stop_price = entry + pol.stop / qty          # pol.stop is negative
    current_step = -1
    eod = ET.localize(datetime.datetime.combine(trade["day"], datetime.time(*EOD_HHMM)))

    for i, (ts, o, h, l, c) in enumerate(series):
        nxt_open = series[i + 1][1] if i + 1 < len(series) else c

        if ts >= eod:
            return (c - entry) * qty, "eod"

        # STOP FIRST: a bar that could have hit both is resolved against us.
        if l <= stop_price:
            fill = min(stop_price, nxt_open)     # market order -> next print, may gap
            return (fill - entry) * qty, "stop"

        if h >= target_price:
            return (target_price - entry) * qty, "target"   # limit fills at its price

        # Ratchet on the bar CLOSE, effective NEXT bar. Close (not high) because the
        # live bot only sees prices at ~4s poll instants; crediting it the exact
        # intra-minute high locks profits it never actually saw.
        if pol.use_ladder:
            new = select_stop_pl((c - entry) * qty, current_step, pol.steps)
            if new is not None:
                current_step = new
                stop_price = entry + new / qty

    return (series[-1][4] - entry) * qty, "eod"


def replay(trades, bars, pol: ExitPolicy):
    res = []
    for t in trades:
        b = bars.get((t["sym"], t["day"]))
        if not b:
            continue
        pl, reason = simulate(t, b, pol)
        if pl is None:
            continue
        res.append({**t, "sim_pl": pl, "reason": reason})
    return res


def summarize(res, label):
    pls = [r["sim_pl"] for r in res]
    if not pls:
        return f"{label:<34} (no data)"
    w = [p for p in pls if p > 0]
    wr = len(w) / len(pls) * 100
    exp = st.mean(pls)
    return (f"{label:<34} total ${sum(pls):+9,.2f}   exp ${exp:+6.2f}/trade   "
            f"win {wr:4.1f}%   n={len(pls)}")


# ----------------------------------------------------------------------- entry
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=70)
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--sweep", action="store_true")
    a = ap.parse_args()
    if not (a.validate or a.sweep):
        a.validate = a.sweep = True

    print("loading trades from broker fills...", file=sys.stderr)
    trades = load_trades(a.days)
    print(f"  {len(trades)} round-trip trades", file=sys.stderr)
    print("loading minute bars (cached after first run)...", file=sys.stderr)
    bars = load_bars(trades)
    covered = [t for t in trades if (t["sym"], t["day"]) in bars]
    print(f"  bars for {len(covered)}/{len(trades)} trades\n", file=sys.stderr)

    if a.validate:
        cur = ExitPolicy("current config")
        res = replay(covered, bars, cur)
        # Score ONLY trades the bracket itself exited. The rest were decided by
        # mechanisms this harness deliberately does not model (loss-limit
        # close_all, per-position hard stop, 15:45 EOD, the reverted stop-limit
        # era) -- counting them would grade the sim on work it never claimed.
        inscope = [r for r in res if r["exit_cls"] == "oco" and r["exit_type"] in ("stop", "limit")]
        out = [r for r in res if r not in inscope]
        sim = sum(r["sim_pl"] for r in inscope)
        act = sum(r["actual_pl"] for r in inscope)
        print("=" * 78)
        print("VALIDATION - can the harness reproduce what actually happened?")
        print("=" * 78)
        print(f"  config          : target +${cur.target} / stop ${cur.stop} / ladder {cur.steps}")
        print(f"  scored on       : {len(inscope)} trades the BRACKET exited "
              f"({len(out)} excluded as out of scope)")
        print(f"  ACTUAL realised : ${act:+,.2f}   (${act/len(inscope):+.2f}/trade)")
        print(f"  SIMULATED       : ${sim:+,.2f}   (${sim/len(inscope):+.2f}/trade)")
        err = abs(sim - act) / max(abs(act), 1) * 100
        print(f"  error           : ${sim-act:+,.2f}  ({err:.1f}%)")
        print(f"  verdict         : {'TRUSTWORTHY (<20% error)' if err < 20 else 'DO NOT TRUST SWEEPS'}")
        agree = [r for r in inscope if (r["sim_pl"] > 0) == (r["actual_pl"] > 0)]
        print(f"  sign agreement  : {len(agree)/len(inscope)*100:.1f}%")
        print(f"  bias            : sim is ${sim-act:+,.2f} KINDER than reality -> "
              f"read every sweep number as an OPTIMISTIC UPPER BOUND")
        print(f"  sim exit mix    : {dict(collections.Counter(r['reason'] for r in inscope))}")
        print()

    if a.sweep:
        print("=" * 78)
        print("SWEEP - would a different bracket have done better on these entries?")
        print("=" * 78)
        cands = [
            ExitPolicy("current [(5,3),(12,7),(22,15),(32,25)]"),
            ExitPolicy("no ladder at all", use_ladder=False),
            ExitPolicy("8cc853c ladder (10,3),(15,8),(18,13)", steps=[(10, 3), (15, 8), (18, 13)]),
            ExitPolicy("first rung 12 not 5", steps=[(12, 3), (22, 15), (32, 25)]),
            ExitPolicy("first rung 20 not 5", steps=[(20, 10), (32, 25)]),
            ExitPolicy("tighter stop -10", stop=-10.0),
            ExitPolicy("wider stop -40", stop=-40.0),
            ExitPolicy("target +20 (reachable)", target=20.0),
            ExitPolicy("target +20, no ladder", target=20.0, use_ladder=False),
            ExitPolicy("target +80, stop -40, no ladder", target=80.0, stop=-40.0, use_ladder=False),
        ]
        rows = []
        for pol in cands:
            rows.append((pol.name, replay(covered, bars, pol)))
        for name, res in sorted(rows, key=lambda r: -sum(x["sim_pl"] for x in r[1])):
            print("  " + summarize(res, name))
        print()
        best = max(rows, key=lambda r: sum(x["sim_pl"] for x in r[1]))
        print(f"  best-of-sweep: {best[0]}  ${sum(x['sim_pl'] for x in best[1]):+,.2f}")
        print("  NOTE: 'best' here is the best of a handful of variants on ONE 2-month")
        print("  sample. If nothing clears zero, no bracket saves this signal.")


if __name__ == "__main__":
    main()
