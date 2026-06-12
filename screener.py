"""
Dynamic universe builder.
Pulls the S&P 500, S&P MidCap 400, S&P SmallCap 600 and Nasdaq 100 from Wikipedia
(the S&P Composite 1500 plus Nasdaq names outside it — ~1,500 liquid tickers) and
batch-downloads recent price/volume data so both bots can score the full market
instead of a fixed list.
"""
import time
import pandas as pd
import yfinance as yf
import logging

log = logging.getLogger(__name__)

# yfinance logs every ticker it can't fetch in a bulk download at ERROR level
# ("$X: possibly delisted; no price data found" plus a giant "N Failed downloads"
# dump) — hundreds of lines per scan when Yahoo's free endpoint throttles a
# ~1500-ticker request. These are NOT our errors: the scan already skips any
# symbol missing from the batch (see the `symbol not in raw.columns` guards), and
# a genuine whole-batch failure is still caught and logged by us via the
# try/except in get_top_momentum. Silence the library's own logger so the noise
# stops burying real errors in bot.log.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)


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


def get_sp400() -> list:
    """Pull current S&P MidCap 400 tickers from Wikipedia."""
    import requests
    from io import StringIO
    try:
        resp = requests.get(
            "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
            headers={"User-Agent": "Mozilla/5.0 (compatible; trading-bot/1.0)"},
            timeout=15,
        )
        resp.raise_for_status()
        table = pd.read_html(StringIO(resp.text), attrs={"id": "constituents"})
        tickers = table[0]["Symbol"].tolist()
        return [t.replace(".", "-") for t in tickers]
    except Exception as e:
        log.warning(f"Could not fetch S&P 400 list: {e}. Falling back to built-in list.")
        return FALLBACK_SP400


def get_sp600() -> list:
    """Pull current S&P SmallCap 600 tickers from Wikipedia."""
    import requests
    from io import StringIO
    try:
        resp = requests.get(
            "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
            headers={"User-Agent": "Mozilla/5.0 (compatible; trading-bot/1.0)"},
            timeout=15,
        )
        resp.raise_for_status()
        table = pd.read_html(StringIO(resp.text), attrs={"id": "constituents"})
        tickers = table[0]["Symbol"].tolist()
        return [t.replace(".", "-") for t in tickers]
    except Exception as e:
        log.warning(f"Could not fetch S&P 600 list: {e}. Falling back to built-in list.")
        return FALLBACK_SP600


def get_universe() -> list:
    """S&P 500 + MidCap 400 + SmallCap 600 + Nasdaq 100 + WATCHLIST, deduplicated.

    Sources are merged in order, each contributing only the names not already
    seen, so the S&P Composite 1500 (500+400+600) plus the few Nasdaq-100 names
    outside it form a ~1,500-ticker liquid universe.
    """
    seen = set()
    combined = []
    for label, lst in (("S&P 500",   get_sp500()),
                       ("S&P 400",   get_sp400()),
                       ("S&P 600",   get_sp600()),
                       ("Nasdaq 100", get_nasdaq100())):
        new = [t for t in lst if t and t not in seen]
        if combined and new:   # don't log the first (base) source
            log.info(f"{label} adding {len(new)} ticker(s) new to the universe")
        combined += new
        seen.update(new)
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


_DOWNLOAD_CHUNK = 100  # tickers per Yahoo request; one ~1500-name call gets throttled


def _chunks(seq: list, size: int) -> list:
    """Split `seq` into `size`-length chunks, folding a trailing single element into
    the previous chunk so no chunk has length 1 (yfinance returns a differently
    shaped frame for a single ticker — we normalise it below, but avoid it where we
    can)."""
    out = [seq[i:i + size] for i in range(0, len(seq), size)]
    if len(out) >= 2 and len(out[-1]) == 1:
        tail = out.pop()            # pop first: doing it inline shifts the -2 index
        out[-1] = out[-1] + tail
    return out


def _download_history(tickers: list, period: str, chunk_size: int = _DOWNLOAD_CHUNK,
                      max_passes: int = 2):
    """Batch-download daily OHLCV for `tickers` over `period`, in chunks.

    Yahoo's free endpoint throttles a single ~1500-ticker request and silently drops
    ~1/3 of it (AMZN/TSLA/JPM included — not real delistings). Downloading in
    `chunk_size` batches dodges that, and a second pass retries whatever didn't come
    back. Returns a (ticker, field) MultiIndex frame (so callers can do raw[symbol]),
    or None if nothing returned at all.
    """
    deduped = list(dict.fromkeys(tickers))   # preserve order, drop dupes
    frames = []
    have: set = set()
    for pass_num in range(max_passes):
        todo = [t for t in deduped if t not in have]
        if not todo:
            break
        if pass_num > 0:
            log.info(f"Retrying {len(todo)} ticker(s) Yahoo dropped on the first pass...")
            time.sleep(1)   # brief breather before retrying the stragglers
        for chunk in _chunks(todo, chunk_size):
            try:
                df = yf.download(
                    chunk, period=period, interval="1d", group_by="ticker",
                    auto_adjust=True, threads=True, progress=False,
                )
            except Exception as e:
                log.warning(f"chunk download failed ({len(chunk)} tickers): {e}")
                continue
            if df is None or df.empty:
                continue
            # yfinance returns a FLAT frame for a single-ticker chunk; normalise to the
            # (ticker, field) MultiIndex the callers expect.
            if not isinstance(df.columns, pd.MultiIndex):
                df.columns = pd.MultiIndex.from_product([[chunk[0]], df.columns])
            frames.append(df)
            have |= set(df.columns.get_level_values(0)) & set(chunk)
    if not frames:
        return None
    combined = pd.concat(frames, axis=1)
    # a straggler can arrive in BOTH passes; keep the first occurrence of each column.
    return combined.loc[:, ~combined.columns.duplicated()]


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

    raw = _download_history(universe, period="30d")
    if raw is None:
        log.error("Momentum scan: no price data returned from any chunk.")
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

    raw = _download_history(universe, period="60d")
    if raw is None:
        log.error("Mean reversion scan: no price data returned from any chunk.")
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

# Representative liquid names only (the live Wikipedia fetch is the primary
# source). Any stale ticker here is harmlessly skipped by the scan.
FALLBACK_SP400 = [
    "AA","AAL","ACM","AGCO","ALSN","BWA","CACI","CASY","CHRW","CHDN",
    "CIEN","CLF","COHR","DKS","DOCU","EME","EWBC","EXEL","FLR","GGG",
    "GME","GNTX","HAS","JBLU","JEF","JWN","KBR","LAD","LECO","LSCC",
    "MANH","MAT","MUR","NVT","OLLI","OC","PB","PFGC","PNFP","PSTG",
    "RGA","RGLD","RPM","SAIA","SF","SNX","THC","THO","TOL","TPX",
    "TREX","TXRH","UGI","USFD","VNO","WEN","WSM","WWD","X","ZION",
]

FALLBACK_SP600 = [
    "AAP","ABCB","ABG","ABM","ACIW","AEO","AMN","ANF","ARCB","ASO",
    "AWR","AX","BANR","BCC","BGS","BKE","BOOT","CAL","CARG","CBRL",
    "CENX","CNK","CRK","CRS","CVCO","CWEN","DDS","DEA","DFIN","DIN",
    "DNOW","EAT","EPC","FUN","GEO","GFF","GVA","HELE","HIBB","HWKN",
    "JACK","JBSS","KSS","KTB","LCII","MATX","MGY","MTRN","NPK","OII",
    "PRDO","PZZA","ROG","SBH","SHAK","SHOO","SLVM","SXT","TGNA","TMP",
    "TPH","UE","VIAV","VSH","WGO","WOR","WWW",
]
