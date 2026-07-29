import os
import math
import time
import html
import base64
import logging
import json
from datetime import datetime
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
    "atr_4h": {},                      # symbol -> float (ATR-14 on 4h, refreshed hourly)
    "sigma_daily": {},                 # symbol -> float (std of daily % returns over ~30d)
    "last_ema200_refresh": 0,          # unix ts of last 4h-stats refresh
    "last_momentum_alerted": {},       # symbol -> {threshold: ts}
    "last_overheated_alerted": {},     # symbol -> ts (24h +20% & RSI>=70 cooldown)
    "last_oversold_alerted": {},       # symbol -> ts (24h -20% & RSI<=30 cooldown)
    "last_breakdown_alerted": {},           # symbol -> ts (breakdown_short TEST cooldown)
    "last_momentum_long_alerted": {},       # symbol -> ts (momentum_long TEST cooldown)
    "sl_blocked": {},                       # unused — SL re-entry cooldown disabled
    "last_new_listing_short_alerted": {},   # symbol -> ts (new_listing_short TEST cooldown)
    "last_pump_alerted": {},           # symbol -> ts (24h +30% pump cooldown)
    "last_vol_surge_alerted": {},      # symbol -> ts (300% vol + CRSI extreme cooldown)
    "market_regime": "NEUTRAL",        # BTC daily EMA-200 regime: BULL/BEAR/NEUTRAL
    "market_regime_ts": 0,             # unix ts of last regime refresh
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
# SL_REENTRY_COOLDOWN — disabled (no block after SL hit)

# --- Reversal confirmation for SHORT signals ---
# A SHORT is only sent if price has already pulled back ≥ this % from its
# 24-hour high.  Prevents shorting a coin that is still actively rising.
SHORT_REVERSAL_CONFIRM_PCT = 0.75  # price must be ≥ 0.75% below 24h high

# --- New listing pump→dump SHORT (TEST) ---
NEW_LISTING_WINDOW_H     = 48     # only watch coins listed in last 48h
NEW_LISTING_PUMP_PCT     = 20.0   # coin must be ≥+20% above its listing price
NEW_LISTING_PUMP_RSI_MIN = 65.0   # RSI must confirm overheating
NEW_LISTING_SHORT_COOLDOWN = 14400  # 4h cooldown per symbol

# Volume-surge + CRSI combo alert (calendar-day vol vs yesterday)
MIN_VOLUME_USDT_BROAD = 10_000     # lower floor so pairs like ORCAUSDT enter the daily refresh
VOLUME_SURGE_PCT = 300.0           # today's USDT vol must be ≥ +300% over yesterday's
# SHORT-side tightened 75→90 on 2026-05-17 after a 100-symbol × 180-day
# backtest: at the old threshold the SHORT side was break-even (PnL ≈ 0%);
# at CRSI≥90 winrate jumps to 74% with +5.8% avg PnL on the 7d horizon
# (n=57). LONG threshold stays at 25 because backtest evidence for a tighter
# cut was underpowered (n≈12). See `telegram-webhook-bot/backtest.py`.
CRSI_OVERBOUGHT = 90.0
CRSI_OVERSOLD = 25.0
VOLUME_SURGE_COOLDOWN = 14400      # 4h per coin so a multi-hour pump doesn't re-fire each cycle
CRSI_KLINES_LIMIT = 110            # needs ≥104 closes (RSI_3 + streak_RSI_2 + 100-day rank)
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
OVERHEATED_24H_PCT = 15.0
OVERSOLD_24H_PCT = -15.0
OVERHEATED_COOLDOWN = 14400        # 4h
OVERSOLD_COOLDOWN = 7200           # 2h

# Top-gainers list threshold (used by /top30 — pure +X% mover, no alert).
# The pump_24h *alert* itself was removed in favor of volume_surge_{short,long}.
PUMP_24H_PCT = 30.0

# EMA-200 (4h) trend filter
EMA200_PERIOD = 200
EMA200_FETCH_LIMIT = 250
EMA200_REFRESH_INTERVAL = 3600     # hourly

# Hit-rate tracking
HIT_RATE_DB_PATH = os.path.join(os.path.dirname(__file__) or ".", "alerts.db")

# Rolling backup of alerts.db. Keeps the last ALERTS_DB_BACKUP_KEEP hourly
# snapshots in a sibling directory; sufficient for short-term recovery after
# an accidental schema change or corruption. Real durability still needs an
# offsite copy (Object Storage / external bucket).
ALERTS_DB_BACKUP_DIR = os.path.join(
    os.path.dirname(__file__) or ".", "alerts_db_backups"
)
ALERTS_DB_BACKUP_KEEP = 24

# Persisted runtime state — currently just the last-known-good Binance host
# so a process restart doesn't redo the 4-second 451 carousel on every boot.
BOT_STATE_PATH = os.path.join(
    os.path.dirname(__file__) or ".", ".bot_state.json"
)
HIT_RATE_INTERVALS = ((180, "3м"), (300, "5м"), (900, "15м"), (3600, "1ч"), (14400, "4ч"))
HIT_RATE_WIN_PCT = 1.0             # >=1% move in predicted direction = win (legacy, kept for compat)
HIT_RATE_RETENTION_DAYS = 120
# SL/TP for simulated P&L in hit-rate stats (matches backtest defaults)
HIT_RATE_SL_PCT = 2.5              # stop-loss % (raised from 2.0 to reduce noise SL hits)
HIT_RATE_TP_PCT = 4.0              # take-profit % for LONG
HIT_RATE_TP_PCT_SHORT = 6.0        # take-profit % for SHORT (wider target, asymmetric)


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

def send_telegram(text: str) -> bool:
    with state_lock:
        if state["silenced"]:
            logger.info("Alert suppressed (silenced): %s", text[:60])
            return False
    return _telegram_send(TELEGRAM_CHAT_ID, text)


def _telegram_send(
    chat_id: str | int,
    text: str,
    reply_markup: dict | None = None,
) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.error("Telegram send failed: %s", e)
        return False


def _telegram_answer_callback(callback_id: str, text: str = "") -> None:
    """Acknowledge a callback_query so Telegram stops showing a loading spinner."""
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_id, "text": text, "cache_time": 1},
            timeout=10,
        )
    except Exception as e:
        logger.warning("answerCallbackQuery failed: %s", e)


def _telegram_edit_markup(chat_id: int, message_id: int, reply_markup: dict | None) -> None:
    """Replace the inline keyboard of an existing message (used after voting)."""
    try:
        payload: dict = {"chat_id": chat_id, "message_id": message_id}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageReplyMarkup",
            json=payload,
            timeout=10,
        )
    except Exception as e:
        logger.warning("editMessageReplyMarkup failed: %s", e)


# ---------------------------------------------------------------------------
# Gemini Vision — used to extract trade details from forwarded Binance share
# cards so the user can drop a screenshot into the bot instead of typing
# `/trade SYMBOL ... ENTRY EXIT` by hand. Env vars are auto-provisioned by
# Replit AI Integrations (no key needed in source).
# ---------------------------------------------------------------------------
_GEMINI_BASE_URL = os.environ.get("AI_INTEGRATIONS_GEMINI_BASE_URL")
_GEMINI_API_KEY = os.environ.get("AI_INTEGRATIONS_GEMINI_API_KEY")
_GEMINI_VISION_MODEL = "gemini-2.5-flash"
_TRADE_EXTRACT_PROMPT = (
    "You are looking at a Binance Futures trade share card (screenshot). "
    "Extract the trade details and return STRICT JSON only — no prose, no "
    "code fences. Schema: "
    '{"symbol": string, "direction": "лонг"|"шорт", "entry": number, '
    '"exit": number}. '
    "Rules: symbol must end with USDT (e.g. BTCUSDT, MIRAUSDT). "
    "Direction: 'Купить'/'Buy'/'Long' → 'лонг'; 'Продать'/'Sell'/'Short' → 'шорт'. "
    "entry = «Цена входа» / «Entry Price». "
    "exit  = «Средняя цена закрытия» / «Average Close Price» / «Exit Price». "
    "Use plain decimal numbers (no thousand separators, dot as decimal). "
    "If the image is not a Binance Futures trade card, or any required field "
    'is missing, return {"error": "<short reason in Russian>"} instead.'
)


def _safe_err(e: Exception) -> str:
    """Log-safe error representation. The Telegram bot token lives in URLs and
    the Gemini API key would too if it were ever leaked; ``requests`` puts the
    full URL into RequestException stringification, so we must NEVER pass the
    raw exception into ``logger.*("%s", e)``. This helper keeps just the
    exception class + HTTP status when available."""
    name = type(e).__name__
    status = None
    resp = getattr(e, "response", None)
    if resp is not None:
        status = getattr(resp, "status_code", None)
    return f"{name}(status={status})" if status is not None else name


def _gemini_extract_trade_from_image(
    image_bytes: bytes, mime_type: str = "image/jpeg",
) -> dict | None:
    """Call Gemini Vision with the trade-extraction prompt and parse its JSON.
    Returns the parsed dict (which may be either the trade fields or
    {"error": ...}) or None on transport/parse failure.

    URL shape: the Replit AI Integrations proxy is OpenAI/Google-compatible
    but mounts the model endpoints directly under the base URL (no /v1beta
    prefix — mirrors the GoogleGenAI SDK template's ``apiVersion: ""``).
    Auth uses the ``x-goog-api-key`` header so the key never appears in the
    URL or in ``requests`` exception strings if a call fails."""
    if not _GEMINI_BASE_URL or not _GEMINI_API_KEY:
        logger.error("Gemini env vars missing — cannot parse trade photo")
        return None
    url = (f"{_GEMINI_BASE_URL.rstrip('/')}/models/"
           f"{_GEMINI_VISION_MODEL}:generateContent")
    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {"text": _TRADE_EXTRACT_PROMPT},
                {"inline_data": {
                    "mime_type": mime_type,
                    "data": base64.b64encode(image_bytes).decode("ascii"),
                }},
            ],
        }],
        "generationConfig": {
            "responseMimeType": "application/json",
            "maxOutputTokens": 512,
            "temperature": 0.0,
        },
    }
    try:
        resp = requests.post(
            url,
            headers={"x-goog-api-key": _GEMINI_API_KEY},
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        parts = body["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
        return json.loads(text)
    except (requests.RequestException, KeyError, IndexError,
            json.JSONDecodeError, ValueError) as e:
        logger.warning("Gemini vision call failed: %s", _safe_err(e))
        return None


def _telegram_download_photo(file_id: str) -> tuple[bytes, str] | None:
    """Resolve a Telegram file_id → raw image bytes + mime type. Returns None
    on failure. Mime is inferred from the path extension since Telegram does
    not return Content-Type for the file CDN. The bot token is part of the
    URL, so failures are logged via _safe_err to avoid leaking the token."""
    try:
        gf = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile",
            params={"file_id": file_id}, timeout=10,
        )
        gf.raise_for_status()
        file_path = gf.json()["result"]["file_path"]
        dl = requests.get(
            f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}",
            timeout=30,
        )
        dl.raise_for_status()
        ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else "jpg"
        mime = "image/png" if ext == "png" else "image/jpeg"
        return dl.content, mime
    except (requests.RequestException, KeyError) as e:
        logger.warning("Telegram getFile/download failed for file_id=%s: %s",
                       file_id, _safe_err(e))
        return None


# Bounded executor for photo-parsing jobs. Each job blocks for up to ~30s on
# the vision call, so an unbounded thread-per-photo pattern (as we use for
# /commands) would let a burst of forwards exhaust sockets/memory. Two
# workers is enough for human-paced forwarding; excess photos queue here.
_photo_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="photo")


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
        "/top30":    handle_top30_command,
        "/signal":   handle_signal_command,
        "/stats":    handle_stats_command,
        "/trade":    handle_trade_command,
        "/mytrades":   handle_mytrades_command,
        "/positions":  handle_positions_command,
        "/analyze":  handle_analyze_command,
        "/silence":  handle_silence_command,
        "/unmute":   handle_unmute_command,
    }
    offset: int | None = None
    logger.info("Telegram command polling started")
    while True:
        try:
            params: dict = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
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
                cb = update.get("callback_query")
                if cb:
                    threading.Thread(
                        target=lambda c=cb: handle_callback_query(c), daemon=True
                    ).start()
                    continue
                message = update.get("message", {})
                chat_id = message.get("chat", {}).get("id")
                if not chat_id:
                    continue
                # Photo branch: forwarded Binance share-card screenshots are
                # parsed by Gemini Vision and routed through /trade analysis.
                # Uses a bounded executor (2 workers) so a burst of forwards
                # can't exhaust resources — each parse blocks ~30s on vision.
                if message.get("photo"):
                    _photo_executor.submit(handle_trade_photo, chat_id, message)
                    continue
                raw_text = (message.get("text") or "").strip()
                text = raw_text.lower()
                if not text:
                    continue
                for cmd, handler in COMMANDS.items():
                    # Exact match — the command must be the whole word, optionally
                    # followed by whitespace + args. Prevents '/trader' matching '/trade'.
                    if text == cmd or text.startswith(cmd + " "):
                        logger.info("Command %s from chat_id=%s", cmd, chat_id)
                        # Commands that take args receive the full raw text;
                        # the rest are called with just chat_id (backwards-compat).
                        takes_args = cmd in ("/trade", "/analyze")
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
    if os.environ.get("BOT_POLLING_DISABLED", "").strip().lower() in {"1", "true", "yes"}:
        logger.info("Telegram polling disabled (BOT_POLLING_DISABLED=true) — running in send-only mode")
        return
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

def _load_bot_state() -> dict:
    """Read the persisted bot state file. Returns {} if missing/broken so a
    corrupted state file can never block startup."""
    try:
        with open(BOT_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {}


_bot_state_last_write = 0.0
_BOT_STATE_DEBOUNCE_SEC = 60.0


def _save_bot_state(updates: dict) -> None:
    """Merge `updates` into the persisted state file. Best-effort; failures
    are logged but never raised — losing this file just means the next boot
    starts from defaults. Debounced to once per minute so pathological host
    flapping can't produce a thundering herd of small writes."""
    global _bot_state_last_write
    now = time.time()
    if now - _bot_state_last_write < _BOT_STATE_DEBOUNCE_SEC:
        return
    try:
        current = _load_bot_state()
        current.update(updates)
        tmp = BOT_STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(current, f)
        os.replace(tmp, BOT_STATE_PATH)
        _bot_state_last_write = now
    except OSError as e:
        logger.warning("Could not persist bot state: %s", e)


def _binance_get(path: str, params: dict | None = None, timeout: int = 15):
    """Try each Binance host in turn; persist the last working host both in
    memory (for this process) and on disk (so restarts don't redo the 4s
    451-carousel on every boot)."""
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
                _save_bot_state({"binance_base": host})
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


_CRSI_KLINES_CACHE: dict[str, tuple[float, list[float]]] = {}
_CRSI_KLINES_CACHE_TTL = 1800  # 30 min — daily closes don't change intraday


def _fetch_daily_closes_for_crsi(symbol: str) -> list[float] | None:
    """Cached daily-close fetcher for CRSI. Returns None on error or
    insufficient candles."""
    now = time.time()
    cached = _CRSI_KLINES_CACHE.get(symbol)
    if cached and now - cached[0] < _CRSI_KLINES_CACHE_TTL:
        return cached[1]
    try:
        resp = _binance_get(
            "/api/v3/klines",
            params={"symbol": symbol, "interval": "1d", "limit": CRSI_KLINES_LIMIT},
            timeout=10,
        )
        candles = resp.json()
    except Exception as e:
        logger.warning("CRSI daily klines fetch failed for %s: %s", symbol, e)
        return None
    if not candles or len(candles) < 30:
        return None
    closes = [float(k[4]) for k in candles]
    _CRSI_KLINES_CACHE[symbol] = (now, closes)
    return closes


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


def _wilder_rsi(values: list[float], period: int) -> float | None:
    """Wilder-smoothed RSI returning the latest value. Used for CRSI's short
    sub-periods (3 and 2) where the simple `_calculate_rsi` (which only uses
    the first `period` deltas) would be too coarse."""
    n = len(values)
    if n < period + 1:
        return None
    arr = np.asarray(values, dtype=float)
    deltas = np.diff(arr)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + float(gains[i])) / period
        avg_loss = (avg_loss * (period - 1) + float(losses[i])) / period
    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0
    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi if math.isfinite(rsi) else None


def _calculate_crsi(
    closes: list[float], rsi_p: int = 3, streak_p: int = 2, rank_p: int = 100,
) -> float | None:
    """Connors RSI = mean of three components: RSI(close, 3), RSI(streak, 2),
    and PercentRank of today's 1-day return over the prior 100 days.
    Returns None if there are not enough candles."""
    n = len(closes)
    if n < rank_p + 2:
        return None
    rsi_close = _wilder_rsi(closes, rsi_p)
    # Up/down streak series (signed run length).
    streaks: list[float] = [0.0]
    for i in range(1, n):
        prev = streaks[-1]
        if closes[i] > closes[i - 1]:
            streaks.append(prev + 1.0 if prev > 0 else 1.0)
        elif closes[i] < closes[i - 1]:
            streaks.append(prev - 1.0 if prev < 0 else -1.0)
        else:
            streaks.append(0.0)
    rsi_streak = _wilder_rsi(streaks, streak_p)
    # PercentRank of today's 1-day return over the previous `rank_p` days.
    arr = np.asarray(closes, dtype=float)
    prevs = arr[:-1]
    if np.any(prevs <= 0):
        return None
    returns = (arr[1:] - prevs) / prevs
    today = float(returns[-1])
    window = returns[-(rank_p + 1):-1]
    if len(window) < rank_p:
        return None
    rank_pct = float(np.sum(window < today)) / len(window) * 100.0
    if rsi_close is None or rsi_streak is None:
        return None
    crsi = (rsi_close + rsi_streak + rank_pct) / 3.0
    return crsi if math.isfinite(crsi) else None


def _calculate_ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    arr = np.asarray(values, dtype=float)
    ema = float(arr[:period].mean())  # SMA seed
    multiplier = 2.0 / (period + 1)
    for v in arr[period:]:
        ema = (float(v) - ema) * multiplier + ema
    return float(ema)


def _calculate_atr(
    highs: list[float], lows: list[float], closes: list[float], period: int = 14,
) -> float | None:
    """Wilder's ATR. Returns absolute price units (same as price)."""
    n = len(closes)
    if n < period + 1 or len(highs) != n or len(lows) != n:
        return None
    trs: list[float] = []
    for i in range(1, n):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return None
    arr = np.asarray(trs, dtype=float)
    atr = float(arr[:period].mean())
    for v in arr[period:]:
        atr = (atr * (period - 1) + float(v)) / period
    if not math.isfinite(atr) or atr <= 0:
        return None
    return atr


def _calculate_supertrend(
    highs: list[float], lows: list[float], closes: list[float],
    period: int = 10, multiplier: float = 3.0,
) -> str | None:
    """Supertrend indicator on 4h candles. Returns 'UP' (bullish) or 'DOWN' (bearish).
    Uses Wilder-smoothed ATR and standard band-flip logic."""
    n = len(closes)
    if n < period + 2:
        return None
    # True Range series (indexed from 1)
    trs: list[float] = []
    for i in range(1, n):
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    if len(trs) < period:
        return None
    # Wilder-smoothed ATR
    atr_s: list[float] = [sum(trs[:period]) / period]
    for tr in trs[period:]:
        atr_s.append((atr_s[-1] * (period - 1) + tr) / period)
    # atr_s[i] corresponds to candle index (period + i)
    final_ub: list[float] = [0.0] * n
    final_lb: list[float] = [0.0] * n
    direction: list[bool] = [True] * n   # True = UP (bullish)
    for i in range(period, n):
        atr_i = atr_s[i - period]
        hl2 = (highs[i] + lows[i]) / 2.0
        basic_ub = hl2 + multiplier * atr_i
        basic_lb = hl2 - multiplier * atr_i
        if i == period:
            final_ub[i] = basic_ub
            final_lb[i] = basic_lb
            direction[i] = closes[i] >= final_lb[i]
        else:
            prev_ub = final_ub[i - 1]
            prev_lb = final_lb[i - 1]
            # Upper band: lock down only when previous close was below it
            final_ub[i] = min(basic_ub, prev_ub) if closes[i - 1] <= prev_ub else basic_ub
            # Lower band: lock up only when previous close was above it
            final_lb[i] = max(basic_lb, prev_lb) if closes[i - 1] >= prev_lb else basic_lb
            if direction[i - 1]:                     # was UP
                direction[i] = closes[i] >= final_lb[i]   # flip to DOWN if below lower band
            else:                                    # was DOWN
                direction[i] = closes[i] > final_ub[i]    # flip to UP if above upper band
    return "UP" if direction[-1] else "DOWN"


def _calculate_daily_sigma(closes: list[float]) -> float | None:
    """Std of daily % returns derived from 4h closes (every 6 candles = 1 day).
    Returns a unitless fraction (e.g. 0.045 = 4.5% daily σ).
    Sampling is anchored on the LATEST close so the most recent regime is
    represented; we stride backward by 6 and reverse to get chronological order."""
    if len(closes) < 13:
        return None
    # Reverse-stride keeps the final close as the anchor; cap at 30 daily samples.
    sampled = closes[::-6][::-1]
    if len(sampled) > 30:
        sampled = sampled[-30:]
    if len(sampled) < 8:
        return None
    rets: list[float] = []
    for i in range(1, len(sampled)):
        prev = sampled[i - 1]
        if prev <= 0:
            continue
        r = (sampled[i] - prev) / prev
        if math.isfinite(r):
            rets.append(r)
    if len(rets) < 7:
        return None
    sigma = float(np.std(rets, ddof=1))
    if not math.isfinite(sigma) or sigma <= 0:
        return None
    return sigma


def get_4h_stats(symbol: str) -> dict | None:
    """Fetch 4h klines once; derive EMA-200, ATR-14, and approximate daily σ
    from the same data. Returns a dict or None on failure."""
    try:
        resp = _binance_get(
            "/api/v3/klines",
            params={"symbol": symbol, "interval": "4h", "limit": EMA200_FETCH_LIMIT},
            timeout=10,
        )
        raw = resp.json()
        if len(raw) < EMA200_PERIOD:
            return None
        highs = [float(k[2]) for k in raw]
        lows = [float(k[3]) for k in raw]
        closes = [float(k[4]) for k in raw]
        ema = _calculate_ema(closes, EMA200_PERIOD)
        if ema is None:
            return None
        atr = _calculate_atr(highs, lows, closes, 14)
        sigma = _calculate_daily_sigma(closes)
        return {"ema200": ema, "atr": atr, "sigma_daily": sigma}
    except Exception as e:
        logger.warning("4h stats fetch failed for %s: %s", symbol, e)
        return None


def get_ema200_4h(symbol: str) -> float | None:
    stats = get_4h_stats(symbol)
    return stats["ema200"] if stats else None


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
    """Refresh EMA-200, ATR-14, and daily σ for the given symbols (single
    kline fetch per symbol). Kept under the historical name to preserve call
    sites."""
    updated = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(get_4h_stats, s): s for s in symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                stats = future.result()
            except Exception:
                stats = None
            if not stats:
                continue
            with state_lock:
                state["ema200_4h"][symbol] = stats["ema200"]
                if stats.get("atr") is not None:
                    state["atr_4h"][symbol] = stats["atr"]
                if stats.get("sigma_daily") is not None:
                    state["sigma_daily"][symbol] = stats["sigma_daily"]
            updated += 1
    with state_lock:
        state["last_ema200_refresh"] = time.time()
    logger.info("Refreshed 4h stats (EMA/ATR/σ) for %d/%d symbols", updated, len(symbols))
    return updated


# ---------------------------------------------------------------------------
# Market regime (BTC daily EMA-200: BULL / NEUTRAL / BEAR)
# ---------------------------------------------------------------------------

# Signals that should never fire regardless of regime (pure noise).
# Note: NEUTRAL/WATCH recommendations are already blocked by send_alert_with_log;
# this set covers alert_type names that are directional but proven useless.
BLOCKED_SIGNALS: set[str] = {"confluence_neutral"}

_REGIME_TTL = 3600  # refresh at most once per hour


def get_market_regime() -> str:
    """Return the current macro market regime based on BTC daily EMA-200.

    BULL   — BTC spot price > EMA-200*(1+1.5%) AND EMA-200 rising vs 10 bars ago
    BEAR   — BTC spot price < EMA-200*(1-1.5%) AND EMA-200 falling vs 10 bars ago
    NEUTRAL — anything else (sideways / insufficient data)

    Result is cached in `state` for _REGIME_TTL seconds.
    """
    with state_lock:
        cached_val = state["market_regime"]
        cached_ts  = state["market_regime_ts"]

    if time.time() - cached_ts < _REGIME_TTL:
        return cached_val

    try:
        resp = _binance_get(
            "/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "1d", "limit": 300},
            timeout=10,
        )
        data   = resp.json()
        closes = [float(c[4]) for c in data]
        if len(closes) < 12:
            return cached_val  # not enough data — keep last known

        ema   = closes[0]
        k     = 2.0 / (200 + 1)
        for price in closes[1:]:
            ema = price * k + ema * (1 - k)
            # note: we need the full series for ema_prev; rebuild below

        # Full EMA-200 series
        ema_s = [closes[0]]
        for price in closes[1:]:
            ema_s.append(price * k + ema_s[-1] * (1 - k))

        price_now = closes[-1]
        ema_now   = ema_s[-1]
        ema_prev  = ema_s[-10]
        buffer    = 0.015

        if price_now > ema_now * (1 + buffer) and ema_now > ema_prev:
            regime = "BULL"
        elif price_now < ema_now * (1 - buffer) and ema_now < ema_prev:
            regime = "BEAR"
        else:
            regime = "NEUTRAL"

        with state_lock:
            state["market_regime"]    = regime
            state["market_regime_ts"] = time.time()
        logger.info("Market regime updated: %s (BTC=%.0f EMA200=%.0f)", regime, price_now, ema_now)
        return regime
    except Exception as e:
        logger.warning("get_market_regime failed: %s", e)
        return cached_val  # stale value is safer than crashing


def get_regime_label(alert_type: str, recommendation: str) -> tuple[bool, str]:
    """Return (should_send, label_text) for an outgoing alert.

    should_send is False only for explicitly blocked signal types.
    For all others: send but append a short regime context line.
    """
    if alert_type in BLOCKED_SIGNALS:
        return (False, "")

    regime = get_market_regime()

    aligned = (
        (regime == "BULL"  and recommendation == "LONG") or
        (regime == "BEAR"  and recommendation == "SHORT")
    )
    against = (
        (regime == "BULL"  and recommendation == "SHORT") or
        (regime == "BEAR"  and recommendation == "LONG")
    )

    if aligned:
        label = f"🌐 Фаза рынка: {regime} ✅ (по тренду)"
    elif against:
        label = f"🌐 Фаза рынка: {regime} ⚠️ (против тренда — повышенный риск)"
    else:
        label = f"🌐 Фаза рынка: {regime} ➖ (боковик)"

    return (True, label)


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
                price_3m  REAL,
                price_5m  REAL,
                price_15m REAL,
                price_1h  REAL,
                price_4h  REAL
            )
        """)
        _db_conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts)")
        # Migrate: add columns on existing alerts table if missing
        for _col, _coltype in (
            ("score",    "INTEGER"),
            ("price_3m", "REAL"),
            ("price_5m", "REAL"),
        ):
            try:
                _db_conn.execute(f"ALTER TABLE alerts ADD COLUMN {_col} {_coltype}")
            except sqlite3.OperationalError:
                pass
        _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS hidden_items (
                kind TEXT NOT NULL,
                value TEXT NOT NULL,
                ts INTEGER NOT NULL,
                PRIMARY KEY (kind, value)
            )
        """)
        _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS alert_feedback (
                alert_id INTEGER PRIMARY KEY,
                vote INTEGER NOT NULL,
                ts INTEGER NOT NULL
            )
        """)
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
        _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS coingecko_baseline (
                coin_id TEXT PRIMARY KEY
            )
        """)
        _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS position_monitors (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_id    INTEGER,
                ts_open     INTEGER NOT NULL,
                symbol      TEXT    NOT NULL,
                direction   TEXT    NOT NULL,
                entry_price REAL    NOT NULL,
                sl_price    REAL    NOT NULL,
                tp_price    REAL    NOT NULL,
                status      TEXT    NOT NULL DEFAULT 'open',
                ts_close    INTEGER,
                exit_price  REAL,
                pnl_pct     REAL
            )
        """)
        _db_conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_posmon_status "
            "ON position_monitors(status)"
        )
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
    """Back-fill follow-up prices for past alerts whose time windows have
    elapsed, using the just-fetched tickers map. Cheap to call every cycle.
    Supports five snapshot horizons: 3м / 5м / 15м / 1ч / 4ч.
    Note: 3m/5m prices are captured at the first cycle tick *after* the
    threshold passes (≈ 3–10 min after the signal), which is a known
    approximation inherent in the 5-min polling architecture.
    """
    if not tickers:
        return
    now_ts = int(time.time())
    c3m  = now_ts - HIT_RATE_INTERVALS[0][0]   # 180 s
    c5m  = now_ts - HIT_RATE_INTERVALS[1][0]   # 300 s
    c15  = now_ts - HIT_RATE_INTERVALS[2][0]   # 900 s
    c1h  = now_ts - HIT_RATE_INTERVALS[3][0]   # 3 600 s
    c4h  = now_ts - HIT_RATE_INTERVALS[4][0]   # 14 400 s
    retain_cutoff = now_ts - HIT_RATE_RETENTION_DAYS * 86400
    try:
        with _db_lock:
            conn = _get_db()
            rows = conn.execute(
                "SELECT id, symbol, price_3m, price_5m, price_15m, price_1h, price_4h, ts "
                "FROM alerts "
                "WHERE (price_3m  IS NULL AND ts <= ?) "
                "   OR (price_5m  IS NULL AND ts <= ?) "
                "   OR (price_15m IS NULL AND ts <= ?) "
                "   OR (price_1h  IS NULL AND ts <= ?) "
                "   OR (price_4h  IS NULL AND ts <= ?)",
                (c3m, c5m, c15, c1h, c4h),
            ).fetchall()
            updates: list[tuple] = []
            for row_id, symbol, p3m, p5m, p15, p1h, p4h, ts in rows:
                t = tickers.get(symbol)
                if not t:
                    continue
                try:
                    cur = float(t["lastPrice"])
                except (ValueError, KeyError):
                    continue
                new_p3m  = cur if (p3m  is None and ts <= c3m)  else p3m
                new_p5m  = cur if (p5m  is None and ts <= c5m)  else p5m
                new_p15  = cur if (p15  is None and ts <= c15)  else p15
                new_p1h  = cur if (p1h  is None and ts <= c1h)  else p1h
                new_p4h  = cur if (p4h  is None and ts <= c4h)  else p4h
                updates.append((new_p3m, new_p5m, new_p15, new_p1h, new_p4h, row_id))
            if updates:
                conn.executemany(
                    "UPDATE alerts SET "
                    "price_3m=?, price_5m=?, price_15m=?, price_1h=?, price_4h=? "
                    "WHERE id=?",
                    updates,
                )
            conn.execute("DELETE FROM alerts WHERE ts < ?", (retain_cutoff,))
            conn.commit()
        if updates:
            logger.info("Hit-rate: filled %d follow-up prices", len(updates))
    except Exception as e:
        logger.warning("fill_alert_followups failed: %s", e)


# ---------------------------------------------------------------------------
# User preferences (hidden types/symbols) + alert feedback
# ---------------------------------------------------------------------------

_prefs_lock = threading.Lock()
_hidden_types: set[str] = set()
_hidden_symbols: set[str] = set()
_prefs_loaded = False


def _load_prefs() -> None:
    global _prefs_loaded
    try:
        with _db_lock:
            conn = _get_db()
            rows = conn.execute("SELECT kind, value FROM hidden_items").fetchall()
        with _prefs_lock:
            _hidden_types.clear()
            _hidden_symbols.clear()
            for kind, value in rows:
                if kind == "type":
                    _hidden_types.add(value)
                elif kind == "symbol":
                    _hidden_symbols.add(value)
            _prefs_loaded = True
    except Exception as e:
        logger.warning("_load_prefs failed: %s", e)


def _ensure_prefs_loaded() -> None:
    if not _prefs_loaded:
        _load_prefs()


def is_hidden(alert_type: str, symbol: str) -> bool:
    _ensure_prefs_loaded()
    with _prefs_lock:
        return alert_type in _hidden_types or symbol in _hidden_symbols


def add_hidden(kind: str, value: str) -> bool:
    """Returns True if it was newly added, False if already hidden."""
    try:
        with _db_lock:
            conn = _get_db()
            cur = conn.execute(
                "INSERT OR IGNORE INTO hidden_items (kind, value, ts) VALUES (?, ?, ?)",
                (kind, value, int(time.time())),
            )
            conn.commit()
            added = cur.rowcount > 0
        with _prefs_lock:
            if kind == "type":
                _hidden_types.add(value)
            elif kind == "symbol":
                _hidden_symbols.add(value)
        return added
    except Exception as e:
        logger.warning("add_hidden failed: %s", e)
        return False


def remove_hidden(kind: str, value: str) -> bool:
    try:
        with _db_lock:
            conn = _get_db()
            cur = conn.execute(
                "DELETE FROM hidden_items WHERE kind=? AND value=?",
                (kind, value),
            )
            conn.commit()
            removed = cur.rowcount > 0
        with _prefs_lock:
            if kind == "type":
                _hidden_types.discard(value)
            elif kind == "symbol":
                _hidden_symbols.discard(value)
        return removed
    except Exception as e:
        logger.warning("remove_hidden failed: %s", e)
        return False


def list_hidden() -> tuple[list[str], list[str]]:
    _ensure_prefs_loaded()
    with _prefs_lock:
        return sorted(_hidden_types), sorted(_hidden_symbols)


def record_feedback(alert_id: int, vote: int) -> None:
    try:
        with _db_lock:
            conn = _get_db()
            conn.execute(
                "INSERT OR REPLACE INTO alert_feedback (alert_id, vote, ts) VALUES (?, ?, ?)",
                (alert_id, 1 if vote > 0 else -1, int(time.time())),
            )
            conn.commit()
    except Exception as e:
        logger.warning("record_feedback failed: %s", e)


# ---------------------------------------------------------------------------
# Signal scoring
# ---------------------------------------------------------------------------

_WINRATE_CACHE: dict[tuple[str, str], tuple[float, float | None]] = {}
_WINRATE_CACHE_TTL = 300  # 5 minutes


def _winrate_for(alert_type: str, side: str) -> float | None:
    """Historical win-rate at 4h horizon for (alert_type, side) over last 30d.
    Returns None if there is not enough data."""
    if side not in ("buy", "sell"):
        return None
    now = time.time()
    cached = _WINRATE_CACHE.get((alert_type, side))
    if cached and now - cached[0] < _WINRATE_CACHE_TTL:
        return cached[1]
    try:
        with _db_lock:
            conn = _get_db()
            since = int(now) - 30 * 86400
            until = int(now) - 4 * 3600
            rows = conn.execute(
                "SELECT recommendation, price_at_alert, price_4h FROM alerts "
                "WHERE alert_type=? AND ts >= ? AND ts <= ? AND price_4h IS NOT NULL",
                (alert_type, since, until),
            ).fetchall()
    except Exception as e:
        logger.warning("_winrate_for failed: %s", e)
        return None
    wins = total = 0
    for rec, p0, p4 in rows:
        if not p0 or p0 <= 0:
            continue
        row_side = _classify_side(alert_type, rec)
        if row_side != side:
            continue
        change = (p4 - p0) / p0
        signed = change if side == "buy" else -change
        total += 1
        if signed > 0:
            wins += 1
    wr = (wins / total) if total >= 8 else None
    _WINRATE_CACHE[(alert_type, side)] = (now, wr)
    return wr


_FEEDBACK_CACHE: dict[tuple[str, str], tuple[float, float | None, int]] = {}
_FEEDBACK_CACHE_TTL = 300


def _feedback_score(alert_type: str, side: str) -> tuple[float | None, int]:
    """Net user-feedback score in [-1, +1] for this (alert_type, side) over
    the last 30d, plus the number of votes. Side-aware so that votes on LONG
    setups don't bias SHORT scores for mixed-direction types (e.g. confluence).
    Returns (None, n) if fewer than 5 votes."""
    now = time.time()
    key = (alert_type, side)
    cached = _FEEDBACK_CACHE.get(key)
    if cached and now - cached[0] < _FEEDBACK_CACHE_TTL:
        return (cached[1], cached[2])
    # Map our internal 'side' to the recommendation strings persisted in `alerts`.
    rec_for_side = {"buy": "LONG", "sell": "SHORT"}.get(side)
    if rec_for_side is None:
        _FEEDBACK_CACHE[key] = (now, None, 0)
        return (None, 0)
    try:
        with _db_lock:
            conn = _get_db()
            since = int(now) - 30 * 86400
            rows = conn.execute(
                "SELECT f.vote FROM alert_feedback f "
                "JOIN alerts a ON a.id = f.alert_id "
                "WHERE a.alert_type=? AND a.recommendation=? AND f.ts >= ?",
                (alert_type, rec_for_side, since),
            ).fetchall()
    except Exception as e:
        logger.warning("_feedback_score failed: %s", e)
        return (None, 0)
    n = len(rows)
    if n < 5:
        result: float | None = None
    else:
        votes = [1 if int(r[0]) > 0 else -1 for r in rows]
        result = sum(votes) / n
    _FEEDBACK_CACHE[key] = (now, result, n)
    return (result, n)


def compute_signal_score(
    alert_type: str,
    side: str,
    *,
    rsi: float | None = None,
    above_ema: bool | None = None,
    spike_ratio: float | None = None,
    pct24: float | None = None,
    btc_pct24: float | None = None,
) -> int:
    """Return 0-100 score for an alert. Higher = stronger setup."""
    if side not in ("buy", "sell"):
        return 50
    score = 50.0
    wr = _winrate_for(alert_type, side)
    if wr is not None:
        score += (wr - 0.5) * 60  # ±30 pts
    # User-feedback adjustment: thumbs-up / thumbs-down on this (alert_type, side)
    fb, fb_n = _feedback_score(alert_type, side)
    if fb is not None:
        # Cap influence so a streak of votes can't fully override fundamentals
        # Effective weight grows with vote count up to 20 votes.
        weight = min(1.0, fb_n / 20.0)
        score += fb * 15.0 * weight  # up to ±15 pts
    if rsi is not None:
        if side == "buy":
            score += max(-10.0, min(10.0, (50.0 - rsi) * 0.5))
        else:
            score += max(-10.0, min(10.0, (rsi - 50.0) * 0.5))
    if above_ema is not None:
        aligned = (side == "buy" and above_ema) or (side == "sell" and not above_ema)
        score += 8 if aligned else -8
    if btc_pct24 is not None:
        if side == "buy":
            score += max(-10.0, min(10.0, btc_pct24 * 1.5))
        else:
            score += max(-10.0, min(10.0, -btc_pct24 * 1.5))
    if spike_ratio is not None and spike_ratio > 1:
        score += min(10.0, (spike_ratio - 1) * 5)
    if pct24 is not None and side == "sell" and pct24 > 30:
        # extreme one-day pumps are more likely to mean-revert
        score += min(5.0, (pct24 - 30) * 0.2)
    return max(0, min(100, int(round(score))))


def _format_sl_tp(side: str, entry: float | None, atr: float | None) -> str:
    """Return a 'Стоп / Цель' line based on ATR. Empty string if not applicable.
    side: 'buy'/'long' or 'sell'/'short'. SL=1.5×ATR, TP=3×ATR, R/R 1:2."""
    if entry is None or atr is None or atr <= 0:
        return ""
    if not (math.isfinite(entry) and math.isfinite(atr) and entry > 0):
        return ""
    s = side.lower()
    if s in ("buy", "long"):
        sl = entry - 1.5 * atr
        tp = entry + 3.0 * atr
    elif s in ("sell", "short"):
        sl = entry + 1.5 * atr
        tp = entry - 3.0 * atr
    else:
        return ""
    if sl <= 0 or tp <= 0:
        return ""
    return (
        f"🛡️ Стоп: <code>${sl:,.6g}</code>  •  "
        f"🎯 Цель: <code>${tp:,.6g}</code>  •  R/R 1:2"
    )


def _vol_threshold(symbol: str, base_pct: float, k_sigma: float) -> float:
    """Volatility-adjusted percentage threshold: max(base, k·σ·100) where σ is
    the symbol's recent daily-return std. Falls back to `base_pct` when σ is
    unknown. `base_pct` may be negative (e.g. -20 for oversold) — in that case
    the returned value is also negative with the same |·| semantics."""
    with state_lock:
        sigma = state["sigma_daily"].get(symbol)
    if sigma is None or sigma <= 0:
        return base_pct
    sigma_pct = sigma * 100.0 * k_sigma
    if base_pct >= 0:
        return max(base_pct, sigma_pct)
    return min(base_pct, -sigma_pct)


def _strength_label(score: int) -> str:
    if score >= 75:
        return "сильный"
    if score >= 60:
        return "хороший"
    if score >= 45:
        return "средний"
    return "слабый"


# ---------------------------------------------------------------------------
# Alert send with inline buttons
# ---------------------------------------------------------------------------

MIN_ALERT_SCORE = int(os.environ.get("MIN_ALERT_SCORE", "60"))

# Per-type score minimums — override the global MIN_ALERT_SCORE for specific
# signal types where historical data shows a higher bar improves quality.
MIN_SCORE_BY_TYPE: dict[str, int] = {
    "overheated_24h":      70,   # only strong setups (vs global 60)
    "momentum_down_5":     65,
    "momentum_down_10":    65,
    "breakdown_short":     55,   # TEST signal — lower bar while calibrating
    "momentum_long":       55,   # TEST signal — fade SHORT: pump+10% 24h → reversal
    "new_listing_short":   55,   # TEST signal — pump→dump after new listing
}

# --- Breakdown continuation SHORT (TEST) ---
BREAKDOWN_SHORT_PCT  = -10.0   # 24h drop must be ≤ this to qualify
BREAKDOWN_RSI_MIN    = 30.0    # RSI must be above (oversold coins excluded — different setup)
BREAKDOWN_RSI_MAX    = 58.0    # RSI must be below (confirms selling pressure, not neutral)
BREAKDOWN_COOLDOWN   = 14400   # 4h cooldown per coin

# --- Momentum continuation LONG (TEST) ---
MOMENTUM_LONG_PCT    = 10.0    # 24h rise must be ≥ this to qualify
MOMENTUM_LONG_RSI_MIN = 42.0   # RSI must be above (confirms momentum, not dead-cat)
MOMENTUM_LONG_RSI_MAX = 68.0   # RSI must be below overbought (still room to grow)
MOMENTUM_LONG_COOLDOWN = 14400 # 4h cooldown per coin

# Dynamic cooldown for overheated_24h based on market regime:
# in a bull market the same coin can re-trigger quickly — use longer gap.
OVERHEATED_COOLDOWN_BY_REGIME: dict[str, int] = {
    "BULL":    8 * 3600,   # 8 h — uptrend retests are common, avoid re-firing
    "BEAR":    4 * 3600,   # 4 h — standard
    "NEUTRAL": 4 * 3600,   # 4 h — standard
}


def _binance_url(symbol: str) -> str:
    """Universal link to the USDⓈ-M Futures pair page on Binance. On iOS/Android
    with the Binance app installed, the OS hands this URL to the app and opens
    the in-app pair card; on desktop or without the app, it falls back to the
    web page. Futures URLs use the unsplit symbol form (e.g. ``MIRAUSDT``)."""
    return f"https://www.binance.com/ru/futures/{symbol.upper()}"


def _build_alert_buttons(alert_id: int, symbol: str, alert_type: str) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "↑ сработал", "callback_data": f"fb:{alert_id}:1"},
                {"text": "↓ не сработал", "callback_data": f"fb:{alert_id}:-1"},
            ],
            [
                {"text": "Скрыть пару", "callback_data": f"hs:{symbol}"},
                {"text": "Скрыть тип", "callback_data": f"ht:{alert_type}"},
                {"text": "Открыть в Binance", "url": _binance_url(symbol)},
            ],
        ]
    }


def _build_voted_buttons(alert_id: int, symbol: str, vote: int) -> dict:
    mark = "✓ Сработал" if vote > 0 else "✓ Не сработал"
    return {
        "inline_keyboard": [
            [{"text": mark, "callback_data": f"noop:{alert_id}"}],
            [{"text": "Открыть в Binance", "url": _binance_url(symbol)}],
        ]
    }


def send_alert_with_log(
    symbol: str,
    alert_type: str,
    recommendation: str | None,
    price: float | None,
    body_text: str,
    score: int | None = None,
) -> tuple[bool, int | None]:
    """Insert into alerts (if price valid), then send Telegram with inline
    buttons referencing the new id. Honors hide-type/hide-symbol prefs,
    silenced state, and MIN_ALERT_SCORE.

    Returns (delivered, alert_id). Callers should consume cooldowns whenever
    `delivered` is True, regardless of whether `alert_id` is set (it may be
    None when the row could not be logged but the message was still sent).
    `delivered` is False for hidden/silenced/below-min-score suppressions and
    for Telegram send errors, so cooldowns stay live for retry.
    """
    if is_hidden(alert_type, symbol):
        logger.info("Suppressed %s/%s (hidden by user prefs)", symbol, alert_type)
        return (False, None)
    with state_lock:
        if state["silenced"]:
            logger.info("Suppressed %s/%s (silenced): %s", symbol, alert_type, body_text[:60])
            return (False, None)
    # Only directional signals — drop NEUTRAL/WATCH so the channel stays
    # actionable. New-listing alerts use a separate sender and are unaffected.
    if recommendation not in ("LONG", "SHORT"):
        logger.info(
            "Suppressed %s/%s (recommendation=%r, only LONG/SHORT delivered)",
            symbol, alert_type, recommendation,
        )
        return (False, None)
    # Score gate — per-type minimum overrides the global floor where set.
    min_score = MIN_SCORE_BY_TYPE.get(alert_type, MIN_ALERT_SCORE)
    if score is None or score < min_score:
        logger.info(
            "Suppressed %s/%s (score=%s < min %d)",
            symbol, alert_type, score, min_score,
        )
        return (False, None)
    if price is None or price <= 0:
        # Cannot log without price; send without buttons. If it goes through,
        # consume cooldowns so we don't spam every cycle.
        ok = _telegram_send(TELEGRAM_CHAT_ID, body_text)
        return (ok, None)
    try:
        with _db_lock:
            conn = _get_db()
            cur = conn.execute(
                "INSERT INTO alerts (ts, symbol, alert_type, recommendation, "
                "price_at_alert, score) VALUES (?, ?, ?, ?, ?, ?)",
                (int(time.time()), symbol, alert_type, recommendation, float(price), score),
            )
            conn.commit()
            alert_id = int(cur.lastrowid or 0)
    except Exception as e:
        logger.warning("send_alert_with_log insert failed: %s", e)
        # Fall back to send-without-buttons. Still consume cooldowns on success
        # so we don't repeatedly retry a broken DB path every cycle.
        ok = _telegram_send(TELEGRAM_CHAT_ID, body_text)
        return (ok, None)

    markup = _build_alert_buttons(alert_id, symbol, alert_type)
    # Regime label: append market context to every outgoing alert.
    _regime_send, _regime_label = get_regime_label(alert_type, recommendation)
    if not _regime_send:
        # Blocked by BLOCKED_SIGNALS — roll back the DB row we just inserted.
        try:
            with _db_lock:
                conn = _get_db()
                conn.execute("DELETE FROM alerts WHERE id=?", (alert_id,))
                conn.commit()
        except Exception:
            pass
        return (False, None)
    if _regime_label:
        body_text = f"{body_text}\n{_regime_label}"
    # C. Leverage warning appended to every SHORT alert.
    if recommendation == "SHORT":
        body_text = (
            body_text
            + "\n\n⚠️ <i>Горизонт сигнала: 1-4 ч, ожидаемое движение 2-7%. "
            "Плечо выше 5× может привести к ликвидации раньше, "
            "чем сигнал отработает. Управляйте размером позиции.</i>"
        )
    # Duplicate the Binance link inside the message text so the user can
    # long-press → "Открыть во внешнем браузере", which lets the OS resolve
    # the universal-link and hand it to the Binance app. Inline-button taps
    # always go through Telegram's in-app browser and skip universal-links.
    body_with_link = (
        f"{body_text}\n\n"
        f"🔗 <a href=\"{_binance_url(symbol)}\">Открыть в приложении Binance</a> "
        f"<i>(зажмите ссылку → «Открыть во внешнем браузере»)</i>"
    )
    if not _telegram_send(TELEGRAM_CHAT_ID, body_with_link, reply_markup=markup):
        # Rollback so cooldowns don't suppress retries
        try:
            with _db_lock:
                conn = _get_db()
                conn.execute("DELETE FROM alerts WHERE id=?", (alert_id,))
                conn.commit()
        except Exception:
            pass
        return (False, None)
    # Launch position monitor for the delivered signal
    if price is not None and price > 0:
        _auto_start_monitor(alert_id, symbol, recommendation, float(price))
    return (True, alert_id)


def handle_callback_query(cb: dict) -> None:
    """Dispatch inline-button presses: feedback, hide-type, hide-symbol."""
    cb_id = cb.get("id")
    data = (cb.get("data") or "").strip()
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    if not cb_id or not data:
        return
    try:
        kind, _, rest = data.partition(":")
        if kind == "fb":
            aid_str, _, vote_str = rest.partition(":")
            try:
                aid = int(aid_str)
                vote = int(vote_str)
            except ValueError:
                _telegram_answer_callback(cb_id, "Неверные данные")
                return
            record_feedback(aid, vote)
            # Look up symbol for the graph button
            try:
                with _db_lock:
                    conn = _get_db()
                    row = conn.execute(
                        "SELECT symbol FROM alerts WHERE id=?", (aid,)
                    ).fetchone()
                symbol = row[0] if row else ""
            except Exception:
                symbol = ""
            if chat_id and message_id and symbol:
                _telegram_edit_markup(chat_id, message_id, _build_voted_buttons(aid, symbol, vote))
            _telegram_answer_callback(cb_id, "Спасибо! Учтено")
        elif kind == "ht":
            if rest:
                newly = add_hidden("type", rest)
                _telegram_answer_callback(
                    cb_id,
                    f"Тип «{rest}» скрыт" if newly else f"Тип «{rest}» уже скрыт",
                )
        elif kind == "hs":
            if rest:
                newly = add_hidden("symbol", rest)
                _telegram_answer_callback(
                    cb_id,
                    f"Пара {rest} скрыта" if newly else f"Пара {rest} уже скрыта",
                )
        elif kind == "noop":
            _telegram_answer_callback(cb_id)
        else:
            _telegram_answer_callback(cb_id)
    except Exception as e:
        logger.exception("handle_callback_query failed: %s", e)
        _telegram_answer_callback(cb_id, "Ошибка обработки")


def _sltp_outcome(p0: float, p_future: float, rec: str,
                  sl_pct: float = HIT_RATE_SL_PCT,
                  tp_pct: float = HIT_RATE_TP_PCT) -> str:
    """Approximate SL/TP outcome using a single close-price snapshot.

    Since we only store close prices (not OHLC) we can only detect *clear*
    hits — if the price crossed TP or SL level.  When both are crossed
    simultaneously (impossible with a single close) or neither, we return
    'timeout'.  This is an approximation; the backtest uses candle high/low
    for a stricter simulation.

    Returns: 'tp' | 'sl' | 'timeout'
    """
    if p0 <= 0:
        return "timeout"
    pct = (p_future - p0) / p0 * 100.0
    if rec == "LONG":
        if pct <= -sl_pct:
            return "sl"
        if pct >= tp_pct:
            return "tp"
    elif rec == "SHORT":
        if pct >= sl_pct:
            return "sl"
        if pct <= -tp_pct:
            return "tp"
    return "timeout"


def compute_hit_rate_stats(days: int = 7) -> list[dict]:
    """Aggregate simulated SL/TP P&L per (alert_type, recommendation) over
    the last `days` days.

    For each alert we walk through price snapshots at 15m → 1h → 4h and apply
    a fixed SL={HIT_RATE_SL_PCT}% / TP={HIT_RATE_TP_PCT}% rule:
      - 'tp'      = price crossed TP level → win, P&L = +tp_pct
      - 'sl'      = price crossed SL level → loss, P&L = -sl_pct
      - 'timeout' = neither triggered, P&L = signed close move at that checkpoint

    Each horizon is evaluated independently (not walk-through) so the stats
    answer: "if I closed the position exactly at this checkpoint, what was my P&L?"
    """
    cutoff = int(time.time()) - days * 86400
    out: list[dict] = []
    try:
        with _db_lock:
            conn = _get_db()
            cursor = conn.execute(
                "SELECT alert_type, recommendation, price_at_alert, "
                "price_3m, price_5m, price_15m, price_1h, price_4h "
                "FROM alerts WHERE ts >= ?",
                (cutoff,),
            )
            # agg[key] = {total, horizons: {label: {tp, sl, timeout, pnl_sum, n}}}
            agg: dict[tuple, dict] = {}
            sl_pct = HIT_RATE_SL_PCT
            for atype, rec, p0, p3m, p5m, p15, p1h, p4h in cursor:
                tp_pct = HIT_RATE_TP_PCT_SHORT if rec == "SHORT" else HIT_RATE_TP_PCT
                if p0 is None or p0 <= 0:
                    continue
                key = (atype, rec or "—")
                a = agg.setdefault(key, {
                    "total": 0,
                    "horizons": {
                        "3м":  {"tp": 0, "sl": 0, "timeout": 0, "pnl_sum": 0.0, "n": 0},
                        "5м":  {"tp": 0, "sl": 0, "timeout": 0, "pnl_sum": 0.0, "n": 0},
                        "15м": {"tp": 0, "sl": 0, "timeout": 0, "pnl_sum": 0.0, "n": 0},
                        "1ч":  {"tp": 0, "sl": 0, "timeout": 0, "pnl_sum": 0.0, "n": 0},
                        "4ч":  {"tp": 0, "sl": 0, "timeout": 0, "pnl_sum": 0.0, "n": 0},
                    },
                })
                a["total"] += 1
                for label, p_future in (
                    ("3м", p3m), ("5м", p5m), ("15м", p15), ("1ч", p1h), ("4ч", p4h)
                ):
                    if p_future is None:
                        continue
                    h = a["horizons"][label]
                    outcome = _sltp_outcome(p0, p_future, rec or "", sl_pct, tp_pct)
                    h[outcome] += 1
                    h["n"] += 1
                    if outcome == "tp":
                        h["pnl_sum"] += tp_pct
                    elif outcome == "sl":
                        h["pnl_sum"] -= sl_pct
                    else:
                        # timeout: use actual signed close move
                        pct = (p_future - p0) / p0 * 100.0
                        if rec == "SHORT":
                            pct = -pct
                        h["pnl_sum"] += pct
        for (atype, rec), a in agg.items():
            # Ensure all five horizon keys are always present (even if no data)
            for _lbl in ("3м", "5м", "15м", "1ч", "4ч"):
                a["horizons"].setdefault(
                    _lbl, {"tp": 0, "sl": 0, "timeout": 0, "pnl_sum": 0.0, "n": 0}
                )
            out.append({
                "alert_type":    atype,
                "recommendation": rec,
                "total":         a["total"],
                "sl_pct":        HIT_RATE_SL_PCT,
                "tp_pct":        HIT_RATE_TP_PCT_SHORT if rec == "SHORT" else HIT_RATE_TP_PCT,
                "horizons":      a["horizons"],
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

    # If this process hasn't loaded the baseline yet, try to hydrate it from
    # the SQLite store so restarts don't keep wiping the known-IDs set.
    with state_lock:
        already_initialized = state["coingecko_initialized"]

    if not already_initialized:
        try:
            with _db_lock:
                conn = _get_db()
                rows = conn.execute("SELECT coin_id FROM coingecko_baseline").fetchall()
            persisted = {r[0] for r in rows}
        except Exception as e:
            logger.warning("CoinGecko: failed to load persisted baseline: %s", e)
            persisted = set()
        if persisted:
            with state_lock:
                state["known_coingecko_ids"] = persisted
                state["coingecko_initialized"] = True
            logger.info(
                "CoinGecko: hydrated baseline from DB (%d coins) — alerts active",
                len(persisted),
            )

    with state_lock:
        known_pairs = set(state["known_pairs"])
        known_ids = set(state["known_coingecko_ids"])
        first_run = not state["coingecko_initialized"]
        state["known_coingecko_ids"] = current_ids
        state["coingecko_initialized"] = True
        state["last_coingecko_check"] = time.time()

    # Persist the freshly-fetched id set (always — keeps DB in sync with reality).
    # Use `with conn:` so a failure mid-rewrite auto-rollbacks instead of leaving
    # a dangling transaction that could later commit partial state.
    try:
        with _db_lock:
            conn = _get_db()
            with conn:
                conn.execute("DELETE FROM coingecko_baseline")
                conn.executemany(
                    "INSERT INTO coingecko_baseline (coin_id) VALUES (?)",
                    [(cid,) for cid in current_ids],
                )
    except Exception as e:
        logger.warning("CoinGecko: failed to persist baseline: %s", e)

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
    rsi_map: dict[str, float] | None = None,
) -> int:
    """For each symbol with a 15-min pct change, send a single alert at the
    highest tier crossed (e.g. +12% triggers only the +10% alert, not all 4).
    Cooldown is per (symbol, threshold-tier).
    """
    sent = 0
    ema_blocked = 0
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

        # A. Disabled tiers:
        #    momentum_down_2: 28% hit-rate, avg price +0.84% after signal (wrong dir).
        #    momentum_down_3: backtest shows TP 29%, SL 37% — net negative expectancy.
        if threshold in (-2.0, -3.0):
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

        # B. EMA-200 trend filter for momentum shorts: if the coin is above
        #    EMA-200 (4h) it is in an uptrend — don't short into the trend.
        if pct < 0:
            with state_lock:
                ema_val = state["ema200_4h"].get(symbol)
            if price is not None and ema_val is not None and price > ema_val:
                logger.debug(
                    "Momentum short suppressed by EMA-200 filter: %s price=%.6g ema=%.6g",
                    symbol, price, ema_val,
                )
                ema_blocked += 1
                continue

            # C. RSI confirmation: a momentum-down signal has better edge when
            #    RSI was elevated before the drop — confirms the fall is from a
            #    heated level, not a random wick.
            #    Threshold lowered 60→50: ≥60 was too strict (fired only 3 times ever).
            if rsi_map is not None:
                rsi_val = rsi_map.get(symbol)
                if rsi_val is not None and rsi_val < 50:
                    logger.debug(
                        "Momentum short suppressed by RSI filter: %s rsi=%.1f < 50",
                        symbol, rsi_val,
                    )
                    continue

        emoji = _MOMENTUM_EMOJI.get(threshold, "📊")
        sign = "+" if pct > 0 else ""
        rec = "LONG" if pct > 0 else "SHORT"
        kind = f"momentum_{'up' if pct > 0 else 'down'}_{int(abs(threshold))}"
        side = "buy" if pct > 0 else "sell"
        btc_t = tickers.get("BTCUSDT") if tickers else None
        btc_pct24 = None
        if btc_t:
            try:
                btc_pct24 = float(btc_t["priceChangePercent"])
            except (ValueError, KeyError, TypeError):
                btc_pct24 = None
        score = compute_signal_score(kind, side, btc_pct24=btc_pct24)
        with state_lock:
            atr = state["atr_4h"].get(symbol)
        sl_tp = _format_sl_tp(side, price, atr)
        body = (
            f"{emoji} <b><code>{symbol}</code> {sign}{pct:.1f}% за 15 минут</b>\n"
            f"💰 Цена: {price_str}\n"
            f"🎯 Сила сигнала: <b>{score}/100</b> ({_strength_label(score)})"
            + (f"\n{sl_tp}" if sl_tp else "")
        )
        delivered, _aid = send_alert_with_log(symbol, kind, rec, price, body, score)
        if not delivered:
            continue

        with state_lock:
            state["last_momentum_alerted"].setdefault(symbol, {})[threshold] = now
        sent += 1
    return sent, ema_blocked


def check_overheated_oversold(
    tickers: dict[str, dict] | None,
    rsi_map: dict[str, float],
) -> tuple[int, int, int, int]:
    """Standalone "overheated" (24h >= +20% AND RSI >= 70) and "oversold"
    (24h <= -20% AND RSI <= 30) alerts. Bypass confluence.
    Returns: sent_oh, sent_os, ema_blocked_oh, reversal_blocked_oh
    """
    if not tickers:
        return 0, 0, 0, 0
    sent_oh, sent_os, ema_blocked_oh, reversal_blocked_oh = 0, 0, 0, 0
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

        btc_t = tickers.get("BTCUSDT")
        btc_pct24 = None
        if btc_t:
            try:
                btc_pct24 = float(btc_t["priceChangePercent"])
            except (ValueError, KeyError, TypeError):
                btc_pct24 = None

        oh_threshold = _vol_threshold(symbol, OVERHEATED_24H_PCT, 2.5)
        os_threshold = _vol_threshold(symbol, OVERSOLD_24H_PCT, 2.5)
        with state_lock:
            atr = state["atr_4h"].get(symbol)

        if pct24 >= oh_threshold and rsi >= RSI_OVERBOUGHT:
            # B. EMA-200 trend filter: a coin that is overheated AND still
            #    above EMA-200 is in a strong uptrend — fading it with a
            #    short has historically low edge. Skip if in uptrend.
            with state_lock:
                ema_oh = state["ema200_4h"].get(symbol)
            if ema_oh is not None and price > ema_oh:
                logger.debug(
                    "Overheated short suppressed by EMA-200 filter: %s price=%.6g ema=%.6g",
                    symbol, price, ema_oh,
                )
                ema_blocked_oh += 1
                continue

            # Reversal confirmation: price must have pulled back ≥ 1.5% from
            # its 24h high before we short. Prevents shorting a rising coin.
            try:
                high_24h = float(t["highPrice"])
                if high_24h > 0:
                    pullback_pct = (high_24h - price) / high_24h * 100.0
                    if pullback_pct < SHORT_REVERSAL_CONFIRM_PCT:
                        logger.debug(
                            "Overheated short suppressed by reversal filter: "
                            "%s price=%.6g high=%.6g pullback=%.2f%%",
                            symbol, price, high_24h, pullback_pct,
                        )
                        reversal_blocked_oh += 1
                        continue
            except (ValueError, KeyError):
                pass

            with state_lock:
                last = state["last_overheated_alerted"].get(symbol, 0)
            regime = state.get("market_regime", "NEUTRAL")
            oh_cooldown = OVERHEATED_COOLDOWN_BY_REGIME.get(regime, OVERHEATED_COOLDOWN)
            if now - last >= oh_cooldown:
                score = compute_signal_score(
                    "overheated_24h", "sell",
                    rsi=rsi, pct24=pct24, btc_pct24=btc_pct24,
                )
                sl_tp = _format_sl_tp("sell", price, atr)
                body = (
                    f"⚠️ <b>ПЕРЕГРЕТА — возможен шорт</b>\n"
                    f"Монета выросла слишком быстро\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"<code>{symbol}</code>\n"
                    f"📈 24ч: <b>+{pct24:.1f}%</b> (порог <b>+{oh_threshold:.1f}%</b>)\n"
                    f"🔥 RSI: <b>{rsi:.1f}</b>\n"
                    f"💰 Цена: ${price:,.6g}\n"
                    f"🎯 Сила сигнала: <b>{score}/100</b> ({_strength_label(score)})"
                    + (f"\n{sl_tp}" if sl_tp else "")
                )
                delivered, _aid = send_alert_with_log(symbol, "overheated_24h", "SHORT", price, body, score)
                if delivered:
                    with state_lock:
                        state["last_overheated_alerted"][symbol] = now
                    sent_oh += 1

        elif pct24 <= os_threshold and rsi <= RSI_OVERSOLD:
            with state_lock:
                last = state["last_oversold_alerted"].get(symbol, 0)
            if now - last >= OVERSOLD_COOLDOWN:
                score = compute_signal_score(
                    "oversold_24h", "buy",
                    rsi=rsi, pct24=pct24, btc_pct24=btc_pct24,
                )
                sl_tp = _format_sl_tp("buy", price, atr)
                body = (
                    f"💎 <b>ПЕРЕПРОДАНА — возможен лонг</b>\n"
                    f"Монета упала слишком сильно\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"<code>{symbol}</code>\n"
                    f"📉 24ч: <b>{pct24:.1f}%</b> (порог <b>{os_threshold:.1f}%</b>)\n"
                    f"🧊 RSI: <b>{rsi:.1f}</b>\n"
                    f"💰 Цена: ${price:,.6g}\n"
                    f"🎯 Сила сигнала: <b>{score}/100</b> ({_strength_label(score)})"
                    + (f"\n{sl_tp}" if sl_tp else "")
                )
                delivered, _aid = send_alert_with_log(symbol, "oversold_24h", "LONG", price, body, score)
                if delivered:
                    with state_lock:
                        state["last_oversold_alerted"][symbol] = now
                    sent_os += 1
    return sent_oh, sent_os, ema_blocked_oh, reversal_blocked_oh


def check_breakdown_short(
    tickers: dict[str, dict] | None,
    rsi_map: dict[str, float],
) -> int:
    """🧪 ТЕСТ: Breakdown continuation SHORT.

    Fires when:
      - 24h drop ≤ BREAKDOWN_SHORT_PCT (−10%)  — сильное дневное движение вниз
      - Price < EMA-200 (4h)                    — подтверждённый нисходящий тренд
      - RSI in (BREAKDOWN_RSI_MIN, BREAKDOWN_RSI_MAX) = (30, 58)
          above 30 = не перепродана, ещё есть куда падать
          below 58 = давление продавцов подтверждено
      - Volume ≥ MIN_VOLUME_USDT                — ликвидная монета

    Цель: ловить монеты типа BANKUSDT, которые уже сломали структуру и
    продолжают падение — в отличие от overheated (шорт от вершины).
    Помечается [ТЕСТ] до накопления статистики.
    """
    if not tickers:
        return 0
    sent = 0
    now = time.time()

    btc_t = tickers.get("BTCUSDT")
    btc_pct24: float | None = None
    if btc_t:
        try:
            btc_pct24 = float(btc_t["priceChangePercent"])
        except (ValueError, KeyError, TypeError):
            pass

    for symbol, t in tickers.items():
        rsi = rsi_map.get(symbol)
        if rsi is None:
            continue
        try:
            pct24 = float(t["priceChangePercent"])
            price = float(t["lastPrice"])
            vol24 = float(t["quoteVolume"])
        except (ValueError, KeyError):
            continue

        # Liquidity filter
        if vol24 < MIN_VOLUME_USDT:
            continue

        # 1. Strong 24h drop
        if pct24 > BREAKDOWN_SHORT_PCT:
            continue

        # 2. RSI in "room to fall" zone — not oversold, not neutral
        if not (BREAKDOWN_RSI_MIN < rsi < BREAKDOWN_RSI_MAX):
            continue

        # 3. Price below EMA-200 — confirmed downtrend
        with state_lock:
            ema = state["ema200_4h"].get(symbol)
            atr = state["atr_4h"].get(symbol)
        if ema is None or price >= ema:
            continue

        # Cooldown
        with state_lock:
            last = state["last_breakdown_alerted"].get(symbol, 0)
        if now - last < BREAKDOWN_COOLDOWN:
            continue

        ema_pct = (ema - price) / ema * 100.0   # how far below EMA (positive = below)

        score = compute_signal_score(
            "breakdown_short", "sell",
            rsi=rsi, above_ema=False, pct24=pct24, btc_pct24=btc_pct24,
        )
        min_score = MIN_SCORE_BY_TYPE.get("breakdown_short", MIN_ALERT_SCORE)
        if score < min_score:
            logger.debug(
                "Suppressed %s/breakdown_short (score=%d < min %d)", symbol, score, min_score,
            )
            continue

        sl_tp = _format_sl_tp("sell", price, atr)
        body = (
            f"🧪 <b>[ТЕСТ] ПРОБОЙ ВНИЗ — шорт продолжается</b>\n"
            f"Монета в нисходящем тренде, импульс не исчерпан\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<code>{symbol}</code>\n"
            f"📉 24ч: <b>{pct24:.1f}%</b>\n"
            f"📊 RSI: <b>{rsi:.1f}</b> — давление продавцов, не перепродан\n"
            f"📍 Ниже EMA-200: <b>{ema_pct:.1f}%</b>\n"
            f"💰 Цена: <b>${price:,.6g}</b>\n"
            f"🎯 Сила: <b>{score}/100</b> ({_strength_label(score)})"
            + (f"\n{sl_tp}" if sl_tp else "")
        )
        delivered, _aid = send_alert_with_log(
            symbol, "breakdown_short", "SHORT", price, body, score,
        )
        if delivered:
            with state_lock:
                state["last_breakdown_alerted"][symbol] = now
            logger.info("Breakdown SHORT alert sent: %s pct24=%.1f rsi=%.1f", symbol, pct24, rsi)
            sent += 1

    return sent


def check_momentum_long(
    tickers: dict[str, dict] | None,
    rsi_map: dict[str, float],
) -> int:
    """🧪 ТЕСТ: Momentum fade SHORT («памп иссякает»).

    Статистика показала: при росте +10% за 24ч с RSI 42-68 выше EMA-200
    цена стабильно падает после сигнала — значит эти условия предсказывают
    разворот, а не продолжение. Инвертируем в SHORT.

    Условия входа (прежние — статистически работают против лонга):
      - 24h рост ≥ MOMENTUM_LONG_PCT (+10%)    — монета уже сделала ход
      - Price > EMA-200 (4h)                   — памп был сильным
      - RSI in (MOMENTUM_LONG_RSI_MIN, MOMENTUM_LONG_RSI_MAX) = (42, 68)
          не перекуплена (RSI<68), но импульс уже есть (RSI>42) → fade-зона
      - Цена ≥ 0.75% ниже 24ч-максимума        — первый откат подтверждён
    """
    if not tickers:
        return 0
    sent = 0
    now = time.time()

    btc_t = tickers.get("BTCUSDT")
    btc_pct24: float | None = None
    if btc_t:
        try:
            btc_pct24 = float(btc_t["priceChangePercent"])
        except (ValueError, KeyError, TypeError):
            pass

    for symbol, t in tickers.items():
        rsi = rsi_map.get(symbol)
        if rsi is None:
            continue
        try:
            pct24 = float(t["priceChangePercent"])
            price = float(t["lastPrice"])
            vol24 = float(t["quoteVolume"])
        except (ValueError, KeyError):
            continue

        # Liquidity filter
        if vol24 < MIN_VOLUME_USDT:
            continue

        # 1. Strong 24h rise
        if pct24 < MOMENTUM_LONG_PCT:
            continue

        # 2. RSI in "room to grow" zone — momentum confirmed, not overbought
        if not (MOMENTUM_LONG_RSI_MIN < rsi < MOMENTUM_LONG_RSI_MAX):
            continue

        # 3. Price above EMA-200 — confirmed uptrend
        with state_lock:
            ema = state["ema200_4h"].get(symbol)
            atr = state["atr_4h"].get(symbol)
        if ema is None or price <= ema:
            continue

        # Reversal confirmation: price must be ≥ 0.75% below 24h high
        # (same filter as overheated/confluence SHORTs)
        high_24h = float(t.get("highPrice", 0) or 0)
        if high_24h > 0:
            pullback_pct = (high_24h - price) / high_24h * 100.0
            if pullback_pct < SHORT_REVERSAL_CONFIRM_PCT:
                logger.debug(
                    "%s/momentum_long SHORT blocked: pullback=%.2f%% < %.2f%% (still near peak)",
                    symbol, pullback_pct, SHORT_REVERSAL_CONFIRM_PCT,
                )
                continue

        # Cooldown
        with state_lock:
            last = state["last_momentum_long_alerted"].get(symbol, 0)
        if now - last < MOMENTUM_LONG_COOLDOWN:
            continue

        ema_pct = (price - ema) / ema * 100.0   # how far above EMA (positive = above)

        score = compute_signal_score(
            "momentum_long", "sell",
            rsi=rsi, above_ema=True, pct24=pct24, btc_pct24=btc_pct24,
        )
        min_score = MIN_SCORE_BY_TYPE.get("momentum_long", MIN_ALERT_SCORE)
        if score < min_score:
            logger.debug(
                "Suppressed %s/momentum_long (score=%d < min %d)", symbol, score, min_score,
            )
            continue

        sl_tp = _format_sl_tp("sell", price, atr)
        body = (
            f"🧪 <b>[ТЕСТ] ПАМП ИССЯКАЕТ — шорт на развороте</b>\n"
            f"Монета выросла +{pct24:.1f}% за 24ч, но начинает откат\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<code>{symbol}</code>\n"
            f"📉 24ч: <b>+{pct24:.1f}%</b> → откат подтверждён\n"
            f"📊 RSI: <b>{rsi:.1f}</b> — fade-зона, импульс гаснет\n"
            f"📍 Выше EMA-200: <b>+{ema_pct:.1f}%</b> (перерасширение)\n"
            f"💰 Цена: <b>${price:,.6g}</b>\n"
            f"🎯 Сила: <b>{score}/100</b> ({_strength_label(score)})"
            + (f"\n{sl_tp}" if sl_tp else "")
        )
        delivered, _aid = send_alert_with_log(
            symbol, "momentum_long", "SHORT", price, body, score,
        )
        if delivered:
            with state_lock:
                state["last_momentum_long_alerted"][symbol] = now
            logger.info("Momentum fade SHORT sent: %s pct24=%.1f rsi=%.1f", symbol, pct24, rsi)
            sent += 1

    return sent


def check_new_listing_pumps(
    tickers: dict[str, dict] | None,
    rsi_map: dict[str, float],
) -> int:
    """🧪 ТЕСТ: New listing pump→dump SHORT.

    Паттерн: новые монеты часто делают сильный памп при листинге,
    после чего следует дамп. Сигнал срабатывает когда:
      - Монета листалась в последние NEW_LISTING_WINDOW_H часов
      - Текущая цена ≥ +NEW_LISTING_PUMP_PCT от цены листинга (+20%)
      - RSI ≥ NEW_LISTING_PUMP_RSI_MIN (65) — перегрев подтверждён
    Данные о листинге берутся из таблицы alerts (alert_type='new_listing').
    """
    if not tickers:
        return 0
    sent = 0
    now = time.time()
    cutoff = int(now) - NEW_LISTING_WINDOW_H * 3600

    # Fetch recently listed coins and their listing prices from DB
    try:
        with _db_lock:
            conn = _get_db()
            listings = conn.execute(
                "SELECT symbol, price_at_alert, ts FROM alerts "
                "WHERE alert_type='new_listing' AND ts >= ? AND price_at_alert > 0",
                (cutoff,),
            ).fetchall()
    except Exception as exc:
        logger.warning("check_new_listing_pumps DB query failed: %s", exc)
        return 0

    if not listings:
        return 0

    btc_t = tickers.get("BTCUSDT")
    btc_pct24: float | None = None
    if btc_t:
        try:
            btc_pct24 = float(btc_t["priceChangePercent"])
        except (ValueError, KeyError, TypeError):
            pass

    for symbol, listing_price, listing_ts in listings:
        if listing_price is None or listing_price <= 0:
            continue

        t = tickers.get(symbol)
        if t is None:
            continue
        try:
            price = float(t["lastPrice"])
            vol24 = float(t["quoteVolume"])
        except (ValueError, KeyError):
            continue

        rsi = rsi_map.get(symbol)
        if rsi is None:
            continue

        # 1. Coin pumped ≥ +20% above listing price
        pump_pct = (price - listing_price) / listing_price * 100.0
        if pump_pct < NEW_LISTING_PUMP_PCT:
            continue

        # 2. RSI confirms overheating
        if rsi < NEW_LISTING_PUMP_RSI_MIN:
            continue

        # 3. Minimum liquidity
        if vol24 < MIN_VOLUME_USDT:
            continue

        # Cooldown
        with state_lock:
            last = state["last_new_listing_short_alerted"].get(symbol, 0)
        if now - last < NEW_LISTING_SHORT_COOLDOWN:
            continue

        hours_since_listing = (now - listing_ts) / 3600.0
        with state_lock:
            atr = state["atr_4h"].get(symbol)

        score = compute_signal_score(
            "new_listing_short", "sell",
            rsi=rsi, btc_pct24=btc_pct24,
        )
        min_score = MIN_SCORE_BY_TYPE.get("new_listing_short", MIN_ALERT_SCORE)
        if score < min_score:
            continue

        sl_tp = _format_sl_tp("sell", price, atr)
        body = (
            f"🧪 <b>[ТЕСТ] ПАМП НОВОГО ЛИСТИНГА — возможен шорт</b>\n"
            f"Монета выросла от цены листинга, возможен разворот вниз\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<code>{symbol}</code>\n"
            f"🆕 Листинг: <b>${listing_price:,.6g}</b> "
            f"({hours_since_listing:.1f}ч назад)\n"
            f"📈 Памп от листинга: <b>+{pump_pct:.1f}%</b>\n"
            f"🔥 RSI: <b>{rsi:.1f}</b> — перегрет\n"
            f"💰 Цена сейчас: <b>${price:,.6g}</b>\n"
            f"🎯 Сила: <b>{score}/100</b> ({_strength_label(score)})"
            + (f"\n{sl_tp}" if sl_tp else "")
        )
        delivered, _aid = send_alert_with_log(
            symbol, "new_listing_short", "SHORT", price, body, score,
        )
        if delivered:
            with state_lock:
                state["last_new_listing_short_alerted"][symbol] = now
            logger.info(
                "New-listing SHORT alert: %s listing=%.6g now=%.6g pump=+%.1f%% rsi=%.1f",
                symbol, listing_price, price, pump_pct, rsi,
            )
            sent += 1

    return sent


def check_volume_surge_crsi(tickers: dict[str, dict] | None) -> int:
    """Volume-spike + CRSI combo. For any pair in the cached
    state['volume_ranking'] (which is built from the broadened daily refresh,
    so coins like ORCAUSDT are eligible) where today's USDT volume is
    ≥ +VOLUME_SURGE_PCT vs yesterday's AND the daily Connors-RSI is in an
    extreme zone, fire a directional alert:
      • CRSI ≥ CRSI_OVERBOUGHT (90) → SHORT
      • CRSI ≤ CRSI_OVERSOLD   (25) → LONG
    The CRSI calc fetches daily closes lazily and caches them for 30 min, so
    only actual surge candidates pay the API cost."""
    if not tickers:
        return 0
    sent = 0
    now = time.time()
    with state_lock:
        ranking_snapshot = list(state["volume_ranking"])
        ranking_updated = state["volume_ranking_updated"]
    age_min = (now - ranking_updated) / 60.0 if ranking_updated else float("inf")
    logger.info(
        "Volume-surge scan: ranking size=%d, age=%.1f min",
        len(ranking_snapshot), age_min,
    )
    # Defensive: refresh sorts desc by pct, but if a future change ever broke
    # that invariant we'd silently skip valid surge candidates after the first
    # below-threshold row. Re-sort locally so the `break` stays correct.
    if len(ranking_snapshot) >= 2:
        for a, b in zip(ranking_snapshot, ranking_snapshot[1:]):
            if a[3] < b[3]:
                logger.warning(
                    "volume_ranking not sorted desc by pct (%.2f < %.2f); "
                    "re-sorting locally for the scan",
                    a[3], b[3],
                )
                ranking_snapshot.sort(key=lambda r: r[3], reverse=True)
                break
    btc_t = tickers.get("BTCUSDT")
    btc_pct24 = None
    if btc_t:
        try:
            btc_pct24 = float(btc_t["priceChangePercent"])
        except (ValueError, KeyError, TypeError):
            btc_pct24 = None

    for symbol, yest_vol, today_vol, pct in ranking_snapshot:
        # ranking is sorted desc by pct, so we can stop at the first miss.
        if pct < VOLUME_SURGE_PCT:
            break
        if yest_vol <= 0 or today_vol <= 0:
            continue
        with state_lock:
            last = state["last_vol_surge_alerted"].get(symbol, 0)
        if now - last < VOLUME_SURGE_COOLDOWN:
            continue
        t = tickers.get(symbol)
        if not t:
            continue
        try:
            price = float(t["lastPrice"])
            pct24 = float(t["priceChangePercent"])
        except (ValueError, KeyError, TypeError):
            continue
        if not (math.isfinite(price) and math.isfinite(pct24)) or price <= 0:
            continue

        closes = _fetch_daily_closes_for_crsi(symbol)
        if not closes:
            continue
        crsi = _calculate_crsi(closes)
        if crsi is None:
            continue

        if crsi >= CRSI_OVERBOUGHT:
            side, rec, kind = "sell", "SHORT", "volume_surge_short"
            zone_label = "перекуплен"
            dir_word = "ШОРТ"
        elif crsi <= CRSI_OVERSOLD:
            side, rec, kind = "buy", "LONG", "volume_surge_long"
            zone_label = "перепродан"
            dir_word = "ЛОНГ"
        else:
            continue

        score = compute_signal_score(
            kind, side, pct24=pct24, btc_pct24=btc_pct24,
        )
        with state_lock:
            atr = state["atr_4h"].get(symbol)
        sl_tp = _format_sl_tp(side, price, atr)
        body = (
            f"🌋 <b>ОБЪЁМНЫЙ ВЗРЫВ + CRSI — {dir_word}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<code>{symbol}</code>\n"
            f"📊 Объём сегодня: <b>${today_vol:,.0f}</b> "
            f"(<b>+{pct:.0f}%</b> к вчера)\n"
            f"   Вчера: ${yest_vol:,.0f}\n"
            f"⚡ CRSI (дневной): <b>{crsi:.1f}</b> — {zone_label}\n"
            f"📈 24ч: {pct24:+.1f}%\n"
            f"💰 Цена: ${price:,.6g}\n"
            f"🎯 Сила сигнала: <b>{score}/100</b> ({_strength_label(score)})"
            + (f"\n{sl_tp}" if sl_tp else "")
        )
        delivered, _aid = send_alert_with_log(symbol, kind, rec, price, body, score)
        if delivered:
            with state_lock:
                state["last_vol_surge_alerted"][symbol] = now
            sent += 1
    return sent


# NOTE: `check_24h_pumps` was removed — it was a pure "WATCH" alert (no
# directional bias), so the LONG/SHORT-only filter in `send_alert_with_log`
# would have suppressed every fire. The same coins now surface through
# `volume_surge_short` (when volume + CRSI overbought confirm), which is the
# replacement directional pump signal. State key `last_pump_alerted` is left
# in place for backward compatibility with restored snapshots.


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
        "confluence_ema_blocked": 0,         # confluence SHORTs suppressed by EMA-200 filter
        "confluence_btc_blocked": 0,         # confluence signals suppressed by BTC direction filter
        "confluence_reversal_blocked": 0,    # confluence SHORTs suppressed: price still at 24h high
        "overheated_reversal_blocked": 0,    # overheated SHORTs suppressed: price still at 24h high
        "single_signals_skipped": 0,   # coins with only 1 signal — suppressed
        "momentum_alerts": 0,          # standalone 15-min price-momentum alerts
        "momentum_ema_blocked": 0,     # momentum SHORTs suppressed by EMA-200 filter
        "overheated_alerts": 0,        # standalone +20% & RSI>=70
        "overheated_ema_blocked": 0,   # overheated SHORTs suppressed by EMA-200 filter
        "oversold_alerts": 0,          # standalone -20% & RSI<=30
        "vol_surge_alerts": 0,         # standalone +300% daily-volume + CRSI extreme
        "breakdown_alerts": 0,         # TEST: breakdown continuation SHORT
        "momentum_long_alerts": 0,     # TEST: momentum fade SHORT (pamped +10% → reversal)
        "new_listing_short_alerts": 0, # TEST: new listing pump→dump SHORT
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
            broad_pairs: list[str] = []
            if tickers:
                for sym, t in tickers.items():
                    try:
                        vol = float(t["quoteVolume"])
                    except (ValueError, KeyError):
                        continue
                    if vol >= MIN_VOLUME_USDT:
                        liquid_vol_map[sym] = vol
                    if vol >= MIN_VOLUME_USDT_BROAD:
                        broad_pairs.append(sym)
                summary["liquid_pairs"] = len(liquid_vol_map)
                logger.info(
                    "%d / %d pairs pass volume filter ($%s+ 24h USDT vol); "
                    "broad pool for volume-surge scan: %d (≥$%s)",
                    len(liquid_vol_map), len(tickers), f"{MIN_VOLUME_USDT:,.0f}",
                    len(broad_pairs), f"{MIN_VOLUME_USDT_BROAD:,.0f}",
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
                mom_sent, mom_ema_blocked = check_momentum(pct_15m_map, tickers, rsi_map)
                summary["momentum_alerts"] = mom_sent
                summary["momentum_ema_blocked"] = mom_ema_blocked

                # 6b. Standalone overheated / oversold (24h ±20% confirmed by RSI)
                oh_n, os_n, oh_ema_blocked, oh_reversal_blocked = check_overheated_oversold(tickers, rsi_map)
                summary["overheated_reversal_blocked"] = oh_reversal_blocked
                summary["overheated_alerts"] = oh_n
                summary["overheated_ema_blocked"] = oh_ema_blocked
                summary["oversold_alerts"] = os_n

                # 6c. Volume surge (+300% daily vol) + CRSI extreme combo
                # (Pre-refresh pass — catches surges seen by the previous hour's ranking.)
                summary["vol_surge_alerts"] = check_volume_surge_crsi(tickers)

                # 6d. TEST: Breakdown continuation SHORT (drop ≥10% + below EMA + RSI 30-58)
                summary["breakdown_alerts"] = check_breakdown_short(tickers, rsi_map)

                # 6e. TEST: Momentum continuation LONG (rise ≥10% + above EMA + RSI 42-68)
                summary["momentum_long_alerts"] = check_momentum_long(tickers, rsi_map)

                # 6f. TEST: New listing pump→dump SHORT (≥+20% from listing + RSI≥65)
                summary["new_listing_short_alerts"] = check_new_listing_pumps(tickers, rsi_map)

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
                    # Use the broader pool so the volume-surge scanner can see
                    # coins like ORCAUSDT that sometimes dip below the standard
                    # $50k liquidity floor. Existing alerts still gate on
                    # `liquid_pairs` themselves.
                    refresh_pool = broad_pairs or [s for s, _ in liquid_pairs]
                    logger.info(
                        "Refreshing 7d/30d highs + volume ranking for %d pairs...",
                        len(refresh_pool),
                    )
                    try:
                        refresh_weekly_monthly_highs(refresh_pool)
                    except Exception as e:
                        logger.error("Weekly/monthly refresh failed: %s", e)
                        summary["errors"].append(f"WM refresh: {e}")
                    else:
                        # Re-scan against the freshly rebuilt ranking so a brand
                        # new ≥300% surge doesn't wait another 5-min cycle. The
                        # 4h per-symbol cooldown prevents duplicates with the
                        # pre-refresh pass above.
                        try:
                            summary["vol_surge_alerts"] += check_volume_surge_crsi(tickers)
                        except Exception as e:
                            logger.error("Post-refresh volume-surge scan failed: %s", e)
                            summary["errors"].append(f"vol_surge rescan: {e}")

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
                rec_label = _rec_label(rec_line)

                # EMA-200 trend filter
                # SHORT: suppress if price is above EMA-200 (uptrend — don't fight it)
                if rec_label == "SHORT" and above_ema is True:
                    logger.debug(
                        "Confluence SHORT suppressed by EMA-200 filter: "
                        "%s price=%.6g ema=%.6g",
                        sym, b["price"] or 0, ema or 0,
                    )
                    summary["confluence_ema_blocked"] += 1
                    continue
                # LONG: suppress if price is below EMA-200 (downtrend — don't catch falling knives)
                if rec_label == "LONG" and above_ema is False:
                    logger.debug(
                        "Confluence LONG suppressed by EMA-200 filter: "
                        "%s price=%.6g ema=%.6g (below EMA)",
                        sym, b["price"] or 0, ema or 0,
                    )
                    summary["confluence_ema_blocked"] += 1
                    continue

                # BTC direction filter — alts follow BTC short-term.
                # RSI of BTC (1h) tells us if BTC is in an up or down swing right now.
                # 24h BTC % is a backup for when RSI is in neutral zone.
                btc_rsi = rsi_map.get("BTCUSDT")
                if rec_label == "SHORT":
                    # BTC pumping → shorting alts is risky (they'll be lifted too)
                    btc_bullish = (
                        (btc_rsi is not None and btc_rsi > 55) or
                        (btc_pct24 is not None and btc_pct24 > 2.0)
                    )
                    if btc_bullish:
                        logger.debug(
                            "Confluence SHORT suppressed by BTC filter: %s "
                            "BTC RSI=%.1f 24h=%.1f%%",
                            sym, btc_rsi or 0, btc_pct24 or 0,
                        )
                        summary["confluence_btc_blocked"] += 1
                        continue
                elif rec_label == "LONG":
                    # BTC falling → longing alts is risky (they'll be dragged down too)
                    btc_bearish = (
                        (btc_rsi is not None and btc_rsi < 45) or
                        (btc_pct24 is not None and btc_pct24 < -2.0)
                    )
                    if btc_bearish:
                        logger.debug(
                            "Confluence LONG suppressed by BTC filter: %s "
                            "BTC RSI=%.1f 24h=%.1f%%",
                            sym, btc_rsi or 0, btc_pct24 or 0,
                        )
                        summary["confluence_btc_blocked"] += 1
                        continue

                # Reversal confirmation filter for SHORT signals.
                # Don't short a coin that is still at/near its 24h high —
                # wait for price to pull back ≥ SHORT_REVERSAL_CONFIRM_PCT
                # from the peak, confirming the move has actually turned.
                if rec_label == "SHORT" and b["price"] is not None and tickers:
                    t_conf = tickers.get(sym)
                    if t_conf is not None:
                        try:
                            high_24h = float(t_conf["highPrice"])
                            if high_24h > 0:
                                pullback_pct = (high_24h - b["price"]) / high_24h * 100.0
                                if pullback_pct < SHORT_REVERSAL_CONFIRM_PCT:
                                    logger.debug(
                                        "Confluence SHORT suppressed by reversal filter: "
                                        "%s price=%.6g high=%.6g pullback=%.2f%%",
                                        sym, b["price"], high_24h, pullback_pct,
                                    )
                                    summary["confluence_reversal_blocked"] += 1
                                    continue
                        except (ValueError, KeyError):
                            pass

                side_for_score = (
                    "buy" if rec_label == "LONG"
                    else "sell" if rec_label == "SHORT"
                    else "neutral"
                )
                btc_t = tickers.get("BTCUSDT") if tickers else None
                btc_pct24 = None
                if btc_t:
                    try:
                        btc_pct24 = float(btc_t["priceChangePercent"])
                    except (ValueError, KeyError, TypeError):
                        btc_pct24 = None
                score = compute_signal_score(
                    "confluence", side_for_score,
                    rsi=b["rsi"],
                    above_ema=above_ema,
                    spike_ratio=b["spike_ratio"],
                    btc_pct24=btc_pct24,
                )
                header = [f"<b>🚨 КОНФЛЮЭНЦИЯ СИГНАЛОВ: <code>{sym}</code></b>"]
                if b["price"] is not None:
                    header.append(f"Цена: <b>${b['price']:,.6g}</b>")
                if b["volume"] is not None:
                    header.append(f"Объём за 24ч: ${b['volume']:,.0f}")
                with state_lock:
                    atr = state["atr_4h"].get(sym)
                sl_tp = _format_sl_tp(side_for_score, b["price"], atr)
                body_lines = header + [
                    "",
                    f"<b>Сработавшие сигналы ({len(b['lines'])}):</b>",
                    *[f"  • {ln}" for ln in b["lines"]],
                    "",
                    rec_line,
                    reason_line,
                    f"🎯 Сила сигнала: <b>{score}/100</b> ({_strength_label(score)})",
                ]
                if sl_tp:
                    body_lines.append(sl_tp)
                body = "\n".join(body_lines)
                delivered, _aid = send_alert_with_log(
                    sym, "confluence", rec_label, b["price"], body, score
                )
                if not delivered:
                    # Silenced, hidden, or Telegram error — do NOT consume cooldowns
                    logger.info(
                        "Confluence alert %s NOT delivered (silenced/hidden/error); cooldowns preserved",
                        sym,
                    )
                    continue

                logger.info(
                    "Confluence alert %s: %d signals %s score=%d",
                    sym, len(b["lines"]), sorted(b["flags"]), score,
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
    log_cycle(summary)

    with state_lock:
        state["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        state["last_run_summary"] = summary


# ---------------------------------------------------------------------------
# Position Monitor — TP / SL / timeout tracking for every delivered signal
# ---------------------------------------------------------------------------
# Each delivered LONG/SHORT alert spawns a daemon thread that polls Binance
# every ~45 s and fires a Telegram exit notification when the position hits
# TP, SL, or the 4-hour timeout.  State is persisted in `position_monitors`
# (SQLite) so monitors survive a bot restart.
# ---------------------------------------------------------------------------

MONITOR_POLL_INTERVAL = 45          # seconds between price polls
MONITOR_TIMEOUT_HOURS = 4           # hours until forced timeout
MONITOR_SLIPPAGE_PCT  = 5.0         # re-verify if price moves >5 % in one tick

_active_monitors: dict[int, "PositionMonitor"] = {}
_monitors_lock = threading.Lock()


def _get_current_price(symbol: str) -> float | None:
    """Fetch the latest spot price via the lightweight /ticker/price endpoint."""
    try:
        resp = _binance_get(
            "/api/v3/ticker/price", params={"symbol": symbol}, timeout=6
        )
        return float(resp.json()["price"])
    except Exception as exc:
        logger.debug("_get_current_price %s failed: %s", symbol, exc)
        return None


def _format_elapsed(seconds: float) -> str:
    m = int(seconds / 60)
    if m >= 60:
        return f"{m // 60} ч {m % 60} мин"
    return f"{m} минут"


def _ensure_entry_notified(
    alert_id: int | None,
    symbol: str,
    direction: str,
    entry: float,
    sl_price: float,
    tp_price: float,
) -> None:
    """Before sending an exit notification, verify the entry alert was delivered.
    If alert_id is missing or not found in the alerts table (entry was lost due
    to a crash, Telegram failure, or silent drop), send a retroactive entry
    message so the user always sees entry → exit in that order.
    """
    if alert_id:
        try:
            with _db_lock:
                conn = _get_db()
                row = conn.execute(
                    "SELECT id FROM alerts WHERE id=?", (alert_id,)
                ).fetchone()
            if row:
                return  # Entry was properly logged and delivered
        except Exception as exc:
            logger.warning("_ensure_entry_notified DB check failed: %s", exc)

    # Entry alert was never logged or not delivered → send retroactively
    direction_emoji = "📈" if direction == "LONG" else "📉"
    rec_emoji       = "🟢" if direction == "LONG" else "🔴"
    body = (
        f"⚠️ <b>[ПРОПУЩЕННЫЙ ВХОД]</b>\n"
        f"Бот открыл позицию, но входной сигнал не был доставлен вовремя\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{direction_emoji} {rec_emoji} <b>{direction}</b> <code>{symbol}</code>\n"
        f"💰 Вход: <b>${entry:,.6g}</b>\n"
        f"🛡️ Стоп: <b>${sl_price:,.6g}</b>  •  🎯 Цель: <b>${tp_price:,.6g}</b>\n"
        f"<i>Это ретроактивное уведомление — позиция уже закрывается</i>"
    )
    try:
        _telegram_send(TELEGRAM_CHAT_ID, body)
        logger.info(
            "Retroactive entry notification sent: %s %s @ %.6g",
            direction, symbol, entry,
        )
    except Exception as exc:
        logger.warning("_ensure_entry_notified telegram send failed: %s", exc)


def _send_exit_notification(
    symbol: str,
    direction: str,
    result: str,         # "TP" | "SL" | "TIMEOUT"
    entry: float,
    exit_price: float,
    elapsed_seconds: float,
) -> None:
    pnl_pct = (
        (exit_price - entry) / entry * 100
        if direction == "LONG"
        else (entry - exit_price) / entry * 100
    )
    pnl_sign = "+" if pnl_pct >= 0 else ""
    elapsed_str = _format_elapsed(elapsed_seconds)

    if result == "TP":
        emoji, header = "✅", "ВЫХОД ПО TP"
    elif result == "SL":
        emoji, header = "❌", "ВЫХОД ПО SL"
    else:
        emoji, header = "⏱️", "ТАЙМАУТ"

    if result == "TIMEOUT":
        body = (
            f"{emoji} <b>{header}</b>\n"
            f"{direction} <code>{symbol}</code>\n"
            f"Вход: <b>${entry:,.6g}</b>  →  Текущая: <b>${exit_price:,.6g}</b>\n"
            f"P&L: <b>{pnl_sign}{pnl_pct:.2f}%</b>\n"
            f"Время удержания: <b>{elapsed_str}</b> (лимит)\n"
            f"<i>Закрыта по таймауту</i>"
        )
    else:
        body = (
            f"{emoji} <b>{header}</b>\n"
            f"{direction} <code>{symbol}</code>\n"
            f"Вход: <b>${entry:,.6g}</b>  →  Выход: <b>${exit_price:,.6g}</b>\n"
            f"P&L: <b>{pnl_sign}{pnl_pct:.2f}%</b>\n"
            f"Время: <b>{elapsed_str}</b>"
        )

    try:
        _telegram_send(TELEGRAM_CHAT_ID, body)
    except Exception as exc:
        logger.warning("_send_exit_notification failed: %s", exc)


class PositionMonitor(threading.Thread):
    """Daemon thread: polls spot price every MONITOR_POLL_INTERVAL seconds.
    Closes on TP / SL hit or after MONITOR_TIMEOUT_HOURS, then sends a
    Telegram exit notification and updates the DB row."""

    def __init__(
        self,
        position_id: int,
        alert_id: int | None,
        symbol: str,
        direction: str,     # "LONG" or "SHORT"
        entry: float,
        sl_price: float,
        tp_price: float,
        ts_open: int,
    ) -> None:
        super().__init__(name=f"pos-mon-{position_id}", daemon=True)
        self.position_id = position_id
        self.alert_id    = alert_id
        self.symbol      = symbol
        self.direction   = direction
        self.entry       = entry
        self.sl_price    = sl_price
        self.tp_price    = tp_price
        self.ts_open     = ts_open
        self._stop_evt   = threading.Event()

    def stop(self) -> None:
        self._stop_evt.set()

    def run(self) -> None:
        logger.info(
            "PositionMonitor started: id=%d %s %s entry=%.6g sl=%.6g tp=%.6g",
            self.position_id, self.direction, self.symbol,
            self.entry, self.sl_price, self.tp_price,
        )
        prev_price: float | None = None

        while not self._stop_evt.wait(timeout=MONITOR_POLL_INTERVAL):
            try:
                current = _get_current_price(self.symbol)
                if current is None:
                    continue

                # Anti-slippage: if price moved >5 % since last tick, re-verify
                if prev_price is not None:
                    delta_pct = abs(current - prev_price) / prev_price * 100
                    if delta_pct > MONITOR_SLIPPAGE_PCT:
                        time.sleep(5)
                        current2 = _get_current_price(self.symbol)
                        if current2 is not None:
                            current = current2
                prev_price = current

                # Evaluate TP / SL conditions
                result: str | None = None
                if self.direction == "LONG":
                    if current >= self.tp_price:
                        result = "TP"
                    elif current <= self.sl_price:
                        result = "SL"
                else:   # SHORT
                    if current <= self.tp_price:
                        result = "TP"
                    elif current >= self.sl_price:
                        result = "SL"

                elapsed = time.time() - self.ts_open
                if result is None and elapsed >= MONITOR_TIMEOUT_HOURS * 3600:
                    result = "TIMEOUT"

                if result is not None:
                    self._close(result, current, elapsed)
                    return

            except Exception as exc:
                logger.warning("PositionMonitor %s: %s", self.symbol, exc)

        logger.debug("PositionMonitor %s stopped externally", self.symbol)

    def _close(self, result: str, exit_price: float, elapsed: float) -> None:
        pnl_pct = (
            (exit_price - self.entry) / self.entry * 100
            if self.direction == "LONG"
            else (self.entry - exit_price) / self.entry * 100
        )
        try:
            with _db_lock:
                conn = _get_db()
                conn.execute(
                    "UPDATE position_monitors "
                    "SET status=?, ts_close=?, exit_price=?, pnl_pct=? "
                    "WHERE id=?",
                    (
                        result.lower(), int(time.time()),
                        exit_price, round(pnl_pct, 4),
                        self.position_id,
                    ),
                )
                conn.commit()
        except Exception as exc:
            logger.warning("PositionMonitor DB update failed: %s", exc)

        # Guard: if the entry signal was never delivered (Telegram failure,
        # silent drop, or bot restart), send it now before the exit so the
        # user always sees entry → exit in order, never exit alone.
        _ensure_entry_notified(
            self.alert_id, self.symbol, self.direction,
            self.entry, self.sl_price, self.tp_price,
        )
        _send_exit_notification(
            self.symbol, self.direction, result,
            self.entry, exit_price, elapsed,
        )
        # SL re-entry cooldown disabled — no block after SL hit
        with _monitors_lock:
            _active_monitors.pop(self.position_id, None)

        logger.info(
            "PositionMonitor closed: id=%d %s %s result=%s exit=%.6g pnl=%.2f%%",
            self.position_id, self.direction, self.symbol,
            result, exit_price, pnl_pct,
        )


def _auto_start_monitor(
    alert_id: int,
    symbol: str,
    direction: str,   # "LONG" or "SHORT"
    entry: float,
) -> None:
    """Compute TP/SL from fixed percentages and launch a PositionMonitor."""
    if os.environ.get("TESTING", "").strip().lower() in {"1", "true", "yes"}:
        return
    if direction not in ("LONG", "SHORT"):
        return
    # One active position per symbol: skip if a monitor for this symbol is already running
    with _monitors_lock:
        open_symbols = {m.symbol for m in _active_monitors.values()}
    if symbol in open_symbols:
        logger.info(
            "_auto_start_monitor blocked: %s already has an open position monitor", symbol,
        )
        return
    if direction == "LONG":
        sl_price = entry * (1 - HIT_RATE_SL_PCT / 100)
        tp_price = entry * (1 + HIT_RATE_TP_PCT / 100)
    else:
        sl_price = entry * (1 + HIT_RATE_SL_PCT / 100)
        tp_price = entry * (1 - HIT_RATE_TP_PCT_SHORT / 100)
    ts_open = int(time.time())

    try:
        with _db_lock:
            conn = _get_db()
            cur = conn.execute(
                "INSERT INTO position_monitors "
                "(alert_id, ts_open, symbol, direction, "
                "entry_price, sl_price, tp_price, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'open')",
                (alert_id, ts_open, symbol, direction,
                 entry, sl_price, tp_price),
            )
            conn.commit()
            pos_id = int(cur.lastrowid or 0)
    except Exception as exc:
        logger.warning("_auto_start_monitor DB insert failed: %s", exc)
        return

    monitor = PositionMonitor(
        pos_id, alert_id, symbol, direction,
        entry, sl_price, tp_price, ts_open,
    )
    monitor.start()
    with _monitors_lock:
        _active_monitors[pos_id] = monitor

    logger.info(
        "PositionMonitor launched: id=%d %s %s entry=%.6g sl=%.6g tp=%.6g",
        pos_id, direction, symbol, entry, sl_price, tp_price,
    )


def restore_position_monitors() -> None:
    """Called at startup: resume all positions still marked 'open' in the DB.
    Positions that already passed the 4-hour timeout are closed immediately."""
    if os.environ.get("TESTING", "").strip().lower() in {"1", "true", "yes"}:
        return
    try:
        with _db_lock:
            conn = _get_db()
            rows = conn.execute(
                "SELECT id, alert_id, symbol, direction, "
                "entry_price, sl_price, tp_price, ts_open "
                "FROM position_monitors WHERE status='open'"
            ).fetchall()
    except Exception as exc:
        logger.warning("restore_position_monitors failed to read DB: %s", exc)
        return

    if not rows:
        return

    logger.info("Restoring %d open position monitor(s)…", len(rows))
    for pos_id, alert_id, symbol, direction, entry, sl_price, tp_price, ts_open in rows:
        elapsed = time.time() - ts_open
        if elapsed >= MONITOR_TIMEOUT_HOURS * 3600:
            # Expired while bot was down — close as timeout
            current = _get_current_price(symbol) or entry
            pnl_pct = (
                (current - entry) / entry * 100 if direction == "LONG"
                else (entry - current) / entry * 100
            )
            try:
                with _db_lock:
                    conn = _get_db()
                    conn.execute(
                        "UPDATE position_monitors "
                        "SET status='timeout', ts_close=?, exit_price=?, pnl_pct=? "
                        "WHERE id=?",
                        (int(time.time()), current, round(pnl_pct, 4), pos_id),
                    )
                    conn.commit()
            except Exception as exc:
                logger.warning("restore timeout close failed: %s", exc)
            _send_exit_notification(
                symbol, direction, "TIMEOUT", entry, current, elapsed
            )
            logger.info(
                "Restored monitor id=%d expired — sent TIMEOUT (elapsed=%.0fs)",
                pos_id, elapsed,
            )
            continue

        monitor = PositionMonitor(
            pos_id, alert_id, symbol, direction,
            entry, sl_price, tp_price, ts_open,
        )
        monitor.start()
        with _monitors_lock:
            _active_monitors[pos_id] = monitor
        logger.info(
            "Restored monitor: id=%d %s %s (%.0fs elapsed)",
            pos_id, direction, symbol, elapsed,
        )


# ---------------------------------------------------------------------------
# Cycle logging
# ---------------------------------------------------------------------------
_CYCLE_LOG_PATH = os.path.join(os.path.dirname(__file__), "cycle_log.jsonl")

def log_cycle(summary: dict) -> None:
    line = {"ts": datetime.utcnow().isoformat(), **summary}
    try:
        with open(_CYCLE_LOG_PATH, "a") as f:
            f.write(json.dumps(line) + "\n")
    except Exception as exc:
        logger.warning("log_cycle write failed: %s", exc)


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
        f"  💎 Перепродана (-20% + RSI≤30): <b>{summary.get('oversold_alerts', 0)}</b>\n"
        f"  🌋 Объём+CRSI: <b>{summary.get('vol_surge_alerts', 0)}</b>"
        f"{error_line}\n\n"
        f"<b>Правила:</b>\n"
        f"  Алерт только при ≥ {CONFLUENCE_MIN_SIGNALS} сигналах на одной монете\n"
        f"  Мин. объём: ${MIN_VOLUME_USDT:,.0f}\n"
        f"  Всплеск объёма: ≥ {VOLUME_SPIKE_MULTIPLIER}× средн. (5м)\n"
        f"  RSI перекупленность: ≥ {RSI_OVERBOUGHT}  |  кулдаун {RSI_ALERT_COOLDOWN // 3600}ч\n"
        f"  RSI перепроданность: ≤ {RSI_OVERSOLD}  |  кулдаун {RSI_ALERT_COOLDOWN // 3600}ч\n"
        f"  EMA-200 (4ч): тренд-фильтр для шорт/лонг\n"
        f"  Интервал проверки: 5 мин\n\n"
        f"<b>Команды:</b> /status · /top10 · /top30 · /signal · /stats · /trade · /mytrades · /silence · /unmute"
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


_top30_cache: dict = {"ts": 0.0, "tickers": None}
_top30_lock = threading.Lock()
TOP30_CACHE_TTL = 30  # seconds — coalesce command spam into one Binance fetch


def handle_top30_command(chat_id: int) -> None:
    """List every USDT pair currently up ≥ PUMP_24H_PCT in 24h, sorted desc."""
    with state_lock:
        initialized = state["initialized"]

    if not initialized:
        _telegram_send(chat_id, "<b>⏳ Бот ещё инициализируется...</b>\nЗагляните через минуту.")
        return

    # Use a 30s shared snapshot so concurrent /top30 calls share one Binance fetch.
    tickers: dict[str, dict] | None = None
    try:
        with _top30_lock:
            now = time.time()
            cached = _top30_cache["tickers"]
            if cached is not None and (now - _top30_cache["ts"]) < TOP30_CACHE_TTL:
                tickers = cached
            else:
                with state_lock:
                    symbols = list(state["known_pairs"])
                if not symbols:
                    _telegram_send(chat_id, "⏳ Список пар ещё не загружен. Попробуйте через минуту.")
                    return
                tickers = get_24h_tickers(symbols)
                _top30_cache["tickers"] = tickers
                _top30_cache["ts"] = now
    except Exception as e:
        logger.exception("/top30: failed to fetch 24h tickers: %s", e)
        _telegram_send(chat_id, "⚠️ Не удалось получить данные с Binance. Попробуйте позже.")
        return

    movers: list[tuple[str, float, float, float]] = []  # (sym, pct, price, volume)
    for sym, t in tickers.items():
        try:
            pct = float(t["priceChangePercent"])
            price = float(t["lastPrice"])
            volume = float(t["quoteVolume"])
        except (ValueError, KeyError, TypeError):
            continue
        if not (math.isfinite(pct) and math.isfinite(price) and math.isfinite(volume)):
            continue
        if volume <= 0 or price <= 0:
            continue
        if pct >= PUMP_24H_PCT:
            movers.append((sym, pct, price, volume))

    movers.sort(key=lambda x: x[1], reverse=True)

    if not movers:
        _telegram_send(
            chat_id,
            f"📊 <b>Топ рост 24ч</b>\n\n"
            f"Сейчас нет монет с ростом ≥ <b>+{PUMP_24H_PCT:.0f}%</b> за 24 часа.",
        )
        return

    lines = [
        f"🚀 <b>Топ рост 24ч (≥ +{PUMP_24H_PCT:.0f}%)</b>",
        f"<i>Всего монет: {len(movers)}</i>\n",
    ]
    for i, (sym, pct, price, volume) in enumerate(movers, 1):
        lines.append(
            f"{i}. <code>{sym}</code>  <b>+{pct:.1f}%</b>\n"
            f"   💰 ${price:,.6g}  |  📊 ${volume:,.0f}"
        )
    lines.append("\n⚠️ Внимание: возможна коррекция после сильного роста")
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
    """Show simulated SL/TP P&L stats per alert type for the last HIT_RATE_RETENTION_DAYS days."""
    stats = compute_hit_rate_stats(days=HIT_RATE_RETENTION_DAYS)
    if not stats:
        _telegram_send(
            chat_id,
            f"📊 <b>Статистика за {HIT_RATE_RETENTION_DAYS} дней</b>\n\n"
            f"Пока нет данных — алерты ещё не накоплены.",
        )
        return

    sl_pct = HIT_RATE_SL_PCT
    tp_pct = HIT_RATE_TP_PCT

    def fmt_horizon(h: dict) -> str:
        """Format one horizon bucket as  TP%/SL% avg_pnl."""
        n = h["n"]
        if n == 0:
            return "—"
        tp_r = h["tp"]  / n * 100.0
        sl_r = h["sl"]  / n * 100.0
        avg  = h["pnl_sum"] / n
        sign = "+" if avg >= 0 else ""
        return f"🟢{tp_r:.0f}% 🔴{sl_r:.0f}% ({sign}{avg:.1f}%)"

    lines = [
        f"📊 <b>Статистика за {HIT_RATE_RETENTION_DAYS} дней</b>",
        f"<i>SL {sl_pct:.0f}% / TP {tp_pct:.0f}% · симуляция по ценам снапшотов</i>",
        "",
    ]
    for r in stats:
        h3m  = r["horizons"]["3м"]
        h5m  = r["horizons"]["5м"]
        h15  = r["horizons"]["15м"]
        h1h  = r["horizons"]["1ч"]
        h4h  = r["horizons"]["4ч"]
        lines.append(
            f"<b>{r['alert_type']}</b> · {r['recommendation']} · {r['total']} алертов\n"
            f"   3м: {fmt_horizon(h3m)}\n"
            f"   5м: {fmt_horizon(h5m)}\n"
            f"  15м: {fmt_horizon(h15)}\n"
            f"   1ч: {fmt_horizon(h1h)}\n"
            f"   4ч: {fmt_horizon(h4h)}"
        )
    lines.append("")
    lines.append(
        f"<i>🟢 = TP достигнут (+{tp_pct:.0f}%)  🔴 = стоп (−{sl_pct:.0f}%)  "
        f"в скобках — средний P&L включая таймауты</i>"
    )
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


def handle_trade_photo(chat_id: int, message: dict) -> None:
    """Extract a trade from a forwarded Binance Futures share-card screenshot
    via Gemini Vision, then run it through the same analysis as `/trade`.
    Designed so the user can drop a picture into the bot instead of typing
    `/trade SYMBOL DIR ENTRY EXIT`. Sends a friendly Russian error message
    on any failure path so the user always knows what to do next."""
    photos = message.get("photo") or []
    if not photos:
        return
    # Telegram returns the same image in multiple resolutions; the largest is
    # last and produces the best OCR.
    file_id = photos[-1].get("file_id")
    if not file_id:
        return

    _telegram_send(chat_id, "🔎 Распознаю сделку на скриншоте…")

    downloaded = _telegram_download_photo(file_id)
    if downloaded is None:
        _telegram_send(chat_id,
            "⚠️ Не удалось скачать изображение из Telegram. Попробуйте ещё раз.")
        return
    image_bytes, mime = downloaded

    parsed = _gemini_extract_trade_from_image(image_bytes, mime_type=mime)
    if parsed is None:
        _telegram_send(chat_id,
            "⚠️ Не удалось разобрать изображение (vision-сервис не ответил). "
            "Попробуйте ещё раз через минуту или введите сделку вручную: "
            "<code>/trade SYMBOL лонг|шорт ENTRY EXIT</code>")
        return
    if "error" in parsed:
        reason = html.escape(str(parsed.get("error", "не распознано")))
        _telegram_send(chat_id,
            f"⚠️ {reason}\n\n"
            "Это должен быть скриншот карточки Binance Futures Share. "
            "Или введите сделку вручную: "
            "<code>/trade SYMBOL лонг|шорт ENTRY EXIT</code>")
        return

    # Build a synthetic `/trade ...` text and reuse the existing handler so
    # validation, persistence, and analysis stay in one place.
    try:
        sym = str(parsed["symbol"]).upper().strip()
        direction = str(parsed["direction"]).lower().strip()
        entry = float(parsed["entry"])
        exit_ = float(parsed["exit"])
    except (KeyError, TypeError, ValueError) as e:
        logger.warning("Vision returned malformed trade JSON %r: %s", parsed, e)
        _telegram_send(chat_id,
            "⚠️ Распознавание вернуло некорректные данные. Попробуйте "
            "другой скриншот или введите сделку вручную: "
            "<code>/trade SYMBOL лонг|шорт ENTRY EXIT</code>")
        return

    synthetic = f"/trade {sym} {direction} {entry} {exit_}"
    logger.info("Trade photo from chat_id=%s parsed as: %s", chat_id, synthetic)
    handle_trade_command(chat_id, synthetic)


def handle_positions_command(chat_id: int) -> None:
    """Show all currently active position monitors with live PnL."""
    with _monitors_lock:
        monitors = list(_active_monitors.values())

    if not monitors:
        _telegram_send(chat_id, "📭 <b>Нет открытых позиций</b>")
        return

    lines = [f"📊 <b>Открытые позиции: {len(monitors)}</b>", ""]
    now = time.time()
    for m in sorted(monitors, key=lambda x: x.ts_open):
        current = _get_current_price(m.symbol)
        elapsed_h = (now - m.ts_open) / 3600
        if current is not None:
            pnl_pct = (
                (current - m.entry) / m.entry * 100 if m.direction == "LONG"
                else (m.entry - current) / m.entry * 100
            )
            pnl_emoji = "🟢" if pnl_pct > 0 else ("🔴" if pnl_pct < 0 else "⚪️")
            pnl_str = f"{pnl_pct:+.2f}%"
        else:
            pnl_emoji, pnl_str = "⚪️", "—"
            current = m.entry

        dir_emoji = "📈" if m.direction == "LONG" else "📉"
        lines.append(f"{dir_emoji} <b>{m.symbol}</b>  {m.direction}")
        lines.append(
            f"   Вход: <code>${m.entry:,.6g}</code>  "
            f"Сейчас: <code>${current:,.6g}</code>"
        )
        lines.append(f"   PnL: {pnl_emoji} <b>{pnl_str}</b>  ({elapsed_h:.1f}ч)")
        lines.append(
            f"   SL: <code>${m.sl_price:,.6g}</code>  "
            f"TP: <code>${m.tp_price:,.6g}</code>"
        )
        lines.append("")

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
        "<b>🎯 Текущие сигналы — топ-10 по росту объёма за 24ч (к вчерашнему)</b>",
        "<i>Показываются только направленные сигналы (LONG / SHORT). "
        "Нейтральные пары скрыты — они не рассылаются в канал.</i>\n",
    ]
    shown = 0
    skipped_neutral = 0
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
        # Skip NEUTRAL/мixed — channel filter drops them anyway, no point
        # listing them here. Use the same side classifier as everywhere else.
        side = _classify_side("", rec_short)
        if side == "neutral":
            skipped_neutral += 1
            continue
        shown += 1
        rsi_str = f"{rsi_v:.1f}" if rsi_v is not None else "—"
        spike_str = f"{spike_v:.1f}×" if spike_v is not None else "—"
        lines.append(
            f"{shown}. <code>{sym}</code> — <b>{rec_short}</b>\n"
            f"   RSI: {rsi_str}  |  Объём 5м: {spike_str}\n"
            f"   {reason_line}"
        )

    if shown == 0:
        lines.append(
            "Сейчас среди топ-10 по объёму нет направленных сигналов "
            "(все нейтральны)."
        )
    elif skipped_neutral:
        lines.append(f"\n<i>Скрыто нейтральных: {skipped_neutral}</i>")

    _telegram_send(chat_id, "\n".join(lines))


def handle_analyze_command(chat_id: int, raw_text: str) -> None:
    """Real-time full analysis of a single coin: /analyze SYMBOL"""
    parts = raw_text.strip().split()
    if len(parts) < 2:
        _telegram_send(
            chat_id,
            "Использование: <code>/analyze BTCUSDT</code>\n"
            "Пример: <code>/analyze DEXEUSDT</code>",
        )
        return

    raw_sym = parts[1].upper().strip()
    symbol = raw_sym if raw_sym.endswith("USDT") else raw_sym + "USDT"

    _telegram_send(chat_id, f"⏳ Анализирую <code>{symbol}</code>…")

    # ── Parallel data fetch ────────────────────────────────────────────────
    def fetch_ticker():
        try:
            resp = _binance_get(
                "/api/v3/ticker/24hr", params={"symbol": symbol}, timeout=10,
            )
            d = resp.json()
            # Binance returns {"code": -1121} for unknown symbols
            return None if "code" in d else d
        except Exception as e:
            logger.warning("analyze ticker failed %s: %s", symbol, e)
            return None

    def fetch_4h_klines():
        try:
            resp = _binance_get(
                "/api/v3/klines",
                params={"symbol": symbol, "interval": "4h", "limit": EMA200_FETCH_LIMIT},
                timeout=10,
            )
            return resp.json()
        except Exception as e:
            logger.warning("analyze 4h klines failed %s: %s", symbol, e)
            return None

    def fetch_rsi():
        return get_klines_rsi(symbol)

    def fetch_5m():
        return get_5m_signals(symbol)

    with ThreadPoolExecutor(max_workers=4) as ex:
        f_tick = ex.submit(fetch_ticker)
        f_4h   = ex.submit(fetch_4h_klines)
        f_rsi  = ex.submit(fetch_rsi)
        f_5m   = ex.submit(fetch_5m)

    ticker     = f_tick.result()
    klines_4h  = f_4h.result()
    rsi        = f_rsi.result()
    spike_ratio, pct_15m = f_5m.result()

    # ── Ticker validation ──────────────────────────────────────────────────
    price = pct24 = volume_usdt = None
    if ticker:
        try:
            price       = float(ticker["lastPrice"])
            pct24       = float(ticker["priceChangePercent"])
            volume_usdt = float(ticker["quoteVolume"])
        except (ValueError, KeyError):
            pass

    if price is None:
        _telegram_send(
            chat_id,
            f"❌ Монета <code>{symbol}</code> не найдена на Binance Spot.\n"
            "Проверьте название (пример: <code>BTCUSDT</code>, <code>DEXEUSDT</code>).",
        )
        return

    # ── 4h indicators ──────────────────────────────────────────────────────
    ema200 = atr = supertrend = None
    if klines_4h and len(klines_4h) >= EMA200_PERIOD:
        h4_highs  = [float(k[2]) for k in klines_4h]
        h4_lows   = [float(k[3]) for k in klines_4h]
        h4_closes = [float(k[4]) for k in klines_4h]
        ema200     = _calculate_ema(h4_closes, EMA200_PERIOD)
        atr        = _calculate_atr(h4_highs, h4_lows, h4_closes, 14)
        supertrend = _calculate_supertrend(h4_highs, h4_lows, h4_closes)

    above_ema: bool | None = None
    ema_pct: float | None = None
    if ema200 is not None and price > 0:
        above_ema = price > ema200
        ema_pct   = (price - ema200) / ema200 * 100.0

    # ── Market regime ──────────────────────────────────────────────────────
    regime = get_market_regime()

    # ── Signal evaluation ──────────────────────────────────────────────────
    oh_threshold = _vol_threshold(symbol, OVERHEATED_24H_PCT, 2.5)
    os_threshold = _vol_threshold(symbol, OVERSOLD_24H_PCT, 2.5)

    signals_triggered: list[tuple[str, str, int, str]] = []  # (type, dir, score, reason)
    signals_missing:   list[str] = []

    btc_pct24: float | None = None
    with state_lock:
        btc_t = None  # no tickers dict here; skip BTC pct

    # — overheated_24h SHORT —
    if pct24 is not None and rsi is not None:
        if pct24 >= oh_threshold and rsi >= RSI_OVERBOUGHT:
            if above_ema is True:
                signals_missing.append(
                    f"overheated SHORT: условия есть, но цена выше EMA-200 → заблокирован"
                )
            else:
                sc = compute_signal_score(
                    "overheated_24h", "sell", rsi=rsi, above_ema=above_ema, pct24=pct24,
                )
                min_sc = MIN_SCORE_BY_TYPE.get("overheated_24h", MIN_ALERT_SCORE)
                if sc >= min_sc:
                    signals_triggered.append((
                        "overheated_24h", "SHORT", sc,
                        f"+{pct24:.1f}% за 24ч (порог +{oh_threshold:.1f}%) + RSI {rsi:.1f}",
                    ))
                else:
                    signals_missing.append(
                        f"overheated SHORT: score {sc} &lt; {min_sc} (нужно усиление)"
                    )
        else:
            parts_missing = []
            if pct24 is not None and pct24 < oh_threshold:
                parts_missing.append(f"рост {pct24:+.1f}% &lt; порога {oh_threshold:.1f}%")
            if rsi is not None and rsi < RSI_OVERBOUGHT:
                parts_missing.append(f"RSI {rsi:.1f} &lt; {RSI_OVERBOUGHT}")
            if parts_missing:
                signals_missing.append("overheated SHORT: " + ", ".join(parts_missing))

    # — oversold_24h LONG —
    if pct24 is not None and rsi is not None:
        if pct24 <= os_threshold and rsi <= RSI_OVERSOLD:
            sc = compute_signal_score(
                "oversold_24h", "buy", rsi=rsi, above_ema=above_ema, pct24=pct24,
            )
            if sc >= MIN_SCORE_BY_TYPE.get("oversold_24h", MIN_ALERT_SCORE):
                signals_triggered.append((
                    "oversold_24h", "LONG", sc,
                    f"{pct24:.1f}% за 24ч (порог {os_threshold:.1f}%) + RSI {rsi:.1f}",
                ))
            else:
                signals_missing.append(f"oversold LONG: score {sc} слишком низкий")
        else:
            parts_missing = []
            if pct24 is not None and pct24 > os_threshold:
                parts_missing.append(f"падение {pct24:.1f}% &gt; порога {os_threshold:.1f}%")
            if rsi is not None and rsi > RSI_OVERSOLD:
                parts_missing.append(f"RSI {rsi:.1f} &gt; {RSI_OVERSOLD}")
            if parts_missing:
                signals_missing.append("oversold LONG: " + ", ".join(parts_missing))

    # — momentum_down SHORT (15m) —
    if pct_15m is not None:
        DISABLED = {-2.0, -3.0}
        active = sorted(t for t in MOMENTUM_THRESHOLDS_DOWN if t not in DISABLED)
        for threshold in active:  # ascending: -10, -5 …
            if pct_15m <= threshold:
                if above_ema is True:
                    signals_missing.append(
                        f"momentum_down SHORT ({threshold:.0f}%): цена выше EMA-200 → заблокирован"
                    )
                elif rsi is not None and rsi < 60:
                    signals_missing.append(
                        f"momentum_down SHORT: RSI {rsi:.1f} &lt; 60 → заблокирован"
                    )
                else:
                    signals_triggered.append((
                        "momentum_down", "SHORT", 65,
                        f"{pct_15m:.1f}% за 15м (порог {threshold:.0f}%)",
                    ))
                break
        else:
            if pct_15m > 0:
                signals_missing.append(
                    f"momentum_down: рост +{pct_15m:.1f}% за 15м, нет нисходящего движения"
                )
            else:
                signals_missing.append(
                    f"momentum_down: −{abs(pct_15m):.1f}% за 15м, "
                    f"не достигает минимального порога ({min(active):.0f}%)"
                )

    # — confluence —
    near_high = _near_24h_high(ticker)
    c_flags: list[str] = []
    if rsi is not None and rsi >= RSI_OVERBOUGHT:
        c_flags.append(f"RSI {rsi:.0f} перекуплен")
    elif rsi is not None and rsi <= RSI_OVERSOLD:
        c_flags.append(f"RSI {rsi:.0f} перепродан")
    if spike_ratio is not None and spike_ratio >= VOLUME_SPIKE_MULTIPLIER:
        c_flags.append(f"объём {spike_ratio:.1f}×")
    if near_high:
        c_flags.append("вблизи 24ч макс.")

    if len(c_flags) >= CONFLUENCE_MIN_SIGNALS:
        rec_line, _ = make_recommendation(
            rsi=rsi,
            spike_ratio=spike_ratio,
            broke_weekly=False,
            broke_monthly=False,
            near_24h_high=near_high,
            above_ema200=above_ema,
        )
        rec_label = _rec_label(rec_line)
        if rec_label == "SHORT" and above_ema is True:
            signals_missing.append(
                f"confluence SHORT: блокирован EMA-200 (цена выше)"
            )
        elif rec_label in ("LONG", "SHORT"):
            sc_side = "sell" if rec_label == "SHORT" else "buy"
            sc = compute_signal_score(
                "confluence", sc_side,
                rsi=rsi, above_ema=above_ema, spike_ratio=spike_ratio,
            )
            signals_triggered.append((
                "confluence", rec_label, sc,
                " + ".join(c_flags),
            ))
        else:
            signals_missing.append(
                f"confluence: смешанные сигналы ({', '.join(c_flags)})"
            )
    else:
        need = CONFLUENCE_MIN_SIGNALS - len(c_flags)
        if c_flags:
            signals_missing.append(
                f"confluence: {', '.join(c_flags)} — нужно ещё {need} сигнал(а)"
            )
        else:
            signals_missing.append("confluence: нет активных сигналов (RSI нейтраль, объём норм.)")

    # ── Best signal ────────────────────────────────────────────────────────
    best: tuple[str, str, int, str] | None = None
    if signals_triggered:
        best = max(signals_triggered, key=lambda x: x[2])

    sig_type  = best[0] if best else None
    direction = best[1] if best else None
    score     = best[2] if best else None
    reason    = best[3] if best else None

    # ── TP / SL / R:R ─────────────────────────────────────────────────────
    sl_price = tp_price = rr = None
    if price and direction:
        if atr and atr > 0:
            if direction == "LONG":
                sl_price = price - 1.5 * atr
                tp_price = price + 3.0 * atr
            else:
                sl_price = price + 1.5 * atr
                tp_price = price - 3.0 * atr
        else:
            # Fallback: fixed SL 2% / TP 4% LONG, TP 6% SHORT
            if direction == "LONG":
                sl_price = price * 0.98
                tp_price = price * (1 + HIT_RATE_TP_PCT / 100)
            else:
                sl_price = price * 1.02
                tp_price = price * (1 - HIT_RATE_TP_PCT_SHORT / 100)
        if sl_price and tp_price and sl_price > 0 and tp_price > 0:
            rr = abs(tp_price - price) / abs(sl_price - price)

    # ── Trend alignment ───────────────────────────────────────────────────
    trend_aligned: bool | None = None
    if direction:
        if (direction == "LONG" and regime == "BULL") or (direction == "SHORT" and regime == "BEAR"):
            trend_aligned = True
        elif (direction == "LONG" and regime == "BEAR") or (direction == "SHORT" and regime == "BULL"):
            trend_aligned = False

    # ── Build message ──────────────────────────────────────────────────────
    lines: list[str] = [f"🔍 <b>АНАЛИЗ {symbol}</b>", ""]

    # 1. Current data
    lines.append("📊 <b>ТЕКУЩИЕ ДАННЫЕ:</b>")
    lines.append(f"Цена: <b>${price:,.6g}</b>")
    if volume_usdt is not None:
        if volume_usdt >= 1_000_000:
            vol_str = f"${volume_usdt / 1_000_000:.1f}M"
        else:
            vol_str = f"${volume_usdt / 1_000:.0f}K"
        lines.append(f"Объём 24ч: <b>{vol_str}</b>")
    if pct24 is not None:
        sign = "+" if pct24 >= 0 else ""
        lines.append(f"Изменение 24ч: <b>{sign}{pct24:.2f}%</b>")
    lines.append("")

    # 2. Indicators
    lines.append("📈 <b>ИНДИКАТОРЫ:</b>")
    if rsi is not None:
        rsi_lbl = (
            "перекуплен 🔥" if rsi >= RSI_OVERBOUGHT
            else "перепродан 🧊" if rsi <= RSI_OVERSOLD
            else "нейтраль"
        )
        lines.append(f"RSI (14): <b>{rsi:.1f}</b> → {rsi_lbl}")
    else:
        lines.append("RSI (14): <i>нет данных</i>")

    if ema200 is not None and ema_pct is not None:
        ema_dir = "выше ↑" if above_ema else "ниже ↓"
        lines.append(f"EMA-200 (4ч): <b>${ema200:,.6g}</b>")
        lines.append(f"Цена vs EMA: <b>{ema_dir}</b> на {abs(ema_pct):.2f}%")
    else:
        lines.append("EMA-200 (4ч): <i>нет данных</i>")

    if spike_ratio is not None:
        vol_lbl = " 🚀" if spike_ratio >= VOLUME_SPIKE_MULTIPLIER else ""
        lines.append(f"Volume Spike 5м: <b>{spike_ratio:.1f}×</b> от среднего{vol_lbl}")
    else:
        lines.append("Volume Spike 5м: <i>нет данных</i>")

    if supertrend is not None:
        st_icon = "🟢" if supertrend == "UP" else "🔴"
        lines.append(f"Supertrend: {st_icon} <b>{supertrend}</b>")
    else:
        lines.append("Supertrend: <i>нет данных</i>")
    lines.append("")

    # 3. Recommendation
    lines.append("🎯 <b>РЕКОМЕНДАЦИЯ:</b>")
    if direction and reason and score is not None:
        dir_icon = "📈" if direction == "LONG" else "📉"
        type_labels = {
            "overheated_24h": "перегрев 24ч",
            "oversold_24h":   "перепроданность 24ч",
            "momentum_down":  "импульс вниз 15м",
            "confluence":     "конфлюэнс",
        }
        type_lbl = type_labels.get(sig_type or "", sig_type or "")
        lines.append(f"Сигнал: {dir_icon} <b>{direction}</b> ({type_lbl})")
        lines.append(f"Причина: {reason}")
        lines.append(f"Сила: <b>{score}/100</b> ({_strength_label(score)})")
    else:
        lines.append("Сигнал: <b>НЕТ СИГНАЛА</b>")
        # Pick most informative missing condition
        if signals_missing:
            # Prefer the one that got closest (has numeric thresholds)
            lines.append(f"Причина: {signals_missing[0]}")
        lines.append("Сила: —")
    lines.append("")

    # 4. Risk management
    lines.append("🛡️ <b>УПРАВЛЕНИЕ РИСКОМ:</b>")
    if sl_price and tp_price and rr and direction and price:
        sl_pct_val = abs(sl_price - price) / price * 100
        tp_pct_val = abs(tp_price - price) / price * 100
        sl_sign = "+" if direction == "SHORT" else "−"
        tp_sign = "+" if direction == "LONG" else "−"
        lines.append(f"Stop Loss: <b>${sl_price:,.6g}</b> ({sl_sign}{sl_pct_val:.2f}%)")
        lines.append(f"Take Profit: <b>${tp_price:,.6g}</b> ({tp_sign}{tp_pct_val:.2f}%)")
        lines.append(f"R/R: 1:{rr:.1f}")
    else:
        lines.append("Stop Loss / Take Profit: нет активного сигнала")
    lines.append("")

    # 5. Market phase
    regime_icons = {"BULL": "🟢", "BEAR": "🔴", "NEUTRAL": "⚪"}
    regime_icon = regime_icons.get(regime, "⚪")
    lines.append(f"🌍 <b>ФАЗА РЫНКА:</b> {regime_icon} {regime}")
    if direction:
        if trend_aligned is True:
            lines.append("Статус: ✅ По тренду")
        elif trend_aligned is False:
            lines.append("Статус: ⚠️ Против тренда")
        else:
            lines.append("Статус: ➡️ Тренд нейтральный")

    # 6. Notes
    notes: list[str] = []
    if above_ema is True and direction == "SHORT":
        notes.append("цена выше EMA-200 — часть SHORT-сигналов заблокирована ботом")
    elif above_ema is False and direction == "LONG":
        notes.append("цена ниже EMA-200 — возможно продолжение нисходящего тренда")
    if supertrend and direction:
        st_aligned = (supertrend == "UP" and direction == "LONG") or \
                     (supertrend == "DOWN" and direction == "SHORT")
        if not st_aligned:
            notes.append(f"Supertrend {supertrend} расходится с сигналом {direction}")
    if not direction and len(signals_missing) > 1:
        notes.append("нет ни одного активного сигнала — монета в нейтральной зоне")

    if notes:
        lines.append("")
        lines.append(f"💡 <i>Заметка: {notes[0]}</i>")

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

@app.route("/ping", methods=["GET"])
def ping():
    return "alive"


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
# Public read-only API for the web app
# ---------------------------------------------------------------------------

def _classify_side(alert_type: str, recommendation: str | None) -> str:
    if recommendation:
        rec = recommendation.upper()
        if "ЛОНГ" in rec or "LONG" in rec or "BUY" in rec:
            return "buy"
        if "ШОРТ" in rec or "SHORT" in rec or "SELL" in rec:
            return "sell"
    a = (alert_type or "").lower()
    if a.startswith(("oversold", "momentum_up", "high_break",
                     "weekly_high", "monthly_high", "vol_spike",
                     "new_listing", "new_coin", "rsi_oversold")):
        return "buy"
    if a.startswith(("overheated", "momentum_down", "pump_24h",
                     "rsi_overbought", "volume_surge_short")):
        return "sell"
    if a.startswith("volume_surge_long"):
        return "buy"
    return "neutral"


def _label_alert_type(alert_type: str) -> str:
    a = (alert_type or "")
    base = {
        "pump_24h": "Перегрев +30% за 24ч",
        "volume_surge_short": "Объёмный взрыв + CRSI (SHORT)",
        "volume_surge_long": "Объёмный взрыв + CRSI (LONG)",
        "overheated_24h": "Перегрев 24ч",
        "overheated": "Перегрев",
        "oversold_24h": "Перепродан 24ч",
        "oversold": "Перепродан",
        "rsi_overbought": "RSI перекуплен",
        "rsi_oversold": "RSI перепродан",
        "high_break": "Пробой максимума",
        "weekly_high": "Недельный максимум",
        "monthly_high": "Месячный максимум",
        "vol_spike": "Всплеск объёма",
        "confluence": "Совпадение сигналов",
        "new_listing": "Новый листинг",
        "new_coin": "Новая монета CoinGecko",
    }
    if a in base:
        return base[a]
    if a == "momentum_long":
        return "Памп иссякает (fade SHORT)"
    if a.startswith("momentum_up_"):
        return f"Импульс вверх ×{a.rsplit('_', 1)[-1]}"
    if a.startswith("momentum_down_"):
        return f"Импульс вниз ×{a.rsplit('_', 1)[-1]}"
    return a


@app.route("/bot-api/signals/recent", methods=["GET"])
def api_signals_recent():
    try:
        limit = max(1, min(int(request.args.get("limit", 50)), 200))
    except (TypeError, ValueError):
        limit = 50
    side_filter = (request.args.get("side") or "").strip().lower()
    if side_filter not in ("buy", "sell"):
        side_filter = ""

    signals: list[dict] = []
    last_id: int | None = None
    page_size = limit if not side_filter else limit * 4
    max_scans = 6
    try:
        with _db_lock:
            conn = _get_db()
            for _ in range(max_scans):
                if last_id is None:
                    cur = conn.execute(
                        "SELECT id, ts, symbol, alert_type, recommendation, "
                        "price_at_alert, price_3m, price_5m, price_15m, price_1h, price_4h, score "
                        "FROM alerts ORDER BY id DESC LIMIT ?",
                        (page_size,),
                    )
                else:
                    cur = conn.execute(
                        "SELECT id, ts, symbol, alert_type, recommendation, "
                        "price_at_alert, price_3m, price_5m, price_15m, price_1h, price_4h, score "
                        "FROM alerts WHERE id < ? ORDER BY id DESC LIMIT ?",
                        (last_id, page_size),
                    )
                rows = cur.fetchall()
                if not rows:
                    break
                for r in rows:
                    last_id = r[0]
                    side = _classify_side(r[3], r[4])
                    if side_filter and side != side_filter:
                        continue
                    signals.append({
                        "id": r[0],
                        "ts": r[1],
                        "symbol": r[2],
                        "alertType": r[3],
                        "alertTypeLabel": _label_alert_type(r[3]),
                        "recommendation": r[4],
                        "side": side,
                        "priceAtAlert": r[5],
                        "price3m":  r[6],
                        "price5m":  r[7],
                        "price15m": r[8],
                        "price1h":  r[9],
                        "price4h":  r[10],
                        "score":    r[11],
                    })
                    if len(signals) >= limit:
                        break
                if len(signals) >= limit or not side_filter:
                    break
    except Exception as e:
        logger.warning("api_signals_recent failed: %s", e)
        return jsonify({"error": "db_error"}), 500

    return jsonify({"signals": signals, "count": len(signals)})


@app.route("/bot-api/signals/stats", methods=["GET"])
def api_signals_stats():
    try:
        with _db_lock:
            conn = _get_db()
            total = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
            now_ts = int(time.time())
            last_24h = conn.execute(
                "SELECT COUNT(*) FROM alerts WHERE ts >= ?",
                (now_ts - 86400,),
            ).fetchone()[0]
            grouped = conn.execute(
                "SELECT alert_type, recommendation, COUNT(*) FROM alerts "
                "WHERE ts >= ? GROUP BY alert_type, recommendation",
                (now_ts - 86400 * 7,),
            ).fetchall()
    except Exception as e:
        logger.warning("api_signals_stats failed: %s", e)
        return jsonify({"error": "db_error"}), 500

    type_agg: dict[str, dict] = {}
    for alert_type, rec, count in grouped:
        side = _classify_side(alert_type, rec)
        entry = type_agg.setdefault(alert_type, {
            "alertType": alert_type,
            "label": _label_alert_type(alert_type),
            "count": 0,
            "buy": 0,
            "sell": 0,
            "neutral": 0,
        })
        entry["count"] += count
        entry[side] += count
    rows_out = sorted(type_agg.values(), key=lambda e: e["count"], reverse=True)
    for entry in rows_out:
        if entry["buy"] >= entry["sell"] and entry["buy"] >= entry["neutral"]:
            entry["side"] = "buy"
        elif entry["sell"] >= entry["neutral"]:
            entry["side"] = "sell"
        else:
            entry["side"] = "neutral"

    return jsonify({
        "total": total,
        "last24h": last_24h,
        "byTypeLast7d": rows_out,
    })


@app.route("/bot-api/signals/performance", methods=["GET"])
def api_signals_performance():
    """Backtest-style performance per alert type.

    For each (alert_type, side) bucket within the window, compute how many
    follow-up prices we have at 15m/1h/4h, the win-rate (price moved in the
    expected direction), and the average signed return aligned with that
    direction. Buy signals expect price to go up; sell signals expect it to
    go down. Neutral signals are excluded since they have no expected side.
    """
    try:
        window_arg = (request.args.get("window") or "30d").strip().lower()
        if window_arg.endswith("d"):
            window_days = max(1, min(int(window_arg[:-1]), 365))
        else:
            window_days = max(1, min(int(window_arg), 365))
    except (TypeError, ValueError):
        window_days = 30

    now_ts = int(time.time())
    since = now_ts - 86400 * window_days
    # require at least 4h of settle time so the 4h follow-up could be filled
    # 5-minute settle so even 3m/5m follow-ups can appear; longer horizons
    # naturally have fewer follow-ups for recent signals — that is expected.
    settled_before = now_ts - 5 * 60

    try:
        with _db_lock:
            conn = _get_db()
            rows = conn.execute(
                "SELECT alert_type, recommendation, price_at_alert, "
                "price_3m, price_5m, price_15m, price_1h, price_4h "
                "FROM alerts WHERE ts >= ? AND ts <= ?",
                (since, settled_before),
            ).fetchall()
    except Exception as e:
        logger.warning("api_signals_performance failed: %s", e)
        return jsonify({"error": "db_error"}), 500

    buckets: dict[tuple[str, str], dict] = {}
    for alert_type, rec, p0, p3m, p5m, p15, p1h, p4h in rows:
        side = _classify_side(alert_type, rec)
        if side == "neutral":
            continue
        if not p0 or p0 <= 0:
            continue
        key = (alert_type, side)
        b = buckets.setdefault(key, {
            "alertType": alert_type,
            "label": _label_alert_type(alert_type),
            "side": side,
            "count": 0,
            "horizons": {
                h: {"followups": 0, "wins": 0, "sumReturn": 0.0}
                for h in ("3m", "5m", "15m", "1h", "4h")
            },
        })
        b["count"] += 1
        for horizon, pf in (
            ("3m", p3m), ("5m", p5m), ("15m", p15), ("1h", p1h), ("4h", p4h)
        ):
            if pf is None or pf <= 0:
                continue
            change = (pf - p0) / p0
            signed = change if side == "buy" else -change
            stat = b["horizons"][horizon]
            stat["followups"] += 1
            stat["sumReturn"] += signed
            if signed > 0:
                stat["wins"] += 1

    out = []
    for b in buckets.values():
        horizons_out = {}
        for h, stat in b["horizons"].items():
            f = stat["followups"]
            horizons_out[h] = {
                "followups": f,
                "winRate": (stat["wins"] / f) if f else None,
                "avgReturn": (stat["sumReturn"] / f) if f else None,
            }
        out.append({
            "alertType": b["alertType"],
            "label": b["label"],
            "side": b["side"],
            "count": b["count"],
            "horizons": horizons_out,
        })
    out.sort(key=lambda e: e["count"], reverse=True)

    return jsonify({
        "windowDays": window_days,
        "since": since,
        "until": settled_before,
        "totalSignals": sum(e["count"] for e in out),
        "byType": out,
    })


@app.route("/bot-api/positions", methods=["GET"])
def api_positions():
    """Return all currently active position monitors with live PnL."""
    with _monitors_lock:
        monitors = list(_active_monitors.values())

    now = time.time()
    result = []
    for m in sorted(monitors, key=lambda x: x.ts_open):
        current = _get_current_price(m.symbol)
        if current is not None:
            pnl_pct = (
                (current - m.entry) / m.entry * 100 if m.direction == "LONG"
                else (m.entry - current) / m.entry * 100
            )
        else:
            current = None
            pnl_pct = None
        result.append({
            "id":             m.position_id,
            "symbol":         m.symbol,
            "direction":      m.direction,
            "entry":          m.entry,
            "currentPrice":   current,
            "slPrice":        m.sl_price,
            "tpPrice":        m.tp_price,
            "pnlPct":         round(pnl_pct, 4) if pnl_pct is not None else None,
            "tsOpen":         m.ts_open,
            "elapsedSeconds": int(now - m.ts_open),
        })

    return jsonify({"count": len(result), "positions": result})


@app.route("/bot-api/status", methods=["GET"])
def api_status():
    with state_lock:
        last_run_raw = state["last_run"]
    last_run_ts: int | None = None
    if isinstance(last_run_raw, (int, float)):
        last_run_ts = int(last_run_raw)
    elif isinstance(last_run_raw, str) and last_run_raw:
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(last_run_raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            last_run_ts = int(dt.timestamp())
        except (ValueError, TypeError):
            last_run_ts = None
    with state_lock:
        return jsonify({
            "initialized": state["initialized"],
            "trackedPairs": len(state["known_pairs"]),
            "silenced": state["silenced"],
            "lastRun": last_run_ts,
            "lastRunSummary": state["last_run_summary"],
            "activeBinanceHost": BINANCE_BASE,
        })


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


# Restore last-known-good Binance host from disk so the first cycle after a
# restart hits the working CDN immediately instead of running through 4
# blocked hosts. _binance_get will keep it up to date going forward.
_restored = _load_bot_state().get("binance_base")
if _restored and _restored in BINANCE_HOSTS:
    BINANCE_BASE = _restored
    logger.info("Restored active Binance host from state: %s", _restored)


def backup_alerts_db() -> None:
    """Take a timestamped copy of alerts.db and prune to the last N snapshots.
    Uses sqlite3's online backup API so it's safe even while the DB is being
    written to by another thread."""
    try:
        os.makedirs(ALERTS_DB_BACKUP_DIR, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
        dst_path = os.path.join(ALERTS_DB_BACKUP_DIR, f"alerts_{ts}.db")
        # Online backup — atomic, doesn't require a write lock for long.
        src = sqlite3.connect(HIT_RATE_DB_PATH)
        try:
            dst = sqlite3.connect(dst_path)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        # Rotate: keep the newest ALERTS_DB_BACKUP_KEEP files only.
        # `existing` already includes the file we just wrote.
        existing = sorted(
            f for f in os.listdir(ALERTS_DB_BACKUP_DIR)
            if f.startswith("alerts_") and f.endswith(".db")
        )
        for stale in existing[:-ALERTS_DB_BACKUP_KEEP]:
            try:
                os.remove(os.path.join(ALERTS_DB_BACKUP_DIR, stale))
            except OSError:
                pass
        kept = min(len(existing), ALERTS_DB_BACKUP_KEEP)
        logger.info("alerts.db backup written: %s (kept %d snapshots)",
                    os.path.basename(dst_path), kept)
    except Exception as e:
        logger.exception("alerts.db backup failed: %s", e)


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
scheduler.add_job(
    backup_alerts_db, "interval", hours=1, id="alerts_db_backup",
)
# Skip background threads when imported under tests (pytest's conftest sets
# TESTING=1). Strict truthy parsing so an accidental TESTING=0/false in prod
# does NOT silently disable the workers.
if os.environ.get("TESTING", "").strip().lower() not in {"1", "true", "yes"}:
    restore_position_monitors()
    scheduler.start()
    start_command_polling()
    start_watchdog()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
