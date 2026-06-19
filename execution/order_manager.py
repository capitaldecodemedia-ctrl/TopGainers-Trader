"""
Order Manager.
PAPER_TRADE=true  → simulates fills, logs to DB, sends Telegram.
PAPER_TRADE=false → places real orders via Fyers API.

Paper fill price = last traded price at time of signal (realistic).
"""

import logging
from datetime import datetime
from typing import Optional

from config.settings import PAPER_TRADE
from core.database import get_conn
from core.notifier import alert_entry, alert_exit
from execution.sl_engine import calculate_cost

log = logging.getLogger(__name__)


class Position:
    """Represents one open position."""

    def __init__(self, symbol, entry_price, sl_price, target_price,
                 qty, risk_amount, risk_pct, score, sl_model,
                 regime, paper=True):
        self.symbol       = symbol
        self.entry_price  = entry_price
        self.sl_price     = sl_price
        self.target_price = target_price
        self.qty          = qty
        self.risk_amount  = risk_amount
        self.risk_pct     = risk_pct
        self.score        = score
        self.sl_model     = sl_model
        self.regime       = regime
        self.paper        = paper
        self.entry_time   = datetime.now()
        self.partial_booked = False
        self.partial_qty    = 0
        self.db_id          = None

    def to_dict(self):
        return self.__dict__


class OrderManager:
    def __init__(self):
        self.open_position: Optional[Position] = None

    def has_open_position(self) -> bool:
        return self.open_position is not None

    # ── Entry ──────────────────────────────────────────────────────────────

    def enter_trade(self, candidate: dict, sl_price: float, target_price: float,
                    qty: int, risk_amount: float, risk_pct: float,
                    sl_model: str, regime: str,
                    est_cost: float = None, total_risk: float = None) -> Optional[Position]:
        symbol      = candidate["symbol"]
        entry_price = candidate["ltp"]

        if PAPER_TRADE:
            fill_price = entry_price   # paper: fill at LTP
            log.info(f"[ORDER] PAPER ENTRY {symbol} qty={qty} @ ₹{fill_price}")
        else:
            fill_price = self._place_live_order(symbol, qty, "BUY")
            if fill_price is None:
                log.error(f"[ORDER] Live entry failed for {symbol}")
                return None

        pos = Position(
            symbol=symbol, entry_price=fill_price,
            sl_price=sl_price, target_price=target_price,
            qty=qty, risk_amount=risk_amount, risk_pct=risk_pct,
            score=candidate["score"], sl_model=sl_model,
            regime=regime, paper=PAPER_TRADE
        )

        # Persist to DB
        pos.db_id = self._insert_trade(pos)
        self.open_position = pos

        alert_entry(symbol, fill_price, sl_price, target_price,
                    qty, risk_amount, risk_pct, candidate["score"],
                    sl_model, PAPER_TRADE, est_cost=est_cost, total_risk=total_risk)
        return pos

    # ── Exit ───────────────────────────────────────────────────────────────

    def exit_trade(self, current_price: float, reason: str,
                   qty_override: int = None) -> Optional[dict]:
        if not self.open_position:
            return None

        pos = self.open_position
        exit_qty   = qty_override or pos.qty
        exit_price = current_price

        if PAPER_TRADE:
            fill_price = exit_price
            log.info(f"[ORDER] PAPER EXIT {pos.symbol} qty={exit_qty} @ ₹{fill_price} ({reason})")
        else:
            fill_price = self._place_live_order(pos.symbol, exit_qty, "SELL")
            if fill_price is None:
                log.error(f"[ORDER] Live exit failed for {pos.symbol}")
                fill_price = exit_price   # fallback to LTP

        gross_pnl = (fill_price - pos.entry_price) * exit_qty
        costs     = calculate_cost(pos.entry_price, fill_price, exit_qty)
        net_pnl   = gross_pnl - costs["total"]
        risk      = pos.entry_price - pos.sl_price
        r_mult    = (fill_price - pos.entry_price) / risk if risk > 0 else 0

        result = {
            "symbol"     : pos.symbol,
            "entry_price": pos.entry_price,
            "exit_price" : fill_price,
            "qty"        : exit_qty,
            "gross_pnl"  : round(gross_pnl, 2),
            "net_pnl"    : round(net_pnl, 2),
            "total_cost" : round(costs["total"], 2),
            "r_multiple" : round(r_mult, 3),
            "exit_reason": reason,
            "regime"     : pos.regime,
        }

        alert_exit(pos.symbol, pos.entry_price, fill_price,
                   exit_qty, net_pnl, r_mult, reason, PAPER_TRADE)

        # Partial booking: book 50% at 1R, keep remainder open
        if reason == "TARGET_1R" and not pos.partial_booked:
            partial_qty   = exit_qty // 2
            remain_qty    = exit_qty - partial_qty
            pos.partial_booked = True
            pos.partial_qty    = remain_qty
            pos.qty            = remain_qty
            # Don't clear position yet — still have trailing qty
            self._update_trade_db(pos.db_id, fill_price, net_pnl * 0.5, costs["total"], r_mult, "PARTIAL")
            log.info(f"[ORDER] Partial booked {partial_qty} @ ₹{fill_price} — trailing {remain_qty}")
            return {**result, "partial": True, "remain_qty": remain_qty}

        # Full exit
        self._update_trade_db(pos.db_id, fill_price, net_pnl, costs["total"], r_mult, reason)
        self.open_position = None
        return {**result, "partial": False}

    # ── DB helpers ─────────────────────────────────────────────────────────

    def _insert_trade(self, pos: Position) -> int:
        with get_conn() as conn:
            cur = conn.execute("""
                INSERT INTO trades
                (date, entry_time, symbol, entry_price, sl_price, target_price,
                 qty, risk_amount, risk_pct_used, score_at_entry, sl_model, regime, paper)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                pos.entry_time.date().isoformat(),
                pos.entry_time.strftime("%H:%M:%S"),
                pos.symbol, pos.entry_price, pos.sl_price, pos.target_price,
                pos.qty, pos.risk_amount, pos.risk_pct,
                pos.score, pos.sl_model, pos.regime, int(pos.paper)
            ))
            return cur.lastrowid

    def _update_trade_db(self, trade_id, exit_price, net_pnl, cost, r_mult, reason):
        with get_conn() as conn:
            conn.execute("""
                UPDATE trades SET
                    exit_time=?, exit_price=?, net_pnl=?, total_cost=?,
                    r_multiple=?, exit_reason=?, gross_pnl=?
                WHERE id=?
            """, (
                datetime.now().strftime("%H:%M:%S"),
                exit_price, round(net_pnl, 2), round(cost, 2),
                round(r_mult, 3), reason,
                round(net_pnl + cost, 2),   # gross = net + cost
                trade_id
            ))

    # ── Live order placement (Fyers) ───────────────────────────────────────

    def _place_live_order(self, symbol: str, qty: int, side: str) -> Optional[float]:
        """Places a market order via Fyers. Returns fill price or None."""
        try:
            from data.fyers_data import get_client, get_quote
            fyers = get_client()
            order_data = {
                "symbol"      : symbol,
                "qty"         : qty,
                "type"        : 2,           # Market order
                "side"        : 1 if side == "BUY" else -1,
                "productType" : "INTRADAY",
                "limitPrice"  : 0,
                "stopPrice"   : 0,
                "validity"    : "DAY",
                "disclosedQty": 0,
                "offlineOrder": False,
            }
            resp = fyers.place_order(data=order_data)
            if resp.get("s") == "ok":
                # Fetch actual fill price from positions
                quote = get_quote([symbol])
                return quote.get(symbol, {}).get("ltp", None)
            else:
                log.error(f"[ORDER] Fyers order failed: {resp}")
                return None
        except Exception as e:
            log.error(f"[ORDER] Live order exception: {e}")
            return None
