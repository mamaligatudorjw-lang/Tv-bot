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

# Also write to a persistent file so we can debug even when terminal
# output is not captured by the workflow log viewer.
_file_log_path = os.path.join(os.path.dirname(__file__), "bot_debug.log")
try:
    _fh = logging.FileHandler(_file_log_path, encoding="utf-8")
    _fh.setLevel(logging.INFO)
    _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logging.getLogger().addHandler(_fh)
except Exception:
    pass  # non-fatal — terminal logging still works

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
# Gate.io Futures — primary data source (replaces Binance Spot).
# Binance Spot is blocked on many Futures symbols; Gate.io Futures API is
# accessible from Replit and covers a wider set of USDT perpetuals.
# ---------------------------------------------------------------------------
GATEIO_BASE = "https://api.gateio.ws/api/v4/futures/usdt"

def _to_gate(symbol: str) -> str:
    """Convert internal symbol 'AKEUSDT' → Gate.io contract 'AKE_USDT'."""
    if symbol.endswith("USDT"):
        return symbol[:-4] + "_USDT"
    return symbol

def _from_gate(symbol: str) -> str:
    """Convert Gate.io contract 'AKE_USDT' → internal symbol 'AKEUSDT'."""
    if symbol.endswith("_USDT"):
        return symbol[:-5] + "USDT"
    return symbol

def _gateio_get(path: str, params: dict | None = None, timeout: int = 10):
    """GET request to Gate.io Futures REST API.

    A global semaphore (GATEIO_MAX_CONCURRENT) caps the number of in-flight
    requests so we don't trigger Gate.io's 429 rate limit during bulk scans.
    """
    url = f"{GATEIO_BASE}{path}"
    with _gateio_sem:
        resp = requests.get(url, params=params, timeout=timeout,
                            headers={"Accept": "application/json"})
    resp.raise_for_status()
    return resp

def _gateio_klines(symbol: str, interval: str, limit: int) -> list:
    """Fetch Gate.io candlesticks and return them in Binance-compatible format.

    Binance kline indices used by this bot:
      [0] open_time   [2] high   [3] low   [4] close
      [5] base_volume [7] quote_volume_usdt
    Gate.io fields: t(ts) o(open) h(high) l(low) c(close) v(base) sum(quote USDT)
    """
    resp = _gateio_get(
        "/candlesticks",
        params={"contract": _to_gate(symbol), "interval": interval, "limit": limit},
        timeout=10,
    )
    raw = resp.json()
    # Convert to Binance-compatible list: [ts, o, h, l, c, v, ts, sum, ...]
    return [
        [c["t"], c["o"], c["h"], c["l"], c["c"], str(c["v"]), c["t"], c["sum"]]
        for c in raw
    ]

def _gateio_ticker(symbol: str) -> dict | None:
    """Fetch single contract ticker from Gate.io and return in Binance-compatible format."""
    try:
        resp = _gateio_get("/tickers", params={"contract": _to_gate(symbol)}, timeout=8)
        data = resp.json()
        if not data:
            return None
        t = data[0]
        return {
            "symbol":             symbol,
            "lastPrice":          t.get("last", "0"),
            "price":              t.get("last", "0"),
            "priceChangePercent": t.get("change_percentage", "0"),
            "quoteVolume":        t.get("volume_24h_quote", "0"),
            "highPrice":          t.get("high_24h", "0"),
            "lowPrice":           t.get("low_24h", "0"),
        }
    except Exception as e:
        logger.warning("_gateio_ticker %s failed: %s", symbol, e)
        return None

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
    "liquid_symbols": [],              # list[str] liquid pairs from last cycle (for startup ranking refresh)
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
    "last_listing_dump_alerted": {},        # symbol -> ts (listing_dump_long TEST cooldown)
    "last_listing_peak_alerted": {},        # symbol -> ts (listing_peak_short TEST cooldown)
    "last_pump_alerted": {},           # symbol -> ts (24h +30% pump cooldown)
    "last_vol_surge_alerted": {},      # symbol -> ts (300% vol + CRSI extreme cooldown)
    "last_streak_1h_alerted": {},      # symbol -> ts (intraday 6h+ streak LONG/SHORT cooldown)
    "last_vwap_rev_alerted": {},       # symbol -> ts (VWAP reversion shadow cooldown)
    "last_liq_reversal_alerted": {},   # symbol -> ts (liq reversal confirmation shadow cooldown)
    "last_bb_squeeze_alerted": {},     # symbol -> ts (Bollinger squeeze shadow cooldown)
    "last_ema_cross_alerted": {},      # symbol -> ts (EMA crossover shadow cooldown)
    "shadow_notif_today": {},          # {notif_key: {"date": "YYYY-MM-DD", "count": int}}
    "last_lsr_shift_alerted": {},      # symbol -> ts (whale LSR flip cooldown)
    "lsr_prev": {},                    # symbol -> float (previous LSR value for shift detection)
    "prior_day_pct": {},               # symbol -> float: yesterday's close-to-close % change
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
CONFLUENCE_MIN_SIGNALS = 3         # only alert when ≥ this many signals fire on same coin in one cycle
CONFLUENCE_LONG_MIN_SCORE  = 65   # skip confluence LONG when signal score is below this threshold
CONFLUENCE_SHORT_MIN_SCORE = 65   # skip confluence SHORT when signal score is below this threshold
CONFLUENCE_RSI_MAX_LONG    = 45   # LONG only when RSI ≤ this (perversely oversold)
CONFLUENCE_RSI_MIN_SHORT   = 65   # SHORT only when RSI ≥ this (overbought confirmed)
MIN_VOLUME_CONFLUENCE = 500_000   # 24h USDT volume floor for confluence entries
MAX_OPEN_CONFLUENCE_POSITIONS = 15  # cap on simultaneous real confluence demo positions
# Positions opened BEFORE this timestamp (task-53 deploy, 2026-08-12 07:26:31 UTC) are
# legacy bets that run to their own SL/TP; they must not count against the new 15-slot cap.
CONFLUENCE_CAP_CUTOFF_TS = 1_786_519_591
# ---------------------------------------------------------------------------
# Non-crypto symbol filter — Gate.io Futures lists synthetic equity/commodity
# contracts alongside crypto. Block them from ALL signal types by prefix.
# Extend this set if new non-crypto pairs appear.
NON_CRYPTO_PREFIXES: frozenset[str] = frozenset({
    # Precious metals / commodities
    "XAG", "XAU", "SLVON",
    # US equity derivatives
    "AAPL", "AAPLX", "GOOGL", "GOOGLX", "AMZN", "MSFT",
    "NVDA", "TSLA", "META", "NFLX", "BABA",
    # EU / Asia equity derivatives
    "ASML", "TENCENT", "TENCENTHKD", "XIAOMI", "XIAOMIHKD",
    # Indices
    "SPX", "NDX",
})


def _is_crypto_pair(symbol: str) -> bool:
    """Return True only for genuine crypto USDT pairs.

    Gate.io Futures includes synthetic stock/commodity/index contracts
    (XAGUSDT, AAPLXUSDT, SPXUSDT, …).  Those are excluded from all signal
    types by checking whether the symbol starts with a known non-crypto prefix.
    """
    for prefix in NON_CRYPTO_PREFIXES:
        if symbol.startswith(prefix):
            return False
    return True


# ---------------------------------------------------------------------------
# Signal enable flags — включить/выключить каждый тип сигналов
# ---------------------------------------------------------------------------
CONFLUENCE_ENABLED              = True   # конфлюенс (LONG + SHORT)
CONFLUENCE_BTC_FILTER_ENABLED   = False  # False = не блокировать по тренду BTC, торговать по тренду монеты
OVERHEATED_ENABLED              = True   # перегрев 24h (LONG) — монета сильно выросла
OVERSOLD_REVERSAL_CANDLE_FILTER = False  # True = требовать зелёную свечу для oversold LONG
OVERSOLD_1M_GREEN_CANDLES     = 3       # сколько зелёных 1м свечей подряд нужно перед отправкой
                                         # False = входить сразу (текущий режим)
AI_VETO_ENABLED                 = False  # False = ИИ не блокирует сигналы (все теневые ai_veto → реальные)
VOL_SURGE_ENABLED               = False  # объёмный всплеск + CRSI
BREAKDOWN_ENABLED               = False  # пробой вниз SHORT
MOMENTUM_LONG_ENABLED           = False  # моментум LONG (рост)
NEW_LISTING_ENABLED             = False  # новые листинги SHORT
LISTING_PEAK_ENABLED            = False  # пик листинга SHORT
STREAK_1H_ENABLED               = True   # серия свечей (1h streak)
WHALE_LSR_ENABLED               = False  # сдвиг позиций китов
COINGECKO_CHECK_INTERVAL_MIN = 30  # CoinGecko "upcoming listing" monitor cadence
COINGECKO_NEW_COIN_ALERTS_ENABLED = False  # «МОНЕТА ДО BINANCE» alerts — off until useful
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
# Suppress overheated SHORT when the coin was already trending up the day before.
# If yesterday's close-to-close change > this threshold the move is a multi-day
# trend, not a single-day spike ripe for reversal.
PRIOR_TREND_BLOCK_PCT = 15.0        # yesterday up >15% → it's a trend, don't fade

# --- New listing pump→dump SHORT (TEST) ---
NEW_LISTING_WINDOW_H     = 48     # only watch coins listed in last 48h
NEW_LISTING_PUMP_PCT     = 20.0   # coin must be ≥+20% above its listing price
NEW_LISTING_PUMP_RSI_MIN = 65.0   # RSI must confirm overheating
NEW_LISTING_SHORT_COOLDOWN = 14400  # 4h cooldown per symbol

# --- Listing dump → LONG recovery signal ---
# Catches coins like MUUUSDT/SOXLUSDT: listed in last 90 days, already dumped
# ≥40% from their post-listing 30d peak, RSI confirms oversold → likely bounce.
NEW_LISTING_DUMP_WINDOW_DAYS  = 90     # listing must be within this many days
NEW_LISTING_DUMP_FROM_PEAK_PCT = 40.0  # price must be ≥40% below 30d high
NEW_LISTING_DUMP_RSI_MAX       = 35.0  # RSI must confirm oversold
NEW_LISTING_DUMP_MIN_VOL       = 50_000  # minimum 24h USDT volume
NEW_LISTING_DUMP_COOLDOWN      = 14400  # 4h cooldown per symbol

# --- New listing ATH peak → SHORT (long-duration dump trade) ---
# Fires when a recently listed coin (≤72h) has pumped hard AND RSI is very
# overbought — entry at the peak, then held for weeks/months as it dumps.
NEW_LISTING_PEAK_WINDOW_H    = 72     # listing within last 72 hours
NEW_LISTING_PEAK_PUMP_MIN    = 30.0   # pumped ≥30% from listing price
NEW_LISTING_PEAK_RSI_MIN     = 70.0   # RSI ≥ 70 — very overbought
NEW_LISTING_PEAK_SL_PCT      = 15.0   # SL 15% above entry (wide, avoids noise)
NEW_LISTING_PEAK_TP_PCT      = 60.0   # TP 60% below entry (massive dump target)
NEW_LISTING_PEAK_TIMEOUT_H   = 720    # hold up to 30 days (720 hours)
NEW_LISTING_PEAK_COOLDOWN    = 86400  # 24h cooldown per symbol

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
GATEIO_MAX_CONCURRENT = 10   # max parallel Gate.io API requests; raise if 429s disappear
MIN_VOLUME_USDT = 50_000

# Semaphore that caps concurrent Gate.io HTTP calls across all threads.
_gateio_sem = threading.Semaphore(GATEIO_MAX_CONCURRENT)

# Momentum (15-min price change) — visually distinct alerts at each tier
MOMENTUM_THRESHOLDS_UP = (2.0, 3.0, 5.0, 10.0)
MOMENTUM_THRESHOLDS_DOWN = (-2.0, -3.0, -5.0, -10.0)
MOMENTUM_COOLDOWN = 1800           # 30 min per (symbol, threshold-tier)

# Overheated / oversold 24h combo alerts (price + RSI confirmation)
OVERHEATED_24H_PCT = 15.0
OVERSOLD_24H_PCT = -10.0
OVERHEATED_COOLDOWN = 14400        # 4h
OVERSOLD_COOLDOWN = 7200           # 2h

# Liquidity reversal confirmation shadow
# Shadow-only for ~3 weeks to compare vs plain liquidity (no confirmation).
# Confirmation: last 15m candle must be in reversal direction + elevated volume.
LIQUIDITY_REVERSAL_SHADOW      = True   # False = fully disable shadow; True = active
LIQUIDITY_REVERSAL_VOL_MULT    = 1.5    # reversal candle vol must be ≥1.5× 10-bar avg
LIQUIDITY_REVERSAL_ZONE_PCT    = 0.5    # zone must have been touched within 0.5% of low/high

# Top-gainers list threshold (used by /top30 — pure +X% mover, no alert).
# The pump_24h *alert* itself was removed in favor of volume_surge_{short,long}.
PUMP_24H_PCT = 30.0

# pump_24h_fade: fade the pump → SHORT signal
# Shadow-only for first ~2 weeks to validate hypothesis on fresh data.
# Условия: 24h рост ≥ PUMP_FADE_MIN_PCT + RSI > PUMP_FADE_RSI_MIN → SHORT.
# Включить реальные позиции: PUMP_FADE_SHADOW_ONLY = False
PUMP_FADE_SHADOW_ONLY   = True     # True = только shadow-сбор; False = реальная paper-торговля
PUMP_FADE_MIN_PCT       = 30.0     # мин. 24h рост % для квалификации
PUMP_FADE_RSI_MIN       = 65.0     # RSI выше порога — подтверждение перегрева
PUMP_FADE_MAX_PCT       = 200.0    # игнорировать крайние выбросы (листинговые пампы)
PUMP_FADE_COOLDOWN      = 86400    # 24ч кулдаун на символ
PUMP_FADE_MIN_VOL       = 500_000  # мин. объём $500k 24ч (отфильтровать мусор)
PUMP_FADE_MIN_SCORE     = 55       # мин. score для отправки алерта
# SL cap: pump-fade enters already-volatile coins, so ATR is naturally wider than
# confluence. 12% is a first approximation — wider than confluence (8%), but still
# prevents runaway losses like AVAAIUSDT −14.47%.  Review after 20+ closed trades.
PUMP_FADE_SL_MAX_PCT    = 12.0     # temporary; revise when distribution becomes clear

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
HIT_RATE_INTERVALS = (
    (180,    "3м"),
    (300,    "5м"),
    (900,    "15м"),
    (3600,   "1ч"),
    (14400,  "4ч"),
    (86400,  "1д"),
    (172800, "2д"),
    (259200, "3д"),
    (604800, "7д"),
)
HIT_RATE_WIN_PCT = 1.0             # >=1% move in predicted direction = win (legacy, kept for compat)
HIT_RATE_RETENTION_DAYS = 120
# SL/TP for simulated P&L in hit-rate stats (matches backtest defaults)
HIT_RATE_SL_PCT = 2.5              # stop-loss % (raised from 2.0 to reduce noise SL hits)
HIT_RATE_TP_PCT = 4.0              # take-profit % for LONG
HIT_RATE_TP_PCT_SHORT = 6.0        # take-profit % for SHORT (wider target, asymmetric)

# Timestamp when wick-based SL/TP detection was enabled in check_demo_positions.
# Positions closed BEFORE this ts used lastPrice only ("old" / "старые").
# Positions closed ON OR AFTER use last-completed-1m-candle low/high ("honest" / "честные").
WICK_CHECK_ENABLED_TS: int = 1786478696   # 2026-08-11 20:04:56 UTC

# --- Long-duration SHORT regime (2026-08-04) ---
# Backtest on 522+ historical signals from alerts.db: SHORT signals held
# 3–7 days average +7…+10% PnL with 73–80% win rate, while the old 4-hour
# scalp closed at −0.05%. LONG signals only work on the short horizon, so
# these overrides apply to SHORT positions only (LONG keeps the 4h scalp).
# listing_peak_short keeps its own explicit wider params (SL15/TP60/720h).
SHORT_HOLD_TIMEOUT_HOURS = 168.0   # hold SHORTs up to 7 days
SHORT_HOLD_SL_PCT        = 15.0    # wide stop above entry — survives noise
SHORT_HOLD_TP_PCT        = 20.0    # take-profit 20% below entry

# ATR-based SL/TP multipliers (crunch from demo data 2026-08-08):
#   9/10 real SHORT demo positions stopped out at avg SL=4.98% (1.5×ATR too tight).
#   The only TP hit had SL=15%/TP=20% (fixed fallback). Shadow SHORTs with
#   wider ATR had 34% win rate vs 10% for tight-ATR regulars.
#   Fix: raise SHORT SL to 2.0×ATR + enforce 2.5% minimum floor.
SHORT_ATR_SL_MULT    = 2.0    # was 1.5; wider stop survives intraday noise
SHORT_ATR_SL_MIN_PCT = 2.5    # floor: SL at least 2.5% above entry even if ATR is tiny
# TP is derived from actual SL distance × 2.0 to keep R:R 1:2
SHORT_ATR_TP_MULT_RR = 2.0    # TP = (SL dist) × this factor

# Confluence-specific ATR cap: SL distance cannot exceed this % of entry price.
# Prevents ultra-wide stops on volatile coins (e.g. COOKIEUSDT −16% SL incident).
# Applied via _compute_confluence_sl_tp() wrapper.
CONFLUENCE_ATR_SL_MAX_PCT = 8.0    # max SL distance as % of entry; 0=no cap

# In BULL market, counter-trend SHORTs require stronger conviction.
SHORT_BULL_REGIME_MIN_SCORE = 80   # raised from type-default 75 when regime=BULL
MAX_OPEN_POSITIONS = 50            # cap on simultaneous position monitors
# Раздельные лимиты по направлениям (2026-08-04): SHORT-ы теперь живут до
# 7 дней (listing_peak_short — до 30), поэтому без квоты они могут занять
# весь общий лимит и заблокировать 4ч LONG-скальпы. LONG-ам гарантирована
# своя квота, SHORT-ам — своя; сумма квот равна MAX_OPEN_POSITIONS.
MAX_OPEN_LONG_POSITIONS = 25       # квота для LONG (быстрые 4ч скальпы)
MAX_OPEN_SHORT_POSITIONS = 25      # квота для долгих SHORT-ов (до 7-30 дней)

# TOP signals: score >= this → «🏆 ТОП СИГНАЛ» marker in Telegram and a
# separate bucket in /demo (real vs shadow vs top comparison).
TOP_SIGNAL_SCORE = 70

# ---------------------------------------------------------------------------
# Feature flags — отключить фильтр без удаления кода
# ---------------------------------------------------------------------------
# bear_downtrend_blocks_long: блокировать LONG в BEAR если 3-4 дня вниз,
# НО ПРОПУСКАТЬ при 5+ днях — демо показало 90.9% WR при экстремальном перепроданости.
# 3-4д: 18% WR (правильно блокировать), 5д+: 90.9% WR (отскок дна, пропускать!)
BEAR_DOWNTREND_FILTER_ENABLED = True    # включён; логика 3-4д=блок, 5д+=пропуск

# pump_filter: блокировал LONG/SHORT во время активного памп/дамп тренда
# Разделён на два флага — демо показало разное поведение для LONG и SHORT:
#   SHORT + памп: данные показали что блокировка убивала 52% WR сигналы → отключена
#   LONG  + дамп: неправильно блокировать (тренд продолжения, 57% TP у теней)
PUMP_FILTER_SHORT_ENABLED = False       # ВЫКЛ: теневые данные показали, что памп → SHORT прибылен
PUMP_FILTER_LONG_ENABLED  = False       # не блокировать LONG во время дампа (оставить выкл.)
UPTREND_FLIP_MIN_CANDLES  = 4           # X-of-N gate: заблокировать SHORT если ≥ этого числа
UPTREND_FLIP_N_WINDOW     = 6           # из N последних 4h-свечей были бычьими (4of6 выбрано по бэктесту Aug 10-13)
SHADOW_ONLY_MODE          = True        # True = отправлять теневые сигналы в Telegram, реальные — молчать
SHADOW_MODE_EXEMPT_TYPES  = {"oversold_24h", "streak_1h", "confluence"}  # эти типы приходят как реальные сигналы (без 👻)
UPTREND_FLIP_INTERVAL     = "4h"        # таймфрейм для uptrend-флипа

# --- Display leverage for ROI calculation in Telegram messages ---
# All P&L / SL / TP percentages shown to the user are multiplied by this
# factor so the user sees ROI at their preferred leverage, not raw price move.
DISPLAY_LEVERAGE = 10

# ---------------------------------------------------------------------------
# Funding Rate scoring thresholds (№1) — крутить при необходимости
# ---------------------------------------------------------------------------
FUNDING_SHORT_THR_1  = 0.0005   # +0.05%: лонги платят → SHORT +10 баллов
FUNDING_SHORT_THR_2  = 0.0010   # +0.10%: лонги сильно платят → SHORT +15 баллов
FUNDING_SHORT_PTS_1  = 10
FUNDING_SHORT_PTS_2  = 15
FUNDING_LONG_THR_NEG = -0.0003  # −0.03%: шорты платят → LONG +10 баллов
FUNDING_LONG_PTS_BON = 10
FUNDING_LONG_THR_BAD = 0.0010   # +0.10%: все в лонгах → LONG −10 баллов
FUNDING_LONG_PTS_PEN = 10

# ---------------------------------------------------------------------------
# L/S Ratio scoring thresholds (№2) — крутить при необходимости
# ---------------------------------------------------------------------------
LSR_SHORT_THR = 1.8   # толпа в лонгах → SHORT +7 баллов
LSR_LONG_THR  = 0.7   # толпа в шортах → LONG +7 баллов
LSR_BONUS_PTS = 7

# ---------------------------------------------------------------------------
# Liquidation veto (№5)
# ---------------------------------------------------------------------------
LIQ_VETO_WINDOW   = 900    # 15 минут в секундах
LIQ_VETO_VOL_FRAC = 0.003  # 0.3% от 24ч объёма = крупное событие ликвидации
STATS_CACHE_TTL   = 60     # кэш contract_stats (сек/пара)
LIQ_CACHE_TTL     = 300    # кэш ликвидаций (сек/пара)


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
        body = ""
        try:
            body = resp.text[:300]
        except Exception:
            pass
        logger.error("Telegram send failed: %s | body: %s", e, body)
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
_WHALE_ALERT_API_KEY = os.environ.get("WHALE_ALERT_API_KEY", "")
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


_GEMINI_AI_COMMENTARY = True   # set False to disable AI comments on signals

def _call_gemini_text(
    prompt: str,
    max_tokens: int = 512,
    temperature: float = 0.7,
    timeout: int = 15,
) -> str | None:
    """Call Gemini with a plain-text prompt. Returns the response text or None."""
    if not _GEMINI_BASE_URL or not _GEMINI_API_KEY:
        return None
    url = (f"{_GEMINI_BASE_URL.rstrip('/')}/models/"
           f"{_GEMINI_VISION_MODEL}:generateContent")
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": temperature,
            # Disable built-in reasoning/thinking so ALL tokens go to output,
            # not to the hidden thinking buffer (critical for gemini-2.5-flash).
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    try:
        resp = requests.post(
            url,
            headers={"x-goog-api-key": _GEMINI_API_KEY},
            json=payload,
            timeout=timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        candidate = body["candidates"][0]
        finish = candidate.get("finishReason", "")
        # Collect only non-thought parts (skip thought parts from thinking models)
        parts = candidate["content"]["parts"]
        text = "".join(
            p.get("text", "") for p in parts if not p.get("thought", False)
        ).strip()
        if finish == "MAX_TOKENS":
            logger.warning("Gemini hit MAX_TOKENS — response truncated (max=%s).", max_tokens)
            text = text + "…"
        return text
    except Exception as e:
        logger.warning("Gemini text call failed: %s", _safe_err(e))
        return None


def _call_gemini_with_search(
    prompt: str,
    max_tokens: int = 256,
    temperature: float = 0.1,
    timeout: int = 25,
) -> tuple[str | None, str]:
    """Call Gemini with Google Search grounding enabled.

    Returns (text_response, source_snippet) where source_snippet is a brief
    citation extracted from groundingMetadata, or '' if no results were used.
    Fails open (None, '') on any error — callers must handle None as "allow".

    NOTE: thinkingConfig is intentionally omitted here — Google Search grounding
    is incompatible with thinkingBudget=0 in the Gemini 2.5 API and will cause
    a 400 error if included.
    """
    if not _GEMINI_BASE_URL or not _GEMINI_API_KEY:
        return None, ""
    url = (f"{_GEMINI_BASE_URL.rstrip('/')}/models/"
           f"{_GEMINI_VISION_MODEL}:generateContent")
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": temperature,
        },
    }
    try:
        resp = requests.post(
            url,
            headers={"x-goog-api-key": _GEMINI_API_KEY},
            json=payload,
            timeout=timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        candidate = body["candidates"][0]
        parts = candidate.get("content", {}).get("parts", [])
        text = "".join(
            p.get("text", "") for p in parts if not p.get("thought", False)
        ).strip()

        # Extract the first web source from grounding metadata (if any)
        source_snippet = ""
        grounding = candidate.get("groundingMetadata", {})
        chunks = grounding.get("groundingChunks", [])
        if chunks:
            web = chunks[0].get("web", {})
            title = web.get("title", "").strip()
            uri   = web.get("uri",   "").strip()
            if title:
                # Keep URI short so it fits in the Telegram message
                source_snippet = title if not uri else f"{title} — {uri[:80]}"

        logger.debug(
            "Gemini search veto for prompt snippet %r → %r (source: %r)",
            prompt[:60], text[:80], source_snippet[:60],
        )
        return text, source_snippet

    except Exception as e:
        logger.warning("Gemini search call failed: %s", _safe_err(e))
        return None, ""


def _ai_signal_commentary(
    symbol: str,
    recommendation: str,
    alert_type: str,
    score: int,
    rsi: float | None = None,
    pct24: float | None = None,
) -> str:
    """Get a short AI commentary for a signal. Returns '' on failure."""
    type_labels = {
        "confluence": "конфлюэнция нескольких сигналов",
        "oversold_24h": "перепроданность 24ч (RSI + сильное падение)",
        "overheated_24h": "перегретость 24ч (RSI + сильный рост)",
        "breakdown_short": "пробой уровня вниз",
        "volume_surge_short": "объёмный взрыв + CRSI перекупленность",
        "pump_24h_fade":      "памп 24ч → шорт (fade-стратегия)",
    }
    type_desc = type_labels.get(alert_type, alert_type)
    direction = "ЛОНГ" if recommendation == "LONG" else "ШОРТ"
    rsi_str = f"RSI {rsi:.1f}" if rsi is not None else "RSI н/д"
    pct_str = f"24ч {pct24:+.1f}%" if pct24 is not None else ""
    prompt = (
        f"Ты — опытный крипто-трейдер. Дай очень краткий комментарий (1-2 предложения, "
        f"НЕ больше 120 символов) на русском языке почему этот сигнал интересен или рискован.\n\n"
        f"Монета: {symbol}\n"
        f"Направление: {direction}\n"
        f"Тип: {type_desc}\n"
        f"Сила сигнала: {score}/100\n"
        f"{rsi_str}  {pct_str}\n\n"
        f"Пиши ТОЛЬКО сам комментарий — без вводных слов, заголовков и форматирования."
    )
    text = _call_gemini_text(prompt, max_tokens=150, temperature=0.75, timeout=12)
    if not text:
        return ""
    text = text[:160].strip()
    return f"\n🤖 <i>{html.escape(text)}</i>"


# ── Trend pump/dump filter ────────────────────────────────────────────────────

def _trend_pump_filter(
    symbol: str, recommendation: str, price: float
) -> tuple[bool, str]:
    """Block SHORT during active pump; block LONG during active dump.

    Conditions that block SHORT (symmetric conditions block LONG):
    • Price above EMA-50 (1h) AND 4h price change > +5 %, OR
    • Last 3 completed hourly candles all green with rising volume.

    Returns (blocked, reason). Fails open (False) on any API error.
    """
    if recommendation not in ("SHORT", "LONG"):
        return False, ""
    try:
        candles = _gateio_klines(symbol, "1h", 56)
        if not candles or len(candles) < 10:
            return False, ""

        opens  = [float(k[1]) for k in candles]
        closes = [float(k[4]) for k in candles]
        vols   = [float(k[7]) for k in candles]   # quote vol USDT

        ema50 = _calculate_ema(closes[:-1], min(50, len(closes) - 1)) if len(closes) > 50 else None
        # 4h change: last completed candle vs 4 candles earlier
        change_4h = ((closes[-2] - closes[-6]) / closes[-6] * 100
                     if len(closes) >= 6 else 0.0)

        # Last 3 COMPLETED candles (skip -1 which may still be forming)
        last3_o = opens[-4:-1]
        last3_c = closes[-4:-1]
        last3_v = vols[-4:-1]
        all_green  = all(c > o for o, c in zip(last3_o, last3_c))
        all_red    = all(c < o for o, c in zip(last3_o, last3_c))
        rising_vol = (
            len(last3_v) == 3
            and last3_v[1] > last3_v[0]
            and last3_v[2] > last3_v[1]
        )

        if recommendation == "SHORT":
            if ema50 and price > ema50 and change_4h > 15.0:
                return True, f"памп: +{change_4h:.1f}% за 4ч, цена выше EMA-50(1ч)"
            if all_green and rising_vol:
                return True, "3 зелёных 1ч-свечи подряд с ростом объёма — памп активен"
        else:  # LONG
            if ema50 and price < ema50 and change_4h < -5.0:
                return True, f"дамп: {change_4h:.1f}% за 4ч, цена ниже EMA-50(1ч)"
            if all_red and rising_vol:
                return True, "3 красных 1ч-свечи подряд с ростом объёма — дамп активен"
        return False, ""
    except Exception as exc:
        logger.debug("_trend_pump_filter %s: %s", symbol, exc)
        return False, ""


# ── Whale / large-trader stats ───────────────────────────────────────────────

def _get_gate_whale_stats(symbol: str) -> str:
    """Return a one-line summary of whale positioning for this contract.

    Uses Gate.io /contract_stats which is free and already authenticated
    (no extra key needed — same API used for klines).

    Fields:
      top_lsr_size   — Long/Short ratio of top traders by notional size.
                       >1.5 = heavily long, <0.67 = heavily short.
      last_funding_rate — positive = longs pay (bullish bias), negative = shorts pay.

    Returns '' on error or if the contract doesn't exist on Gate.io futures.
    """
    try:
        resp = _gateio_get(
            "/contract_stats",
            params={"contract": _to_gate(symbol), "interval": "1h", "limit": 1},
            timeout=6,
        )
        data = resp.json()
        if not data:
            return ""
        row = data[0]
        lsr = float(row.get("top_lsr_size") or 0)
        funding = float(row.get("last_funding_rate") or 0)
        if lsr <= 0:
            return ""

        if lsr >= 1.5:
            whale_emoji = "🐋🟢"
            whale_desc = f"Киты в лонге (LSR {lsr:.2f})"
        elif lsr <= 0.67:
            whale_emoji = "🐋🔴"
            whale_desc = f"Киты в шорте (LSR {lsr:.2f})"
        else:
            whale_emoji = "🐋"
            whale_desc = f"Киты нейтральны (LSR {lsr:.2f})"

        funding_pct = funding * 100
        funding_str = f"  ·  Фандинг: {funding_pct:+.4f}%"
        return f"{whale_emoji} {whale_desc}{funding_str}"
    except Exception as exc:
        logger.debug("_get_gate_whale_stats %s: %s", symbol, exc)
        return ""


# ---------------------------------------------------------------------------
# Cached contract_stats — funding rate & LSR (STATS_CACHE_TTL per pair)
# ---------------------------------------------------------------------------

_contract_stats_cache: dict[str, tuple[float, float, float]] = {}
# symbol → (cached_at_ts, lsr, funding_rate)
_contract_stats_lock = threading.Lock()


def _get_contract_stats_cached(symbol: str) -> tuple[float | None, float | None]:
    """Return (top_lsr_size, last_funding_rate) from Gate.io /contract_stats.

    Results cached per symbol for STATS_CACHE_TTL seconds.
    Returns (None, None) on any error — callers must handle gracefully.
    """
    now = time.time()
    with _contract_stats_lock:
        cached = _contract_stats_cache.get(symbol)
        if cached and now - cached[0] < STATS_CACHE_TTL:
            lsr_v = cached[1] if cached[1] > 0 else None
            return (lsr_v, cached[2])
    try:
        resp = _gateio_get(
            "/contract_stats",
            params={"contract": _to_gate(symbol), "interval": "1h", "limit": 1},
            timeout=6,
        )
        data = resp.json()
        if not data:
            return (None, None)
        row = data[0]
        lsr = float(row.get("top_lsr_size") or 0)
        funding = float(row.get("last_funding_rate") or 0)
        with _contract_stats_lock:
            _contract_stats_cache[symbol] = (now, lsr, funding)
        return (lsr if lsr > 0 else None, funding)
    except Exception as exc:
        logger.debug("_get_contract_stats_cached %s: %s", symbol, exc)
        return (None, None)


# ---------------------------------------------------------------------------
# Liquidation data cache — Gate.io /liq_orders (LIQ_CACHE_TTL per pair)
# ---------------------------------------------------------------------------

_liq_cache: dict[str, tuple[float, float, float]] = {}
# symbol → (cached_at_ts, liq_short_usd, liq_long_usd)
_liq_cache_lock = threading.Lock()


def _get_gate_liq_data(symbol: str) -> tuple[float, float]:
    """Return (liq_short_usd, liq_long_usd) for the last LIQ_VETO_WINDOW seconds.

    Uses Gate.io /futures/usdt/liq_orders (public, no key needed).
    Results cached for LIQ_CACHE_TTL seconds.

    Gate.io convention:
      size > 0 → short position force-closed (short squeeze / покупка для закрытия)
      size < 0 → long  position force-closed (лонг-каскад / продажа для закрытия)

    Returns (0.0, 0.0) on any error — callers will skip the veto gracefully.
    """
    now = time.time()
    with _liq_cache_lock:
        cached = _liq_cache.get(symbol)
        if cached and now - cached[0] < LIQ_CACHE_TTL:
            return (cached[1], cached[2])
    try:
        resp = _gateio_get(
            "/liq_orders",
            params={
                "contract": _to_gate(symbol),
                "from": int(now) - LIQ_VETO_WINDOW,
                "limit": 1000,
            },
            timeout=8,
        )
        orders = resp.json()
        if not isinstance(orders, list):
            return (0.0, 0.0)
        liq_short = 0.0
        liq_long  = 0.0
        for o in orders:
            size  = float(o.get("size") or 0)
            price = float(o.get("fill_price") or o.get("order_price") or 0)
            if price <= 0:
                continue
            usd = abs(size) * price
            if size > 0:
                liq_short += usd   # шорты ликвидированы (сквиз)
            else:
                liq_long  += usd   # лонги ликвидированы (каскад)
        with _liq_cache_lock:
            _liq_cache[symbol] = (now, liq_short, liq_long)
        return (liq_short, liq_long)
    except Exception as exc:
        logger.warning("_get_gate_liq_data %s: %s", symbol, exc)
        return (0.0, 0.0)


def _check_liq_veto(
    symbol: str, side: str, vol_24h_usdt: float
) -> tuple[bool, str]:
    """Return (should_veto, reason) based on recent liquidation data.

    SHORT + крупные ликвидации шортов → шорт-сквиз уже случился → вето
    LONG  + крупные ликвидации лонгов → лонг-каскад активен → вето

    Порог: LIQ_VETO_VOL_FRAC × vol_24h_usdt (относительный, не фиксированный $).
    При ошибке эндпоинта → (False, "") — сигнал проходит как обычно.
    """
    if vol_24h_usdt <= 0:
        return (False, "")
    threshold = vol_24h_usdt * LIQ_VETO_VOL_FRAC
    try:
        liq_short, liq_long = _get_gate_liq_data(symbol)
    except Exception:
        return (False, "")
    if side == "sell" and liq_short >= threshold:
        reason = (
            f"шорт-сквиз: ${liq_short:,.0f} шортов ликвидировано за 15м "
            f"(порог ${threshold:,.0f})"
        )
        return (True, reason)
    if side == "buy" and liq_long >= threshold:
        reason = (
            f"лонг-каскад: ${liq_long:,.0f} лонгов ликвидировано за 15м "
            f"(порог ${threshold:,.0f})"
        )
        return (True, reason)
    return (False, "")


# ---------------------------------------------------------------------------
# Funding + LSR factor: compute score delta and signal text lines
# ---------------------------------------------------------------------------

def _funding_lsr_score_and_text(
    symbol: str,
    side: str,
) -> tuple[int, str, int, int]:
    """Fetch funding rate + LSR, compute score adjustment, return factor text.

    Returns (total_delta, factor_text, funding_pts, lsr_pts).
    factor_text — \\n-разделённые строки для тела сигнала (пусто если нет факторов).
    funding_pts и lsr_pts сохраняются в alerts для /demo разбивки.
    При недоступности API возвращает (0, "", 0, 0) — сигнал работает без фактора.
    """
    lsr, funding = _get_contract_stats_cached(symbol)
    delta       = 0
    lines: list[str] = []
    funding_pts = 0
    lsr_pts     = 0

    # ── Funding rate ─────────────────────────────────────────────────────
    if funding is not None:
        fpct = funding * 100
        if side == "sell":   # SHORT сигнал
            if funding >= FUNDING_SHORT_THR_2:
                funding_pts = FUNDING_SHORT_PTS_2
                lines.append(
                    f"💰 Фандинг: {fpct:+.4f}% (лонги сильно платят) → +{funding_pts} баллов"
                )
            elif funding >= FUNDING_SHORT_THR_1:
                funding_pts = FUNDING_SHORT_PTS_1
                lines.append(
                    f"💰 Фандинг: {fpct:+.4f}% (лонги платят) → +{funding_pts} баллов"
                )
        else:                # LONG сигнал
            if funding >= FUNDING_LONG_THR_BAD:
                funding_pts = -FUNDING_LONG_PTS_PEN
                lines.append(
                    f"💰 Фандинг: {fpct:+.4f}% (лонгов много, риск) → {funding_pts} баллов"
                )
            elif funding <= FUNDING_LONG_THR_NEG:
                funding_pts = FUNDING_LONG_PTS_BON
                lines.append(
                    f"💰 Фандинг: {fpct:+.4f}% (шорты платят) → +{funding_pts} баллов"
                )
        delta += funding_pts

    # ── LSR ─────────────────────────────────────────────────────────────
    if lsr is not None and lsr > 0:
        if side == "sell" and lsr >= LSR_SHORT_THR:
            lsr_pts = LSR_BONUS_PTS
            lines.append(f"👥 L/S: {lsr:.2f} (толпа в лонгах) → +{lsr_pts} баллов")
        elif side == "buy" and lsr <= LSR_LONG_THR:
            lsr_pts = LSR_BONUS_PTS
            lines.append(f"👥 L/S: {lsr:.2f} (толпа в шортах) → +{lsr_pts} баллов")
        delta += lsr_pts

    return (delta, "\n".join(lines), funding_pts, lsr_pts)


def _get_whale_alert_line(symbol: str) -> str:
    """Return a one-line summary of the largest on-chain transfer in last 24h.

    Uses the Whale Alert free API (100 req/day, min $500k transfers).
    Requires WHALE_ALERT_API_KEY env var — if not set, returns ''.

    Exchange inflow  (→ exchange) is bearish — suggests sell pressure.
    Exchange outflow (← exchange) is bullish — suggests accumulation.
    """
    if not _WHALE_ALERT_API_KEY:
        return ""
    coin = symbol.upper().replace("USDT", "").replace("_", "").lower()
    if not coin:
        return ""
    try:
        since = int(time.time()) - 86400
        resp = requests.get(
            "https://api.whale-alert.io/v1/transactions",
            params={
                "api_key": _WHALE_ALERT_API_KEY,
                "min_value": 500_000,
                "start": since,
                "currency": coin,
            },
            timeout=8,
        )
        resp.raise_for_status()
        txs = resp.json().get("transactions", [])
        if not txs:
            return ""
        biggest = max(txs, key=lambda t: t.get("amount_usd", 0))
        amount_usd = biggest.get("amount_usd", 0)
        from_type = biggest.get("from", {}).get("owner_type", "")
        to_type   = biggest.get("to",   {}).get("owner_type", "")
        age_h = (int(time.time()) - biggest.get("timestamp", int(time.time()))) / 3600

        if to_type == "exchange":
            direction = "→ на биржу ⚠️"
        elif from_type == "exchange":
            direction = "← с биржи 🟢"
        else:
            direction = f"({from_type or '?'} → {to_type or '?'})"

        return (
            f"🚨 Крупный перевод: ${amount_usd / 1e6:.1f}M "
            f"{coin.upper()} {direction}  {age_h:.1f}ч назад"
        )
    except Exception as exc:
        logger.debug("_get_whale_alert_line %s: %s", symbol, exc)
        return ""


# ── AI veto for confluence signals ────────────────────────────────────────────

def _ai_veto_confluence(
    symbol: str,
    recommendation: str,
    rsi: float | None,
    pct24: float | None,
    above_ema: bool | None,
) -> tuple[bool, str, str]:
    """Ask Gemini (with Google Search) whether the signal is safe to send.

    Three-layer check in a single Gemini call:
    1. News check — block on critical fundamental events (hack, ban, rug pull,
       delisting, project shutdown). Ordinary price/volume news does NOT block.
       Positive long-term news → ДЕРЖАТЬ (hold recommendation appended to signal).
    2. Technical check — block if the coin is actively pumping during a SHORT
       signal or actively dumping during a LONG signal.

    Returns (allow, note, hold_str):
    • allow=False  → suppress; note = blocking reason.
    • allow=True   → send; note = short confirmation appended to the alert.
    • hold_str     → non-empty when Gemini finds positive long-term news and
                     recommends holding the position for N days. Appended to body.
    Fails OPEN (allow=True, "", "") on Gemini error / timeout.
    """
    if not _GEMINI_BASE_URL or not _GEMINI_API_KEY:
        return True, "", ""

    regime = get_market_regime()
    direction_ru = "ШОРТ" if recommendation == "SHORT" else "ЛОНГ"
    rsi_str = f"{rsi:.1f}" if rsi is not None else "н/д"
    pct_str = f"{pct24:+.1f}%" if pct24 is not None else "н/д"
    ema_str = (
        "выше EMA-200 — восходящий тренд" if above_ema is True
        else "ниже EMA-200 — нисходящий тренд" if above_ema is False
        else "положение неизвестно"
    )
    coin_ticker = symbol[:-4] if symbol.upper().endswith("USDT") else symbol

    prompt = (
        f"Ты — торговый фильтр крипто-бота. Найди свежие новости о монете "
        f"{coin_ticker} ({symbol}) и вынеси вердикт.\n\n"
        f"МОНЕТА: {symbol}\n"
        f"СИГНАЛ БОТА: {direction_ru}\n"
        f"RSI (1ч): {rsi_str}\n"
        f"Изменение за 24ч: {pct_str}\n"
        f"Положение vs EMA-200 (4ч): {ema_str}\n"
        f"Режим рынка (BTC): {regime}\n\n"
        f"Возможные вердикты:\n\n"
        f"1. ПОДТВЕРДИТЬ — новостей нет или они нейтральные, техника поддерживает сигнал.\n"
        f"   ВЕРДИКТ: ПОДТВЕРДИТЬ\n"
        f"   ПРИЧИНА: [1 предложение — объясни ПОЧЕМУ сигнал разумен: что именно указывает на "
        f"{'перекупленность и потенциальный разворот вниз' if recommendation == 'SHORT' else 'перепроданность и потенциальный отскок вверх'}. "
        f"Без двойных отрицаний, без слова 'не противоречит'.]\n\n"
        f"2. ЗАБЛОКИРОВАТЬ — только при критической угрозе: хак/эксплойт, санкции "
        f"регулятора, закрытие проекта, делистинг, скам/rug pull. "
        f"ИЛИ явное противоречие технике (памп при ШОРТ / дамп при ЛОНГ).\n"
        f"   ВЕРДИКТ: ЗАБЛОКИРОВАТЬ\n"
        f"   ПРИЧИНА: [1 предложение, указать источник]\n\n"
        f"3. ДЕРЖАТЬ — если найдена ПОЗИТИВНАЯ новость с долгосрочным влиянием "
        f"(листинг на крупной бирже, крупное партнёрство, апгрейд протокола, "
        f"инвестиции фондов). Сигнал отправляется, но с рекомендацией держать позицию.\n"
        f"   ВЕРДИКТ: ДЕРЖАТЬ\n"
        f"   ПРИЧИНА: [название события, 1 предложение]\n"
        f"   ДНИ: [число от 1 до 14]\n"
        f"   ЦЕЛЬ: [целевая цена в USD или 0 если неизвестна]\n\n"
        f"Ответь СТРОГО по одному из форматов выше. Без лишних слов."
    )
    try:
        resp, source = _call_gemini_with_search(
            prompt, max_tokens=200, temperature=0.1, timeout=25
        )
    except Exception:
        return True, "⚠️ проверка недоступна (ошибка сети)", ""

    if not resp:
        return True, "⚠️ проверка недоступна (нет ответа)", ""

    resp_upper = resp.upper()
    reason = ""
    hold_days = 3
    hold_target = 0.0

    for line in resp.splitlines():
        lu = line.upper()
        if "ПРИЧИНА" in lu and ":" in line:
            reason = line.split(":", 1)[-1].strip()
        elif lu.startswith("ДНИ:") or lu.startswith("ДНИ :"):
            try:
                hold_days = max(1, min(14, int(line.split(":", 1)[-1].strip())))
            except (ValueError, TypeError):
                pass
        elif lu.startswith("ЦЕЛЬ:") or lu.startswith("ЦЕЛЬ :"):
            try:
                hold_target = float(line.split(":", 1)[-1].strip())
            except (ValueError, TypeError):
                pass

    if "ЗАБЛОКИРОВАТЬ" in resp_upper:
        full_reason = reason or "ИИ: обнаружена проблема с монетой"
        if source and source[:30] not in full_reason:
            full_reason = f"{full_reason} [{source[:100]}]"
        return False, full_reason, ""

    if "ДЕРЖАТЬ" in resp_upper:
        event = reason or "позитивная новость"
        hold_str = f"📰 {event} — рекомендуется держать {hold_days} дн."
        if hold_target > 0:
            hold_str += f" / цель ${hold_target:,.4g}"
        if source and source[:30] not in hold_str:
            hold_str += f"\n   🔗 {source[:100]}"
        return True, reason, hold_str

    return True, reason or "критичных новостей нет", ""


# ── Demo (paper trading) helpers ──────────────────────────────────────────────

def _compute_demo_sl_tp(
    direction: str, price: float, atr: float | None,
    max_sl_pct: float | None = None,   # cap SL distance; None or 0 = no cap
) -> tuple[float | None, float | None]:
    """Return (sl_price, tp_price) for a $100 demo position.

    max_sl_pct: if set, SL distance is capped at this % of entry price.
    TP adjusts proportionally to preserve R:R ratio.
    """
    if not price or price <= 0:
        return None, None
    if atr and atr > 0:
        if direction == "LONG":
            sl_dist = 1.5 * atr
            if max_sl_pct and max_sl_pct > 0:
                sl_dist = min(sl_dist, price * max_sl_pct / 100)
            return price - sl_dist, price + sl_dist * 2   # R:R 1:2 maintained
        # SHORT: wider SL with minimum floor to survive crypto noise
        raw_sl   = price + SHORT_ATR_SL_MULT * atr
        floor_sl = price * (1 + SHORT_ATR_SL_MIN_PCT / 100)
        sl       = max(raw_sl, floor_sl)
        if max_sl_pct and max_sl_pct > 0:
            cap_sl = price * (1 + max_sl_pct / 100)
            sl     = min(sl, cap_sl)   # cap: SL no more than max_sl_pct above entry
        sl_dist = sl - price
        tp      = price - sl_dist * SHORT_ATR_TP_MULT_RR   # R:R 1:2
        return sl, tp
    # Fixed fallback (no ATR available)
    if direction == "LONG":
        return price * (1 - HIT_RATE_SL_PCT / 100), price * (1 + HIT_RATE_TP_PCT / 100)
    # SHORT fallback: also respect max_sl_pct cap so wrappers like _compute_pump_fade_sl_tp
    # stay effective even when ATR data isn't available yet.
    sl_dist_fb = SHORT_HOLD_SL_PCT / 100 * price
    if max_sl_pct and max_sl_pct > 0:
        sl_dist_fb = min(sl_dist_fb, max_sl_pct / 100 * price)
    return price + sl_dist_fb, price - sl_dist_fb * SHORT_ATR_TP_MULT_RR


def _compute_confluence_sl_tp(
    direction: str, price: float, atr: float | None
) -> tuple[float | None, float | None]:
    """_compute_demo_sl_tp with confluence-specific SL cap (CONFLUENCE_ATR_SL_MAX_PCT).

    Prevents excessively wide stops on volatile coins (e.g. COOKIEUSDT −16% SL incident).
    R:R is preserved — TP scales to match the capped SL distance.
    """
    cap = CONFLUENCE_ATR_SL_MAX_PCT if CONFLUENCE_ATR_SL_MAX_PCT > 0 else None
    return _compute_demo_sl_tp(direction, price, atr, max_sl_pct=cap)


def _compute_pump_fade_sl_tp(
    price: float, atr: float | None
) -> tuple[float | None, float | None]:
    """_compute_demo_sl_tp for pump_24h_fade SHORT entries with its own SL cap.

    12% cap (vs 8% for confluence) — pump-fade targets already-volatile coins where
    ATR is naturally wider.  Also applies to the no-ATR fallback (normally 15%).
    Temporary value: review when 20+ closed pump_fade trades are available.
    Controlled by PUMP_FADE_SL_MAX_PCT.
    """
    cap = PUMP_FADE_SL_MAX_PCT if PUMP_FADE_SL_MAX_PCT > 0 else None
    return _compute_demo_sl_tp("SHORT", price, atr, max_sl_pct=cap)


def _demo_open_position(
    symbol: str,
    direction: str,
    entry_price: float,
    sl_price: float,
    tp_price: float,
    size_usd: float = 100.0,
    is_shadow: bool = False,
    shadow_reason: str | None = None,
    alert_type: str | None = None,
    score: int | None = None,
    notify_body: str | None = None,
) -> None:
    """Insert a paper-trading position row. score>=TOP_SIGNAL_SCORE marks
    a real position as TOP for the /demo three-way comparison.
    When SHADOW_ONLY_MODE=True, shadow positions are sent to Telegram."""
    is_top = (not is_shadow) and score is not None and score >= TOP_SIGNAL_SCORE
    _pid = os.getpid()
    _ts_recv = int(time.time())
    try:
        with _db_lock:
            conn = _get_db()
            # Skip if the same symbol+direction is already open (real or shadow separately)
            existing = conn.execute(
                "SELECT id, alert_type, ts_open FROM demo_positions "
                "WHERE symbol=? AND direction=? AND status='open' AND is_shadow=?",
                (symbol, direction, 1 if is_shadow else 0),
            ).fetchone()
            if existing:
                logger.warning(
                    "Demo DUPLICATE BLOCKED: %s %s/%s — already open id=%d type=%s "
                    "opened=%d pid=%d ts_recv=%d",
                    symbol, direction, alert_type or "?",
                    existing[0], existing[1] or "?", existing[2] or 0,
                    _pid, _ts_recv,
                )
                return
            cur = conn.execute(
                "INSERT OR IGNORE INTO demo_positions "
                "(ts_open, symbol, direction, entry_price, sl_price, tp_price, "
                " size_usd, status, is_shadow, shadow_reason, alert_type, is_top) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)",
                (_ts_recv, symbol, direction, entry_price,
                 sl_price, tp_price, size_usd,
                 1 if is_shadow else 0, shadow_reason, alert_type,
                 1 if is_top else 0),
            )
            _new_demo_id = cur.lastrowid  # capture before lock releases
            conn.commit()
            if cur.rowcount == 0:
                # INSERT was silently blocked by UNIQUE constraint (shouldn't reach here
                # since we checked above, but belt-and-suspenders)
                logger.warning(
                    "Demo UNIQUE constraint blocked INSERT: %s %s/%s pid=%d ts=%d",
                    symbol, direction, alert_type or "?", _pid, _ts_recv,
                )
                return
        logger.info(
            "Demo %s: %s %s/%s entry=%.6g sl=%.6g tp=%.6g pid=%d ts=%d",
            "shadow" if is_shadow else "pos",
            symbol, direction, alert_type or "?",
            entry_price, sl_price, tp_price, _pid, _ts_recv,
        )
        if is_shadow and notify_body:
            _notif_key = (
                "confluence_cap"
                if (shadow_reason or "").startswith("confluence_cap")
                else (alert_type or "unknown")
            )
            if _shadow_notif_allowed(_notif_key):
                with state_lock:
                    _silenced = state["silenced"]
                if not _silenced:
                    # Embed all needed fields directly in callback_data so the
                    # callback handler works regardless of which bot instance
                    # (dev or deployed) processes it — each has its own SQLite DB
                    # and cannot reliably look up the other's row IDs.
                    # Format: watch:{demo_id}:{symbol}:{direction}:{entry_price}:{alert_type}
                    # Max observed length ≈ 54 bytes; Telegram limit is 64 bytes.
                    _atype_cb = (alert_type or "?").replace(":", "_")  # colon is delimiter
                    _watch_cb  = f"watch:{_new_demo_id}:{symbol}:{direction}:{entry_price:.8g}:{_atype_cb}"
                    _watch_markup = {
                        "inline_keyboard": [[
                            {"text": "🔔 Уведомить о развороте",
                             "callback_data": _watch_cb}
                        ]]
                    }
                    _telegram_send(
                        TELEGRAM_CHAT_ID,
                        f"🔬 SHADOW — не реальная позиция\n{notify_body}",
                        reply_markup=_watch_markup,
                    )
    except Exception as exc:
        logger.warning("_demo_open_position failed for %s: %s", symbol, exc)


def _liquidity_entry_trigger(symbol: str, direction: str, price: float,
                              original_sl: float | None) -> None:
    """Background: run liquidity-entry decision for an oversold signal."""
    try:
        from liquidity import find_liquidity_zones, liquidity_entry_decision
        candles = _gateio_klines(symbol, "15m", 100)
        if len(candles) < 7:
            return
        zones    = find_liquidity_zones(candles)
        decision = liquidity_entry_decision(direction, price, zones)
        if not decision:
            logger.debug("Liquidity %s %s: no qualifying zone", symbol, direction)
            return

        now      = int(time.time())
        etype    = decision["entry_type"]

        if etype == "skip":
            logger.info("Liquidity %s %s: skipped — %s", symbol, direction, decision.get("reason"))
            return

        sl_price    = decision["sl_price"]
        tp_price    = decision["tp_price"]
        zone_price  = decision["zone_price"]
        zone_weight = decision["zone_weight"]
        ts_expires  = now + 4 * 3600

        if etype == "limit":
            lp = decision["limit_price"]
            with _db_lock:
                _get_db().execute(
                    "INSERT INTO liquidity_orders "
                    "(ts_created,ts_expires,symbol,direction,entry_type,"
                    " limit_price,sl_price,tp_price,zone_price,zone_weight,"
                    " original_sl_price,status) "
                    "VALUES (?,?,?,?,'limit',?,?,?,?,?,?,'pending')",
                    (now, ts_expires, symbol, direction, lp,
                     sl_price, tp_price, zone_price, zone_weight, original_sl)
                )
                _get_db().commit()
            exp_h = (ts_expires - now) // 3600
            send_telegram(
                f"<i>Параллельная стратегия — вход и SL/TP рассчитаны от зоны "
                f"ликвидности, не совпадают с сигналом выше. Это нормально.</i>\n\n"
                f"🧲 <b>ЛИМИТКА {symbol} {direction}</b> @<b>${lp:,.6g}</b>\n"
                f"Зона ликвидности: ${zone_price:,.6g}  (вес {zone_weight})\n"
                f"🛑 SL: <b>${sl_price:,.6g}</b>  │  🎯 TP: <b>${tp_price:,.6g}</b>\n"
                f"⏳ Отмена через {exp_h}ч"
            )
            logger.info("Liquidity: limit placed %s %s @%.6g zone=%.6g w=%d",
                        symbol, direction, lp, zone_price, zone_weight)

        elif etype == "market":
            ep = price
            with _db_lock:
                _get_db().execute(
                    "INSERT INTO liquidity_orders "
                    "(ts_created,ts_expires,symbol,direction,entry_type,"
                    " entry_price,sl_price,tp_price,zone_price,zone_weight,"
                    " original_sl_price,status,ts_filled) "
                    "VALUES (?,?,?,?,'market',?,?,?,?,?,?,'filled',?)",
                    (now, ts_expires, symbol, direction, ep,
                     sl_price, tp_price, zone_price, zone_weight, original_sl, now)
                )
                _get_db().commit()
            logger.info("Liquidity: market entry %s %s @%.6g zone=%.6g w=%d",
                        symbol, direction, ep, zone_price, zone_weight)

    except Exception as exc:
        logger.warning("_liquidity_entry_trigger %s: %s", symbol, exc)


def _liquidity_reversal_shadow(symbol: str, direction: str, price: float,
                                original_sl: float | None) -> None:
    """Shadow: same liquidity-zone trigger but only enters after reversal confirmation.

    After finding the qualifying zone (same logic as _liquidity_entry_trigger), checks:
      LONG: last completed 15m candle is bullish (close > open) AND the candle's low
            touched within LIQUIDITY_REVERSAL_ZONE_PCT of the zone price AND volume
            ≥ LIQUIDITY_REVERSAL_VOL_MULT × 10-bar average.
      SHORT: mirror — last candle bearish, high touched zone, volume elevated.

    Opens a shadow demo_position (alert_type='liq_reversal', is_shadow=True) instead of
    a liquidity_order, so it shows up in /demo shadow stats alongside other signals.
    """
    if not LIQUIDITY_REVERSAL_SHADOW:
        return
    try:
        from liquidity import find_liquidity_zones, liquidity_entry_decision
        candles = _gateio_klines(symbol, "15m", 100)
        if len(candles) < 12:
            return

        zones    = find_liquidity_zones(candles)
        decision = liquidity_entry_decision(direction, price, zones)
        if not decision or decision.get("entry_type") == "skip":
            return

        zone_price = decision["zone_price"]

        # --- Reversal confirmation on last COMPLETED 15m candle ---
        completed = candles[:-1]  # drop current live candle
        if len(completed) < 10:
            return
        last_c     = completed[-1]
        last_open  = float(last_c[1])
        last_high  = float(last_c[2])
        last_low   = float(last_c[3])
        last_close = float(last_c[4])
        last_vol   = float(last_c[5])
        avg_vol_10 = sum(float(c[5]) for c in completed[-10:]) / 10.0

        if direction == "LONG":
            # Require bullish reversal candle that touched the zone
            if last_close <= last_open:
                logger.debug("LiqReversal %s LONG: last candle not bullish, skip", symbol)
                return
            touch_pct = abs(last_low - zone_price) / zone_price * 100.0
            if touch_pct > LIQUIDITY_REVERSAL_ZONE_PCT:
                logger.debug(
                    "LiqReversal %s LONG: low %.6g not close enough to zone %.6g (%.2f%%)",
                    symbol, last_low, zone_price, touch_pct,
                )
                return
        else:  # SHORT
            if last_close >= last_open:
                logger.debug("LiqReversal %s SHORT: last candle not bearish, skip", symbol)
                return
            touch_pct = abs(last_high - zone_price) / zone_price * 100.0
            if touch_pct > LIQUIDITY_REVERSAL_ZONE_PCT:
                logger.debug(
                    "LiqReversal %s SHORT: high %.6g not close enough to zone %.6g (%.2f%%)",
                    symbol, last_high, zone_price, touch_pct,
                )
                return

        if avg_vol_10 > 0 and last_vol < avg_vol_10 * LIQUIDITY_REVERSAL_VOL_MULT:
            logger.debug(
                "LiqReversal %s %s: vol %.0f < %.1f× avg %.0f, skip",
                symbol, direction, last_vol, LIQUIDITY_REVERSAL_VOL_MULT, avg_vol_10,
            )
            return

        # All checks passed — open shadow demo position
        sl_price = decision["sl_price"]
        tp_price = decision["tp_price"]
        now_ts   = int(time.time())

        _demo_open_position(
            symbol, direction, price, sl_price, tp_price,
            is_shadow=True,
            alert_type="liq_reversal",
            score=65,
            notify_body=(
                f"💧 Liq Reversal <b>{'LONG 📈' if direction == 'LONG' else 'SHORT 📉'}</b>"
                f" <code>{symbol}</code>\n"
                f"Цена: <b>${price:,.6g}</b>  |  Зона: ${zone_price:,.6g}\n"
                f"🟢 TP: <b>${tp_price:,.6g}</b>  │  🔴 SL: <b>${sl_price:,.6g}</b>"
            ),
        )

        with state_lock:
            state["last_liq_reversal_alerted"][symbol] = float(now_ts)

        logger.info(
            "LiqReversal shadow %s %s: zone=%.6g sl=%.6g tp=%.6g vol_ratio=%.2f touch=%.2f%%",
            symbol, direction, zone_price, sl_price, tp_price,
            last_vol / avg_vol_10 if avg_vol_10 > 0 else 0, touch_pct,
        )

    except Exception as exc:
        logger.warning("_liquidity_reversal_shadow %s: %s", symbol, exc)


# cooldown per watchlist entry so a prolonged reversal doesn't spam
_watchlist_last_trigger: dict[int, int] = {}   # watchlist_id → last_triggered_ts
_watchlist_lock = threading.Lock()
_ew_last_trigger: dict[int, int] = {}          # entry_watch_id → last_confirmed_ts
_ew_lock = threading.Lock()


def check_watchlist() -> None:
    """Every 60 s: check active watchlist entries for reversal confirmation.

    For each entry the same 3 conditions as liq_reversal are checked on the
    last completed 15-m candle:
      1. Candle closed in signal direction (bullish for LONG, bearish for SHORT)
      2. Candle's low (LONG) or high (SHORT) touched entry_price ±0.5%
      3. Volume ≥ LIQUIDITY_REVERSAL_VOL_MULT × 10-bar avg

    Every partial hit is always logged to watchlist_triggers for future WR analysis.
    A full 3/3 hit sends a Telegram notification and marks the entry 'triggered'.
    Entries older than expires_ts are auto-expired.
    """
    now = int(time.time())
    try:
        with _db_lock:
            conn = _get_db()
            # Expire stale entries first
            expired = conn.execute(
                "UPDATE watchlist SET status='expired' "
                "WHERE status='active' AND expires_ts <= ?",
                (now,)
            ).rowcount
            if expired:
                conn.commit()
                logger.info("Watchlist: auto-expired %d entries", expired)
            rows = conn.execute(
                "SELECT id, source_demo_id, symbol, direction, alert_type, entry_price "
                "FROM watchlist WHERE status='active' AND expires_ts > ?",
                (now,)
            ).fetchall()
    except Exception as exc:
        logger.warning("check_watchlist DB fetch: %s", exc)
        return

    for wl_id, demo_id, symbol, direction, alert_type, entry_price in rows:
        try:
            # Per-entry cooldown: don't re-alert within 4 h of the last trigger
            with _watchlist_lock:
                last_trig = _watchlist_last_trigger.get(wl_id, 0)
            if now - last_trig < 4 * 3600:
                continue

            candles = _gateio_klines(symbol, "15m", 15)
            if len(candles) < 12:
                continue

            completed = candles[:-1]        # drop still-forming candle
            last_c   = completed[-1]
            last_open  = float(last_c[1])
            last_high  = float(last_c[2])
            last_low   = float(last_c[3])
            last_close = float(last_c[4])
            last_vol   = float(last_c[5])
            avg_vol_10 = sum(float(c[5]) for c in completed[-10:]) / 10.0

            # Condition 1: candle direction matches signal
            if direction == "LONG":
                cond_candle = last_close > last_open
                touch_val   = last_low
            else:
                cond_candle = last_close < last_open
                touch_val   = last_high

            # Condition 2: candle touched entry zone within 0.5%
            if entry_price and entry_price > 0:
                touch_pct  = abs(touch_val - entry_price) / entry_price * 100.0
                cond_touch = touch_pct <= 0.5
            else:
                cond_touch = False

            # Condition 3: volume elevated
            cond_volume = avg_vol_10 > 0 and last_vol >= avg_vol_10 * LIQUIDITY_REVERSAL_VOL_MULT

            any_hit = cond_candle or cond_touch or cond_volume
            all_hit = cond_candle and cond_touch and cond_volume

            # Always log partial hits for analysis
            if any_hit:
                try:
                    with _db_lock:
                        _c = _get_db()
                        _c.execute(
                            "INSERT INTO watchlist_triggers "
                            "(watchlist_id, source_demo_id, symbol, triggered_ts, "
                            " trigger_price, cond_candle, cond_touch, cond_volume) "
                            "VALUES (?,?,?,?,?,?,?,?)",
                            (wl_id, demo_id, symbol, now, last_close,
                             1 if cond_candle else 0,
                             1 if cond_touch  else 0,
                             1 if cond_volume else 0),
                        )
                        _c.commit()
                except Exception as log_exc:
                    logger.warning("watchlist_triggers insert %s: %s", symbol, log_exc)

            if all_hit:
                # Mark triggered and notify
                try:
                    with _db_lock:
                        _c = _get_db()
                        _c.execute(
                            "UPDATE watchlist SET status='triggered' WHERE id=?",
                            (wl_id,)
                        )
                        _c.commit()
                except Exception as upd_exc:
                    logger.warning("watchlist mark triggered %s: %s", symbol, upd_exc)

                with _watchlist_lock:
                    _watchlist_last_trigger[wl_id] = now

                dir_emoji = "📈" if direction == "LONG" else "📉"
                conds_lines = (
                    f"{'✅' if cond_candle else '❌'} Свеча закрылась в направлении сигнала\n"
                    f"{'✅' if cond_touch  else '❌'} Касание зоны входа ±0.5%\n"
                    f"{'✅' if cond_volume else '❌'} Объём ≥1.5× среднего (10 свечей)"
                )
                body = (
                    f"🔔 <b>Разворот подтверждён</b> {dir_emoji}\n"
                    f"<code>{symbol}</code> · {alert_type}\n"
                    f"💲 Цена: <b>${last_close:,.6g}</b>\n"
                    f"📍 Зона входа: <b>${entry_price:,.6g}</b>\n\n"
                    f"{conds_lines}"
                )
                send_telegram(body)
                logger.info(
                    "Watchlist triggered: %s %s wl_id=%d price=%.6g",
                    symbol, direction, wl_id, last_close,
                )
        except Exception as sym_exc:
            logger.warning("check_watchlist %s wl_id=%d: %s", symbol, wl_id, sym_exc)


def check_entry_watch() -> None:
    """Every 300 s: check entry_watch rows for optimal entry confirmation.

    Three mutually-exclusive outcomes per active row (checked in priority order):
      - sl_hit:     price touched SL → cancel, no further watching.
      - pullback:   price pulled back to or past entry level, candle closed in
                    signal direction, volume elevated → confirm with better price.
      - immediate:  price already running in signal direction (first check only,
                    no pullback) → confirm original entry price immediately.
    All outcomes are logged to entry_watch_log for future win-rate analysis.
    """
    now = int(time.time())
    try:
        with _db_lock:
            conn = _get_db()
            expired = conn.execute(
                "UPDATE entry_watch SET status='expired' "
                "WHERE status='active' AND expires_ts <= ?",
                (now,)
            ).rowcount
            if expired:
                conn.commit()
                logger.info("EntryWatch: auto-expired %d entries", expired)
            rows = conn.execute(
                "SELECT id, source_alert_id, symbol, direction, alert_type, "
                "entry_price, sl_price, first_check_done "
                "FROM entry_watch WHERE status='active' AND expires_ts > ?",
                (now,)
            ).fetchall()
    except Exception as exc:
        logger.warning("check_entry_watch DB fetch: %s", exc)
        return

    for ew_id, alert_id, symbol, direction, alert_type, entry_price, sl_price, first_done in rows:
        try:
            with _ew_lock:
                last_trig = _ew_last_trigger.get(ew_id, 0)
            if now - last_trig < 4 * 3600:
                continue

            candles = _gateio_klines(symbol, "15m", 15)
            if len(candles) < 12:
                continue

            completed  = candles[:-1]       # drop still-forming candle
            last_c     = completed[-1]
            last_open  = float(last_c[1])
            last_high  = float(last_c[2])
            last_low   = float(last_c[3])
            last_close = float(last_c[4])
            last_vol   = float(last_c[5])
            avg_vol_10 = sum(float(c[5]) for c in completed[-10:]) / 10.0

            dir_emoji = "📈" if direction == "LONG" else "📉"

            # ── Priority 1: SL hit ─────────────────────────────────────────
            sl_hit = (
                (direction == "LONG"  and last_low  <= sl_price) or
                (direction == "SHORT" and last_high >= sl_price)
            )
            if sl_hit:
                with _db_lock:
                    _c = _get_db()
                    _c.execute(
                        "UPDATE entry_watch SET status='cancelled_sl' WHERE id=?",
                        (ew_id,)
                    )
                    _c.execute(
                        "INSERT INTO entry_watch_log "
                        "(entry_watch_id, source_alert_id, symbol, checked_ts, "
                        " trigger_price, outcome, confirmed_price, "
                        " cond_direction, cond_volume, cond_no_pullback) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (ew_id, alert_id, symbol, now, last_close,
                         "sl_hit", None, 0, 0, 0),
                    )
                    _c.commit()
                send_telegram(
                    f"🚫 <b>Точка входа отменена</b> — цена достигла SL {dir_emoji}\n"
                    f"<code>{symbol}</code> {direction} · {alert_type}\n"
                    f"🛑 SL: <b>${sl_price:,.6g}</b>  │  Цена: <b>${last_close:,.6g}</b>"
                )
                logger.info(
                    "EntryWatch SL-cancelled: %s %s ew_id=%d sl=%.6g price=%.6g",
                    symbol, direction, ew_id, sl_price, last_close,
                )
                continue

            # ── Priority 2: pullback confirm ───────────────────────────────
            # Candle low (LONG) or high (SHORT) touched or exceeded entry_price,
            # meaning a pullback brought price back to entry zone or better.
            # Then the candle closed in the signal direction with elevated volume
            # → signal confirmed with an improved entry price.
            if direction == "LONG":
                cond_touch   = last_low  <= entry_price
                better_price = last_low
            else:
                cond_touch   = last_high >= entry_price
                better_price = last_high

            cond_candle = (
                (direction == "LONG"  and last_close > last_open) or
                (direction == "SHORT" and last_close < last_open)
            )
            cond_volume = avg_vol_10 > 0 and last_vol >= avg_vol_10 * LIQUIDITY_REVERSAL_VOL_MULT

            # Always log any partial hit for analysis
            any_hit = cond_touch or cond_candle or cond_volume
            if any_hit:
                try:
                    with _db_lock:
                        _c = _get_db()
                        _c.execute(
                            "INSERT INTO entry_watch_log "
                            "(entry_watch_id, source_alert_id, symbol, checked_ts, "
                            " trigger_price, outcome, confirmed_price, "
                            " cond_direction, cond_volume, cond_no_pullback) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (ew_id, alert_id, symbol, now, last_close,
                             "partial", None,
                             1 if cond_candle else 0,
                             1 if cond_volume else 0,
                             0),
                        )
                        _c.commit()
                except Exception as log_exc:
                    logger.warning("entry_watch_log partial %s: %s", symbol, log_exc)

            if cond_touch and cond_candle and cond_volume:
                with _db_lock:
                    _c = _get_db()
                    _c.execute(
                        "UPDATE entry_watch SET status='confirmed_pullback' WHERE id=?",
                        (ew_id,)
                    )
                    _c.execute(
                        "INSERT INTO entry_watch_log "
                        "(entry_watch_id, source_alert_id, symbol, checked_ts, "
                        " trigger_price, outcome, confirmed_price, "
                        " cond_direction, cond_volume, cond_no_pullback) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (ew_id, alert_id, symbol, now, last_close,
                         "pullback", better_price, 1, 1, 0),
                    )
                    _c.commit()
                with _ew_lock:
                    _ew_last_trigger[ew_id] = now
                improvement = abs(better_price - entry_price) / entry_price * 100
                send_telegram(
                    f"🎯 <b>Лучшая точка входа найдена</b> {dir_emoji}\n"
                    f"<code>{symbol}</code> · {alert_type}\n"
                    f"📍 Улучшенная цена: <b>${better_price:,.6g}</b> "
                    f"({'−' if direction == 'LONG' else '+'}{improvement:.2f}% к исходной)\n"
                    f"📌 Исходная цена: ${entry_price:,.6g}\n"
                    f"✅ Свеча закрылась в направлении сигнала\n"
                    f"✅ Объём: ×{last_vol / avg_vol_10:.1f} от среднего"
                )
                logger.info(
                    "EntryWatch pullback confirmed: %s %s ew_id=%d "
                    "entry=%.6g better=%.6g vol_ratio=%.1f",
                    symbol, direction, ew_id, entry_price, better_price,
                    last_vol / avg_vol_10 if avg_vol_10 > 0 else 0,
                )
                continue

            # ── Priority 3: immediate confirm (first check only) ───────────
            # Price is already running in signal direction with no pullback yet.
            # Notify to enter at the original price — don't wait indefinitely.
            if not first_done:
                cond_running = (
                    (direction == "LONG"  and last_close > entry_price) or
                    (direction == "SHORT" and last_close < entry_price)
                )
                if cond_running:
                    with _db_lock:
                        _c = _get_db()
                        _c.execute(
                            "UPDATE entry_watch SET status='confirmed_immediate' WHERE id=?",
                            (ew_id,)
                        )
                        _c.execute(
                            "INSERT INTO entry_watch_log "
                            "(entry_watch_id, source_alert_id, symbol, checked_ts, "
                            " trigger_price, outcome, confirmed_price, "
                            " cond_direction, cond_volume, cond_no_pullback) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (ew_id, alert_id, symbol, now, last_close,
                             "immediate", entry_price, 1, 0, 1),
                        )
                        _c.commit()
                    with _ew_lock:
                        _ew_last_trigger[ew_id] = now
                    send_telegram(
                        f"🎯 <b>Цена уже идёт по сигналу</b> {dir_emoji}\n"
                        f"<code>{symbol}</code> · {alert_type}\n"
                        f"💲 Входи по исходной цене: <b>${entry_price:,.6g}</b>\n"
                        f"📊 Текущая: ${last_close:,.6g}"
                    )
                    logger.info(
                        "EntryWatch immediate confirmed: %s %s ew_id=%d "
                        "entry=%.6g current=%.6g",
                        symbol, direction, ew_id, entry_price, last_close,
                    )
                else:
                    # Price hasn't moved yet — mark first check done, keep watching
                    with _db_lock:
                        _c = _get_db()
                        _c.execute(
                            "UPDATE entry_watch SET first_check_done=1 WHERE id=?",
                            (ew_id,)
                        )
                        _c.commit()

        except Exception as sym_exc:
            logger.warning("check_entry_watch %s ew_id=%d: %s", symbol, ew_id, sym_exc)


def check_liquidity_orders() -> None:
    """
    Background job (every 60 s): manage pending and filled liquidity orders.

    Pending limits: fill / expire / mark missed_move.
    Filled orders:  close on TP/SL; track if smart SL saved a position.
    """
    try:
        now = int(time.time())
        with _db_lock:
            conn = _get_db()
            pending = conn.execute(
                "SELECT id,symbol,direction,limit_price,sl_price,tp_price,"
                "       ts_expires,zone_price,zone_weight "
                "FROM liquidity_orders WHERE status='pending'"
            ).fetchall()
            filled = conn.execute(
                "SELECT id,symbol,direction,entry_price,sl_price,tp_price,original_sl_price "
                "FROM liquidity_orders WHERE status='filled' AND result IS NULL"
            ).fetchall()

        # ── Pending limit orders ─────────────────────────────────────────────
        for oid, sym, dr, lp, sl, tp, ts_exp, zp, zw in pending:
            ticker = _gateio_ticker(sym)
            if not ticker:
                continue
            cur = float(ticker["lastPrice"])

            # Expired?
            if now >= ts_exp:
                with _db_lock:
                    _get_db().execute(
                        "UPDATE liquidity_orders SET status='expired',ts_closed=? WHERE id=?",
                        (now, oid))
                    _get_db().commit()
                send_telegram(
                    f"⏰ <b>Лимитка истекла</b>: {sym} {dr} @${lp:,.6g} — не исполнена за 4ч"
                )
                logger.info("Liquidity: expired %s %s id=%d", sym, dr, oid)
                continue

            # Missed move: price moved 1.5% toward TP without filling
            if lp:
                toward_tp_pct = ((cur - lp) / lp * 100.0 if dr == "LONG"
                                 else (lp - cur) / lp * 100.0)
                if toward_tp_pct >= 1.5:
                    with _db_lock:
                        _get_db().execute(
                            "UPDATE liquidity_orders SET status='missed_move',ts_closed=? WHERE id=?",
                            (now, oid))
                        _get_db().commit()
                    send_telegram(
                        f"🏃 <b>Движение без входа</b>: {sym} {dr}\n"
                        f"Цена ушла к TP, лимитка @${lp:,.6g} не исполнена"
                    )
                    logger.info("Liquidity: missed_move %s %s id=%d", sym, dr, oid)
                    continue

            # Filled? Check current price AND last completed 1m candle extremes.
            # Wick fills require the candle to penetrate the limit level by at
            # least 0.05% — a bare touch is tracked as "touched_no_fill" only.
            _WICK_MIN_PCT = 0.0005   # 0.05%
            filled_cond = (cur <= lp if dr == "LONG" else cur >= lp) if lp else False
            wick_filled  = False
            wick_touched = False     # candle touched but did not penetrate 0.05%
            fill_price   = lp        # limit orders always fill at the limit price, not market price
            if not filled_cond and lp:
                try:
                    _c1m = _gateio_klines(sym, "1m", 2)
                    if len(_c1m) >= 2:
                        # Use the last *completed* candle (index -2); -1 is still forming
                        last_closed = _c1m[-2]
                        c_low  = float(last_closed[3])
                        c_high = float(last_closed[2])
                        if dr == "LONG":
                            # Candle must close at least 0.05% below the limit price
                            if c_low <= lp * (1.0 - _WICK_MIN_PCT):
                                wick_filled = True
                            elif c_low <= lp:
                                wick_touched = True   # bare touch — no fill
                        else:  # SHORT
                            if c_high >= lp * (1.0 + _WICK_MIN_PCT):
                                wick_filled = True
                            elif c_high >= lp:
                                wick_touched = True
                except Exception as _wf_exc:
                    logger.debug("Liquidity wick-fill check %s: %s", sym, _wf_exc)

            if wick_touched and not wick_filled:
                # Record bare touch without fill
                with _db_lock:
                    _get_db().execute(
                        "UPDATE liquidity_orders SET touched_no_fill=touched_no_fill+1 WHERE id=?",
                        (oid,))
                    _get_db().commit()
                logger.info("Liquidity: touched_no_fill %s %s limit=%.6g id=%d",
                            sym, dr, lp, oid)

            if wick_filled:
                fill_price = lp   # simulate fill at the exact limit level, not the wick tip
            if filled_cond or wick_filled:
                wick_tag = " (прокол свечи)" if wick_filled else ""
                with _db_lock:
                    _get_db().execute(
                        "UPDATE liquidity_orders SET status='filled',entry_price=?,ts_filled=? WHERE id=?",
                        (fill_price, now, oid))
                    _get_db().commit()
                send_telegram(
                    f"✅ <b>Лимитка исполнена{wick_tag}</b>: {sym} {dr} "
                    f"@<b>${fill_price:,.6g}</b>\n"
                    f"🛑 SL: ${sl:,.6g}  │  🎯 TP: ${tp:,.6g}"
                )
                logger.info("Liquidity: filled%s %s %s @%.6g id=%d",
                            " (wick)" if wick_filled else "", sym, dr, fill_price, oid)

        # ── Filled orders: check SL/TP ───────────────────────────────────────
        for oid, sym, dr, ep, sl, tp, orig_sl in filled:
            if not ep:
                continue
            ticker = _gateio_ticker(sym)
            if not ticker:
                continue
            cur = float(ticker["lastPrice"])

            # Check if price touched original SL (for "saved" tracking)
            touched = 0
            if orig_sl:
                touched = (1 if (dr == "LONG" and cur < orig_sl)
                           else (1 if (dr == "SHORT" and cur > orig_sl) else 0))
            if touched:
                with _db_lock:
                    _get_db().execute(
                        "UPDATE liquidity_orders SET touched_orig_sl=1 WHERE id=? AND touched_orig_sl=0",
                        (oid,))
                    _get_db().commit()

            hit_tp = (cur >= tp if dr == "LONG" else cur <= tp)
            hit_sl = (cur <= sl if dr == "LONG" else cur >= sl)
            # Also check last completed 1m candle for wick hits on SL/TP
            _exit_by_wick = False
            if not hit_tp and not hit_sl:
                try:
                    _c1m_f = _gateio_klines(sym, "1m", 2)
                    if len(_c1m_f) >= 2:
                        _lc = _c1m_f[-2]
                        _c_low  = float(_lc[3])
                        _c_high = float(_lc[2])
                        wick_tp = (_c_high >= tp if dr == "LONG" else _c_low <= tp)
                        wick_sl = (_c_low  <= sl if dr == "LONG" else _c_high >= sl)
                        if wick_tp or wick_sl:
                            hit_tp = wick_tp
                            hit_sl = wick_sl and not wick_tp   # TP wins if both wick
                            _exit_by_wick = True
                except Exception as _we:
                    logger.debug("Liquidity wick SL/TP check %s: %s", sym, _we)
            if not hit_tp and not hit_sl:
                continue

            # For wick exits, use the level price (SL or TP) as exit, not current price
            if _exit_by_wick:
                exit_price = tp if hit_tp else sl
            else:
                exit_price = cur
            result  = "tp" if hit_tp else "sl"
            pnl_usd = (100.0 * (exit_price - ep) / ep if dr == "LONG"
                       else 100.0 * (ep - exit_price) / ep)
            # "saved" = price crossed original SL but smart SL held, closed TP
            saved = 0
            if result == "tp":
                with _db_lock:
                    row = _get_db().execute(
                        "SELECT touched_orig_sl FROM liquidity_orders WHERE id=?", (oid,)
                    ).fetchone()
                    if row and row[0]:
                        saved = 1

            with _db_lock:
                _get_db().execute(
                    "UPDATE liquidity_orders "
                    "SET result=?,ts_closed=?,exit_price=?,pnl_usd=?,saved_from_sl=? "
                    "WHERE id=?",
                    (result, now, exit_price, pnl_usd, saved, oid))
                _get_db().commit()

            sign = "+" if pnl_usd >= 0 else ""
            r_icon = "✅ TP" if result == "tp" else "❌ SL"
            saved_note = "  💾 Умный SL спас сделку!" if saved else ""
            wick_note  = " (wick)" if _exit_by_wick else ""
            send_telegram(
                f"{r_icon}{wick_note} <b>Ликвидность {sym} {dr}</b>  "
                f"${ep:,.6g} → ${exit_price:,.6g}  <b>{sign}${pnl_usd:.2f}</b>{saved_note}"
            )
            logger.info("Liquidity: closed%s %s %s result=%s exit=%.6g pnl=%.2f id=%d",
                        " (wick)" if _exit_by_wick else "",
                        sym, dr, result, exit_price, pnl_usd, oid)

    except Exception as exc:
        logger.warning("check_liquidity_orders failed: %s", exc)


def check_demo_positions() -> None:
    """Close demo positions that reached TP or SL. Called every 60 s.

    Exit detection (since WICK_CHECK_ENABLED_TS):
      1. lastPrice crosses SL/TP  → exit at lastPrice  (wick_close=0)
      2. last completed 1m candle low/high crosses SL/TP → exit at the level  (wick_close=1)
    Exit price is always the level (SL or TP), never the wick extreme, for honest PnL.
    """
    try:
        with _db_lock:
            conn = _get_db()
            rows = conn.execute(
                "SELECT id, symbol, direction, entry_price, sl_price, tp_price "
                "FROM demo_positions WHERE status='open'"
            ).fetchall()
    except Exception as exc:
        logger.warning("check_demo_positions DB read: %s", exc)
        return

    if not rows:
        return

    # Batch: fetch lastPrice AND last completed 1m candle for each unique symbol
    symbols = list({r[1] for r in rows})
    prices: dict[str, float] = {}
    candles_1m: dict[str, tuple[float, float]] = {}   # symbol → (c_low, c_high)
    for sym in symbols:
        try:
            t = _gateio_ticker(sym)
            if t:
                prices[sym] = float(t["lastPrice"])
        except Exception:
            pass
        try:
            _c = _gateio_klines(sym, "1m", 2)
            if len(_c) >= 2:
                _lc = _c[-2]   # last *completed* candle; index -1 is still forming
                candles_1m[sym] = (float(_lc[3]), float(_lc[2]))   # (low, high)
        except Exception:
            pass

    now_ts = int(time.time())
    for row_id, symbol, direction, entry, sl, tp in rows:
        price = prices.get(symbol)
        if price is None:
            continue

        # ── 1. Price check (unchanged behaviour) ────────────────────────────
        hit_tp = (direction == "LONG" and price >= tp) or (direction == "SHORT" and price <= tp)
        hit_sl = (direction == "LONG" and price <= sl) or (direction == "SHORT" and price >= sl)
        wick_close = 0
        exit_price = price   # default: last traded price

        # ── 2. Wick check (new: catches wicks between polling ticks) ────────
        if not hit_tp and not hit_sl:
            lc = candles_1m.get(symbol)
            if lc:
                c_low, c_high = lc
                wick_tp = (c_high >= tp if direction == "LONG" else c_low <= tp)
                wick_sl = (c_low  <= sl if direction == "LONG" else c_high >= sl)
                if wick_tp or wick_sl:
                    hit_tp     = wick_tp
                    hit_sl     = wick_sl and not wick_tp   # TP wins if both
                    wick_close = 1
                    exit_price = tp if hit_tp else sl       # level, not wick extreme

        if not hit_tp and not hit_sl:
            continue

        status = "tp" if hit_tp else "sl"
        pnl_usd = (
            100.0 * (exit_price - entry) / entry if direction == "LONG"
            else 100.0 * (entry - exit_price) / entry
        )
        try:
            with _db_lock:
                conn = _get_db()
                conn.execute(
                    "UPDATE demo_positions "
                    "SET status=?, ts_close=?, exit_price=?, pnl_usd=?, wick_close=? WHERE id=?",
                    (status, now_ts, exit_price, pnl_usd, wick_close, row_id),
                )
                conn.commit()
            logger.info(
                "Demo pos %d %s %s: %s exit=%.6g pnl=$%.2f%s",
                row_id, symbol, direction, status.upper(), exit_price, pnl_usd,
                " (wick)" if wick_close else "",
            )
        except Exception as exc:
            logger.warning("check_demo_positions update %d: %s", row_id, exc)


def handle_demo_command(chat_id: int) -> None:
    """Show paper-trading stats broken down by signal type, with TP/SL/PnL per position."""
    TYPE_LABELS: dict[str, str] = {
        "confluence":     "🚨 Конфлюэнция",
        "oversold_24h":   "🟢 Перепроданность",
        "overheated_24h": "🔥 Перегрев",
        "streak_1h":      "⚡ Серия 1ч",
    }

    try:
        with _db_lock:
            conn = _get_db()
            # Per-type stats: real signals only
            type_stats = conn.execute(
                "SELECT alert_type, "
                "  SUM(CASE WHEN status='open'  THEN 1 ELSE 0 END) as open_cnt, "
                "  SUM(CASE WHEN status!='open' THEN 1 ELSE 0 END) as closed_cnt, "
                "  COALESCE(SUM(CASE WHEN status='tp' THEN 1 ELSE 0 END),0) as tp_cnt, "
                "  COALESCE(SUM(CASE WHEN status='sl' THEN 1 ELSE 0 END),0) as sl_cnt, "
                "  COALESCE(SUM(CASE WHEN status!='open' THEN pnl_usd ELSE 0 END),0) as closed_pnl, "
                "  COALESCE(AVG(CASE WHEN status!='open' THEN pnl_usd END),0) as avg_pnl "
                "FROM demo_positions WHERE is_shadow=0 "
                "GROUP BY alert_type ORDER BY closed_pnl DESC"
            ).fetchall()

            # Top-5 open positions per type (newest first)
            open_by_type: dict[str, list] = {}
            for row in type_stats:
                at = row[0]
                open_by_type[at] = conn.execute(
                    "SELECT symbol, direction, entry_price, sl_price, tp_price, ts_open "
                    "FROM demo_positions "
                    "WHERE status='open' AND is_shadow=0 AND alert_type=? "
                    "ORDER BY ts_open DESC LIMIT 5",
                    (at,)
                ).fetchall()

            # Last-3 closed positions per type (most recent first)
            closed_by_type: dict[str, list] = {}
            for row in type_stats:
                at = row[0]
                closed_by_type[at] = conn.execute(
                    "SELECT symbol, direction, entry_price, exit_price, pnl_usd, status, ts_open, ts_close "
                    "FROM demo_positions "
                    "WHERE status!='open' AND is_shadow=0 AND alert_type=? "
                    "ORDER BY ts_close DESC LIMIT 3",
                    (at,)
                ).fetchall()

            # Shadow totals (all types combined)
            sc_open_rows = conn.execute(
                "SELECT symbol, direction, entry_price, alert_type FROM demo_positions "
                "WHERE status='open' AND is_shadow=1"
            ).fetchall()
            sc_open = len(sc_open_rows)
            sc_total, sc_tp, sc_sl, sc_pnl = conn.execute(
                "SELECT COUNT(*), "
                "COALESCE(SUM(CASE WHEN status='tp' THEN 1 ELSE 0 END),0), "
                "COALESCE(SUM(CASE WHEN status='sl' THEN 1 ELSE 0 END),0), "
                "COALESCE(SUM(pnl_usd),0) "
                "FROM demo_positions WHERE status!='open' AND is_shadow=1"
            ).fetchone()
            # Shadow breakdown by direction (LONG / SHORT)
            sc_dir_stats = conn.execute(
                "SELECT direction, "
                "  SUM(CASE WHEN status='open' THEN 1 ELSE 0 END), "
                "  SUM(CASE WHEN status!='open' THEN 1 ELSE 0 END), "
                "  COALESCE(SUM(CASE WHEN status!='open' THEN pnl_usd ELSE 0 END), 0) "
                "FROM demo_positions WHERE is_shadow=1 GROUP BY direction"
            ).fetchall()
            # Shadow breakdown by signal type
            sc_type_stats = conn.execute(
                "SELECT alert_type, "
                "  SUM(CASE WHEN status='open' THEN 1 ELSE 0 END), "
                "  SUM(CASE WHEN status!='open' THEN 1 ELSE 0 END), "
                "  COALESCE(SUM(CASE WHEN status!='open' THEN pnl_usd ELSE 0 END), 0) "
                "FROM demo_positions WHERE is_shadow=1 GROUP BY alert_type"
            ).fetchall()
            # Shadow breakdown by block reason (grouped by prefix before ':')
            sc_reason_stats = conn.execute("""
                SELECT
                  CASE
                    WHEN shadow_reason LIKE 'bear_downtrend%' THEN 'bear_downtrend'
                    WHEN shadow_reason LIKE '%:%'
                         THEN TRIM(SUBSTR(shadow_reason, 1, INSTR(shadow_reason, ':') - 1))
                    ELSE COALESCE(shadow_reason, '(нет причины)')
                  END AS reason_prefix,
                  SUM(CASE WHEN status='open'  THEN 1 ELSE 0 END)                                AS open_n,
                  SUM(CASE WHEN status!='open' THEN 1 ELSE 0 END)                                AS closed_n,
                  SUM(CASE WHEN status='tp'    THEN 1 ELSE 0 END)                                 AS tp_n,
                  SUM(CASE WHEN status='sl'    THEN 1 ELSE 0 END)                                 AS sl_n,
                  COALESCE(SUM(CASE WHEN status!='open' THEN pnl_usd ELSE 0 END), 0)             AS total_pnl,
                  COALESCE(AVG(CASE WHEN status!='open' THEN pnl_usd END), 0)                    AS avg_pnl
                FROM demo_positions
                WHERE is_shadow=1
                  AND shadow_reason IS NOT NULL AND shadow_reason != ''
                GROUP BY reason_prefix
                ORDER BY total_pnl DESC
            """).fetchall()

            # Liquidity orders summary
            liq_row = conn.execute("""
                SELECT
                  SUM(CASE WHEN entry_type='limit' THEN 1 ELSE 0 END)                       AS lp,
                  SUM(CASE WHEN status='filled' AND entry_type='limit' THEN 1 ELSE 0 END)    AS lf,
                  SUM(CASE WHEN entry_type='market' THEN 1 ELSE 0 END)                       AS mkt,
                  SUM(CASE WHEN status='expired'    THEN 1 ELSE 0 END)                       AS exp,
                  SUM(CASE WHEN status='missed_move' THEN 1 ELSE 0 END)                      AS miss,
                  SUM(CASE WHEN result IS NULL AND status='filled' THEN 1 ELSE 0 END)        AS open_n,
                  SUM(CASE WHEN result IS NOT NULL THEN 1 ELSE 0 END)                        AS ct,
                  SUM(CASE WHEN result='tp'         THEN 1 ELSE 0 END)                       AS ctp,
                  SUM(CASE WHEN result='sl'         THEN 1 ELSE 0 END)                       AS csl,
                  COALESCE(SUM(CASE WHEN result IS NOT NULL THEN pnl_usd ELSE 0 END), 0)     AS cpnl,
                  COALESCE(SUM(touched_no_fill), 0)                                           AS tnf
                FROM liquidity_orders
            """).fetchone()
            liq_open_rows = conn.execute(
                "SELECT symbol, direction, entry_price, sl_price, tp_price, ts_filled "
                "FROM liquidity_orders WHERE status='filled' AND result IS NULL "
                "ORDER BY ts_filled DESC LIMIT 5"
            ).fetchall()
            liq_closed_rows = conn.execute(
                "SELECT symbol, direction, entry_price, exit_price, pnl_usd, result, ts_filled, ts_closed "
                "FROM liquidity_orders WHERE result IS NOT NULL "
                "ORDER BY ts_closed DESC LIMIT 3"
            ).fetchall()
    except Exception as exc:
        _telegram_send(chat_id, f"❌ Ошибка БД: {exc}")
        return

    # Fetch current prices for all displayed open positions (real + shadow + liquidity)
    all_syms: set[str] = set()
    for rows in open_by_type.values():
        for sym, *_ in rows:
            all_syms.add(sym)
    for sym, *_ in sc_open_rows:
        all_syms.add(sym)
    for sym, *_ in liq_open_rows:
        all_syms.add(sym)
    open_prices: dict[str, float] = {}
    for sym in all_syms:
        try:
            t = _gateio_ticker(sym)
            if t:
                open_prices[sym] = float(t["lastPrice"])
        except Exception:
            pass

    now_ts = int(time.time())

    def _fmt_open(sym: str, dr: str, entry: float, sl: float, tp: float, ts: int) -> list[str]:
        """Open position: нереализованный PnL в % и $."""
        age_h = (now_ts - ts) / 3600
        icon = "📈" if dr == "LONG" else "📉"
        cur = open_prices.get(sym)
        out: list[str] = []
        if cur and entry:
            upnl_pct = (cur - entry) / entry * 100.0 if dr == "LONG" else (entry - cur) / entry * 100.0
            upnl_usd = upnl_pct  # $100/сделка → 1% = $1
            pnl_icon = "🟢" if upnl_pct >= 0 else "🔴"
            s = "+" if upnl_pct >= 0 else ""
            out.append(
                f"  {icon} <code>{sym}</code> {dr}  "
                f"вход <b>{entry:,.5g}</b> → <b>{cur:,.5g}</b>  "
                f"{pnl_icon} <b>{s}{upnl_pct:.1f}%</b> (<b>{s}${upnl_usd:.2f}</b>)  ⏱{age_h:.1f}ч"
            )
        else:
            out.append(f"  {icon} <code>{sym}</code> {dr}  вход <b>{entry:,.5g}</b>  ⏱{age_h:.1f}ч")
        if tp and sl:
            out.append(f"      🎯 TP: <b>${tp:,.5g}</b>  │  🛑 SL: <b>${sl:,.5g}</b>")
        return out

    def _fmt_closed(sym: str, dr: str, entry: float, exit_p: float,
                    pnl_usd: float, status: str, ts_open: int, ts_close: int) -> list[str]:
        """Closed position: реализованный PnL в $ и статус TP/SL."""
        icon = "📈" if dr == "LONG" else "📉"
        result_icon = "✅" if status == "tp" else "❌"
        pnl_icon = "🟢" if (pnl_usd or 0) >= 0 else "🔴"
        s = "+" if (pnl_usd or 0) >= 0 else ""
        dur_h = (ts_close - ts_open) / 3600 if ts_close and ts_open else 0
        entry_s = f"{entry:,.5g}" if entry else "—"
        exit_s  = f"{exit_p:,.5g}" if exit_p else "—"
        pnl_s   = f"{s}${pnl_usd:.2f}" if pnl_usd is not None else "—"
        tag = "TP" if status == "tp" else ("SL" if status == "sl" else status.upper())
        return [
            f"  {result_icon} <code>{sym}</code> {dr}  "
            f"{entry_s} → {exit_s}  "
            f"{pnl_icon} <b>{pnl_s}</b>  [{tag}]  ⏱{dur_h:.0f}ч"
        ]

    lines: list[str] = ["📊 <b>ДЕМО-РЕЖИМ (paper trading, $100/сделка)</b>"]

    for alert_type, open_cnt, closed_cnt, tp_cnt, sl_cnt, closed_pnl, avg_pnl in type_stats:
        label = TYPE_LABELS.get(alert_type, alert_type)
        wr = tp_cnt / closed_cnt * 100 if closed_cnt else 0

        # Unrealized PnL across all displayed open rows
        disp_open = open_by_type.get(alert_type, [])
        unrealized_usd = sum(
            100.0 * ((open_prices[sym] - entry) / entry if dr == "LONG" else (entry - open_prices[sym]) / entry)
            for sym, dr, entry, *_ in disp_open
            if sym in open_prices and entry
        )

        # Profitability marker
        if closed_cnt >= 3:
            prof = "✅" if closed_pnl > 0 else "❌"
        else:
            prof = "🔄"

        lines += ["", "━━━━━━━━━━━━━━━━━━━━", f"{prof} <b>{label}</b>"]
        lines.append(f"  Открытых: <b>{open_cnt}</b>  │  Закрытых: <b>{closed_cnt}</b>")
        if closed_cnt > 0:
            c_sign = "+" if closed_pnl >= 0 else ""
            a_sign = "+" if avg_pnl >= 0 else ""
            lines.append(f"  Win-rate: <b>{wr:.0f}%</b>  (TP: {tp_cnt} / SL: {sl_cnt})")
            lines.append(
                f"  PnL закрытых: <b>{c_sign}${closed_pnl:.2f}</b>  ·  avg: <b>{a_sign}${avg_pnl:.2f}</b>"
            )

        # --- Открытые позиции (топ-5) ---
        if disp_open:
            u_sign = "+" if unrealized_usd >= 0 else ""
            suffix = f"топ-5 из {open_cnt}" if open_cnt > 5 else f"{open_cnt} поз."
            lines.append(
                f"\n  📂 <b>Открытые</b> ({suffix})  нереализованный: <b>{u_sign}${unrealized_usd:.2f}</b>"
            )
            for sym, dr, entry, sl, tp, ts in disp_open:
                lines += _fmt_open(sym, dr, entry, sl, tp, ts)

        # --- Последние 3 закрытые позиции ---
        disp_closed = closed_by_type.get(alert_type, [])
        if disp_closed:
            lines.append(f"\n  📁 <b>Последние закрытые</b> (из {closed_cnt})")
            for sym, dr, entry, exit_p, pnl_usd, status, ts_open, ts_close in disp_closed:
                lines += _fmt_closed(sym, dr, entry, exit_p, pnl_usd, status, ts_open, ts_close)

    # Shadow summary — compute unrealized per direction and per type
    sc_wr = sc_tp / sc_total * 100 if sc_total else 0
    sc_unrealized = 0.0
    sc_unreal_dir: dict[str, float] = {}
    sc_unreal_type: dict[str, float] = {}
    for sym, dr, entry, at in sc_open_rows:
        cur = open_prices.get(sym)
        if cur and entry:
            pct = (cur - entry) / entry * 100.0 if dr == "LONG" else (entry - cur) / entry * 100.0
            sc_unrealized += pct
            sc_unreal_dir[dr]  = sc_unreal_dir.get(dr, 0.0)  + pct
            sc_unreal_type[at] = sc_unreal_type.get(at, 0.0) + pct

    lines += [
        "", "━━━━━━━━━━━━━━━━━━━━",
        "👻 <b>Теневые (заблокированы)</b>",
        f"  Открытых: <b>{sc_open}</b>  │  Закрытых: <b>{sc_total}</b>",
    ]
    if sc_total:
        s_sign = "+" if sc_pnl >= 0 else ""
        lines += [
            f"  Win-rate: <b>{sc_wr:.0f}%</b>  (TP: {sc_tp} / SL: {sc_sl})",
            f"  PnL если бы открыли: <b>{s_sign}${sc_pnl:.2f}</b>",
        ]
    if sc_open:
        u_sign = "+" if sc_unrealized >= 0 else ""
        lines.append(f"  Нереализованный: <b>{u_sign}${sc_unrealized:.2f}</b> ({sc_open} поз.)")

    # --- Разбивка по LONG / SHORT ---
    if sc_dir_stats:
        lines.append("")
        for dr, d_open, d_closed, d_pnl in sc_dir_stats:
            d_unreal = sc_unreal_dir.get(dr, 0.0)
            icon  = "📈" if dr == "LONG" else "📉"
            ps    = "+" if d_pnl    >= 0 else ""
            us    = "+" if d_unreal >= 0 else ""
            lines.append(
                f"  {icon} <b>{dr}</b>:  "
                f"Открыто <b>{d_open}</b>  │  Закрыто <b>{d_closed}</b>  │  "
                f"Прибыль <b>{ps}${d_pnl:.2f}</b>  │  "
                f"Нереал. <b>{us}${d_unreal:.2f}</b>"
            )

    # --- Разбивка по типу сигнала ---
    if sc_type_stats:
        lines.append("")
        for at, t_open, t_closed, t_pnl in sc_type_stats:
            t_unreal = sc_unreal_type.get(at, 0.0)
            lbl  = TYPE_LABELS.get(at, at or "—")
            ps   = "+" if t_pnl    >= 0 else ""
            us   = "+" if t_unreal >= 0 else ""
            lines.append(
                f"  <b>{lbl}</b>:  "
                f"Открыто <b>{t_open}</b>  │  Закрыто <b>{t_closed}</b>  │  "
                f"Прибыль <b>{ps}${t_pnl:.2f}</b>  │  "
                f"Нереал. <b>{us}${t_unreal:.2f}</b>"
            )

    # ── 🧲 Умный вход по ликвидности ────────────────────────────────────────
    if liq_row:
        lp, lf, mkt, exp, miss, open_n, ct, ctp, csl, cpnl, tnf = liq_row
        lp = lp or 0; lf = lf or 0; mkt = mkt or 0; exp = exp or 0
        miss = miss or 0; open_n = open_n or 0; ct = ct or 0
        ctp = ctp or 0; csl = csl or 0; cpnl = cpnl or 0.0; tnf = tnf or 0
        fill_rate = lf / lp * 100.0 if lp else 0.0
        wr = ctp / ct * 100.0 if ct else 0.0

        lines += ["", "━━━━━━━━━━━━━━━━━━━━",
                  "🧲 <b>Умный вход по ликвидности</b>"]
        lines.append(
            f"  Лимиток: <b>{lp}</b>  →  исполнено <b>{lf}</b> ({fill_rate:.0f}%)"
            + (f"  │  рыночных: <b>{mkt}</b>" if mkt else "")
        )
        if tnf or exp or miss:
            parts = []
            if tnf:  parts.append(f"касаний без входа: {tnf}")
            if exp:  parts.append(f"истекло: {exp}")
            if miss: parts.append(f"ушло без входа: {miss}")
            lines.append("  " + "  │  ".join(parts))

        # Open liquidity positions
        if liq_open_rows:
            unreal_liq = 0.0
            liq_open_lines = []
            for sym, dr, ep, sl, tp, ts in liq_open_rows:
                icon = "📈" if dr == "LONG" else "📉"
                age_h = (now_ts - (ts or now_ts)) / 3600
                cur = open_prices.get(sym)
                if cur and ep:
                    upct = (cur - ep) / ep * 100.0 if dr == "LONG" else (ep - cur) / ep * 100.0
                    unreal_liq += upct
                    s = "+" if upct >= 0 else ""
                    pi = "🟢" if upct >= 0 else "🔴"
                    liq_open_lines.append(
                        f"  {icon} <code>{sym}</code> {dr}  {ep:,.5g}→{cur:,.5g}  {pi}<b>{s}{upct:.1f}%</b>  ⏱{age_h:.1f}ч"
                    )
                else:
                    liq_open_lines.append(f"  {icon} <code>{sym}</code> {dr}  вход {ep:,.5g}  ⏱{age_h:.1f}ч")
            us = "+" if unreal_liq >= 0 else ""
            lines.append(f"\n  📂 <b>Открытые</b> ({open_n} поз.)  нереализованный: <b>{us}${unreal_liq:.2f}</b>")
            lines += liq_open_lines

        # Closed liquidity positions
        if ct:
            cs = "+" if cpnl >= 0 else ""
            lines.append(
                f"\n  📁 <b>Закрытых: {ct}</b>  Win-rate: <b>{wr:.0f}%</b> (TP:{ctp}/SL:{csl})  "
                f"PnL: <b>{cs}${cpnl:.2f}</b>"
            )
            for sym, dr, ep, ex_p, pnl, res, ts_o, ts_c in (liq_closed_rows or []):
                icon = "📈" if dr == "LONG" else "📉"
                ri = "✅" if res == "tp" else "❌"
                pi = "🟢" if (pnl or 0) >= 0 else "🔴"
                s = "+" if (pnl or 0) >= 0 else ""
                dur_h = (ts_c - ts_o) / 3600 if ts_c and ts_o else 0
                tag = "TP" if res == "tp" else "SL"
                lines.append(
                    f"  {ri} <code>{sym}</code> {dr}  {ep:,.5g}→{ex_p:,.5g}  "
                    f"{pi}<b>{s}${pnl:.2f}</b>  [{tag}]  ⏱{dur_h:.0f}ч"
                )

    # ── Основное сообщение (main stats + shadow summary + liquidity) ─────────
    _main_chunk = ""
    for _ln in lines:
        _candidate = _main_chunk + ("\n" if _main_chunk else "") + _ln
        if len(_candidate) > 3800 and _main_chunk:
            _telegram_send(chat_id, _main_chunk)
            _main_chunk = _ln
        else:
            _main_chunk = _candidate
    if _main_chunk:
        _telegram_send(chat_id, _main_chunk)

    # ── Блок «По причине блокировки» — всегда отдельным сообщением ──────────
    if sc_reason_stats:
        _sr_lines = ["🔒 <b>По причине блокировки</b> (теневые позиции):"]
        for rp, r_open, r_closed, r_tp, r_sl, r_pnl, r_avg in sc_reason_stats:
            rp_safe = html.escape(str(rp))
            if r_closed == 0:
                _sr_lines.append(f"  ⏳ <code>{rp_safe}</code>: открыто {r_open}")
                continue
            wr_r = r_tp / r_closed * 100
            pnl_icon = "🟢" if r_pnl >= 0 else "🔴"
            ps = "+" if r_pnl >= 0 else ""
            as_ = "+" if r_avg >= 0 else ""
            _sr_lines.append(
                f"  {pnl_icon} <code>{rp_safe}</code>: "
                f"{r_closed} сд., WR {wr_r:.0f}% "
                f"│ Σ <b>{ps}${r_pnl:.2f}</b> "
                f"│ avg <b>{as_}${r_avg:.2f}</b>"
            )
        _telegram_send(chat_id, "\n".join(_sr_lines))


def handle_demoshadow_command(chat_id: int) -> None:
    """Show last 15 shadow (blocked) positions with block reason, direction, PnL."""
    try:
        with _db_lock:
            conn = _get_db()
            rows = conn.execute("""
                SELECT symbol, direction, alert_type, shadow_reason,
                       entry_price, exit_price, pnl_usd, status,
                       ts_open, ts_close
                FROM demo_positions
                WHERE is_shadow=1
                ORDER BY ts_open DESC
                LIMIT 20
            """).fetchall()
            reason_agg = conn.execute("""
                SELECT
                  CASE
                    WHEN shadow_reason LIKE 'bear_downtrend%' THEN 'bear_downtrend'
                    WHEN shadow_reason LIKE '%:%'
                         THEN TRIM(SUBSTR(shadow_reason, 1, INSTR(shadow_reason, ':') - 1))
                    ELSE COALESCE(shadow_reason, '?')
                  END AS rp,
                  COUNT(*) AS total,
                  SUM(CASE WHEN status!='open' THEN 1 ELSE 0 END) AS closed_n,
                  SUM(CASE WHEN status='tp'   THEN 1 ELSE 0 END) AS tp_n,
                  COALESCE(SUM(CASE WHEN status!='open' THEN pnl_usd ELSE 0 END), 0) AS pnl
                FROM demo_positions
                WHERE is_shadow=1 AND shadow_reason IS NOT NULL AND shadow_reason != ''
                GROUP BY rp ORDER BY pnl DESC
            """).fetchall()
    except Exception as exc:
        _telegram_send(chat_id, f"❌ Ошибка БД: {exc}")
        return

    lines = ["👻 <b>Заблокированные сигналы — разбивка по причине</b>"]
    if reason_agg:
        lines.append("")
        for rp, total, closed_n, tp_n, pnl in reason_agg:
            wr = tp_n / closed_n * 100 if closed_n else 0
            pi = "🟢" if pnl >= 0 else "🔴"
            ps = "+" if pnl >= 0 else ""
            status_note = f"WR {wr:.0f}%, Σ{ps}${pnl:.2f}" if closed_n else f"открыто: {total}"
            lines.append(
                f"  {pi} <code>{rp}</code>: {total} всего / {closed_n} закрыто — {status_note}"
            )

    lines += ["", "📋 <b>Последние 20 заблокированных</b>"]
    import time as _time_mod
    now_ts = int(_time_mod.time())
    for sym, dr, at, reason, ep, ex_p, pnl, sts, ts_o, ts_c in rows:
        icon = "📈" if dr == "LONG" else "📉"
        age_h = (now_ts - (ts_o or now_ts)) / 3600
        at_short = {"confluence": "Конфл", "oversold_24h": "Перепрод",
                    "overheated_24h": "Перегрев", "streak_1h": "Серия"}.get(at, at or "?")
        reason_short = (reason or "?")[:35]
        if sts != "open":
            ri = "✅" if sts == "tp" else "❌"
            ps = "+" if (pnl or 0) >= 0 else ""
            lines.append(
                f"  {icon} <code>{sym}</code> [{at_short}] {ri} {ps}${(pnl or 0):.2f} "
                f"🔒 <i>{reason_short}</i>"
            )
        else:
            dur = f"⏱{age_h:.1f}ч"
            lines.append(
                f"  {icon} <code>{sym}</code> [{at_short}] открыто {dur} "
                f"🔒 <i>{reason_short}</i>"
            )
    _telegram_send(chat_id, "\n".join(lines))


def handle_demo2_command(chat_id: int) -> None:
    """Detailed list of every open demo position with individual PnL, grouped LONG/SHORT."""
    TYPE_SHORT: dict[str, str] = {
        "confluence":     "Конфл",
        "oversold_24h":   "Перепрод",
        "streak_1h":      "Серия",
        "overheated_24h": "Перегрев",
    }
    try:
        with _db_lock:
            conn = _get_db()
            open_rows = conn.execute(
                "SELECT symbol, direction, entry_price, sl_price, tp_price, ts_open, alert_type "
                "FROM demo_positions "
                "WHERE status='open' AND is_shadow=0 "
                "ORDER BY direction DESC, ts_open DESC"
            ).fetchall()
            closed_rows = conn.execute(
                "SELECT symbol, direction, entry_price, exit_price, pnl_usd, "
                "status, ts_open, ts_close, alert_type "
                "FROM demo_positions "
                "WHERE status!='open' AND is_shadow=0 "
                "ORDER BY ts_close DESC LIMIT 15"
            ).fetchall()
    except Exception as exc:
        _telegram_send(chat_id, f"❌ Ошибка БД: {exc}")
        return

    # Fetch prices for all open positions in parallel
    syms = list({r[0] for r in open_rows})
    prices: dict[str, float] = {}
    def _fetch_price(sym: str) -> None:
        try:
            t = _gateio_ticker(sym)
            if t:
                prices[sym] = float(t["lastPrice"])
        except Exception:
            pass
    with ThreadPoolExecutor(max_workers=12) as ex:
        list(ex.map(_fetch_price, syms))

    now_ts = int(time.time())
    longs  = [r for r in open_rows if r[1] == "LONG"]
    shorts = [r for r in open_rows if r[1] == "SHORT"]

    # Total unrealized PnL
    total_unreal = 0.0
    for sym, dr, entry, *_ in open_rows:
        cur = prices.get(sym)
        if cur and entry:
            total_unreal += (cur - entry) / entry * 100.0 if dr == "LONG" \
                       else (entry - cur) / entry * 100.0

    def _pos_line(sym: str, dr: str, entry: float, sl: float, tp: float,
                  ts: int, at: str) -> str:
        age_h = (now_ts - ts) / 3600
        icon  = "📈" if dr == "LONG" else "📉"
        cur   = prices.get(sym)
        t_lbl = TYPE_SHORT.get(at or "", at or "")
        if cur and entry:
            pct = (cur - entry) / entry * 100.0 if dr == "LONG" \
                 else (entry - cur) / entry * 100.0
            usd = pct           # $100 per trade → 1% = $1
            p_icon = "🟢" if pct >= 0 else "🔴"
            s = "+" if pct >= 0 else ""
            return (
                f"{icon} <code>{sym}</code>  "
                f"вход <b>{entry:,.5g}</b> → <b>{cur:,.5g}</b>  "
                f"{p_icon} <b>{s}{pct:.1f}% ({s}${usd:.2f})</b>  "
                f"⏱{age_h:.0f}ч  <i>[{t_lbl}]</i>\n"
                f"     🎯 TP: <b>${tp:,.5g}</b>  │  🛑 SL: <b>${sl:,.5g}</b>"
            )
        return (
            f"{icon} <code>{sym}</code>  вход <b>{entry:,.5g}</b>  "
            f"⏱{age_h:.0f}ч  <i>[{t_lbl}]</i>"
        )

    u_sign = "+" if total_unreal >= 0 else ""
    all_lines: list[str] = [
        "📋 <b>ДЕМО — все открытые позиции</b>",
        f"  📈 LONG: <b>{len(longs)}</b>  │  📉 SHORT: <b>{len(shorts)}</b>  │  Итого: <b>{len(open_rows)}</b>",
        f"  Нереализованный PnL: <b>{u_sign}${total_unreal:.2f}</b>",
    ]

    if longs:
        all_lines += ["", f"━━━━━━━ 📈 LONG ({len(longs)}) ━━━━━━━"]
        for sym, dr, entry, sl, tp, ts, at in longs:
            all_lines.append(_pos_line(sym, dr, entry, sl, tp, ts, at))

    if shorts:
        all_lines += ["", f"━━━━━━━ 📉 SHORT ({len(shorts)}) ━━━━━━━"]
        for sym, dr, entry, sl, tp, ts, at in shorts:
            all_lines.append(_pos_line(sym, dr, entry, sl, tp, ts, at))

    if closed_rows:
        all_lines += ["", "━━━━━━━ 📁 Последние закрытые ━━━━━━━"]
        for sym, dr, entry, exit_p, pnl_usd, status, ts_open, ts_close, at in closed_rows:
            icon     = "📈" if dr == "LONG" else "📉"
            res_icon = "✅" if status == "tp" else "❌"
            p_icon   = "🟢" if (pnl_usd or 0) >= 0 else "🔴"
            s        = "+" if (pnl_usd or 0) >= 0 else ""
            tag      = "TP" if status == "tp" else ("SL" if status == "sl" else status.upper())
            t_lbl    = TYPE_SHORT.get(at or "", at or "")
            dur_h    = (ts_close - ts_open) / 3600 if ts_close and ts_open else 0
            all_lines.append(
                f"{res_icon} <code>{sym}</code> {dr}  "
                f"{entry:,.5g} → {exit_p:,.5g}  "
                f"{p_icon} <b>{s}${pnl_usd:.2f}</b>  [{tag}]  ⏱{dur_h:.0f}ч  <i>[{t_lbl}]</i>"
            )

    # Send in chunks (Telegram limit 4096 chars)
    chunk: list[str] = []
    chunk_len = 0
    part = 1
    for line in all_lines:
        if chunk_len + len(line) + 1 > 3800 and chunk:
            _telegram_send(chat_id, "\n".join(chunk))
            chunk = [f"📋 <b>...продолжение (ч.{part})</b>"]
            chunk_len = len(chunk[0])
            part += 1
        chunk.append(line)
        chunk_len += len(line) + 1
    if chunk:
        _telegram_send(chat_id, "\n".join(chunk))


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
        "/ai":       handle_ai_command,
        "/silence":  handle_silence_command,
        "/unmute":   handle_unmute_command,
        "/demo":        handle_demo_command,
        "/demo2":       handle_demo2_command,
        "/demoshadow":  handle_demoshadow_command,
        "/scorestats":  handle_scorestats_command,
        "/addpos":   handle_addpos_command,
        "/mypos":    handle_mypos_command,
        "/closepos": handle_closepos_command,
        "/unwatch":  handle_unwatch_command,
    }
    offset: int | None = None
    logger.info("Telegram command polling started")
    while True:
        try:
            # Use POST with JSON body so Telegram correctly parses
            # allowed_updates as a JSON array (GET+params sends repeated
            # keys which Telegram ignores, leaving the cached filter intact
            # and breaking callback_query delivery).
            payload: dict = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                payload["offset"] = offset
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                json=payload,
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
                # AI question handler — fires when user tapped "🤖 Спросить AI" button
                with _ai_ask_pending_lock:
                    pending = _ai_ask_pending.pop(chat_id, None)
                if pending and time.time() - pending["ts"] < 300:  # 5-min window
                    def _handle_ai_q(p=pending, q=raw_text, cid=chat_id):
                        sym = p["symbol"]; rec = p["rec"]
                        atype = p["alert_type"]; score = p["score"]
                        rsi = None
                        try: rsi = get_klines_rsi(sym)
                        except Exception: pass
                        pct24 = None
                        try:
                            t = _gateio_ticker(sym)
                            if t: pct24 = float(t["priceChangePercent"])
                        except Exception: pass
                        closes = _fetch_daily_closes_for_crsi(sym)
                        trend_up   = _count_consecutive_trend_days(closes, "up")   if closes else 0
                        trend_down = _count_consecutive_trend_days(closes, "down") if closes else 0
                        trend_str = (
                            f"растёт {trend_up} дней подряд" if trend_up >= 2
                            else f"падает {trend_down} дней подряд" if trend_down >= 2
                            else "боковое движение"
                        )
                        prompt = (
                            f"Ты — опытный трейдер крипто-фьючерсов. Пользователь получил сигнал и задаёт вопрос.\n\n"
                            f"=== КОНТЕКСТ СИГНАЛА ===\n"
                            f"Монета: {sym}\n"
                            f"Направление: {rec}\n"
                            f"Тип сигнала: {atype}\n"
                            f"Score: {score}/100\n"
                            f"RSI: {f'{rsi:.1f}' if rsi else 'н/д'}\n"
                            f"24ч изменение: {f'{pct24:+.1f}%' if pct24 else 'н/д'}\n"
                            f"Тренд (дневные): {trend_str}\n\n"
                            f"=== ВОПРОС ПОЛЬЗОВАТЕЛЯ ===\n{q}\n\n"
                            f"Ответь по-русски, конкретно, 2-4 предложения. "
                            f"Не гарантируй прибыль. Упомяни ключевой риск."
                        )
                        answer = _call_gemini_text(prompt, max_tokens=300, temperature=0.7, timeout=20)
                        if answer:
                            _telegram_send(cid,
                                f"🤖 <b>{html.escape(sym)} · AI ответ</b>\n\n"
                                f"<i>{html.escape(answer)}</i>"
                            )
                        else:
                            _telegram_send(cid, "⚠️ AI не ответил, попробуй ещё раз.")
                    threading.Thread(target=_handle_ai_q, daemon=True).start()
                    continue
                for cmd, handler in COMMANDS.items():
                    # Exact match — the command must be the whole word, optionally
                    # followed by whitespace + args. Prevents '/trader' matching '/trade'.
                    if text == cmd or text.startswith(cmd + " "):
                        logger.info("Command %s from chat_id=%s", cmd, chat_id)
                        # Commands that take args receive the full raw text;
                        # the rest are called with just chat_id (backwards-compat).
                        takes_args = cmd in ("/trade", "/analyze", "/ai", "/addpos", "/closepos", "/unwatch")
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
    logger.info("Telegram command polling started")


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
            # 1. Telegram polling thread (skip if polling is disabled)
            if os.getenv("BOT_POLLING_DISABLED", "").lower() != "true":
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


def _binance_get(path: str, params: dict | None = None, timeout: int = 8):
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
    """Return all active USDT perpetual contracts from Gate.io Futures."""
    resp = _gateio_get("/contracts", timeout=15)
    contracts = resp.json()
    return [
        _from_gate(c["name"])
        for c in contracts
        if c["name"].endswith("_USDT")
        and c.get("status") == "trading"
        and not c.get("in_delisting", False)
    ]


def get_24h_tickers(symbols: list[str]) -> dict[str, dict]:
    """Fetch all Gate.io Futures tickers and return in Binance-compatible format."""
    resp = _gateio_get("/tickers", timeout=10)
    all_tickers = resp.json()
    symbol_set = set(symbols)
    result = {}
    for t in all_tickers:
        sym = _from_gate(t.get("contract", ""))
        if sym not in symbol_set:
            continue
        result[sym] = {
            "symbol":             sym,
            "lastPrice":          t.get("last", "0"),
            "price":              t.get("last", "0"),
            "priceChangePercent": t.get("change_percentage", "0"),
            "quoteVolume":        t.get("volume_24h_quote", "0"),
            "highPrice":          t.get("high_24h", "0"),
            "lowPrice":           t.get("low_24h", "0"),
        }
    return result


def get_klines_rsi(symbol: str) -> float | None:
    try:
        candles = _gateio_klines(symbol, "1h", RSI_PERIOD + 1)
        closes = [float(k[4]) for k in candles]
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
      the candle 2 slots earlier (~10-minute window).
    """
    try:
        candles = _gateio_klines(symbol, "5m", 14)
        if len(candles) < 13:
            return None, None

        # candles[-1] may be incomplete; use candles[-2] as latest complete
        volumes = [float(k[5]) for k in candles]
        last_vol = volumes[-2]
        avg_vol = float(np.mean(volumes[:-2]))
        spike: float | None = (last_vol / avg_vol) if avg_vol > 0 else None

        closes = [float(k[4]) for k in candles]
        price_now = closes[-2]
        price_15m = closes[-4] if len(closes) >= 4 else None
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
    """Return daily kline summary from Gate.io.

    Keys: highs, closes, yesterday_vol, today_vol.
    - highs/closes: list[float], oldest→newest, length = number of complete candles
    - yesterday_vol / today_vol: 24h quote volume (USDT)
    """
    try:
        candles = _gateio_klines(symbol, "1d", limit)
        if not candles:
            return None
        highs  = [float(k[2]) for k in candles]
        closes = [float(k[4]) for k in candles]
        yesterday_vol = float(candles[-2][7]) if len(candles) >= 2 else 0.0
        today_vol = float(candles[-1][7])
        return {"highs": highs, "closes": closes,
                "yesterday_vol": yesterday_vol, "today_vol": today_vol}
    except Exception as e:
        logger.warning("Daily klines failed for %s: %s", symbol, e)
        return None


def get_two_day_volumes(symbol: str) -> tuple[float, float] | None:
    """Return (yesterday_usdt_vol, today_usdt_vol) from the last 2 daily candles."""
    try:
        candles = _gateio_klines(symbol, "1d", 2)
        if len(candles) < 2:
            return None
        return float(candles[0][7]), float(candles[1][7])
    except Exception as e:
        logger.warning("2-day volume fetch failed for %s: %s", symbol, e)
        return None


_CRSI_KLINES_CACHE: dict[str, tuple[float, list[float]]] = {}
_CRSI_KLINES_CACHE_TTL = 1800  # 30 min — daily closes don't change intraday

# Pending "ask AI" state: chat_id → {alert_id, symbol, rec, alert_type, score, ts}
_ai_ask_pending: dict[int, dict] = {}
_ai_ask_pending_lock = threading.Lock()


def _fetch_daily_closes_for_crsi(symbol: str) -> list[float] | None:
    """Cached daily-close fetcher for CRSI. Returns None on error or
    insufficient candles."""
    now = time.time()
    cached = _CRSI_KLINES_CACHE.get(symbol)
    if cached and now - cached[0] < _CRSI_KLINES_CACHE_TTL:
        return cached[1]
    try:
        candles = _gateio_klines(symbol, "1d", CRSI_KLINES_LIMIT)
    except Exception as e:
        logger.warning("CRSI daily klines fetch failed for %s: %s", symbol, e)
        return None
    if not candles or len(candles) < 30:
        return None
    closes = [float(k[4]) for k in candles]
    _CRSI_KLINES_CACHE[symbol] = (now, closes)
    return closes


def _count_consecutive_trend_days(closes: list[float], direction: str) -> int:
    """Count how many consecutive daily closes moved in `direction` ('up'/'down').

    Uses the last N daily candles (most-recent last). Returns 0 if data is
    insufficient or no consecutive streak is found.
    """
    if not closes or len(closes) < 2:
        return 0
    count = 0
    # Walk backwards from the most-recent candle (skip the current day — it
    # may be incomplete) comparing close[i] vs close[i-1].
    for i in range(len(closes) - 1, 0, -1):
        if direction == "up" and closes[i] > closes[i - 1]:
            count += 1
        elif direction == "down" and closes[i] < closes[i - 1]:
            count += 1
        else:
            break
    return count


def _count_up_in_window(closes: list[float], n: int = 6) -> int:
    """Count how many of the last N 4h-candles closed higher than the previous one.

    Uses the most-recent (n+1) candles to form n consecutive pairs and counts
    bullish pairs. Ignores the current (incomplete) candle — caller should pass
    only completed closes (all but the last element of the raw list).

    Returns 0 if there is insufficient data.
    """
    if not closes or len(closes) < n + 1:
        return 0
    recent = closes[-(n + 1):]          # n+1 candles → n pairs
    return sum(1 for i in range(1, len(recent)) if recent[i] > recent[i - 1])


def _shadow_notif_allowed(notif_key: str) -> bool:
    """Return True (and increment today's counter) when a shadow Telegram
    notification is within the daily limit for *notif_key*.

    Keys and limits are configured in SHADOW_NOTIF_LIMITS. Returns False
    immediately when the key has no entry (limit=0) or the daily cap is full.
    Thread-safe via state_lock.
    """
    limit = SHADOW_NOTIF_LIMITS.get(notif_key, 0)
    if limit == 0:
        return False
    today = time.strftime("%Y-%m-%d")
    with state_lock:
        entry = state["shadow_notif_today"].get(notif_key)
        if entry is None or entry["date"] != today:
            entry = {"date": today, "count": 0}
            state["shadow_notif_today"][notif_key] = entry
        if entry["count"] >= limit:
            return False
        entry["count"] += 1
        return True


def _trend_warning_line(symbol: str, recommendation: str) -> str:
    """Return a warning string if the signal direction conflicts with the
    multi-day daily trend, or '' if no conflict detected.

    SHORT against N≥3 consecutive UP days → warn.
    LONG  against N≥3 consecutive DOWN days → warn.
    """
    closes = _fetch_daily_closes_for_crsi(symbol)
    if not closes:
        return ""
    if recommendation == "SHORT":
        days = _count_consecutive_trend_days(closes, "up")
        if days >= 3:
            return (
                f"⚠️ <b>Тренд вверх {days} дней подряд</b> — SHORT против тренда, риск выше обычного"
            )
    elif recommendation == "LONG":
        days = _count_consecutive_trend_days(closes, "down")
        if days >= 3:
            return (
                f"⚠️ <b>Тренд вниз {days} дней подряд</b> — LONG против тренда, риск выше обычного"
            )
    return ""


def _bear_downtrend_blocks_long(symbol: str) -> tuple[bool, int]:
    """Return (should_block, down_days) when a LONG signal should be suppressed.

    Logic: perversold RSI in a BEAR market with 3+ consecutive down days is
    NOT a bounce — it is a falling knife.  Block the LONG and open a shadow
    demo position so we can track the missed opportunity.

    Conditions (all must be true):
      1. Market regime == "BEAR"    (BTC below its 4h EMA-200)
      2. Symbol has ≥ 3 consecutive daily closes moving down

    Fails OPEN (returns False) on any data error so real signals are never
    silently dropped due to a candle-fetch timeout.
    """
    if get_market_regime() != "BEAR":
        return False, 0
    closes = _fetch_daily_closes_for_crsi(symbol)
    if not closes:
        return False, 0
    days = _count_consecutive_trend_days(closes, "down")
    # 3-4 дня вниз → блокировать (18% WR, монета продолжает падать)
    # 5+ дней вниз → НЕ блокировать (90.9% WR, экстремальная перепроданность = отскок)
    block = 3 <= days <= 4
    return (block, days)


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
        raw = _gateio_klines(symbol, "4h", EMA200_FETCH_LIMIT)
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


# --- Futures fallbacks (now use Gate.io — same data source as primary) ---

def get_klines_rsi_futures(symbol: str) -> float | None:
    """RSI via Gate.io (unified data source — no separate futures endpoint needed)."""
    return get_klines_rsi(symbol)


def get_ema200_4h_futures(symbol: str) -> float | None:
    """EMA-200 via Gate.io (unified data source)."""
    stats = get_4h_stats(symbol)
    return stats["ema200"] if stats else None


def get_24h_ticker_futures(symbol: str) -> dict | None:
    """24h ticker via Gate.io (unified data source)."""
    return _gateio_ticker(symbol)


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
        data   = _gateio_klines("BTCUSDT", "1d", 300)
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
    # Exception: pure oversold bounce (RSI ≤ 30) is a reversal — fires naturally below EMA
    short_blocked_by_ema = above_ema200 is True and short_signal
    long_blocked_by_ema = above_ema200 is False and long_signal and not is_oversold
    if above_ema200 is True:
        short_signal = False
    if above_ema200 is False and not is_oversold:
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
            ("price_1d", "REAL"),
            ("price_2d", "REAL"),
            ("price_3d", "REAL"),
            ("price_7d", "REAL"),
            ("factor_funding_pts", "INTEGER DEFAULT 0"),
            ("factor_lsr_pts",     "INTEGER DEFAULT 0"),
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
        # Migrate: per-position custom timeout (long-duration trades)
        try:
            _db_conn.execute(
                "ALTER TABLE position_monitors "
                "ADD COLUMN timeout_hours REAL DEFAULT 4"
            )
        except sqlite3.OperationalError:
            pass  # column already exists
        # ── Visitor analytics ───────────────────────────────────────────────
        _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS page_views (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           INTEGER NOT NULL,
                page         TEXT    NOT NULL,
                visitor_hash TEXT    NOT NULL,
                user_agent   TEXT
            )
        """)
        _db_conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pv_ts      ON page_views(ts)"
        )
        _db_conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pv_visitor ON page_views(visitor_hash)"
        )
        # ── Demo (paper trading) positions ───────────────────────────────────
        _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS demo_positions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_open       INTEGER NOT NULL,
                symbol        TEXT    NOT NULL,
                direction     TEXT    NOT NULL,
                entry_price   REAL    NOT NULL,
                sl_price      REAL    NOT NULL,
                tp_price      REAL    NOT NULL,
                size_usd      REAL    NOT NULL DEFAULT 100,
                status        TEXT    NOT NULL DEFAULT 'open',
                ts_close      INTEGER,
                exit_price    REAL,
                pnl_usd       REAL,
                is_shadow     INTEGER NOT NULL DEFAULT 0,
                shadow_reason TEXT,
                alert_type    TEXT,
                is_top        INTEGER NOT NULL DEFAULT 0,
                wick_close    INTEGER NOT NULL DEFAULT 0
            )
        """)
        # Migrate: wick_close for existing installs
        try:
            _db_conn.execute(
                "ALTER TABLE demo_positions ADD COLUMN wick_close INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError as _mig_wc:
            if "duplicate column" not in str(_mig_wc).lower():
                raise
        _db_conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_demo_status "
            "ON demo_positions(status, is_shadow)"
        )
        # Unique constraint: only one open position per (symbol, direction, is_shadow).
        # Belt-and-suspenders against the in-memory duplicate check above.
        _db_conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_demo_open_one_per_coin "
            "ON demo_positions(symbol, direction, is_shadow) WHERE status='open'"
        )
        # Migrate: is_top flag for existing installs
        try:
            _db_conn.execute(
                "ALTER TABLE demo_positions ADD COLUMN is_top INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError as _mig_exc:
            if "duplicate column" not in str(_mig_exc).lower():
                logger.error("demo_positions is_top migration failed: %s", _mig_exc)
                raise
        # Backfill: all real (non-shadow) positions created before is_top was added
        # should be marked is_top=1, since any sent signal had score >= MIN_SEND_SCORE > TOP_SIGNAL_SCORE.
        _backfilled = _db_conn.execute(
            "UPDATE demo_positions SET is_top=1 WHERE is_shadow=0 AND is_top=0"
        ).rowcount
        if _backfilled:
            logger.info("demo_positions: backfilled is_top=1 for %d existing real positions", _backfilled)
        # Liquidity-entry strategy: parallel demo-only strategy
        _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS liquidity_orders (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_created        INTEGER NOT NULL,
                ts_expires        INTEGER NOT NULL,
                symbol            TEXT    NOT NULL,
                direction         TEXT    NOT NULL,
                entry_type        TEXT    NOT NULL,
                limit_price       REAL,
                entry_price       REAL,
                sl_price          REAL    NOT NULL,
                tp_price          REAL    NOT NULL,
                zone_price        REAL    NOT NULL,
                zone_weight       INTEGER NOT NULL,
                original_sl_price REAL,
                status            TEXT    NOT NULL DEFAULT 'pending',
                ts_filled         INTEGER,
                ts_closed         INTEGER,
                exit_price        REAL,
                pnl_usd           REAL,
                result            TEXT,
                saved_from_sl     INTEGER NOT NULL DEFAULT 0,
                touched_orig_sl   INTEGER NOT NULL DEFAULT 0,
                touched_no_fill   INTEGER NOT NULL DEFAULT 0
            )
        """)
        # Migrate: touched_no_fill for existing installs
        try:
            _db_conn.execute(
                "ALTER TABLE liquidity_orders ADD COLUMN touched_no_fill INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError as _mig_liq:
            if "duplicate column" not in str(_mig_liq).lower():
                raise
        # Personal positions tracker — user registers their own open positions;
        # bot monitors and sends confirmed reversal alerts.
        _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS user_positions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol       TEXT    NOT NULL,
                direction    TEXT    NOT NULL,  -- 'LONG' or 'SHORT'
                note         TEXT,
                ts_added     INTEGER NOT NULL,
                ts_closed    INTEGER,
                status       TEXT    NOT NULL DEFAULT 'open',
                last_alerted INTEGER NOT NULL DEFAULT 0
            )
        """)
        _db_conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_userpos_status "
            "ON user_positions(status)"
        )
        # ── Reversal watchlist ───────────────────────────────────────────────
        # One row per shadow-signal the user asked to watch for reversal conf.
        _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                source_demo_id INTEGER NOT NULL,
                symbol         TEXT    NOT NULL,
                direction      TEXT    NOT NULL,
                alert_type     TEXT    NOT NULL,
                entry_price    REAL    NOT NULL,
                added_ts       INTEGER NOT NULL,
                expires_ts     INTEGER NOT NULL,
                status         TEXT    NOT NULL DEFAULT 'active'
            )
        """)
        _db_conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_watchlist_active "
            "ON watchlist(status, expires_ts)"
        )
        # DB-level race guard: only one active entry per (symbol, direction).
        # Partial index so completed/expired rows don't interfere.
        _db_conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_watchlist_active "
            "ON watchlist(symbol, direction) WHERE status='active'"
        )
        # Every reversal-confirmation check is logged here for future WR analysis.
        _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS watchlist_triggers (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                watchlist_id   INTEGER NOT NULL,
                source_demo_id INTEGER NOT NULL,
                symbol         TEXT    NOT NULL,
                triggered_ts   INTEGER NOT NULL,
                trigger_price  REAL    NOT NULL,
                cond_candle    INTEGER NOT NULL DEFAULT 0,
                cond_touch     INTEGER NOT NULL DEFAULT 0,
                cond_volume    INTEGER NOT NULL DEFAULT 0
            )
        """)
        _db_conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_wl_triggers_wl "
            "ON watchlist_triggers(watchlist_id)"
        )
        # ── Entry confirmation watch ─────────────────────────────────────
        # One row per real LONG/SHORT signal the user asked to watch for an
        # optimal entry point (via inline button on the signal message).
        _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS entry_watch (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                source_alert_id  INTEGER NOT NULL,
                symbol           TEXT    NOT NULL,
                direction        TEXT    NOT NULL,
                alert_type       TEXT    NOT NULL,
                entry_price      REAL    NOT NULL,
                sl_price         REAL    NOT NULL,
                added_ts         INTEGER NOT NULL,
                expires_ts       INTEGER NOT NULL,
                first_check_done INTEGER NOT NULL DEFAULT 0,
                status           TEXT    NOT NULL DEFAULT 'active'
            )
        """)
        _db_conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ew_active "
            "ON entry_watch(status, expires_ts)"
        )
        # One active watch per alert — prevents double-tapping the button.
        _db_conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_ew_alert "
            "ON entry_watch(source_alert_id) WHERE status='active'"
        )
        # Outcome log for future win-rate analysis.
        _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS entry_watch_log (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_watch_id   INTEGER NOT NULL,
                source_alert_id  INTEGER NOT NULL,
                symbol           TEXT    NOT NULL,
                checked_ts       INTEGER NOT NULL,
                trigger_price    REAL    NOT NULL,
                outcome          TEXT    NOT NULL,
                confirmed_price  REAL,
                cond_direction   INTEGER NOT NULL DEFAULT 0,
                cond_volume      INTEGER NOT NULL DEFAULT 0,
                cond_no_pullback INTEGER NOT NULL DEFAULT 0
            )
        """)
        _db_conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ewl_ew "
            "ON entry_watch_log(entry_watch_id)"
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
    Horizons: 3м / 5м / 15м / 1ч / 4ч / 1д / 2д / 3д / 7д.
    Prices are captured at the first cycle tick *after* the threshold passes
    (≈ 5 min approximation for short horizons, negligible for daily ones).
    """
    if not tickers:
        return
    now_ts = int(time.time())
    c3m  = now_ts - 180
    c5m  = now_ts - 300
    c15  = now_ts - 900
    c1h  = now_ts - 3_600
    c4h  = now_ts - 14_400
    c1d  = now_ts - 86_400
    c2d  = now_ts - 172_800
    c3d  = now_ts - 259_200
    c7d  = now_ts - 604_800
    retain_cutoff = now_ts - HIT_RATE_RETENTION_DAYS * 86400
    try:
        with _db_lock:
            conn = _get_db()
            rows = conn.execute(
                "SELECT id, symbol, "
                "price_3m, price_5m, price_15m, price_1h, price_4h, "
                "price_1d, price_2d, price_3d, price_7d, ts "
                "FROM alerts "
                "WHERE (price_3m  IS NULL AND ts <= ?) "
                "   OR (price_5m  IS NULL AND ts <= ?) "
                "   OR (price_15m IS NULL AND ts <= ?) "
                "   OR (price_1h  IS NULL AND ts <= ?) "
                "   OR (price_4h  IS NULL AND ts <= ?) "
                "   OR (price_1d  IS NULL AND ts <= ?) "
                "   OR (price_2d  IS NULL AND ts <= ?) "
                "   OR (price_3d  IS NULL AND ts <= ?) "
                "   OR (price_7d  IS NULL AND ts <= ?)",
                (c3m, c5m, c15, c1h, c4h, c1d, c2d, c3d, c7d),
            ).fetchall()
            updates: list[tuple] = []
            for row_id, symbol, p3m, p5m, p15, p1h, p4h, p1d, p2d, p3d, p7d, ts in rows:
                t = tickers.get(symbol)
                if not t:
                    continue
                try:
                    cur = float(t["lastPrice"])
                except (ValueError, KeyError):
                    continue
                updates.append((
                    cur if (p3m is None and ts <= c3m)  else p3m,
                    cur if (p5m is None and ts <= c5m)  else p5m,
                    cur if (p15 is None and ts <= c15)  else p15,
                    cur if (p1h is None and ts <= c1h)  else p1h,
                    cur if (p4h is None and ts <= c4h)  else p4h,
                    cur if (p1d is None and ts <= c1d)  else p1d,
                    cur if (p2d is None and ts <= c2d)  else p2d,
                    cur if (p3d is None and ts <= c3d)  else p3d,
                    cur if (p7d is None and ts <= c7d)  else p7d,
                    row_id,
                ))
            if updates:
                conn.executemany(
                    "UPDATE alerts SET "
                    "price_3m=?, price_5m=?, price_15m=?, price_1h=?, price_4h=?, "
                    "price_1d=?, price_2d=?, price_3d=?, price_7d=? "
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


def _format_sl_tp(side: str, entry: float | None, atr: float | None, score: int = 0) -> str:
    """Return SL + 3-level TP lines for any signal.

    ATR-based levels (R:R per level):
      LONG:  SL  = entry − 1.5×ATR
             TP1 = entry + 1.5×ATR  (R:R 1:1)  → снять 40% позиции
             TP2 = entry + 3.0×ATR  (R:R 1:2)  → снять 40%
             TP3 = entry + 4.5×ATR  (R:R 1:3)  → снять 20%  [только score ≥ 70]

      SHORT: SL  = max(2×ATR, entry×1.025) выше цены входа
             TP1 = entry − SL_dist×1  (R:R 1:1)  → снять 40%
             TP2 = entry − SL_dist×2  (R:R 1:2)  → снять 40%
             TP3 = entry − SL_dist×3  (R:R 1:3)  → снять 20%  [только score ≥ 70]

    Fallback (ATR=None): фиксированные уровни из бэктеста; TP1=50% от TP2, TP3=150%.
    TP3 показывается только при score ≥ 70 (сильный сигнал, стоит держать дольше).
    Демо-позиции по-прежнему закрываются по TP2.
    side: 'buy'/'long' или 'sell'/'short'.
    """
    if entry is None or not (math.isfinite(entry) and entry > 0):
        return ""
    s = side.lower()
    if s not in ("buy", "long", "sell", "short"):
        return ""

    use_atr  = atr is not None and atr > 0 and math.isfinite(atr)
    show_tp3 = score >= 70

    if use_atr:
        if s in ("buy", "long"):
            sl   = entry - 1.5 * atr
            tp1  = entry + 1.5 * atr   # R:R 1:1
            tp2  = entry + 3.0 * atr   # R:R 1:2
            tp3  = entry + 4.5 * atr   # R:R 1:3
        else:
            raw_sl   = entry + SHORT_ATR_SL_MULT * atr
            floor_sl = entry * (1 + SHORT_ATR_SL_MIN_PCT / 100)
            sl       = max(raw_sl, floor_sl)
            sl_dist  = sl - entry
            tp1  = entry - sl_dist        # R:R 1:1
            tp2  = entry - sl_dist * 2    # R:R 1:2
            tp3  = entry - sl_dist * 3    # R:R 1:3
        orient = ""
    else:
        # Fallback: fixed backtested constants — valid as orientation levels
        if s in ("buy", "long"):
            sl   = entry * (1 - HIT_RATE_SL_PCT / 100)
            tp2  = entry * (1 + HIT_RATE_TP_PCT / 100)
            tp1  = entry + (tp2 - entry) * 0.5
            tp3  = entry + (tp2 - entry) * 1.5
        else:
            sl   = entry * (1 + SHORT_HOLD_SL_PCT / 100)
            tp2  = entry * (1 - SHORT_HOLD_TP_PCT / 100)
            tp1  = entry - (entry - tp2) * 0.5
            tp3  = entry - (entry - tp2) * 1.5
        orient = " ориент."

    if sl <= 0 or tp1 <= 0 or tp2 <= 0:
        return ""

    def _lev_roi(p: float) -> str:
        """Return '×2:±X% / ×5:±Y% / ×10:±Z%' for a price level."""
        raw = abs(p - entry) / entry * 100
        sign = "+" if p > entry else "−"
        r2  = f"{sign}{raw * 2:.0f}%"
        r5  = f"{sign}{raw * 5:.0f}%"
        r10 = f"{sign}{raw * 10:.0f}%"
        suffix = orient if orient else ""
        return f"×2:{r2} / ×5:{r5} / ×10:{r10}{suffix}"

    has_tp3 = show_tp3 and tp3 > 0
    tp1_label = "снять 40%" if has_tp3 else "снять 50%"
    tp2_label = "снять 40%" if has_tp3 else "закрыть всё"
    lines = [
        f"🛡️ SL:  <code>${sl:,.6g}</code>  {_lev_roi(sl)}",
        f"🎯 TP1: <code>${tp1:,.6g}</code>  {_lev_roi(tp1)}  · {tp1_label}",
        f"🎯 TP2: <code>${tp2:,.6g}</code>  {_lev_roi(tp2)}  · {tp2_label}",
    ]
    if has_tp3:
        lines.append(
            f"🎯 TP3: <code>${tp3:,.6g}</code>  {_lev_roi(tp3)}  · снять 20%"
        )
    return "\n".join(lines)


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

MIN_ALERT_SCORE = int(os.environ.get("MIN_ALERT_SCORE", "50"))

# Per-type score minimums — override the global MIN_ALERT_SCORE for specific
# signal types where historical data shows a higher bar improves quality.
MIN_SCORE_BY_TYPE: dict[str, int] = {
    # ── SHORT / SELL — raised from 70→75 (2026-08-08): demo data showed 10% win rate
    # for regular SHORTs. Only the highest-conviction shorts should fire.
    "overheated_24h":      75,
    "confluence":          52,   # LONG и SHORT: score≥52 (тренд по EMA-200)
    "breakdown_short":     75,   # raised from 70
    "momentum_up_2":       75,   # raised from 70
    "momentum_up_3":       75,   # raised from 70
    "momentum_up_5":       75,   # raised from 70
    "momentum_up_10":      75,   # raised from 70
    "momentum_down_5":     75,   # raised from 70
    "momentum_down_10":    75,   # raised from 70
    "volume_surge_short":  75,   # raised from 70
    "pump_24h_fade":       65,   # fade 24h pump → SHORT; shadow-only initially
    "new_listing_short":   75,   # raised from 70
    "listing_peak_short":  75,   # raised from 70
    # ── LONG / BUY — score filter doesn't improve quality; keep lower bar ──
    "momentum_long":       55,
    "listing_dump_long":   50,
}

# ── LONG-specific score window — backtest 30d ───────────────────────────────
# For LONG signals score is INVERSELY correlated with outcome: the only
# profitable zone is 55–65 (TP:SL 1.19x). Above 65 quality degrades sharply
# (≥70 → 0.59x). Below 55 → mostly SL (71% SL rate). Separate min/max dicts
# let SHORT keep the ≥70 bar while LONG gets its own optimal window.
MIN_SCORE_LONG_BY_TYPE: dict[str, int] = {
    "confluence":   55,   # override global 70 — LONG sweet spot starts at 55
    "oversold_24h": 55,   # score 55-65: TP 37-40%; ≥60 SL rate spikes to 50%+
}
MAX_SCORE_LONG_BY_TYPE: dict[str, int] = {
    # "confluence" max removed — EMA-based direction, старая статистика RSI неактуальна
    "oversold_24h": 65,   # above 65: TP:SL collapses to 0.32x
}

# --- Breakdown continuation SHORT ---
BREAKDOWN_SHORT_PCT          = -10.0  # 24h drop must be ≤ this for "room to fall" path
BREAKDOWN_RSI_MIN            = 30.0   # RSI must be above (confirms room to fall)
BREAKDOWN_RSI_MAX            = 58.0   # RSI must be below (confirms selling pressure)
BREAKDOWN_COOLDOWN           = 14400  # 4h cooldown per coin
# "Oversold continuation" path — RSI already low but coin keeps falling
BREAKDOWN_RSI_OVERSOLD_MAX   = 35.0   # RSI must be ≤ this (oversold territory)
BREAKDOWN_RSI_OVERSOLD_DROP  = -15.0  # 24h drop must be ≤ this (stronger confirmation)

# --- Intraday hourly streak (6h+ consecutive green/red 1h candles) ---
STREAK_1H_MIN          = 6       # minimum consecutive green/red hours to trigger
STREAK_1H_COOLDOWN     = 28800   # 8h cooldown per symbol (avoid re-spamming same streak)
STREAK_1H_PRE_FILTER   = 3.0    # min |pct24| to even fetch 1h candles (saves API calls)
STREAK_1H_RSI_MAX_LONG = 73.0   # RSI ceiling for LONG streak (not already overbought)
STREAK_1H_RSI_MIN_SHORT= 18.0   # RSI floor for SHORT streak (extreme oversold ≤18 skipped)
STREAK_1H_LONG_MAX_GAIN = 15.0  # если монета уже выросла >15% за стрик — перегрета, пропустить ЛОНГ
STREAK_1H_SHORT_MAX_LOSS= 15.0  # если монета уже упала  >15% за стрик — перепродана, пропустить SHORT
STREAK_1H_REVERSAL_PCT  = 0.5   # если живая цена ниже last_close на >0.5% — разворот, пропустить ЛОНГ

# --- VWAP Reversion shadow (mean-reversion complement to Серия 1ч) ---
# Opposite direction from streak_1h: upstreak → SHORT to VWAP, downstreak → LONG to VWAP.
# Shadow-only for ~3 weeks; set False to go live after validation.
VWAP_REVERSION_SHADOW_ONLY  = True
VWAP_REVERSION_MIN_DIST_PCT = 1.0   # price must be ≥1% away from VWAP ("extended")
VWAP_REVERSION_MAX_DIST_PCT = 8.0   # >8% = likely breakout, skip
VWAP_REVERSION_VOL_MULT     = 1.3   # reversal candle volume must be ≥1.3× 10-bar avg
VWAP_REVERSION_ATR_SL_MULT  = 0.8   # SL = 0.8 × 1h-ATR (tighter than streak's 4h-based)
VWAP_REVERSION_ATR_TP_MULT  = 1.6   # TP dist = 1.6 × SL dist  ≈ R:R 2:1
VWAP_REVERSION_COOLDOWN     = 28800  # 8h per symbol (same as streak_1h)

# --- Bollinger Squeeze shadow (volatility squeeze → breakout) ---
# Backtest (10 pairs × 1000 1h candles): WR=37.5% (LONG 40.3%, SHORT 35.0%), ΣPnL=+7%.
# Break-even at R:R 2:1 = 33.3%. Shadow-only for ~3 weeks.
BB_SQUEEZE_SHADOW_ONLY  = True    # set False to go live after validation
BB_SQUEEZE_PERIOD       = 20      # Bollinger Band length (standard)
BB_SQUEEZE_MULT         = 2.0     # BB standard deviation multiplier
BB_SQUEEZE_PCT10_WIN    = 100     # lookback window for bottom-10% squeeze percentile
BB_SQUEEZE_VOL_MULT     = 1.3     # breakout candle volume must be ≥1.3× 10-bar avg
BB_SQUEEZE_ATR_SL_MULT  = 1.0    # SL = 1.0 × 1h-ATR
BB_SQUEEZE_ATR_TP_MULT  = 2.0    # TP dist = 2.0 × SL dist  (R:R 2:1)
BB_SQUEEZE_COOLDOWN     = 28800   # 8h per symbol

# --- EMA Crossover shadow (9/21, 4h timeframe, gap threshold) ---
# 1h attempted but EMA(9/21) gaps max at 0.22% on 1h — threshold 0.3-0.5% unreachable.
# Switched to 4h: gap p75=0.117%, threshold 0.15% covers best-performing tier.
# Backtest (10 pairs × 500 4h candles, thresh≥0.15%): WR=36.8%, ΣPnL=+11.4%, avg=+0.30%.
EMA_CROSS_SHADOW_ONLY   = True    # set False to go live after validation
EMA_CROSS_FAST          = 9       # fast EMA period
EMA_CROSS_SLOW          = 21      # slow EMA period
EMA_CROSS_GAP_PCT       = 0.15    # min |EMA9-EMA21|/price at crossover (4h-realistic %)
EMA_CROSS_ATR_SL_MULT   = 1.0    # SL = 1.0 × 4h-ATR
EMA_CROSS_ATR_TP_MULT   = 2.0    # TP = 2.0 × SL dist  (R:R 2:1)
EMA_CROSS_COOLDOWN      = 21600   # 6h per symbol

# --- Shadow signal Telegram notification daily limits ---
# Each key maps an alert_type (or "confluence_cap") to the max notifications per day.
# 0 = don't send.  Designed to sum to ~20 signals/day in realistic steady-state.
SHADOW_NOTIF_LIMITS: dict[str, int] = {
    "bb_squeeze":      10,  # 8h cooldown/symbol; burst on first deploy — hard cap
    "ema_cross":       10,  # 4h + gap filter → rare; allow up to 10
    "pump_24h_fade":   8,   # ~5/day natural rate
    "vwap_reversion":  8,   # ~1/day → allow all
    "liq_reversal":    8,   # rarely fires → allow all
    "confluence_cap":  8,   # cap-blocked confluence (прошли всё, не хватило слота)
    "confluence":      4,   # liq_veto / pump_filter vetoed confluence
    "overheated_24h":  4,   # liq_veto / ai_veto for overheated
    "oversold_24h":    4,   # liq_veto for oversold
}

# --- Whale LSR shift (top traders' L/S ratio flips between cycles) ---
LSR_SHIFT_THRESHOLD  = 0.38    # min |lsr_new - lsr_prev| to count as a flip
LSR_BULL_MIN         = 1.30    # after shift, LSR must be ≥ this (whales in long)
LSR_BEAR_MAX         = 0.72    # after shift, LSR must be ≤ this (whales in short)
LSR_SHIFT_COOLDOWN   = 14400   # 4h cooldown per symbol
LSR_MONITOR_TOP_N    = 40      # check LSR for top-N by volume per cycle

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


def _coin_trend_label(symbol: str) -> str:
    """Return a one-line coin trend summary based on yesterday's close-to-close %
    change (cached in state["prior_day_pct"] during the hourly refresh).

    Examples:
      📊 Тренд монеты (вчера): ↗️ +48.9%   ← strong up-day before this signal
      📊 Тренд монеты (вчера): ↘️ -12.3%
      📊 Тренд монеты (вчера): ➡️ +0.8%
    """
    with state_lock:
        pct = state["prior_day_pct"].get(symbol)
    if pct is None:
        return ""
    if pct >= 5:
        arrow = "↗️"
    elif pct <= -5:
        arrow = "↘️"
    else:
        arrow = "➡️"
    sign = "+" if pct >= 0 else ""
    return f"\n📊 Тренд монеты (вчера): {arrow} {sign}{pct:.1f}%"


def _build_alert_buttons(
    alert_id: int,
    symbol: str,
    alert_type: str,
    direction: str | None = None,
    entry_price: float | None = None,
    sl_price: float | None = None,
) -> dict:
    rows: list[list[dict]] = [
        [
            {"text": "↑ сработал", "callback_data": f"fb:{alert_id}:1"},
            {"text": "↓ не сработал", "callback_data": f"fb:{alert_id}:-1"},
        ],
        [
            {"text": "🤖 Спросить AI", "callback_data": f"ai_ask:{alert_id}"},
            {"text": "📈 Лонг", "callback_data": f"ap:{symbol}:LONG"},
            {"text": "📉 Шорт", "callback_data": f"ap:{symbol}:SHORT"},
        ],
        [
            {"text": "Скрыть пару", "callback_data": f"hs:{symbol}"},
            {"text": "Скрыть тип", "callback_data": f"ht:{alert_type}"},
        ],
    ]
    # Entry-confirmation button — only for LONG/SHORT signals with a valid SL.
    # Format: ew:{alert_id}:{symbol}:{direction}:{entry_price:.6g}:{sl_price:.6g}:{alert_type}
    # All fields embedded in callback_data so the handler works across dev/deployed DB split.
    if direction and entry_price and sl_price and entry_price > 0 and sl_price > 0:
        _atype_cb = (alert_type or "?").replace(":", "_")
        _ew_cb = (
            f"ew:{alert_id}:{symbol}:{direction}"
            f":{entry_price:.6g}:{sl_price:.6g}:{_atype_cb}"
        )
        if len(_ew_cb.encode()) <= 64:
            rows.insert(0, [
                {"text": "🎯 Подтвердить точку входа", "callback_data": _ew_cb}
            ])
    return {"inline_keyboard": rows}


def _build_voted_buttons(alert_id: int, symbol: str, vote: int) -> dict:
    mark = "✓ Сработал" if vote > 0 else "✓ Не сработал"
    return {
        "inline_keyboard": [
            [{"text": mark, "callback_data": f"noop:{alert_id}"}],
        ]
    }


def _get_signal_edge_label(alert_type: str, recommendation: str) -> str | None:
    """Query 30-day historical performance for this (alert_type, recommendation).

    Checks 2d, 3d, 7d horizons. If the best horizon has avg aligned return ≥ 2%
    with ≥ 5 follow-ups, returns a formatted recommendation label.
    Returns None when data is insufficient or edge is below the threshold.
    """
    side = "buy" if recommendation == "LONG" else "sell"
    since = int(time.time()) - 30 * 86400
    settled_before = int(time.time()) - 5 * 60
    try:
        with _db_lock:
            conn = _get_db()
            rows = conn.execute(
                "SELECT price_at_alert, price_2d, price_3d, price_7d "
                "FROM alerts "
                "WHERE alert_type=? AND recommendation=? AND ts >= ? AND ts <= ?",
                (alert_type, recommendation, since, settled_before),
            ).fetchall()
    except Exception as e:
        logger.debug("_get_signal_edge_label query failed: %s", e)
        return None

    if not rows:
        return None

    bucket: dict[str, list[float]] = {"2d": [], "3d": [], "7d": []}
    for p0, p2d, p3d, p7d in rows:
        if not p0 or p0 <= 0:
            continue
        for key, pf in (("2d", p2d), ("3d", p3d), ("7d", p7d)):
            if pf and pf > 0:
                change = (pf - p0) / p0
                aligned = change if side == "buy" else -change
                bucket[key].append(aligned)

    # Pick the best horizon: highest avg return with ≥ 5 completed samples
    best_horizon: str | None = None
    best_avg = 0.0
    for h, vals in bucket.items():
        if len(vals) < 5:
            continue
        avg = sum(vals) / len(vals)
        if avg > best_avg:
            best_avg = avg
            best_horizon = h

    if best_horizon is None or best_avg < 0.02:
        return None

    vals = bucket[best_horizon]
    wins = sum(1 for v in vals if v > 0)
    win_rate = wins / len(vals)
    h_label = {"2d": "2 дня", "3d": "3 дня", "7d": "7 дней"}[best_horizon]

    if best_avg >= 0.10:
        badge = "🔥 Очень сильный сигнал"
    elif best_avg >= 0.05:
        badge = "💪 Сильный сигнал"
    else:
        badge = "📈 Умеренный сигнал"

    if side == "sell":
        action = "Держите SHORT"
        move_str = f"после сигнала монета падала в среднем на <b>{best_avg * 100:.1f}%</b>"
    else:
        action = "Держите LONG"
        move_str = f"после сигнала монета росла в среднем на <b>+{best_avg * 100:.1f}%</b>"
    return (
        f"\n\n{badge}\n"
        f"📊 По статистике 30 дней: {action} — "
        f"{move_str} за {h_label}\n"
        f"Win-rate: <b>{win_rate * 100:.0f}%</b> · Выборка: <b>{len(vals)} сигнала</b>"
    )


def send_alert_with_log(
    symbol: str,
    alert_type: str,
    recommendation: str | None,
    price: float | None,
    body_text: str,
    score: int | None = None,
    rsi: float | None = None,
    pct24: float | None = None,
    factor_funding_pts: int = 0,
    factor_lsr_pts: int = 0,
    sl_price: float | None = None,
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
    if SHADOW_ONLY_MODE and alert_type not in SHADOW_MODE_EXEMPT_TYPES:
        logger.debug("SHADOW_ONLY_MODE: suppressing real signal %s/%s", symbol, alert_type)
        return (False, None)
    # Don't send a new signal if the same symbol already has an open real position.
    # Prevents duplicate alerts for the same coin until the current trade closes.
    try:
        with _db_lock:
            _already_open = _get_db().execute(
                "SELECT COUNT(*) FROM demo_positions "
                "WHERE symbol=? AND status='open' AND is_shadow=0",
                (symbol,),
            ).fetchone()[0]
        if _already_open:
            logger.info(
                "Suppressed %s/%s — already has an open real position", symbol, alert_type
            )
            return (False, None)
    except Exception as _chk_exc:
        logger.warning(
            "open-position check failed for %s/%s — allowing signal through: %s",
            symbol, alert_type, _chk_exc,
        )
    # Only directional signals — drop NEUTRAL/WATCH so the channel stays
    # actionable. New-listing alerts use a separate sender and are unaffected.
    if recommendation not in ("LONG", "SHORT"):
        logger.info(
            "Suppressed %s/%s (recommendation=%r, only LONG/SHORT delivered)",
            symbol, alert_type, recommendation,
        )
        return (False, None)
    # Score gate — per-type minimum (LONG signals use their own window).
    # Backtest: for LONG, score is inversely correlated with outcome — the
    # profitable zone is 55–65; above 65 quality degrades sharply.
    if recommendation == "LONG" and alert_type in MIN_SCORE_LONG_BY_TYPE:
        min_score = MIN_SCORE_LONG_BY_TYPE[alert_type]
    else:
        min_score = MIN_SCORE_BY_TYPE.get(alert_type, MIN_ALERT_SCORE)
    if score is None or score < min_score:
        logger.info(
            "Suppressed %s/%s (score=%s < min %d)",
            symbol, alert_type, score, min_score,
        )
        return (False, None)
    # BULL market penalty for counter-trend SHORTs: crypto trends up by default,
    # shorting against a BULL regime needs stronger conviction than the type floor.
    if recommendation == "SHORT" and score < SHORT_BULL_REGIME_MIN_SCORE:
        try:
            if get_market_regime() == "BULL":
                logger.info(
                    "Suppressed %s/%s SHORT (BULL regime, score=%s < %d)",
                    symbol, alert_type, score, SHORT_BULL_REGIME_MIN_SCORE,
                )
                return (False, None)
        except Exception:
            pass  # regime unavailable — let signal through
    # LONG score ceiling — above the ceiling quality degrades (backtest-driven).
    if recommendation == "LONG":
        max_score_long = MAX_SCORE_LONG_BY_TYPE.get(alert_type)
        if max_score_long is not None and score > max_score_long:
            logger.info(
                "Suppressed %s/%s LONG (score=%s > max %d — outside optimal zone 55–65)",
                symbol, alert_type, score, max_score_long,
            )
            return (False, None)
    # TOP marker: strongest signals get a visible header so the user can
    # treat them as a separate, higher-conviction stream.
    if score is not None and score >= TOP_SIGNAL_SCORE:
        body_text = "🏆 <b>ТОП СИГНАЛ</b>\n" + body_text
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
                "price_at_alert, score, factor_funding_pts, factor_lsr_pts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (int(time.time()), symbol, alert_type, recommendation,
                 float(price), score, factor_funding_pts, factor_lsr_pts),
            )
            conn.commit()
            alert_id = int(cur.lastrowid or 0)
    except Exception as e:
        logger.warning("send_alert_with_log insert failed: %s", e)
        # Fall back to send-without-buttons. Still consume cooldowns on success
        # so we don't repeatedly retry a broken DB path every cycle.
        ok = _telegram_send(TELEGRAM_CHAT_ID, body_text)
        return (ok, None)

    # Derive SL for the entry-confirmation button when not supplied by caller.
    # Uses the same ATR-based formula as demo positions so the SL is consistent
    # with what was shown in the signal message body.
    if sl_price is None and price and price > 0 and recommendation in ("LONG", "SHORT"):
        try:
            with state_lock:
                _ew_atr = state["atr_4h"].get(symbol)
            _sl_derived, _ = _compute_demo_sl_tp(recommendation, price, _ew_atr)
            sl_price = _sl_derived
        except Exception:
            pass

    markup = _build_alert_buttons(
        alert_id, symbol, alert_type,
        direction=recommendation,
        entry_price=price,
        sl_price=sl_price,
    )
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
    # Coin trend: show yesterday's % change so the trader sees if the move is
    # part of a multi-day trend or a single-day spike.
    coin_trend = _coin_trend_label(symbol)
    if coin_trend:
        body_text = f"{body_text}{coin_trend}"
    # C. Historical edge label — appended when 30-day stats show ≥2% avg return
    #    in the signal's direction at any 2d/3d/7d horizon with ≥5 samples.
    edge_label = _get_signal_edge_label(alert_type, recommendation)
    if edge_label:
        body_text = body_text + edge_label
    if not _telegram_send(TELEGRAM_CHAT_ID, body_text, reply_markup=markup):
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
    # AI commentary — sent as a follow-up message so the main signal is never delayed
    if _GEMINI_AI_COMMENTARY:
        def _send_ai_comment(sym=symbol, rec=recommendation, at=alert_type, sc=score,
                             rsi_=rsi, pct24_=pct24):
            try:
                comment = _ai_signal_commentary(sym, rec, at, sc, rsi=rsi_, pct24=pct24_)
                if comment:
                    _telegram_send(TELEGRAM_CHAT_ID, f"<code>{sym}</code>{comment}")
            except Exception as exc:
                logger.warning("AI commentary failed for %s: %s", sym, _safe_err(exc))
        threading.Thread(target=_send_ai_comment, daemon=True).start()
    return (True, alert_id)


def handle_callback_query(cb: dict) -> None:
    """Dispatch inline-button presses: feedback, hide-type, hide-symbol."""
    cb_id = cb.get("id")
    data = (cb.get("data") or "").strip()
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    message_id = msg.get("message_id")
    logger.info("callback_query received: id=%s data=%r chat_id=%s", cb_id, data, chat_id)
    if not cb_id or not data:
        logger.warning("callback_query dropped: cb_id=%r data=%r", cb_id, data)
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
            # Answer FIRST so Telegram spinner stops immediately
            _telegram_answer_callback(cb_id, "Спасибо! Учтено")
            record_feedback(aid, vote)
            # Look up symbol to update the keyboard
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
        elif kind == "ai_ask":
            # Answer the callback IMMEDIATELY — before any slow work —
            # so Telegram stops the spinner within the 10-second window.
            _telegram_answer_callback(cb_id, "🤖 Готов к вопросу")
            logger.info("ai_ask callback: data=%s chat_id=%s", data, chat_id)
            try:
                aid = int(rest)
            except ValueError:
                _telegram_send(chat_id, "❌ Некорректный ID сигнала.")
                return
            # Fetch signal context from DB (may block briefly if check cycle running)
            sig_row = None
            try:
                with _db_lock:
                    conn = _get_db()
                    sig_row = conn.execute(
                        "SELECT symbol, recommendation, alert_type, score FROM alerts WHERE id=?",
                        (aid,)
                    ).fetchone()
            except Exception as db_exc:
                logger.warning("ai_ask DB lookup failed: %s", db_exc)
            if not sig_row:
                _telegram_send(chat_id, "❌ Сигнал не найден в базе.")
                return
            sym, rec, atype, score = sig_row
            with _ai_ask_pending_lock:
                _ai_ask_pending[chat_id] = {
                    "alert_id": aid, "symbol": sym,
                    "rec": rec, "alert_type": atype,
                    "score": score, "ts": time.time(),
                }
            _telegram_send(
                chat_id,
                f"🤖 <b>Задай вопрос по сигналу <code>{sym}</code> ({rec})</b>\n"
                f"Например: «Стоит ли входить сейчас?» или «Какие риски?»\n\n"
                f"Просто напиши сообщение — отвечу через несколько секунд."
            )
            logger.info("ai_ask pending set for chat_id=%s symbol=%s", chat_id, sym)
        elif kind == "ap":
            # "ap:SYMBOL:LONG" or "ap:SYMBOL:SHORT" — add personal position via button
            sym, _, direction = rest.partition(":")
            if sym and direction in ("LONG", "SHORT"):
                now = int(time.time())
                try:
                    with _db_lock:
                        db = _get_db()
                        existing = db.execute(
                            "SELECT id FROM user_positions WHERE symbol=? AND status='open'",
                            (sym,)
                        ).fetchone()
                        if existing:
                            db.execute(
                                "UPDATE user_positions SET direction=?, ts_added=?, last_alerted=0 WHERE id=?",
                                (direction, now, existing[0]),
                            )
                            action = "обновлена"
                        else:
                            db.execute(
                                "INSERT INTO user_positions (symbol, direction, note, ts_added) VALUES (?,?,NULL,?)",
                                (sym, direction, now),
                            )
                            action = "добавлена"
                        db.commit()
                    dir_emoji = "📈" if direction == "LONG" else "📉"
                    _telegram_answer_callback(
                        cb_id,
                        f"{dir_emoji} {sym} {direction} {action} в трекер",
                    )
                    logger.info("UserPos via button: %s %s (%s)", sym, direction, action)
                except Exception as db_exc:
                    logger.error("ap callback DB error: %s", db_exc)
                    _telegram_answer_callback(cb_id, "❌ Ошибка базы данных")
            else:
                _telegram_answer_callback(cb_id)
        elif kind == "unmute_all":
            # Clear all hidden types/symbols AND silenced state
            try:
                with _db_lock:
                    conn = _get_db()
                    conn.execute("DELETE FROM hidden_items")
                    conn.commit()
                with _prefs_lock:
                    _hidden_types.clear()
                    _hidden_symbols.clear()
                with state_lock:
                    state["silenced"] = False
                    state["silenced_at"] = None
                _telegram_answer_callback(cb_id, "✅ Все заглушки сняты")
                logger.info("unmute_all via button from chat_id=%s", chat_id)
                if chat_id:
                    _telegram_send(
                        chat_id,
                        "🔔 <b>Всё разглушено</b>\n"
                        "Все скрытые типы и монеты восстановлены. Сигналы снова активны.",
                    )
            except Exception as _e:
                logger.error("unmute_all callback failed: %s", _e)
                _telegram_answer_callback(cb_id, "❌ Ошибка")
        elif kind == "watch":
            # Format: watch:{demo_id}:{symbol}:{direction}:{entry_price}:{alert_type}
            # All fields are embedded in callback_data so this handler works
            # regardless of which bot instance (dev/deployed) processes it.
            parts_w = rest.split(":", 4)
            if len(parts_w) != 5:
                _telegram_answer_callback(cb_id, "❌ Некорректные данные кнопки")
                return
            _w_demo_id_str, _w_sym, _w_dir, _w_entry_str, _w_atype = parts_w
            try:
                _w_demo_id = int(_w_demo_id_str)
                _w_entry   = float(_w_entry_str)
            except ValueError:
                _telegram_answer_callback(cb_id, "❌ Некорректные данные кнопки")
                return
            _telegram_answer_callback(cb_id, "✅ Слежу за разворотом 48ч")
            now_w = int(time.time())
            try:
                with _db_lock:
                    db = _get_db()
                    # INSERT OR IGNORE + rowcount — DB-level duplicate guard.
                    # The partial UNIQUE INDEX ux_watchlist_active blocks concurrent
                    # double-taps at the DB level; no separate SELECT needed.
                    _ins = db.execute(
                        "INSERT OR IGNORE INTO watchlist "
                        "(source_demo_id, symbol, direction, alert_type, "
                        " entry_price, added_ts, expires_ts) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (_w_demo_id, _w_sym, _w_dir, _w_atype or "unknown",
                         _w_entry, now_w, now_w + 48 * 3600),
                    )
                    db.commit()
                    _inserted = _ins.rowcount > 0
                if _inserted:
                    logger.info(
                        "Watchlist added: %s %s demo_id=%d", _w_sym, _w_dir, _w_demo_id
                    )
                    if chat_id:
                        _telegram_send(
                            chat_id,
                            f"🔔 <b>Наблюдение добавлено</b>\n"
                            f"<code>{_w_sym}</code> {_w_dir} ({_w_atype or '?'})\n"
                            f"Жду подтверждения разворота — авто-удаление через 48ч\n"
                            f"Отмена: <code>/unwatch {_w_sym}</code>",
                        )
                else:
                    logger.info(
                        "Watchlist duplicate blocked by DB: %s %s demo_id=%d",
                        _w_sym, _w_dir, _w_demo_id,
                    )
                    if chat_id:
                        # Rare path — safe to do one extra SELECT for UX detail
                        try:
                            with _db_lock:
                                _ex = _get_db().execute(
                                    "SELECT alert_type FROM watchlist "
                                    "WHERE symbol=? AND direction=? AND status='active'",
                                    (_w_sym, _w_dir),
                                ).fetchone()
                            _ex_atype = (_ex[0] if _ex else None) or "?"
                        except Exception:
                            _ex_atype = "?"
                        _telegram_send(
                            chat_id,
                            f"🔔 <code>{_w_sym}</code> {_w_dir} уже в активном наблюдении"
                            f" (сигнал: {_ex_atype})",
                        )
            except Exception as w_exc:
                logger.error("watch callback failed: %s", w_exc)
                if chat_id:
                    _telegram_send(chat_id, "❌ Ошибка базы данных")
        elif kind == "ew":
            # Format: ew:{alert_id}:{symbol}:{direction}:{entry_price}:{sl_price}:{alert_type}
            # All fields embedded in callback_data so this handler works regardless
            # of which bot instance (dev/deployed) processes it.
            parts_ew = rest.split(":", 5)
            if len(parts_ew) != 6:
                _telegram_answer_callback(cb_id, "❌ Некорректные данные кнопки")
                return
            _ew_aid_str, _ew_sym, _ew_dir, _ew_entry_str, _ew_sl_str, _ew_atype = parts_ew
            try:
                _ew_aid   = int(_ew_aid_str)
                _ew_entry = float(_ew_entry_str)
                _ew_sl    = float(_ew_sl_str)
            except ValueError:
                _telegram_answer_callback(cb_id, "❌ Некорректные данные кнопки")
                return
            _telegram_answer_callback(cb_id, "🎯 Слежу за точкой входа 48ч")
            now_ew = int(time.time())
            try:
                with _db_lock:
                    db = _get_db()
                    # INSERT OR IGNORE + rowcount — unique index ux_ew_alert prevents
                    # double-tapping the button on the same alert at the DB level.
                    _ins_ew = db.execute(
                        "INSERT OR IGNORE INTO entry_watch "
                        "(source_alert_id, symbol, direction, alert_type, "
                        " entry_price, sl_price, added_ts, expires_ts) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (_ew_aid, _ew_sym, _ew_dir, _ew_atype or "unknown",
                         _ew_entry, _ew_sl, now_ew, now_ew + 48 * 3600),
                    )
                    db.commit()
                    _ew_inserted = _ins_ew.rowcount > 0
                if _ew_inserted:
                    logger.info(
                        "EntryWatch added: %s %s alert_id=%d", _ew_sym, _ew_dir, _ew_aid
                    )
                    if chat_id:
                        _telegram_send(
                            chat_id,
                            f"🎯 <b>Наблюдение за точкой входа добавлено</b>\n"
                            f"<code>{_ew_sym}</code> {_ew_dir} ({_ew_atype or '?'})\n"
                            f"📍 Цена входа: <b>${_ew_entry:,.6g}</b>\n"
                            f"🛑 SL: <b>${_ew_sl:,.6g}</b>\n"
                            f"Жду оптимальный момент — авто-удаление через 48ч",
                        )
                else:
                    if chat_id:
                        _telegram_send(
                            chat_id,
                            f"🎯 <code>{_ew_sym}</code> {_ew_dir} уже в наблюдении за входом",
                        )
            except Exception as ew_exc:
                logger.error("ew callback failed: %s", ew_exc)
                if chat_id:
                    _telegram_send(chat_id, "❌ Ошибка базы данных")
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
# Intraday helpers
# ---------------------------------------------------------------------------

def _fetch_1h_closes(symbol: str, limit: int = 14) -> list[float] | None:
    """Return the last `limit-1` completed 1h closes (oldest→newest), skipping
    the current (potentially incomplete) candle.  Returns None on error."""
    try:
        candles = _gateio_klines(symbol, "1h", limit)
        if not candles or len(candles) < 8:
            return None
        return [float(k[4]) for k in candles[:-1]]   # drop last (live) candle
    except Exception:
        return None


def _fetch_1h_ohlcv(symbol: str, limit: int = 25) -> list | None:
    """Return the last limit-1 completed 1h candles (oldest→newest), Gate.io format.
    Drops the current (potentially incomplete) candle."""
    try:
        candles = _gateio_klines(symbol, "1h", limit)
        if not candles or len(candles) < 8:
            return None
        return candles[:-1]
    except Exception:
        return None


def _calc_vwap_atr1h(candles: list) -> tuple[float | None, float | None]:
    """Compute daily VWAP and average 1h-ATR from completed 1h candles.
    Gate.io candle format: [ts, open, high, low, close, vol, ...]
    Returns (vwap, atr_1h) or (None, None) on any error."""
    try:
        if len(candles) < 2:
            return None, None
        tp_vol   = 0.0
        vol_total = 0.0
        trs: list[float] = []
        for i, c in enumerate(candles):
            h, l, cl, vol = float(c[2]), float(c[3]), float(c[4]), float(c[5])
            tp_vol    += (h + l + cl) / 3.0 * vol
            vol_total += vol
            if i > 0:
                prev_c = float(candles[i - 1][4])
                trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
        vwap = tp_vol / vol_total if vol_total > 0 else None
        atr  = sum(trs) / len(trs) if trs else None
        return vwap, atr
    except Exception:
        return None, None


def _fetch_4h_ohlcv(symbol: str, limit: int = 60) -> list | None:
    """Return the last limit-1 completed 4h candles (oldest→newest), Gate.io format."""
    try:
        candles = _gateio_klines(symbol, "4h", limit)
        if not candles or len(candles) < 10:
            return None
        return candles[:-1]  # drop current incomplete candle
    except Exception:
        return None


def _calc_bollinger(
    closes: list[float],
    period: int = 20,
    mult: float = 2.0,
) -> tuple[list, list, list]:
    """Compute Bollinger Bands.  Returns (upper, lower, width_pct) lists — None where
    insufficient history.  width_pct = (upper-lower)/middle × 100."""
    n = len(closes)
    upper: list = [None] * n
    lower: list = [None] * n
    width: list = [None] * n
    for i in range(period - 1, n):
        window = closes[i - period + 1 : i + 1]
        sma = sum(window) / period
        variance = sum((x - sma) ** 2 for x in window) / period
        std = variance ** 0.5
        u = sma + mult * std
        l = sma - mult * std
        upper[i] = u
        lower[i] = l
        width[i]  = (u - l) / sma * 100.0 if sma else None
    return upper, lower, width


def _calc_ema_arr(prices: list[float], period: int) -> list:
    """Exponential moving average — returns list of float|None, same length as prices."""
    k = 2.0 / (period + 1)
    result: list = [None] * len(prices)
    for i in range(period - 1, len(prices)):
        if result[i - 1] is None:
            result[i] = sum(prices[i - period + 1 : i + 1]) / period
        else:
            result[i] = prices[i] * k + result[i - 1] * (1 - k)
    return result


def _fetch_gate_lsr_raw(symbol: str) -> float | None:
    """Return the raw top_lsr_size from Gate.io /contract_stats (1h, 1 row).
    Returns None on any error or if the contract isn't listed."""
    try:
        resp = _gateio_get(
            "/contract_stats",
            params={"contract": _to_gate(symbol), "interval": "1h", "limit": 1},
            timeout=5,
        )
        data = resp.json()
        if not data:
            return None
        lsr = float(data[0].get("top_lsr_size") or 0)
        return lsr if lsr > 0 else None
    except Exception:
        return None


def check_intraday_streak(
    tickers: dict[str, dict],
    rsi_map: dict[str, float],
) -> int:
    """Send LONG/SHORT alerts when a coin has 6+ consecutive green/red 1h candles.

    Pre-filter: only fetch 1h data for coins that already show ±STREAK_1H_PRE_FILTER%
    on the 24h ticker (reduces API calls from ~500 to ~50).

    LONG  → 6+ consecutive green hours, RSI < STREAK_1H_RSI_MAX_LONG
    SHORT → 6+ consecutive red   hours, RSI > STREAK_1H_RSI_MIN_SHORT
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
        try:
            pct24   = float(t["priceChangePercent"])
            price   = float(t["lastPrice"])
            vol24   = float(t["quoteVolume"])
        except (ValueError, KeyError):
            continue

        if vol24 < MIN_VOLUME_USDT:
            continue

        # Fast pre-filter: coin must already be showing movement
        if abs(pct24) < STREAK_1H_PRE_FILTER:
            continue

        # Cooldown
        with state_lock:
            last = state["last_streak_1h_alerted"].get(symbol, 0)
        if now - last < STREAK_1H_COOLDOWN:
            continue

        rsi = rsi_map.get(symbol)

        # Fetch 1h candles
        closes = _fetch_1h_closes(symbol, limit=14)
        if not closes or len(closes) < STREAK_1H_MIN + 1:
            continue

        streak_up   = _count_consecutive_trend_days(closes, "up")
        streak_down = _count_consecutive_trend_days(closes, "down")

        if streak_up >= STREAK_1H_MIN and pct24 > 0:
            # LONG signal — sustained uptrend
            if rsi is not None and rsi >= STREAK_1H_RSI_MAX_LONG:
                continue  # already overbought, skip

            # Cumulative gain over streak window
            streak_start_price = closes[-(streak_up + 1)]
            streak_gain = (closes[-1] - streak_start_price) / streak_start_price * 100

            # Перегрев: монета уже слишком сильно выросла — ловить хвост поздно
            if streak_gain > STREAK_1H_LONG_MAX_GAIN:
                logger.debug(
                    "Streak1h LONG %s skipped: streak_gain=%.1f%% > max %.1f%%",
                    symbol, streak_gain, STREAK_1H_LONG_MAX_GAIN,
                )
                continue

            # Разворот: живая цена уже ниже последней закрытой свечи → тренд кончился
            last_close = closes[-1]
            reversal_drop = (last_close - price) / last_close * 100
            if reversal_drop > STREAK_1H_REVERSAL_PCT:
                logger.info(
                    "Streak1h LONG %s skipped: price=%.6g already %.2f%% below last_close=%.6g",
                    symbol, price, reversal_drop, last_close,
                )
                continue

            with state_lock:
                atr = state["atr_4h"].get(symbol)
                ema = state["ema200_4h"].get(symbol)

            above_ema = (ema is not None and price > ema)
            whale = _get_gate_whale_stats(symbol)

            score = compute_signal_score(
                "streak_1h", "buy",
                rsi=rsi, above_ema=above_ema, pct24=pct24, btc_pct24=btc_pct24,
            )
            # Funding + LSR adjustment
            _fl_delta_sl, _fl_text_sl, _fl_fpts_sl, _fl_lpts_sl = (
                _funding_lsr_score_and_text(symbol, "buy")
            )
            score = max(0, min(100, score + _fl_delta_sl))
            sl_tp = _format_sl_tp("buy", price, atr, score=score)
            ema_note = "📍 Выше EMA-200 ✅" if above_ema else "📍 Ниже EMA-200 ⚠️"
            rsi_str  = f"{rsi:.1f}" if rsi is not None else "н/д"
            body = (
                f"📈 <b>ВОСХОДЯЩИЙ ТРЕНД {streak_up}ч — ранний ЛОНГ</b>\n"
                f"Монета растёт <b>{streak_up} часов подряд</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"<code>{symbol}</code>\n"
                f"⏱ Рост за {streak_up}ч: <b>+{streak_gain:.1f}%</b>\n"
                f"📈 24ч: <b>{pct24:+.1f}%</b>\n"
                f"🧊 RSI: <b>{rsi_str}</b>\n"
                f"💰 Цена: <b>${price:,.6g}</b>\n"
                f"{ema_note}\n"
                f"🎯 Сила сигнала: <b>{score}/100</b> ({_strength_label(score)})"
            )
            if sl_tp:
                body += f"\n{sl_tp}"
            if _fl_text_sl:
                body += f"\n{_fl_text_sl}"
            if whale:
                body += f"\n{whale}"

            delivered, _aid = send_alert_with_log(
                symbol, "streak_1h", "LONG", price, body, score,
                rsi=rsi, pct24=pct24,
                factor_funding_pts=_fl_fpts_sl, factor_lsr_pts=_fl_lpts_sl,
            )
            if delivered:
                with state_lock:
                    state["last_streak_1h_alerted"][symbol] = now
                logger.info(
                    "Streak1h LONG: %s streak=%dh gain=%.1f%% rsi=%s",
                    symbol, streak_up, streak_gain, rsi_str,
                )
                sent += 1
                # Track real demo position so streak signals join the /demo stats
                _dsl_st, _dtp_st = _compute_demo_sl_tp("LONG", price, atr)
                if _dsl_st and _dtp_st:
                    _demo_open_position(
                        symbol, "LONG", price, _dsl_st, _dtp_st,
                        is_shadow=False,
                        alert_type="streak_1h",
                        score=score,
                    )

        elif streak_down >= STREAK_1H_MIN and pct24 < 0:
            # SHORT signal — sustained downtrend
            if rsi is not None and rsi <= STREAK_1H_RSI_MIN_SHORT:
                continue  # already oversold, skip

            streak_start_price = closes[-(streak_down + 1)]
            streak_loss = (closes[-1] - streak_start_price) / streak_start_price * 100

            # Перепроданность: монета уже слишком сильно упала — ловить хвост шорта поздно
            if abs(streak_loss) > STREAK_1H_SHORT_MAX_LOSS:
                logger.debug(
                    "Streak1h SHORT %s skipped: streak_loss=%.1f%% > max %.1f%%",
                    symbol, streak_loss, STREAK_1H_SHORT_MAX_LOSS,
                )
                continue

            # Разворот: живая цена уже выше последней закрытой свечи → тренд кончился
            last_close_s = closes[-1]
            reversal_pump = (price - last_close_s) / last_close_s * 100
            if reversal_pump > STREAK_1H_REVERSAL_PCT:
                logger.info(
                    "Streak1h SHORT %s skipped: price=%.6g already %.2f%% above last_close=%.6g",
                    symbol, price, reversal_pump, last_close_s,
                )
                continue

            with state_lock:
                atr = state["atr_4h"].get(symbol)
                ema = state["ema200_4h"].get(symbol)

            below_ema = (ema is not None and price < ema)
            whale = _get_gate_whale_stats(symbol)

            score = compute_signal_score(
                "streak_1h", "sell",
                rsi=rsi, above_ema=(not below_ema), pct24=pct24, btc_pct24=btc_pct24,
            )
            # Funding + LSR adjustment
            _fl_delta_ss, _fl_text_ss, _fl_fpts_ss, _fl_lpts_ss = (
                _funding_lsr_score_and_text(symbol, "sell")
            )
            score = max(0, min(100, score + _fl_delta_ss))
            sl_tp = _format_sl_tp("sell", price, atr, score=score)
            ema_note = "📍 Ниже EMA-200 ✅" if below_ema else "📍 Выше EMA-200 ⚠️"
            rsi_str  = f"{rsi:.1f}" if rsi is not None else "н/д"
            body = (
                f"📉 <b>НИСХОДЯЩИЙ ТРЕНД {streak_down}ч — ранний ШОРТ</b>\n"
                f"Монета падает <b>{streak_down} часов подряд</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"<code>{symbol}</code>\n"
                f"⏱ Падение за {streak_down}ч: <b>{streak_loss:.1f}%</b>\n"
                f"📉 24ч: <b>{pct24:+.1f}%</b>\n"
                f"🧊 RSI: <b>{rsi_str}</b>\n"
                f"💰 Цена: <b>${price:,.6g}</b>\n"
                f"{ema_note}\n"
                f"🎯 Сила сигнала: <b>{score}/100</b> ({_strength_label(score)})"
            )
            if sl_tp:
                body += f"\n{sl_tp}"
            if _fl_text_ss:
                body += f"\n{_fl_text_ss}"
            if whale:
                body += f"\n{whale}"

            delivered, _aid = send_alert_with_log(
                symbol, "streak_1h", "SHORT", price, body, score,
                rsi=rsi, pct24=pct24,
                factor_funding_pts=_fl_fpts_ss, factor_lsr_pts=_fl_lpts_ss,
            )
            if delivered:
                with state_lock:
                    state["last_streak_1h_alerted"][symbol] = now
                logger.info(
                    "Streak1h SHORT: %s streak=%dh loss=%.1f%% rsi=%s",
                    symbol, streak_down, streak_loss, rsi_str,
                )
                sent += 1
                # Track real demo position so streak signals join the /demo stats
                _dsl_st2, _dtp_st2 = _compute_demo_sl_tp("SHORT", price, atr)
                if _dsl_st2 and _dtp_st2:
                    _demo_open_position(
                        symbol, "SHORT", price, _dsl_st2, _dtp_st2,
                        is_shadow=False,
                        alert_type="streak_1h",
                        score=score,
                    )

    return sent


def check_vwap_reversion(
    tickers: dict[str, dict],
    rsi_map: dict[str, float],
) -> int:
    """Shadow: mean-reversion entry when a 1h streak leaves price extended from VWAP.

    Direction is OPPOSITE to streak_1h (mean-reversion, not momentum):
      upstreak  (≥6 consecutive green 1h candles) → price above VWAP → SHORT toward VWAP
      downstreak (≥6 consecutive red  1h candles) → price below VWAP → LONG  toward VWAP

    Volume confirmation: last completed 1h candle must point in the reversal direction
    with volume ≥ VWAP_REVERSION_VOL_MULT × 10-bar average.

    TP/SL use 1h-ATR (≈ ¼ of the 4h-ATR used by streak_1h), targeting a 1–4h hold.
    TP is set at the VWAP level (natural mean-reversion target) or R:R 2:1, whichever
    is closer to entry — so the position exits before the streak's original momentum
    could resume.

    Always opens as is_shadow=True for the first ~3 weeks (VWAP_REVERSION_SHADOW_ONLY).
    """
    if not tickers:
        return 0
    sent = 0
    now = time.time()

    for symbol, t in tickers.items():
        try:
            pct24 = float(t["priceChangePercent"])
            price = float(t["lastPrice"])
            vol24 = float(t["quoteVolume"])
        except (ValueError, KeyError):
            continue

        if vol24 < MIN_VOLUME_USDT:
            continue
        if abs(pct24) < STREAK_1H_PRE_FILTER:  # reuse streak pre-filter to save API calls
            continue

        with state_lock:
            last = state["last_vwap_rev_alerted"].get(symbol, 0)
        if now - last < VWAP_REVERSION_COOLDOWN:
            continue

        # Fetch full 1h OHLCV (need high/low/vol for VWAP; limit 25 = 24 completed candles)
        candles = _fetch_1h_ohlcv(symbol, limit=25)
        if not candles or len(candles) < STREAK_1H_MIN + 2:
            continue

        closes = [float(c[4]) for c in candles]
        streak_up   = _count_consecutive_trend_days(closes, "up")
        streak_down = _count_consecutive_trend_days(closes, "down")

        if streak_up < STREAK_1H_MIN and streak_down < STREAK_1H_MIN:
            continue

        # VWAP and 1h ATR from completed candles
        vwap, atr_1h = _calc_vwap_atr1h(candles)
        if not vwap or not atr_1h or atr_1h <= 0:
            continue

        # Mean-reversion direction: opposite of trend
        if streak_up >= STREAK_1H_MIN:
            if price <= vwap:
                continue  # price already at/below VWAP — no setup
            dist_pct  = (price - vwap) / vwap * 100.0
            direction = "SHORT"
        else:
            if price >= vwap:
                continue  # price already at/above VWAP — no setup
            dist_pct  = (vwap - price) / vwap * 100.0
            direction = "LONG"

        # Extension gate: too close = noise, too far = breakout not reversion
        if dist_pct < VWAP_REVERSION_MIN_DIST_PCT or dist_pct > VWAP_REVERSION_MAX_DIST_PCT:
            continue

        # Volume confirmation: last completed candle must be in the reversal direction
        last_c     = candles[-1]
        last_open  = float(last_c[1])
        last_close = float(last_c[4])
        last_vol   = float(last_c[5])
        avg_vol_10 = sum(float(c[5]) for c in candles[-10:]) / 10.0

        if direction == "SHORT" and last_close >= last_open:
            continue  # need bearish reversal candle (close < open)
        if direction == "LONG"  and last_close <= last_open:
            continue  # need bullish reversal candle (close > open)

        if avg_vol_10 > 0 and last_vol < avg_vol_10 * VWAP_REVERSION_VOL_MULT:
            continue  # volume not confirming the reversal

        # RSI sanity: don't short into extreme oversold, don't long into extreme overbought
        rsi = rsi_map.get(symbol)
        if direction == "SHORT" and rsi is not None and rsi < 35:
            continue
        if direction == "LONG"  and rsi is not None and rsi > 65:
            continue

        # TP/SL: use 1h ATR for a tight, 1–4h realistic hold
        sl_dist  = VWAP_REVERSION_ATR_SL_MULT * atr_1h
        tp_dist  = VWAP_REVERSION_ATR_TP_MULT * sl_dist   # R:R ≈ 2:1

        if direction == "SHORT":
            sl_price = price + sl_dist
            # TP targets VWAP; never go further than full tp_dist
            tp_price = max(vwap, price - tp_dist)
        else:
            sl_price = price - sl_dist
            tp_price = min(vwap, price + tp_dist)

        if sl_price <= 0 or tp_price <= 0:
            continue
        # Geometry sanity
        if direction == "LONG"  and not (sl_price < price < tp_price):
            continue
        if direction == "SHORT" and not (tp_price < price < sl_price):
            continue

        _demo_open_position(
            symbol, direction, price, sl_price, tp_price,
            is_shadow=True,
            alert_type="vwap_reversion",
            score=60,
            notify_body=(
                f"🔄 VWAP Reversion <b>{'LONG 📈' if direction == 'LONG' else 'SHORT 📉'}</b>"
                f" <code>{symbol}</code>\n"
                f"Цена: <b>${price:,.6g}</b>  |  Откл от VWAP: {dist_pct:.1f}%\n"
                f"🟢 TP: <b>${tp_price:,.6g}</b>  │  🔴 SL: <b>${sl_price:,.6g}</b>"
            ),
        )

        with state_lock:
            state["last_vwap_rev_alerted"][symbol] = now

        logger.info(
            "VWAP reversion shadow %s %s: dist=%.1f%% vwap=%.6g atr1h=%.6g sl=%.6g tp=%.6g",
            symbol, direction, dist_pct, vwap, atr_1h, sl_price, tp_price,
        )
        sent += 1

    return sent


def check_bollinger_squeeze(
    tickers: dict[str, dict],
    rsi_map: dict[str, float],
) -> int:
    """Shadow: detect Bollinger Band squeeze → breakout entry on 1h candles.

    Squeeze: current BB width (as % of middle) is in the bottom 10% of the last
    BB_SQUEEZE_PCT10_WIN widths.  Entry: the next candle CLOSES outside the bands
    with volume ≥ BB_SQUEEZE_VOL_MULT × 10-bar avg.

    LONG  = close above upper band after squeeze (bullish breakout)
    SHORT = close below lower band after squeeze (bearish breakdown)

    Backtest (10 pairs × 1000 1h candles): WR=37.5% vs 33.3% break-even at R:R 2:1.
    LONG WR=40.3%, SHORT WR=35.0%.  Shadow-only for ~3 weeks.
    """
    if not tickers:
        return 0
    sent = 0
    now = time.time()

    for symbol, t in tickers.items():
        try:
            pct24 = float(t["priceChangePercent"])
            price = float(t["lastPrice"])
            vol24 = float(t["quoteVolume"])
        except (ValueError, KeyError):
            continue

        if vol24 < MIN_VOLUME_USDT:
            continue

        with state_lock:
            last = state["last_bb_squeeze_alerted"].get(symbol, 0)
        if now - last < BB_SQUEEZE_COOLDOWN:
            continue

        candles = _fetch_1h_ohlcv(symbol, limit=BB_SQUEEZE_PCT10_WIN + 3)
        if not candles or len(candles) < BB_SQUEEZE_PERIOD + 10:
            continue

        closes  = [float(c[4]) for c in candles]
        volumes = [float(c[5]) for c in candles]
        upper, lower, width = _calc_bollinger(closes, BB_SQUEEZE_PERIOD, BB_SQUEEZE_MULT)
        _, atr_1h = _calc_vwap_atr1h(candles)

        if atr_1h is None or atr_1h <= 0:
            continue

        # Squeeze check on the SECOND-TO-LAST completed candle (i = n-2):
        # width[i] is the squeeze bar; width[i+1] is the breakout bar.
        n = len(closes)
        squeeze_idx = n - 2   # the squeeze candle
        break_idx   = n - 1   # the breakout candle (most recent completed)

        if width[squeeze_idx] is None or upper[squeeze_idx] is None:
            continue

        # Bottom-10% of last PCT10_WIN widths ending at squeeze_idx
        window_start = max(0, squeeze_idx - BB_SQUEEZE_PCT10_WIN + 1)
        recent_widths = [w for w in width[window_start:squeeze_idx] if w is not None]
        if len(recent_widths) < 30:
            continue
        p10 = sorted(recent_widths)[int(len(recent_widths) * 0.10)]
        if width[squeeze_idx] > p10:
            continue  # not in squeeze

        # Breakout candle check
        b_close = closes[break_idx]
        b_vol   = volumes[break_idx]
        avg_vol = sum(volumes[max(0, break_idx - 10) : break_idx]) / min(10, break_idx)

        if b_close > upper[squeeze_idx] and b_vol >= avg_vol * BB_SQUEEZE_VOL_MULT:
            direction = "LONG"
            sl_price  = b_close - BB_SQUEEZE_ATR_SL_MULT * atr_1h
            tp_price  = b_close + BB_SQUEEZE_ATR_TP_MULT * BB_SQUEEZE_ATR_SL_MULT * atr_1h
        elif b_close < lower[squeeze_idx] and b_vol >= avg_vol * BB_SQUEEZE_VOL_MULT:
            direction = "SHORT"
            sl_price  = b_close + BB_SQUEEZE_ATR_SL_MULT * atr_1h
            tp_price  = b_close - BB_SQUEEZE_ATR_TP_MULT * BB_SQUEEZE_ATR_SL_MULT * atr_1h
        else:
            continue

        if sl_price <= 0 or tp_price <= 0:
            continue
        if direction == "LONG"  and not (sl_price < b_close < tp_price):
            continue
        if direction == "SHORT" and not (tp_price < b_close < sl_price):
            continue

        # RSI sanity: avoid obvious counter-trend entries
        rsi = rsi_map.get(symbol)
        if direction == "LONG"  and rsi is not None and rsi > 80:
            continue  # overbought — squeeze already exhausted
        if direction == "SHORT" and rsi is not None and rsi < 20:
            continue  # oversold — bounce risk

        _demo_open_position(
            symbol, direction, b_close, sl_price, tp_price,
            is_shadow=True,
            alert_type="bb_squeeze",
            score=62,
            notify_body=(
                f"📊 BB Squeeze <b>{'LONG 📈' if direction == 'LONG' else 'SHORT 📉'}</b>"
                f" <code>{symbol}</code>\n"
                f"Цена: <b>${b_close:,.6g}</b>  |  Squeeze: {width[squeeze_idx]:.2f}%\n"
                f"🟢 TP: <b>${tp_price:,.6g}</b>  │  🔴 SL: <b>${sl_price:,.6g}</b>"
            ),
        )

        with state_lock:
            state["last_bb_squeeze_alerted"][symbol] = now

        logger.info(
            "BB squeeze shadow %s %s: price=%.6g squeeze_width=%.2f%% sl=%.6g tp=%.6g",
            symbol, direction, b_close, width[squeeze_idx], sl_price, tp_price,
        )
        sent += 1

    return sent


def check_ema_crossover(tickers: dict[str, dict]) -> int:
    """Shadow: EMA(9) × EMA(21) crossover on 4h with minimum gap threshold.

    LONG  = EMA9 crosses above EMA21  (golden cross)
    SHORT = EMA9 crosses below EMA21  (death cross)

    Gap threshold EMA_CROSS_GAP_PCT filters weak/noisy crossovers.
    1h tested but max gap at crossover = 0.22%, making 0.3-0.5% targets unreachable.
    4h: p75 gap = 0.117%, threshold 0.15% keeps the best-performing tier.
    Backtest (10 pairs × 500 4h candles, gap≥0.15%): WR=36.8%, ΣPnL=+11.4%.

    Opens is_shadow=True positions (alert_type='ema_cross') for tracking.
    """
    if not tickers:
        return 0
    sent = 0
    now = time.time()

    for symbol, t in tickers.items():
        try:
            vol24 = float(t["quoteVolume"])
        except (ValueError, KeyError):
            continue

        if vol24 < MIN_VOLUME_USDT:
            continue

        with state_lock:
            last = state["last_ema_cross_alerted"].get(symbol, 0)
        if now - last < EMA_CROSS_COOLDOWN:
            continue

        candles = _fetch_4h_ohlcv(symbol, limit=EMA_CROSS_SLOW + 10)
        if not candles or len(candles) < EMA_CROSS_SLOW + 3:
            continue

        closes = [float(c[4]) for c in candles]
        e_fast = _calc_ema_arr(closes, EMA_CROSS_FAST)
        e_slow = _calc_ema_arr(closes, EMA_CROSS_SLOW)
        _, atr_4h = _calc_vwap_atr1h(candles)  # works for any timeframe

        if atr_4h is None or atr_4h <= 0:
            continue

        n = len(closes)
        i = n - 1  # most recent completed 4h candle

        if any(x is None for x in [e_fast[i], e_fast[i-1], e_slow[i], e_slow[i-1]]):
            continue

        price   = closes[i]
        gap_pct = abs(e_fast[i] - e_slow[i]) / price * 100.0

        # Detect crossover on the last candle
        if   e_fast[i-1] < e_slow[i-1] and e_fast[i] > e_slow[i]:
            direction = "LONG"
        elif e_fast[i-1] > e_slow[i-1] and e_fast[i] < e_slow[i]:
            direction = "SHORT"
        else:
            continue  # no crossover

        if gap_pct < EMA_CROSS_GAP_PCT:
            continue  # too weak — likely a noisy whipsaw

        sl_price = price - EMA_CROSS_ATR_SL_MULT * atr_4h if direction == "LONG" \
                   else price + EMA_CROSS_ATR_SL_MULT * atr_4h
        tp_price = price + EMA_CROSS_ATR_TP_MULT * EMA_CROSS_ATR_SL_MULT * atr_4h \
                   if direction == "LONG" \
                   else price - EMA_CROSS_ATR_TP_MULT * EMA_CROSS_ATR_SL_MULT * atr_4h

        if sl_price <= 0 or tp_price <= 0:
            continue
        if direction == "LONG"  and not (sl_price < price < tp_price):
            continue
        if direction == "SHORT" and not (tp_price < price < sl_price):
            continue

        _demo_open_position(
            symbol, direction, price, sl_price, tp_price,
            is_shadow=True,
            alert_type="ema_cross",
            score=63,
            notify_body=(
                f"📈 EMA Cross 4h <b>{'LONG 📈' if direction == 'LONG' else 'SHORT 📉'}</b>"
                f" <code>{symbol}</code>\n"
                f"Цена: <b>${price:,.6g}</b>  |  Gap: {gap_pct:.2f}%\n"
                f"🟢 TP: <b>${tp_price:,.6g}</b>  │  🔴 SL: <b>${sl_price:,.6g}</b>"
            ),
        )

        with state_lock:
            state["last_ema_cross_alerted"][symbol] = now

        logger.info(
            "EMA cross shadow %s %s: price=%.6g gap=%.3f%% sl=%.6g tp=%.6g",
            symbol, direction, price, gap_pct, sl_price, tp_price,
        )
        sent += 1

    return sent


def check_whale_lsr_shift(liquid_pairs: list[tuple[str, float]]) -> int:
    """Alert when Gate.io top-trader LSR flips significantly in one 5-min cycle.

    Strategy:
    - Each cycle, fetch current LSR for the top LSR_MONITOR_TOP_N pairs by volume.
    - Compare to the stored previous value (state["lsr_prev"]).
    - If |current - previous| >= LSR_SHIFT_THRESHOLD AND current crosses a
      bull/bear threshold → send an alert.

    LONG  alert: LSR was < 1.0, now >= LSR_BULL_MIN   (whales rushed into LONG)
    SHORT alert: LSR was > 1.0, now <= LSR_BEAR_MAX   (whales rushed into SHORT)
    """
    if not liquid_pairs:
        return 0
    sent = 0
    now = time.time()

    # Take the top N symbols by volume for LSR polling
    top_pairs = sorted(liquid_pairs, key=lambda x: x[1], reverse=True)[:LSR_MONITOR_TOP_N]

    for symbol, _vol in top_pairs:
        # Cooldown check first to avoid unnecessary API call
        with state_lock:
            last = state["last_lsr_shift_alerted"].get(symbol, 0)
        if now - last < LSR_SHIFT_COOLDOWN:
            continue

        current_lsr = _fetch_gate_lsr_raw(symbol)
        if current_lsr is None:
            continue

        with state_lock:
            prev_lsr = state["lsr_prev"].get(symbol)
            state["lsr_prev"][symbol] = current_lsr  # always update

        if prev_lsr is None:
            continue  # first time seeing this symbol — just store, don't alert

        delta = current_lsr - prev_lsr

        if abs(delta) < LSR_SHIFT_THRESHOLD:
            continue  # not a significant shift

        # Determine direction
        if prev_lsr < 1.0 and current_lsr >= LSR_BULL_MIN:
            direction = "LONG"
            emoji    = "🐋🟢"
            verdict  = "Киты резко перешли в ЛОНГ"
        elif prev_lsr > 1.0 and current_lsr <= LSR_BEAR_MAX:
            direction = "SHORT"
            emoji    = "🐋🔴"
            verdict  = "Киты резко перешли в ШОРТ"
        else:
            continue  # shift happened but didn't cross a threshold

        with state_lock:
            atr   = state["atr_4h"].get(symbol)
            ema   = state["ema200_4h"].get(symbol)

        # Fetch current price from ticker-free path
        gate_t = _gateio_ticker(symbol)
        price  = float(gate_t["lastPrice"]) if gate_t else None

        sl_tp  = _format_sl_tp("buy" if direction == "LONG" else "sell", price, atr) if price else ""
        ema_note = ""
        if ema and price:
            if direction == "LONG":
                ema_note = "\n📍 Выше EMA-200 ✅" if price > ema else "\n📍 Ниже EMA-200 ⚠️"
            else:
                ema_note = "\n📍 Ниже EMA-200 ✅" if price < ema else "\n📍 Выше EMA-200 ⚠️"

        price_str = f"${price:,.6g}" if price else "н/д"
        body = (
            f"{emoji} <b>ВХОД КИТОВ — {verdict}</b>\n"
            f"LSR изменился: <b>{prev_lsr:.2f} → {current_lsr:.2f}</b> "
            f"(Δ{delta:+.2f} за 5 мин)\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<code>{symbol}</code>\n"
            f"💰 Цена: <b>{price_str}</b>{ema_note}\n"
            f"🎯 Рекомендация: <b>{direction}</b> — следуй за китами"
        )
        if sl_tp:
            body += f"\n{sl_tp}"

        score = 65  # fixed moderate score — based on whale signal alone
        delivered, _aid = send_alert_with_log(
            symbol, "whale_lsr_shift", direction, price, body, score,
        )
        if delivered:
            with state_lock:
                state["last_lsr_shift_alerted"][symbol] = now
            logger.info(
                "WhaleLSR %s: %s lsr %.2f→%.2f delta %.2f",
                direction, symbol, prev_lsr, current_lsr, delta,
            )
            sent += 1

    return sent


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
    """Send the dedicated eye-catching new-listing alert (always — bypasses the confluence rule).
    Direction is always SHORT: new listings pump on listing day → fade the pump."""
    price_str = "—"
    volume_str = "—"
    if ticker:
        try:
            price_str = f"${float(ticker['lastPrice']):,.6g}"
        except (ValueError, KeyError):
            pass
        try:
            volume_str = f"${float(ticker['quoteVolume']):,.0f}"
        except (ValueError, KeyError):
            pass

    body = (
        f"🚨🚨🚨 <b>НОВЫЙ ЛИСТИНГ НА BINANCE</b> 🚨🚨🚨\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🆕 <code>{symbol}</code> появился на Binance!\n"
        f"💰 Цена: <b>{price_str}</b>\n"
        f"📊 Объём 24ч: {volume_str}\n"
        f"📉 Рекомендация: <b>SHORT — памп на листинге → шорт</b>\n"
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
        log_alert(symbol, "new_listing", "SHORT", price_for_log)
    logger.info("New-listing alert sent: %s", symbol)


def check_coingecko_new_coins() -> None:
    """Every 30 min: fetch CoinGecko coin list and alert on coins NEWLY appearing
    on CoinGecko that are NOT yet listed on Binance Spot USDT. First run populates
    the baseline without alerting. A safety cap prevents alert floods on anomalies."""
    if not COINGECKO_NEW_COIN_ALERTS_ENABLED:
        logger.debug("CoinGecko new-coin alerts disabled (COINGECKO_NEW_COIN_ALERTS_ENABLED=False)")
        return
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
            f"📉 При листинге план: <b>SHORT — памп при листинге → шорт</b>\n"
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
            highs  = data["highs"]
            closes = data.get("closes", [])
            weekly_high = max(highs[-7:]) if len(highs) >= 7 else max(highs)
            monthly_high = max(highs)
            with state_lock:
                state["weekly_highs"][symbol] = weekly_high
                state["monthly_highs"][symbol] = monthly_high
                # Cache yesterday's close-to-close % change for trend detection.
                # closes[-1] = today (incomplete), closes[-2] = yesterday,
                # closes[-3] = day before yesterday.
                if len(closes) >= 3 and closes[-3] > 0:
                    prior_pct = (closes[-2] - closes[-3]) / closes[-3] * 100
                    state["prior_day_pct"][symbol] = prior_pct
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
    2.0: "🔴",    3.0: "🔴🔴",   5.0: "🔴🔴🔴",  10.0: "💥💥💥",   # pump → SHORT (fade)
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
        rec = "SHORT"   # momentum_up now fades the pump — always SHORT
        kind = f"momentum_{'up' if pct > 0 else 'down'}_{int(abs(threshold))}"
        side = "sell"
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
        sl_tp = _format_sl_tp(side, price, atr, score=score)
        if pct > 0:
            body = (
                f"{emoji} <b><code>{symbol}</code> памп +{pct:.1f}% за 10м → SHORT</b>\n"
                f"💰 Цена: {price_str}\n"
                f"📉 Фейд памп — входим в шорт\n"
                f"🎯 Сила сигнала: <b>{score}/100</b> ({_strength_label(score)})"
                + (f"\n{sl_tp}" if sl_tp else "")
            )
        else:
            body = (
                f"{emoji} <b><code>{symbol}</code> {sign}{pct:.1f}% за 15 минут</b>\n"
                f"💰 Цена: {price_str}\n"
                f"🎯 Сила сигнала: <b>{score}/100</b> ({_strength_label(score)})"
                + (f"\n{sl_tp}" if sl_tp else "")
            )
        delivered, _aid = send_alert_with_log(
            symbol, kind, rec, price, body, score,
            rsi=rsi_map.get(symbol) if rsi_map else None,
            pct24=pct,
        )
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
    sent_oh, sent_os, ema_blocked_oh, reversal_blocked_oh, bear_long_blocked = 0, 0, 0, 0, 0
    now = time.time()

    # ── Pre-scan: collect co-movers for "also rising / also falling" note ───
    # These lists contain ALL coins that crossed the same directional threshold
    # in this cycle — regardless of RSI/EMA/cooldown filters.  They are shown
    # in the signal body so the trader can spot correlated pumps/dumps.
    oh_peers: list[tuple[str, float]] = []   # (symbol, pct24) — overheated
    os_peers: list[tuple[str, float]] = []   # (symbol, pct24) — oversold
    for _sym, _t in tickers.items():
        try:
            _p = float(_t["priceChangePercent"])
            _thresh_oh = _vol_threshold(_sym, OVERHEATED_24H_PCT, 2.5)
            _thresh_os = _vol_threshold(_sym, OVERSOLD_24H_PCT, 2.5)
        except (ValueError, KeyError):
            continue
        if _p >= _thresh_oh:
            oh_peers.append((_sym, _p))
        elif _p <= _thresh_os:
            os_peers.append((_sym, _p))
    oh_peers.sort(key=lambda x: x[1], reverse=True)   # strongest pump first
    os_peers.sort(key=lambda x: x[1])                  # strongest dump first
    # ────────────────────────────────────────────────────────────────────────

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
            if not OVERHEATED_ENABLED:
                continue  # OVERHEATED_ENABLED=False: overheated_24h заблокирован
            # Momentum LONG: coin pumped hard with high RSI → ride the momentum.
            # Backtest shows 63% of overheated events continue rising in next 12h.
            # Require price ABOVE EMA-200 (strong uptrend confirmation).
            with state_lock:
                ema_oh = state["ema200_4h"].get(symbol)
            if ema_oh is not None and price < ema_oh:
                logger.debug(
                    "Overheated LONG suppressed by EMA-200 filter: %s price=%.6g ema=%.6g",
                    symbol, price, ema_oh,
                )
                ema_blocked_oh += 1
                continue

            with state_lock:
                last = state["last_overheated_alerted"].get(symbol, 0)
            regime = state.get("market_regime", "NEUTRAL")
            oh_cooldown = OVERHEATED_COOLDOWN_BY_REGIME.get(regime, OVERHEATED_COOLDOWN)
            if now - last >= oh_cooldown:
                score = compute_signal_score(
                    "overheated_24h", "buy",
                    rsi=rsi, pct24=pct24, btc_pct24=btc_pct24,
                )
                # Funding + LSR adjustment
                _fl_delta_oh, _fl_text_oh, _fl_fpts_oh, _fl_lpts_oh = (
                    _funding_lsr_score_and_text(symbol, "buy")
                )
                score = max(0, min(100, score + _fl_delta_oh))
                sl_tp = _format_sl_tp("buy", price, atr, score=score)
                # Co-movers: up to 3 other coins also in the overheated zone
                _oh_others = [
                    f"<code>{s}</code> +{p:.1f}%"
                    for s, p in oh_peers if s != symbol
                ][:3]
                _co_line = (
                    f"\n🔗 Также растут: {', '.join(_oh_others)}"
                    if _oh_others else ""
                )
                _oh_trend_warn = _trend_warning_line(symbol, "LONG")
                body = (
                    f"🚀 <b>ИМПУЛЬС ВВЕРХ — продолжение роста</b>\n"
                    f"Монета в сильном восходящем импульсе\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"<code>{symbol}</code>\n"
                    f"📈 24ч: <b>+{pct24:.1f}%</b> (порог <b>+{oh_threshold:.1f}%</b>)\n"
                    f"🔥 RSI: <b>{rsi:.1f}</b>\n"
                    f"💰 Цена: ${price:,.6g}\n"
                    f"🎯 Сила сигнала: <b>{score}/100</b> ({_strength_label(score)})"
                    + (f"\n{sl_tp}" if sl_tp else "")
                    + _co_line
                    + (f"\n{_oh_trend_warn}" if _oh_trend_warn else "")
                )
                # ── Ликвидационное вето (лонг-каскад) ───────────────────────
                _vol24_oh = float((tickers.get(symbol) or {}).get("quoteVolume", 0))
                _liq_veto_oh, _liq_reason_oh = _check_liq_veto(symbol, "buy", _vol24_oh)
                if _liq_veto_oh:
                    logger.info(
                        "SUPPRESSED overheated %s — ликвидации: %s", symbol, _liq_reason_oh
                    )
                    _dsl_lv, _dtp_lv = _compute_demo_sl_tp("LONG", price, atr)
                    if _dsl_lv and _dtp_lv:
                        _demo_open_position(
                            symbol, "LONG", price, _dsl_lv, _dtp_lv,
                            is_shadow=True,
                            shadow_reason=f"liq_veto: {_liq_reason_oh}",
                            alert_type="overheated_24h",
                            notify_body=body,
                        )
                    continue
                # ── AI veto (AI_VETO_ENABLED=False → отключён) ───────────────
                _ai_ok_oh, _ai_note_oh, _ai_hold_oh = True, "", ""
                if AI_VETO_ENABLED:
                    _ai_ok_oh, _ai_note_oh, _ai_hold_oh = _ai_veto_confluence(
                        symbol, "LONG", rsi, pct24, None
                    )
                    if not _ai_ok_oh:
                        logger.info(
                            "SUPPRESSED overheated %s — ИИ: %s", symbol, _ai_note_oh,
                        )
                        _dsl, _dtp = _compute_demo_sl_tp("LONG", price, atr)
                        if _dsl and _dtp:
                            _demo_open_position(
                                symbol, "LONG", price, _dsl, _dtp,
                                is_shadow=True,
                                shadow_reason=f"ai_veto: {_ai_note_oh}",
                                alert_type="overheated_24h",
                                notify_body=body,
                            )
                        continue
                # ── Факторы фандинг + LSR ────────────────────────────────────
                if _fl_text_oh:
                    body += f"\n{_fl_text_oh}"
                # ── Whale stats ──────────────────────────────────────────────
                _whale_oh = _get_gate_whale_stats(symbol)
                _whale_alert_oh = _get_whale_alert_line(symbol)
                if _whale_oh:
                    body += f"\n{_whale_oh}"
                if _whale_alert_oh:
                    body += f"\n{_whale_alert_oh}"
                if _ai_hold_oh:
                    body += f"\n⏳ {html.escape(_ai_hold_oh)}"
                if _ai_note_oh:
                    body += f"\n🤖 ИИ подтвердил: <i>{html.escape(_ai_note_oh)}</i>"
                delivered, _aid = send_alert_with_log(
                    symbol, "overheated_24h", "LONG", price, body, score,
                    rsi=rsi, pct24=pct24,
                    factor_funding_pts=_fl_fpts_oh, factor_lsr_pts=_fl_lpts_oh,
                )
                if delivered:
                    with state_lock:
                        state["last_overheated_alerted"][symbol] = now
                    sent_oh += 1
                    # Track real demo position so we can measure win-rate later
                    _dsl_oh, _dtp_oh = _compute_demo_sl_tp("LONG", price, atr)
                    if _dsl_oh and _dtp_oh:
                        _demo_open_position(
                            symbol, "LONG", price, _dsl_oh, _dtp_oh,
                            is_shadow=False,
                            alert_type="overheated_24h",
                            score=score,
                        )

        elif pct24 <= os_threshold and rsi <= RSI_OVERSOLD:
            # LONG bounce: coin dumped hard AND RSI is deeply oversold →
            # extreme capitulation, high probability of reversal.  Enter LONG
            # to ride the bounce. Data: 48.9% WR in shadow, R:R 2.23.
            with state_lock:
                last = state["last_oversold_alerted"].get(symbol, 0)
            if now - last >= OVERSOLD_COOLDOWN:
                # ── Подтверждение разворота: последняя завершённая 1ч свеча зелёная ──
                # RSI ≤ 30 + падение само по себе не значит разворот — монета может
                # продолжать падать. Требуем close > open последней 1ч свечи как сигнал
                # что давление продавцов ослабло и начался откуп.
                _os_candles = _gateio_klines(symbol, "1h", 2)
                _os_reversal_confirmed = False
                if len(_os_candles) >= 1:
                    _cur_candle = _os_candles[-1]  # текущая формирующаяся свеча
                    try:
                        # Цена выше открытия текущей свечи → откуп идёт прямо сейчас
                        _os_reversal_confirmed = float(price) > float(_cur_candle[1])
                    except (ValueError, IndexError, TypeError):
                        _os_reversal_confirmed = True  # ошибка данных → не блокировать
                else:
                    _os_reversal_confirmed = True  # нет данных → не блокировать
                if not _os_reversal_confirmed:
                    if OVERSOLD_REVERSAL_CANDLE_FILTER:
                        # Фильтр включён: ждём зелёную свечу, теневая позиция
                        logger.info(
                            "SHADOW oversold LONG %s — цена ниже открытия текущей 1ч свечи, откуп не начался",
                            symbol,
                        )
                        _dsl_rc, _dtp_rc = _compute_demo_sl_tp("LONG", price, atr)
                        if _dsl_rc and _dtp_rc:
                            _demo_open_position(
                                symbol, "LONG", price, _dsl_rc, _dtp_rc,
                                is_shadow=True,
                                shadow_reason="no_reversal_candle",
                                alert_type="oversold_24h",
                            )
                        continue
                    # OVERSOLD_REVERSAL_CANDLE_FILTER=False: входим сразу, без ожидания свечи

                score = compute_signal_score(
                    "oversold_24h", "buy",
                    rsi=rsi, pct24=pct24, btc_pct24=btc_pct24,
                )
                # Funding + LSR adjustment
                _fl_delta_os, _fl_text_os, _fl_fpts_os, _fl_lpts_os = (
                    _funding_lsr_score_and_text(symbol, "buy")
                )
                score = max(0, min(100, score + _fl_delta_os))
                sl_tp = _format_sl_tp("buy", price, atr, score=score)
                # Co-movers: up to 3 other coins also oversold
                _os_others = [
                    f"<code>{s}</code> {p:.1f}%"
                    for s, p in os_peers if s != symbol
                ][:3]
                _co_line_os = (
                    f"\n🔗 Также перепроданы: {', '.join(_os_others)}"
                    if _os_others else ""
                )
                _os_trend_warn = _trend_warning_line(symbol, "LONG")
                # Сколько дней подряд монета падала перед сигналом
                _os_closes = _fetch_daily_closes_for_crsi(symbol)
                _os_down_days = _count_consecutive_trend_days(_os_closes, "down") if _os_closes else 0
                _os_down_line = ""
                if _os_down_days >= 2:
                    _os_down_line = f"\n📆 Падает <b>{_os_down_days} дн. подряд</b>"
                    if _os_down_days >= 5:
                        _os_down_line += " 🔥 экстремальная перепроданность"
                    elif _os_down_days >= 3:
                        _os_down_line += " ⚠️"
                body = (
                    f"🟢 <b>ПЕРЕПРОДАННОСТЬ — разворот вверх</b>\n"
                    f"Монета сильно упала, RSI в зоне перепроданности — отскок вероятен\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"<code>{symbol}</code>\n"
                    f"📉 24ч: <b>{pct24:.1f}%</b> (порог <b>{os_threshold:.1f}%</b>)\n"
                    f"🟢 RSI: <b>{rsi:.1f}</b> — зона перепроданности\n"
                    f"💰 Цена: ${price:,.6g}\n"
                    f"🎯 Сила сигнала: <b>{score}/100</b> ({_strength_label(score)})"
                    + (f"\n{sl_tp}" if sl_tp else "")
                    + _os_down_line
                    + _co_line_os
                    + (f"\n{_os_trend_warn}" if _os_trend_warn else "")
                )
                # ── Ликвидационное вето ──────────────────────────────────────
                _vol24_os = float((tickers.get(symbol) or {}).get("quoteVolume", 0))
                _liq_veto_os, _liq_reason_os = _check_liq_veto(symbol, "buy", _vol24_os)
                if _liq_veto_os:
                    logger.info(
                        "SUPPRESSED oversold %s — ликвидации: %s", symbol, _liq_reason_os
                    )
                    _dsl_lv2, _dtp_lv2 = _compute_demo_sl_tp("LONG", price, atr)
                    if _dsl_lv2 and _dtp_lv2:
                        _demo_open_position(
                            symbol, "LONG", price, _dsl_lv2, _dtp_lv2,
                            is_shadow=True,
                            shadow_reason=f"liq_veto: {_liq_reason_os}",
                            alert_type="oversold_24h",
                            notify_body=body,
                        )
                    continue
                # ── AI veto (AI_VETO_ENABLED=False → отключён) ───────────────
                _ai_ok_os, _ai_note_os, _ai_hold_os = True, "", ""
                if AI_VETO_ENABLED:
                    _ai_ok_os, _ai_note_os, _ai_hold_os = _ai_veto_confluence(
                        symbol, "LONG", rsi, pct24, None
                    )
                    if not _ai_ok_os:
                        logger.info(
                            "SUPPRESSED oversold %s — ИИ: %s", symbol, _ai_note_os,
                        )
                        _dsl, _dtp = _compute_demo_sl_tp("LONG", price, atr)
                        if _dsl and _dtp:
                            _demo_open_position(
                                symbol, "LONG", price, _dsl, _dtp,
                                is_shadow=True,
                                shadow_reason=f"ai_veto: {_ai_note_os}",
                                alert_type="oversold_24h",
                                notify_body=body,
                            )
                        continue
                # ── Факторы фандинг + LSR ────────────────────────────────────
                if _fl_text_os:
                    body += f"\n{_fl_text_os}"
                # ── Whale stats ──────────────────────────────────────────────
                _whale_os = _get_gate_whale_stats(symbol)
                _whale_alert_os = _get_whale_alert_line(symbol)
                if _whale_os:
                    body += f"\n{_whale_os}"
                if _whale_alert_os:
                    body += f"\n{_whale_alert_os}"
                if _ai_hold_os:
                    body += f"\n⏳ {html.escape(_ai_hold_os)}"
                if _ai_note_os:
                    body += f"\n🤖 ИИ подтвердил: <i>{html.escape(_ai_note_os)}</i>"
                delivered, _aid = send_alert_with_log(
                    symbol, "oversold_24h", "LONG", price, body, score,
                    rsi=rsi, pct24=pct24,
                    factor_funding_pts=_fl_fpts_os, factor_lsr_pts=_fl_lpts_os,
                )
                if delivered:
                    with state_lock:
                        state["last_oversold_alerted"][symbol] = now
                    sent_os += 1
                    # Track real demo position so we can measure win-rate later
                    _dsl_os, _dtp_os = _compute_demo_sl_tp("LONG", price, atr)
                    if _dsl_os and _dtp_os:
                        _demo_open_position(
                            symbol, "LONG", price, _dsl_os, _dtp_os,
                            is_shadow=False,
                            alert_type="oversold_24h",
                            score=score,
                        )
                        # ── Умный вход по ликвидности (параллельная стратегия) ──
                        threading.Thread(
                            target=_liquidity_entry_trigger,
                            args=(symbol, "LONG", price, _dsl_os),
                            daemon=True,
                        ).start()
                        # ── Shadow: лимитный вход только после подтверждения разворота ──
                        threading.Thread(
                            target=_liquidity_reversal_shadow,
                            args=(symbol, "LONG", price, _dsl_os),
                            daemon=True,
                        ).start()
    return sent_oh, sent_os, ema_blocked_oh, reversal_blocked_oh, bear_long_blocked


def check_breakdown_short(
    tickers: dict[str, dict] | None,
    rsi_map: dict[str, float],
) -> int:
    """Breakdown continuation SHORT — два пути входа:

    Путь A — «есть куда падать» (RSI нейтральный):
      - 24h drop ≤ BREAKDOWN_SHORT_PCT (−10%)
      - RSI in (BREAKDOWN_RSI_MIN, BREAKDOWN_RSI_MAX) = (30, 58)
      - Price < EMA-200 (4h)

    Путь B — «перепродан, но падает дальше» (RSI низкий):
      - 24h drop ≤ BREAKDOWN_RSI_OVERSOLD_DROP (−15%)  — более сильный импульс
      - RSI ≤ BREAKDOWN_RSI_OVERSOLD_MAX (35)           — перепродан, тренд сильный
      - Price < EMA-200 (4h)

    Цель: ловить монеты, которые уже сломали структуру и продолжают падение.
    Путь B закрывает паттерн «RSI низкий, но монета продолжает падать».
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

        # Путь A: «есть куда падать» — RSI нейтральный, сильный дроп
        path_a = (BREAKDOWN_RSI_MIN < rsi < BREAKDOWN_RSI_MAX) and (pct24 <= BREAKDOWN_SHORT_PCT)
        # Путь B: «перепродан, но падает дальше» — RSI низкий, ещё более сильный дроп
        path_b = (rsi <= BREAKDOWN_RSI_OVERSOLD_MAX) and (pct24 <= BREAKDOWN_RSI_OVERSOLD_DROP)

        if not (path_a or path_b):
            continue

        # Price below EMA-200 — confirmed downtrend (required for both paths)
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

        if path_b:
            rsi_line = f"📊 RSI: <b>{rsi:.1f}</b> — перепродан, но тренд продолжается ↓"
            headline = "📉 <b>ПРОБОЙ ВНИЗ — перепродан и продолжает падать</b>"
            subtitle = "RSI низкий, но импульс вниз не исчерпан — тренд сильнее отскока"
        else:
            rsi_line = f"📊 RSI: <b>{rsi:.1f}</b> — давление продавцов, есть куда падать"
            headline = "📉 <b>ПРОБОЙ ВНИЗ — шорт продолжается</b>"
            subtitle = "Монета в нисходящем тренде, импульс не исчерпан"

        sl_tp = _format_sl_tp("sell", price, atr)
        body = (
            f"{headline}\n"
            f"{subtitle}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<code>{symbol}</code>\n"
            f"📉 24ч: <b>{pct24:.1f}%</b>\n"
            f"{rsi_line}\n"
            f"📍 Ниже EMA-200: <b>{ema_pct:.1f}%</b>\n"
            f"💰 Цена: <b>${price:,.6g}</b>\n"
            f"🎯 Сила: <b>{score}/100</b> ({_strength_label(score)})"
            + (f"\n{sl_tp}" if sl_tp else "")
        )
        delivered, _aid = send_alert_with_log(
            symbol, "breakdown_short", "SHORT", price, body, score,
            rsi=rsi, pct24=pct24,
        )
        if delivered:
            with state_lock:
                state["last_breakdown_alerted"][symbol] = now
            logger.info(
                "Breakdown SHORT alert sent: %s path=%s pct24=%.1f rsi=%.1f",
                symbol, "B(oversold)" if path_b else "A(room)", pct24, rsi,
            )
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

        sl_tp = _format_sl_tp("sell", price, atr, score=score)
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

        sl_tp = _format_sl_tp("sell", price, atr, score=score)
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
        else:
            # volume_surge_long disabled — backtest 30d: 100% SL rate (0 TP of 8 signals)
            continue

        score = compute_signal_score(
            kind, side, pct24=pct24, btc_pct24=btc_pct24,
        )
        with state_lock:
            atr = state["atr_4h"].get(symbol)
        sl_tp = _format_sl_tp(side, price, atr, score=score)
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


def check_pump_24h_fade(
    tickers: dict[str, dict] | None,
    rsi_map: dict[str, float],
) -> int:
    """Fade 24h pumps → SHORT signal (pump-and-dump reversion strategy).

    Fires when a coin pumped ≥ PUMP_FADE_MIN_PCT in 24h AND RSI confirms overbought.
    Academic basis: post-pump reversion averages ~−13% in 4h (Dhawan & Putniņš, 2020).

    PUMP_FADE_SHADOW_ONLY=True  → all positions shadow, no real alerts sent.
    PUMP_FADE_SHADOW_ONLY=False → real paper-trade alerts after 2-week validation.
    """
    if not tickers:
        return 0
    sent = 0
    now = time.time()

    with state_lock:
        btc_t = tickers.get("BTCUSDT")
        btc_pct24: float | None = None
        if btc_t:
            try:
                btc_pct24 = float(btc_t["priceChangePercent"])
            except (ValueError, KeyError, TypeError):
                pass

    for symbol, t in tickers.items():
        if not symbol.endswith("USDT"):
            continue
        try:
            price = float(t["lastPrice"])
            pct24 = float(t["priceChangePercent"])
            vol24 = float(t.get("quoteVolume", 0))
        except (ValueError, KeyError, TypeError):
            continue
        if not (math.isfinite(price) and math.isfinite(pct24)) or price <= 0:
            continue

        # Pump threshold: min +30%, max 200% (avoid listing-day outliers)
        if pct24 < PUMP_FADE_MIN_PCT or pct24 > PUMP_FADE_MAX_PCT:
            continue
        # Volume gate: illiquid coins have unreliable price moves
        if vol24 < PUMP_FADE_MIN_VOL:
            continue

        rsi = rsi_map.get(symbol)
        if rsi is None or rsi < PUMP_FADE_RSI_MIN:
            continue

        with state_lock:
            last = state["last_pump_alerted"].get(symbol, 0)
        if now - last < PUMP_FADE_COOLDOWN:
            continue

        with state_lock:
            atr = state["atr_4h"].get(symbol)

        score = compute_signal_score(
            "volume_surge_short", "sell",   # conceptually identical: fade overbought
            rsi=rsi, pct24=pct24, btc_pct24=btc_pct24,
        )
        sl_tp = _format_sl_tp("sell", price, atr, score=score)
        is_shadow = PUMP_FADE_SHADOW_ONLY

        body = (
            f"📉 <b>ПАМП → ШОРТ (fade): <code>{symbol}</code></b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📈 24ч рост: <b>+{pct24:.1f}%</b> — ожидаем откат\n"
            f"🧊 RSI: <b>{rsi:.1f}</b> — перегрет\n"
            f"💰 Цена: <b>${price:,.6g}</b>\n"
            f"🎯 Сила сигнала: <b>{score}/100</b> ({_strength_label(score)})"
            + (f"\n{sl_tp}" if sl_tp else "")
            + ("\n⚠️ <i>[ТЕСТ: shadow-режим, ещё не торгуем]</i>" if is_shadow else "")
        )

        if not is_shadow:
            if score < PUMP_FADE_MIN_SCORE:
                logger.debug(
                    "pump_24h_fade %s skipped: score=%d < %d", symbol, score, PUMP_FADE_MIN_SCORE
                )
                _dsl_pf, _dtp_pf = _compute_pump_fade_sl_tp(price, atr)
                if _dsl_pf and _dtp_pf:
                    _demo_open_position(
                        symbol, "SHORT", price, _dsl_pf, _dtp_pf,
                        is_shadow=True,
                        shadow_reason=f"low_score: {score}<{PUMP_FADE_MIN_SCORE}",
                        alert_type="pump_24h_fade",
                    )
                continue
            delivered, _ = send_alert_with_log(
                symbol, "pump_24h_fade", "SHORT", price, body, score,
            )
            if not delivered:
                continue
            with state_lock:
                state["last_pump_alerted"][symbol] = now
            sent += 1
        else:
            # Shadow mode: log and record, don't send real Telegram alert
            logger.info(
                "pump_24h_fade SHADOW: %s pct24=+%.1f%% rsi=%.1f score=%d",
                symbol, pct24, rsi, score,
            )
            with state_lock:
                state["last_pump_alerted"][symbol] = now

        _dsl_pf, _dtp_pf = _compute_pump_fade_sl_tp(price, atr)
        if _dsl_pf and _dtp_pf:
            _demo_open_position(
                symbol, "SHORT", price, _dsl_pf, _dtp_pf,
                is_shadow=is_shadow,
                shadow_reason=("pump_fade_shadow_mode" if is_shadow else None),
                alert_type="pump_24h_fade",
                score=score,
                notify_body=(
                    f"📉 Pump Fade <b>SHORT 📉</b> <code>{symbol}</code>\n"
                    f"Цена: <b>${price:,.6g}</b>  |  +{pct24:.1f}% за 24ч  |  RSI {rsi:.0f}\n"
                    f"🟢 TP: <b>${_dtp_pf:,.6g}</b>  │  🔴 SL: <b>${_dsl_pf:,.6g}</b>"
                ) if is_shadow else None,
            )

    return sent


def check_listing_dump_long(
    tickers: dict[str, dict] | None,
    rsi_map: dict[str, float],
) -> int:
    """🧪 ТЕСТ: Листинг-дамп → LONG восстановление.

    Паттерн: монеты вроде MUUUSDT / SOXLUSDT — листинг ≤90 дней назад,
    сильный памп после листинга, затем падение ≥40% от месячного пика.
    RSI подтверждает перепроданность → высокая вероятность отскока.

    Условия:
      - Монета листалась в последние NEW_LISTING_DUMP_WINDOW_DAYS дней (90)
      - Текущая цена ≥ −NEW_LISTING_DUMP_FROM_PEAK_PCT от 30d максимума (−40%)
      - RSI ≤ NEW_LISTING_DUMP_RSI_MAX (35) — зона перепроданности
      - Объём ≥ NEW_LISTING_DUMP_MIN_VOL USDT за 24ч
      - 4ч кулдаун на монету
    """
    if not tickers:
        return 0
    sent = 0
    now = time.time()
    cutoff = int(now) - NEW_LISTING_DUMP_WINDOW_DAYS * 86400

    # Fetch coins that were listed within the window
    try:
        with _db_lock:
            conn = _get_db()
            listings = conn.execute(
                "SELECT symbol, price_at_alert, ts FROM alerts "
                "WHERE alert_type='new_listing' AND ts >= ? AND price_at_alert > 0 "
                "GROUP BY symbol ORDER BY ts ASC",
                (cutoff,),
            ).fetchall()
    except Exception as exc:
        logger.warning("check_listing_dump_long DB query failed: %s", exc)
        return 0

    if not listings:
        return 0

    with state_lock:
        monthly_highs = dict(state["monthly_highs"])
        weekly_highs  = dict(state["weekly_highs"])

    btc_t = tickers.get("BTCUSDT")
    btc_pct24: float | None = None
    if btc_t:
        try:
            btc_pct24 = float(btc_t["priceChangePercent"])
        except (ValueError, KeyError, TypeError):
            pass

    for symbol, listing_price, listing_ts in listings:
        t = tickers.get(symbol)
        if t is None:
            continue
        try:
            price  = float(t["lastPrice"])
            vol24  = float(t["quoteVolume"])
            pct24  = float(t["priceChangePercent"])
        except (ValueError, KeyError):
            continue
        if price <= 0 or not math.isfinite(price):
            continue

        # Volume filter
        if vol24 < NEW_LISTING_DUMP_MIN_VOL:
            continue

        # RSI must be oversold
        rsi = rsi_map.get(symbol)
        if rsi is None or rsi > NEW_LISTING_DUMP_RSI_MAX:
            continue

        # Use 30d high; fall back to 7d high if not yet populated
        peak = monthly_highs.get(symbol) or weekly_highs.get(symbol)
        if peak is None or peak <= 0:
            continue

        dump_pct = (peak - price) / peak * 100.0
        if dump_pct < NEW_LISTING_DUMP_FROM_PEAK_PCT:
            continue

        # Cooldown
        with state_lock:
            last = state["last_listing_dump_alerted"].get(symbol, 0)
        if now - last < NEW_LISTING_DUMP_COOLDOWN:
            continue

        days_since = (now - listing_ts) / 86400.0
        with state_lock:
            atr = state["atr_4h"].get(symbol)

        score = compute_signal_score(
            "listing_dump_long", "buy",
            rsi=rsi, pct24=pct24, btc_pct24=btc_pct24,
        )
        min_score = MIN_SCORE_BY_TYPE.get("listing_dump_long", MIN_ALERT_SCORE)
        if score < min_score:
            continue

        sl_tp = _format_sl_tp("buy", price, atr, score=score)
        body = (
            f"🧪 <b>[ТЕСТ] ЛИСТИНГ-ДАМП — возможен отскок (ЛОНГ)</b>\n"
            f"Монета упала далеко от пика после листинга\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<code>{symbol}</code>\n"
            f"🆕 Листинг: {days_since:.0f} дн. назад  |  "
            f"Цена листинга: <b>${listing_price:,.6g}</b>\n"
            f"📉 Дамп от пика: <b>−{dump_pct:.1f}%</b>  "
            f"(пик ${peak:,.6g} → сейчас ${price:,.6g})\n"
            f"🧊 RSI: <b>{rsi:.1f}</b> — перепродан\n"
            f"📊 24ч изменение: {pct24:+.1f}%  |  "
            f"Объём: ${vol24:,.0f}\n"
            f"🎯 Сила сигнала: <b>{score}/100</b> ({_strength_label(score)})"
            + (f"\n{sl_tp}" if sl_tp else "")
        )
        delivered, _aid = send_alert_with_log(
            symbol, "listing_dump_long", "LONG", price, body, score,
        )
        if delivered:
            with state_lock:
                state["last_listing_dump_alerted"][symbol] = now
            logger.info(
                "Listing-dump LONG: %s listing=%.6g peak=%.6g now=%.6g "
                "dump=−%.1f%% rsi=%.1f",
                symbol, listing_price, peak, price, dump_pct, rsi,
            )
            sent += 1

    return sent


def check_listing_peak_short(
    tickers: dict[str, dict] | None,
    rsi_map: dict[str, float],
) -> int:
    """🧪 ТЕСТ: Пик нового листинга → ШОРТ (долгосрочная позиция).

    Паттерн: новые монеты (≤72ч) часто делают памп ≥30% от цены листинга,
    после чего следует многонедельный или многомесячный дамп на 60-90%.
    Сигнал срабатывает на пике:
      - Монета листалась в последние NEW_LISTING_PEAK_WINDOW_H часов (72)
      - Текущая цена ≥ +NEW_LISTING_PEAK_PUMP_MIN от цены листинга (+30%)
      - RSI ≥ NEW_LISTING_PEAK_RSI_MIN (70) — очень перегрета
      - 24ч кулдаун на монету

    Позиция открывается с широким SL/TP и держится до 30 дней:
      SL = +15% от входа (не выбьет на шуме)
      TP = −60% от входа (цель — массивный дамп)
    """
    if not tickers:
        return 0
    sent = 0
    now = time.time()
    cutoff = int(now) - NEW_LISTING_PEAK_WINDOW_H * 3600

    try:
        with _db_lock:
            conn = _get_db()
            listings = conn.execute(
                "SELECT symbol, price_at_alert, ts FROM alerts "
                "WHERE alert_type='new_listing' AND ts >= ? AND price_at_alert > 0 "
                "GROUP BY symbol ORDER BY ts ASC",
                (cutoff,),
            ).fetchall()
    except Exception as exc:
        logger.warning("check_listing_peak_short DB query failed: %s", exc)
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
            price  = float(t["lastPrice"])
            vol24  = float(t["quoteVolume"])
            pct24  = float(t["priceChangePercent"])
        except (ValueError, KeyError):
            continue
        if price <= 0 or not math.isfinite(price):
            continue

        # Volume filter
        if vol24 < MIN_VOLUME_USDT:
            continue

        # RSI must confirm extreme overheating
        rsi = rsi_map.get(symbol)
        if rsi is None or rsi < NEW_LISTING_PEAK_RSI_MIN:
            continue

        # Coin must have pumped enough from listing price
        pump_pct = (price - listing_price) / listing_price * 100.0
        if pump_pct < NEW_LISTING_PEAK_PUMP_MIN:
            continue

        # Cooldown
        with state_lock:
            last = state["last_listing_peak_alerted"].get(symbol, 0)
        if now - last < NEW_LISTING_PEAK_COOLDOWN:
            continue

        hours_since = (now - listing_ts) / 3600.0
        with state_lock:
            atr = state["atr_4h"].get(symbol)

        score = compute_signal_score(
            "listing_peak_short", "sell",
            rsi=rsi, pct24=pct24, btc_pct24=btc_pct24,
        )
        min_score = MIN_SCORE_BY_TYPE.get("listing_peak_short", MIN_ALERT_SCORE)
        if score < min_score:
            continue

        # Format wide SL/TP for display
        sl_price_disp = price * (1 + NEW_LISTING_PEAK_SL_PCT / 100)
        tp_price_disp = price * (1 - NEW_LISTING_PEAK_TP_PCT / 100)
        sl_roi_disp = NEW_LISTING_PEAK_SL_PCT * DISPLAY_LEVERAGE
        tp_roi_disp = NEW_LISTING_PEAK_TP_PCT * DISPLAY_LEVERAGE

        body = (
            f"🧪 <b>[ТЕСТ] ПИК ЛИСТИНГА — ШОРТ (долгосрочный)</b>\n"
            f"Монета на пике после листинга — цель дамп −60%\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<code>{symbol}</code>\n"
            f"🆕 Листинг: {hours_since:.1f}ч назад  |  "
            f"Цена листинга: <b>${listing_price:,.6g}</b>\n"
            f"📈 Памп от листинга: <b>+{pump_pct:.1f}%</b>\n"
            f"🔥 RSI: <b>{rsi:.1f}</b> — сильно перегрет\n"
            f"💰 Вход: <b>${price:,.6g}</b>  |  24ч: {pct24:+.1f}%\n"
            f"🛡️ Стоп: <b>${sl_price_disp:,.6g}</b> (+{sl_roi_disp:.0f}% ROI ×{DISPLAY_LEVERAGE})\n"
            f"🎯 Цель: <b>${tp_price_disp:,.6g}</b> (−{tp_roi_disp:.0f}% ROI ×{DISPLAY_LEVERAGE})\n"
            f"⏳ Удерживать до <b>30 дней</b>\n"
            f"📊 Сила сигнала: <b>{score}/100</b> ({_strength_label(score)})"
        )
        delivered, aid = send_alert_with_log(
            symbol, "listing_peak_short", "SHORT", price, body, score,
        )
        if delivered and aid:
            # Open a long-duration position with wide SL/TP
            _auto_start_monitor(
                aid, symbol, "SHORT", price,
                sl_pct=NEW_LISTING_PEAK_SL_PCT,
                tp_pct=NEW_LISTING_PEAK_TP_PCT,
                timeout_hours=NEW_LISTING_PEAK_TIMEOUT_H,
            )
            with state_lock:
                state["last_listing_peak_alerted"][symbol] = now
            logger.info(
                "Listing-peak SHORT: %s listing=%.6g pump=+%.1f%% "
                "rsi=%.1f sl=%.0f%% tp=%.0f%% timeout=%.0fh",
                symbol, listing_price, pump_pct, rsi,
                NEW_LISTING_PEAK_SL_PCT, NEW_LISTING_PEAK_TP_PCT,
                NEW_LISTING_PEAK_TIMEOUT_H,
            )
            sent += 1

    return sent


# ---------------------------------------------------------------------------
# Personal position tracker — monitor user's own open positions for reversals
# ---------------------------------------------------------------------------

_USERPOS_REVERSAL_COOLDOWN   = 6 * 3600   # 6h between alerts per position
_USERPOS_RSI_PERIOD          = 14
# Minimum total candle move to count as a real confirmation (not noise)
REVERSAL_CANDLE_MIN_MOVE_PCT = 0.5        # % total move across consecutive candles
# Minimum position age before a correction with < 3 confirmations is sent
REVERSAL_MIN_AGE_SECS        = 30 * 60   # 30 minutes
# Path C — emergency reversal: sharp pump then sharp drop, bypasses EMA/RSI wait
REVERSAL_PUMP_LOOKBACK       = 3          # candles to look back for the pump
REVERSAL_PUMP_MIN_PCT        = 15.0       # pump must be ≥ this % to qualify
REVERSAL_SHARP_DROP_PCT      = 5.0        # last candle must drop ≥ this % to trigger


def _calc_rsi_simple(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 2:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    return 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)


def check_user_position_reversals() -> None:
    """Check all open user positions for confirmed reversal signals.

    Confirmation requires at least 2 of 3 indicators to agree before alerting.
    This prevents false positives like the one we saw with TUTUSDT.

    SHORT position reversal (go LONG):
      1. RSI (1h) has climbed: recent low was ≤ 38, current ≥ 45
      2. ≥ 2 consecutive green 1h candles
      3. Price crossed above 1h EMA-20

    LONG position reversal (go SHORT):
      1. RSI (1h) has dropped: recent high was ≥ 62, current ≤ 55
      2. ≥ 2 consecutive red 1h candles
      3. Price crossed below 1h EMA-20
    """
    try:
        db = _get_db()
        rows = db.execute(
            "SELECT id, symbol, direction, note, ts_added, last_alerted "
            "FROM user_positions WHERE status='open'"
        ).fetchall()
    except Exception as e:
        logger.error("check_user_position_reversals: DB error: %s", e)
        return

    if not rows:
        return

    now = int(time.time())
    GATE_BASE = "https://fx-api.gateio.ws/api/v4"

    for pos_id, symbol, direction, note, ts_added, last_alerted in rows:
        # Respect cooldown
        if now - last_alerted < _USERPOS_REVERSAL_COOLDOWN:
            continue

        gate_sym = symbol[:-4] + "_USDT" if symbol.endswith("USDT") else symbol + "_USDT"
        try:
            r = requests.get(
                f"{GATE_BASE}/futures/usdt/candlesticks",
                params={"contract": gate_sym, "interval": "1h", "limit": 30},
                timeout=8,
            )
            candles = r.json()
            if not isinstance(candles, list) or len(candles) < 22:
                continue
            candles = candles[:-1]   # drop live candle
        except Exception:
            continue

        closes = [float(c["c"]) for c in candles]
        opens  = [float(c["o"]) for c in candles]
        price  = closes[-1]

        # ── Indicator 1: RSI trend ────────────────────────────────────────
        rsi_now  = _calc_rsi_simple(closes)
        rsi_prev = _calc_rsi_simple(closes[:-3])   # 3h ago
        if rsi_now is None or rsi_prev is None:
            continue

        if direction == "SHORT":
            # SHORT reversal: RSI was oversold/low, now climbing
            rsi_confirms = rsi_prev <= 38 and rsi_now >= 45
        else:
            # LONG reversal: RSI was overbought/high, now falling
            rsi_confirms = rsi_prev >= 62 and rsi_now <= 55

        # ── Indicator 2: consecutive candles (with noise filter) ─────────
        consec_count = 0
        for i in range(-1, -5, -1):
            if direction == "SHORT":
                if closes[i] > opens[i]:   # green
                    consec_count += 1
                else:
                    break
            else:
                if closes[i] < opens[i]:   # red
                    consec_count += 1
                else:
                    break
        candle_confirms = False
        if consec_count >= 2:
            # FIX 2: require a minimum total price move — not just colour
            # compare open of the first counted candle to close of the last
            first_open  = opens[-consec_count]
            total_move  = (
                abs(closes[-1] - first_open) / first_open * 100
                if first_open != 0 else 0.0
            )
            if total_move >= REVERSAL_CANDLE_MIN_MOVE_PCT:
                candle_confirms = True
            else:
                logger.info(
                    "check_user_position_reversals: %s %s — candle streak "
                    "suppressed (move %.3f%% < %.1f%% threshold)",
                    symbol, direction, total_move, REVERSAL_CANDLE_MIN_MOVE_PCT,
                )

        # ── Indicator 3: EMA-20 true crossover (1h) ─────────────────────
        # FIX 1: detect an actual cross — prev candle on one side, current
        # candle on the other — instead of just checking "price > EMA now".
        # Requires len(closes) >= 22 (already guaranteed above).
        multiplier = 2.0 / (20 + 1)
        # EMA value at the previous completed candle (closes[-2])
        ema20_prev = closes[-21]
        for c in closes[-20:-1]:            # 19 steps → lands at closes[-2]
            ema20_prev = c * multiplier + ema20_prev * (1 - multiplier)
        # EMA value at the current completed candle (closes[-1])
        ema20_curr = closes[-1] * multiplier + ema20_prev * (1 - multiplier)
        prev_price = closes[-2]
        if direction == "SHORT":
            # Cross UP: prev candle closed below EMA, current closed above
            ema_confirms = (prev_price < ema20_prev) and (price > ema20_curr)
        else:
            # Cross DOWN: prev candle closed above EMA, current closed below
            ema_confirms = (prev_price > ema20_prev) and (price < ema20_curr)
        # Use ema20_curr for display in the alert text
        ema20 = ema20_curr

        confirmed_count = sum([rsi_confirms, candle_confirms, ema_confirms])

        # ── Path C: emergency — sharp pump then sharp drop (1-2 candles) ──
        # Fires for LONG positions immediately without waiting for EMA cross or
        # candle streak. Designed for situations like +36% pump followed by
        # -14% reversal within the same hour (e.g. BMTUSDT 2026-08-09).
        if direction == "LONG" and len(closes) >= REVERSAL_PUMP_LOOKBACK + 2:
            _pc_start = closes[-1 - REVERSAL_PUMP_LOOKBACK]
            _pc_peak  = max(closes[-REVERSAL_PUMP_LOOKBACK:-1])
            _pc_pump  = (
                (_pc_peak - _pc_start) / _pc_start * 100
                if _pc_start else 0.0
            )
            _pc_drop  = (
                (closes[-1] - opens[-1]) / opens[-1] * 100
                if opens[-1] else 0.0
            )
            if _pc_pump >= REVERSAL_PUMP_MIN_PCT and _pc_drop <= -REVERSAL_SHARP_DROP_PCT:
                logger.info(
                    "check_user_position_reversals Path C: %s LONG — "
                    "pump +%.1f%% then candle %.1f%%",
                    symbol, _pc_pump, _pc_drop,
                )
                _note_line_c = (
                    f"\n📝 Ваша заметка: <i>{html.escape(note)}</i>" if note else ""
                )
                _pc_body = (
                    f"⚡ <b>ЭКСТРЕННАЯ КОРРЕКЦИЯ по вашей позиции</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"<code>{symbol}</code> · Ваш LONG → возможен ШОРТ 📉\n"
                    f"💰 Цена: ${price:,.6g}\n"
                    f"🚨 Острый разворот:\n"
                    f"  • Памп <b>+{_pc_pump:.1f}%</b> за {REVERSAL_PUMP_LOOKBACK} свечи\n"
                    f"  • Последняя свеча: <b>{_pc_drop:.1f}%</b> — резкий откат"
                    f"{_note_line_c}\n\n"
                    f"⚠️ Не дожидается EMA-кросса — действуй немедленно.\n"
                    f"⚠️ Не финансовый совет — примите решение самостоятельно."
                )
                _ok_c = send_telegram(_pc_body)
                if _ok_c:
                    try:
                        db.execute(
                            "UPDATE user_positions SET last_alerted=? WHERE id=?",
                            (now, pos_id),
                        )
                        db.commit()
                    except Exception as e:
                        logger.error(
                            "check_user_position_reversals Path C: update error: %s", e
                        )
                    logger.info(
                        "UserPos Path C alert sent: %s LONG pump=+%.1f%% drop=%.1f%%",
                        symbol, _pc_pump, _pc_drop,
                    )
                continue   # don't also fire the regular reversal for this cycle

        if confirmed_count < 2:
            continue

        # ── FIX 3: timing guard — protect fresh positions from immediate noise
        pos_age_secs = now - ts_added
        if pos_age_secs < REVERSAL_MIN_AGE_SECS and confirmed_count < 3:
            logger.info(
                "check_user_position_reversals: %s %s — correction suppressed "
                "(position only %dm old, conf=%d < 3 required within first %dm)",
                symbol, direction,
                pos_age_secs // 60, confirmed_count,
                REVERSAL_MIN_AGE_SECS // 60,
            )
            continue

        # ── Build alert ──────────────────────────────────────────────────
        reversal_dir   = "ЛОНГ" if direction == "SHORT" else "ШОРТ"
        reversal_emoji = "📈" if direction == "SHORT" else "📉"
        signals_text = []
        if rsi_confirms:
            if direction == "SHORT":
                signals_text.append(f"📊 RSI вырос: {rsi_prev:.0f} → {rsi_now:.0f} (выход из зоны перепроданности)")
            else:
                signals_text.append(f"📊 RSI упал: {rsi_prev:.0f} → {rsi_now:.0f} (выход из зоны перекупленности)")
        if candle_confirms:
            ctype = "зелёных" if direction == "SHORT" else "красных"
            signals_text.append(f"🕯 {consec_count} {ctype} свечи подряд на 1ч")
        if ema_confirms:
            cross = "пересёк EMA-20 вверх" if direction == "SHORT" else "пересёк EMA-20 вниз"
            signals_text.append(f"📉 Цена {cross} (${ema20:.6g})")

        conf_label = "✅✅✅ Тройное подтверждение" if confirmed_count == 3 else "✅✅ Двойное подтверждение"
        note_line  = f"\n📝 Ваша заметка: <i>{html.escape(note)}</i>" if note else ""

        body = (
            f"{reversal_emoji} <b>Коррекция по вашей позиции</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<code>{symbol}</code> · Ваш {direction} → сигнал <b>{reversal_dir}</b>\n"
            f"💰 Цена: ${price:,.6g}\n"
            f"{conf_label}\n"
            f"{chr(10).join(signals_text)}"
            f"{note_line}\n\n"
            f"⚠️ Не финансовый совет — примите решение самостоятельно."
        )
        ok = send_telegram(body)
        if ok:
            try:
                db.execute(
                    "UPDATE user_positions SET last_alerted=? WHERE id=?",
                    (now, pos_id),
                )
                db.commit()
            except Exception as e:
                logger.error("check_user_position_reversals: update error: %s", e)
            logger.info(
                "UserPos reversal alert sent: %s %s → %s (conf=%d)",
                symbol, direction, reversal_dir, confirmed_count,
            )


# ---------------------------------------------------------------------------
# /addpos  /mypos  /closepos — personal position tracker commands
# ---------------------------------------------------------------------------

def handle_addpos_command(chat_id: int, text: str) -> None:
    """Register a personal position for reversal monitoring.
    Usage: /addpos SYMBOL DIRECTION [note]
    Example: /addpos BTCUSDT SHORT "вошёл по сигналу 6ч стрик"
    """
    parts = text.strip().split(None, 3)
    if len(parts) < 3:
        _telegram_send(chat_id,
            "📍 <b>Трекер личных позиций</b>\n\n"
            "Формат: <code>/addpos SYMBOL DIRECTION</code>\n"
            "Пример: <code>/addpos BTCUSDT SHORT</code>\n"
            "С заметкой: <code>/addpos ETHUSDT LONG вошёл по стрику</code>\n\n"
            "Бот будет присылать подтверждённые сигналы коррекции\n"
            "(минимум 2 из 3 индикаторов должны совпасть)."
        )
        return

    symbol = parts[1].upper().strip()
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    direction = parts[2].upper().strip()
    if direction in ("LONG", "ЛОНГ", "BUY", "L"):
        direction = "LONG"
    elif direction in ("SHORT", "ШОРТ", "SELL", "S"):
        direction = "SHORT"
    else:
        _telegram_send(chat_id, "❌ Направление: <code>LONG</code> или <code>SHORT</code>")
        return

    note = parts[3].strip('"\'') if len(parts) > 3 else None
    now  = int(time.time())

    try:
        db = _get_db()
        # Check if already tracking this symbol
        existing = db.execute(
            "SELECT id FROM user_positions WHERE symbol=? AND status='open'",
            (symbol,)
        ).fetchone()
        if existing:
            db.execute(
                "UPDATE user_positions SET direction=?, note=?, ts_added=?, last_alerted=0 WHERE id=?",
                (direction, note, now, existing[0]),
            )
            action = "обновлена"
        else:
            db.execute(
                "INSERT INTO user_positions (symbol, direction, note, ts_added) VALUES (?,?,?,?)",
                (symbol, direction, note, now),
            )
            action = "добавлена"
        db.commit()
    except Exception as e:
        logger.error("handle_addpos_command: %s", e)
        _telegram_send(chat_id, "❌ Ошибка базы данных.")
        return

    dir_emoji = "📈" if direction == "LONG" else "📉"
    note_line = f"\n📝 Заметка: <i>{html.escape(note)}</i>" if note else ""
    _telegram_send(chat_id,
        f"✅ Позиция {action}!\n\n"
        f"{dir_emoji} <b>{symbol}</b> {direction}{note_line}\n\n"
        f"Бот будет мониторить и пришлёт сигнал когда появится подтверждённая коррекция."
    )


def handle_mypos_command(chat_id: int) -> None:
    """List all open personal positions."""
    try:
        db  = _get_db()
        rows = db.execute(
            "SELECT symbol, direction, note, ts_added, last_alerted "
            "FROM user_positions WHERE status='open' ORDER BY ts_added DESC"
        ).fetchall()
    except Exception as e:
        logger.error("handle_mypos_command: %s", e)
        _telegram_send(chat_id, "❌ Ошибка базы данных.")
        return

    if not rows:
        _telegram_send(chat_id,
            "📭 Нет открытых позиций.\n\n"
            "Добавить: <code>/addpos BTCUSDT SHORT</code>"
        )
        return

    now   = int(time.time())
    lines = ["📍 <b>Ваши открытые позиции:</b>\n"]
    for symbol, direction, note, ts_added, last_alerted in rows:
        dir_emoji = "📈" if direction == "LONG" else "📉"
        age_h  = (now - ts_added) // 3600
        note_s = f"  <i>{html.escape(note)}</i>" if note else ""
        alerted_s = ""
        if last_alerted:
            h = (now - last_alerted) // 3600
            alerted_s = f"  · последний алерт {h}ч назад"
        lines.append(f"{dir_emoji} <code>{symbol}</code> {direction} · {age_h}ч назад{note_s}{alerted_s}")

    lines.append(f"\nЗакрыть: <code>/closepos BTCUSDT</code>")
    _telegram_send(chat_id, "\n".join(lines))


def handle_closepos_command(chat_id: int, text: str) -> None:
    """Close (stop tracking) a personal position.
    Usage: /closepos SYMBOL
    """
    parts = text.strip().split()
    if len(parts) < 2:
        _telegram_send(chat_id, "Формат: <code>/closepos BTCUSDT</code>")
        return

    symbol = parts[1].upper().strip()
    if not symbol.endswith("USDT"):
        symbol += "USDT"

    try:
        db  = _get_db()
        cur = db.execute(
            "UPDATE user_positions SET status='closed', ts_closed=? "
            "WHERE symbol=? AND status='open'",
            (int(time.time()), symbol),
        )
        db.commit()
        affected = cur.rowcount
    except Exception as e:
        logger.error("handle_closepos_command: %s", e)
        _telegram_send(chat_id, "❌ Ошибка базы данных.")
        return

    if affected:
        _telegram_send(chat_id, f"✅ Позиция <code>{symbol}</code> закрыта и удалена из мониторинга.")
    else:
        _telegram_send(chat_id, f"❌ Открытой позиции по <code>{symbol}</code> не найдено.\n\nСписок: /mypos")


def handle_unwatch_command(chat_id: int, text: str) -> None:
    """/unwatch SYMBOL — stop watching for reversal on this symbol."""
    parts = text.strip().split()
    if len(parts) < 2:
        _telegram_send(chat_id, "Формат: <code>/unwatch BTCUSDT</code>")
        return
    sym = parts[1].upper().strip()
    if not sym.endswith("USDT"):
        sym += "USDT"
    try:
        with _db_lock:
            db = _get_db()
            n = db.execute(
                "UPDATE watchlist SET status='removed' WHERE symbol=? AND status='active'",
                (sym,)
            ).rowcount
            db.commit()
        if n:
            _telegram_send(chat_id, f"✅ <code>{sym}</code> удалён из наблюдения ({n} запись)")
        else:
            _telegram_send(chat_id, f"⚠️ <code>{sym}</code> не найден в активном наблюдении")
    except Exception as exc:
        logger.error("handle_unwatch_command: %s", exc)
        _telegram_send(chat_id, "❌ Ошибка базы данных")


# NOTE: `check_24h_pumps` was removed — it was a pure "WATCH" alert (no
# directional bias), so the LONG/SHORT-only filter in `send_alert_with_log`
# would have suppressed every fire. The same coins now surface through
# `volume_surge_short` (when volume + CRSI overbought confirm), which is the
# replacement directional pump signal. State key `last_pump_alerted` is left
# in place for backward compatibility with restored snapshots.


# ---------------------------------------------------------------------------
# Main job
# ---------------------------------------------------------------------------

_CYCLE_HARD_LIMIT = 240  # seconds — abort cycle if it runs longer than this


def run_checks():
    logger.info("Starting Binance check cycle...")
    start = time.time()
    _deadline = start + _CYCLE_HARD_LIMIT
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
        "pump_fade_alerts": 0,         # pump 24h → SHORT fade signals (shadow-only initially)
        "breakdown_alerts": 0,         # TEST: breakdown continuation SHORT
        "momentum_long_alerts": 0,     # TEST: momentum fade SHORT (pamped +10% → reversal)
        "new_listing_short_alerts": 0, # TEST: new listing pump→dump SHORT
        "streak_1h_alerts": 0,         # intraday 6h+ streak LONG/SHORT
        "vwap_rev_alerts": 0,          # VWAP reversion shadow (mean-reversion complement)
        "liq_reversal_alerts": 0,      # liquidity reversal confirmation shadow
        "bb_squeeze_alerts": 0,        # Bollinger squeeze shadow
        "ema_cross_alerts": 0,         # EMA 9/21 crossover shadow (4h)
        "whale_lsr_shift_alerts": 0,   # whale LSR flip alerts
        "errors": [],
    }

    try:
        # 1. Fetch all USDT pairs (crypto-only — strip synthetic equity/commodity)
        try:
            current_pairs = {s for s in get_all_usdt_pairs() if _is_crypto_pair(s)}
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
                # Keep a snapshot of liquid symbols for the startup ranking refresh thread
                with state_lock:
                    state["liquid_symbols"] = list(liquid_vol_map.keys())

            liquid_pairs = list(liquid_vol_map.items())

            # 4b. Send eye-catching new-listing alerts now that tickers are available
            for sym in new_listings:
                send_new_listing_alert(sym, tickers.get(sym) if tickers else None)

            # Deadline check after heavy fetches (pairs + tickers)
            if time.time() > _deadline:
                logger.warning("Cycle deadline exceeded after tickers fetch — aborting early")
                return

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

                # Deadline check after RSI/spikes (most expensive per-symbol pass)
                if time.time() > _deadline:
                    logger.warning("Cycle deadline exceeded after RSI/spikes — aborting early")
                    return

                # 6a. Standalone momentum alerts (15-min price change tiers)
                mom_sent, mom_ema_blocked = check_momentum(pct_15m_map, tickers, rsi_map)
                summary["momentum_alerts"] = mom_sent
                summary["momentum_ema_blocked"] = mom_ema_blocked

                # 6b. Standalone overheated / oversold (24h ±20% confirmed by RSI)
                oh_n, os_n, oh_ema_blocked, oh_reversal_blocked, oh_bear_blocked = check_overheated_oversold(tickers, rsi_map)
                summary["overheated_reversal_blocked"] = oh_reversal_blocked
                summary["overheated_alerts"] = oh_n
                summary["overheated_ema_blocked"] = oh_ema_blocked
                summary["oversold_alerts"] = os_n
                summary["bear_downtrend_long_blocked"] = (
                    summary.get("bear_downtrend_long_blocked", 0) + oh_bear_blocked
                )

                # 6c. Volume surge (+300% daily vol) + CRSI extreme combo
                # (Pre-refresh pass — catches surges seen by the previous hour's ranking.)
                summary["vol_surge_alerts"] = check_volume_surge_crsi(tickers) if VOL_SURGE_ENABLED else 0
                # pump_24h_fade: runs always (shadow=True collects data; shadow=False sends alerts)
                summary["pump_fade_alerts"] = check_pump_24h_fade(tickers, rsi_map)

                # 6d. TEST: Breakdown continuation SHORT (drop ≥10% + below EMA + RSI 30-58)
                summary["breakdown_alerts"] = check_breakdown_short(tickers, rsi_map) if BREAKDOWN_ENABLED else 0

                # 6e. TEST: Momentum continuation LONG (rise ≥10% + above EMA + RSI 42-68)
                summary["momentum_long_alerts"] = check_momentum_long(tickers, rsi_map) if MOMENTUM_LONG_ENABLED else 0

                # 6f. TEST: New listing pump→dump SHORT (≥+20% from listing + RSI≥65)
                summary["new_listing_short_alerts"] = check_new_listing_pumps(tickers, rsi_map) if NEW_LISTING_ENABLED else 0

                # 6g. listing_dump_long disabled — backtest 30d: 0 TP of 4 signals, 50% SL
                summary["listing_dump_long_alerts"] = 0

                # 6h. TEST: New listing ATH peak → SHORT long-duration (pump≥30% + RSI≥70)
                summary["listing_peak_short_alerts"] = check_listing_peak_short(tickers, rsi_map) if LISTING_PEAK_ENABLED else 0

                # 6i. Intraday hourly streak — 6+ consecutive green/red 1h candles
                summary["streak_1h_alerts"] = check_intraday_streak(tickers, rsi_map) if STREAK_1H_ENABLED else 0

                # 6i-shadow. VWAP reversion — mean-reversion counterpart to streak_1h
                # (shadow-only, controlled by VWAP_REVERSION_SHADOW_ONLY)
                if STREAK_1H_ENABLED:
                    try:
                        summary["vwap_rev_alerts"] = check_vwap_reversion(tickers, rsi_map)
                    except Exception as _e:
                        logger.error("check_vwap_reversion failed: %s", _e)
                        summary["errors"].append(f"vwap_rev: {_e}")

                # 6i-shadow-2. Bollinger Squeeze — squeeze → 1h breakout (shadow)
                try:
                    summary["bb_squeeze_alerts"] = check_bollinger_squeeze(tickers, rsi_map)
                except Exception as _e:
                    logger.error("check_bollinger_squeeze failed: %s", _e)
                    summary["errors"].append(f"bb_squeeze: {_e}")

                # 6i-shadow-3. EMA 9/21 crossover with gap threshold on 4h (shadow)
                try:
                    summary["ema_cross_alerts"] = check_ema_crossover(tickers)
                except Exception as _e:
                    logger.error("check_ema_crossover failed: %s", _e)
                    summary["errors"].append(f"ema_cross: {_e}")

                # 6j. Whale LSR shift — top traders flip L/S ratio significantly in one cycle
                summary["whale_lsr_shift_alerts"] = check_whale_lsr_shift(liquid_pairs) if WHALE_LSR_ENABLED else 0

                # 6k. Personal position tracker — check user's own open positions
                check_user_position_reversals()

                # 7. Refresh stored 7d/30d highs + EMA-200 (4h) once per hour
                # Skip heavy refresh passes if we're already close to the deadline
                if time.time() > _deadline:
                    logger.warning("Cycle deadline exceeded before weekly/EMA refresh — aborting early")
                    return

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
            # btc_pct24 for confluence BTC filter
            btc_pct24: float | None = None
            if tickers:
                btc_t = tickers.get("BTCUSDT")
                if btc_t:
                    try:
                        btc_pct24 = float(btc_t["priceChangePercent"])
                    except (ValueError, KeyError, TypeError):
                        btc_pct24 = None
            # Count open confluence real positions once before the loop
            _cf_open_count = 0
            try:
                with _db_lock:
                    _cf_open_count = _get_db().execute(
                        "SELECT COUNT(*) FROM demo_positions "
                        "WHERE alert_type='confluence' AND status='open' AND is_shadow=0"
                        " AND ts_open >= ?",
                        (CONFLUENCE_CAP_CUTOFF_TS,),
                    ).fetchone()[0] or 0
            except Exception:
                pass
            _cf_opened_this_cycle = 0   # tracks positions opened in this cycle

            for sym, b in buckets.items():
                if not CONFLUENCE_ENABLED:
                    continue  # CONFLUENCE_ENABLED=False: confluence заблокирован
                if len(b["lines"]) < CONFLUENCE_MIN_SIGNALS:
                    summary["single_signals_skipped"] += 1
                    logger.debug("Skipped single-signal coin %s: %s", sym, sorted(b["flags"]))
                    continue

                ema = ema_map.get(sym)
                above_ema: bool | None = None
                if ema is not None and b["price"] is not None:
                    above_ema = b["price"] > ema

                # Направление определяется трендом монеты (EMA-200):
                # выше EMA → LONG, ниже EMA → SHORT.
                # Если EMA не загружена — fallback на make_recommendation().
                if above_ema is True:
                    rec_label = "LONG"
                    rec_line = "📊 Рекомендация: ЛОНГ 📈"
                    reason_line = "Причина: монета выше EMA-200 (4ч) — восходящий тренд"
                elif above_ema is False:
                    rec_label = "SHORT"
                    rec_line = "📊 Рекомендация: ШОРТ 📉"
                    reason_line = "Причина: монета ниже EMA-200 (4ч) — нисходящий тренд"
                else:
                    rec_line, reason_line = make_recommendation(
                        rsi=b["rsi"],
                        spike_ratio=b["spike_ratio"],
                        broke_weekly=b["broke_weekly"],
                        broke_monthly=b["broke_monthly"],
                        near_24h_high=b["near_24h_high"],
                        above_ema200=above_ema,
                    )
                    rec_label = _rec_label(rec_line)

                # ── Volume filter: skip dead/illiquid pairs early ────────────────
                _cf_vol24 = float((tickers.get(sym) or {}).get("quoteVolume", 0)) if tickers else 0.0
                if _cf_vol24 < MIN_VOLUME_CONFLUENCE:
                    logger.debug(
                        "Confluence %s skipped: low volume $%.0f < $%.0f",
                        sym, _cf_vol24, MIN_VOLUME_CONFLUENCE,
                    )
                    summary["single_signals_skipped"] += 1
                    if b["price"]:
                        with state_lock:
                            _atr_lv24 = state["atr_4h"].get(sym)
                        _dsl_lv24, _dtp_lv24 = _compute_confluence_sl_tp(rec_label, b["price"], _atr_lv24)
                        if _dsl_lv24 and _dtp_lv24:
                            _demo_open_position(
                                sym, rec_label, b["price"], _dsl_lv24, _dtp_lv24,
                                is_shadow=True,
                                shadow_reason=f"low_volume: ${_cf_vol24:,.0f}",
                                alert_type="confluence",
                            )
                    continue

                # Bear downtrend filter for LONG signals.
                # ОТКЛЮЧЁН: демо-позиции показали 90% win rate у теней —
                # фильтр убивал отскоки от дна. Включить: BEAR_DOWNTREND_FILTER_ENABLED = True
                if BEAR_DOWNTREND_FILTER_ENABLED and rec_label == "LONG":
                    _block_bd, _bd_days = _bear_downtrend_blocks_long(sym)
                    if _block_bd:
                        logger.info(
                            "Confluence LONG suppressed by bear-downtrend filter: "
                            "%s downtrend=%dd BEAR market", sym, _bd_days,
                        )
                        summary["confluence_bear_long_blocked"] = (
                            summary.get("confluence_bear_long_blocked", 0) + 1
                        )
                        if b["price"]:
                            with state_lock:
                                _atr_bd = state["atr_4h"].get(sym)
                            _dsl, _dtp = _compute_confluence_sl_tp("LONG", b["price"], _atr_bd)
                            if _dsl and _dtp:
                                _bd_body = (
                                    f"<b>🚨 КОНФЛЮЭНЦИЯ: <code>{sym}</code></b>\n"
                                    f"💰 Цена: ${b['price']:,.6g}\n"
                                    + "".join(f"  • {ln}\n" for ln in b.get("lines", []))
                                    + f"📊 Рекомендация: <b>LONG 📈</b>\n"
                                    f"🟢 TP: <b>${_dtp:,.6g}</b>  │  🔴 SL: <b>${_dsl:,.6g}</b>\n"
                                    f"🔒 Блок: медвежий тренд {_bd_days} дн. подряд"
                                )
                                _demo_open_position(
                                    sym, "LONG", b["price"], _dsl, _dtp,
                                    is_shadow=True,
                                    shadow_reason=f"bear_downtrend_{_bd_days}d",
                                    alert_type="confluence",
                                    notify_body=_bd_body,
                                )
                        continue

                # BTC direction filter — CONFLUENCE_BTC_FILTER_ENABLED=False → выкл
                # Торгуем по тренду монеты (EMA-200), а не по тренду BTC
                btc_rsi = rsi_map.get("BTCUSDT")
                if CONFLUENCE_BTC_FILTER_ENABLED:
                    if rec_label == "SHORT":
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
                # Funding + LSR adjustment
                _fl_delta_cf, _fl_text_cf, _fl_fpts_cf, _fl_lpts_cf = (
                    _funding_lsr_score_and_text(sym, side_for_score)
                )
                score = max(0, min(100, score + _fl_delta_cf))

                # --- LONG quality filters ---
                if rec_label == "LONG":
                    # 1. Score gate: skip weak-signal LONGs
                    if score < CONFLUENCE_LONG_MIN_SCORE:
                        logger.debug(
                            "Confluence LONG skipped (score %d < %d): %s",
                            score, CONFLUENCE_LONG_MIN_SCORE, sym,
                        )
                        summary["single_signals_skipped"] += 1
                        continue
                    # 2. Stablecoin / dormant-pair filter: if 24h move is tiny
                    #    the coin is pegged or dead — not a real trade candidate.
                    if tickers:
                        t_sym = tickers.get(sym)
                        if t_sym is not None:
                            try:
                                pct24_sym = float(t_sym["priceChangePercent"])
                                if abs(pct24_sym) < 1.5:
                                    logger.debug(
                                        "Confluence LONG skipped (stablecoin/dormant "
                                        "24h=%.2f%%): %s", pct24_sym, sym,
                                    )
                                    summary["single_signals_skipped"] += 1
                                    continue
                            except (ValueError, KeyError, TypeError):
                                pass

                # --- SHORT score gate (symmetric with LONG) ---
                if rec_label == "SHORT":
                    if score < CONFLUENCE_SHORT_MIN_SCORE:
                        logger.debug(
                            "Confluence SHORT skipped (score %d < %d): %s",
                            score, CONFLUENCE_SHORT_MIN_SCORE, sym,
                        )
                        summary["single_signals_skipped"] += 1
                        continue

                # ── RSI quality gate ──────────────────────────────────────────────
                # LONG only when genuinely oversold (RSI ≤ CONFLUENCE_RSI_MAX_LONG=45)
                # SHORT only when genuinely overbought (RSI ≥ CONFLUENCE_RSI_MIN_SHORT=65)
                if b.get("rsi") is not None:
                    _rsi_val = b["rsi"]
                    if rec_label == "LONG" and _rsi_val > CONFLUENCE_RSI_MAX_LONG:
                        logger.debug(
                            "Confluence LONG skipped: RSI %.1f > %d: %s",
                            _rsi_val, CONFLUENCE_RSI_MAX_LONG, sym,
                        )
                        summary["single_signals_skipped"] += 1
                        if b["price"]:
                            with state_lock:
                                _atr_rsi = state["atr_4h"].get(sym)
                            _dsl_rsi, _dtp_rsi = _compute_confluence_sl_tp("LONG", b["price"], _atr_rsi)
                            if _dsl_rsi and _dtp_rsi:
                                _demo_open_position(
                                    sym, "LONG", b["price"], _dsl_rsi, _dtp_rsi,
                                    is_shadow=True,
                                    shadow_reason=(
                                        f"rsi_filter: rsi={_rsi_val:.0f}>{CONFLUENCE_RSI_MAX_LONG}"
                                    ),
                                    alert_type="confluence",
                                )
                        continue
                    if rec_label == "SHORT" and _rsi_val < CONFLUENCE_RSI_MIN_SHORT:
                        logger.debug(
                            "Confluence SHORT skipped: RSI %.1f < %d: %s",
                            _rsi_val, CONFLUENCE_RSI_MIN_SHORT, sym,
                        )
                        summary["single_signals_skipped"] += 1
                        if b["price"]:
                            with state_lock:
                                _atr_rsi = state["atr_4h"].get(sym)
                            _dsl_rsi, _dtp_rsi = _compute_confluence_sl_tp("SHORT", b["price"], _atr_rsi)
                            if _dsl_rsi and _dtp_rsi:
                                _demo_open_position(
                                    sym, "SHORT", b["price"], _dsl_rsi, _dtp_rsi,
                                    is_shadow=True,
                                    shadow_reason=(
                                        f"rsi_filter: rsi={_rsi_val:.0f}<{CONFLUENCE_RSI_MIN_SHORT}"
                                    ),
                                    alert_type="confluence",
                                )
                        continue

                # ── Trend pump/dump filter ───────────────────────────────────────
                # SHORT+памп: блокировать (PUMP_FILTER_SHORT_ENABLED=True)
                # LONG+дамп:  не блокировать (PUMP_FILTER_LONG_ENABLED=False, 57% win rate у теней)
                _ai_note = ""
                _pump_filter_active = (
                    (PUMP_FILTER_SHORT_ENABLED and rec_label == "SHORT") or
                    (PUMP_FILTER_LONG_ENABLED  and rec_label == "LONG")
                )
                if _pump_filter_active and b["price"] is not None:
                    _pump_blocked, _pump_reason = _trend_pump_filter(
                        sym, rec_label, b["price"]
                    )
                    if _pump_blocked:
                        logger.info(
                            "Confluence %s %s blocked by pump filter: %s",
                            sym, rec_label, _pump_reason,
                        )
                        summary["confluence_ema_blocked"] += 1
                        with state_lock:
                            _atr_p = state["atr_4h"].get(sym)
                        _dsl, _dtp = _compute_confluence_sl_tp(rec_label, b["price"], _atr_p)
                        if _dsl and _dtp:
                            _pf_body = (
                                f"<b>🚨 КОНФЛЮЭНЦИЯ: <code>{sym}</code></b>\n"
                                f"💰 Цена: ${b['price']:,.6g}\n"
                                + "".join(f"  • {ln}\n" for ln in b.get("lines", []))
                                + f"📊 Рекомендация: <b>{'LONG 📈' if rec_label == 'LONG' else 'SHORT 📉'}</b>\n"
                                f"🎯 Сила сигнала: <b>{score}/100</b>\n"
                                f"🟢 TP: <b>${_dtp:,.6g}</b>  │  🔴 SL: <b>${_dsl:,.6g}</b>\n"
                                f"🔒 Блок: {_pump_reason}"
                            )
                            _demo_open_position(
                                sym, rec_label, b["price"], _dsl, _dtp,
                                is_shadow=True,
                                shadow_reason=f"pump_filter: {_pump_reason}",
                                alert_type="confluence",
                                notify_body=_pf_body,
                            )
                        continue

                # ── Ликвидационное вето ──────────────────────────────────────────
                if b["price"] is not None:
                    _vol24_cf = float((tickers.get(sym) or {}).get("quoteVolume", 0)) if tickers else 0.0
                    _liq_veto_cf, _liq_reason_cf = _check_liq_veto(sym, side_for_score, _vol24_cf)
                    if _liq_veto_cf:
                        logger.info(
                            "SUPPRESSED confluence %s %s — ликвидации: %s",
                            sym, rec_label, _liq_reason_cf,
                        )
                        summary["confluence_ema_blocked"] = (
                            summary.get("confluence_ema_blocked", 0) + 1
                        )
                        with state_lock:
                            _atr_lv = state["atr_4h"].get(sym)
                        _dsl_lv3, _dtp_lv3 = _compute_confluence_sl_tp(rec_label, b["price"], _atr_lv)
                        if _dsl_lv3 and _dtp_lv3:
                            _lv_body = (
                                f"<b>🚨 КОНФЛЮЭНЦИЯ: <code>{sym}</code></b>\n"
                                f"💰 Цена: ${b['price']:,.6g}\n"
                                + "".join(f"  • {ln}\n" for ln in b.get("lines", []))
                                + f"📊 Рекомендация: <b>{'LONG 📈' if rec_label == 'LONG' else 'SHORT 📉'}</b>\n"
                                f"🎯 Сила сигнала: <b>{score}/100</b>\n"
                                f"🟢 TP: <b>${_dtp_lv3:,.6g}</b>  │  🔴 SL: <b>${_dsl_lv3:,.6g}</b>\n"
                                f"🔒 Блок: ликвидации — {_liq_reason_cf}"
                            )
                            _demo_open_position(
                                sym, rec_label, b["price"], _dsl_lv3, _dtp_lv3,
                                is_shadow=True,
                                shadow_reason=f"liq_veto: {_liq_reason_cf}",
                                alert_type="confluence",
                                notify_body=_lv_body,
                            )
                        continue
                # ── AI veto (AI_VETO_ENABLED=False → отключён полностью) ─────────
                _pct24_v: float | None = None
                if tickers:
                    _tv = tickers.get(sym)
                    if _tv:
                        try:
                            _pct24_v = float(_tv["priceChangePercent"])
                        except (ValueError, KeyError, TypeError):
                            pass
                _ai_allow, _ai_note, _ai_hold = True, "", ""
                if AI_VETO_ENABLED and rec_label == "LONG":
                    _ai_allow, _ai_note, _ai_hold = _ai_veto_confluence(
                        sym, rec_label, b["rsi"], _pct24_v, above_ema
                    )
                if not _ai_allow:
                    logger.info(
                        "SUPPRESSED: %s %s, причина ИИ: %s",
                        sym, rec_label, _ai_note,
                    )
                    summary["confluence_ai_blocked"] = (
                        summary.get("confluence_ai_blocked", 0) + 1
                    )
                    if b["price"]:
                        with state_lock:
                            _atr_v = state["atr_4h"].get(sym)
                        _dsl, _dtp = _compute_confluence_sl_tp(rec_label, b["price"], _atr_v)
                        if _dsl and _dtp:
                            _av_body = (
                                f"<b>🚨 КОНФЛЮЭНЦИЯ: <code>{sym}</code></b>\n"
                                f"💰 Цена: ${b['price']:,.6g}\n"
                                + "".join(f"  • {ln}\n" for ln in b.get("lines", []))
                                + f"📊 Рекомендация: <b>{'LONG 📈' if rec_label == 'LONG' else 'SHORT 📉'}</b>\n"
                                f"🎯 Сила сигнала: <b>{score}/100</b>\n"
                                f"🟢 TP: <b>${_dtp:,.6g}</b>  │  🔴 SL: <b>${_dsl:,.6g}</b>\n"
                                f"🔒 Блок: ИИ — {_ai_note}"
                            )
                            _demo_open_position(
                                sym, rec_label, b["price"], _dsl, _dtp,
                                is_shadow=True,
                                shadow_reason=f"ai_veto: {_ai_note}",
                                alert_type="confluence",
                                notify_body=_av_body,
                            )
                    continue

                # ── Uptrend flip: block SHORT if ≥ X of last N 4h-candles are bullish ──
                # Uses X-of-N rule (not consecutive) to catch "pulsing" uptrends where
                # the coin rises with occasional pullback candles.  Threshold chosen by
                # back-test on Aug 10-13 data (63 SL, 13 TP):
                #   4of6 → blocks 27% SL, keeps 92% TP, precision 94% ← deployed
                #   5of6 → blocks 10% SL only (too conservative)
                #   consec≥3 → blocks 13% SL (original disabled logic, too strict)
                _uptrend_candles_cf = 0
                if UPTREND_FLIP_MIN_CANDLES > 0 and rec_label == "SHORT":
                    try:
                        _4h_flip = _gateio_klines(sym, UPTREND_FLIP_INTERVAL,
                                                   UPTREND_FLIP_N_WINDOW + 2)
                        if _4h_flip:
                            _4h_closes_flip = [float(k[4]) for k in _4h_flip[:-1]]  # drop live
                            _uptrend_candles_cf = _count_up_in_window(
                                _4h_closes_flip, UPTREND_FLIP_N_WINDOW
                            )
                    except Exception:
                        pass
                if UPTREND_FLIP_MIN_CANDLES > 0 and _uptrend_candles_cf >= UPTREND_FLIP_MIN_CANDLES:
                    logger.info(
                        "Confluence SHORT %s blocked — %d/%d %s candles bullish",
                        sym, _uptrend_candles_cf, UPTREND_FLIP_N_WINDOW, UPTREND_FLIP_INTERVAL,
                    )
                    summary["confluence_ema_blocked"] = (
                        summary.get("confluence_ema_blocked", 0) + 1
                    )
                    # Shadow the original SHORT for win-rate tracking
                    with state_lock:
                        _atr_flip = state["atr_4h"].get(sym)
                    if b["price"]:
                        _dsl_flip, _dtp_flip = _compute_confluence_sl_tp(
                            "SHORT", b["price"], _atr_flip
                        )
                        if _dsl_flip and _dtp_flip:
                            _demo_open_position(
                                sym, "SHORT", b["price"], _dsl_flip, _dtp_flip,
                                is_shadow=True,
                                shadow_reason=(
                                    f"uptrend_flip: {_uptrend_candles_cf}of{UPTREND_FLIP_N_WINDOW}x{UPTREND_FLIP_INTERVAL}"
                                ),
                                alert_type="confluence",
                            )
                    # Recalculate funding/LSR for LONG direction
                    _fl_delta_flip, _fl_text_flip, _fl_fpts_flip, _fl_lpts_flip = (
                        _funding_lsr_score_and_text(sym, "buy")
                    )
                    _flip_score = min(score + _fl_delta_flip, 100)
                    _flip_sl_tp = _format_sl_tp("buy", b["price"], _atr_flip, score=_flip_score)
                    _flip_whale = _get_gate_whale_stats(sym)
                    _hours_up = _uptrend_candles_cf * (
                        4 if UPTREND_FLIP_INTERVAL == "4h" else
                        6 if UPTREND_FLIP_INTERVAL == "6h" else
                        1
                    )
                    _flip_lines: list[str] = [
                        f"<b>🚨 КОНФЛЮЭНЦИЯ СИГНАЛОВ: <code>{sym}</code></b>",
                    ]
                    if b["price"] is not None:
                        _flip_lines.append(f"Цена: <b>${b['price']:,.6g}</b>")
                    if b["volume"] is not None:
                        _flip_lines.append(f"Объём за 24ч: ${b['volume']:,.0f}")
                    _flip_lines += [
                        "",
                        f"<b>Сработавшие сигналы ({len(b['lines'])}):</b>",
                        *[f"  • {ln}" for ln in b["lines"]],
                        "",
                        f"📊 Рекомендация: <b>ЛОНГ 📈</b>",
                        (
                            f"Причина: устойчивый тренд вверх "
                            f"<b>{_uptrend_candles_cf} свечей ({_hours_up}ч)</b> — "
                            f"сигналы перекупленности не блокируют импульс, тренд продолжается"
                        ),
                        f"🎯 Сила сигнала: <b>{_flip_score}/100</b> ({_strength_label(_flip_score)})",
                    ]
                    if _flip_sl_tp:
                        _flip_lines.append(_flip_sl_tp)
                    if _fl_text_flip:
                        _flip_lines.append(_fl_text_flip)
                    if _flip_whale:
                        _flip_lines.append(_flip_whale)
                    if _ai_note:
                        _flip_lines.append(
                            f"🤖 ИИ подтвердил: <i>{html.escape(_ai_note)}</i>"
                        )
                    _flip_body = "\n".join(_flip_lines)
                    _delivered_flip, _ = send_alert_with_log(
                        sym, "confluence", "LONG", b["price"], _flip_body, _flip_score,
                        rsi=b.get("rsi"), pct24=_pct24_v,
                        factor_funding_pts=_fl_fpts_flip, factor_lsr_pts=_fl_lpts_flip,
                    )
                    if _delivered_flip and b["price"]:
                        _dsl_f2, _dtp_f2 = _compute_confluence_sl_tp(
                            "LONG", b["price"], _atr_flip
                        )
                        if _dsl_f2 and _dtp_f2:
                            _demo_open_position(
                                sym, "LONG", b["price"], _dsl_f2, _dtp_f2,
                                alert_type="confluence",
                                score=_flip_score,
                            )
                        with state_lock:
                            for key, s in b["cooldowns"]:
                                state[key][s] = now_ts
                    continue

                # ── Confluence position cap (last gate before real signal) ──────
                _cf_total_now = _cf_open_count + _cf_opened_this_cycle
                if _cf_total_now >= MAX_OPEN_CONFLUENCE_POSITIONS:
                    logger.info(
                        "Confluence %s %s cap-blocked (%d/%d open)",
                        sym, rec_label, _cf_total_now, MAX_OPEN_CONFLUENCE_POSITIONS,
                    )
                    summary["confluence_cap_blocked"] = (
                        summary.get("confluence_cap_blocked", 0) + 1
                    )
                    if b["price"]:
                        with state_lock:
                            _atr_cap = state["atr_4h"].get(sym)
                        _dsl_cap, _dtp_cap = _compute_confluence_sl_tp(rec_label, b["price"], _atr_cap)
                        if _dsl_cap and _dtp_cap:
                            _demo_open_position(
                                sym, rec_label, b["price"], _dsl_cap, _dtp_cap,
                                is_shadow=True,
                                shadow_reason=(
                                    f"confluence_cap: {_cf_total_now}/{MAX_OPEN_CONFLUENCE_POSITIONS}"
                                ),
                                alert_type="confluence",
                                notify_body=(
                                    f"🚫 Confluence <b>{'LONG 📈' if rec_label == 'LONG' else 'SHORT 📉'}</b>"
                                    f" <code>{sym}</code> (слот занят)\n"
                                    f"Цена: <b>${b['price']:,.6g}</b>  |  Сила: {score}/100"
                                    f"  |  Открыто: {_cf_total_now}/{MAX_OPEN_CONFLUENCE_POSITIONS}\n"
                                    f"🟢 TP: <b>${_dtp_cap:,.6g}</b>  │  🔴 SL: <b>${_dsl_cap:,.6g}</b>"
                                ),
                            )
                    continue

                header = [f"<b>🚨 КОНФЛЮЭНЦИЯ СИГНАЛОВ: <code>{sym}</code></b>"]
                if b["price"] is not None:
                    header.append(f"Цена: <b>${b['price']:,.6g}</b>")
                if b["volume"] is not None:
                    header.append(f"Объём за 24ч: ${b['volume']:,.0f}")
                with state_lock:
                    atr = state["atr_4h"].get(sym)
                sl_tp = _format_sl_tp(side_for_score, b["price"], atr, score=score)
                _trend_warn = _trend_warning_line(sym, rec_label)
                # ── Whale stats (fetched after veto passes, no wasted calls) ──
                _whale_cf = _get_gate_whale_stats(sym)
                _whale_alert_cf = _get_whale_alert_line(sym)
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
                if _trend_warn:
                    body_lines.append(_trend_warn)
                if _fl_text_cf:
                    body_lines.append(_fl_text_cf)
                if _whale_cf:
                    body_lines.append(_whale_cf)
                if _whale_alert_cf:
                    body_lines.append(_whale_alert_cf)
                if _ai_hold:
                    body_lines.append(f"⏳ {html.escape(_ai_hold)}")
                if _ai_note:
                    body_lines.append(
                        f"🤖 ИИ подтвердил: <i>{html.escape(_ai_note)}</i>"
                    )
                body = "\n".join(body_lines)
                delivered, _aid = send_alert_with_log(
                    sym, "confluence", rec_label, b["price"], body, score,
                    rsi=b.get("rsi"), pct24=_pct24_v,
                    factor_funding_pts=_fl_fpts_cf, factor_lsr_pts=_fl_lpts_cf,
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
                # Open real demo position for this delivered signal
                if b["price"]:
                    with state_lock:
                        _atr_d = state["atr_4h"].get(sym)
                    _dsl, _dtp = _compute_confluence_sl_tp(rec_label, b["price"], _atr_d)
                    if _dsl and _dtp:
                        _demo_open_position(
                            sym, rec_label, b["price"], _dsl, _dtp,
                            alert_type="confluence",
                            score=score,
                        )
                        _cf_opened_this_cycle += 1   # track against cap

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

            # 11. Update in-memory price cache from tickers snapshot
            if tickers:
                global _price_cache, _price_cache_ts
                new_cache: dict[str, float] = {}
                for sym, t in tickers.items():
                    try:
                        new_cache[sym] = float(t["lastPrice"])
                    except (ValueError, KeyError):
                        pass
                # Also populate Binance-style 'B'-suffix variants for active
                # monitors (e.g. KORUBUSDT → KORU_USDT on Gate.io).
                # This prevents the positions endpoint from making a live
                # API call on every request for these symbols.
                with _monitors_lock:
                    monitored_syms = {m.symbol for m in _active_monitors.values()}
                for msym in monitored_syms:
                    if msym not in new_cache and msym.endswith("BUSDT") and len(msym) > 6:
                        alt = msym[:-5] + "USDT"   # KORUBUSDT → KORUSDT
                        if alt in new_cache:
                            new_cache[msym] = new_cache[alt]
                with _price_cache_lock:
                    _price_cache = new_cache
                    _price_cache_ts = time.time()

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
                    f"<b>Команды мониторинга:</b>\n"
                    f"  /status · /top10 · /top30 · /signal · /stats\n\n"
                    f"<b>Команды торговли:</b>\n"
                    f"  /trade · /mytrades · /positions · /analyze · /ai\n\n"
                    f"<b>Демо P&L:</b>\n"
                    f"  /demo\n\n"
                    f"<b>Личный трекер позиций:</b>\n"
                    f"  /addpos SYMBOL LONG|SHORT — добавить позицию\n"
                    f"  /mypos — список открытых позиций\n"
                    f"  /closepos SYMBOL — закрыть позицию\n\n"
                    f"<b>Настройки:</b>\n"
                    f"  /silence · /unmute"
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
MONITOR_TIMEOUT_HOURS = 4           # hours until forced timeout (SHORT scalp fallback)
LONG_HOLD_TIMEOUT_HOURS = 48        # LONG hold window — backtest: TP hits mostly at 1d–3d
MONITOR_SLIPPAGE_PCT  = 5.0         # re-verify if price moves >5 % in one tick

_active_monitors: dict[int, "PositionMonitor"] = {}
_monitors_lock = threading.Lock()

# In-memory price cache — updated every check cycle from the tickers snapshot.
# Used by api_positions() so it never makes individual Binance calls per position.
_price_cache: dict[str, float] = {}
_price_cache_lock = threading.Lock()
_price_cache_ts: float = 0.0   # unix timestamp of last update


def _get_current_price(symbol: str) -> float | None:
    """Fetch the latest price from Gate.io Futures.

    Falls back to stripping a trailing 'B' before 'USDT' for symbols that were
    originally opened on Binance (e.g. KORUBUSDT → KORU_USDT on Gate.io).
    """
    def _try(sym: str) -> float | None:
        try:
            resp = _gateio_get("/tickers", params={"contract": _to_gate(sym)}, timeout=6)
            data = resp.json()
            if data and float(data[0].get("last", 0)):
                return float(data[0]["last"])
        except Exception as exc:
            logger.debug("_get_current_price %s failed: %s", sym, exc)
        return None

    price = _try(symbol)
    if price is not None:
        return price
    # Binance symbols like KORUBUSDT → Gate.io KORU_USDT (drop 'B' before USDT)
    if symbol.endswith("BUSDT") and len(symbol) > 6:
        return _try(symbol[:-5] + "USDT")
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
    roi_pct = pnl_pct * DISPLAY_LEVERAGE
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
            f"ROI ×{DISPLAY_LEVERAGE}: <b>{pnl_sign}{roi_pct:.1f}%</b>  "
            f"<i>({pnl_sign}{pnl_pct:.2f}% цена)</i>\n"
            f"Время удержания: <b>{elapsed_str}</b> (лимит)\n"
            f"<i>Закрыта по таймауту</i>"
        )
    else:
        body = (
            f"{emoji} <b>{header}</b>\n"
            f"{direction} <code>{symbol}</code>\n"
            f"Вход: <b>${entry:,.6g}</b>  →  Выход: <b>${exit_price:,.6g}</b>\n"
            f"ROI ×{DISPLAY_LEVERAGE}: <b>{pnl_sign}{roi_pct:.1f}%</b>  "
            f"<i>({pnl_sign}{pnl_pct:.2f}% цена)</i>\n"
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
        timeout_hours: float = MONITOR_TIMEOUT_HOURS,
    ) -> None:
        super().__init__(name=f"pos-mon-{position_id}", daemon=True)
        self.position_id   = position_id
        self.alert_id      = alert_id
        self.symbol        = symbol
        self.direction     = direction
        self.entry         = entry
        self.sl_price      = sl_price
        self.tp_price      = tp_price
        self.ts_open       = ts_open
        self.timeout_hours = timeout_hours
        self._stop_evt     = threading.Event()

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
                if result is None and elapsed >= self.timeout_hours * 3600:
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
        # Атомарная проверка + обновление: защита от двойного уведомления при
        # перезапуске бота. Если старый поток успел закрыть позицию до рестарта,
        # новый восстановленный поток увидит status != 'open' и выйдет без уведомления.
        #
        # ВАЖНО: уведомление отправляется ТОЛЬКО после успешной записи в БД.
        # Если запись упала (например, lock занят при рестарте), уведомление не идёт —
        # это предотвращает дубли вида "старый поток не записал, но уведомил".
        already_closed = False
        db_written = False
        try:
            with _db_lock:
                conn = _get_db()
                cur_row = conn.execute(
                    "SELECT status FROM position_monitors WHERE id=?",
                    (self.position_id,),
                ).fetchone()
                if cur_row and cur_row[0] != "open":
                    already_closed = True
                    logger.warning(
                        "PositionMonitor %d %s: already closed (status=%s) — "
                        "skipping duplicate exit notification",
                        self.position_id, self.symbol, cur_row[0],
                    )
                else:
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
                    db_written = True  # запись прошла — только теперь шлём уведомление
        except Exception as exc:
            logger.warning(
                "PositionMonitor %d %s: DB update failed (%s) — "
                "notification suppressed to prevent duplicate",
                self.position_id, self.symbol, exc,
            )

        if already_closed or not db_written:
            with _monitors_lock:
                _active_monitors.pop(self.position_id, None)
            return

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
    *,
    sl_pct: float | None = None,
    tp_pct: float | None = None,
    timeout_hours: float | None = None,
) -> None:
    """Compute TP/SL and launch a PositionMonitor.

    sl_pct / tp_pct / timeout_hours override the per-direction defaults —
    pass explicit values for special trades (e.g. listing_peak_short uses
    SL=15%, TP=60%, 720h).

    Direction defaults (backtest-driven, see SHORT_HOLD_* constants):
    - LONG:  4h scalp, SL=HIT_RATE_SL_PCT, TP=HIT_RATE_TP_PCT
    - SHORT: 7-day hold, SL=SHORT_HOLD_SL_PCT, TP=SHORT_HOLD_TP_PCT
    """
    if os.environ.get("TESTING", "").strip().lower() in {"1", "true", "yes"}:
        return
    if direction not in ("LONG", "SHORT"):
        return
    # One active position per symbol + global cap
    with _monitors_lock:
        open_count = len(_active_monitors)
        open_symbols = {m.symbol for m in _active_monitors.values()}
        open_by_dir = sum(
            1 for m in _active_monitors.values() if m.direction == direction
        )
    if open_count >= MAX_OPEN_POSITIONS:
        logger.info(
            "_auto_start_monitor blocked: cap reached (%d/%d open)", open_count, MAX_OPEN_POSITIONS,
        )
        return
    # Per-direction quota: long-hold SHORTs (7-30d) must not starve
    # short-lived LONG scalps of monitor slots (and vice versa).
    dir_cap = (
        MAX_OPEN_SHORT_POSITIONS if direction == "SHORT" else MAX_OPEN_LONG_POSITIONS
    )
    if open_by_dir >= dir_cap:
        logger.info(
            "_auto_start_monitor blocked: %s quota reached (%d/%d open %s, %d/%d total)",
            direction, open_by_dir, dir_cap, direction, open_count, MAX_OPEN_POSITIONS,
        )
        return
    if symbol in open_symbols:
        logger.info(
            "_auto_start_monitor blocked: %s already has an open position monitor", symbol,
        )
        return

    # Resolve SL/TP/timeout (explicit override or per-direction defaults).
    # SHORT positions default to the long-hold regime (7d, wide SL/TP) —
    # backtest showed +7…+10% at 3–7d vs −0.05% on the old 4h scalp.
    if direction == "SHORT":
        _sl = sl_pct if sl_pct is not None else SHORT_HOLD_SL_PCT
        _tp = tp_pct if tp_pct is not None else SHORT_HOLD_TP_PCT
        timeout_hours = (
            timeout_hours if timeout_hours is not None else SHORT_HOLD_TIMEOUT_HOURS
        )
    else:
        _sl = sl_pct if sl_pct is not None else HIT_RATE_SL_PCT
        _tp = tp_pct if tp_pct is not None else HIT_RATE_TP_PCT
        # LONG positions hold up to 48h — backtest shows TP hits mostly at 1d–3d;
        # the old 4h timeout was cutting positions before they had time to recover.
        timeout_hours = (
            timeout_hours if timeout_hours is not None else LONG_HOLD_TIMEOUT_HOURS
        )
    if direction == "LONG":
        sl_price = entry * (1 - _sl / 100)
        tp_price = entry * (1 + _tp / 100)
    else:
        sl_price = entry * (1 + _sl / 100)
        tp_price = entry * (1 - _tp / 100)
    ts_open = int(time.time())

    try:
        with _db_lock:
            conn = _get_db()
            cur = conn.execute(
                "INSERT INTO position_monitors "
                "(alert_id, ts_open, symbol, direction, "
                "entry_price, sl_price, tp_price, status, timeout_hours) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)",
                (alert_id, ts_open, symbol, direction,
                 entry, sl_price, tp_price, timeout_hours),
            )
            conn.commit()
            pos_id = int(cur.lastrowid or 0)
    except Exception as exc:
        logger.warning("_auto_start_monitor DB insert failed: %s", exc)
        return

    monitor = PositionMonitor(
        pos_id, alert_id, symbol, direction,
        entry, sl_price, tp_price, ts_open,
        timeout_hours=timeout_hours,
    )
    monitor.start()
    with _monitors_lock:
        _active_monitors[pos_id] = monitor

    logger.info(
        "PositionMonitor launched: id=%d %s %s entry=%.6g sl=%.6g tp=%.6g timeout=%.0fh",
        pos_id, direction, symbol, entry, sl_price, tp_price, timeout_hours,
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
        # Per-position timeout stored in DB; fall back to global default
        try:
            with _db_lock:
                _th_row = _get_db().execute(
                    "SELECT timeout_hours FROM position_monitors WHERE id=?", (pos_id,)
                ).fetchone()
            _timeout_h = float(_th_row[0]) if _th_row and _th_row[0] else MONITOR_TIMEOUT_HOURS
        except Exception:
            _timeout_h = MONITOR_TIMEOUT_HOURS
        elapsed = time.time() - ts_open
        if elapsed >= _timeout_h * 3600:
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
            timeout_hours=_timeout_h,
        )
        monitor.start()
        with _monitors_lock:
            _active_monitors[pos_id] = monitor
        logger.info(
            "Restored monitor: id=%d %s %s (%.0fs elapsed, timeout=%.0fh)",
            pos_id, direction, symbol, elapsed, _timeout_h,
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

def handle_scorestats_command(chat_id: int) -> None:
    """Show PnL breakdown by score bucket across all signal types."""
    try:
        with _db_lock:
            conn = _get_db()
            # Overall breakdown by score bucket
            buckets = conn.execute("""
                SELECT
                    (a.score / 10) * 10 AS bucket,
                    COUNT(*) AS cnt,
                    ROUND(100.0 * SUM(CASE WHEN pm.pnl_pct > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) AS wr,
                    ROUND(AVG(pm.pnl_pct), 2) AS avg_pnl,
                    ROUND(SUM(pm.pnl_pct), 2) AS total_pnl
                FROM alerts a
                JOIN position_monitors pm ON pm.alert_id = a.id
                WHERE a.score IS NOT NULL
                  AND pm.pnl_pct IS NOT NULL
                  AND pm.status != 'open'
                  AND a.ts >= ?
                GROUP BY bucket
                ORDER BY bucket
            """, (int(time.time()) - 30 * 86400,)).fetchall()

            # Per-type breakdown (top signal types only, ≥5 closed positions)
            by_type = conn.execute("""
                SELECT
                    a.alert_type,
                    a.recommendation,
                    (a.score / 10) * 10 AS bucket,
                    COUNT(*) AS cnt,
                    ROUND(100.0 * SUM(CASE WHEN pm.pnl_pct > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) AS wr,
                    ROUND(AVG(pm.pnl_pct), 2) AS avg_pnl
                FROM alerts a
                JOIN position_monitors pm ON pm.alert_id = a.id
                WHERE a.score IS NOT NULL
                  AND pm.pnl_pct IS NOT NULL
                  AND pm.status != 'open'
                  AND a.ts >= ?
                GROUP BY a.alert_type, a.recommendation, bucket
                HAVING cnt >= 5
                ORDER BY a.alert_type, a.recommendation, bucket
            """, (int(time.time()) - 30 * 86400,)).fetchall()
    except Exception as e:
        logger.warning("handle_scorestats_command failed: %s", e)
        _telegram_send(chat_id, "❌ Ошибка при чтении статистики.")
        return

    if not buckets:
        _telegram_send(chat_id, "⏳ Недостаточно данных — нет закрытых позиций со score.")
        return

    lines = ["📊 <b>Статистика по уровню сигнала (score)</b>", "За последние 30 дней\n"]

    # Overall table
    lines.append("<b>Все типы сигналов:</b>")
    for bucket, cnt, wr, avg_pnl, total_pnl in buckets:
        emoji = "✅" if avg_pnl > 0 else "❌"
        bar_len = min(int(abs(avg_pnl) * 4), 10)
        bar = ("▲" if avg_pnl > 0 else "▼") * bar_len
        avg_sign = "+" if avg_pnl >= 0 else ""
        lines.append(
            f"{emoji} <b>Score {bucket}–{bucket+9}</b>  cnt={cnt}  "
            f"WR={wr:.0f}%  avg={avg_sign}{avg_pnl:.2f}%  {bar}"
        )

    # Per-type details (grouped by type+side)
    if by_type:
        lines.append("\n<b>По типу сигнала (≥5 позиций):</b>")
        prev_key = None
        for atype, rec, bucket, cnt, wr, avg_pnl in by_type:
            key = (atype, rec)
            if key != prev_key:
                type_label = _label_alert_type(atype) if atype in (
                    "confluence", "oversold_24h", "overheated_24h",
                    "breakdown_short", "streak_1h", "volume_surge_short",
                    "volume_surge_long", "momentum_long",
                ) else atype
                lines.append(f"\n<i>{type_label} {rec}:</i>")
                prev_key = key
            e = "✅" if avg_pnl > 0 else "❌"
            s = "+" if avg_pnl >= 0 else ""
            lines.append(f"  {e} {bucket}–{bucket+9}: WR={wr:.0f}%  avg={s}{avg_pnl:.2f}%  (n={cnt})")

    # Quick insight
    lines.append("\n<b>💡 Вывод:</b>")
    best = max(buckets, key=lambda r: r[3])   # highest avg_pnl
    worst = min(buckets, key=lambda r: r[3])  # lowest avg_pnl
    lines.append(f"Лучший диапазон: score {best[0]}–{best[0]+9} (avg {'+' if best[3]>=0 else ''}{best[3]:.2f}%)")
    lines.append(f"Худший диапазон: score {worst[0]}–{worst[0]+9} (avg {'+' if worst[3]>=0 else ''}{worst[3]:.2f}%)")

    _telegram_send(chat_id, "\n".join(lines))


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

    # ── Hidden items section ────────────────────────────────────────────────
    _ensure_prefs_loaded()
    with _prefs_lock:
        hidden_types_snap = set(_hidden_types)
        hidden_symbols_snap = set(_hidden_symbols)
    _type_names = {
        "oversold_24h":   "перепроданность",
        "overheated_24h": "импульс вверх",
        "confluence":     "конфлюэнция",
        "streak_1h":      "серия свечей",
        "breakdown_short": "пробой вниз",
        "momentum_long":  "моментум лонг",
    }
    hidden_lines = []
    for t in sorted(hidden_types_snap):
        friendly = _type_names.get(t, t)
        hidden_lines.append(f"  ⛔ тип: <b>{friendly}</b>")
    for s in sorted(hidden_symbols_snap):
        hidden_lines.append(f"  ⛔ монета: <code>{s}</code>")

    has_mutes = bool(hidden_lines) or silenced
    if hidden_lines:
        hidden_section = "\n\n🔕 <b>Заглушено (сигналы не отправляются!):</b>\n" + "\n".join(hidden_lines)
    else:
        hidden_section = ""
    status_markup = (
        {"inline_keyboard": [[{"text": "🔓 Разглушить всё", "callback_data": "unmute_all"}]]}
        if has_mutes else None
    )
    # ────────────────────────────────────────────────────────────────────────

    msg = (
        f"<b>📊 Статус монитора Binance</b>"
        f"{silence_line}"
        f"{hidden_section}\n\n"
        f"<b>Отслеживается пар:</b> {tracked} USDT\n"
        f"<b>Ликвидных пар:</b> {liquid} (≥${MIN_VOLUME_USDT:,.0f} объёма)\n"
        f"<b>Активный хост:</b> <code>{active_host}</code>\n\n"
        f"<b>Последняя проверка:</b> {last_run}\n"
        f"<b>Время цикла:</b> {elapsed}с\n\n"
        f"<b>За последний цикл:</b>\n"
        f"  🚨 Отправлено конфлюэнция-алертов: <b>{summary.get('confluence_alerts', 0)}</b>\n"
        f"  💤 Подавлено одиночных сигналов: {summary.get('single_signals_skipped', 0)}\n"
        f"  🔒 Заблокировано лимитом позиций: {summary.get('confluence_cap_blocked', 0)}\n\n"
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
        f"  🌋 Объём+CRSI: <b>{summary.get('vol_surge_alerts', 0)}</b>\n"
        f"  📉 Памп→Шорт (fade): <b>{summary.get('pump_fade_alerts', 0)}</b>"
        f"{'  🔬 shadow' if PUMP_FADE_SHADOW_ONLY else ''}"
        f"{error_line}\n\n"
        f"<b>Правила:</b>\n"
        f"  Алерт только при ≥ {CONFLUENCE_MIN_SIGNALS} сигналах, RSI LONG≤{CONFLUENCE_RSI_MAX_LONG}/SHORT≥{CONFLUENCE_RSI_MIN_SHORT}, vol≥${MIN_VOLUME_CONFLUENCE/1e3:.0f}k\n"
        f"  Мин. объём: ${MIN_VOLUME_USDT:,.0f}\n"
        f"  Всплеск объёма: ≥ {VOLUME_SPIKE_MULTIPLIER}× средн. (5м)\n"
        f"  RSI перекупленность: ≥ {RSI_OVERBOUGHT}  |  кулдаун {RSI_ALERT_COOLDOWN // 3600}ч\n"
        f"  RSI перепроданность: ≤ {RSI_OVERSOLD}  |  кулдаун {RSI_ALERT_COOLDOWN // 3600}ч\n"
        f"  EMA-200 (4ч): тренд-фильтр для шорт/лонг\n"
        f"  Интервал проверки: 5 мин\n\n"
        f"<b>Команды мониторинга:</b>\n"
        f"  /status · /top10 · /top30 · /signal · /stats\n\n"
        f"<b>Команды торговли:</b>\n"
        f"  /trade · /mytrades · /positions · /analyze · /ai\n\n"
        f"<b>Демо P&L:</b>\n"
        f"  /demo\n\n"
        f"<b>Личный трекер позиций:</b>\n"
        f"  /addpos SYMBOL LONG|SHORT — добавить позицию\n"
        f"  /mypos — список открытых позиций\n"
        f"  /closepos SYMBOL — закрыть позицию\n\n"
        f"<b>Настройки:</b>\n"
        f"  /silence · /unmute"
    )
    _telegram_send(chat_id, msg, reply_markup=status_markup)


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
    import html as _html
    import datetime as _dt

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

    # ── Old-vs-honest split per alert_type from demo_positions ──────────────
    _demo_split: dict[str, tuple[int, int]] = {}
    try:
        with _db_lock:
            _rows = _get_db().execute(
                """
                SELECT alert_type,
                       SUM(CASE WHEN ts_close <  ? THEN 1 ELSE 0 END) AS old_n,
                       SUM(CASE WHEN ts_close >= ? THEN 1 ELSE 0 END) AS hon_n
                FROM demo_positions
                WHERE status != 'open' AND is_shadow = 0
                  AND ts_close IS NOT NULL AND alert_type IS NOT NULL
                GROUP BY alert_type
                """,
                (WICK_CHECK_ENABLED_TS, WICK_CHECK_ENABLED_TS),
            ).fetchall()
            for _atype, _old, _hon in _rows:
                _demo_split[_atype] = (int(_old or 0), int(_hon or 0))
    except Exception as _ds_exc:
        logger.debug("demo_split query failed: %s", _ds_exc)

    def fmt_horizon(h: dict) -> str:
        n = h["n"]
        if n == 0:
            return "—"
        tp_r = h["tp"]  / n * 100.0
        sl_r = h["sl"]  / n * 100.0
        avg  = h["pnl_sum"] / n
        sign = "+" if avg >= 0 else ""
        return f"🟢{tp_r:.0f}% 🔴{sl_r:.0f}% ({sign}{avg:.1f}%)"

    # ── Header (sent with first chunk) ───────────────────────────────────────
    header = (
        f"📊 <b>Статистика за {HIT_RATE_RETENTION_DAYS} дней</b>\n"
        f"<i>SL {sl_pct:.0f}% / TP {tp_pct:.0f}% · симуляция по ценам снапшотов</i>"
    )

    # ── Per-type blocks — each type = one atomic block string ────────────────
    type_blocks: list[str] = []
    for r in stats:
        h3m = r["horizons"]["3м"]
        h5m = r["horizons"]["5м"]
        h15 = r["horizons"]["15м"]
        h1h = r["horizons"]["1ч"]
        h4h = r["horizons"]["4ч"]
        # html.escape() ALL dynamic strings from DB
        atype_safe = _html.escape(str(r["alert_type"]))
        rec_safe   = _html.escape(str(r["recommendation"]))
        _old_n, _hon_n = _demo_split.get(r["alert_type"], (0, 0))
        _total_closed = _old_n + _hon_n
        _closed_note = (
            f" · закрыто: {_total_closed} ({_old_n} старых / {_hon_n} честных)"
            if _total_closed else ""
        )
        type_blocks.append(
            f"<b>{atype_safe}</b> · {rec_safe} · {r['total']} алертов{_closed_note}\n"
            f"   3м: {fmt_horizon(h3m)}\n"
            f"   5м: {fmt_horizon(h5m)}\n"
            f"  15м: {fmt_horizon(h15)}\n"
            f"   1ч: {fmt_horizon(h1h)}\n"
            f"   4ч: {fmt_horizon(h4h)}"
        )

    # ── Footer ────────────────────────────────────────────────────────────────
    _wick_dt = _dt.datetime.utcfromtimestamp(WICK_CHECK_ENABLED_TS).strftime("%d.%m.%Y %H:%M UTC")
    footer = (
        f"\n<i>🟢 = TP достигнут (+{tp_pct:.0f}%)  🔴 = стоп (−{sl_pct:.0f}%)  "
        f"в скобках — средний P&amp;L включая таймауты</i>\n"
        f"<i>честные = закрыты по свечному фитилю с {_wick_dt}</i>"
    )

    # ── Send per-type stats, splitting at 3800 chars ─────────────────────────
    MAX_MSG = 3800

    def _flush(chunk_lines: list[str], is_first: bool) -> None:
        prefix = header + "\n\n" if is_first else ""
        body   = "\n".join(chunk_lines)
        _telegram_send(chat_id, prefix + body + footer)

    chunk: list[str] = []
    chunk_len  = len(header) + len(footer) + 2   # reserve header+footer
    first_msg  = True

    for block in type_blocks:
        cost = len(block) + 1   # +1 for \n separator
        if chunk and chunk_len + cost > MAX_MSG:
            _flush(chunk, first_msg)
            first_msg = False
            chunk     = []
            chunk_len = len(footer) + 1
        chunk.append(block)
        chunk_len += cost

    if chunk:
        _flush(chunk, first_msg)

    # ── Shadow-reason summary ─────────────────────────────────────────────────
    try:
        with _db_lock:
            _sr_rows = _get_db().execute("""
                SELECT
                  CASE
                    WHEN shadow_reason LIKE 'bear_downtrend%' THEN 'bear_downtrend'
                    WHEN shadow_reason LIKE '%:%'
                         THEN TRIM(SUBSTR(shadow_reason, 1, INSTR(shadow_reason, ':') - 1))
                    ELSE COALESCE(shadow_reason, '?')
                  END AS rp,
                  COUNT(*) AS n,
                  SUM(CASE WHEN status='tp'    THEN 1 ELSE 0 END) AS tp_n,
                  COALESCE(SUM(CASE WHEN status!='open' THEN pnl_usd ELSE 0 END), 0) AS pnl
                FROM demo_positions
                WHERE is_shadow=1
                  AND shadow_reason IS NOT NULL AND shadow_reason != ''
                  AND status != 'open'
                GROUP BY rp
                ORDER BY pnl DESC
            """).fetchall()
        _sr_total_n   = sum(r[1] for r in _sr_rows)
        _sr_total_tp  = sum(r[2] for r in _sr_rows)
        _sr_total_pnl = sum(r[3] for r in _sr_rows)
        if _sr_rows:
            _sr_wr = _sr_total_tp / _sr_total_n * 100 if _sr_total_n else 0
            _sr_ps = "+" if _sr_total_pnl >= 0 else ""
            _sr_lines = [
                "🔒 <b>Заблокированные сигналы (закрытые)</b>",
                f"  Всего: <b>{_sr_total_n}</b>  │  WR: <b>{_sr_wr:.0f}%</b>  │  PnL: <b>{_sr_ps}${_sr_total_pnl:.2f}</b>",
            ]
            for rp, n, tp_n, pnl in _sr_rows:
                wr_r = tp_n / n * 100 if n else 0
                pnl_icon = "🟢" if pnl >= 0 else "🔴"
                ps = "+" if pnl >= 0 else ""
                _sr_lines.append(
                    f"  {pnl_icon} <code>{rp}</code>: {n} сд., WR {wr_r:.0f}%, Σ <b>{ps}${pnl:.2f}</b>"
                )
            _telegram_send(chat_id, "\n".join(_sr_lines))
    except Exception as _sr_exc:
        logger.warning("stats shadow-reason query failed: %s", _sr_exc)

    # ── 🧲 Liquidity block — always a SEPARATE message ───────────────────────
    try:
        with _db_lock:
            conn = _get_db()
            liq = conn.execute("""
                SELECT
                  SUM(CASE WHEN entry_type='limit' THEN 1 ELSE 0 END)                       AS lp,
                  SUM(CASE WHEN status='filled' AND entry_type='limit' THEN 1 ELSE 0 END)    AS lf,
                  SUM(CASE WHEN status='expired'    THEN 1 ELSE 0 END)                       AS exp,
                  SUM(CASE WHEN status='missed_move' THEN 1 ELSE 0 END)                      AS missed,
                  SUM(CASE WHEN entry_type='market' THEN 1 ELSE 0 END)                       AS mkt,
                  SUM(CASE WHEN result IS NULL AND status='filled' THEN 1 ELSE 0 END)        AS open_pos,
                  SUM(CASE WHEN result IS NOT NULL THEN 1 ELSE 0 END)                        AS ct,
                  SUM(CASE WHEN result='tp'         THEN 1 ELSE 0 END)                       AS ctp,
                  SUM(CASE WHEN result='sl'         THEN 1 ELSE 0 END)                       AS csl,
                  COALESCE(SUM(CASE WHEN result IS NOT NULL THEN pnl_usd ELSE 0 END), 0)     AS cpnl,
                  COALESCE(AVG(CASE WHEN result IS NOT NULL THEN pnl_usd END), 0)             AS avg_pnl,
                  COALESCE(SUM(CASE WHEN entry_type='limit' AND result IS NOT NULL THEN pnl_usd ELSE 0 END), 0) AS lpnl,
                  COALESCE(SUM(CASE WHEN entry_type='market' AND result IS NOT NULL THEN pnl_usd ELSE 0 END), 0) AS mpnl,
                  COALESCE(SUM(saved_from_sl), 0)                                             AS saved,
                  COALESCE(SUM(touched_no_fill), 0)                                           AS tnf
                FROM liquidity_orders
            """).fetchone()
            liq_open_rows = conn.execute(
                "SELECT symbol, direction, entry_price FROM liquidity_orders "
                "WHERE status='filled' AND result IS NULL"
            ).fetchall()
    except Exception as _liq_exc:
        logger.warning("stats liquidity query failed: %s", _liq_exc)
        liq = None

    if liq and any(v for v in liq):
        lp, lf, exp, miss, mkt, open_pos, ct, ctp, csl, cpnl, avg_pnl, lpnl, mpnl, saved, tnf = liq
        lp = lp or 0; lf = lf or 0; exp = exp or 0; miss = miss or 0
        mkt = mkt or 0; open_pos = open_pos or 0; ct = ct or 0
        ctp = ctp or 0; csl = csl or 0; saved = saved or 0; tnf = tnf or 0
        cpnl = cpnl or 0.0; avg_pnl = avg_pnl or 0.0
        lpnl = lpnl or 0.0; mpnl = mpnl or 0.0
        fill_rate = lf / lp * 100.0 if lp else 0.0
        wr        = ctp / ct * 100.0 if ct else 0.0

        unreal = 0.0
        for sym, dr, ep in (liq_open_rows or []):
            try:
                t = _gateio_ticker(sym)
                if t and ep:
                    cur = float(t["lastPrice"])
                    unreal += (cur - ep) / ep * 100.0 if dr == "LONG" else (ep - cur) / ep * 100.0
            except Exception:
                pass

        def _s(v: float) -> str: return "+" if v >= 0 else ""

        liq_lines = [
            "🧲 <b>Умный вход по ликвидности</b>",
            f"  Лимиток выставлено: <b>{lp}</b>  │  Исполнено: <b>{lf}</b>  │  Fill rate: <b>{fill_rate:.0f}%</b>",
            f"  Касаний без заполнения: <b>{tnf}</b>  (прокол &lt;0.05%)",
            f"  Истекло: {exp}  │  Ушло без входа: {miss}  │  Рыночных входов: {mkt}",
            f"  Открытых: <b>{open_pos}</b>  │  Нереализованный: <b>{_s(unreal)}${unreal:.2f}</b>",
        ]
        if ct:
            liq_lines += [
                f"  Закрытых: <b>{ct}</b>  │  Win-rate: <b>{wr:.0f}%</b>  (TP: {ctp} / SL: {csl})",
                f"  PnL закрытых: <b>{_s(cpnl)}${cpnl:.2f}</b>  ·  avg: <b>{_s(avg_pnl)}${avg_pnl:.2f}</b>",
                f"  Лимитный вход PnL: <b>{_s(lpnl)}${lpnl:.2f}</b>  │  Рыночный: <b>{_s(mpnl)}${mpnl:.2f}</b>",
            ]
        if saved:
            liq_lines.append(f"  💾 Спасено от выбивания: <b>{saved}</b>")

        _telegram_send(chat_id, "\n".join(liq_lines))
    else:
        _telegram_send(chat_id, "🧲 <b>Умный вход по ликвидности</b>\n  Сделок пока нет.")


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

    volume_24h: float | None = None
    ticker = _gateio_ticker(symbol)
    if ticker is not None:
        try:
            volume_24h = float(ticker.get("quoteVolume", 0)) or None
        except (TypeError, ValueError):
            volume_24h = None

    data_source = "futures"
    # Futures fallback: if NOTHING came back (the symbol probably isn't on Gate.io)
    if rsi is None and ema200 is None and volume_24h is None:
        logger.info("Trade /trade: no Gate.io data for %s", symbol)
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
    """Show currently active position monitors with live PnL (max 20)."""
    with _monitors_lock:
        monitors = list(_active_monitors.values())

    if not monitors:
        _telegram_send(chat_id, "📭 <b>Нет открытых позиций</b>")
        return

    monitors = sorted(monitors, key=lambda x: x.ts_open)
    total = len(monitors)
    shown = monitors[:20]
    now = time.time()

    # ── Direction summary block ──────────────────────────────────────────────
    def _dir_summary(direction: str) -> str:
        group = [m for m in monitors if m.direction == direction]
        if not group:
            return ""
        in_profit = in_loss = sum_profit = sum_loss = 0
        for m in group:
            cur = _get_current_price(m.symbol)
            if cur is None:
                continue
            pnl = (
                (cur - m.entry) / m.entry * 100 if direction == "LONG"
                else (m.entry - cur) / m.entry * 100
            )
            if pnl > 0:
                in_profit += 1; sum_profit += pnl
            elif pnl < 0:
                in_loss += 1; sum_loss += pnl
        net = sum_profit + sum_loss
        net_emoji = "🟢" if net > 0 else ("🔴" if net < 0 else "⚪️")
        icon = "📈" if direction == "LONG" else "📉"
        profit_line = f"   ✅ В прибыли: {in_profit}  (+{sum_profit:.1f}%)" if in_profit else ""
        loss_line   = f"   ❌ В убытке:  {in_loss}  ({sum_loss:.1f}%)"   if in_loss   else ""
        return (
            f"{icon} <b>{direction}: {len(group)} поз.</b>\n"
            + (profit_line + "\n" if profit_line else "")
            + (loss_line   + "\n" if loss_line   else "")
            + f"   {net_emoji} Итог: {net:+.1f}%"
        )

    short_block = _dir_summary("SHORT")
    long_block  = _dir_summary("LONG")
    divider = "━━━━━━━━━━━━━━━━━━━━"

    lines = [
        f"📊 <b>Открытые позиции: {total}</b>",
        "",
    ]
    if short_block:
        lines += [short_block, ""]
    if long_block:
        lines += [long_block, ""]
    lines += [divider, f"<i>Детали (последние {len(shown)}):</i>", ""]
    # ────────────────────────────────────────────────────────────────────────

    for m in shown:
        current = _get_current_price(m.symbol)
        elapsed_h = (now - m.ts_open) / 3600
        if current is not None:
            pnl_pct = (
                (current - m.entry) / m.entry * 100 if m.direction == "LONG"
                else (m.entry - current) / m.entry * 100
            )
            roi_pct = pnl_pct * DISPLAY_LEVERAGE
            pnl_emoji = "🟢" if pnl_pct > 0 else ("🔴" if pnl_pct < 0 else "⚪️")
            pnl_str = f"ROI ×{DISPLAY_LEVERAGE}: {roi_pct:+.1f}%  ({pnl_pct:+.2f}% цена)"
        else:
            pnl_emoji, pnl_str = "⚪️", "—"
            current = m.entry

        sl_roi = abs(m.sl_price - m.entry) / m.entry * 100 * DISPLAY_LEVERAGE
        tp_roi = abs(m.tp_price - m.entry) / m.entry * 100 * DISPLAY_LEVERAGE
        sl_sign = "−" if m.direction == "LONG" else "+"
        tp_sign = "+" if m.direction == "LONG" else "−"

        dir_emoji = "📈" if m.direction == "LONG" else "📉"
        lines.append(f"{dir_emoji} <b>{m.symbol}</b>  {m.direction}")
        lines.append(
            f"   Вход: <code>${m.entry:,.6g}</code>  "
            f"Сейчас: <code>${current:,.6g}</code>"
        )
        lines.append(f"   {pnl_emoji} <b>{pnl_str}</b>  ({elapsed_h:.1f}ч)")
        lines.append(
            f"   🛡️ SL: <code>${m.sl_price:,.6g}</code> ({sl_sign}{sl_roi:.0f}%)  "
            f"🎯 TP: <code>${m.tp_price:,.6g}</code> ({tp_sign}{tp_roi:.0f}%)"
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
        return _gateio_ticker(symbol)

    def fetch_4h_klines():
        try:
            return _gateio_klines(symbol, "4h", EMA200_FETCH_LIMIT)
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
            f"❌ Монета <code>{symbol}</code> не найдена на Gate.io Futures.\n"
            "Проверьте название (пример: <code>AKEUSDT</code>, <code>BTCUSDT</code>).",
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
        sl_roi_val = sl_pct_val * DISPLAY_LEVERAGE
        tp_roi_val = tp_pct_val * DISPLAY_LEVERAGE
        sl_sign = "+" if direction == "SHORT" else "−"
        tp_sign = "+" if direction == "LONG" else "−"
        lines.append(f"Stop Loss: <b>${sl_price:,.6g}</b> ({sl_sign}{sl_roi_val:.0f}% ROI ×{DISPLAY_LEVERAGE})")
        lines.append(f"Take Profit: <b>${tp_price:,.6g}</b> ({tp_sign}{tp_roi_val:.0f}% ROI ×{DISPLAY_LEVERAGE})")
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


def handle_ai_command(chat_id: int, raw_text: str) -> None:
    """Full AI analysis of a coin: /ai BTCUSDT
    Fetches live market data and asks Gemini for a structured assessment."""
    parts = raw_text.strip().split()
    if len(parts) < 2:
        _telegram_send(
            chat_id,
            "🤖 <b>AI-анализ монеты</b>\n\n"
            "Использование: <code>/ai BTCUSDT</code>\n"
            "Пример: <code>/ai ICNTUSDT</code>\n\n"
            "ИИ оценит тренд, RSI, объём, риски и даст торговую рекомендацию.",
        )
        return

    raw_sym = parts[1].upper().strip()
    symbol = raw_sym if raw_sym.endswith("USDT") else raw_sym + "USDT"
    _telegram_send(chat_id, f"🤖 Анализирую <code>{symbol}</code> с помощью ИИ…")

    if not _GEMINI_BASE_URL or not _GEMINI_API_KEY:
        _telegram_send(chat_id, "❌ Gemini AI не настроен. Обратитесь к администратору.")
        return

    # ── Gather live data ────────────────────────────────────────────────────
    try:
        ticker = _gateio_ticker(symbol)
    except Exception:
        ticker = None
    rsi = None
    try:
        rsi = get_klines_rsi(symbol)
    except Exception:
        pass
    closes = _fetch_daily_closes_for_crsi(symbol)

    if not ticker:
        _telegram_send(
            chat_id,
            f"❌ Монета <code>{symbol}</code> не найдена на Gate.io Futures.",
        )
        return

    try:
        price       = float(ticker["lastPrice"])
        pct24       = float(ticker["priceChangePercent"])
        volume_usdt = float(ticker.get("quoteVolume") or ticker.get("baseVolume") or 0)
    except (ValueError, KeyError, TypeError):
        _telegram_send(chat_id, f"❌ Не удалось получить данные по <code>{symbol}</code>.")
        return

    # Trend: consecutive days
    trend_up   = _count_consecutive_trend_days(closes, "up")   if closes else 0
    trend_down = _count_consecutive_trend_days(closes, "down") if closes else 0

    # EMA-200 position
    ema_status = "данных нет"
    try:
        klines_4h = _gateio_klines(symbol, "4h", EMA200_FETCH_LIMIT)
        if klines_4h and len(klines_4h) >= EMA200_PERIOD:
            h4_closes = [float(k[4]) for k in klines_4h]
            ema200 = _calculate_ema(h4_closes, EMA200_PERIOD)
            if ema200:
                ema_pct = (price - ema200) / ema200 * 100
                ema_status = (
                    f"{'выше' if price > ema200 else 'ниже'} EMA-200 на {abs(ema_pct):.1f}%"
                )
    except Exception:
        pass

    # Market regime
    regime = get_market_regime()

    # ── Build Gemini prompt ─────────────────────────────────────────────────
    rsi_str   = f"{rsi:.1f}" if rsi else "н/д"
    vol_str   = f"${volume_usdt:,.0f}" if volume_usdt else "н/д"
    trend_str = (
        f"растёт {trend_up} дней подряд" if trend_up >= 2
        else f"падает {trend_down} дней подряд" if trend_down >= 2
        else "боковое движение или 1 день"
    )

    prompt = (
        f"Ты — опытный трейдер крипто-фьючерсов. Проведи анализ монеты и дай чёткую торговую оценку.\n\n"
        f"=== ДАННЫЕ ПО {symbol} ===\n"
        f"Цена: ${price:,.6g}\n"
        f"Изменение за 24ч: {pct24:+.2f}%\n"
        f"Объём за 24ч: {vol_str}\n"
        f"RSI (14): {rsi_str}\n"
        f"Тренд (дневные свечи): {trend_str}\n"
        f"Положение относительно EMA-200 (4ч): {ema_status}\n"
        f"Рыночный режим: {regime}\n\n"
        f"=== ЗАДАЧА ===\n"
        f"Ответь строго по этой структуре на РУССКОМ языке:\n\n"
        f"📊 ТРЕНД: [Восходящий / Нисходящий / Боковой] — одна фраза объяснения\n\n"
        f"⚡ КЛЮЧЕВЫЕ ФАКТОРЫ:\n"
        f"• [фактор 1]\n"
        f"• [фактор 2]\n"
        f"• [фактор 3]\n\n"
        f"🎯 РЕКОМЕНДАЦИЯ: [ЛОНГ / ШОРТ / ЖДАТЬ] — причина в одном предложении\n\n"
        f"⚠️ ГЛАВНЫЙ РИСК: [одна фраза]\n\n"
        f"Будь конкретным. Не пиши общие фразы. Не давай гарантий прибыли."
    )

    analysis = _call_gemini_text(prompt, max_tokens=1024, temperature=0.65, timeout=30)
    if not analysis:
        _telegram_send(
            chat_id,
            f"⚠️ Gemini не ответил за 30 секунд. Попробуй ещё раз через минуту.",
        )
        return

    header = (
        f"🤖 <b>AI-анализ: <code>{symbol}</code></b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 ${price:,.6g}  |  {pct24:+.1f}% за 24ч  |  RSI {rsi_str}\n"
        f"📅 Тренд: {trend_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    # Send header (HTML) and analysis (plain) as two separate messages to
    # avoid Telegram's 4096-char limit cutting off the analysis mid-sentence.
    _telegram_send(chat_id, header)
    # Strip any residual HTML-unsafe chars from Gemini's plain-text output
    # before sending as a separate plain-text message.
    safe_analysis = analysis.replace("<", "\u003c").replace(">", "\u003e").replace("&", "\u0026")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": safe_analysis}, timeout=15)
    except Exception as _exc:
        logger.error("AI analysis send failed: %s", _exc)
    logger.info("AI analysis sent for %s to chat_id=%s", symbol, chat_id)


def handle_unmute_command(chat_id: int) -> None:
    with state_lock:
        state["silenced"] = False
        state["silenced_at"] = None
    # Also clear all hidden types and symbols from DB
    try:
        with _db_lock:
            conn = _get_db()
            conn.execute("DELETE FROM hidden_items")
            conn.commit()
        with _prefs_lock:
            _hidden_types.clear()
            _hidden_symbols.clear()
    except Exception as e:
        logger.warning("handle_unmute_command: failed to clear hidden_items: %s", e)
    logger.info("Alerts unmuted via /unmute from chat_id=%s", chat_id)
    _telegram_send(chat_id, (
        "<b>🔔 Алерты возобновлены</b>\n"
        "Все скрытые типы и монеты разблокированы. Сигналы снова активны."
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
        return f"Памп ×{a.rsplit('_', 1)[-1]} → SHORT"
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
    PERIODS = [
        ("18h",  int(18 * 3600)),
        ("24h",  int(24 * 3600)),
        ("3d",   int(3  * 86400)),
        ("6d",   int(6  * 86400)),
        ("12d",  int(12 * 86400)),
        ("30d",  int(30 * 86400)),
    ]
    try:
        with _db_lock:
            conn = _get_db()
            now_ts = int(time.time())
            total = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
            # Count per period
            period_counts: dict[str, int] = {}
            for label, seconds in PERIODS:
                cnt = conn.execute(
                    "SELECT COUNT(*) FROM alerts WHERE ts >= ?",
                    (now_ts - seconds,),
                ).fetchone()[0]
                period_counts[label] = cnt
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
        "last24h": period_counts.get("24h", 0),
        "periodCounts": period_counts,
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
        if window_arg.endswith("h"):
            window_seconds = max(3600, min(int(window_arg[:-1]) * 3600, 365 * 86400))
        elif window_arg.endswith("d"):
            window_seconds = max(86400, min(int(window_arg[:-1]) * 86400, 365 * 86400))
        else:
            window_seconds = max(86400, min(int(window_arg) * 86400, 365 * 86400))
    except (TypeError, ValueError):
        window_seconds = 30 * 86400

    now_ts = int(time.time())
    since = now_ts - window_seconds
    # require at least 4h of settle time so the 4h follow-up could be filled
    # 5-minute settle so even 3m/5m follow-ups can appear; longer horizons
    # naturally have fewer follow-ups for recent signals — that is expected.
    settled_before = now_ts - 5 * 60

    try:
        with _db_lock:
            conn = _get_db()
            rows = conn.execute(
                "SELECT alert_type, recommendation, price_at_alert, "
                "price_3m, price_5m, price_15m, price_1h, price_4h, "
                "price_1d, price_2d, price_3d, price_7d "
                "FROM alerts WHERE ts >= ? AND ts <= ?",
                (since, settled_before),
            ).fetchall()
    except Exception as e:
        logger.warning("api_signals_performance failed: %s", e)
        return jsonify({"error": "db_error"}), 500

    buckets: dict[tuple[str, str], dict] = {}
    for alert_type, rec, p0, p3m, p5m, p15, p1h, p4h, p1d, p2d, p3d, p7d in rows:
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
                for h in ("3m", "5m", "15m", "1h", "4h", "1d", "2d", "3d", "7d")
            },
        })
        b["count"] += 1
        for horizon, pf in (
            ("3m", p3m), ("5m", p5m), ("15m", p15), ("1h", p1h), ("4h", p4h),
            ("1d", p1d), ("2d", p2d), ("3d", p3d), ("7d", p7d),
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
        "window": window_arg,
        "windowSeconds": window_seconds,
        "since": since,
        "until": settled_before,
        "totalSignals": sum(e["count"] for e in out),
        "byType": out,
    })


@app.route("/bot-api/positions", methods=["GET"])
def api_positions():
    """Return all currently active position monitors with live PnL.

    Primary source: the 5-min price cache populated by run_checks.
    Fallback: one Gate.io batch call for symbols missing from the cache
    (handles freshly-started bot and symbols with Binance-style 'B' suffix).
    """
    with _monitors_lock:
        monitors = list(_active_monitors.values())

    with _price_cache_lock:
        prices = dict(_price_cache)

    # ── Batch-fetch prices when the 5-min cache is still empty (startup) ───
    # After the first run_checks cycle the cache has 878 symbols + B-suffix
    # variants for active monitors, so this block is skipped on every normal
    # request and only fires during the first ~5 minutes after a restart.
    cache_is_empty = not prices  # True only before first cycle completes
    if cache_is_empty:
        missing = [m.symbol for m in monitors]
        if missing:
            try:
                live = get_24h_tickers(missing)   # single Gate.io call
                # Binance 'B'-suffix fallback: KORUBUSDT → KORU_USDT
                still_missing = [s for s in missing if s not in live]
                alt_map: dict[str, str] = {}
                for s in still_missing:
                    if s.endswith("BUSDT") and len(s) > 6:
                        alt = s[:-5] + "USDT"
                        alt_map[alt] = s
                if alt_map:
                    alt_live = get_24h_tickers(list(alt_map))
                    for alt, orig in alt_map.items():
                        if alt in alt_live:
                            live[orig] = alt_live[alt]
                for sym, t in live.items():
                    try:
                        p = float(t["lastPrice"])
                        if p:
                            prices[sym] = p
                    except (ValueError, KeyError):
                        pass
            except Exception as e:
                logger.warning("api_positions: startup live fetch failed: %s", e)
    # ────────────────────────────────────────────────────────────────────────

    now = time.time()
    result = []
    for m in sorted(monitors, key=lambda x: x.ts_open):
        current = prices.get(m.symbol) or None
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


@app.route("/bot-api/track", methods=["POST"])
def api_track():
    """Record a page-view beacon from the web dashboard.

    Body (JSON): { "page": "/", "referrer": "..." }
    The visitor is identified by a salted SHA-256 of IP + User-Agent —
    no raw IP address is ever stored.
    """
    import hashlib
    try:
        body     = request.get_json(silent=True) or {}
        page     = (body.get("page") or "/")[:200]
        ua       = (request.headers.get("User-Agent") or "")[:300]
        ip       = request.headers.get("X-Forwarded-For", request.remote_addr or "")
        ip       = ip.split(",")[0].strip()
        raw      = f"v1:{ip}:{ua}"
        visitor  = hashlib.sha256(raw.encode()).hexdigest()[:32]
        with _db_lock:
            conn = _get_db()
            conn.execute(
                "INSERT INTO page_views (ts, page, visitor_hash, user_agent) VALUES (?,?,?,?)",
                (int(time.time()), page, visitor, ua[:200]),
            )
            conn.commit()
    except Exception as e:
        logger.debug("api_track error: %s", e)
    return "", 204


@app.route("/bot-api/analytics", methods=["GET"])
def api_analytics():
    """Visitor analytics for the web dashboard.

    Returns daily views (last 14 days), totals for 1d / 7d / 30d,
    unique-visitor counts, returning-visitor rate, and page breakdown.
    """
    now = int(time.time())
    try:
        with _db_lock:
            conn = _get_db()

            def _q(since: int):
                return conn.execute(
                    "SELECT page, visitor_hash FROM page_views WHERE ts >= ?",
                    (since,),
                ).fetchall()

            rows_1d  = _q(now - 86400)
            rows_7d  = _q(now - 7  * 86400)
            rows_30d = _q(now - 30 * 86400)

            # Daily buckets — last 14 days
            daily_rows = conn.execute(
                "SELECT ts, visitor_hash FROM page_views WHERE ts >= ?",
                (now - 14 * 86400,),
            ).fetchall()

    except Exception as e:
        logger.warning("api_analytics error: %s", e)
        return jsonify({"error": "db_error"}), 500

    def _summary(rows):
        visitors = [r[1] for r in rows]
        unique   = len(set(visitors))
        total    = len(rows)
        # returning = visitors seen more than once in this window
        from collections import Counter
        counts   = Counter(visitors)
        returning = sum(1 for c in counts.values() if c > 1)
        return {
            "views":     total,
            "unique":    unique,
            "returning": returning,
            "returnRate": round(returning / unique * 100, 1) if unique else 0,
        }

    # Pages breakdown (30d)
    from collections import Counter
    page_counts = Counter(r[0] for r in rows_30d)
    pages = [
        {"page": p, "views": c}
        for p, c in page_counts.most_common(10)
    ]

    # Daily buckets
    from collections import defaultdict
    day_views    = defaultdict(int)
    day_visitors = defaultdict(set)
    for ts, vh in daily_rows:
        day = time.strftime("%Y-%m-%d", time.gmtime(ts))
        day_views[day]    += 1
        day_visitors[day].add(vh)
    daily = []
    for i in range(13, -1, -1):
        day = time.strftime("%Y-%m-%d", time.gmtime(now - i * 86400))
        daily.append({
            "date":    day,
            "views":   day_views.get(day, 0),
            "unique":  len(day_visitors.get(day, set())),
        })

    return jsonify({
        "1d":   _summary(rows_1d),
        "7d":   _summary(rows_7d),
        "30d":  _summary(rows_30d),
        "pages": pages,
        "daily": daily,
    })


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


@app.route("/bot-api/ai-analyze", methods=["POST"])
def api_ai_analyze():
    """POST { symbol } → { analysis } — Gemini AI analysis of a coin."""
    from flask import request as flask_request
    try:
        body = flask_request.get_json(force=True) or {}
        raw_sym = str(body.get("symbol", "")).strip().upper()
        if not raw_sym:
            return jsonify({"error": "symbol required"}), 400
        symbol = raw_sym if raw_sym.endswith("USDT") else raw_sym + "USDT"

        # Gather live data
        ticker = None
        try: ticker = _gateio_ticker(symbol)
        except Exception: pass
        rsi = None
        try: rsi = get_klines_rsi(symbol)
        except Exception: pass
        closes = _fetch_daily_closes_for_crsi(symbol)

        if not ticker:
            return jsonify({"error": f"Монета {symbol} не найдена"}), 404

        price = pct24 = volume = None
        try:
            price   = float(ticker["lastPrice"])
            pct24   = float(ticker["priceChangePercent"])
            volume  = float(ticker.get("quoteVolume") or 0)
        except Exception:
            return jsonify({"error": "Не удалось получить данные"}), 500

        trend_up   = _count_consecutive_trend_days(closes, "up")   if closes else 0
        trend_down = _count_consecutive_trend_days(closes, "down") if closes else 0
        trend_str = (
            f"растёт {trend_up} дней подряд" if trend_up >= 2
            else f"падает {trend_down} дней подряд" if trend_down >= 2
            else "боковое движение"
        )

        prompt = (
            f"Ты — опытный трейдер крипто-фьючерсов. Проведи анализ монеты.\n\n"
            f"=== {symbol} ===\n"
            f"Цена: ${price:,.6g}\n"
            f"Изменение за 24ч: {pct24:+.2f}%\n"
            f"Объём за 24ч: ${volume:,.0f}\n"
            f"RSI (14): {f'{rsi:.1f}' if rsi else 'н/д'}\n"
            f"Тренд (дневные свечи): {trend_str}\n\n"
            f"Ответь строго по структуре на РУССКОМ:\n\n"
            f"📊 ТРЕНД: [Восходящий / Нисходящий / Боковой] — одна фраза\n\n"
            f"⚡ КЛЮЧЕВЫЕ ФАКТОРЫ:\n• [фактор 1]\n• [фактор 2]\n• [фактор 3]\n\n"
            f"🎯 РЕКОМЕНДАЦИЯ: [ЛОНГ / ШОРТ / ЖДАТЬ] — причина\n\n"
            f"⚠️ ГЛАВНЫЙ РИСК: [одна фраза]\n\n"
            f"Не гарантируй прибыль. Будь конкретным."
        )
        analysis = _call_gemini_text(prompt, max_tokens=1024, temperature=0.65, timeout=30)
        if not analysis:
            return jsonify({"error": "AI не ответил, попробуйте позже"}), 503

        return jsonify({
            "symbol": symbol,
            "price": price,
            "pct24": pct24,
            "rsi": rsi,
            "trend": trend_str,
            "analysis": analysis,
        })
    except Exception as exc:
        logger.warning("api_ai_analyze error: %s", exc)
        return jsonify({"error": "Внутренняя ошибка"}), 500


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
scheduler.add_job(
    check_demo_positions, "interval", seconds=60, id="demo_check",
)
scheduler.add_job(
    check_liquidity_orders, "interval", seconds=60, id="liquidity_check",
)
scheduler.add_job(
    check_watchlist, "interval", seconds=300, id="watchlist_check",
)
scheduler.add_job(
    check_entry_watch, "interval", seconds=300, id="entry_watch_check",
)
# Skip background threads when imported under tests (pytest's conftest sets
# TESTING=1). Strict truthy parsing so an accidental TESTING=0/false in prod
# does NOT silently disable the workers.
def _startup_ranking_refresh() -> None:
    """After the first check cycle populates liquid_symbols, immediately build
    the volume ranking so /top10 works right after startup without waiting for
    the cycle to reach step 7 (which can be skipped by the deadline check)."""
    deadline = time.time() + 900  # give up after 15 min
    while time.time() < deadline:
        time.sleep(10)
        with state_lock:
            syms = list(state["liquid_symbols"])
            already_built = state["volume_ranking_updated"] > 0
        if already_built:
            return  # ranking already built by the normal cycle — nothing to do
        if syms:
            logger.info("Startup ranking refresh: building volume ranking for %d liquid pairs", len(syms))
            try:
                refresh_weekly_monthly_highs(syms)
            except Exception as _e:
                logger.warning("Startup ranking refresh failed: %s", _e)
            return
    logger.warning("Startup ranking refresh: timed out waiting for liquid_symbols")


def _restore_cooldowns_from_db() -> None:
    """Restore per-symbol alert cooldowns from demo_positions after a restart.
    Without this, every restart clears in-memory state and the same coins
    re-signal immediately even though their positions are still open."""
    try:
        now_ts = int(time.time())
        with _db_lock:
            conn = _get_db()
            os_rows = conn.execute(
                "SELECT symbol, MAX(ts_open) FROM demo_positions "
                "WHERE alert_type='oversold_24h' AND is_shadow=0 "
                "  AND ts_open > ? GROUP BY symbol",
                (now_ts - OVERSOLD_COOLDOWN,),
            ).fetchall()
            sk_rows = conn.execute(
                "SELECT symbol, MAX(ts_open) FROM demo_positions "
                "WHERE alert_type='streak_1h' AND is_shadow=0 "
                "  AND ts_open > ? GROUP BY symbol",
                (now_ts - STREAK_1H_COOLDOWN,),
            ).fetchall()
            vr_rows = conn.execute(
                "SELECT symbol, MAX(ts_open) FROM demo_positions "
                "WHERE alert_type='vwap_reversion' AND is_shadow=1 "
                "  AND ts_open > ? GROUP BY symbol",
                (now_ts - VWAP_REVERSION_COOLDOWN,),
            ).fetchall()
            lr_rows = conn.execute(
                "SELECT symbol, MAX(ts_open) FROM demo_positions "
                "WHERE alert_type='liq_reversal' AND is_shadow=1 "
                "  AND ts_open > ? GROUP BY symbol",
                (now_ts - OVERSOLD_COOLDOWN,),
            ).fetchall()
            bbs_rows = conn.execute(
                "SELECT symbol, MAX(ts_open) FROM demo_positions "
                "WHERE alert_type='bb_squeeze' AND is_shadow=1 "
                "  AND ts_open > ? GROUP BY symbol",
                (now_ts - BB_SQUEEZE_COOLDOWN,),
            ).fetchall()
            emc_rows = conn.execute(
                "SELECT symbol, MAX(ts_open) FROM demo_positions "
                "WHERE alert_type='ema_cross' AND is_shadow=1 "
                "  AND ts_open > ? GROUP BY symbol",
                (now_ts - EMA_CROSS_COOLDOWN,),
            ).fetchall()
        with state_lock:
            for sym, ts in os_rows:
                state["last_oversold_alerted"][sym] = ts
            for sym, ts in sk_rows:
                state["last_streak_1h_alerted"][sym] = ts
            for sym, ts in vr_rows:
                state["last_vwap_rev_alerted"][sym] = ts
            for sym, ts in lr_rows:
                state["last_liq_reversal_alerted"][sym] = ts
            for sym, ts in bbs_rows:
                state["last_bb_squeeze_alerted"][sym] = ts
            for sym, ts in emc_rows:
                state["last_ema_cross_alerted"][sym] = ts
        logger.info(
            "Cooldowns restored: %d oversold, %d streak_1h, %d vwap_rev, "
            "%d liq_rev, %d bb_squeeze, %d ema_cross",
            len(os_rows), len(sk_rows), len(vr_rows), len(lr_rows),
            len(bbs_rows), len(emc_rows),
        )
    except Exception as exc:
        logger.warning("_restore_cooldowns_from_db failed: %s", exc)


if os.environ.get("TESTING", "").strip().lower() not in {"1", "true", "yes"}:
    _restore_cooldowns_from_db()
    restore_position_monitors()
    scheduler.start()
    start_command_polling()
    start_watchdog()
    threading.Thread(target=_startup_ranking_refresh, daemon=True, name="startup-ranking").start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
