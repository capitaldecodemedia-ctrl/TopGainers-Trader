"""
Web server for Railway deployment.

Purpose:
  1. /login          -> redirects you to Fyers login page
  2. /fyers/callback -> Fyers redirects here after approval;
                        exchanges auth_code for access_token automatically,
                        stores it in-memory + writes to a local token file
  3. /health         -> Railway health check / uptime ping
  4. /status         -> quick JSON view of today's session state
  5. /dashboard      -> human-readable results page (works without Telegram)
  6. /trades         -> raw JSON trade list for any date

This replaces the manual copy-paste auth flow. Visit https://<your-app>.railway.app/login
each morning before market open, approve once, done — token is captured automatically.

Visit https://<your-app>.railway.app/dashboard any time to see results,
regardless of whether Telegram alerts are reachable.
"""

import os
import threading
import logging
from pathlib import Path
from flask import Flask, request, redirect, jsonify

from fyers_apiv3 import fyersModel

log = logging.getLogger("web")
app = Flask(__name__)

TOKEN_FILE = Path(__file__).parent / "fyers_token.txt"


def get_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def save_token(token: str):
    TOKEN_FILE.write_text(token.strip())
    os.environ["FYERS_ACCESS_TOKEN"] = token.strip()
    log.info("[AUTH] Token saved and loaded into environment")


def load_saved_token() -> str:
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    return ""


@app.route("/")
def index():
    return jsonify({
        "service": "top-gainers-trader",
        "status": "running",
        "routes": ["/login", "/fyers/callback", "/health", "/status", "/dashboard", "/trades"]
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/login")
def login():
    app_id       = get_env("FYERS_APP_ID")
    secret_id    = get_env("FYERS_SECRET_ID")
    redirect_uri = get_env("FYERS_REDIRECT_URI")

    if not app_id or not secret_id:
        return jsonify({"error": "FYERS_APP_ID or FYERS_SECRET_ID not set in Railway env vars"}), 500

    session = fyersModel.SessionModel(
        client_id=app_id,
        secret_key=secret_id,
        redirect_uri=redirect_uri,
        response_type="code",
        grant_type="authorization_code"
    )
    login_url = session.generate_authcode()
    return redirect(login_url)


@app.route("/fyers/callback")
def fyers_callback():
    auth_code = request.args.get("auth_code")
    s = request.args.get("s")

    if not auth_code:
        return jsonify({"error": "No auth_code in callback", "params": dict(request.args)}), 400

    app_id       = get_env("FYERS_APP_ID")
    secret_id    = get_env("FYERS_SECRET_ID")
    redirect_uri = get_env("FYERS_REDIRECT_URI")

    session = fyersModel.SessionModel(
        client_id=app_id,
        secret_key=secret_id,
        redirect_uri=redirect_uri,
        response_type="code",
        grant_type="authorization_code"
    )
    session.set_token(auth_code)
    response = session.generate_token()

    if response.get("s") != "ok":
        return jsonify({"error": "Token exchange failed", "response": response}), 500

    access_token = response["access_token"]
    save_token(access_token)

    return jsonify({
        "status": "success",
        "message": "Fyers access token captured. You can close this tab. Trading loop will pick it up automatically.",
        "token_preview": access_token[:20] + "..."
    })


@app.route("/status")
def status():
    from datetime import date
    from core.database import get_conn
    today = date.today().isoformat()
    try:
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM daily_memory WHERE date=?", (today,)).fetchone()
        if row:
            return jsonify(dict(row))
        return jsonify({"date": today, "message": "No session data yet today"})
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/trades")
def trades():
    """JSON list of trades. Optional ?date=YYYY-MM-DD, defaults to today."""
    from datetime import date
    from core.database import get_conn
    target_date = request.args.get("date", date.today().isoformat())
    try:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE date=? ORDER BY entry_time", (target_date,)
            ).fetchall()
        return jsonify({"date": target_date, "count": len(rows), "trades": [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/dashboard")
def dashboard():
    """Human-readable HTML view of today's (or any date's) trades + summary."""
    from datetime import date
    from core.database import get_conn

    target_date = request.args.get("date", date.today().isoformat())

    try:
        with get_conn() as conn:
            trades = conn.execute(
                "SELECT * FROM trades WHERE date=? ORDER BY entry_time", (target_date,)
            ).fetchall()
            mem = conn.execute(
                "SELECT * FROM daily_memory WHERE date=?", (target_date,)
            ).fetchone()
    except Exception as e:
        return f"<p>Error reading database: {e}</p>", 500

    trades = [dict(t) for t in trades]
    mem = dict(mem) if mem else {}

    completed = [t for t in trades if t.get("exit_time")]
    wins = sum(1 for t in completed if (t.get("net_pnl") or 0) >= 0)
    losses = len(completed) - wins
    net_pnl = sum(t.get("net_pnl") or 0 for t in completed)
    win_rate = round(wins / len(completed) * 100, 1) if completed else 0

    pnl_color = "#0F6E56" if net_pnl >= 0 else "#993C1D"

    rows_html = ""
    if trades:
        for t in trades:
            row_pnl = t.get("net_pnl")
            row_color = "#0F6E56" if (row_pnl or 0) >= 0 else "#993C1D"
            pnl_str = f"₹{row_pnl:.2f}" if row_pnl is not None else "—"
            rows_html += f"""
            <tr>
                <td>{t.get('entry_time','—')}</td>
                <td>{t.get('symbol','—')}</td>
                <td>₹{t.get('entry_price','—')}</td>
                <td>{t.get('exit_price') and f"₹{t['exit_price']}" or '—'}</td>
                <td>{t.get('qty','—')}</td>
                <td>{t.get('sl_model','—')}</td>
                <td>{t.get('exit_reason') or 'OPEN'}</td>
                <td style="color:{row_color};font-weight:600">{pnl_str}</td>
                <td>{t.get('score_at_entry') or '—'}</td>
            </tr>"""
    else:
        rows_html = "<tr><td colspan='9' style='text-align:center;color:#888'>No trades yet for this date</td></tr>"

    html = f"""
    <html>
    <head>
        <title>Trading Dashboard — {target_date}</title>
        <style>
            body {{ font-family: -apple-system, sans-serif; max-width: 900px; margin: 40px auto; padding: 0 20px; color: #2C2C2A; }}
            h1 {{ font-size: 20px; font-weight: 500; }}
            .summary {{ display: flex; gap: 16px; margin: 20px 0; flex-wrap: wrap; }}
            .card {{ background: #F1EFE8; border-radius: 10px; padding: 12px 18px; min-width: 120px; }}
            .card .label {{ font-size: 12px; color: #5F5E5A; }}
            .card .val {{ font-size: 20px; font-weight: 500; margin-top: 4px; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 20px; font-size: 13px; }}
            th {{ text-align: left; padding: 8px; border-bottom: 2px solid #D3D1C7; color: #5F5E5A; font-weight: 500; }}
            td {{ padding: 8px; border-bottom: 1px solid #E8E6DD; }}
            .nav {{ margin-bottom: 16px; }}
            .nav a {{ color: #185FA5; text-decoration: none; margin-right: 16px; font-size: 13px; }}
            form {{ margin-bottom: 20px; }}
            input[type=date] {{ padding: 6px 10px; border-radius: 6px; border: 1px solid #D3D1C7; }}
            button {{ padding: 6px 14px; border-radius: 6px; border: none; background: #185FA5; color: white; cursor: pointer; }}
        </style>
    </head>
    <body>
        <div class="nav">
            <a href="/login">Re-authenticate Fyers</a>
            <a href="/status">Raw JSON status</a>
            <a href="/trades?date={target_date}">Raw JSON trades</a>
        </div>
        <h1>Trading dashboard — {target_date}</h1>
        <form method="get" action="/dashboard">
            <input type="date" name="date" value="{target_date}">
            <button type="submit">View date</button>
        </form>
        <div class="summary">
            <div class="card"><div class="label">Trades</div><div class="val">{len(completed)}</div></div>
            <div class="card"><div class="label">Wins / Losses</div><div class="val">{wins} / {losses}</div></div>
            <div class="card"><div class="label">Win rate</div><div class="val">{win_rate}%</div></div>
            <div class="card"><div class="label">Net P&amp;L</div><div class="val" style="color:{pnl_color}">₹{net_pnl:.2f}</div></div>
            <div class="card"><div class="label">Risk level end</div><div class="val">{mem.get('risk_pct_end','—')}%</div></div>
            <div class="card"><div class="label">Session stopped?</div><div class="val">{'Yes — ' + str(mem.get('stop_reason')) if mem.get('session_stopped') else 'No'}</div></div>
        </div>
        <table>
            <tr>
                <th>Time</th><th>Symbol</th><th>Entry</th><th>Exit</th><th>Qty</th>
                <th>SL model</th><th>Exit reason</th><th>Net P&amp;L</th><th>Score</th>
            </tr>
            {rows_html}
        </table>
    </body>
    </html>
    """
    return html


def run_web():
    port = int(get_env("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    run_web()
