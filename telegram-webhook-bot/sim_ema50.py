#!/usr/bin/env python3
"""
Симуляция фильтра EMA-50 на реальных SHORT-алертах из alerts.db.

Запуск (из папки telegram-webhook-bot/):
    TESTING=1 python sim_ema50.py
"""
import os, sys, sqlite3, time
from concurrent.futures import ThreadPoolExecutor, as_completed

os.environ.setdefault("TESTING", "1")
import app  # noqa: E402  (подтягивает _binance_get и _calculate_ema)

# ---------------------------------------------------------------------------
# Настройки фильтра (код пользователя — не трогаем)
# ---------------------------------------------------------------------------
USE_EMA200_FILTER = True
USE_EMA50_FILTER  = False   # переключается автоматически в цикле сравнения

SL_PCT  = 0.02
TP_PCT  = 0.04
HORIZON_HOURS = 4

def ema(values, period):
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e

def short_allowed(coin_closes_4h):
    if not coin_closes_4h:
        return True   # нет истории → не фильтруем (не штрафуем за отсутствие данных)
    price = coin_closes_4h[-1]
    if USE_EMA200_FILTER and len(coin_closes_4h) >= 200:
        if price > ema(coin_closes_4h[-200:], 200):
            return False
    if USE_EMA50_FILTER and len(coin_closes_4h) >= 50:
        if price > ema(coin_closes_4h[-50:], 50):
            return False
    return True

def simulate_short(alerts, price_lookup, coin_history_lookup,
                   sl=SL_PCT, tp=TP_PCT, horizon_hours=HORIZON_HOURS):
    results = {"count": 0, "tp": 0, "sl": 0, "pnl_sum": 0.0, "skipped": 0}
    for a in alerts:
        history = coin_history_lookup(a["symbol"], a["timestamp"])
        if not short_allowed(history):
            results["skipped"] += 1
            continue
        entry = a["entry_price"]
        outcome = None
        for m in range(1, horizon_hours * 60 + 1):
            price = price_lookup(a["symbol"], a["timestamp"], m)
            if price is None:
                continue
            change = (entry - price) / entry
            if change >= tp:
                outcome = ("tp", tp); break
            if change <= -sl:
                outcome = ("sl", -sl); break
        if outcome is None:
            last = price_lookup(a["symbol"], a["timestamp"], horizon_hours * 60)
            change = (entry - last) / entry if last else 0.0
            outcome = ("timeout", change)
        results["count"]   += 1
        results["pnl_sum"] += outcome[1]
        if outcome[0] == "tp": results["tp"] += 1
        if outcome[0] == "sl": results["sl"] += 1
    n = results["count"] or 1
    return {
        "trades":       results["count"],
        "skipped":      results["skipped"],
        "tp_pct":       round(results["tp"]  / n * 100, 1),
        "sl_pct":       round(results["sl"]  / n * 100, 1),
        "avg_pnl_pct":  round(results["pnl_sum"] / n * 100, 2),
    }

# ---------------------------------------------------------------------------
# load_alerts — alerts.db
# ---------------------------------------------------------------------------
_DB_PATH = os.path.join(os.path.dirname(__file__), "alerts.db")

def load_alerts(signal_type: str) -> list[dict]:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT symbol, price_at_alert AS entry_price, ts AS timestamp "
        "FROM alerts "
        "WHERE alert_type = ? AND recommendation = 'SHORT' AND price_at_alert > 0",
        (signal_type,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ---------------------------------------------------------------------------
# price_lookup — предзагрузка 1h OHLC для каждого (symbol, ts)
#
# Для каждой 1-часовой свечи после alert_ts возвращаем её close всем минутам
# внутри этого часа.  Это означает, что цикл simulate_short «видит» цену
# 60 раз подряд — первое же попадание в SL/TP внутри часа фиксируется в
# начале часа.  Для сравнительного теста (с фильтром vs без) систематическая
# погрешность одинакова и не влияет на вывод.
# ---------------------------------------------------------------------------
_kline_1h_cache: dict[tuple, list] = {}   # (symbol, ts) -> [{open_time_ms, high, low, close}]

def _preload_1h(symbol: str, ts: int) -> list:
    """Fetch 1h OHLC candles covering [ts, ts + HORIZON_HOURS] for one alert."""
    key = (symbol, ts)
    if key in _kline_1h_cache:
        return _kline_1h_cache[key]
    try:
        resp = app._binance_get(
            "/api/v3/klines",
            params={
                "symbol":    symbol,
                "interval":  "1h",
                "startTime": ts * 1000,
                "endTime":   (ts + HORIZON_HOURS * 3600 + 3600) * 1000,
                "limit":     HORIZON_HOURS + 2,
            },
            timeout=10,
        )
        candles = [
            {
                "open_time_ms": int(c[0]),
                "high":         float(c[2]),
                "low":          float(c[3]),
                "close":        float(c[4]),
            }
            for c in resp.json()
        ]
        _kline_1h_cache[key] = candles
        return candles
    except Exception as exc:
        print(f"  [warn] 1h klines fetch failed {symbol} ts={ts}: {exc}", file=sys.stderr)
        _kline_1h_cache[key] = []
        return []

def price_lookup(symbol: str, ts: int, minutes_ahead: int) -> float | None:
    """Return the close of the 1h candle that contains (ts + minutes_ahead)."""
    candles = _kline_1h_cache.get((symbol, ts))
    if not candles:
        return None
    target_ms = (ts + minutes_ahead * 60) * 1000
    for c in candles:
        if c["open_time_ms"] <= target_ms < c["open_time_ms"] + 3_600_000:
            return c["close"]
    return None

# ---------------------------------------------------------------------------
# coin_history_lookup — предзагрузка 4h свечей по символу
#
# EMA-200 требует 200 свечей (= 800 ч ≈ 33 дня).
# Загружаем 220 свечей 4h на символ один раз, фильтруем по ts.
# ---------------------------------------------------------------------------
_HISTORY_CANDLES = 700  # 700×4h = 117 дней; покрывает EMA-200 + всю историю алертов в DB
_kline_4h_cache: dict[str, list] = {}   # symbol -> [{open_time_ms, close}] sorted asc

def _preload_4h(symbol: str) -> list:
    if symbol in _kline_4h_cache:
        return _kline_4h_cache[symbol]
    try:
        resp = app._binance_get(
            "/api/v3/klines",
            params={
                "symbol":   symbol,
                "interval": "4h",
                "limit":    _HISTORY_CANDLES,
            },
            timeout=10,
        )
        candles = [
            {"open_time_ms": int(c[0]), "close": float(c[4])}
            for c in resp.json()
        ]
        _kline_4h_cache[symbol] = candles
        return candles
    except Exception as exc:
        print(f"  [warn] 4h klines fetch failed {symbol}: {exc}", file=sys.stderr)
        _kline_4h_cache[symbol] = []
        return []

def coin_history_lookup(symbol: str, ts: int) -> list:
    """Return list of 4h close prices strictly before `ts`."""
    candles = _kline_4h_cache.get(symbol, [])
    ts_ms = ts * 1000
    return [c["close"] for c in candles if c["open_time_ms"] < ts_ms]

# ---------------------------------------------------------------------------
# Pre-fetch all needed data in parallel before running the simulation
# ---------------------------------------------------------------------------
SHORT_SIGNALS = ["overheated_24h", "confluence", "momentum_down_1"]

def prefetch_all(all_alerts: dict[str, list]) -> None:
    symbols   = {a["symbol"] for alerts in all_alerts.values() for a in alerts}
    alert_keys = [(a["symbol"], a["timestamp"])
                  for alerts in all_alerts.values() for a in alerts]

    total = len(symbols) + len(alert_keys)
    done  = 0

    print(f"Предзагрузка данных: {len(symbols)} монет (4h) + "
          f"{len(alert_keys)} алертов (1h)…", flush=True)

    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = {}
        for sym in symbols:
            futures[ex.submit(_preload_4h, sym)] = f"4h:{sym}"
        for sym, ts in alert_keys:
            futures[ex.submit(_preload_1h, sym, ts)] = f"1h:{sym}@{ts}"

        for f in as_completed(futures):
            done += 1
            if done % 20 == 0:
                print(f"  {done}/{total}", flush=True)
            try:
                f.result()
            except Exception as e:
                print(f"  prefetch error ({futures[f]}): {e}", file=sys.stderr)

    print("Готово.", flush=True)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    global USE_EMA200_FILTER, USE_EMA50_FILTER

    all_alerts = {sig: load_alerts(sig) for sig in SHORT_SIGNALS}
    n_total = sum(len(v) for v in all_alerts.values())
    print(f"\nЗагружено алертов: {n_total} SHORT "
          f"({', '.join(f'{sig}={len(v)}' for sig, v in all_alerts.items())})\n")

    prefetch_all(all_alerts)

    # ---- таблица сравнения ----
    print()
    header = f"{'Сигнал':<22} {'Фильтр':<22} {'Сделок':>7} {'Отсеяно':>8} " \
             f"{'TP%':>7} {'SL%':>7} {'avgP&L':>9}"
    print(header)
    print("-" * len(header))

    for sig, alerts in all_alerts.items():
        if not alerts:
            print(f"{sig:<22}  нет данных")
            continue

        # Вариант 1: без фильтров
        USE_EMA200_FILTER = False
        USE_EMA50_FILTER  = False
        res_none = simulate_short(alerts, price_lookup, coin_history_lookup)

        # Вариант 2: только EMA-200
        USE_EMA200_FILTER = True
        USE_EMA50_FILTER  = False
        res_ema200 = simulate_short(alerts, price_lookup, coin_history_lookup)

        # Вариант 3: EMA-200 + EMA-50
        USE_EMA200_FILTER = True
        USE_EMA50_FILTER  = True
        res_both = simulate_short(alerts, price_lookup, coin_history_lookup)

        def row(label, r):
            return (f"{sig:<22} {label:<22} {r['trades']:>7} {r['skipped']:>8} "
                    f"{r['tp_pct']:>6.1f}% {r['sl_pct']:>6.1f}% "
                    f"{r['avg_pnl_pct']:>+8.2f}%")

        print(row("без фильтра", res_none))
        print(row("EMA-200", res_ema200))
        print(row("EMA-200 + EMA-50", res_both))
        print()

if __name__ == "__main__":
    main()
