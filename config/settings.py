import os
from dotenv import load_dotenv

load_dotenv()

# ── Fyers ──────────────────────────────────────────────────────────────────
FYERS_APP_ID       = os.getenv("FYERS_APP_ID", "")
FYERS_SECRET_ID    = os.getenv("FYERS_SECRET_ID", "")
FYERS_ACCESS_TOKEN = os.getenv("FYERS_ACCESS_TOKEN", "")
FYERS_REDIRECT_URI = os.getenv("FYERS_REDIRECT_URI", "https://localhost/fyers/callback")

# ── Telegram ───────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Capital & trading ──────────────────────────────────────────────────────
CAPITAL              = float(os.getenv("CAPITAL", 50000))
PAPER_TRADE          = os.getenv("PAPER_TRADE", "true").lower() == "true"
CANDLE_INTERVAL      = int(os.getenv("CANDLE_INTERVAL", 5))

# ── Risk ───────────────────────────────────────────────────────────────────
BASE_RISK_PCT          = float(os.getenv("BASE_RISK_PCT", 1.0))
MIN_RISK_PCT           = float(os.getenv("MIN_RISK_PCT", 0.25))
MAX_TRADES_PER_DAY     = int(os.getenv("MAX_TRADES_PER_DAY", 10))
DAILY_LOSS_LIMIT_PCT   = float(os.getenv("DAILY_LOSS_LIMIT_PCT", 2.0))
DAILY_PROFIT_TARGET_PCT= float(os.getenv("DAILY_PROFIT_TARGET_PCT", 3.0))

# ── Scoring ────────────────────────────────────────────────────────────────
MIN_SCORE              = float(os.getenv("MIN_SCORE", 65))
MIN_REL_VOLUME         = float(os.getenv("MIN_REL_VOLUME", 2.0))
MIN_AVG_DAILY_VALUE_CR = float(os.getenv("MIN_AVG_DAILY_VALUE_CR", 50))

# ── Cost-efficiency filter ───────────────────────────────────────────────
# Reject a trade if estimated brokerage/STT/GST/slippage would consume more
# than this % of the intended risk budget. Prevents small, cost-dominated
# trades where fixed costs (~₹40-60) overwhelm a tight stop-loss.
MAX_COST_PCT_OF_RISK = float(os.getenv("MAX_COST_PCT_OF_RISK", 25))

# ── Market timing (IST) ────────────────────────────────────────────────────
MARKET_OPEN     = "09:15"
SCAN_START      = "09:30"   # first 15 min skipped — too chaotic
SCAN_END        = "15:00"   # no new entries after this
FORCE_EXIT_TIME = "15:15"   # hard exit all positions
MARKET_CLOSE    = "15:30"

# ── Derived ────────────────────────────────────────────────────────────────
DAILY_LOSS_LIMIT   = CAPITAL * DAILY_LOSS_LIMIT_PCT / 100
DAILY_PROFIT_TARGET= CAPITAL * DAILY_PROFIT_TARGET_PCT / 100
