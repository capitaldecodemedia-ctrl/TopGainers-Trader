"""
Sends [TRADE] prefixed messages to existing Capital Decode Telegram bot.
Non-blocking: uses a background thread so it never delays trade logic.
"""

import threading
import requests
import logging
from datetime import datetime
from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

log = logging.getLogger(__name__)


def _send(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("[TELEGRAM] Token or chat_id missing — skipping")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML"
        }, timeout=10)
        if not r.ok:
            log.warning(f"[TELEGRAM] Failed: {r.text}")
    except Exception as e:
        log.error(f"[TELEGRAM] Error: {e}")


def notify(text: str):
    """Fire-and-forget — never blocks the trading loop."""
    threading.Thread(target=_send, args=(text,), daemon=True).start()


# ── Formatted alert builders ───────────────────────────────────────────────

def alert_entry(symbol, entry, sl, target, qty, risk_amt, risk_pct, score, sl_model,
                paper=True, est_cost=None, total_risk=None):
    tag = "📄 PAPER" if paper else "🟢 LIVE"
    cost_line = f"Est. cost: ₹{est_cost:.0f}\n" if est_cost is not None else ""
    true_risk_line = f"True risk : ₹{total_risk:.0f} (incl. costs)\n" if total_risk is not None else ""
    notify(
        f"<b>[TRADE] ENTRY {tag}</b>\n"
        f"Symbol  : <code>{symbol}</code>\n"
        f"Entry   : ₹{entry:.2f}  |  Qty: {qty}\n"
        f"SL      : ₹{sl:.2f}  ({sl_model})\n"
        f"Target  : ₹{target:.2f}\n"
        f"Risk    : ₹{risk_amt:.0f}  ({risk_pct:.2f}% of capital)\n"
        f"{cost_line}"
        f"{true_risk_line}"
        f"Score   : {score:.1f}/100\n"
        f"Time    : {datetime.now().strftime('%H:%M:%S')}"
    )


def alert_exit(symbol, entry, exit_price, qty, net_pnl, r_multiple, reason, paper=True):
    tag = "📄 PAPER" if paper else "🔴 LIVE"
    emoji = "✅" if net_pnl >= 0 else "❌"
    notify(
        f"<b>[TRADE] EXIT {tag}</b> {emoji}\n"
        f"Symbol  : <code>{symbol}</code>\n"
        f"Entry   : ₹{entry:.2f}  →  Exit: ₹{exit_price:.2f}\n"
        f"Qty     : {qty}  |  Reason: {reason}\n"
        f"Net P&L : ₹{net_pnl:.2f}\n"
        f"R-mult  : {r_multiple:.2f}R\n"
        f"Time    : {datetime.now().strftime('%H:%M:%S')}"
    )


def alert_risk_change(old_pct, new_pct, reason):
    notify(
        f"<b>[TRADE] ⚠️ RISK ADJUSTED</b>\n"
        f"{old_pct:.2f}% → {new_pct:.2f}%\n"
        f"Reason: {reason}"
    )


def alert_session_stop(reason, net_pnl, trades):
    notify(
        f"<b>[TRADE] 🛑 SESSION STOPPED</b>\n"
        f"Reason  : {reason}\n"
        f"Net P&L : ₹{net_pnl:.2f}\n"
        f"Trades  : {trades}\n"
        f"Time    : {datetime.now().strftime('%H:%M:%S')}"
    )


def alert_daily_summary(date, trades, wins, losses, net_pnl, max_dd, win_rate, risk_end):
    emoji = "🟢" if net_pnl >= 0 else "🔴"
    notify(
        f"<b>[TRADE] 📊 DAILY SUMMARY — {date}</b> {emoji}\n"
        f"Trades  : {trades}  |  W/L: {wins}/{losses}\n"
        f"Win rate: {win_rate:.1f}%\n"
        f"Net P&L : ₹{net_pnl:.2f}\n"
        f"Max DD  : ₹{max_dd:.2f}\n"
        f"Risk end: {risk_end:.2f}% of capital"
    )


def alert_no_trade(reason):
    notify(f"<b>[TRADE] ℹ️ NO TRADE</b>\n{reason}\n{datetime.now().strftime('%H:%M:%S')}")
