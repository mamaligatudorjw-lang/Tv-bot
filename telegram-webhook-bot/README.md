# Binance Monitor → Telegram Bot

Polls the Gate.io Futures market every 5 minutes and sends Telegram alerts for the configured signal types across USDT perpetual pairs.

## Price timing and outcome reports

Outcome reports classify the result from the recorded `entry_price`, which is the bot's paper-entry basis. During a delayed polling cycle, that recorded basis may differ from the price a human could see when acting on the Telegram alert. Snapshot-to-delivery price risk is measured separately by the polling telemetry and is not retroactively applied to historical outcomes.

## Alerts

| Signal | Trigger |
|---|---|
| 🆕 New Listing | A new USDT pair appears on Binance Spot |
| 📈 New 24h High | A coin sets a new 24h high between checks |
| 🔥 RSI Overbought | RSI (14, 1h candles) ≥ 70 |
| 🧊 RSI Oversold | RSI (14, 1h candles) ≤ 30 |

RSI and 24h high alerts only fire for pairs with ≥ $50,000 in 24h USDT volume.
Each alert type has a 1-hour cooldown per coin to avoid spam.

## Telegram Commands

Send these in your chat with the bot:

| Command | Description |
|---|---|
| `/status` | Current state: pairs tracked, last run, alert counts, silence status |
| `/silence` | Pause all market alerts (bot keeps running and monitoring) |
| `/unmute` | Resume alerts |

## Keep-alive with UptimeRobot

Replit free-tier projects sleep after inactivity. Set up UptimeRobot to ping `/health` every 5 minutes to keep the bot awake.

**Steps:**
1. Go to [uptimerobot.com](https://uptimerobot.com) and create a free account
2. Click **Add New Monitor**
3. Set the following:
   - **Monitor Type:** HTTP(s)
   - **Friendly Name:** Binance Bot
   - **URL:** `https://<your-replit-domain>/health`
   - **Monitoring Interval:** 5 minutes
4. Click **Create Monitor**

Your Replit domain is shown in the workspace preview bar. It looks like:
`https://xxxx-xxxx.janeway.replit.dev`

The `/health` endpoint returns `{"status": "ok", ...}` which UptimeRobot treats as a successful ping.

## Binance API Fallback

The bot automatically cycles through these global Binance hosts if the primary is geo-blocked:

1. `api1.binance.com`
2. `api2.binance.com`
3. `api3.binance.com`
4. `api4.binance.com`
5. `data-api.binance.vision` ← typically works from Replit

## Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Status, tracked pairs, silence state, last run summary |
| `POST` | `/run-now` | Trigger an immediate check cycle |
| `POST` | `/telegram-update` | Telegram webhook receiver (registered automatically) |

## Environment secrets

| Secret | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | `890707423` |

## Running

```bash
cd telegram-webhook-bot
gunicorn --bind 0.0.0.0:5000 app:app --log-level info
```
