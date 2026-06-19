"""
Adaptive Risk Engine — the "AI memory" layer.

Logic:
  - Starts each day at BASE_RISK_PCT
  - Reduces risk after each loss (steeper reduction on consecutive losses)
  - Restores risk gradually on winning streaks
  - Reads pattern_memory to penalise entry in conditions that historically lost
  - Writes to daily_memory and pattern_memory after each trade
  - Never goes below MIN_RISK_PCT, never above BASE_RISK_PCT

This is rule-based ML — no LLM API calls. Fast, deterministic, cheap.
"""

import logging
import sqlite3
import pandas as pd
from datetime import datetime, date
from typing import Optional

from config.settings import (
    BASE_RISK_PCT, MIN_RISK_PCT, CAPITAL,
    DAILY_LOSS_LIMIT, DAILY_PROFIT_TARGET, MAX_TRADES_PER_DAY
)
from core.database import get_conn
from core.notifier import alert_risk_change, alert_session_stop

log = logging.getLogger(__name__)


class RiskEngine:
    def __init__(self):
        self.today         = date.today().isoformat()
        self.current_risk_pct  = BASE_RISK_PCT
        self.trades_taken  = 0
        self.wins          = 0
        self.losses        = 0
        self.consec_losses = 0
        self.consec_wins   = 0
        self.session_pnl   = 0.0          # net P&L so far today
        self.peak_pnl      = 0.0          # for intraday drawdown tracking
        self.session_stopped = False
        self.stop_reason   = None
        self._init_daily_memory()
        self._load_pattern_penalties()

    # ── Initialise / restore today's row ──────────────────────────────────

    def _init_daily_memory(self):
        with get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM daily_memory WHERE date=?", (self.today,)
            ).fetchone()
            if row:
                self.trades_taken  = row["trades_taken"]
                self.wins          = row["wins"]
                self.losses        = row["losses"]
                self.consec_losses = row["consec_losses"]
                self.session_pnl   = row["net_pnl"]
                self.current_risk_pct = row["risk_pct_end"] or BASE_RISK_PCT
                self.session_stopped  = bool(row["session_stopped"])
                self.stop_reason      = row["stop_reason"]
            else:
                # First time today — inherit yesterday's ending risk as starting point
                yesterday_risk = self._get_yesterday_ending_risk()
                self.current_risk_pct = min(yesterday_risk, BASE_RISK_PCT)
                conn.execute(
                    "INSERT OR IGNORE INTO daily_memory (date, risk_pct_start, risk_pct_end) VALUES (?,?,?)",
                    (self.today, self.current_risk_pct, self.current_risk_pct)
                )
        log.info(f"[RISK] Session risk: {self.current_risk_pct:.2f}%  "
                 f"Trades: {self.trades_taken}  W/L: {self.wins}/{self.losses}")

    def _get_yesterday_ending_risk(self) -> float:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT risk_pct_end FROM daily_memory WHERE date < ? ORDER BY date DESC LIMIT 1",
                (self.today,)
            ).fetchone()
        if row and row["risk_pct_end"]:
            # If yesterday ended below base, nudge back up by 0.1% per good day
            return min(row["risk_pct_end"] + 0.1, BASE_RISK_PCT)
        return BASE_RISK_PCT

    # ── Pattern memory ────────────────────────────────────────────────────

    def _load_pattern_penalties(self):
        """
        Build a dict of pattern_key → win_rate from last 30 days.
        Used to reduce effective score for historically bad conditions.
        """
        self.pattern_stats: dict[str, dict] = {}
        with get_conn() as conn:
            rows = conn.execute("""
                SELECT pattern_key,
                       COUNT(*) as total,
                       SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END) as wins
                FROM pattern_memory
                WHERE date >= date('now','-30 days')
                GROUP BY pattern_key
                HAVING total >= 5
            """).fetchall()
        for row in rows:
            self.pattern_stats[row["pattern_key"]] = {
                "win_rate": row["wins"] / row["total"],
                "total"   : row["total"],
            }
        log.info(f"[RISK] Loaded {len(self.pattern_stats)} pattern stats from memory")

    def get_pattern_penalty(self, regime: str, time_slot: str, rel_vol_tier: str) -> float:
        """
        Returns a score penalty (0–20) based on historical win rate in this condition.
        High-loss patterns get penalised so they rarely reach MIN_SCORE.
        """
        key = f"{regime}+{time_slot}+{rel_vol_tier}"
        stats = self.pattern_stats.get(key)
        if not stats:
            return 0.0
        win_rate = stats["win_rate"]
        if win_rate >= 0.55:
            return 0.0    # no penalty
        elif win_rate >= 0.45:
            return 5.0
        elif win_rate >= 0.35:
            return 12.0
        else:
            return 20.0   # very bad historical pattern

    # ── Risk calculation ──────────────────────────────────────────────────

    def get_current_risk_pct(self) -> float:
        return self.current_risk_pct

    def calculate_position(self, entry_price: float, sl_price: float,
                           target_price: float = None) -> dict:
        """
        Returns: {qty, risk_amount, risk_pct, risk_per_share, viable, reason}

        FIX A — Cost-aware sizing: the actual loss on a stopped-out trade
        includes brokerage/STT/GST/slippage on top of the raw price move.
        We size the position so the *combined* loss (price move + costs)
        stays within the intended risk budget — not just the price move alone.

        FIX B — Minimum trade size filter: if estimated costs would eat up
        too large a share of the intended risk (default cap: 25%), the trade
        is rejected outright. Small, cost-dominated trades have a structurally
        worse risk:reward than the setup suggests and are not worth taking.
        """
        from execution.sl_engine import calculate_cost
        from config.settings import MAX_COST_PCT_OF_RISK

        risk_amount    = CAPITAL * self.current_risk_pct / 100
        risk_per_share = entry_price - sl_price
        if risk_per_share <= 0:
            return {"viable": False, "reason": "SL >= entry price"}

        qty_by_capital = int((CAPITAL * 0.20) / entry_price)   # max 20% per trade

        # First pass: naive qty ignoring costs, to get a starting estimate
        qty_naive = int(risk_amount / risk_per_share)
        qty = min(qty_naive, qty_by_capital)

        if qty < 1:
            return {"viable": False,
                    "reason": f"Qty=0: risk ₹{risk_amount:.0f}, risk/share ₹{risk_per_share:.2f}"}

        # Estimate round-trip cost at this qty (entry → stop-loss exit, the
        # worst case scenario cost-wise since loss-side STT/slippage apply)
        est_cost = calculate_cost(entry_price, sl_price, qty)["total"]

        # FIX A — Reduce qty so (price-move loss + costs) <= risk_amount.
        # Solve directly: qty * risk_per_share + cost(qty) <= risk_amount.
        # Cost scales roughly linearly with qty, so one correction pass is
        # sufficient in practice; loop a few times to be safe.
        for _ in range(5):
            available_for_price_risk = risk_amount - est_cost
            if available_for_price_risk <= 0:
                qty = 0
                break
            new_qty = int(available_for_price_risk / risk_per_share)
            new_qty = min(new_qty, qty_by_capital)
            if new_qty == qty:
                break
            qty = new_qty
            if qty < 1:
                break
            est_cost = calculate_cost(entry_price, sl_price, qty)["total"]

        if qty < 1:
            return {"viable": False,
                    "reason": f"Qty=0 after cost adjustment: risk ₹{risk_amount:.0f} too small "
                              f"for risk/share ₹{risk_per_share:.2f} once costs included"}

        # Recompute final cost + actual risk at the settled qty
        final_cost  = calculate_cost(entry_price, sl_price, qty)["total"]
        price_risk  = qty * risk_per_share
        total_risk  = price_risk + final_cost   # what you'd actually lose if SL hits

        # FIX B — Minimum trade size filter: reject if costs dominate the trade
        cost_pct_of_risk = (final_cost / risk_amount * 100) if risk_amount > 0 else 999
        if cost_pct_of_risk > MAX_COST_PCT_OF_RISK:
            return {
                "viable": False,
                "reason": (f"Costs (₹{final_cost:.0f}) would eat {cost_pct_of_risk:.0f}% of "
                           f"intended risk (₹{risk_amount:.0f}) — exceeds {MAX_COST_PCT_OF_RISK:.0f}% cap. "
                           f"Trade too small to be cost-efficient.")
            }

        return {
            "viable"           : True,
            "qty"              : qty,
            "risk_amount"      : round(price_risk, 2),       # raw price-move risk
            "estimated_cost"   : round(final_cost, 2),
            "total_risk"       : round(total_risk, 2),       # what you'd actually lose incl. costs
            "risk_pct"         : round(total_risk / CAPITAL * 100, 3),  # true % risked, costs included
            "cost_pct_of_risk" : round(cost_pct_of_risk, 1),
            "risk_per_share"   : round(risk_per_share, 2),
            "capital_used"     : round(qty * entry_price, 2),
        }

    # ── Gates ─────────────────────────────────────────────────────────────

    def can_trade(self) -> tuple[bool, str]:
        if self.session_stopped:
            return False, f"Session stopped: {self.stop_reason}"
        if self.trades_taken >= MAX_TRADES_PER_DAY:
            return False, f"Max trades reached ({MAX_TRADES_PER_DAY})"
        if self.session_pnl <= -DAILY_LOSS_LIMIT:
            self._stop_session("Daily loss limit hit")
            return False, "Daily loss limit"
        if self.session_pnl >= DAILY_PROFIT_TARGET:
            self._stop_session("Daily profit target hit")
            return False, "Daily profit target hit"
        return True, "ok"

    def _stop_session(self, reason: str):
        self.session_stopped = True
        self.stop_reason     = reason
        self._persist()
        alert_session_stop(reason, self.session_pnl, self.trades_taken)
        log.warning(f"[RISK] SESSION STOPPED — {reason}")

    # ── Post-trade update ─────────────────────────────────────────────────

    def record_trade_result(self, net_pnl: float, r_multiple: float,
                            regime: str, time_slot: str, rel_vol_tier: str):
        """Called after each trade exit. Updates risk, memory, patterns."""
        self.trades_taken += 1
        self.session_pnl  += net_pnl
        self.peak_pnl      = max(self.peak_pnl, self.session_pnl)

        outcome = "WIN" if net_pnl >= 0 else "LOSS"
        old_risk = self.current_risk_pct

        if net_pnl >= 0:
            self.wins         += 1
            self.consec_losses = 0
            self.consec_wins  += 1
            new_risk = self._risk_on_win(old_risk)
        else:
            self.losses       += 1
            self.consec_losses+= 1
            self.consec_wins   = 0
            new_risk = self._risk_on_loss(old_risk)

        if new_risk != old_risk:
            self.current_risk_pct = new_risk
            alert_risk_change(old_risk, new_risk,
                f"{'Win' if net_pnl>=0 else 'Loss'} — consec_losses={self.consec_losses}")

        # Persist pattern
        pattern_key = f"{regime}+{time_slot}+{rel_vol_tier}"
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO pattern_memory (date, pattern_key, outcome, r_multiple) VALUES (?,?,?,?)",
                (self.today, pattern_key, outcome, round(r_multiple, 3))
            )
        self._persist()
        log.info(f"[RISK] Trade recorded: {outcome} ₹{net_pnl:.2f}  "
                 f"Risk: {old_risk:.2f}→{new_risk:.2f}%  "
                 f"Session P&L: ₹{self.session_pnl:.2f}")

    def _risk_on_loss(self, current: float) -> float:
        """Geometric reduction on losses. Steeper with consecutive losses."""
        reductions = {1: 0.20, 2: 0.30, 3: 0.40}   # % reduction per consec loss
        pct = reductions.get(self.consec_losses, 0.50)
        new = current * (1 - pct)
        return max(round(new, 3), MIN_RISK_PCT)

    def _risk_on_win(self, current: float) -> float:
        """Gradual restoration — max +0.1% per win, capped at BASE."""
        if current >= BASE_RISK_PCT:
            return current
        new = current + 0.1
        return min(round(new, 3), BASE_RISK_PCT)

    def _persist(self):
        with get_conn() as conn:
            conn.execute("""
                UPDATE daily_memory SET
                    trades_taken=?, wins=?, losses=?, consec_losses=?,
                    net_pnl=?, risk_pct_end=?, session_stopped=?, stop_reason=?
                WHERE date=?
            """, (self.trades_taken, self.wins, self.losses, self.consec_losses,
                  round(self.session_pnl, 2), self.current_risk_pct,
                  int(self.session_stopped), self.stop_reason, self.today))

    # ── Regime classifier ─────────────────────────────────────────────────

    @staticmethod
    def classify_regime(nifty_df: pd.DataFrame) -> str:
        """
        Simple regime from last 20 Nifty candles.
        Returns: TRENDING / CHOPPY / VOLATILE
        """
        if nifty_df.empty or len(nifty_df) < 20:
            return "UNKNOWN"
        from core.indicators import atr, ema
        last20 = nifty_df.tail(20)
        atr_val = atr(last20, 14).iloc[-1]
        price   = last20["close"].iloc[-1]
        vol_pct = atr_val / price * 100

        ema20 = ema(last20["close"], 20)
        slope = (ema20.iloc[-1] - ema20.iloc[-5]) / ema20.iloc[-5] * 100

        if vol_pct > 0.8:
            return "VOLATILE"
        elif abs(slope) > 0.3:
            return "TRENDING"
        else:
            return "CHOPPY"

    @staticmethod
    def time_slot() -> str:
        h = datetime.now().hour
        if h < 11:
            return "MORNING"
        elif h < 13:
            return "MIDDAY"
        else:
            return "AFTERNOON"

    @staticmethod
    def rel_vol_tier(rel_vol: float) -> str:
        if rel_vol >= 5:
            return "VERY_HIGH"
        elif rel_vol >= 3:
            return "HIGH"
        elif rel_vol >= 2:
            return "MEDIUM"
        else:
            return "LOW"
