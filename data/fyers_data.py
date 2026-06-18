"""
Fyers data layer.
Handles: auth token, OHLCV candles, live quotes, top gainers scan.
All symbols in NSE:SYMBOL-EQ format.
"""

import logging
import os
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import requests
from fyers_apiv3 import fyersModel

from config.settings import (
    FYERS_APP_ID, CANDLE_INTERVAL,
    MIN_AVG_DAILY_VALUE_CR, MIN_REL_VOLUME
)

log = logging.getLogger(__name__)

TOKEN_FILE = Path(__file__).parent.parent / "fyers_token.txt"

# ── Fyers client (singleton, but re-checks token freshness) ────────────────
_fyers: Optional[fyersModel.FyersModel] = None
_loaded_token: Optional[str] = None


def _get_current_token() -> str:
    """
    Token priority: fyers_token.txt (written by web.py /fyers/callback)
    > FYERS_ACCESS_TOKEN env var (Railway var or .env, set manually).
    """
    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text().strip()
        if token:
            return token
    return os.environ.get("FYERS_ACCESS_TOKEN", "")


def get_client() -> fyersModel.FyersModel:
    """Returns a Fyers client. Re-initialises if the token has changed
    since last call (e.g. after a fresh /login flow)."""
    global _fyers, _loaded_token
    current_token = _get_current_token()

    if not current_token:
        raise RuntimeError(
            "No Fyers access token available. Visit /login on your Railway "
            "app to authenticate, or set FYERS_ACCESS_TOKEN env var."
        )

    if _fyers is None or current_token != _loaded_token:
        _fyers = fyersModel.FyersModel(
            client_id=FYERS_APP_ID,
            token=current_token,
            log_path=""
        )
        _loaded_token = current_token
        log.info("[DATA] Fyers client (re)initialised with current token")

    return _fyers


# ── Candle fetcher ─────────────────────────────────────────────────────────

def get_candles(symbol: str, interval: int = None, lookback_days: int = 5) -> pd.DataFrame:
    """
    Fetch OHLCV candles for symbol.
    Returns DataFrame with columns: datetime, open, high, low, close, volume.
    """
    interval = interval or CANDLE_INTERVAL
    fyers = get_client()

    end_dt   = datetime.now()
    start_dt = end_dt - timedelta(days=lookback_days)

    data = {
        "symbol"      : symbol,
        "resolution"  : str(interval),
        "date_format" : "1",
        "range_from"  : start_dt.strftime("%Y-%m-%d"),
        "range_to"    : end_dt.strftime("%Y-%m-%d"),
        "cont_flag"   : "1"
    }

    resp = fyers.history(data=data)
    if resp.get("s") != "ok":
        log.error(f"[DATA] Candle fetch failed for {symbol}: {resp}")
        return pd.DataFrame()

    candles = resp.get("candles", [])
    if not candles:
        return pd.DataFrame()

    df = pd.DataFrame(candles, columns=["ts", "open", "high", "low", "close", "volume"])
    df["datetime"] = pd.to_datetime(df["ts"], unit="s").dt.tz_localize("UTC").dt.tz_convert("Asia/Kolkata")
    df = df.drop(columns=["ts"]).set_index("datetime").sort_index()
    return df


# ── Live quote ─────────────────────────────────────────────────────────────

def get_quote(symbols: list[str]) -> dict:
    """
    Fetch live quote for list of symbols.
    Returns dict: {symbol: {ltp, bid, ask, volume, avg_price (VWAP approx)}}
    """
    fyers = get_client()
    data  = {"symbols": ",".join(symbols)}
    resp  = fyers.quotes(data=data)
    if resp.get("s") != "ok":
        log.error(f"[DATA] Quote fetch failed: {resp}")
        return {}

    result = {}
    for item in resp.get("d", []):
        v = item.get("v", {})
        sym = item.get("n", "")
        result[sym] = {
            "ltp"      : v.get("lp", 0),
            "bid"      : v.get("bid", 0),
            "ask"      : v.get("ask", 0),
            "volume"   : v.get("volume", 0),
            "open"     : v.get("open_price", 0),
            "high"     : v.get("high_price", 0),
            "low"      : v.get("low_price", 0),
            "prev_close": v.get("prev_close_price", 0),
            "avg_price": v.get("avg_price", 0),      # Fyers gives VWAP-ish avg
            "chg_pct"  : v.get("ch", 0),
        }
    return result


# ── Top gainers scanner ─────────────────────────────────────────────────────

# Liquid NSE stocks universe — expand as needed
# Format: NSE:SYMBOL-EQ
UNIVERSE = [
    "NSE:RELIANCE-EQ","NSE:TCS-EQ","NSE:INFY-EQ","NSE:HDFCBANK-EQ",
    "NSE:ICICIBANK-EQ","NSE:SBIN-EQ","NSE:AXISBANK-EQ","NSE:KOTAKBANK-EQ",
    "NSE:BAJFINANCE-EQ","NSE:BHARTIARTL-EQ","NSE:WIPRO-EQ","NSE:HCLTECH-EQ",
    "NSE:MARUTI-EQ","NSE:TATAMOTORS-EQ","NSE:TATASTEEL-EQ","NSE:ADANIENT-EQ",
    "NSE:SUNPHARMA-EQ","NSE:DRREDDY-EQ","NSE:CIPLA-EQ","NSE:DIVISLAB-EQ",
    "NSE:TITAN-EQ","NSE:ULTRACEMCO-EQ","NSE:GRASIM-EQ","NSE:HINDALCO-EQ",
    "NSE:JSWSTEEL-EQ","NSE:ONGC-EQ","NSE:POWERGRID-EQ","NSE:NTPC-EQ",
    "NSE:COALINDIA-EQ","NSE:BPCL-EQ","NSE:IOC-EQ","NSE:HEROMOTOCO-EQ",
    "NSE:BAJAJ-AUTO-EQ","NSE:EICHERMOT-EQ","NSE:M&M-EQ","NSE:TATACONSUM-EQ",
    "NSE:NESTLEIND-EQ","NSE:HINDUNILVR-EQ","NSE:BRITANNIA-EQ","NSE:ITC-EQ",
    "NSE:ASIANPAINT-EQ","NSE:PIDILITIND-EQ","NSE:BERGEPAINT-EQ",
    "NSE:INDUSINDBK-EQ","NSE:FEDERALBNK-EQ","NSE:PNB-EQ","NSE:BANKBARODA-EQ",
    "NSE:HAVELLS-EQ","NSE:VOLTAS-EQ","NSE:POLYCAB-EQ","NSE:ABCAPITAL-EQ",
    "NSE:MUTHOOTFIN-EQ","NSE:CHOLAFIN-EQ","NSE:BAJAJFINSV-EQ",
    "NSE:ZOMATO-EQ","NSE:NYKAA-EQ","NSE:PAYTM-EQ","NSE:DMART-EQ",
    "NSE:IRCTC-EQ","NSE:HDFCLIFE-EQ","NSE:SBILIFE-EQ","NSE:ICICIPRULI-EQ",
    "NSE:LUPIN-EQ","NSE:AUROPHARMA-EQ","NSE:TORNTPHARM-EQ","NSE:ALKEM-EQ",
    "NSE:LT-EQ","NSE:SIEMENS-EQ","NSE:ABB-EQ","NSE:BHEL-EQ",
    "NSE:SAIL-EQ","NSE:NMDC-EQ","NSE:VEDL-EQ","NSE:JINDALSTEL-EQ",
    "NSE:DLF-EQ","NSE:GODREJPROP-EQ","NSE:OBEROIRLTY-EQ","NSE:PRESTIGE-EQ",
    "NSE:APOLLOHOSP-EQ","NSE:FORTIS-EQ","NSE:MAXHEALTH-EQ",
    "NSE:NAUKRI-EQ","NSE:COFORGE-EQ","NSE:MPHASIS-EQ","NSE:LTTS-EQ",
    "NSE:PERSISTENT-EQ","NSE:TRENT-EQ","NSE:JUBLFOOD-EQ","NSE:DEVYANI-EQ",
]


def get_top_gainers(min_chg_pct: float = 1.5) -> list[dict]:
    """
    Fetch live quotes for entire universe, filter gainers,
    enrich with relative volume, return sorted by % change.
    Batches requests to stay within API rate limits.
    """
    gainers = []
    batch_size = 50

    for i in range(0, len(UNIVERSE), batch_size):
        batch = UNIVERSE[i:i + batch_size]
        quotes = get_quote(batch)
        time.sleep(0.1)   # rate limit buffer

        for sym, q in quotes.items():
            if q["chg_pct"] < min_chg_pct:
                continue
            if q["ltp"] <= 0 or q["volume"] <= 0:
                continue

            # Relative volume: today's vol vs 5-day avg (computed from candles)
            rel_vol = _compute_rel_vol(sym, q["volume"])

            if rel_vol < MIN_REL_VOLUME:
                continue

            # Liquidity filter: approx daily value in crores
            daily_value_cr = (q["ltp"] * q["volume"]) / 1e7
            if daily_value_cr < MIN_AVG_DAILY_VALUE_CR:
                continue

            gainers.append({
                "symbol"        : sym,
                "ltp"           : q["ltp"],
                "chg_pct"       : q["chg_pct"],
                "volume"        : q["volume"],
                "rel_vol"       : rel_vol,
                "daily_value_cr": daily_value_cr,
                "bid"           : q["bid"],
                "ask"           : q["ask"],
                "open"          : q["open"],
                "high"          : q["high"],
                "low"           : q["low"],
                "prev_close"    : q["prev_close"],
                "avg_price"     : q["avg_price"],   # VWAP approx
            })

    gainers.sort(key=lambda x: x["chg_pct"], reverse=True)
    return gainers


# ── Relative volume helper ─────────────────────────────────────────────────
_vol_cache: dict[str, float] = {}


def _compute_rel_vol(symbol: str, current_vol: int) -> float:
    """Compare current day's volume to 5-day average at same time of day."""
    if symbol not in _vol_cache:
        df = get_candles(symbol, interval=CANDLE_INTERVAL, lookback_days=6)
        if df.empty:
            return 1.0
        today = datetime.now().date()
        df_hist = df[df.index.date < today]
        if df_hist.empty:
            return 1.0
        avg_daily_vol = df_hist.groupby(df_hist.index.date)["volume"].sum().mean()
        _vol_cache[symbol] = avg_daily_vol if avg_daily_vol > 0 else 1.0

    avg_vol = _vol_cache[symbol]
    return round(current_vol / avg_vol, 2) if avg_vol > 0 else 1.0


def clear_vol_cache():
    """Call at session start each day."""
    _vol_cache.clear()
