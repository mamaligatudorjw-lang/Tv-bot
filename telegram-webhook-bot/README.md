# Binance Monitor → Telegram Bot

Polls Binance every 5 minutes and sends Telegram alerts for three signal types across all USDT spot pairs.

## Alerts

| Signal | Trigger |
|---|---|
| 🆕 New Listing | A new USDT pair appears on Binance Spot |
| 📈 New 24h High | A coin sets a new 24h high between checks |
| 🔥 RSI Overbought | RSI (14, 1h candles) rises above 70 |

## How it works

1. On startup, loads all USDT pairs from Binance and sends a confirmation message to Telegram
2. Every 5 minutes:
   - Fetches `/api/v3/exchangeInfo` to detect new listings
   - Fetches `/api/v3/ticker/24hr` for all pairs to detect new 24h highs
   - Fetches 1h klines for all pairs in parallel to compute RSI
3. RSI and 24h high alerts have a 1-hour cooldown per coin to avoid spam

## Environment secrets

| Secret | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | `890707423` |

## Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Status, tracked pairs, last run summary |
| `POST` | `/run-now` | Trigger an immediate check cycle |

## Running

```bash
cd telegram-webhook-bot
gunicorn --bind 0.0.0.0:5000 app:app --log-level info
```
