import os
import time
import logging
import threading
import numpy as np
import requests
from flask import Flask, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from concurrent.futures import ThreadPoolExecutor, as_completed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "890707423")
BINANCE_BASE = "https://api.binance.us"

# --- In-memory state ---
state_lock = threading.Lock()
state = {
    "known_pairs": set(),         # set of USDT symbol strings
    "previous_highs": {},         # symbol -> float (24h high from last check)
    "last_rsi_alerted": {},       # symbol -> timestamp (avoid spam)
    "last_high_alerted": {},      # symbol -> timestamp
    "initialized": False,
    "last_run": None,
    "last_run_summary": {},
}

RSI_ALERT_COOLDOWN = 3600        # re-alert RSI max once per hour per coin
HIGH_ALERT_COOLDOWN = 3600       # re-alert new 24h high max once per hour
RSI_PERIOD = 14
RSI_THRESHOLD = 70.0
MAX_WORKERS = 20                 # parallel kline fetches


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(text: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.error("Telegram send failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Binance helpers
# ---------------------------------------------------------------------------

def get_all_usdt_pairs() -> list[str]:
    resp = requests.get(f"{BINANCE_BASE}/api/v3/exchangeInfo", timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return [
        s["symbol"]
        for s in data["symbols"]
        if s["symbol"].endswith("USDT")
        and s["status"] == "TRADING"
        and s["isSpotTradingAllowed"]
    ]


def get_24h_tickers(symbols: list[str]) -> dict[str, dict]:
    resp = requests.get(f"{BINANCE_BASE}/api/v3/ticker/24hr", timeout=20)
    resp.raise_for_status()
    all_tickers = resp.json()
    symbol_set = set(symbols)
    return {t["symbol"]: t for t in all_tickers if t["symbol"] in symbol_set}


def get_klines_rsi(symbol: str) -> float | None:
    try:
        resp = requests.get(
            f"{BINANCE_BASE}/api/v3/klines",
            params={"symbol": symbol, "interval": "1h", "limit": RSI_PERIOD + 1},
            timeout=10,
        )
        resp.raise_for_status()
        closes = [float(k[4]) for k in resp.json()]
        if len(closes) < RSI_PERIOD + 1:
            return None
        return calculate_rsi(closes, RSI_PERIOD)
    except Exception as e:
        logger.warning("Kline fetch failed for %s: %s", symbol, e)
        return None


def calculate_rsi(closes: list[float], period: int) -> float:
    closes = np.array(closes)
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# ---------------------------------------------------------------------------
# Alert checks
# ---------------------------------------------------------------------------

def check_new_listings(current_pairs: set[str]) -> list[str]:
    new = []
    with state_lock:
        if state["initialized"]:
            new = sorted(current_pairs - state["known_pairs"])
        state["known_pairs"] = current_pairs
    return new


def check_24h_highs(tickers: dict[str, dict]) -> list[tuple[str, float, float]]:
    alerts = []
    now = time.time()
    with state_lock:
        prev_highs = state["previous_highs"]
        last_alerted = state["last_high_alerted"]

        for symbol, t in tickers.items():
            try:
                current_high = float(t["highPrice"])
                last_price = float(t["lastPrice"])
                prev_high = prev_highs.get(symbol)

                # Update stored high
                prev_highs[symbol] = current_high

                if not state["initialized"]:
                    continue

                if prev_high is None:
                    continue

                # Alert when a new 24h high is set AND price is near/at the high
                if current_high > prev_high:
                    last_sent = last_alerted.get(symbol, 0)
                    if now - last_sent >= HIGH_ALERT_COOLDOWN:
                        alerts.append((symbol, current_high, last_price))
                        last_alerted[symbol] = now
            except (ValueError, KeyError):
                continue
    return alerts


def check_rsi(symbols: list[str]) -> list[tuple[str, float]]:
    alerts = []
    now = time.time()

    def fetch(symbol):
        return symbol, get_klines_rsi(symbol)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(fetch, s): s for s in symbols}
        for future in as_completed(futures):
            symbol, rsi = future.result()
            if rsi is not None and rsi >= RSI_THRESHOLD:
                with state_lock:
                    last_sent = state["last_rsi_alerted"].get(symbol, 0)
                    if now - last_sent >= RSI_ALERT_COOLDOWN:
                        alerts.append((symbol, rsi))
                        state["last_rsi_alerted"][symbol] = now

    return sorted(alerts, key=lambda x: x[1], reverse=True)


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

def run_checks():
    logger.info("Starting Binance check cycle...")
    start = time.time()
    summary = {"new_listings": 0, "high_breaks": 0, "rsi_alerts": 0, "errors": []}

    try:
        # 1. Fetch all USDT pairs
        try:
            current_pairs = set(get_all_usdt_pairs())
        except Exception as e:
            logger.error("Failed to fetch pairs: %s", e)
            summary["errors"].append(f"Pairs fetch: {e}")
            current_pairs = None

        if current_pairs:
            # 2. New listings
            new_listings = check_new_listings(current_pairs)
            for symbol in new_listings:
                msg = (
                    f"<b>🆕 NEW BINANCE LISTING</b>\n"
                    f"Symbol: <code>{symbol}</code>\n"
                    f"New USDT pair just went live on Binance Spot!"
                )
                send_telegram(msg)
                logger.info("New listing alert: %s", symbol)
            summary["new_listings"] = len(new_listings)

            # 3. 24h ticker data
            try:
                tickers = get_24h_tickers(list(current_pairs))
            except Exception as e:
                logger.error("Failed to fetch tickers: %s", e)
                summary["errors"].append(f"Tickers fetch: {e}")
                tickers = {}

            # 4. 24h high breaks
            if tickers:
                high_alerts = check_24h_highs(tickers)
                for symbol, high, price in high_alerts:
                    msg = (
                        f"<b>📈 NEW 24H HIGH</b>\n"
                        f"Symbol: <code>{symbol}</code>\n"
                        f"New 24h High: <b>${high:,.6g}</b>\n"
                        f"Current Price: ${price:,.6g}"
                    )
                    send_telegram(msg)
                    logger.info("24h high alert: %s high=%.6g", symbol, high)
                summary["high_breaks"] = len(high_alerts)

            # 5. RSI check — run on all current pairs
            with state_lock:
                initialized = state["initialized"]

            if initialized:
                rsi_alerts = check_rsi(list(current_pairs))
                for symbol, rsi in rsi_alerts:
                    msg = (
                        f"<b>🔥 RSI OVERBOUGHT (1H)</b>\n"
                        f"Symbol: <code>{symbol}</code>\n"
                        f"RSI: <b>{rsi:.1f}</b> (threshold: {RSI_THRESHOLD})\n"
                        f"Possible short-term overheating."
                    )
                    send_telegram(msg)
                    logger.info("RSI alert: %s rsi=%.1f", symbol, rsi)
                summary["rsi_alerts"] = len(rsi_alerts)

        # Mark as initialized after first successful full run
        with state_lock:
            if not state["initialized"] and current_pairs:
                state["initialized"] = True
                logger.info("Initialization complete. Tracking %d USDT pairs.", len(current_pairs))
                send_telegram(
                    f"<b>✅ Binance Monitor Online</b>\n"
                    f"Tracking <b>{len(current_pairs)}</b> USDT pairs.\n"
                    f"Checking every 5 minutes for:\n"
                    f"• New coin listings\n"
                    f"• 24h high breaks\n"
                    f"• RSI &gt; {RSI_THRESHOLD} (1h candles)"
                )

    except Exception as e:
        logger.exception("Unexpected error in check cycle: %s", e)
        summary["errors"].append(str(e))

    elapsed = time.time() - start
    summary["elapsed_seconds"] = round(elapsed, 1)
    logger.info("Check cycle done in %.1fs: %s", elapsed, summary)

    with state_lock:
        state["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        state["last_run_summary"] = summary


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    with state_lock:
        return jsonify({
            "status": "ok",
            "initialized": state["initialized"],
            "tracked_pairs": len(state["known_pairs"]),
            "last_run": state["last_run"],
            "last_run_summary": state["last_run_summary"],
            "telegram_bot_token_set": bool(TELEGRAM_BOT_TOKEN),
            "telegram_chat_id": TELEGRAM_CHAT_ID,
        })


@app.route("/run-now", methods=["POST"])
def trigger_run():
    thread = threading.Thread(target=run_checks, daemon=True)
    thread.start()
    return jsonify({"ok": True, "message": "Check cycle triggered"}), 202


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

scheduler = BackgroundScheduler(timezone="UTC")
scheduler.add_job(run_checks, "interval", minutes=5, id="binance_check", next_run_time=__import__("datetime").datetime.utcnow())
scheduler.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
