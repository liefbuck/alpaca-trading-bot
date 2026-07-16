"""
Does the SCREENER actually pick better than chance?

WHY THIS EXISTS
---------------
backtest.py answers "would a different bracket have done better on these picks"
-- and the answer was no: every exit policy loses, all with 95% CIs entirely
negative. That leaves exactly one question standing, and it is the only one that
matters: does the entry SIGNAL contain any information at all?

If the momentum ranking cannot beat symbols drawn at random from its own
candidate pool, there is nothing to tune. No stop, no target, no ladder, no
position size rescues a selection rule with no edge, and every hour spent on the
exits is spent on the wrong thing.

THE EXPERIMENT -- controlled substitution
-----------------------------------------
For every real trade the bot made, hold EVERYTHING constant except the one
variable under test:
    same day, same entry minute, same notional, same bracket, same exit rules
    -> only the SYMBOL changes.
That isolates selection. A difference in outcome can only come from which stock
was chosen, which is precisely the screener's job.

Both arms are SIMULATED identically (entry at the bar open of the real entry
minute). Comparing a simulated random arm against the screener's REALISED fills
would hand random a free fill and quietly bias the test against the screener --
so the screener is re-simulated under the same rules it is being judged by. Its
realised result is printed alongside for context only, never as the comparison.

Randomness is answered with a DISTRIBUTION, not a single draw. One random
portfolio proves nothing; it is one sample from a wide distribution and can beat
or lose to anything by luck. We run N_DRAWS whole alternate histories and ask
where the screener falls among them. A percentile near 50 means the signal is
worth exactly nothing. Near 0 means it is actively selecting losers -- worse than
no signal at all.

NO LOOKAHEAD
------------
The random arm uses no information whatsoever, so it cannot cheat. The eligible
pool is rebuilt per-day from daily bars ending strictly BEFORE that day, so the
liquidity filter never sees the future either.

Usage:
    python entry_backtest.py                 # the full comparison
    python entry_backtest.py --draws 200     # fewer alternate histories (faster)
"""
import os
import sys
import random
import pickle
import argparse
import datetime
import statistics as st

import pytz
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# Reuse the VALIDATED exit engine rather than writing a second one. backtest.py's
# simulate() is pinned to 17.2% error against the real account and biased KIND;
# a private copy here could drift and quietly grade a bracket nobody runs.
import backtest as bt
from config import POSITION_NOTIONAL, MIN_SHARE_PRICE, MIN_AVG_DOLLAR_VOL

ET = pytz.timezone("America/New_York")
CACHE_DIR = os.path.join(BASE_DIR, ".backtest_cache")
POOL_SIZE = 120        # random-pool breadth; enough to characterise "a liquid name"
N_DRAWS = 400          # alternate histories


def _cache(name, build):
    os.makedirs(CACHE_DIR, exist_ok=True)
    p = os.path.join(CACHE_DIR, name)
    if os.path.exists(p):
        with open(p, "rb") as fh:
            return pickle.load(fh)
    v = build()
    with open(p, "wb") as fh:
        pickle.dump(v, fh)
    return v


def build_eligible_pool(days):
    """Symbols passing the bot's OWN liquidity gate, judged per-day on prior bars.

    Random must draw from the same pool the screener draws from, or the
    comparison is rigged. Uses only bars strictly before each day, so the filter
    cannot see the future.
    """
    def build():
        import warnings
        warnings.filterwarnings("ignore")
        import yfinance as yf
        from screener import get_universe

        universe = get_universe()
        print(f"  universe: {len(universe)} tickers; downloading daily bars...", file=sys.stderr)
        start = min(days) - datetime.timedelta(days=60)
        end = max(days) + datetime.timedelta(days=1)
        raw = yf.download(universe, start=str(start), end=str(end),
                          progress=False, auto_adjust=False, threads=True, group_by="column")

        # Vectorised: a per-ticker-per-day Python loop is ~26k pandas slices and
        # takes longer than the whole rest of the experiment.
        close = raw["Close"]
        dollar_vol = close * raw["Volume"]
        adv20 = dollar_vol.rolling(20, min_periods=20).mean()
        # shift(1): every value is the last COMPLETED bar's, so a row dated `day`
        # only ever carries information from before `day`. No lookahead.
        close_prior = close.shift(1)
        adv_prior = adv20.shift(1)

        bar_dates = list(close_prior.index.date)
        eligible = {}
        for day in days:
            idxs = [j for j, d in enumerate(bar_dates) if d <= day]
            if not idxs:
                eligible[day] = []
                continue
            i = idxs[-1]
            px_row = close_prior.iloc[i]
            adv_row = adv_prior.iloc[i]
            ok = (px_row >= MIN_SHARE_PRICE) & (adv_row >= MIN_AVG_DOLLAR_VOL)
            names = [s for s in ok.index[ok.fillna(False)] if s in universe]
            eligible[day] = names
            print(f"    {day}: {len(names)} eligible", file=sys.stderr)
        return eligible
    return _cache(f"eligible_{min(days)}_{max(days)}.pkl", build)


def fetch_minute_bars(symbols, days):
    """Minute bars for the random pool, keyed (symbol, day)."""
    def build():
        dc = StockHistoricalDataClient(os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY"))
        out = {}
        for day in sorted(days):
            start = ET.localize(datetime.datetime.combine(day, datetime.time(9, 30)))
            end = ET.localize(datetime.datetime.combine(day, datetime.time(16, 0)))
            try:
                df = dc.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=sorted(symbols), timeframe=TimeFrame.Minute,
                    start=start, end=end)).df
            except Exception as e:
                print(f"    bars failed {day}: {e}", file=sys.stderr)
                continue
            if df is None or len(df) == 0:
                continue
            got = 0
            for sym in sorted(symbols):
                try:
                    sub = df.loc[sym]
                except KeyError:
                    continue
                out[(sym, day)] = sorted(
                    (ts.tz_convert(ET), float(r.open), float(r.high), float(r.low), float(r.close))
                    for ts, r in sub.iterrows())
                got += 1
            print(f"    {day}: {got} symbols", file=sys.stderr)
        return out
    return _cache(f"pool_bars_{len(symbols)}_{min(days)}_{max(days)}.pkl", build)


def price_at(bars, when):
    """Fill price at `when`: the open of that minute's bar. No peeking ahead."""
    for ts, o, h, l, c in bars:
        if ts >= when:
            return o, ts
    return None, None


def simulate_entry(sym, day, when, bars, pol):
    """Enter `sym` at `when`, sized to POSITION_NOTIONAL, exit via the real rules."""
    px, ts = price_at(bars, when)
    if px is None or px <= 0:
        return None
    qty = max(1, int(POSITION_NOTIONAL / px))
    trade = {"day": day, "sym": sym, "qty": qty, "entry": px, "entry_at": ts}
    pl, reason = bt.simulate(trade, bars, pol)
    if pl is None:
        return None
    return pl


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--draws", type=int, default=N_DRAWS)
    ap.add_argument("--days", type=int, default=70)
    a = ap.parse_args()

    print("loading the bot's real trades...", file=sys.stderr)
    real = bt.load_trades(a.days)
    days = sorted({t["day"] for t in real})
    print(f"  {len(real)} trades over {len(days)} bracket-era days\n", file=sys.stderr)

    print("building the eligible universe (cached after first run)...", file=sys.stderr)
    eligible = build_eligible_pool(days)

    # One fixed pool, drawn from names eligible on most days, so every alternate
    # history draws from the same well-defined "liquid stock" population.
    from collections import Counter
    counts = Counter(s for names in eligible.values() for s in names)
    common = [s for s, n in counts.most_common() if n >= len(days) * 0.8]
    rng = random.Random(20260716)
    pool = sorted(rng.sample(common, min(POOL_SIZE, len(common))))
    print(f"  pool: {len(pool)} names eligible on >=80% of days\n", file=sys.stderr)

    print("fetching minute bars for the pool (cached; slow first time)...", file=sys.stderr)
    pool_bars = fetch_minute_bars(pool, days)
    print(f"  {len(pool_bars)} (symbol, day) series\n", file=sys.stderr)

    print("fetching minute bars for the screener's own picks...", file=sys.stderr)
    real_bars = bt.load_bars(real)

    pol = bt.ExitPolicy("current config")

    # ---- ARM 1: the screener, re-simulated under the SAME rules as random ----
    screener_pls, usable = [], []
    for t in real:
        b = real_bars.get((t["sym"], t["day"]))
        if not b:
            continue
        pl = simulate_entry(t["sym"], t["day"], t["entry_at"], b, pol)
        if pl is None:
            continue
        screener_pls.append(pl)
        usable.append(t)
    print(f"\n  screener arm: {len(usable)} trades simulated", file=sys.stderr)

    # ---- ARM 2: N alternate histories, random symbol, everything else identical ----
    print(f"  running {a.draws} random alternate histories...", file=sys.stderr)
    draw_totals, draw_means = [], []
    rng2 = random.Random(987654321)
    for i in range(a.draws):
        pls = []
        for t in usable:
            day = t["day"]
            choices = [s for s in eligible.get(day, []) if (s, day) in pool_bars]
            if not choices:
                continue
            for _ in range(6):     # a few tries if a name has no bar at that minute
                sym = rng2.choice(choices)
                pl = simulate_entry(sym, day, t["entry_at"], pool_bars[(sym, day)], pol)
                if pl is not None:
                    pls.append(pl)
                    break
        if pls:
            draw_totals.append(sum(pls))
            draw_means.append(st.mean(pls))

    # ---- ARM 3: SPY, same notional, same minutes, bracket vs buy-and-hold ----
    spy_bars = fetch_minute_bars(["SPY"], days)
    spy_bracket, spy_hold = [], []
    for t in usable:
        b = spy_bars.get(("SPY", t["day"]))
        if not b:
            continue
        pl = simulate_entry("SPY", t["day"], t["entry_at"], b, pol)
        if pl is not None:
            spy_bracket.append(pl)
        px, ts = price_at(b, t["entry_at"])
        if px:
            qty = max(1, int(POSITION_NOTIONAL / px))
            eod = [x for x in b if x[0] >= ET.localize(
                datetime.datetime.combine(t["day"], datetime.time(15, 45)))]
            close = eod[0][1] if eod else b[-1][4]
            spy_hold.append((close - px) * qty)

    # ------------------------------------------------------------------ report
    n = len(screener_pls)
    s_total, s_mean = sum(screener_pls), st.mean(screener_pls)
    r_mean = st.mean(draw_means)
    better = sum(1 for d in draw_totals if d < s_total)      # randoms the screener beat
    pct = better / len(draw_totals) * 100

    print("\n" + "=" * 78)
    print("DOES THE SCREENER BEAT CHANCE?")
    print("=" * 78)
    print(f"  Identical in both arms: {len(days)} days, {n} trades, same entry minutes,")
    print(f"  same ${POSITION_NOTIONAL:.0f} notional, same bracket. ONLY the symbol differs.\n")
    print(f"  SCREENER   total ${s_total:+9,.2f}   ${s_mean:+6.2f}/trade")
    print(f"  RANDOM     mean  ${st.mean(draw_totals):+9,.2f}   ${r_mean:+6.2f}/trade   "
          f"({len(draw_totals)} alternate histories)")
    print(f"             range ${min(draw_totals):+9,.2f} .. ${max(draw_totals):+9,.2f}")
    print(f"             5th-95th pct  ${sorted(draw_totals)[int(len(draw_totals)*.05)]:+,.2f}"
          f" .. ${sorted(draw_totals)[int(len(draw_totals)*.95)]:+,.2f}")
    print()
    print(f"  >>> The screener beat {better}/{len(draw_totals)} random portfolios "
          f"= {pct:.1f}th percentile")
    if pct < 25:
        verdict = ("WORSE THAN RANDOM. The signal is not merely absent -- it is\n"
                   "      actively selecting losers. Picking names with a dartboard\n"
                   "      would have lost less money.")
    elif pct < 75:
        verdict = ("INDISTINGUISHABLE FROM RANDOM. The ranking carries no\n"
                   "      information. Nothing downstream of it can be tuned into an edge.")
    else:
        verdict = ("The screener beats chance -- there IS signal here, even though\n"
                   "      the strategy still loses money overall.")
    print(f"  >>> {verdict}")
    print()
    print(f"  Baselines over the same window:")
    print(f"    do nothing                 $     0.00   <- beats every arm below")
    if spy_hold:
        print(f"    SPY, same $ , hold to 15:45 ${sum(spy_hold):+9,.2f}   ${st.mean(spy_hold):+6.2f}/trade")
    if spy_bracket:
        print(f"    SPY with this bracket      ${sum(spy_bracket):+9,.2f}   ${st.mean(spy_bracket):+6.2f}/trade")
    print(f"    screener (actual realised) ${sum(t['actual_pl'] for t in usable):+9,.2f}   "
          f"(context only -- not the comparison; see module docstring)")
    print()
    print("  Reminder: backtest.py's exit engine is biased ~17% KIND, so both arms")
    print("  here are optimistic. Real results are worse than everything above.")


if __name__ == "__main__":
    main()
