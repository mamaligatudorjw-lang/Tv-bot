# TradingView → Telegram Webhook Bot

A lightweight Flask webhook server that receives POST alerts from TradingView and forwards them to a Telegram chat.

## How it works

1. TradingView fires a webhook POST to your `/webhook` endpoint
2. The Flask app reads the message from the request body
3. It sends the message to your Telegram chat via the Bot API

## Setup

### Required environment secrets

| Secret | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Your bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Numeric ID of the target chat/channel |

### TradingView Alert Configuration

In TradingView, when creating an alert:

- **Webhook URL**: `https://<your-replit-domain>/webhook`
- **Message**: Your alert text, e.g.:
  ```
  {{ticker}} — {{strategy.order.action}} at {{close}}
  ```
- The message can be plain text or a JSON body with a `"message"` or `"text"` key.

### Accepted request formats

**Plain text body:**
```
POST /webhook
Content-Type: text/plain

BTCUSDT BUY signal at 65000
```

**JSON body:**
```json
POST /webhook
Content-Type: application/json

{"message": "BTCUSDT BUY signal at 65000"}
```
or
```json
{"text": "BTCUSDT BUY signal at 65000"}
```

## Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/webhook` | Receive TradingView alert, forward to Telegram |
| `GET` | `/health` | Health check — confirms secrets are set |

## Running locally

```bash
cd telegram-webhook-bot
python app.py
```

Or with gunicorn (production):
```bash
gunicorn --bind 0.0.0.0:5000 app:app
```
