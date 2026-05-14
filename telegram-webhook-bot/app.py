import os
import time
import logging
import threading
import numpy as np
import requests
from flask import Flask, jsonify, request
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

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------
state_lock = threading.Lock()
state = {
    "known_pairs": set(),              # set of USDT symbol strings
    "previous_highs": {},              # symbol -> float (24h high from last check)
    "weekly_highs": {},                # symbol -> float (7-day high, refreshed hourly)
    "monthly_highs": {},               # symbol -> float (30-day high, refreshed hourly)
    "last_weekly_monthly_refresh": 0,  # unix timestamp of last 7d/30d refresh
    "last_rsi_alerted": {},            # symbol -> timestamp (overbought cooldown)
    "last_rsi_oversold_alerted": {},   # symbol -> timestamp (oversold cooldown)
    "last_high_alerted": {},           # symbol -> timestamp (24h high cooldown)
    "last_weekly_alerted": {},         # symbol -> timestamp
    "last_monthly_alerted": {},        # symbol -> timestamp
    "last_vol_spike_alerted": {},      # symbol -> timestamp
    "initialized": False,
    "last_run": None,
    "last_run_summary": {},
    "silenced": False,
    "silenced_at": None,
}

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
RSI_ALERT_COOLDOWN = 3600
HIGH_ALERT_COOLDOWN = 3600
WEEKLY_HIGH_COOLDOWN = 3600
MONTHLY_HIGH_COOLDOWN = 3600
VOLUME_SPIKE_COOLDOWN = 300        # allow once per 5-min cycle
WEEKLY_MONTHLY_REFRESH_INTERVAL = 3600  # refresh 7d/30d highs hourly

RSI_PERIOD = 14
RSI_OVERBOUGHT = 70.0
RSI_OVERSOLD = 30.0
VOLUME_SPIKE_MULTIPLIER = 3.0
MAX_WORKERS = 20
MIN_VOLUME_USDT = 50_000


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

def send_telegram(text: str) -> bool:
    with state_lock:
        if state["silenced"]:
            logger.info("Alert suppressed (silenced): %s", text[:60])
            return False
    return _telegram_send(TELEGRAM_CHAT_ID, text)


def _telegram_send(chat_id: str | int, text: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.error("Telegram send failed: %s", e)
        return False


def register_telegram_webhook() -> None:
    domain = (os.environ.get("REPLIT_DOMAINS", "").split(",")[0] or "").strip()
    if not domain:
        logger.warning("REPLIT_DOMAINS not set — skipping webhook registration")
        return
    webhook_url = f"https://{domain}/telegram-update"
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook",
            json={"url": webhook_url, "allowed_updates": ["message"]},
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("Telegram webhook registered: %s", webhook_url)
    except Exception as e:
        logger.error("Failed to register Telegram webhook: %s", e)


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
        return _calculate_rsi(closes, RSI_PERIOD)
    except Exception as e:
        logger.warning("RSI kline fetch failed for %s: %s", symbol, e)
        return None


def get_volume_spike_ratio(symbol: str) -> float | None:
    """Return ratio of last complete 5m candle base-volume vs 12-candle avg."""
    try:
        resp = _binance_get(
            "/api/v3/klines",
            params={"symbol": symbol, "interval": "5m", "limit": 14},
            timeout=10,
        )
        candles = resp.json()
        if len(candles) < 13:
            return None
        volumes = [float(k[5]) for k in candles]
        # candles[-1] may be incomplete; use candles[-2] as latest complete
        last_vol = volumes[-2]
        avg_vol = float(np.mean(volumes[:-2]))  # average of all complete candles
        if avg_vol == 0:
            return None
        return last_vol / avg_vol
    except Exception as e:
        logger.warning("Spike kline fetch failed for %s: %s", symbol, e)
        return None


def get_daily_highs(symbol: str, limit: int = 31) -> list[float] | None:
    """Return list of daily high prices (oldest first) for the last `limit` days."""
    try:
        resp = _binance_get(
            "/api/v3/klines",
            params={"symbol": symbol, "interval": "1d", "limit": limit},
            timeout=10,
        )
        return [float(k[2]) for k in resp.json()]
    except Exception as e:
        logger.warning("Daily klines failed for %s: %s", symbol, e)
        return None


def get_two_day_volumes(symbol: str) -> tuple[float, float] | None:
    """Return (yesterday_usdt_vol, today_usdt_vol) from the last 2 daily candles."""
    try:
        resp = _binance_get(
            "/api/v3/klines",
            params={"symbol": symbol, "interval": "1d", "limit": 2},
            timeout=10,
        )
        candles = resp.json()
        if len(candles) < 2:
            return None
        # k[7] = quoteAssetVolume (USDT volume for USDT pairs)
        return float(candles[0][7]), float(candles[1][7])
    except Exception as e:
        logger.warning("2-day volume fetch failed for %s: %s", symbol, e)
        return None


def _calculate_rsi(closes: list[float], period: int) -> float:
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
                prev_highs[symbol] = current_high

                if not state["initialized"] or prev_high is None:
                    continue
                if volume_usdt < MIN_VOLUME_USDT:
                    continue
                if current_high > prev_high:
                    last_sent = last_alerted.get(symbol, 0)
                    if now - last_sent >= HIGH_ALERT_COOLDOWN:
                        alerts.append((symbol, current_high, last_price, volume_usdt))
                        last_alerted[symbol] = now
            except (ValueError, KeyError):
                continue
    return alerts


def check_weekly_monthly_highs(
    tickers: dict[str, dict],
    liquid_vol_map: dict[str, float],
) -> tuple[list[tuple], list[tuple]]:
    """Return (weekly_alerts, monthly_alerts) where each item is (symbol, high, price, vol)."""
    weekly, monthly = [], []
    now = time.time()
    with state_lock:
        weekly_highs = state["weekly_highs"]
        monthly_highs = state["monthly_highs"]
        last_w = state["last_weekly_alerted"]
        last_m = state["last_monthly_alerted"]

        for symbol, vol in liquid_vol_map.items():
            t = tickers.get(symbol)
            if not t:
                continue
            try:
                price = float(t["lastPrice"])
                w_high = weekly_highs.get(symbol)
                m_high = monthly_highs.get(symbol)

                if w_high and price > w_high:
                    if now - last_w.get(symbol, 0) >= WEEKLY_HIGH_COOLDOWN:
                        weekly.append((symbol, w_high, price, vol))
                        last_w[symbol] = now

                if m_high and price > m_high:
                    if now - last_m.get(symbol, 0) >= MONTHLY_HIGH_COOLDOWN:
                        monthly.append((symbol, m_high, price, vol))
                        last_m[symbol] = now
            except (ValueError, KeyError):
                continue
    return weekly, monthly


def refresh_weekly_monthly_highs(liquid_symbols: list[str]) -> int:
    """Fetch 31 daily klines per liquid symbol and store 7d/30d highs. Returns count updated."""
    def fetch(symbol):
        highs = get_daily_highs(symbol, limit=31)
        return symbol, highs

    updated = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(fetch, s): s for s in liquid_symbols}
        for future in as_completed(futures):
            symbol, highs = future.result()
            if not highs:
                continue
            weekly_high = max(highs[-7:]) if len(highs) >= 7 else max(highs)
            monthly_high = max(highs)
            with state_lock:
                state["weekly_highs"][symbol] = weekly_high
                state["monthly_highs"][symbol] = monthly_high
            updated += 1

    with state_lock:
        state["last_weekly_monthly_refresh"] = time.time()
    logger.info("Refreshed 7d/30d highs for %d symbols", updated)
    return updated


def check_rsi_and_spikes(
    symbols_volumes: list[tuple[str, float]],
) -> tuple[
    list[tuple[str, float, float]],  # overbought (symbol, rsi, vol)
    list[tuple[str, float, float]],  # oversold
    list[tuple[str, float, float]],  # volume spikes (symbol, ratio, vol)
]:
    overbought, oversold, spikes = [], [], []
    now = time.time()
    volume_map = dict(symbols_volumes)

    def fetch(symbol):
        rsi = get_klines_rsi(symbol)
        spike = get_volume_spike_ratio(symbol)
        return symbol, rsi, spike

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(fetch, s): s for s, _ in symbols_volumes}
        for future in as_completed(futures):
            symbol, rsi, spike_ratio = future.result()
            vol = volume_map[symbol]

            with state_lock:
                # RSI overbought
                if rsi is not None and rsi >= RSI_OVERBOUGHT:
                    if now - state["last_rsi_alerted"].get(symbol, 0) >= RSI_ALERT_COOLDOWN:
                        overbought.append((symbol, rsi, vol))
                        state["last_rsi_alerted"][symbol] = now

                # RSI oversold
                elif rsi is not None and rsi <= RSI_OVERSOLD:
                    if now - state["last_rsi_oversold_alerted"].get(symbol, 0) >= RSI_ALERT_COOLDOWN:
                        oversold.append((symbol, rsi, vol))
                        state["last_rsi_oversold_alerted"][symbol] = now

                # Volume spike
                if spike_ratio is not None and spike_ratio >= VOLUME_SPIKE_MULTIPLIER:
                    if now - state["last_vol_spike_alerted"].get(symbol, 0) >= VOLUME_SPIKE_COOLDOWN:
                        spikes.append((symbol, spike_ratio, vol))
                        state["last_vol_spike_alerted"][symbol] = now

    return (
        sorted(overbought, key=lambda x: x[1], reverse=True),
        sorted(oversold, key=lambda x: x[1]),
        sorted(spikes, key=lambda x: x[1], reverse=True),
    )


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

def run_checks():
    logger.info("Starting Binance check cycle...")
    start = time.time()
    summary = {
        "new_listings": 0, "high_breaks": 0,
        "rsi_overbought": 0, "rsi_oversold": 0,
        "vol_spikes": 0, "weekly_highs": 0, "monthly_highs": 0,
        "errors": [],
    }

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
                send_telegram(
                    f"<b>🆕 NEW BINANCE LISTING</b>\n"
                    f"Symbol: <code>{symbol}</code>\n"
                    f"New USDT pair just went live on Binance Spot!"
                )
                logger.info("New listing alert: %s", symbol)
            summary["new_listings"] = len(new_listings)

            # 3. 24h ticker data
            try:
                tickers = get_24h_tickers(list(current_pairs))
            except Exception as e:
                logger.error("Failed to fetch tickers: %s", e)
                summary["errors"].append(f"Tickers fetch: {e}")
                tickers = {}

            # 4. Build volume-filtered map once — shared by all checks
            liquid_vol_map: dict[str, float] = {}
            if tickers:
                for sym, t in tickers.items():
                    try:
                        vol = float(t["quoteVolume"])
                        if vol >= MIN_VOLUME_USDT:
                            liquid_vol_map[sym] = vol
                    except (ValueError, KeyError):
                        continue
                summary["liquid_pairs"] = len(liquid_vol_map)
                logger.info(
                    "%d / %d pairs pass volume filter ($%s+ 24h USDT vol)",
                    len(liquid_vol_map), len(tickers), f"{MIN_VOLUME_USDT:,.0f}",
                )

            liquid_pairs = list(liquid_vol_map.items())

            # 5. 24h high breaks
            if tickers:
                for symbol, high, price, vol in check_24h_highs(tickers):
                    send_telegram(
                        f"<b>📈 NEW 24H HIGH</b>\n"
                        f"Symbol: <code>{symbol}</code>\n"
                        f"New 24h High: <b>${high:,.6g}</b>\n"
                        f"Current Price: ${price:,.6g}\n"
                        f"24h Volume: ${vol:,.0f}"
                    )
                    logger.info("24h high: %s high=%.6g", symbol, high)
                    summary["high_breaks"] += 1

            with state_lock:
                initialized = state["initialized"]

            if initialized and liquid_pairs:
                # 6. RSI + volume spike (combined parallel fetch)
                overbought, oversold, vol_spikes = check_rsi_and_spikes(liquid_pairs)

                for symbol, rsi, vol in overbought:
                    send_telegram(
                        f"<b>🔥 RSI OVERBOUGHT (1H)</b>\n"
                        f"Symbol: <code>{symbol}</code>\n"
                        f"RSI: <b>{rsi:.1f}</b> ≥ {RSI_OVERBOUGHT}\n"
                        f"24h Volume: ${vol:,.0f}\n"
                        f"Possible short-term overheating."
                    )
                    logger.info("RSI overbought: %s rsi=%.1f", symbol, rsi)
                summary["rsi_overbought"] = len(overbought)

                for symbol, rsi, vol in oversold:
                    send_telegram(
                        f"<b>🧊 RSI OVERSOLD (1H)</b>\n"
                        f"Symbol: <code>{symbol}</code>\n"
                        f"RSI: <b>{rsi:.1f}</b> ≤ {RSI_OVERSOLD}\n"
                        f"24h Volume: ${vol:,.0f}\n"
                        f"Possible short-term bottom / reversal zone."
                    )
                    logger.info("RSI oversold: %s rsi=%.1f", symbol, rsi)
                summary["rsi_oversold"] = len(oversold)

                for symbol, ratio, vol in vol_spikes:
                    send_telegram(
                        f"<b>🚀 VOLUME SPIKE</b>\n"
                        f"Symbol: <code>{symbol}</code>\n"
                        f"5m Volume: <b>{ratio:.1f}×</b> above hourly average\n"
                        f"24h Volume: ${vol:,.0f}\n"
                        f"Unusual buying/selling activity detected."
                    )
                    logger.info("Volume spike: %s ratio=%.1fx", symbol, ratio)
                summary["vol_spikes"] = len(vol_spikes)

                # 7. Weekly / monthly highs
                # Refresh stored 7d/30d highs once per hour
                with state_lock:
                    needs_refresh = (
                        time.time() - state["last_weekly_monthly_refresh"]
                        >= WEEKLY_MONTHLY_REFRESH_INTERVAL
                    )

                if needs_refresh:
                    logger.info("Refreshing 7d/30d highs for %d liquid pairs...", len(liquid_pairs))
                    try:
                        refresh_weekly_monthly_highs([s for s, _ in liquid_pairs])
                    except Exception as e:
                        logger.error("Weekly/monthly refresh failed: %s", e)
                        summary["errors"].append(f"WM refresh: {e}")

                if tickers:
                    weekly_alerts, monthly_alerts = check_weekly_monthly_highs(tickers, liquid_vol_map)

                    for symbol, prev_high, price, vol in weekly_alerts:
                        send_telegram(
                            f"<b>📊 NEW 7-DAY HIGH</b>\n"
                            f"Symbol: <code>{symbol}</code>\n"
                            f"Price: <b>${price:,.6g}</b>\n"
                            f"Previous 7d High: ${prev_high:,.6g}\n"
                            f"24h Volume: ${vol:,.0f}"
                        )
                        logger.info("7d high: %s price=%.6g", symbol, price)
                    summary["weekly_highs"] = len(weekly_alerts)

                    for symbol, prev_high, price, vol in monthly_alerts:
                        send_telegram(
                            f"<b>📊 NEW 30-DAY HIGH</b>\n"
                            f"Symbol: <code>{symbol}</code>\n"
                            f"Price: <b>${price:,.6g}</b>\n"
                            f"Previous 30d High: ${prev_high:,.6g}\n"
                            f"24h Volume: ${vol:,.0f}"
                        )
                        logger.info("30d high: %s price=%.6g", symbol, price)
                    summary["monthly_highs"] = len(monthly_alerts)

        # Mark as initialized after first successful run
        with state_lock:
            if not state["initialized"] and current_pairs:
                state["initialized"] = True
                liquid_count = len(liquid_vol_map) if tickers else 0
                logger.info("Initialization complete. Tracking %d USDT pairs.", len(current_pairs))
                send_telegram(
                    f"<b>✅ Binance Monitor Online</b>\n"
                    f"Tracking <b>{len(current_pairs)}</b> USDT pairs.\n"
                    f"Liquid pairs (&gt;${MIN_VOLUME_USDT:,.0f} 24h vol): <b>{liquid_count}</b>\n"
                    f"Checking every 5 minutes for:\n"
                    f"• 🆕 New coin listings (all pairs)\n"
                    f"• 📈 New 24h high\n"
                    f"• 📊 New 7d / 30d high\n"
                    f"• 🚀 Volume spike ≥ {VOLUME_SPIKE_MULTIPLIER}× avg\n"
                    f"• 🔥 RSI ≥ {RSI_OVERBOUGHT} overbought (1h)\n"
                    f"• 🧊 RSI ≤ {RSI_OVERSOLD} oversold (1h)"
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
# Telegram command handlers
# ---------------------------------------------------------------------------

def handle_status_command(chat_id: int) -> None:
    with state_lock:
        initialized = state["initialized"]
        tracked = len(state["known_pairs"])
        last_run = state["last_run"] or "not yet"
        summary = state["last_run_summary"]
        active_host = BINANCE_BASE
        silenced = state["silenced"]
        silenced_at = state["silenced_at"]

    if not initialized:
        _telegram_send(chat_id, "<b>⏳ Bot is still initializing...</b>\nCheck back in a moment.")
        return

    liquid = summary.get("liquid_pairs", "—")
    elapsed = summary.get("elapsed_seconds", "—")
    errors = summary.get("errors", [])
    error_line = f"\n⚠️ Errors: {len(errors)}" if errors else ""
    silence_line = f"\n🔕 <b>Alerts silenced</b> since {silenced_at}" if silenced else "\n🔔 Alerts active"

    msg = (
        f"<b>📊 Binance Monitor Status</b>"
        f"{silence_line}\n\n"
        f"<b>Pairs tracked:</b> {tracked} USDT\n"
        f"<b>Liquid pairs:</b> {liquid} (≥${MIN_VOLUME_USDT:,.0f} vol)\n"
        f"<b>Active host:</b> <code>{active_host}</code>\n\n"
        f"<b>Last run:</b> {last_run}\n"
        f"<b>Cycle time:</b> {elapsed}s\n\n"
        f"<b>Last cycle alerts:</b>\n"
        f"  🆕 New listings: {summary.get('new_listings', 0)}\n"
        f"  📈 24h high breaks: {summary.get('high_breaks', 0)}\n"
        f"  📊 7d highs: {summary.get('weekly_highs', 0)}  |  "
        f"30d highs: {summary.get('monthly_highs', 0)}\n"
        f"  🚀 Volume spikes: {summary.get('vol_spikes', 0)}\n"
        f"  🔥 RSI overbought: {summary.get('rsi_overbought', 0)}\n"
        f"  🧊 RSI oversold: {summary.get('rsi_oversold', 0)}"
        f"{error_line}\n\n"
        f"<b>Thresholds:</b>\n"
        f"  Volume min: ${MIN_VOLUME_USDT:,.0f}\n"
        f"  Volume spike: ≥ {VOLUME_SPIKE_MULTIPLIER}× avg (5m)\n"
        f"  RSI overbought: ≥ {RSI_OVERBOUGHT}\n"
        f"  RSI oversold: ≤ {RSI_OVERSOLD}\n"
        f"  Check interval: 5 min\n\n"
        f"<b>Commands:</b> /status · /top10 · /silence · /unmute"
    )
    _telegram_send(chat_id, msg)


def handle_top10_command(chat_id: int) -> None:
    _telegram_send(chat_id, "⏳ Fetching volume data for all liquid pairs...")

    with state_lock:
        known = list(state["known_pairs"])

    # Get current 24h tickers to find liquid pairs
    try:
        tickers = get_24h_tickers(known)
    except Exception as e:
        _telegram_send(chat_id, f"❌ Failed to fetch ticker data: {e}")
        return

    liquid_symbols = [
        sym for sym, t in tickers.items()
        if float(t.get("quoteVolume", 0)) >= MIN_VOLUME_USDT
    ]

    if not liquid_symbols:
        _telegram_send(chat_id, "❌ No liquid pairs found.")
        return

    # Fetch 2-day volumes in parallel
    results: list[tuple[str, float, float, float]] = []  # (symbol, yesterday, today, pct_change)

    def fetch(sym):
        return sym, get_two_day_volumes(sym)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(fetch, s): s for s in liquid_symbols}
        for future in as_completed(futures):
            sym, vols = future.result()
            if vols is None:
                continue
            yesterday, today = vols
            if yesterday > 0:
                pct = (today - yesterday) / yesterday * 100
                results.append((sym, yesterday, today, pct))

    if not results:
        _telegram_send(chat_id, "❌ Could not retrieve volume history.")
        return

    top10 = sorted(results, key=lambda x: x[3], reverse=True)[:10]

    lines = ["<b>🏆 Top 10 by Volume Change (vs Yesterday)</b>\n"]
    for i, (sym, yesterday, today, pct) in enumerate(top10, 1):
        sign = "+" if pct >= 0 else ""
        lines.append(
            f"{i}. <code>{sym}</code>  {sign}{pct:.1f}%\n"
            f"   Today: ${today:,.0f}  |  Yesterday: ${yesterday:,.0f}"
        )

    _telegram_send(chat_id, "\n".join(lines))


def handle_silence_command(chat_id: int) -> None:
    now_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with state_lock:
        state["silenced"] = True
        state["silenced_at"] = now_str
    logger.info("Alerts silenced via /silence from chat_id=%s", chat_id)
    _telegram_send(chat_id, (
        "<b>🔕 Alerts silenced</b>\n"
        "Market alerts are now paused.\n"
        "Send /unmute to resume them."
    ))


def handle_unmute_command(chat_id: int) -> None:
    with state_lock:
        state["silenced"] = False
        state["silenced_at"] = None
    logger.info("Alerts unmuted via /unmute from chat_id=%s", chat_id)
    _telegram_send(chat_id, (
        "<b>🔔 Alerts resumed</b>\n"
        "Market alerts are active again."
    ))


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
            "silenced": state["silenced"],
            "silenced_at": state["silenced_at"],
            "last_run": state["last_run"],
            "last_run_summary": state["last_run_summary"],
            "active_binance_host": BINANCE_BASE,
            "telegram_bot_token_set": bool(TELEGRAM_BOT_TOKEN),
            "telegram_chat_id": TELEGRAM_CHAT_ID,
        })


@app.route("/run-now", methods=["POST"])
def trigger_run():
    thread = threading.Thread(target=run_checks, daemon=True)
    thread.start()
    return jsonify({"ok": True, "message": "Check cycle triggered"}), 202


@app.route("/telegram-update", methods=["POST"])
def telegram_update():
    update = request.get_json(silent=True) or {}
    message = update.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    text = (message.get("text") or "").strip().lower()

    if not chat_id:
        return jsonify({"ok": True}), 200

    COMMANDS = {
        "/status":  handle_status_command,
        "/top10":   handle_top10_command,
        "/silence": handle_silence_command,
        "/unmute":  handle_unmute_command,
    }
    for cmd, handler in COMMANDS.items():
        if text.startswith(cmd):
            threading.Thread(target=handler, args=(chat_id,), daemon=True).start()
            break

    return jsonify({"ok": True}), 200


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

scheduler = BackgroundScheduler(timezone="UTC")
scheduler.add_job(
    run_checks, "interval", minutes=5, id="binance_check",
    next_run_time=__import__("datetime").datetime.utcnow(),
)
scheduler.start()

register_telegram_webhook()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
