import os
import time
import html
import logging
import sqlite3
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

# USDⓈ-M Futures endpoints — used as a fallback for symbols like NVDAUSDT
# that exist on futures but not on spot.
BINANCE_FUTURES_HOSTS = [
    "https://fapi.binance.com",
]
BINANCE_FUTURES_BASE = BINANCE_FUTURES_HOSTS[0]

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------
state_lock = threading.RLock()  # reentrant: same thread may re-acquire (e.g. send_telegram called from inside a state_lock block)
state = {
    "known_pairs": set(),              # set of USDT symbol strings
    "known_coingecko_ids": set(),      # set of coingecko coin ids seen on previous fetch
    "coingecko_initialized": False,    # first run populates the set without alerting
    "last_coingecko_check": 0,         # unix timestamp of last coingecko fetch
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
    "ema200_4h": {},                   # symbol -> float (EMA-200 on 4h, refreshed hourly)
    "last_ema200_refresh": 0,          # unix ts of last EMA-200 refresh
    "last_momentum_alerted": {},       # symbol -> {threshold: ts}
    "last_overheated_alerted": {},     # symbol -> ts (24h +20% & RSI>=70 cooldown)
    "last_oversold_alerted": {},       # symbol -> ts (24h -20% & RSI<=30 cooldown)
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
COINGECKO_CHECK_INTERVAL_MIN = 30  # CoinGecko "upcoming listing" monitor cadence
COINGECKO_MAX_ALERTS_PER_CYCLE = 20  # safety cap if CoinGecko returns an anomalous diff
COINGECKO_LIST_URL = "https://api.coingecko.com/api/v3/coins/list"
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

# Momentum (15-min price change) — visually distinct alerts at each tier
MOMENTUM_THRESHOLDS_UP = (2.0, 3.0, 5.0, 10.0)
MOMENTUM_THRESHOLDS_DOWN = (-2.0, -3.0, -5.0, -10.0)
MOMENTUM_COOLDOWN = 1800           # 30 min per (symbol, threshold-tier)

# Overheated / oversold 24h combo alerts (price + RSI confirmation)
OVERHEATED_24H_PCT = 20.0
OVERSOLD_24H_PCT = -20.0
OVERHEATED_COOLDOWN = 14400        # 4h
OVERSOLD_COOLDOWN = 14400          # 4h

# EMA-200 (4h) trend filter
EMA200_PERIOD = 200
EMA200_FETCH_LIMIT = 250
EMA200_REFRESH_INTERVAL = 3600     # hourly

# Hit-rate tracking
HIT_RATE_DB_PATH = os.path.join(os.path.dirname(__file__) or ".", "alerts.db")
HIT_RATE_INTERVALS = ((900, "15м"), (3600, "1ч"), (14400, "4ч"))
HIT_RATE_WIN_PCT = 1.0             # >=1% move in predicted direction = win
HIT_RATE_RETENTION_DAYS = 7


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
        "/status":   handle_status_command,
        "/top10":    handle_top10_command,
        "/signal":   handle_signal_command,
        "/stats":    handle_stats_command,
        "/trade":    handle_trade_command,
        "/mytrades": handle_mytrades_command,
        "/silence":  handle_silence_command,
        "/unmute":   handle_unmute_command,
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
                raw_text = (message.get("text") or "").strip()
                text = raw_text.lower()
                if not chat_id or not text:
                    continue
                for cmd, handler in COMMANDS.items():
                    # Exact match — the command must be the whole word, optionally
                    # followed by whitespace + args. Prevents '/trader' matching '/trade'.
                    if text == cmd or text.startswith(cmd + " "):
                        logger.info("Command %s from chat_id=%s", cmd, chat_id)
                        # Commands that take args receive the full raw text;
                        # the rest are called with just chat_id (backwards-compat).
                        takes_args = cmd in ("/trade",)
                        def _run(h=handler, cid=chat_id, c=cmd, rt=raw_text, ta=takes_args):
                            try:
                                if ta:
                                    h(cid, rt)
                                else:
                                    h(cid)
                                logger.info("Command %s handler completed", c)
                            except Exception as exc:
                                logger.exception("Handler %s crashed: %s", c, exc)
                        threading.Thread(target=_run, daemon=True).start()
                        break
        except Exception as e:
            logger.error("Polling error: %s — retrying in 5s", e)
            time.sleep(5)


_tg_poll_thread: threading.Thread | None = None


def start_command_polling() -> None:
    global _tg_poll_thread
    _delete_telegram_webhook()
    _tg_poll_thread = threading.Thread(
        target=_poll_telegram_commands, daemon=True, name="tg-poll"
    )
    _tg_poll_thread.start()


# ---------------------------------------------------------------------------
# Watchdog — auto-restart background workers if they die
# ---------------------------------------------------------------------------

WATCHDOG_INTERVAL = 30   # seconds between health checks
WATCHDOG_BACKOFF = 5     # seconds to wait after a restart attempt fails


def _watchdog_loop() -> None:
    """Every WATCHDOG_INTERVAL seconds, verify the Telegram polling thread
    and APScheduler are still alive. If either has died, restart it within
    ~30s of the failure. Gunicorn itself supervises the HTTP worker; this
    watchdog covers the background workers that gunicorn does not manage.
    """
    global _tg_poll_thread
    logger.info("Watchdog started (interval=%ds)", WATCHDOG_INTERVAL)
    while True:
        try:
            # 1. Telegram polling thread
            if _tg_poll_thread is None or not _tg_poll_thread.is_alive():
                logger.error("Watchdog: Telegram polling thread died — restarting")
                try:
                    _tg_poll_thread = threading.Thread(
                        target=_poll_telegram_commands, daemon=True, name="tg-poll"
                    )
                    _tg_poll_thread.start()
                except Exception as e:
                    logger.exception("Watchdog: failed to restart tg-poll: %s", e)
                    time.sleep(WATCHDOG_BACKOFF)

            # 2. APScheduler background thread
            if not scheduler.running:
                logger.error("Watchdog: APScheduler stopped — restarting")
                try:
                    scheduler.start()
                except Exception as e:
                    logger.exception("Watchdog: failed to restart scheduler: %s", e)
                    time.sleep(WATCHDOG_BACKOFF)
        except Exception as e:
            logger.exception("Watchdog loop error: %s", e)
        time.sleep(WATCHDOG_INTERVAL)


def start_watchdog() -> None:
    threading.Thread(target=_watchdog_loop, daemon=True, name="watchdog").start()


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


def _binance_futures_get(path: str, params: dict | None = None, timeout: int = 15):
    """Same shape as _binance_get but targets USDⓈ-M Futures hosts.
    Used as a fallback when a symbol isn't listed on spot (e.g. NVDAUSDT).
    """
    global BINANCE_FUTURES_BASE
    hosts_to_try = [BINANCE_FUTURES_BASE] + [
        h for h in BINANCE_FUTURES_HOSTS if h != BINANCE_FUTURES_BASE
    ]
    last_err = None
    for host in hosts_to_try:
        try:
            resp = requests.get(f"{host}{path}", params=params, timeout=timeout)
            if resp.status_code == 451:
                logger.warning("Futures host %s returned 451, trying next...", host)
                last_err = requests.exceptions.HTTPError(f"451 from {host}")
                continue
            resp.raise_for_status()
            if host != BINANCE_FUTURES_BASE:
                logger.info("Switched active Binance futures host to %s", host)
                BINANCE_FUTURES_BASE = host
            return resp
        except requests.exceptions.RequestException as e:
            logger.warning("Futures host %s failed: %s", host, e)
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


def get_5m_signals(symbol: str) -> tuple[float | None, float | None]:
    """Return (spike_ratio, pct_change_15m) from a single 5m kline fetch.
    - spike_ratio: latest-complete 5m base-volume vs 12-candle avg
    - pct_change_15m: pct change between close of latest-complete candle and
      the candle 3 slots earlier (~15-minute window).
    """
    try:
        resp = _binance_get(
            "/api/v3/klines",
            params={"symbol": symbol, "interval": "5m", "limit": 14},
            timeout=10,
        )
        candles = resp.json()
        if len(candles) < 13:
            return None, None

        # candles[-1] may be incomplete; use candles[-2] as latest complete
        volumes = [float(k[5]) for k in candles]
        last_vol = volumes[-2]
        avg_vol = float(np.mean(volumes[:-2]))
        spike: float | None = (last_vol / avg_vol) if avg_vol > 0 else None

        closes = [float(k[4]) for k in candles]
        price_now = closes[-2]
        price_15m = closes[-5] if len(closes) >= 5 else None
        if price_15m is not None and price_15m > 0:
            pct = (price_now - price_15m) / price_15m * 100.0
        else:
            pct = None
        return spike, pct
    except Exception as e:
        logger.warning("5m signal fetch failed for %s: %s", symbol, e)
        return None, None


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


def _calculate_ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    arr = np.asarray(values, dtype=float)
    ema = float(arr[:period].mean())  # SMA seed
    multiplier = 2.0 / (period + 1)
    for v in arr[period:]:
        ema = (float(v) - ema) * multiplier + ema
    return float(ema)


def get_ema200_4h(symbol: str) -> float | None:
    try:
        resp = _binance_get(
            "/api/v3/klines",
            params={"symbol": symbol, "interval": "4h", "limit": EMA200_FETCH_LIMIT},
            timeout=10,
        )
        closes = [float(k[4]) for k in resp.json()]
        if len(closes) < EMA200_PERIOD:
            return None
        return _calculate_ema(closes, EMA200_PERIOD)
    except Exception as e:
        logger.warning("EMA-200 4h fetch failed for %s: %s", symbol, e)
        return None


# --- Futures fallbacks (used by /trade when a symbol isn't on spot) ---

def get_klines_rsi_futures(symbol: str) -> float | None:
    try:
        resp = _binance_futures_get(
            "/fapi/v1/klines",
            params={"symbol": symbol, "interval": "1h", "limit": RSI_PERIOD + 1},
            timeout=10,
        )
        closes = [float(k[4]) for k in resp.json()]
        if len(closes) < RSI_PERIOD + 1:
            return None
        return _calculate_rsi(closes, RSI_PERIOD)
    except Exception as e:
        logger.warning("Futures RSI kline fetch failed for %s: %s", symbol, e)
        return None


def get_ema200_4h_futures(symbol: str) -> float | None:
    try:
        resp = _binance_futures_get(
            "/fapi/v1/klines",
            params={"symbol": symbol, "interval": "4h", "limit": EMA200_FETCH_LIMIT},
            timeout=10,
        )
        closes = [float(k[4]) for k in resp.json()]
        if len(closes) < EMA200_PERIOD:
            return None
        return _calculate_ema(closes, EMA200_PERIOD)
    except Exception as e:
        logger.warning("Futures EMA-200 4h fetch failed for %s: %s", symbol, e)
        return None


def get_24h_ticker_futures(symbol: str) -> dict | None:
    try:
        resp = _binance_futures_get(
            "/fapi/v1/ticker/24hr", params={"symbol": symbol}, timeout=10,
        )
        return resp.json()
    except Exception as e:
        logger.warning("Futures 24h ticker fetch failed for %s: %s", symbol, e)
        return None


def refresh_ema200_4h(symbols: list[str]) -> int:
    updated = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(get_ema200_4h, s): s for s in symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                ema = future.result()
            except Exception:
                ema = None
            if ema is not None:
                with state_lock:
                    state["ema200_4h"][symbol] = ema
                updated += 1
    with state_lock:
        state["last_ema200_refresh"] = time.time()
    logger.info("Refreshed EMA-200 (4h) for %d/%d symbols", updated, len(symbols))
    return updated


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
    above_ema200: bool | None = None,
) -> tuple[str, str]:
    """Return (rec_line, reason_line) in Russian for the given signal mix.

    Rules:
      LONG     — (7d/30d high break + volume spike >= multiplier) OR RSI <= oversold
      SHORT    — RSI >= overbought AND price near 24h high
      NEUTRAL  — anything else / mixed

    EMA-200 (4h) trend filter:
      - above_ema200 True  -> suppress SHORT (we're in an uptrend)
      - above_ema200 False -> suppress LONG  (we're in a downtrend)
      - above_ema200 None  -> no filter (data unavailable)
    """
    has_break = broke_weekly or broke_monthly
    has_spike = spike_ratio is not None and spike_ratio >= VOLUME_SPIKE_MULTIPLIER
    is_oversold = rsi is not None and rsi <= RSI_OVERSOLD
    is_overbought = rsi is not None and rsi >= RSI_OVERBOUGHT

    long_signal = (has_break and has_spike) or is_oversold
    short_signal = is_overbought and near_24h_high

    # EMA-200 4h trend filter — don't fight the dominant trend
    short_blocked_by_ema = above_ema200 is True and short_signal
    long_blocked_by_ema = above_ema200 is False and long_signal
    if above_ema200 is True:
        short_signal = False
    if above_ema200 is False:
        long_signal = False

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
    if short_blocked_by_ema:
        parts.append("шорт отклонён — цена выше EMA-200 (4ч)")
    if long_blocked_by_ema:
        parts.append("лонг отклонён — цена ниже EMA-200 (4ч)")
    reason = "; ".join(parts) if parts else "сигналы смешанные, чёткого направления нет"
    return "📊 Рекомендация: НЕЙТРАЛЬНО ➡️", f"Причина: {reason}"


# ---------------------------------------------------------------------------
# Hit-rate tracking (SQLite)
# ---------------------------------------------------------------------------

_db_lock = threading.Lock()
_db_conn: sqlite3.Connection | None = None


def _get_db() -> sqlite3.Connection:
    global _db_conn
    if _db_conn is None:
        _db_conn = sqlite3.connect(HIT_RATE_DB_PATH, check_same_thread=False)
        _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                alert_type TEXT NOT NULL,
                recommendation TEXT,
                price_at_alert REAL NOT NULL,
                price_15m REAL,
                price_1h REAL,
                price_4h REAL
            )
        """)
        _db_conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts)")
        _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry REAL NOT NULL,
                exit REAL NOT NULL,
                pnl_pct REAL NOT NULL,
                rsi REAL,
                ema200_4h REAL,
                volume_usdt_24h REAL
            )
        """)
        _db_conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_chat_ts ON trades(chat_id, ts DESC)")
        _db_conn.commit()
        logger.info("Hit-rate DB ready at %s", HIT_RATE_DB_PATH)
    return _db_conn


def _rec_label(rec_line: str) -> str:
    if "ЛОНГ" in rec_line:
        return "LONG"
    if "ШОРТ" in rec_line:
        return "SHORT"
    return "NEUTRAL"


def log_alert(symbol: str, alert_type: str, recommendation: str | None, price: float | None) -> None:
    """Log an alert send for later hit-rate measurement. No-op if price missing."""
    if price is None or price <= 0:
        return
    try:
        with _db_lock:
            conn = _get_db()
            conn.execute(
                "INSERT INTO alerts (ts, symbol, alert_type, recommendation, price_at_alert) "
                "VALUES (?, ?, ?, ?, ?)",
                (int(time.time()), symbol, alert_type, recommendation, float(price)),
            )
            conn.commit()
    except Exception as e:
        logger.warning("log_alert failed for %s/%s: %s", symbol, alert_type, e)


def fill_alert_followups(tickers: dict[str, dict] | None) -> None:
    """Back-fill follow-up prices for past alerts whose 15m/1h/4h windows have
    elapsed, using the just-fetched tickers map. Cheap to call every cycle.
    """
    if not tickers:
        return
    now_ts = int(time.time())
    c15 = now_ts - HIT_RATE_INTERVALS[0][0]
    c1h = now_ts - HIT_RATE_INTERVALS[1][0]
    c4h = now_ts - HIT_RATE_INTERVALS[2][0]
    retain_cutoff = now_ts - HIT_RATE_RETENTION_DAYS * 86400
    try:
        with _db_lock:
            conn = _get_db()
            rows = conn.execute(
                "SELECT id, symbol, price_15m, price_1h, price_4h, ts FROM alerts "
                "WHERE (price_15m IS NULL AND ts <= ?) "
                "   OR (price_1h  IS NULL AND ts <= ?) "
                "   OR (price_4h  IS NULL AND ts <= ?)",
                (c15, c1h, c4h),
            ).fetchall()
            updates: list[tuple] = []
            for row_id, symbol, p15, p1h, p4h, ts in rows:
                t = tickers.get(symbol)
                if not t:
                    continue
                try:
                    cur = float(t["lastPrice"])
                except (ValueError, KeyError):
                    continue
                new_p15 = cur if (p15 is None and ts <= c15) else p15
                new_p1h = cur if (p1h is None and ts <= c1h) else p1h
                new_p4h = cur if (p4h is None and ts <= c4h) else p4h
                updates.append((new_p15, new_p1h, new_p4h, row_id))
            if updates:
                conn.executemany(
                    "UPDATE alerts SET price_15m=?, price_1h=?, price_4h=? WHERE id=?",
                    updates,
                )
            conn.execute("DELETE FROM alerts WHERE ts < ?", (retain_cutoff,))
            conn.commit()
        if updates:
            logger.info("Hit-rate: filled %d follow-up prices", len(updates))
    except Exception as e:
        logger.warning("fill_alert_followups failed: %s", e)


def compute_hit_rate_stats(days: int = 7) -> list[dict]:
    """Aggregate wins per (alert_type, recommendation) over the last `days` days.
    A win = price moved >= HIT_RATE_WIN_PCT in the recommended direction.
    """
    cutoff = int(time.time()) - days * 86400
    out: list[dict] = []
    try:
        with _db_lock:
            conn = _get_db()
            cursor = conn.execute(
                "SELECT alert_type, recommendation, price_at_alert, price_15m, price_1h, price_4h "
                "FROM alerts WHERE ts >= ?",
                (cutoff,),
            )
            agg: dict[tuple, dict] = {}
            for atype, rec, p0, p15, p1h, p4h in cursor:
                key = (atype, rec or "—")
                a = agg.setdefault(key, {"total": 0, "w15": [0, 0], "w1h": [0, 0], "w4h": [0, 0]})
                a["total"] += 1
                for col, future in (("w15", p15), ("w1h", p1h), ("w4h", p4h)):
                    if future is None or p0 is None or p0 <= 0:
                        continue
                    pct = (future - p0) / p0 * 100.0
                    a[col][1] += 1
                    if rec == "LONG" and pct >= HIT_RATE_WIN_PCT:
                        a[col][0] += 1
                    elif rec == "SHORT" and pct <= -HIT_RATE_WIN_PCT:
                        a[col][0] += 1
        for (atype, rec), a in agg.items():
            out.append({
                "alert_type": atype, "recommendation": rec, "total": a["total"],
                "w15": a["w15"], "w1h": a["w1h"], "w4h": a["w4h"],
            })
        out.sort(key=lambda r: (-r["total"], r["alert_type"]))
    except Exception as e:
        logger.warning("compute_hit_rate_stats failed: %s", e)
    return out


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


def send_new_listing_alert(symbol: str, ticker: dict | None) -> None:
    """Send the dedicated eye-catching new-listing alert (always — bypasses the confluence rule)."""
    price_str = "—"
    volume_str = "—"
    rsi_v: float | None = None
    spike_v: float | None = None
    near_high = False
    if ticker:
        try:
            price_str = f"${float(ticker['lastPrice']):,.6g}"
        except (ValueError, KeyError):
            pass
        try:
            volume_str = f"${float(ticker['quoteVolume']):,.0f}"
        except (ValueError, KeyError):
            pass
        near_high = _near_24h_high(ticker)

    # Fresh listings rarely have 14h of klines yet, but try anyway
    try:
        rsi_v = get_klines_rsi(symbol)
    except Exception:
        pass
    try:
        spike_v, _pct_unused = get_5m_signals(symbol)
    except Exception:
        pass

    rec_line, _reason = make_recommendation(
        rsi=rsi_v, spike_ratio=spike_v, near_24h_high=near_high,
    )
    # Strip the prefix to fit it onto the template's single recommendation line
    rec_short = rec_line.replace("📊 Рекомендация: ", "")

    body = (
        f"🚨🚨🚨 <b>НОВЫЙ ЛИСТИНГ НА BINANCE</b> 🚨🚨🚨\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🆕 <code>{symbol}</code> появился на Binance!\n"
        f"💰 Цена: <b>{price_str}</b>\n"
        f"📊 Объём 24ч: {volume_str}\n"
        f"🎯 Рекомендация: <b>{rec_short}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡️ <b>ДЕЙСТВУЙ БЫСТРО — первые минуты самые важные!</b>"
    )
    if send_telegram(body):
        price_for_log: float | None = None
        if ticker:
            try:
                price_for_log = float(ticker["lastPrice"])
            except (ValueError, KeyError):
                pass
        log_alert(symbol, "new_listing", _rec_label(rec_line), price_for_log)
    logger.info("New-listing alert sent: %s", symbol)


def check_coingecko_new_coins() -> None:
    """Every 30 min: fetch CoinGecko coin list and alert on coins NEWLY appearing
    on CoinGecko that are NOT yet listed on Binance Spot USDT. First run populates
    the baseline without alerting. A safety cap prevents alert floods on anomalies."""
    logger.info("CoinGecko: checking for upcoming listings...")
    try:
        resp = requests.get(COINGECKO_LIST_URL, timeout=20)
        resp.raise_for_status()
        coins = resp.json()
    except Exception as e:
        logger.error("CoinGecko fetch failed: %s", e)
        return

    if not isinstance(coins, list) or not coins:
        logger.warning("CoinGecko returned empty/invalid payload — skipping cycle")
        return

    current_ids = {c["id"] for c in coins if isinstance(c, dict) and c.get("id")}
    if not current_ids:
        logger.warning("CoinGecko payload had no usable ids — skipping cycle")
        return

    with state_lock:
        known_pairs = set(state["known_pairs"])
        known_ids = set(state["known_coingecko_ids"])
        first_run = not state["coingecko_initialized"]
        state["known_coingecko_ids"] = current_ids
        state["coingecko_initialized"] = True
        state["last_coingecko_check"] = time.time()

    if first_run:
        logger.info(
            "CoinGecko baseline initialized (%d coins). No alerts on first run.",
            len(current_ids),
        )
        return

    new_ids = current_ids - known_ids
    if not new_ids:
        logger.info("CoinGecko: no new coin ids since last cycle")
        return

    # Build alerts only for coins NOT yet on Binance Spot USDT
    candidates: list[tuple[str, str]] = []  # (name, symbol_upper)
    for c in coins:
        if not isinstance(c, dict) or c.get("id") not in new_ids:
            continue
        symbol_upper = (c.get("symbol") or "").upper()
        if not symbol_upper:
            continue
        if f"{symbol_upper}USDT" in known_pairs:
            continue
        candidates.append((c.get("name") or "?", symbol_upper))

    logger.info(
        "CoinGecko: %d new ids total, %d not yet on Binance Spot USDT",
        len(new_ids), len(candidates),
    )

    # Safety cap — if CoinGecko returns an abnormally large diff (API anomaly,
    # first-time bulk imports, etc.) batch the rest into a summary line.
    to_send = candidates[:COINGECKO_MAX_ALERTS_PER_CYCLE]
    overflow = len(candidates) - len(to_send)

    for name, symbol in to_send:
        # CoinGecko name/symbol are free-form user-submitted text — escape HTML
        # metacharacters before interpolating into a Telegram HTML message.
        safe_name = html.escape(name)
        safe_symbol = html.escape(symbol)
        body = (
            f"🔭🔭🔭 <b>МОНЕТА ДО BINANCE</b> 🔭🔭🔭\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🌕 <b>{safe_name}</b> (<code>{safe_symbol}</code>) есть на CoinGecko но <b>ЕЩЁ НЕТ на Binance</b>!\n"
            f"🔗 Следи — возможен скорый листинг на Binance\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👀 Изучи проект заранее!"
        )
        send_telegram(body)

    if overflow > 0:
        send_telegram(
            f"ℹ️ Ещё <b>{overflow}</b> новых монет на CoinGecko не показаны "
            f"(превышен лимит {COINGECKO_MAX_ALERTS_PER_CYCLE} за цикл, "
            f"возможна аномалия API)."
        )
        logger.warning("CoinGecko diff exceeded cap (%d extra not sent)", overflow)


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
    dict[str, float],                # rsi_map: symbol -> rsi
    dict[str, float],                # pct_15m_map: symbol -> 15-min pct change
]:
    overbought, oversold, spikes = [], [], []
    rsi_map: dict[str, float] = {}
    pct_15m_map: dict[str, float] = {}
    now = time.time()
    volume_map = dict(symbols_volumes)

    def fetch(symbol):
        rsi = get_klines_rsi(symbol)
        spike, pct = get_5m_signals(symbol)
        return symbol, rsi, spike, pct

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(fetch, s): s for s, _ in symbols_volumes}
        for future in as_completed(futures):
            symbol, rsi, spike_ratio, pct_15m = future.result()
            vol = volume_map[symbol]
            if rsi is not None:
                rsi_map[symbol] = rsi
            if pct_15m is not None:
                pct_15m_map[symbol] = pct_15m

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
        rsi_map,
        pct_15m_map,
    )


# ---------------------------------------------------------------------------
# Momentum & overheated/oversold alerts (standalone — bypass confluence)
# ---------------------------------------------------------------------------

_MOMENTUM_EMOJI = {
    2.0: "🟢",    3.0: "🟢🟢",   5.0: "🟢🟢🟢",  10.0: "🚀🚀🚀",
    -2.0: "🔴",  -3.0: "🔴🔴",  -5.0: "🔴🔴🔴",  -10.0: "💥💥💥",
}


def check_momentum(
    pct_15m_map: dict[str, float],
    tickers: dict[str, dict] | None,
) -> int:
    """For each symbol with a 15-min pct change, send a single alert at the
    highest tier crossed (e.g. +12% triggers only the +10% alert, not all 4).
    Cooldown is per (symbol, threshold-tier).
    """
    sent = 0
    now = time.time()
    for symbol, pct in pct_15m_map.items():
        if pct is None:
            continue

        threshold: float | None = None
        if pct > 0:
            for t in sorted(MOMENTUM_THRESHOLDS_UP, reverse=True):
                if pct >= t:
                    threshold = t
                    break
        elif pct < 0:
            for t in sorted(MOMENTUM_THRESHOLDS_DOWN):  # ascending: -10, -5, -2
                if pct <= t:
                    threshold = t
                    break

        if threshold is None:
            continue

        with state_lock:
            sym_map = state["last_momentum_alerted"].get(symbol, {})
            last_ts = sym_map.get(threshold, 0)
        if now - last_ts < MOMENTUM_COOLDOWN:
            continue

        t = tickers.get(symbol) if tickers else None
        price: float | None = None
        price_str = "—"
        if t:
            try:
                price = float(t["lastPrice"])
                price_str = f"${price:,.6g}"
            except (ValueError, KeyError):
                pass

        emoji = _MOMENTUM_EMOJI.get(threshold, "📊")
        sign = "+" if pct > 0 else ""
        body = (
            f"{emoji} <b><code>{symbol}</code> {sign}{pct:.1f}% за 15 минут</b>\n"
            f"💰 Цена: {price_str}"
        )
        if not send_telegram(body):
            continue

        with state_lock:
            state["last_momentum_alerted"].setdefault(symbol, {})[threshold] = now

        rec = "LONG" if pct > 0 else "SHORT"
        kind = f"momentum_{'up' if pct > 0 else 'down'}_{int(abs(threshold))}"
        log_alert(symbol, kind, rec, price)
        sent += 1
    return sent


def check_overheated_oversold(
    tickers: dict[str, dict] | None,
    rsi_map: dict[str, float],
) -> tuple[int, int]:
    """Standalone "overheated" (24h >= +20% AND RSI >= 70) and "oversold"
    (24h <= -20% AND RSI <= 30) alerts. Bypass confluence.
    """
    if not tickers:
        return 0, 0
    sent_oh, sent_os = 0, 0
    now = time.time()
    for symbol, t in tickers.items():
        rsi = rsi_map.get(symbol)
        if rsi is None:
            continue
        try:
            pct24 = float(t["priceChangePercent"])
            price = float(t["lastPrice"])
        except (ValueError, KeyError):
            continue

        if pct24 >= OVERHEATED_24H_PCT and rsi >= RSI_OVERBOUGHT:
            with state_lock:
                last = state["last_overheated_alerted"].get(symbol, 0)
            if now - last >= OVERHEATED_COOLDOWN:
                body = (
                    f"⚠️ <b>ПЕРЕГРЕТА — возможен шорт</b>\n"
                    f"Монета выросла слишком быстро\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"<code>{symbol}</code>\n"
                    f"📈 24ч: <b>+{pct24:.1f}%</b>\n"
                    f"🔥 RSI: <b>{rsi:.1f}</b>\n"
                    f"💰 Цена: ${price:,.6g}"
                )
                if send_telegram(body):
                    with state_lock:
                        state["last_overheated_alerted"][symbol] = now
                    log_alert(symbol, "overheated_24h", "SHORT", price)
                    sent_oh += 1

        elif pct24 <= OVERSOLD_24H_PCT and rsi <= RSI_OVERSOLD:
            with state_lock:
                last = state["last_oversold_alerted"].get(symbol, 0)
            if now - last >= OVERSOLD_COOLDOWN:
                body = (
                    f"💎 <b>ПЕРЕПРОДАНА — возможен лонг</b>\n"
                    f"Монета упала слишком сильно\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"<code>{symbol}</code>\n"
                    f"📉 24ч: <b>{pct24:.1f}%</b>\n"
                    f"🧊 RSI: <b>{rsi:.1f}</b>\n"
                    f"💰 Цена: ${price:,.6g}"
                )
                if send_telegram(body):
                    with state_lock:
                        state["last_oversold_alerted"][symbol] = now
                    log_alert(symbol, "oversold_24h", "LONG", price)
                    sent_os += 1
    return sent_oh, sent_os


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
        "momentum_alerts": 0,          # standalone 15-min price-momentum alerts
        "overheated_alerts": 0,        # standalone +20% & RSI>=70
        "oversold_alerts": 0,          # standalone -20% & RSI<=30
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
            # 2. Detect new listings — these get a dedicated eye-catching alert
            # (NOT gated by the confluence rule; new listings are too time-sensitive to suppress)
            new_listings = check_new_listings(current_pairs)
            summary["new_listings"] = len(new_listings)
            # Defer sending until after tickers are fetched so price/volume can be filled in

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

            # 4b. Send eye-catching new-listing alerts now that tickers are available
            for sym in new_listings:
                send_new_listing_alert(sym, tickers.get(sym) if tickers else None)

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
                # 6. Detect RSI + volume spike signals (also returns rsi_map + pct_15m_map)
                overbought, oversold, vol_spikes, rsi_map, pct_15m_map = check_rsi_and_spikes(liquid_pairs)
                summary["rsi_overbought"] = len(overbought)
                summary["rsi_oversold"] = len(oversold)
                summary["vol_spikes"] = len(vol_spikes)

                # 6a. Standalone momentum alerts (15-min price change tiers)
                summary["momentum_alerts"] = check_momentum(pct_15m_map, tickers)

                # 6b. Standalone overheated / oversold (24h ±20% confirmed by RSI)
                oh_n, os_n = check_overheated_oversold(tickers, rsi_map)
                summary["overheated_alerts"] = oh_n
                summary["oversold_alerts"] = os_n

                # 7. Refresh stored 7d/30d highs + EMA-200 (4h) once per hour
                with state_lock:
                    needs_refresh = (
                        time.time() - state["last_weekly_monthly_refresh"]
                        >= WEEKLY_MONTHLY_REFRESH_INTERVAL
                    )
                    needs_ema = (
                        time.time() - state["last_ema200_refresh"]
                        >= EMA200_REFRESH_INTERVAL
                    )

                if needs_refresh:
                    logger.info("Refreshing 7d/30d highs for %d liquid pairs...", len(liquid_pairs))
                    try:
                        refresh_weekly_monthly_highs([s for s, _ in liquid_pairs])
                    except Exception as e:
                        logger.error("Weekly/monthly refresh failed: %s", e)
                        summary["errors"].append(f"WM refresh: {e}")

                if needs_ema:
                    logger.info("Refreshing EMA-200 (4h) for %d liquid pairs...", len(liquid_pairs))
                    try:
                        refresh_ema200_4h([s for s, _ in liquid_pairs])
                    except Exception as e:
                        logger.error("EMA-200 refresh failed: %s", e)
                        summary["errors"].append(f"EMA refresh: {e}")

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

            # 9a. (New listings are sent separately above — not part of confluence buckets)

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
            with state_lock:
                ema_map = dict(state["ema200_4h"])
            for sym, b in buckets.items():
                if len(b["lines"]) < CONFLUENCE_MIN_SIGNALS:
                    summary["single_signals_skipped"] += 1
                    logger.debug("Skipped single-signal coin %s: %s", sym, sorted(b["flags"]))
                    continue

                ema = ema_map.get(sym)
                above_ema: bool | None = None
                if ema is not None and b["price"] is not None:
                    above_ema = b["price"] > ema

                rec_line, reason_line = make_recommendation(
                    rsi=b["rsi"],
                    spike_ratio=b["spike_ratio"],
                    broke_weekly=b["broke_weekly"],
                    broke_monthly=b["broke_monthly"],
                    near_24h_high=b["near_24h_high"],
                    above_ema200=above_ema,
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
                log_alert(sym, "confluence", _rec_label(rec_line), b["price"])

                # Mark cooldowns only for signals that actually contributed to a sent alert
                with state_lock:
                    for key, s in b["cooldowns"]:
                        state[key][s] = now_ts

            if summary["single_signals_skipped"]:
                logger.info(
                    "Suppressed %d single-signal coins (need ≥%d to alert)",
                    summary["single_signals_skipped"], CONFLUENCE_MIN_SIGNALS,
                )

            # 10. Hit-rate: back-fill follow-up prices for past alerts (15м / 1ч / 4ч)
            fill_alert_followups(tickers)

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
                    f"<b>Сигналы конфлюэнции:</b>\n"
                    f"• 📈 Пробой 24ч / 7д / 30д максимума\n"
                    f"• 🚀 Всплеск объёма ≥ {VOLUME_SPIKE_MULTIPLIER}× средн.\n"
                    f"• 🔥 RSI ≥ {RSI_OVERBOUGHT} перекупленность (1ч, кулдаун 4ч)\n"
                    f"• 🧊 RSI ≤ {RSI_OVERSOLD} перепроданность (1ч, кулдаун 4ч)\n\n"
                    f"<b>Отдельные алерты (без конфлюэнции):</b>\n"
                    f"• 🆕 Новый листинг на Binance\n"
                    f"• 🟢/🔴 Импульс ±2%/3%/5%/10% за 15 минут\n"
                    f"• ⚠️ Перегрета (+20% за 24ч + RSI ≥ 70 — возможен шорт)\n"
                    f"• 💎 Перепродана (-20% за 24ч + RSI ≤ 30 — возможен лонг)\n\n"
                    f"<b>Фильтр тренда:</b> EMA-200 (4ч) — блокирует шорт выше EMA и лонг ниже EMA.\n\n"
                    f"<b>Команды:</b> /status /top10 /signal /stats /trade /mytrades /silence /unmute"
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
        f"  🧊 RSI перепроданность: {summary.get('rsi_oversold', 0)}\n\n"
        f"<b>Отдельные алерты (без конфлюэнции):</b>\n"
        f"  🟢/🔴 Импульс 15м: <b>{summary.get('momentum_alerts', 0)}</b>\n"
        f"  ⚠️ Перегрета (+20% + RSI≥70): <b>{summary.get('overheated_alerts', 0)}</b>\n"
        f"  💎 Перепродана (-20% + RSI≤30): <b>{summary.get('oversold_alerts', 0)}</b>"
        f"{error_line}\n\n"
        f"<b>Правила:</b>\n"
        f"  Алерт только при ≥ {CONFLUENCE_MIN_SIGNALS} сигналах на одной монете\n"
        f"  Мин. объём: ${MIN_VOLUME_USDT:,.0f}\n"
        f"  Всплеск объёма: ≥ {VOLUME_SPIKE_MULTIPLIER}× средн. (5м)\n"
        f"  RSI перекупленность: ≥ {RSI_OVERBOUGHT}  |  кулдаун {RSI_ALERT_COOLDOWN // 3600}ч\n"
        f"  RSI перепроданность: ≤ {RSI_OVERSOLD}  |  кулдаун {RSI_ALERT_COOLDOWN // 3600}ч\n"
        f"  EMA-200 (4ч): тренд-фильтр для шорт/лонг\n"
        f"  Интервал проверки: 5 мин\n\n"
        f"<b>Команды:</b> /status · /top10 · /signal · /stats · /trade · /mytrades · /silence · /unmute"
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


def handle_stats_command(chat_id: int) -> None:
    """Show win rates per alert type for the last HIT_RATE_RETENTION_DAYS days."""
    stats = compute_hit_rate_stats(days=HIT_RATE_RETENTION_DAYS)
    if not stats:
        _telegram_send(
            chat_id,
            f"📊 <b>Статистика за {HIT_RATE_RETENTION_DAYS} дней</b>\n\n"
            f"Пока нет данных — алерты ещё не накоплены.",
        )
        return

    def fmt(arr: list) -> str:
        wins, n = arr
        if n == 0:
            return "—"
        return f"{wins/n*100:.0f}% ({n})"

    lines = [f"📊 <b>Статистика за {HIT_RATE_RETENTION_DAYS} дней</b>", ""]
    for r in stats:
        lines.append(
            f"<b>{r['alert_type']}</b> · {r['recommendation']} · "
            f"{r['total']} алертов\n"
            f"  15м: {fmt(r['w15'])}  |  1ч: {fmt(r['w1h'])}  |  4ч: {fmt(r['w4h'])}"
        )
    lines.append("")
    lines.append(f"<i>Успех = движение ≥ {HIT_RATE_WIN_PCT:.0f}% в нужную сторону</i>")
    _telegram_send(chat_id, "\n".join(lines))


# ---------------------------------------------------------------------------
# Trade journal — /trade and /mytrades
# ---------------------------------------------------------------------------

_TRADE_USAGE = (
    "📝 <b>Журнал сделок</b>\n\n"
    "Формат: <code>/trade SYMBOL направление ENTRY EXIT</code>\n"
    "Пример: <code>/trade BTCUSDT лонг 82000 80500</code>\n\n"
    "Направление: <b>лонг</b> или <b>шорт</b>\n"
    "Цены — числа, точка как разделитель."
)


def _parse_trade_args(text: str) -> tuple[str, str, float, float] | None:
    """Return (symbol, direction, entry, exit) or None if parsing fails.
    Direction normalized to 'лонг'/'шорт'."""
    parts = text.split()
    if len(parts) < 5:
        return None
    _cmd, sym, direction, entry_s, exit_s = parts[:5]
    sym = sym.upper().strip()
    if not sym.endswith("USDT") or not sym.replace("USDT", "").isalnum():
        return None

    dir_l = direction.lower().strip()
    if dir_l in ("лонг", "long", "buy", "📈"):
        direction_norm = "лонг"
    elif dir_l in ("шорт", "short", "sell", "📉"):
        direction_norm = "шорт"
    else:
        return None

    try:
        entry = float(entry_s.replace(",", "."))
        exit_ = float(exit_s.replace(",", "."))
    except ValueError:
        return None
    if entry <= 0 or exit_ <= 0:
        return None
    return sym, direction_norm, entry, exit_


def _analyze_trade(
    direction: str, pnl_pct: float,
    rsi: float | None, ema200: float | None, entry: float,
    volume_24h: float | None,
    has_market_data: bool = True,
) -> tuple[list[str], list[str], str]:
    """Return (mistakes, positives, advice) — each in Russian.
    Uses CURRENT indicator snapshot as proxy for trade-time context.
    If has_market_data is False, the analysis falls back to a price-only
    review based on the realized PnL.
    """
    mistakes: list[str] = []
    positives: list[str] = []

    # Price-only fallback when market data is unavailable
    if not has_market_data:
        if pnl_pct > 0:
            positives.append(f"сделка закрыта в плюс ({pnl_pct:+.2f}%)")
            advice = (
                "Рыночные данные сейчас недоступны — анализ выполнен только по цене. "
                "Фиксируйте часть прибыли и переносите стоп в безубыток."
            )
        elif pnl_pct < 0:
            if abs(pnl_pct) > 5:
                mistakes.append(f"крупный убыток {pnl_pct:+.2f}% — стоп-лосс был слишком далеко или его не было")
            advice = (
                "Рыночные данные сейчас недоступны — анализ выполнен только по цене. "
                "Главное правило: всегда ставьте стоп-лосс на 1–2% от входа."
            )
        else:
            advice = (
                "Рыночные данные сейчас недоступны — анализ выполнен только по цене. "
                "Безубыток — нейтральный результат."
            )
        return mistakes, positives, advice

    # Trend alignment vs EMA-200 (4h)
    if ema200 is not None:
        above = entry > ema200
        if direction == "лонг" and not above:
            mistakes.append("вход в лонг ниже EMA-200 (4ч) — против основного тренда")
        elif direction == "шорт" and above:
            mistakes.append("вход в шорт выше EMA-200 (4ч) — против основного тренда")
        else:
            positives.append("вход по тренду EMA-200 (4ч)")

    # RSI extremes at entry-direction
    if rsi is not None:
        if direction == "лонг" and rsi >= RSI_OVERBOUGHT:
            mistakes.append(f"лонг при RSI {rsi:.0f} ≥ {RSI_OVERBOUGHT:.0f} — зона перекупленности")
        elif direction == "шорт" and rsi <= RSI_OVERSOLD:
            mistakes.append(f"шорт при RSI {rsi:.0f} ≤ {RSI_OVERSOLD:.0f} — зона перепроданности")
        elif direction == "лонг" and rsi <= RSI_OVERSOLD:
            positives.append(f"лонг из зоны перепроданности (RSI {rsi:.0f})")
        elif direction == "шорт" and rsi >= RSI_OVERBOUGHT:
            positives.append(f"шорт из зоны перекупленности (RSI {rsi:.0f})")
        else:
            positives.append(f"RSI {rsi:.0f} — нейтральная зона, без экстремумов")

    # Liquidity check
    if volume_24h is not None:
        if volume_24h < MIN_VOLUME_USDT:
            mistakes.append(
                f"низкая ликвидность: объём 24ч ${volume_24h:,.0f} < ${MIN_VOLUME_USDT:,.0f}"
            )
        elif volume_24h >= MIN_VOLUME_USDT * 10:
            positives.append(f"высокая ликвидность: объём 24ч ${volume_24h:,.0f}")

    # Result-based notes
    if pnl_pct > 0:
        positives.append(f"сделка закрыта в плюс ({pnl_pct:+.2f}%)")
    else:
        if abs(pnl_pct) > 5:
            mistakes.append(f"крупный убыток {pnl_pct:+.2f}% — стоп-лосс был слишком далеко или его не было")

    # Advice
    if mistakes:
        advice = (
            "Перед входом проверяйте: тренд EMA-200 (4ч), RSI на 1ч, "
            "объём ≥ ликвидного минимума. Не входите против всех трёх сразу."
        )
        if pnl_pct < 0 and abs(pnl_pct) > 3:
            advice += " Установите стоп-лосс на 1–2% от входа."
    elif pnl_pct > 0:
        advice = (
            "Отличный сетап — фиксируйте часть прибыли и переносите стоп "
            "в безубыток, чтобы защитить плюс."
        )
    else:
        advice = (
            "Сетап был корректен, но движение пошло не туда — это нормально. "
            "Главное — управление риском и быстрая фиксация убытка."
        )
    return mistakes, positives, advice


def handle_trade_command(chat_id: int, raw_text: str) -> None:
    parsed = _parse_trade_args(raw_text)
    if parsed is None:
        _telegram_send(chat_id, _TRADE_USAGE)
        return
    symbol, direction, entry, exit_ = parsed

    # PnL
    if direction == "лонг":
        pnl_pct = (exit_ - entry) / entry * 100.0
    else:  # шорт
        pnl_pct = (entry - exit_) / entry * 100.0

    # Snapshot current indicators (concept of "trade-time context"). Try spot
    # first, then fall back to USDⓈ-M futures for symbols like NVDAUSDT that
    # only exist on the futures market.
    rsi = get_klines_rsi(symbol)
    with state_lock:
        ema200 = state["ema200_4h"].get(symbol)

    def _fetch_24h_ticker_spot(sym: str) -> dict | None:
        try:
            resp = _binance_get(
                "/api/v3/ticker/24hr", params={"symbol": sym}, timeout=10,
            )
            return resp.json()
        except Exception as e:
            logger.warning("Trade /trade: spot 24h ticker fetch failed for %s: %s", sym, e)
            return None

    volume_24h: float | None = None
    ticker = _fetch_24h_ticker_spot(symbol)
    if ticker is not None:
        try:
            volume_24h = float(ticker.get("quoteVolume", 0)) or None
        except (TypeError, ValueError):
            volume_24h = None

    data_source = "spot"
    # Futures fallback: if NOTHING came back from spot (the symbol probably
    # isn't listed there), try the USDⓈ-M futures endpoints.
    if rsi is None and ema200 is None and volume_24h is None:
        logger.info("Trade /trade: no spot data for %s, trying futures fallback", symbol)
        rsi = get_klines_rsi_futures(symbol)
        ema200 = get_ema200_4h_futures(symbol)
        f_ticker = get_24h_ticker_futures(symbol)
        if f_ticker is not None:
            try:
                volume_24h = float(f_ticker.get("quoteVolume", 0)) or None
            except (TypeError, ValueError):
                volume_24h = None
        if rsi is not None or ema200 is not None or volume_24h is not None:
            data_source = "futures"

    has_market_data = (rsi is not None) or (ema200 is not None) or (volume_24h is not None)

    mistakes, positives, advice = _analyze_trade(
        direction, pnl_pct, rsi, ema200, entry, volume_24h,
        has_market_data=has_market_data,
    )

    # Save to DB
    try:
        with _db_lock:
            conn = _get_db()
            conn.execute(
                "INSERT INTO trades (ts, chat_id, symbol, direction, entry, exit, "
                "pnl_pct, rsi, ema200_4h, volume_usdt_24h) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (int(time.time()), int(chat_id), symbol, direction,
                 entry, exit_, pnl_pct, rsi, ema200, volume_24h),
            )
            conn.commit()
    except Exception as e:
        logger.exception("Failed to save trade: %s", e)

    # Format response
    result_emoji = "🟢" if pnl_pct > 0 else ("🔴" if pnl_pct < 0 else "⚪️")

    lines = [
        f"📝 <b>Разбор сделки: <code>{symbol}</code> · {direction.upper()}</b>",
        f"Вход: ${entry:,.6g}  →  Выход: ${exit_:,.6g}",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"{result_emoji} <b>Результат: {pnl_pct:+.2f}%</b>",
        "",
    ]
    if not has_market_data:
        lines.append("📡 <b>Контекст рынка:</b> данные по монете недоступны")
    else:
        rsi_str = f"{rsi:.1f}" if rsi is not None else "недоступно"
        ema_str = f"${ema200:,.6g}" if ema200 is not None else "недоступно"
        vol_str = f"${volume_24h:,.0f}" if volume_24h is not None else "недоступно"
        source_label = "спот" if data_source == "spot" else "фьючерсы"
        lines.extend([
            f"<b>Контекст рынка (сейчас, {source_label}):</b>",
            f"  RSI (1ч): {rsi_str}",
            f"  EMA-200 (4ч): {ema_str}",
            f"  Объём 24ч: {vol_str}",
        ])
    lines.append("")
    if mistakes:
        lines.append("❌ <b>Ошибки:</b>")
        for m in mistakes:
            lines.append(f"  • {m}")
    else:
        lines.append("❌ <b>Ошибки:</b> явных ошибок не обнаружено")
    lines.append("")
    if positives:
        lines.append("✅ <b>Правильно:</b>")
        for p in positives:
            lines.append(f"  • {p}")
    else:
        lines.append("✅ <b>Правильно:</b> сильных плюсов не выделено")
    lines.append("")
    lines.append(f"💡 <b>Совет:</b> {advice}")
    _telegram_send(chat_id, "\n".join(lines))


def handle_mytrades_command(chat_id: int) -> None:
    """Show the user's last 10 trades + W/L statistics."""
    try:
        with _db_lock:
            conn = _get_db()
            rows = conn.execute(
                "SELECT ts, symbol, direction, entry, exit, pnl_pct "
                "FROM trades WHERE chat_id = ? ORDER BY ts DESC LIMIT 10",
                (int(chat_id),),
            ).fetchall()
            stats = conn.execute(
                "SELECT COUNT(*), "
                "SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN pnl_pct < 0 THEN 1 ELSE 0 END), "
                "COALESCE(SUM(pnl_pct), 0), COALESCE(AVG(pnl_pct), 0) "
                "FROM trades WHERE chat_id = ?",
                (int(chat_id),),
            ).fetchone()
    except Exception as e:
        logger.exception("/mytrades failed: %s", e)
        _telegram_send(chat_id, "⚠️ Не удалось получить журнал сделок.")
        return

    total, wins, losses, sum_pnl, avg_pnl = stats or (0, 0, 0, 0.0, 0.0)
    wins = wins or 0
    losses = losses or 0
    if not rows:
        _telegram_send(
            chat_id,
            "📒 <b>Ваш журнал сделок пуст</b>\n\n"
            "Добавьте сделку командой:\n"
            "<code>/trade BTCUSDT лонг 82000 80500</code>",
        )
        return

    win_rate = (wins / total * 100.0) if total > 0 else 0.0

    lines = [
        f"📒 <b>Последние {len(rows)} сделок</b>",
        "",
    ]
    import datetime as _dt
    for ts, sym, direction, entry, exit_, pnl in rows:
        when = _dt.datetime.utcfromtimestamp(int(ts)).strftime("%d.%m %H:%M")
        emoji = "🟢" if pnl > 0 else ("🔴" if pnl < 0 else "⚪️")
        lines.append(
            f"{emoji} <code>{sym}</code> {direction} · "
            f"{pnl:+.2f}% · {when}"
        )
        lines.append(f"   ${entry:,.6g} → ${exit_:,.6g}")
    lines.append("")
    lines.append(f"<b>📊 Статистика (всего: {total}):</b>")
    lines.append(f"  ✅ Прибыльных: {wins}  |  ❌ Убыточных: {losses}")
    lines.append(f"  🎯 Win rate: <b>{win_rate:.1f}%</b>")
    lines.append(f"  Σ PnL: <b>{sum_pnl:+.2f}%</b>  |  средн.: {avg_pnl:+.2f}%")
    _telegram_send(chat_id, "\n".join(lines))


def handle_signal_command(chat_id: int) -> None:
    """Show current LONG/SHORT/NEUTRAL recommendations for the top-10 symbols
    by day-over-day volume change (the same ranking used by /top10)."""
    with state_lock:
        ranking = list(state["volume_ranking"])
        weekly_highs = dict(state["weekly_highs"])
        monthly_highs = dict(state["monthly_highs"])
        ema_map = dict(state["ema200_4h"])

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
        rsi_v = get_klines_rsi(symbol)
        spike_v, _pct = get_5m_signals(symbol)
        return symbol, rsi_v, spike_v

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

        ema = ema_map.get(sym)
        above_ema: bool | None = None
        if ema is not None and price is not None:
            above_ema = price > ema
        rec_line, reason_line = make_recommendation(
            rsi=rsi_v, spike_ratio=spike_v,
            broke_weekly=broke_weekly, broke_monthly=broke_monthly,
            near_24h_high=near_high,
            above_ema200=above_ema,
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

_run_checks_lock = threading.Lock()
_original_run_checks = run_checks


def run_checks():  # type: ignore[no-redef]
    """Single-flight wrapper: skip if a previous cycle is still in progress.
    Prevents overlapping cycles (scheduled vs /run-now) from racing on
    cooldown read/write and producing duplicate alerts.
    """
    if not _run_checks_lock.acquire(blocking=False):
        logger.warning("run_checks skipped — previous cycle still in progress")
        return
    try:
        _original_run_checks()
    finally:
        _run_checks_lock.release()


scheduler = BackgroundScheduler(timezone="UTC")
scheduler.add_job(
    run_checks, "interval", minutes=5, id="binance_check",
    next_run_time=__import__("datetime").datetime.utcnow(),
)
scheduler.add_job(
    check_coingecko_new_coins, "interval", minutes=COINGECKO_CHECK_INTERVAL_MIN,
    id="coingecko_check",
    next_run_time=__import__("datetime").datetime.utcnow(),
)
scheduler.start()

start_command_polling()
start_watchdog()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
