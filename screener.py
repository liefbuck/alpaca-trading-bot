"""
Dynamic universe builder.
Pulls the S&P 500 and Nasdaq 100 from Wikipedia and batch-downloads recent
price/volume data so both bots can score the full market instead of a fixed list.
"""
import pandas as pd
import yfinance as yf
import logging

log = logging.getLogger(__name__)


# Add any tickers you want tracked beyond the S&P 500 here.
WATCHLIST: list[str] = [
    "XE",
    "TE",
]


def get_sp500() -> list:
    """Pull current S&P 500 tickers from Wikipedia."""
    import requests
    from io import StringIO
    try:
        resp = requests.get(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            headers={"User-Agent": "Mozilla/5.0 (compatible; trading-bot/1.0)"},
            timeout=15,
        )
        resp.raise_for_status()
        table = pd.read_html(StringIO(resp.text), attrs={"id": "constituents"})
        tickers = table[0]["Symbol"].tolist()
        # Wikipedia uses dots (BRK.B), yfinance needs dashes (BRK-B)
        return [t.replace(".", "-") for t in tickers]
    except Exception as e:
        log.warning(f"Could not fetch S&P 500 list: {e}. Falling back to built-in list.")
        return FALLBACK_SP500


def get_nasdaq100() -> list:
    """Pull current Nasdaq 100 tickers from Wikipedia."""
    import requests
    from io import StringIO
    try:
        resp = requests.get(
            "https://en.wikipedia.org/wiki/Nasdaq-100",
            headers={"User-Agent": "Mozilla/5.0 (compatible; trading-bot/1.0)"},
            timeout=15,
        )
        resp.raise_for_status()
        tables = pd.read_html(StringIO(resp.text))
        # Find the table that has a 'Ticker' or 'Symbol' column
        for table in tables:
            cols = [c.strip() for c in table.columns.astype(str)]
            for col in cols:
                if col.lower() in ("ticker", "symbol"):
                    tickers = table[col].astype(str).tolist()
                    tickers = [t.replace(".", "-") for t in tickers if t and t != "nan"]
                    if len(tickers) > 50:
                        return tickers
        raise ValueError("Nasdaq-100 table not found on Wikipedia page")
    except Exception as e:
        log.warning(f"Could not fetch Nasdaq 100 list: {e}. Falling back to built-in list.")
        return FALLBACK_NDX100


def get_universe() -> list:
    """S&P 500 + Nasdaq 100 + WATCHLIST, deduplicated."""
    sp500   = get_sp500()
    ndx100  = get_nasdaq100()
    seen    = set(sp500)
    ndx_new = [t for t in ndx100 if t not in seen]
    if ndx_new:
        log.info(f"Nasdaq 100 adding {len(ndx_new)} ticker(s) not already in S&P 500")
    combined = sp500 + ndx_new
    seen.update(ndx_new)
    extras = [t for t in WATCHLIST if t not in seen]
    if extras:
        log.info(f"Watchlist adding {len(extras)} ticker(s): {extras}")
    return combined + extras


def _projected_rel_vol(vol: pd.Series) -> float:
    """
    Return today's relative volume, projected to end-of-day.

    When the market is open, yfinance returns today's incomplete daily bar.
    A raw comparison of 13-minutes of volume vs a full-day average will always
    look tiny and kill every candidate.  Instead we annualise today's partial
    volume by the fraction of the trading day that has elapsed so the number
    is comparable to historical full-day averages.

    Outside market hours the last bar is already complete, so we return the
    raw ratio unchanged.
    """
    import datetime as dt
    import pytz

    ET = pytz.timezone("America/New_York")
    now_et = dt.datetime.now(ET)
    today  = now_et.date()

    # Check whether the last bar in the series is today's (incomplete) bar
    last_idx = vol.index[-1]
    last_date = last_idx.date() if hasattr(last_idx, "date") else last_idx

    vol_raw = float(vol.iloc[-1])
    # Exclude today's incomplete bar from the historical average
    hist_vol = vol.iloc[:-1] if last_date >= today else vol
    vol_avg  = float(hist_vol.mean()) if len(hist_vol) > 0 else float(vol.mean())
    if vol_avg == 0:
        return 0.0

    if last_date >= today:
        # Scale partial volume up to a full-day estimate
        TRADING_MINUTES = 390  # 09:30–16:00
        market_open_et  = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        minutes_elapsed = max(1, (now_et - market_open_et).total_seconds() / 60)
        minutes_elapsed = min(minutes_elapsed, TRADING_MINUTES)
        projected_vol   = vol_raw * (TRADING_MINUTES / minutes_elapsed)
        # Cap at 25x — prevents absurdly inflated values in the first few minutes
        return min(projected_vol / vol_avg, 25.0)
    else:
        return vol_raw / vol_avg


def get_top_momentum(n: int = 50) -> list:
    """
    Score each stock in the S&P 500 + Nasdaq 100 + watchlist by:
      gap%  x  today's relative volume  x  30-day volume trend
    The 30-day volume trend rewards stocks whose volume has been
    consistently rising — early 10 days vs recent 10 days comparison.
    Batch-downloads 30 days so all three signals are available at once.

    rel_vol is projected to end-of-day when called during market hours so
    that an early-morning scan isn't killed by having only a few minutes of
    volume in the current bar.
    """
    import datetime as _dt
    _today = _dt.date.today()
    universe = get_universe()
    log.info(f"Downloading {len(universe)} tickers for momentum scan (30d)...")

    try:
        raw = yf.download(
            universe,
            period="30d",
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            threads=True,
            progress=False,
        )
    except Exception as e:
        log.error(f"Batch download failed: {e}")
        return []

    candidates = []
    for symbol in universe:
        try:
            if symbol not in raw.columns.get_level_values(0):
                continue
            df = raw[symbol].dropna()
            if len(df) < 5:
                continue

            prev_close = float(df["Close"].iloc[-2])
            current    = float(df["Close"].iloc[-1])
            if prev_close == 0:
                continue

            vol = df["Volume"]
            if float(vol.mean()) == 0:
                continue

            # 30-day volume trend: compare first-half avg vs second-half avg.
            # Exclude today's incomplete bar so a partial day doesn't
            # drag down the recent average and suppress valid candidates.
            _last_date = vol.index[-1]
            _last_date = _last_date.date() if hasattr(_last_date, "date") else _last_date
            vol_hist = vol.iloc[:-1] if _last_date >= _today else vol
            mid = len(vol_hist) // 2
            vol_early  = float(vol_hist.iloc[:mid].mean()) if mid > 0 else 1.0
            vol_recent = float(vol_hist.iloc[mid:].mean()) if mid > 0 else 1.0
            vol_trend  = (vol_recent / vol_early) if vol_early > 0 else 1.0

            change_pct = (current - prev_close) / prev_close * 100
            rel_vol    = _projected_rel_vol(vol)

            # Score = gap% x today's relative vol x 30-day vol trend
            score = change_pct * (rel_vol ** 1.25) * vol_trend

            candidates.append({
                "symbol":     symbol,
                "price":      round(current, 2),
                "change_pct": round(change_pct, 2),
                "rel_vol":    round(rel_vol, 2),
                "vol_trend":  round(vol_trend, 2),
                "score":      round(score, 3),
            })
        except Exception:
            continue

    candidates.sort(key=lambda x: x["score"], reverse=True)
    top = [c for c in candidates if c["change_pct"] > 0.5 and c["vol_trend"] > 1.0 and c["rel_vol"] > 1.0]
    log.info(
        f"Momentum scan complete — {len(top)} stocks up >0.5% with rising 30d volume "
        f"out of {len(candidates)} scanned"
    )
    return top[:n]


def get_top_mean_reversion(n: int = 50) -> list:
    """
    Return up to n tickers with RSI < 30 and price below 20-day SMA.
    Needs more history, so uses a 60-day download.
    """
    universe = get_universe()
    log.info(f"Downloading {len(universe)} tickers for mean reversion scan...")

    try:
        raw = yf.download(
            universe,
            period="60d",
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            threads=True,
            progress=False,
        )
    except Exception as e:
        log.error(f"Batch download failed: {e}")
        return []

    candidates = []
    for symbol in universe:
        try:
            if symbol not in raw.columns.get_level_values(0):
                continue
            closes = raw[symbol]["Close"].dropna()
            # Need at least 20 rows for SMA-20 and 15 for RSI-14 to be valid
            if len(closes) < 20:
                continue
            current   = float(closes.iloc[-1])
            sma       = float(closes.rolling(20).mean().iloc[-1])
            pct_below = (sma - current) / sma * 100

            delta    = closes.diff()
            gain     = delta.clip(lower=0).rolling(14).mean()
            loss     = (-delta.clip(upper=0)).rolling(14).mean()
            rs       = gain / loss
            rsi      = float((100 - (100 / (1 + rs))).iloc[-1])

            if rsi < 30 and pct_below >= 1.0:
                candidates.append({
                    "symbol":    symbol,
                    "price":     round(current, 2),
                    "rsi":       round(rsi, 1),
                    "sma":       round(sma, 2),
                    "pct_below": round(pct_below, 2),
                    "score":     round(30 - rsi, 2),
                })
        except Exception:
            continue

    candidates.sort(key=lambda x: x["score"], reverse=True)
    log.info(f"Mean reversion scan complete — {len(candidates)} oversold stocks found")
    return candidates[:n]


# Built-in fallbacks if Wikipedia is unreachable
FALLBACK_SP500 = [
    "AAPL","MSFT","NVDA","TSLA","AMZN","META","GOOGL","AMD","NFLX","COIN",
    "PLTR","SOFI","MARA","RIOT","ROKU","SNAP","UBER","LYFT","SHOP","HOOD",
    "JPM","BAC","WFC","GS","MS","C","BLK","ORCL","INTC","QCOM","TXN","MU",
    "WMT","TGT","COST","HD","LOW","NKE","MCD","XOM","CVX","JNJ","PFE","ABBV",
    "SMCI","ARM","IONQ","RKLB","MSTR","RDDT",
]

FALLBACK_NDX100 = [
    "ADBE","ADSK","AEP","AMAT","AMGN","ANSS","ASML","AVGO","AZN","BIIB",
    "BKNG","CDNS","CEG","CHTR","CMCSA","CPRT","CRWD","CSCO","CSGP","CSX",
    "CTAS","CTSH","DDOG","DLTR","DXCM","EA","EXC","FANG","FAST","FTNT",
    "GILD","IDXX","ILMN","ISRG","KDP","KHC","KLAC","LCID","LRCX","MAR",
    "MCHP","MDLZ","MELI","MNST","MRNA","MRVL","MSFT","MU","NXPI","ODFL",
    "ORLY","PANW","PAYX","PCAR","PDD","PEP","PYPL","REGN","ROP","ROST",
    "SBUX","SGEN","SIRI","SNPS","SPLK","SWKS","TEAM","TMUS","TTWO","TXN",
    "VRSK","VRSN","VRTX","WBA","WDAY","XEL","ZM","ZS",
]
