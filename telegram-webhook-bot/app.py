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
# Global Binance cluster endpoints — tried in order until one succeeds
BINANCE_HOSTS = [
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://api4.binance.com",
    "https://data-api.binance.vision",
]
BINANCE_BASE = BINANCE_HOSTS[0]  # updated at runtime by _binance_get()

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
MIN_VOLUME_USDT = 50_000         # minimum 24h USDT volume to qualify for RSI/high alerts


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

def _binance_get(path: str, params: dict | None = None, timeout: int = 15):
    """Try each Binance host in turn; persist the last working host."""
    global BINANCE_BASE
    hosts_to_try = [BINANCE_BASE] + [h for h in BINANCE_HOSTS if h != BINANCE_BASE]
    last_err = None
    for host in hosts_to_try:
        try:
            resp = requests.get(f"{host}{path}", params=params, timeout=timeout)
            if resp.status_code == 451:
                logger.warning("Host %s returned 451, trying next...", host)
                last_err = requests.exceptions.HTTPError(f"451 from {host}")
                continue
            resp.raise_for_status()
            if host != BINANCE_BASE:
                logger.info("Switched active Binance host to %s", host)
                BINANCE_BASE = host
            return resp
        except requests.exceptions.RequestException as e:
            logger.warning("Host %s failed: %s", host, e)
            last_err = e
    raise last_err


def get_all_usdt_pairs() -> list[str]:
    resp = _binance_get("/api/v3/exchangeInfo", timeout=15)
    data = resp.json()
    return [
        s["symbol"]
        for s in data["symbols"]
        if s["symbol"].endswith("USDT")
        and s["status"] == "TRADING"
        and s["isSpotTradingAllowed"]
    ]


def get_24h_tickers(symbols: list[str]) -> dict[str, dict]:
    resp = _binance_get("/api/v3/ticker/24hr", timeout=20)
    all_tickers = resp.json()
    symbol_set = set(symbols)
    return {t["symbol"]: t for t in all_tickers if t["symbol"] in symbol_set}


def get_klines_rsi(symbol: str) -> float | None:
    try:
        resp = _binance_get(
            "/api/v3/klines",
            params={"symbol": symbol, "interval": "1h", "limit": RSI_PERIOD + 1},
            timeout=10,
        )
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


def check_24h_highs(tickers: dict[str, dict]) -> list[tuple[str, float, float, float]]:
    alerts = []
    now = time.time()
    with state_lock:
        prev_highs = state["previous_highs"]
        last_alerted = state["last_high_alerted"]

        for symbol, t in tickers.items():
            try:
                current_high = float(t["highPrice"])
                last_price = float(t["lastPrice"])
                volume_usdt = float(t["quoteVolume"])
                prev_high = prev_highs.get(symbol)

                # Update stored high
                prev_highs[symbol] = current_high

                if not state["initialized"]:
                    continue

                if prev_high is None:
                    continue

                # Skip low-liquidity coins
                if volume_usdt < MIN_VOLUME_USDT:
                    continue

                # Alert when a new 24h high is set between checks
                if current_high > prev_high:
                    last_sent = last_alerted.get(symbol, 0)
                    if now - last_sent >= HIGH_ALERT_COOLDOWN:
                        alerts.append((symbol, current_high, last_price, volume_usdt))
                        last_alerted[symbol] = now
            except (ValueError, KeyError):
                continue
    return alerts


def check_rsi(symbols_volumes: list[tuple[str, float]]) -> list[tuple[str, float, float]]:
    alerts = []
    now = time.time()
    volume_map = dict(symbols_volumes)

    def fetch(symbol):
        return symbol, get_klines_rsi(symbol)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(fetch, s): s for s, _ in symbols_volumes}
        for future in as_completed(futures):
            symbol, rsi = future.result()
            if rsi is not None and rsi >= RSI_THRESHOLD:
                with state_lock:
                    last_sent = state["last_rsi_alerted"].get(symbol, 0)
                    if now - last_sent >= RSI_ALERT_COOLDOWN:
                        alerts.append((symbol, rsi, volume_map[symbol]))
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

            # 4. Build volume-filtered list once — shared by high-break + RSI checks
            liquid_pairs: list[tuple[str, float]] = []
            if tickers:
                for sym, t in tickers.items():
                    try:
                        vol = float(t["quoteVolume"])
                        if vol >= MIN_VOLUME_USDT:
                            liquid_pairs.append((sym, vol))
                    except (ValueError, KeyError):
                        continue
                summary["liquid_pairs"] = len(liquid_pairs)
                logger.info(
                    "%d / %d pairs pass volume filter ($%s+ 24h USDT volume)",
                    len(liquid_pairs), len(tickers),
                    f"{MIN_VOLUME_USDT:,.0f}",
                )

            # 5. 24h high breaks (volume filter applied inside check)
            if tickers:
                high_alerts = check_24h_highs(tickers)
                for symbol, high, price, vol in high_alerts:
                    msg = (
                        f"<b>📈 NEW 24H HIGH</b>\n"
                        f"Symbol: <code>{symbol}</code>\n"
                        f"New 24h High: <b>${high:,.6g}</b>\n"
                        f"Current Price: ${price:,.6g}\n"
                        f"24h Volume: ${vol:,.0f}"
                    )
                    send_telegram(msg)
                    logger.info("24h high alert: %s high=%.6g vol=$%.0f", symbol, high, vol)
                summary["high_breaks"] = len(high_alerts)

            # 6. RSI check — only liquid pairs
            with state_lock:
                initialized = state["initialized"]

            if initialized and liquid_pairs:
                rsi_alerts = check_rsi(liquid_pairs)
                for symbol, rsi, vol in rsi_alerts:
                    msg = (
                        f"<b>🔥 RSI OVERBOUGHT (1H)</b>\n"
                        f"Symbol: <code>{symbol}</code>\n"
                        f"RSI: <b>{rsi:.1f}</b> (threshold: {RSI_THRESHOLD})\n"
                        f"24h Volume: ${vol:,.0f}\n"
                        f"Possible short-term overheating."
                    )
                    send_telegram(msg)
                    logger.info("RSI alert: %s rsi=%.1f vol=$%.0f", symbol, rsi, vol)
                summary["rsi_alerts"] = len(rsi_alerts)

        # Mark as initialized after first successful full run
        with state_lock:
            if not state["initialized"] and current_pairs:
                state["initialized"] = True
                liquid_count = sum(
                    1 for t in tickers.values()
                    if float(t.get("quoteVolume", 0)) >= MIN_VOLUME_USDT
                ) if tickers else 0
                logger.info("Initialization complete. Tracking %d USDT pairs.", len(current_pairs))
                send_telegram(
                    f"<b>✅ Binance Monitor Online</b>\n"
                    f"Tracking <b>{len(current_pairs)}</b> USDT pairs.\n"
                    f"Liquid pairs (&gt;${MIN_VOLUME_USDT:,.0f} 24h vol): <b>{liquid_count}</b>\n"
                    f"Checking every 5 minutes for:\n"
                    f"• New coin listings (all pairs)\n"
                    f"• 24h high breaks (liquid pairs only)\n"
                    f"• RSI &gt; {RSI_THRESHOLD} on 1h candles (liquid pairs only)"
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
