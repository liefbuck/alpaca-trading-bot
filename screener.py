"""
Dynamic universe builder.
Pulls the S&P 500 from Wikipedia and batch-downloads recent price/volume
data so both bots can score the full market instead of a fixed list.
"""
import pandas as pd
import yfinance as yf
import logging

log = logging.getLogger(__name__)


def get_sp500() -> list:
    """Pull current S&P 500 tickers from Wikipedia."""
    try:
        table = pd.read_html(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            attrs={"id": "constituents"},
        )
        tickers = table[0]["Symbol"].tolist()
        # Wikipedia uses dots (BRK.B), yfinance needs dashes (BRK-B)
        return [t.replace(".", "-") for t in tickers]
    except Exception as e:
        log.warning(f"Could not fetch S&P 500 list: {e}. Falling back to built-in list.")
        return FALLBACK


def get_top_momentum(n: int = 50) -> list:
    """
    Score each S&P 500 stock by:
      gap%  x  today's relative volume  x  30-day volume trend
    The 30-day volume trend rewards stocks whose volume has been
    consistently rising — early 10 days vs recent 10 days comparison.
    Batch-downloads 30 days so all three signals are available at once.
    """
    universe = get_sp500()
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
            if len(df) < 20:
                continue

            prev_close = float(df["Close"].iloc[-2])
            current    = float(df["Close"].iloc[-1])
            if prev_close == 0:
                continue

            vol = df["Volume"]
            vol_today = float(vol.iloc[-1])
            vol_avg   = float(vol.mean())
            if vol_avg == 0:
                continue

            # 30-day volume trend: compare first-half avg vs second-half avg
            # A ratio > 1 means volume is growing over the month
            mid = len(vol) // 2
            vol_early  = float(vol.iloc[:mid].mean())
            vol_recent = float(vol.iloc[mid:].mean())
            vol_trend  = (vol_recent / vol_early) if vol_early > 0 else 1.0

            change_pct = (current - prev_close) / prev_close * 100
            rel_vol    = vol_today / vol_avg

            # Score = gap% x today's relative vol x 30-day vol trend
            score = change_pct * rel_vol * vol_trend

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
    top = [c for c in candidates if c["change_pct"] > 0.5 and c["vol_trend"] > 1.0]
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
    universe = get_sp500()
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
            if len(closes) < 35:
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


# Built-in fallback if Wikipedia is unreachable
FALLBACK = [
    "AAPL","MSFT","NVDA","TSLA","AMZN","META","GOOGL","AMD","NFLX","COIN",
    "PLTR","SOFI","MARA","RIOT","ROKU","SNAP","UBER","LYFT","SHOP","HOOD",
    "JPM","BAC","WFC","GS","MS","C","BLK","ORCL","INTC","QCOM","TXN","MU",
    "WMT","TGT","COST","HD","LOW","NKE","MCD","XOM","CVX","JNJ","PFE","ABBV",
    "SMCI","ARM","IONQ","RKLB","MSTR","RDDT",
]
