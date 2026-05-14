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
state_lock = threading.RLock()  # reentrant: same thread may re-acquire (e.g. send_telegram called from inside a state_lock block)
state = {
    "known_pairs": set(),              # set of USDT symbol strings
    "previous_highs": {},              # symbol -> float (24h high from last check)
    "weekly_highs": {},                # symbol -> float (7-day high, refreshed hourly)
    "monthly_highs": {},               # symbol -> float (30-day high, refreshed hourly)
    "last_weekly_monthly_refresh": 0,  # unix timestamp of last 7d/30d refresh
    "volume_ranking": [],              # list[(symbol, yesterday_vol, today_vol, pct_change)] sorted desc by pct
    "volume_ranking_updated": 0,       # unix timestamp of last volume ranking refresh
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
RSI_ALERT_COOLDOWN = 14400         # 4h cooldown per coin per RSI direction
HIGH_ALERT_COOLDOWN = 3600
CONFLUENCE_MIN_SIGNALS = 2         # only alert when ≥ this many signals fire on same coin in one cycle
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


def _delete_telegram_webhook() -> None:
    """Remove any existing webhook so getUpdates polling works."""
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook",
            json={"drop_pending_updates": True},
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("Telegram webhook deleted — polling mode active")
    except Exception as e:
        logger.error("Failed to delete Telegram webhook: %s", e)


def _poll_telegram_commands() -> None:
    """Long-poll Telegram getUpdates forever; dispatch command handlers."""
    COMMANDS = {
        "/status":  handle_status_command,
        "/top10":   handle_top10_command,
        "/signal":  handle_signal_command,
        "/silence": handle_silence_command,
        "/unmute":  handle_unmute_command,
    }
    offset: int | None = None
    logger.info("Telegram command polling started")
    while True:
        try:
            params: dict = {"timeout": 30, "allowed_updates": ["message"]}
            if offset is not None:
                params["offset"] = offset
            resp = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                params=params,
                timeout=35,
            )
            resp.raise_for_status()
            for update in resp.json().get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message", {})
                chat_id = message.get("chat", {}).get("id")
                text = (message.get("text") or "").strip().lower()
                if not chat_id or not text:
                    continue
                for cmd, handler in COMMANDS.items():
                    if text.startswith(cmd):
                        logger.info("Command %s from chat_id=%s", cmd, chat_id)
                        def _run(h=handler, cid=chat_id, c=cmd):
                            try:
                                h(cid)
                                logger.info("Command %s handler completed", c)
                            except Exception as exc:
                                logger.exception("Handler %s crashed: %s", c, exc)
                        threading.Thread(target=_run, daemon=True).start()
                        break
        except Exception as e:
            logger.error("Polling error: %s — retrying in 5s", e)
            time.sleep(5)


def start_command_polling() -> None:
    _delete_telegram_webhook()
    t = threading.Thread(target=_poll_telegram_commands, daemon=True, name="tg-poll")
    t.start()


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
    data = get_daily_data(symbol, limit=limit)
    return data["highs"] if data else None


def get_daily_data(symbol: str, limit: int = 31) -> dict | None:
    """Return {"highs": [...], "yesterday_vol": float, "today_vol": float} from daily klines.
    Kline tuple index 2 = high, index 7 = quoteAssetVolume (USDT for USDT pairs).
    """
    try:
        resp = _binance_get(
            "/api/v3/klines",
            params={"symbol": symbol, "interval": "1d", "limit": limit},
            timeout=10,
        )
        candles = resp.json()
        if not candles:
            return None
        highs = [float(k[2]) for k in candles]
        yesterday_vol = float(candles[-2][7]) if len(candles) >= 2 else 0.0
        today_vol = float(candles[-1][7])
        return {"highs": highs, "yesterday_vol": yesterday_vol, "today_vol": today_vol}
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
# Recommendation engine
# ---------------------------------------------------------------------------

def _near_24h_high(ticker: dict | None, threshold: float = 0.98) -> bool:
    """True if last price is within (1 - threshold) of the 24h high."""
    if not ticker:
        return False
    try:
        high = float(ticker["highPrice"])
        price = float(ticker["lastPrice"])
        return high > 0 and price >= high * threshold
    except (ValueError, KeyError):
        return False


def make_recommendation(
    *,
    rsi: float | None = None,
    spike_ratio: float | None = None,
    broke_weekly: bool = False,
    broke_monthly: bool = False,
    near_24h_high: bool = False,
) -> tuple[str, str]:
    """Return (rec_line, reason_line) in Russian for the given signal mix.

    Rules:
      LONG     — (7d/30d high break + volume spike ≥ multiplier) OR RSI ≤ oversold
      SHORT    — RSI ≥ overbought AND price near 24h high
      NEUTRAL  — anything else / mixed
    """
    has_break = broke_weekly or broke_monthly
    has_spike = spike_ratio is not None and spike_ratio >= VOLUME_SPIKE_MULTIPLIER
    is_oversold = rsi is not None and rsi <= RSI_OVERSOLD
    is_overbought = rsi is not None and rsi >= RSI_OVERBOUGHT

    long_signal = (has_break and has_spike) or is_oversold
    short_signal = is_overbought and near_24h_high

    if long_signal and not short_signal:
        if has_break and has_spike and is_oversold:
            reason = (
                f"пробой {'30д' if broke_monthly else '7д'} максимума "
                f"со всплеском объёма {spike_ratio:.1f}× и RSI {rsi:.1f} в зоне перепроданности"
            )
        elif has_break and has_spike:
            reason = (
                f"пробой {'30д' if broke_monthly else '7д'} максимума "
                f"со всплеском объёма {spike_ratio:.1f}× — сильный бычий сигнал"
            )
        else:  # oversold only
            reason = (
                f"RSI {rsi:.1f} ≤ {RSI_OVERSOLD} — зона перепроданности, возможен отскок"
            )
        return "📊 Рекомендация: ЛОНГ 📈", f"Причина: {reason}"

    if short_signal and not long_signal:
        return (
            "📊 Рекомендация: ШОРТ 📉",
            f"Причина: RSI {rsi:.1f} ≥ {RSI_OVERBOUGHT} вблизи 24ч максимума — вероятен перегрев",
        )

    # NEUTRAL / mixed — describe the partial signals we do see
    parts = []
    if rsi is not None:
        if is_overbought:
            parts.append(f"RSI {rsi:.1f} перекуплен, но цена не у 24ч максимума")
        elif is_oversold:
            parts.append(f"RSI {rsi:.1f} перепродан")
        else:
            parts.append(f"RSI {rsi:.1f} в нейтральной зоне")
    if has_spike:
        parts.append(f"всплеск объёма {spike_ratio:.1f}×")
    if has_break and not has_spike:
        parts.append(f"пробой {'30д' if broke_monthly else '7д'} максимума без подтверждения объёмом")
    if near_24h_high and not is_overbought:
        parts.append("цена у 24ч максимума")
    reason = "; ".join(parts) if parts else "сигналы смешанные, чёткого направления нет"
    return "📊 Рекомендация: НЕЙТРАЛЬНО ➡️", f"Причина: {reason}"


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
    """Detect 24h-high breaks. Cooldown is *read* to gate eligibility, but NOT
    marked here — the confluence dispatcher in run_checks marks cooldowns only
    for signals that actually contribute to a sent alert."""
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
                        # cooldown marked by confluence dispatcher only on send

                if m_high and price > m_high:
                    if now - last_m.get(symbol, 0) >= MONTHLY_HIGH_COOLDOWN:
                        monthly.append((symbol, m_high, price, vol))
                        # cooldown marked by confluence dispatcher only on send
            except (ValueError, KeyError):
                continue
    return weekly, monthly


def refresh_weekly_monthly_highs(liquid_symbols: list[str]) -> int:
    """Fetch 31 daily klines per liquid symbol; store 7d/30d highs and volume ranking.
    Single Binance pass populates both highs cache and the /top10 volume ranking cache.
    """
    def fetch(symbol):
        return symbol, get_daily_data(symbol, limit=31)

    updated = 0
    ranking: list[tuple[str, float, float, float]] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(fetch, s): s for s in liquid_symbols}
        for future in as_completed(futures):
            symbol, data = future.result()
            if not data:
                continue
            highs = data["highs"]
            weekly_high = max(highs[-7:]) if len(highs) >= 7 else max(highs)
            monthly_high = max(highs)
            with state_lock:
                state["weekly_highs"][symbol] = weekly_high
                state["monthly_highs"][symbol] = monthly_high
            updated += 1

            yest = data["yesterday_vol"]
            today = data["today_vol"]
            if yest > 0:
                pct = (today - yest) / yest * 100
                ranking.append((symbol, yest, today, pct))

    ranking.sort(key=lambda x: x[3], reverse=True)
    with state_lock:
        state["last_weekly_monthly_refresh"] = time.time()
        state["volume_ranking"] = ranking
        state["volume_ranking_updated"] = time.time()
    logger.info(
        "Refreshed 7d/30d highs for %d symbols; volume ranking cached (%d entries)",
        updated, len(ranking),
    )
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
                # Cooldowns are READ to gate eligibility, but NOT marked here.
                # The confluence dispatcher marks them only when an alert is sent.
                if rsi is not None and rsi >= RSI_OVERBOUGHT:
                    if now - state["last_rsi_alerted"].get(symbol, 0) >= RSI_ALERT_COOLDOWN:
                        overbought.append((symbol, rsi, vol))

                elif rsi is not None and rsi <= RSI_OVERSOLD:
                    if now - state["last_rsi_oversold_alerted"].get(symbol, 0) >= RSI_ALERT_COOLDOWN:
                        oversold.append((symbol, rsi, vol))

                if spike_ratio is not None and spike_ratio >= VOLUME_SPIKE_MULTIPLIER:
                    if now - state["last_vol_spike_alerted"].get(symbol, 0) >= VOLUME_SPIKE_COOLDOWN:
                        spikes.append((symbol, spike_ratio, vol))

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
        "confluence_alerts": 0,        # multi-signal alerts actually sent
        "single_signals_skipped": 0,   # coins with only 1 signal — suppressed
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
            # 2. Detect new listings (no individual alert — must combine with another signal)
            new_listings = check_new_listings(current_pairs)
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

            # 5. Detect 24h high breaks (cooldown not yet marked)
            high_24h: list[tuple[str, float, float, float]] = []
            if tickers:
                high_24h = check_24h_highs(tickers)
                summary["high_breaks"] = len(high_24h)

            with state_lock:
                initialized = state["initialized"]

            overbought: list = []
            oversold: list = []
            vol_spikes: list = []
            weekly_alerts: list = []
            monthly_alerts: list = []

            if initialized and liquid_pairs:
                # 6. Detect RSI + volume spike signals
                overbought, oversold, vol_spikes = check_rsi_and_spikes(liquid_pairs)
                summary["rsi_overbought"] = len(overbought)
                summary["rsi_oversold"] = len(oversold)
                summary["vol_spikes"] = len(vol_spikes)

                # 7. Refresh stored 7d/30d highs once per hour
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

                # 8. Detect 7d / 30d high breaks
                if tickers:
                    weekly_alerts, monthly_alerts = check_weekly_monthly_highs(tickers, liquid_vol_map)
                    summary["weekly_highs"] = len(weekly_alerts)
                    summary["monthly_highs"] = len(monthly_alerts)

            # 9. CONFLUENCE DISPATCH — aggregate per symbol, only alert if ≥2 signals
            buckets: dict[str, dict] = {}

            def _bucket(symbol: str) -> dict:
                if symbol not in buckets:
                    t = tickers.get(symbol) if tickers else None
                    price = None
                    quote_vol = None
                    if t:
                        try:
                            price = float(t["lastPrice"])
                            quote_vol = float(t["quoteVolume"])
                        except (ValueError, KeyError):
                            pass
                    buckets[symbol] = {
                        "lines": [],
                        "flags": set(),
                        "rsi": None,
                        "spike_ratio": None,
                        "broke_weekly": False,
                        "broke_monthly": False,
                        "near_24h_high": _near_24h_high(t),
                        "price": price,
                        "volume": quote_vol,
                        "cooldowns": [],  # list of (state_key, symbol) to mark on send
                    }
                return buckets[symbol]

            # 9a. New listings
            for sym in new_listings:
                b = _bucket(sym)
                if "new_listing" not in b["flags"]:
                    b["lines"].append("🆕 Новый листинг на Binance Spot")
                    b["flags"].add("new_listing")

            # 9b. High breaks — count as ONE signal, prefer strongest tier (30d > 7d > 24h)
            high_24h_map = {s: (h, p, v) for s, h, p, v in high_24h}
            weekly_map = {s: (h, p, v) for s, h, p, v in weekly_alerts}
            monthly_map = {s: (h, p, v) for s, h, p, v in monthly_alerts}
            for sym in set(high_24h_map) | set(weekly_map) | set(monthly_map):
                b = _bucket(sym)
                if "high_break" in b["flags"]:
                    continue
                if sym in monthly_map:
                    prev, price, _ = monthly_map[sym]
                    b["lines"].append(f"📊 Пробой 30д максимума: ${price:,.6g} (был ${prev:,.6g})")
                    b["broke_monthly"] = True
                elif sym in weekly_map:
                    prev, price, _ = weekly_map[sym]
                    b["lines"].append(f"📊 Пробой 7д максимума: ${price:,.6g} (был ${prev:,.6g})")
                    b["broke_weekly"] = True
                else:
                    high, _price, _ = high_24h_map[sym]
                    b["lines"].append(f"📈 Новый максимум 24ч: ${high:,.6g}")
                b["flags"].add("high_break")
                b["near_24h_high"] = True
                # When sending, mark all 3 high cooldowns to suppress lower-tier intraday noise
                b["cooldowns"].extend([
                    ("last_high_alerted", sym),
                    ("last_weekly_alerted", sym),
                    ("last_monthly_alerted", sym),
                ])

            # 9c. RSI overbought
            for sym, rsi, _vol in overbought:
                b = _bucket(sym)
                if "rsi_overbought" not in b["flags"]:
                    b["lines"].append(f"🔥 RSI {rsi:.1f} ≥ {RSI_OVERBOUGHT} (перекупленность)")
                    b["flags"].add("rsi_overbought")
                    b["rsi"] = rsi
                    b["cooldowns"].append(("last_rsi_alerted", sym))

            # 9d. RSI oversold
            for sym, rsi, _vol in oversold:
                b = _bucket(sym)
                if "rsi_oversold" not in b["flags"]:
                    b["lines"].append(f"🧊 RSI {rsi:.1f} ≤ {RSI_OVERSOLD} (перепроданность)")
                    b["flags"].add("rsi_oversold")
                    b["rsi"] = rsi
                    b["cooldowns"].append(("last_rsi_oversold_alerted", sym))

            # 9e. Volume spikes
            for sym, ratio, _vol in vol_spikes:
                b = _bucket(sym)
                if "volume_spike" not in b["flags"]:
                    b["lines"].append(f"🚀 Всплеск объёма 5м: {ratio:.1f}× от среднечасового")
                    b["flags"].add("volume_spike")
                    b["spike_ratio"] = ratio
                    b["cooldowns"].append(("last_vol_spike_alerted", sym))

            # 9f. Send confluence alerts (≥ CONFLUENCE_MIN_SIGNALS) and mark cooldowns
            now_ts = time.time()
            for sym, b in buckets.items():
                if len(b["lines"]) < CONFLUENCE_MIN_SIGNALS:
                    summary["single_signals_skipped"] += 1
                    logger.debug("Skipped single-signal coin %s: %s", sym, sorted(b["flags"]))
                    continue

                rec_line, reason_line = make_recommendation(
                    rsi=b["rsi"],
                    spike_ratio=b["spike_ratio"],
                    broke_weekly=b["broke_weekly"],
                    broke_monthly=b["broke_monthly"],
                    near_24h_high=b["near_24h_high"],
                )
                header = [f"<b>🚨 КОНФЛЮЭНЦИЯ СИГНАЛОВ: <code>{sym}</code></b>"]
                if b["price"] is not None:
                    header.append(f"Цена: <b>${b['price']:,.6g}</b>")
                if b["volume"] is not None:
                    header.append(f"Объём за 24ч: ${b['volume']:,.0f}")
                body = "\n".join(header + [
                    "",
                    f"<b>Сработавшие сигналы ({len(b['lines'])}):</b>",
                    *[f"  • {ln}" for ln in b["lines"]],
                    "",
                    rec_line,
                    reason_line,
                ])
                delivered = send_telegram(body)
                if not delivered:
                    # Silenced or Telegram error — do NOT consume cooldowns so signals
                    # remain live for the next cycle.
                    logger.info(
                        "Confluence alert %s NOT delivered (silenced/error); cooldowns preserved",
                        sym,
                    )
                    continue

                logger.info(
                    "Confluence alert %s: %d signals %s",
                    sym, len(b["lines"]), sorted(b["flags"]),
                )
                summary["confluence_alerts"] += 1

                # Mark cooldowns only for signals that actually contributed to a sent alert
                with state_lock:
                    for key, s in b["cooldowns"]:
                        state[key][s] = now_ts

            if summary["single_signals_skipped"]:
                logger.info(
                    "Suppressed %d single-signal coins (need ≥%d to alert)",
                    summary["single_signals_skipped"], CONFLUENCE_MIN_SIGNALS,
                )

        # Mark as initialized after first successful run
        with state_lock:
            if not state["initialized"] and current_pairs:
                state["initialized"] = True
                liquid_count = len(liquid_vol_map) if tickers else 0
                logger.info("Initialization complete. Tracking %d USDT pairs.", len(current_pairs))
                send_telegram(
                    f"<b>✅ Монитор Binance запущен</b>\n"
                    f"Отслеживается <b>{len(current_pairs)}</b> пар USDT.\n"
                    f"Ликвидных пар (&gt;${MIN_VOLUME_USDT:,.0f} объёма за 24ч): <b>{liquid_count}</b>\n\n"
                    f"<b>Правило конфлюэнции:</b> алерт отправляется только когда "
                    f"<b>≥ {CONFLUENCE_MIN_SIGNALS} сигнала</b> срабатывают на одной монете в одном цикле.\n\n"
                    f"Отслеживаемые сигналы:\n"
                    f"• 🆕 Новый листинг\n"
                    f"• 📈 Пробой 24ч / 7д / 30д максимума\n"
                    f"• 🚀 Всплеск объёма ≥ {VOLUME_SPIKE_MULTIPLIER}× средн.\n"
                    f"• 🔥 RSI ≥ {RSI_OVERBOUGHT} перекупленность (1ч, кулдаун 4ч)\n"
                    f"• 🧊 RSI ≤ {RSI_OVERSOLD} перепроданность (1ч, кулдаун 4ч)"
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
        _telegram_send(chat_id, "<b>⏳ Бот ещё инициализируется...</b>\nЗагляните через минуту.")
        return

    liquid = summary.get("liquid_pairs", "—")
    elapsed = summary.get("elapsed_seconds", "—")
    errors = summary.get("errors", [])
    error_line = f"\n⚠️ Ошибок: {len(errors)}" if errors else ""
    silence_line = f"\n🔕 <b>Алерты заглушены</b> с {silenced_at}" if silenced else "\n🔔 Алерты активны"

    msg = (
        f"<b>📊 Статус монитора Binance</b>"
        f"{silence_line}\n\n"
        f"<b>Отслеживается пар:</b> {tracked} USDT\n"
        f"<b>Ликвидных пар:</b> {liquid} (≥${MIN_VOLUME_USDT:,.0f} объёма)\n"
        f"<b>Активный хост:</b> <code>{active_host}</code>\n\n"
        f"<b>Последняя проверка:</b> {last_run}\n"
        f"<b>Время цикла:</b> {elapsed}с\n\n"
        f"<b>За последний цикл:</b>\n"
        f"  🚨 Отправлено конфлюэнция-алертов: <b>{summary.get('confluence_alerts', 0)}</b>\n"
        f"  💤 Подавлено одиночных сигналов: {summary.get('single_signals_skipped', 0)}\n\n"
        f"<b>Обнаружено сигналов (до фильтра):</b>\n"
        f"  🆕 Новые листинги: {summary.get('new_listings', 0)}\n"
        f"  📈 Пробои 24ч максимума: {summary.get('high_breaks', 0)}\n"
        f"  📊 Макс. 7д: {summary.get('weekly_highs', 0)}  |  "
        f"Макс. 30д: {summary.get('monthly_highs', 0)}\n"
        f"  🚀 Всплески объёма: {summary.get('vol_spikes', 0)}\n"
        f"  🔥 RSI перекупленность: {summary.get('rsi_overbought', 0)}\n"
        f"  🧊 RSI перепроданность: {summary.get('rsi_oversold', 0)}"
        f"{error_line}\n\n"
        f"<b>Правила:</b>\n"
        f"  Алерт только при ≥ {CONFLUENCE_MIN_SIGNALS} сигналах на одной монете\n"
        f"  Мин. объём: ${MIN_VOLUME_USDT:,.0f}\n"
        f"  Всплеск объёма: ≥ {VOLUME_SPIKE_MULTIPLIER}× средн. (5м)\n"
        f"  RSI перекупленность: ≥ {RSI_OVERBOUGHT}  |  кулдаун {RSI_ALERT_COOLDOWN // 3600}ч\n"
        f"  RSI перепроданность: ≤ {RSI_OVERSOLD}  |  кулдаун {RSI_ALERT_COOLDOWN // 3600}ч\n"
        f"  Интервал проверки: 5 мин\n\n"
        f"<b>Команды:</b> /status · /top10 · /signal · /silence · /unmute"
    )
    _telegram_send(chat_id, msg)


def handle_top10_command(chat_id: int) -> None:
    with state_lock:
        ranking = list(state["volume_ranking"])
        updated_ts = state["volume_ranking_updated"]

    if not ranking:
        _telegram_send(
            chat_id,
            "⏳ Рейтинг объёмов ещё не готов — бот пополняет кеш. "
            "Попробуйте через минуту.",
        )
        return

    age_min = (time.time() - updated_ts) / 60 if updated_ts else 0
    top10 = ranking[:10]

    lines = [
        f"<b>🏆 Топ-10 по изменению объёма за 24ч (к вчерашнему)</b>",
        f"<i>Обновлено {age_min:.0f} мин назад</i>\n",
    ]
    for i, (sym, yesterday, today, pct) in enumerate(top10, 1):
        sign = "+" if pct >= 0 else ""
        lines.append(
            f"{i}. <code>{sym}</code>  {sign}{pct:.1f}%\n"
            f"   Сегодня: ${today:,.0f}  |  Вчера: ${yesterday:,.0f}"
        )

    _telegram_send(chat_id, "\n".join(lines))


def handle_silence_command(chat_id: int) -> None:
    now_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with state_lock:
        state["silenced"] = True
        state["silenced_at"] = now_str
    logger.info("Alerts silenced via /silence from chat_id=%s", chat_id)
    _telegram_send(chat_id, (
        "<b>🔕 Алерты заглушены</b>\n"
        "Рыночные алерты приостановлены.\n"
        "Отправьте /unmute для их возобновления."
    ))


def handle_signal_command(chat_id: int) -> None:
    """Show current LONG/SHORT/NEUTRAL recommendations for the top-10 symbols
    by day-over-day volume change (the same ranking used by /top10)."""
    with state_lock:
        ranking = list(state["volume_ranking"])
        weekly_highs = dict(state["weekly_highs"])
        monthly_highs = dict(state["monthly_highs"])

    if not ranking:
        _telegram_send(
            chat_id,
            "⏳ Данные для сигналов ещё не готовы — бот пополняет кеш. "
            "Попробуйте через минуту.",
        )
        return

    top10 = ranking[:10]
    symbols = [r[0] for r in top10]

    try:
        tickers = get_24h_tickers(symbols)
    except Exception as e:
        logger.error("Failed to fetch tickers for /signal: %s", e)
        tickers = {}

    def fetch(symbol: str) -> tuple[str, float | None, float | None]:
        return symbol, get_klines_rsi(symbol), get_volume_spike_ratio(symbol)

    signals: dict[str, tuple[float | None, float | None]] = {}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(symbols))) as ex:
        for sym, rsi_v, spike_v in ex.map(fetch, symbols):
            signals[sym] = (rsi_v, spike_v)

    lines = [
        "<b>🎯 Текущие сигналы — топ-10 по росту объёма за 24ч (к вчерашнему)</b>\n",
    ]
    for i, (sym, _yest, _today, _pct) in enumerate(top10, 1):
        rsi_v, spike_v = signals.get(sym, (None, None))
        ticker = tickers.get(sym)
        near_high = _near_24h_high(ticker)

        # Use current price + cached 7d/30d highs to detect active breakouts
        price: float | None = None
        if ticker:
            try:
                price = float(ticker["lastPrice"])
            except (ValueError, KeyError):
                price = None
        w_high = weekly_highs.get(sym)
        m_high = monthly_highs.get(sym)
        broke_weekly = bool(price is not None and w_high and price > w_high)
        broke_monthly = bool(price is not None and m_high and price > m_high)

        rec_line, reason_line = make_recommendation(
            rsi=rsi_v, spike_ratio=spike_v,
            broke_weekly=broke_weekly, broke_monthly=broke_monthly,
            near_24h_high=near_high,
        )
        # Compact display: strip the leading "📊 Рекомендация: " prefix for inline use
        rec_short = rec_line.replace("📊 Рекомендация: ", "")
        rsi_str = f"{rsi_v:.1f}" if rsi_v is not None else "—"
        spike_str = f"{spike_v:.1f}×" if spike_v is not None else "—"
        lines.append(
            f"{i}. <code>{sym}</code> — <b>{rec_short}</b>\n"
            f"   RSI: {rsi_str}  |  Объём 5м: {spike_str}\n"
            f"   {reason_line}"
        )

    _telegram_send(chat_id, "\n".join(lines))


def handle_unmute_command(chat_id: int) -> None:
    with state_lock:
        state["silenced"] = False
        state["silenced_at"] = None
    logger.info("Alerts unmuted via /unmute from chat_id=%s", chat_id)
    _telegram_send(chat_id, (
        "<b>🔔 Алерты возобновлены</b>\n"
        "Рыночные алерты снова активны."
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
    # Commands are handled via long-polling; this endpoint is kept as a no-op
    # fallback in case a webhook is ever re-configured.
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

start_command_polling()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
