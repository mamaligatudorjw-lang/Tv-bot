#!/usr/bin/env python3
"""
Симуляция оптимизированных настроек бота на реальных данных из alerts.db.
Сравнивает: до оптимизации vs после (новые score-пороги, отключённый down_3).

Запуск:
    cd telegram-webhook-bot && TESTING=1 python sim_optimized.py
"""
import os, sys, sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed

os.environ.setdefault("TESTING", "1")
import app  # noqa

# ---------------------------------------------------------------------------
# Конфигурация — новые правила
# ---------------------------------------------------------------------------
MIN_SCORE_BY_TYPE_NEW = {
    "overheated_24h":   70,
    "momentum_down_5":  65,
    "momentum_down_10": 65,
}
MIN_SCORE_DEFAULT = 60
DISABLED_TYPES = {"momentum_down_3", "momentum_down_2"}

SL_PCT        = 0.02
TP_PCT        = 0.04
HORIZON_HOURS = 4

# ---------------------------------------------------------------------------
# EMA helper
# ---------------------------------------------------------------------------
def ema(values, period):
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e

# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------
_kline_1h_cache: dict = {}
_kline_4h_cache: dict = {}
_HISTORY_CANDLES = 700

def _preload_1h(symbol, ts):
    key = (symbol, ts)
    if key in _kline_1h_cache:
        return _kline_1h_cache[key]
    try:
        resp = app._binance_get(
            "/api/v3/klines",
            params={"symbol": symbol, "interval": "1h",
                    "startTime": ts * 1000,
                    "endTime": (ts + HORIZON_HOURS * 3600 + 3600) * 1000,
                    "limit": HORIZON_HOURS + 2},
            timeout=10,
        )
        candles = [{"open_time_ms": int(c[0]), "high": float(c[2]),
                    "low": float(c[3]), "close": float(c[4])}
                   for c in resp.json()]
        _kline_1h_cache[key] = candles
        return candles
    except Exception as exc:
        print(f"  [warn] 1h {symbol}@{ts}: {exc}", file=sys.stderr)
        _kline_1h_cache[key] = []
        return []

def _preload_4h(symbol):
    if symbol in _kline_4h_cache:
        return _kline_4h_cache[symbol]
    try:
        resp = app._binance_get(
            "/api/v3/klines",
            params={"symbol": symbol, "interval": "4h", "limit": _HISTORY_CANDLES},
            timeout=10,
        )
        candles = [{"open_time_ms": int(c[0]), "close": float(c[4])}
                   for c in resp.json()]
        _kline_4h_cache[symbol] = candles
        return candles
    except Exception as exc:
        print(f"  [warn] 4h {symbol}: {exc}", file=sys.stderr)
        _kline_4h_cache[symbol] = []
        return []

def price_lookup(symbol, ts, minutes_ahead):
    candles = _kline_1h_cache.get((symbol, ts), [])
    target_ms = (ts + minutes_ahead * 60) * 1000
    for c in candles:
        if c["open_time_ms"] <= target_ms < c["open_time_ms"] + 3_600_000:
            return c["close"]
    return None

def coin_history_lookup(symbol, ts):
    ts_ms = ts * 1000
    return [c["close"] for c in _kline_4h_cache.get(symbol, [])
            if c["open_time_ms"] < ts_ms]

def short_allowed_ema200(coin_closes_4h, price):
    if not coin_closes_4h or len(coin_closes_4h) < 200:
        return True
    ema200 = ema(coin_closes_4h[-200:], 200)
    return price <= ema200

# ---------------------------------------------------------------------------
# Load alerts
# ---------------------------------------------------------------------------
_DB = os.path.join(os.path.dirname(__file__), "alerts.db")

def load_alerts(signal_type):
    conn = sqlite3.connect(_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT symbol, price_at_alert AS entry_price, ts AS timestamp, score "
        "FROM alerts "
        "WHERE alert_type=? AND recommendation='SHORT' AND price_at_alert>0",
        (signal_type,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ---------------------------------------------------------------------------
# Simulate
# ---------------------------------------------------------------------------
def simulate(alerts, use_score_filter: bool, use_ema200: bool,
             sl=SL_PCT, tp=TP_PCT):
    res = {"count": 0, "tp": 0, "sl": 0, "pnl_sum": 0.0, "skipped_score": 0,
           "skipped_ema": 0, "total": len(alerts)}
    for a in alerts:
        sig = a.get("_type", "unknown")

        # Score filter
        if use_score_filter:
            min_s = MIN_SCORE_BY_TYPE_NEW.get(sig, MIN_SCORE_DEFAULT)
            sc = a.get("score")
            if sc is None or sc < min_s:
                res["skipped_score"] += 1
                continue

        # EMA-200 filter
        if use_ema200:
            hist = coin_history_lookup(a["symbol"], a["timestamp"])
            if not short_allowed_ema200(hist, a["entry_price"]):
                res["skipped_ema"] += 1
                continue

        entry = a["entry_price"]
        outcome = None
        for m in range(1, HORIZON_HOURS * 60 + 1):
            p = price_lookup(a["symbol"], a["timestamp"], m)
            if p is None:
                continue
            change = (entry - p) / entry
            if change >= tp:
                outcome = ("tp", tp); break
            if change <= -sl:
                outcome = ("sl", -sl); break
        if outcome is None:
            last = price_lookup(a["symbol"], a["timestamp"], HORIZON_HOURS * 60)
            change = (entry - last) / entry if last else 0.0
            outcome = ("timeout", change)

        res["count"] += 1
        res["pnl_sum"] += outcome[1]
        if outcome[0] == "tp": res["tp"] += 1
        if outcome[0] == "sl": res["sl"] += 1

    n = max(res["count"], 1)
    return {**res,
            "tp_pct":      round(res["tp"]  / n * 100, 1),
            "sl_pct":      round(res["sl"]  / n * 100, 1),
            "avg_pnl_pct": round(res["pnl_sum"] / n * 100, 2)}

# ---------------------------------------------------------------------------
# Prefetch
# ---------------------------------------------------------------------------
def prefetch(all_alerts):
    symbols    = {a["symbol"] for alerts in all_alerts.values() for a in alerts}
    alert_keys = {(a["symbol"], a["timestamp"])
                  for alerts in all_alerts.values() for a in alerts}
    total = len(symbols) + len(alert_keys)
    done  = 0
    print(f"Предзагрузка: {len(symbols)} монет (4h) + {len(alert_keys)} алертов (1h)…",
          flush=True)
    with ThreadPoolExecutor(max_workers=12) as ex:
        fs = {}
        for sym in symbols:
            fs[ex.submit(_preload_4h, sym)] = f"4h:{sym}"
        for sym, ts in alert_keys:
            fs[ex.submit(_preload_1h, sym, ts)] = f"1h:{sym}@{ts}"
        for f in as_completed(fs):
            done += 1
            if done % 30 == 0:
                print(f"  {done}/{total}", flush=True)
            try: f.result()
            except Exception as e:
                print(f"  err {fs[f]}: {e}", file=sys.stderr)
    print("Готово.\n", flush=True)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
SIGNALS = ["overheated_24h", "momentum_down_3", "momentum_down_5", "momentum_down_10"]

def main():
    all_alerts = {}
    for sig in SIGNALS:
        alerts = load_alerts(sig)
        for a in alerts:
            a["_type"] = sig
        all_alerts[sig] = alerts
        print(f"  {sig}: {len(alerts)} алертов в БД")

    total = sum(len(v) for v in all_alerts.values())
    print(f"\nВсего: {total} SHORT-алертов\n")
    prefetch(all_alerts)

    hdr = (f"{'Сигнал':<22} {'Режим':<28} {'Всего':>6} {'Сделок':>7} "
           f"{'TP%':>7} {'SL%':>7} {'avgP&L':>9}")
    print(hdr)
    print("─" * len(hdr))

    for sig, alerts in all_alerts.items():
        if not alerts:
            print(f"{sig:<22}  нет данных в БД")
            continue

        disabled = sig in DISABLED_TYPES
        label_dis = "ОТКЛЮЧЁН" if disabled else ""

        if disabled:
            # Show what would have happened without any filter
            r = simulate(alerts, use_score_filter=False, use_ema200=False)
            def row(label, r_):
                return (f"{sig:<22} {label:<28} {r_['total']:>6} {r_['count']:>7} "
                        f"{r_['tp_pct']:>6.1f}% {r_['sl_pct']:>6.1f}% "
                        f"{r_['avg_pnl_pct']:>+8.2f}%  ← {label_dis}")
            print(row("без фильтров (был)", r))
            print(f"{'':22} {'→ ОТКЛЮЧЁН в новой версии':<28}")
            print()
            continue

        configs = [
            ("было: EMA-200",          False, True),
            ("стало: EMA-200 + score",  True,  True),
        ]
        rows_out = []
        for label, use_sc, use_ema in configs:
            r = simulate(alerts, use_score_filter=use_sc, use_ema200=use_ema)
            rows_out.append((label, r))

        for label, r in rows_out:
            skipped_info = ""
            if r.get("skipped_score", 0) or r.get("skipped_ema", 0):
                skipped_info = (f"  (−{r.get('skipped_ema',0)} EMA, "
                                f"−{r.get('skipped_score',0)} score)")
            line = (f"{sig:<22} {label:<28} {r['total']:>6} {r['count']:>7} "
                    f"{r['tp_pct']:>6.1f}% {r['sl_pct']:>6.1f}% "
                    f"{r['avg_pnl_pct']:>+8.2f}%{skipped_info}")
            print(line)

        before = rows_out[0][1]["avg_pnl_pct"]
        after  = rows_out[1][1]["avg_pnl_pct"]
        delta  = after - before
        sign   = "+" if delta >= 0 else ""
        print(f"{'':22} {'→ Δ avg P&L':28} {'':>6} {'':>7} "
              f"{'':>7} {'':>7} {sign}{delta:>+7.2f}%")
        print()

if __name__ == "__main__":
    main()
