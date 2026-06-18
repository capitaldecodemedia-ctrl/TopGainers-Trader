"""
Stop Loss Engine — selects the tightest viable SL model for each trade.
Supported models: PREV_CANDLE, SWING, ATR, VWAP
"""

import logging
import pandas as pd
from typing import Optional

from core.indicators import atr, swing_low, vwap

log = logging.getLogger(__name__)

BUFFER_PCT = 0.002   # 0.2% buffer below SL level


def select_sl(df: pd.DataFrame, entry_price: float,
              min_qty: int = 1, max_risk_per_share: float = None) -> dict:
    """
    Try each SL model in order of tightness.
    Returns the best viable SL dict:
      {sl_price, model, risk_per_share}
    Returns None if no model gives a viable trade.
    """
    candidates = _all_sl_candidates(df, entry_price)
    # Sort by risk_per_share ascending (tightest first)
    candidates.sort(key=lambda x: x["risk_per_share"])

    for sl in candidates:
        if sl["risk_per_share"] <= 0:
            continue
        if max_risk_per_share and sl["risk_per_share"] > max_risk_per_share:
            continue
        log.debug(f"[SL] Model={sl['model']} SL={sl['sl_price']:.2f} "
                  f"risk/share={sl['risk_per_share']:.2f}")
        return sl

    # All models too wide — return widest viable one anyway
    viable = [s for s in candidates if s["risk_per_share"] > 0]
    if viable:
        viable.sort(key=lambda x: x["risk_per_share"])
        return viable[0]

    return None


def _all_sl_candidates(df: pd.DataFrame, entry: float) -> list[dict]:
    results = []

    # 1. Previous candle low
    try:
        prev_low = df["low"].iloc[-2]
        sl = prev_low * (1 - BUFFER_PCT)
        if sl < entry:
            results.append({"model": "PREV_CANDLE", "sl_price": round(sl, 2),
                            "risk_per_share": round(entry - sl, 2)})
    except Exception:
        pass

    # 2. Swing low (10 candles)
    try:
        sw_low = swing_low(df, lookback=10)
        sl = sw_low * (1 - BUFFER_PCT)
        if sl < entry:
            results.append({"model": "SWING", "sl_price": round(sl, 2),
                            "risk_per_share": round(entry - sl, 2)})
    except Exception:
        pass

    # 3. ATR-based (1.5x ATR)
    try:
        atr_val = atr(df, period=14).iloc[-1]
        sl = entry - (1.5 * atr_val)
        if sl < entry and sl > 0:
            results.append({"model": "ATR", "sl_price": round(sl, 2),
                            "risk_per_share": round(entry - sl, 2)})
    except Exception:
        pass

    # 4. VWAP-based
    try:
        vwap_val = vwap(df).iloc[-1]
        sl = vwap_val * (1 - BUFFER_PCT)
        if sl < entry and sl > 0:
            results.append({"model": "VWAP", "sl_price": round(sl, 2),
                            "risk_per_share": round(entry - sl, 2)})
    except Exception:
        pass

    return results


def calculate_target(entry: float, sl: float, rr: float = 2.0) -> float:
    """Target = entry + (risk * RR)."""
    risk = entry - sl
    return round(entry + risk * rr, 2)


def calculate_cost(entry: float, exit_price: float, qty: int) -> dict:
    """Full NSE intraday cost model."""
    turnover_buy  = entry * qty
    turnover_sell = exit_price * qty
    total_turnover= turnover_buy + turnover_sell

    brokerage   = 40.0                              # ₹20 each side (flat)
    stt         = turnover_sell * 0.00025           # 0.025% on sell side
    txn_charge  = total_turnover * 0.0000297        # NSE rate
    sebi        = total_turnover * 0.0000001        # ₹10/crore
    gst         = (brokerage + txn_charge + sebi) * 0.18
    stamp       = turnover_buy * 0.00003            # 0.003% on buy
    slippage    = total_turnover * 0.0005           # 0.05% estimate

    total = brokerage + stt + txn_charge + sebi + gst + stamp + slippage
    return {
        "brokerage"  : round(brokerage, 2),
        "stt"        : round(stt, 2),
        "txn_charge" : round(txn_charge, 2),
        "sebi"       : round(sebi, 4),
        "gst"        : round(gst, 2),
        "stamp"      : round(stamp, 2),
        "slippage"   : round(slippage, 2),
        "total"      : round(total, 2),
    }


def trailing_sl(df: pd.DataFrame, current_sl: float, entry: float,
                current_price: float, method: str = "PREV_CANDLE") -> float:
    """
    Update trailing SL. Returns new SL (never lower than current_sl).
    Methods: PREV_CANDLE, ATR, EMA, PCT
    """
    new_sl = current_sl

    if method == "PREV_CANDLE":
        candidate = df["low"].iloc[-2] * (1 - BUFFER_PCT)
        new_sl = max(candidate, current_sl)

    elif method == "ATR":
        atr_val = atr(df, 14).iloc[-1]
        candidate = current_price - atr_val
        new_sl = max(candidate, current_sl)

    elif method == "EMA":
        from core.indicators import ema
        ema9 = ema(df["close"], 9).iloc[-1]
        candidate = ema9 * (1 - BUFFER_PCT)
        new_sl = max(candidate, current_sl)

    elif method == "PCT":
        candidate = current_price * 0.985    # 1.5% trail
        new_sl = max(candidate, current_sl)

    # Breakeven rule: if profit >= 0.5R, move SL to entry
    risk = entry - current_sl
    if risk > 0 and (current_price - entry) >= 0.5 * risk:
        new_sl = max(new_sl, entry)

    return round(new_sl, 2)
