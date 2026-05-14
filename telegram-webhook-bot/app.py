import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"


def send_telegram_message(text: str) -> dict:
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }
    response = requests.post(TELEGRAM_API_URL, json=payload, timeout=10)
    response.raise_for_status()
    return response.json()


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}

    message = data.get("message") or data.get("text") or ""

    if not message:
        raw = request.get_data(as_text=True)
        message = raw.strip()

    if not message:
        return jsonify({"error": "No message found in request"}), 400

    try:
        result = send_telegram_message(message)
        return jsonify({"ok": True, "telegram_response": result}), 200
    except requests.exceptions.HTTPError as e:
        return jsonify({"error": "Telegram API error", "detail": str(e)}), 502
    except requests.exceptions.RequestException as e:
        return jsonify({"error": "Failed to reach Telegram", "detail": str(e)}), 503


@app.route("/health", methods=["GET"])
def health():
    token_set = bool(TELEGRAM_BOT_TOKEN)
    chat_id_set = bool(TELEGRAM_CHAT_ID)
    return jsonify({
        "status": "ok",
        "telegram_bot_token_set": token_set,
        "telegram_chat_id_set": chat_id_set,
    }), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
