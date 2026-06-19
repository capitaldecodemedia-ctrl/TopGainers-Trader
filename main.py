"""
Main Trading Loop — runs continuously from SCAN_START to FORCE_EXIT_TIME.
Cycle: scan → score → risk-check → size → enter → manage → exit → repeat.
Scan interval: every 60 seconds (configurable).
"""

import logging
import time
from datetime import datetime, date
import pytz

from config.settings import (
    SCAN_START, SCAN_END, FORCE_EXIT_TIME, PAPER_TRADE,
    CANDLE_INTERVAL, MIN_SCORE
)
from core.database import init_db
from core.notifier import notify, alert_no_trade
from data.fyers_data import get_top_gainers, get_candles, get_quote, clear_vol_cache
from core.scorer import select_best_candidate
from core.indicators import vwap, ema, atr
from risk.risk_engine import RiskEngine
from execution.sl_engine import select_sl, calculate_target, trailing_sl
from execution.order_manager import OrderManager
from analytics.reporter import send_daily_summary, send_weekly_report, get_pattern_insights

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("main")

IST = pytz.timezone("Asia/Kolkata")
SCAN_INTERVAL_SEC = 60    # how often to scan for new opportunities


def ist_now() -> datetime:
    return datetime.now(IST)


def ist_time_str() -> str:
    return ist_now().strftime("%H:%M")


def is_before(t: str) -> bool:
    return ist_time_str() < t


def is_after(t: str) -> bool:
    return ist_time_str() >= t


# ── Session bootstrap ──────────────────────────────────────────────────────

def start_session():
    init_db()
    clear_vol_cache()
    mode = "📄 PAPER TRADE" if PAPER_TRADE else "🔴 LIVE TRADE"
    notify(
        f"<b>[TRADE] 🚀 SESSION START — {mode}</b>\n"
        f"Date    : {date.today().isoformat()}\n"
        f"Capital : ₹{__import__('config.settings', fromlist=['CAPITAL']).CAPITAL:,.0f}\n"
        f"Scan    : {SCAN_START} → {SCAN_END} IST\n"
        f"Exit by : {FORCE_EXIT_TIME} IST"
    )
    log.info(f"Session started — {mode}")


# ── Position management loop ───────────────────────────────────────────────

def manage_open_position(order_mgr: OrderManager, risk_engine: RiskEngine):
    """Called every scan cycle when a position is open."""
    pos = order_mgr.open_position
    if not pos:
        return

    sym    = pos.symbol
    quote  = get_quote([sym]).get(sym, {})
    ltp    = quote.get("ltp", pos.entry_price)

    if ltp <= 0:
        log.warning(f"[MANAGE] No live price for {sym}")
        return

    # Fetch fresh candles for trailing SL
    df = get_candles(sym, interval=CANDLE_INTERVAL, lookback_days=1)

    # ── Check exits in priority order ─────────────────────────────────────

    # 1. Stop-loss hit
    if ltp <= pos.sl_price:
        result = order_mgr.exit_trade(ltp, "SL")
        if result and not result.get("partial"):
            _post_exit(result, risk_engine)
        return

    # 2. Target 1R reached — partial book
    target_1r = calculate_target(pos.entry_price, pos.sl_price, rr=1.0)
    if ltp >= target_1r and not pos.partial_booked:
        result = order_mgr.exit_trade(ltp, "TARGET_1R", qty_override=pos.qty // 2)
        log.info(f"[MANAGE] Partial booked at 1R. Trailing {pos.qty} remaining.")
        return

    # 3. Final target 2R
    target_2r = calculate_target(pos.entry_price, pos.sl_price, rr=2.0)
    if ltp >= target_2r:
        result = order_mgr.exit_trade(ltp, "TARGET_2R")
        if result and not result.get("partial"):
            _post_exit(result, risk_engine)
        return

    # 4. Force exit time
    if is_after(FORCE_EXIT_TIME):
        result = order_mgr.exit_trade(ltp, "FORCE_EXIT")
        if result and not result.get("partial"):
            _post_exit(result, risk_engine)
        return

    # 5. Update trailing SL (no exit yet)
    if not df.empty:
        new_sl = trailing_sl(df, pos.sl_price, pos.entry_price, ltp,
                             method="PREV_CANDLE")
        if new_sl > pos.sl_price:
            log.info(f"[MANAGE] Trail SL {pos.sl_price:.2f} → {new_sl:.2f}")
            pos.sl_price = new_sl

    log.info(f"[MANAGE] {sym} LTP={ltp:.2f}  SL={pos.sl_price:.2f}  "
             f"T2R={target_2r:.2f}  P&L≈₹{(ltp-pos.entry_price)*pos.qty:.0f}")


def _post_exit(result: dict, risk_engine: RiskEngine):
    """Update AI risk memory after a completed trade."""
    regime    = result.get("regime", "UNKNOWN")
    time_slot = RiskEngine.time_slot()
    rel_vol   = result.get("rel_vol", 2.0)
    rv_tier   = RiskEngine.rel_vol_tier(rel_vol)

    risk_engine.record_trade_result(
        net_pnl    = result["net_pnl"],
        r_multiple = result["r_multiple"],
        regime     = regime,
        time_slot  = time_slot,
        rel_vol_tier = rv_tier
    )


# ── Single scan cycle ──────────────────────────────────────────────────────

def scan_and_enter(order_mgr: OrderManager, risk_engine: RiskEngine):
    """One full scan → score → size → enter cycle."""

    # Gate: time window
    if is_before(SCAN_START) or is_after(SCAN_END):
        return

    # Gate: risk engine
    ok, reason = risk_engine.can_trade()
    if not ok:
        log.info(f"[SCAN] Skipping — {reason}")
        return

    # Gate: no double position
    if order_mgr.has_open_position():
        return

    log.info("[SCAN] Starting scan cycle...")

    # 1. Fetch top gainers
    gainers = get_top_gainers(min_chg_pct=1.5)
    if not gainers:
        log.info("[SCAN] No gainers meeting criteria")
        return

    log.info(f"[SCAN] {len(gainers)} gainers found — fetching candles...")

    # 2. Fetch candles for all candidates (batched)
    candle_map = {}
    for g in gainers[:15]:   # cap at 15 to control API calls
        df = get_candles(g["symbol"], interval=CANDLE_INTERVAL, lookback_days=2)
        candle_map[g["symbol"]] = df
        time.sleep(0.05)

    # 3. Nifty regime (use index candles as proxy)
    nifty_df = get_candles("NSE:NIFTY50-INDEX", interval=CANDLE_INTERVAL, lookback_days=1)
    regime   = RiskEngine.classify_regime(nifty_df)
    log.info(f"[SCAN] Market regime: {regime}")

    # 4. Apply pattern penalty to scores
    time_slot = RiskEngine.time_slot()
    for g in gainers[:15]:
        rv_tier = RiskEngine.rel_vol_tier(g.get("rel_vol", 2.0))
        penalty = risk_engine.get_pattern_penalty(regime, time_slot, rv_tier)
        g["_score_penalty"] = penalty

    # 5. Score and select best
    best = select_best_candidate(gainers[:15], candle_map)

    if not best:
        log.info("[SCAN] No valid candidate after scoring")
        return

    sym = best["symbol"]
    ltp = best["ltp"]
    df  = candle_map[sym]

    # 6. Select stop-loss
    sl_info = select_sl(df, ltp)
    if not sl_info:
        log.warning(f"[SCAN] No viable SL for {sym}")
        alert_no_trade(f"No viable SL for {sym}")
        return

    # 7. Size position
    sizing = risk_engine.calculate_position(ltp, sl_info["sl_price"])
    if not sizing["viable"]:
        log.warning(f"[SCAN] Position not viable: {sizing['reason']}")
        alert_no_trade(sizing["reason"])
        return

    # 8. Calculate target (2R default)
    target = calculate_target(ltp, sl_info["sl_price"], rr=2.0)

    log.info(f"[SCAN] → ENTERING {sym}: entry={ltp} SL={sl_info['sl_price']} "
             f"T={target} qty={sizing['qty']} risk=₹{sizing['risk_amount']:.0f}")

    # 9. Enter
    order_mgr.enter_trade(
        candidate    = best,
        sl_price     = sl_info["sl_price"],
        target_price = target,
        qty          = sizing["qty"],
        risk_amount  = sizing["risk_amount"],
        risk_pct     = sizing["risk_pct"],
        sl_model     = sl_info["model"],
        regime       = regime
    )


# ── Main loop ──────────────────────────────────────────────────────────────

def wait_for_token():
    """Block until a valid Fyers access token is available (via /login)."""
    from data.fyers_data import _get_current_token
    waited = 0
    while not _get_current_token():
        if waited == 0:
            log.warning("[LOOP] No Fyers token yet. Visit /login on your Railway "
                        "app URL to authenticate. Checking every 30s...")
        time.sleep(30)
        waited += 30
        if waited % 300 == 0:
            log.warning(f"[LOOP] Still waiting for Fyers auth... ({waited//60} min)")


def run_one_session():
    """Runs one full trading session (today), from now until FORCE_EXIT_TIME."""
    start_session()

    risk_engine = RiskEngine()
    order_mgr   = OrderManager()

    log.info(f"[LOOP] Waiting for scan window ({SCAN_START} IST)...")

    while True:
        # Hard stop at 15:15
        if is_after(FORCE_EXIT_TIME):
            if order_mgr.has_open_position():
                pos = order_mgr.open_position
                quote = get_quote([pos.symbol]).get(pos.symbol, {})
                ltp = quote.get("ltp", pos.entry_price)
                result = order_mgr.exit_trade(ltp, "FORCE_EXIT")
                if result and not result.get("partial"):
                    _post_exit(result, risk_engine)

            log.info("[LOOP] Force exit time reached — session ending")
            send_daily_summary()

            if datetime.now(IST).weekday() == 4:
                notify(get_pattern_insights())
                send_weekly_report()

            break

        # If position open: manage it
        if order_mgr.has_open_position():
            manage_open_position(order_mgr, risk_engine)
        else:
            scan_and_enter(order_mgr, risk_engine)

        time.sleep(SCAN_INTERVAL_SEC)


def run_forever():
    """
    Runs continuously on Railway:
      - Waits for Fyers auth token if missing
      - Waits for next session start (handles weekends/after-hours)
      - Runs the session, then loops back to wait for tomorrow
    """
    while True:
        wait_for_token()

        now = ist_time_str()
        weekday = datetime.now(IST).weekday()   # 0=Mon ... 5=Sat, 6=Sun

        if weekday >= 5:
            log.info("[LOOP] Weekend — sleeping 1 hour")
            time.sleep(3600)
            continue

        if is_before(SCAN_START):
            log.info(f"[LOOP] Before scan window ({now} < {SCAN_START}) — sleeping 60s")
            time.sleep(60)
            continue

        if is_after(FORCE_EXIT_TIME):
            log.info(f"[LOOP] After force-exit time ({now}) — session over for today, sleeping 30 min")
            time.sleep(1800)
            continue

        log.info("[LOOP] In trading window — starting session")
        try:
            run_one_session()
        except Exception as e:
            log.exception(f"[LOOP] Session crashed: {e}")
            notify(f"<b>[TRADE] ⚠️ ERROR</b>\nSession crashed: {e}")
            time.sleep(60)


if __name__ == "__main__":
    run_forever()
