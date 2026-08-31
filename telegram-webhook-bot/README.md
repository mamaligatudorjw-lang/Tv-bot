# Binance Monitor → Telegram Bot

Polls the Gate.io Futures market every 5 minutes and sends Telegram alerts for the configured signal types across USDT perpetual pairs.

## Price timing and outcome reports

Outcome reports classify the result from the recorded `entry_price`, which is the bot's paper-entry basis. During a delayed polling cycle, that recorded basis may differ from the price a human could see when acting on the Telegram alert. Snapshot-to-delivery price risk is measured separately by the polling telemetry and is not retroactively applied to historical outcomes.

## Polling deadline

The polling cycle has a 240-second operational ceiling. Strategy calls run in
daemonized bounded workers using the remaining cycle budget, so a slow network
request cannot keep the scheduler thread blocked past the deadline. A timed-out
worker may finish its own HTTP client's bounded timeout in the background, but
the cycle is released immediately and does not start later strategies.

Deadline exits write one structured `Cycle aborted` line containing the cycle
ID, duration, reason, stage, and ordered skipped-strategy list. Generate a
warmed-cycle report with:

```bash
python3 polling_deadline_report.py --limit 15 --out polling_deadline_report
```

`overheated_early` and `overheated_24h` are branches nested inside the
`overheated_oversold` strategy and are explicitly reported as nested rather
than given misleading independent timings.

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
| `GET` | `/bot-api/status` | Authenticated operational status and Bybit Demo diagnostics |
| `GET` | `/bot-api/bybit-demo-status` | Authenticated Bybit Demo ledger and health snapshot |
| `POST` | `/run-now` | Trigger an immediate check cycle |
| `POST` | `/telegram-update` | Telegram webhook receiver (registered automatically) |

`/bot-api/status` returns `last_signal_at`, `last_successful_poll_at`,
`polling_stale`, and `polling_stale_after_sec` (120 seconds), plus an
`active_whitelist` array of exactly three slots and an `open_positions` array
containing only `strategy`, `symbol`, and `opened_at`. The third whitelist slot
also reports whether `overheated_early` is `promoted` or `not_promoted`.
Missing poll telemetry is treated as stale.

The two operational endpoints require the `X-Status-Token` header matching the
`STATUS_API_TOKEN` environment secret. Tokens are accepted only in the header
so they do not appear in request URLs or access logs. `/health` remains
unauthenticated for uptime monitoring.

## Environment secrets

| Secret | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | `890707423` |
| `STATUS_API_TOKEN` | Dedicated token for operational status endpoints |

## Running

```bash
cd telegram-webhook-bot
gunicorn --bind 0.0.0.0:5000 app:app --log-level info
```
