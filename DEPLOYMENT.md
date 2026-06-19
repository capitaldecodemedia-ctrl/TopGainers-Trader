# Deployment Guide — GitHub + Railway

Same flow as Capital Decode. Follow in order.

## 1. Push to GitHub

```bash
cd top_gainers_trader
git init
git add .
git commit -m "Initial commit — top gainers trading system"
git branch -M main
git remote add origin https://github.com/<your-username>/<repo-name>.git
git push -u origin main
```

`.env` and `fyers_token.txt` are git-ignored — your secrets never get committed.

## 2. Create Railway project

1. Railway dashboard → New Project → Deploy from GitHub repo
2. Select this repo
3. Railway auto-detects Python via `railway.json` + `Procfile` — no changes needed

## 3. Set environment variables in Railway

Go to your service → Variables tab → add these (Raw Editor is fastest, paste all at once):

```
FYERS_APP_ID=VCI0VLOY6S-100
FYERS_SECRET_ID=BW96RVAWED
FYERS_REDIRECT_URI=https://your-app-name.up.railway.app/fyers/callback
TELEGRAM_BOT_TOKEN=<your bot token>
TELEGRAM_CHAT_ID=787902453
CAPITAL=50000
PAPER_TRADE=true
CANDLE_INTERVAL=5
BASE_RISK_PCT=1.0
MIN_RISK_PCT=0.25
MAX_TRADES_PER_DAY=10
DAILY_LOSS_LIMIT_PCT=2.0
DAILY_PROFIT_TARGET_PCT=3.0
MIN_SCORE=65
MIN_REL_VOLUME=2.0
MIN_AVG_DAILY_VALUE_CR=50
TZ=Asia/Kolkata
```

Leave `FYERS_ACCESS_TOKEN` **unset** — it's captured automatically via the web login flow below.

**Important:** `FYERS_REDIRECT_URI` must use your actual Railway-assigned domain.
Find it under Settings → Domains (Railway gives you a free `*.up.railway.app` URL).

## 4. Update Fyers app redirect URI

Go to your Fyers developer dashboard (myapi.fyers.in or similar) → your app →
edit the **Redirect URI** field to match exactly:

```
https://your-app-name.up.railway.app/fyers/callback
```

This must match what's in your Railway env var exactly (including https, no trailing slash).

## 5. Deploy

Railway auto-deploys on push. Check the deploy logs — you should see:

```
[RUN] Starting Flask web server thread...
[RUN] Starting trading loop...
[LOOP] No Fyers token yet. Visit /login on your Railway app URL to authenticate.
```

## 6. Authenticate with Fyers (do this every trading morning)

Visit:
```
https://your-app-name.up.railway.app/login
```

Log into Fyers, approve. You'll be redirected back and see:
```json
{"status": "success", "message": "Fyers access token captured..."}
```

The trading loop picks up the token automatically within 30 seconds — no restart needed.

**Note:** Fyers tokens expire ~6 AM IST daily. You must revisit `/login` each trading day
before 9:30 IST. (A future improvement: a scheduled reminder — see "Next steps" below.)

## 7. Verify it's running

```
https://your-app-name.up.railway.app/status
```

Returns today's session JSON (trades taken, P&L, risk level) once the session starts.

## 8. Check Telegram

Once Telegram access is restored in your region, you should start receiving `[TRADE]`
prefixed messages in the same chat as Capital Decode — session start, entries, exits,
daily summary.

---

## Daily routine (once live)

1. ~9:00 AM IST: visit `/login`, approve Fyers
2. System runs automatically 9:30 AM – 3:15 PM IST
3. Daily summary auto-sent to Telegram at 3:15 PM
4. Fridays: weekly report + pattern insights also sent

## Troubleshooting

- **"No Fyers token yet" stuck in logs** → you haven't visited `/login` yet today, or
  the redirect URI mismatch between Fyers dashboard and Railway env var
- **Railway app sleeping/restarting** → ensure you're on a plan that doesn't sleep
  idle services (Hobby plan services on Railway stay up if actively running a process,
  unlike serverless platforms)
- **Telegram silent** → check `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` are correct in
  Railway vars; also confirm Telegram isn't blocked in your region currently
