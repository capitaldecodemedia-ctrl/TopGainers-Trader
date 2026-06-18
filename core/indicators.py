"""
All technical indicators computed from OHLCV DataFrames.
No external TA library dependency for core signals — pure pandas/numpy.
"""

import numpy as np
import pandas as pd
from datetime import datetime


# ── VWAP ──────────────────────────────────────────────────────────────────

def vwap(df: pd.DataFrame) -> pd.Series:
    """Intraday VWAP — resets each day."""
    df = df.copy()
    df["tp"] = (df["high"] + df["low"] + df["close"]) / 3
    df["date"] = df.index.date
    df["cum_tpv"] = df.groupby("date").apply(
        lambda g: (g["tp"] * g["volume"]).cumsum()
    ).reset_index(level=0, drop=True)
    df["cum_vol"] = df.groupby("date")["volume"].cumsum()
    return df["cum_tpv"] / df["cum_vol"]


# ── EMA ───────────────────────────────────────────────────────────────────

def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


# ── ATR ───────────────────────────────────────────────────────────────────

def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high_low   = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close  = (df["low"]  - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


# ── Opening Range Breakout ─────────────────────────────────────────────────

def opening_range(df: pd.DataFrame, minutes: int = 30) -> dict:
    """
    Returns {"orb_high": float, "orb_low": float}.
    Uses first <minutes> minutes of today's session.
    """
    today = datetime.now().date()
    today_df = df[df.index.date == today]
    if today_df.empty:
        return {"orb_high": None, "orb_low": None}

    open_time = today_df.index[0]
    orb_df    = today_df[today_df.index <= open_time + pd.Timedelta(minutes=minutes)]

    return {
        "orb_high": orb_df["high"].max(),
        "orb_low" : orb_df["low"].min(),
    }


# ── Higher High / Higher Low structure ────────────────────────────────────

def is_hh_hl(df: pd.DataFrame, lookback: int = 3) -> bool:
    """True if last <lookback> candles form HH + HL pattern."""
    if len(df) < lookback + 1:
        return False
    recent = df.tail(lookback + 1)
    highs  = recent["high"].values
    lows   = recent["low"].values
    hh = all(highs[i] > highs[i - 1] for i in range(1, len(highs)))
    hl = all(lows[i]  > lows[i - 1]  for i in range(1, len(lows)))
    return hh and hl


# ── Supertrend ────────────────────────────────────────────────────────────

def supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> pd.Series:
    """Returns supertrend line. Values < close = uptrend."""
    df    = df.copy()
    df["atr"] = atr(df, period)
    hl2   = (df["high"] + df["low"]) / 2
    df["upper"] = hl2 + multiplier * df["atr"]
    df["lower"] = hl2 - multiplier * df["atr"]

    st  = [0.0] * len(df)
    dir = [1]   * len(df)   # 1 = up, -1 = down

    for i in range(1, len(df)):
        prev_st = st[i - 1]
        curr_lo = df["lower"].iloc[i]
        curr_up = df["upper"].iloc[i]
        close   = df["close"].iloc[i]
        prev_cl = df["close"].iloc[i - 1]

        if prev_st == df["upper"].iloc[i - 1]:
            st[i]  = curr_lo if close > curr_up else curr_up
            dir[i] = 1 if close > curr_up else -1
        else:
            st[i]  = curr_up if close < curr_lo else curr_lo
            dir[i] = -1 if close < curr_lo else 1

    df["st"]  = st
    df["dir"] = dir
    return df[["st", "dir"]]


# ── Swing low finder ──────────────────────────────────────────────────────

def swing_low(df: pd.DataFrame, lookback: int = 10) -> float:
    """Most recent significant swing low within last <lookback> candles."""
    recent = df.tail(lookback)
    return recent["low"].min()


# ── Momentum ──────────────────────────────────────────────────────────────

def momentum_score(df: pd.DataFrame, periods: int = 5) -> float:
    """
    Rate of price rise over last <periods> candles.
    Returns normalised 0-100 score.
    """
    if len(df) < periods + 1:
        return 0.0
    price_chg = (df["close"].iloc[-1] - df["close"].iloc[-periods]) / df["close"].iloc[-periods] * 100
    return min(max(price_chg * 10, 0), 100)


# ── Resistance check ──────────────────────────────────────────────────────

def has_nearby_resistance(df: pd.DataFrame, current_price: float, pct_threshold: float = 2.0) -> bool:
    """
    True if there's a significant resistance level within <pct_threshold>%
    above current price (based on recent highs).
    """
    lookback  = df.tail(50)
    threshold = current_price * (1 + pct_threshold / 100)
    recent_highs = lookback["high"]
    # cluster of highs within zone counts as resistance
    resistance_hits = ((recent_highs >= current_price) & (recent_highs <= threshold)).sum()
    return resistance_hits >= 3


# ── Spread check ─────────────────────────────────────────────────────────

def spread_pct(bid: float, ask: float) -> float:
    if bid <= 0:
        return 999.0
    return (ask - bid) / bid * 100
