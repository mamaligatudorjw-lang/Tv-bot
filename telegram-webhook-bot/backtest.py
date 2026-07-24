#!/usr/bin/env python3
"""
Backtest harness for crypto signals.

Replays the bot's alert logic against historical Binance klines and simulates
real trades with a fixed Stop-Loss (SL) and Take-Profit (TP).  For each signal
the harness walks through per-candle OHLC data and records whichever level is
touched first:
  - TP hit  → win, P&L = +tp_pct
  - SL hit  → loss, P&L = -sl_pct
  - neither within horizon → timeout, P&L = signed close-to-close move
      (can be positive or negative)

Currently covers `volume_surge_short` / `volume_surge_long` — the newest,
least-validated signal — but the structure is meant to be extended to other
alert types.

Usage (from telegram-webhook-bot/):
    TESTING=1 python backtest.py --days 90 --top 50
    TESTING=1 python backtest.py --symbols BTCUSDT,ETHUSDT,SOLUSDT --days 180
    TESTING=1 python backtest.py --days 90 --sl 2.0 --tp 4.0   # custom R/R

Outputs:
    - human-readable summary table to stdout
    - full per-signal detail to backtest_results.json

Important caveats (read before acting on results):
    - Alert price = close of the day with the volume surge.  The real bot
      fires intraday on a rolling-24h ticker, so it could enter earlier and
      at a different price.  Backtested PnL has a 1-day "look-ahead" because
      the surge is only known at end-of-day in this simulation.
    - Universe = top-N USDT pairs by *current* 24h volume → survivorship /
      selection bias vs the live `broad_pairs` set, which is recomputed
      every cycle from a current-volume floor.
    - SL/TP are checked using candle high/low — the order in which they are
      touched within a candle is not known (ambiguity only arises when both
      SL and TP are inside the same candle).  When that happens the harness
      conservatively counts it as an SL hit.
    - Backtest uses the *same* thresholds the live bot uses (imported from
      app.py).  Sweep by editing constants in app.py and re-running.
"""
import os
import sys
import json
import time
import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# Suppress app.py's module-level scheduler/polling/watchdog so importing
# the module doesn't start a real bot.
os.environ.setdefault("TESTING", "1")

import app  # noqa: E402


# Default SL/TP — can be overridden via CLI
DEFAULT_SL_PCT = 2.0   # stop-loss: entry moves against us by this % → out
DEFAULT_TP_PCT = 4.0   # take-profit: entry moves in our favour by this % → out

HORIZONS = [
    ("1h",  1 * 60 * 60),
    ("4h",  4 * 60 * 60),
    ("24h", 1 * 24 * 60 * 60),
    ("3d",  3 * 24 * 60 * 60),
    ("7d",  7 * 24 * 60 * 60),
]

# CRSI needs rank_p + 2 = 102 daily closes minimum.
CRSI_WARMUP_DAYS = 102


def fetch_daily_klines(symbol: str, limit: int):
    """Pull `limit` daily candles for `symbol`. Returns list of dicts."""
    resp = app._binance_get(
        "/api/v3/klines",
        params={"symbol": symbol, "interval": "1d", "limit": limit},
        timeout=15,
    )
    rows = resp.json()
    return [
        {
            "open_time":    int(r[0]),
            "close":        float(r[4]),
            "volume_quote": float(r[7]),  # USDT-denominated volume
        }
        for r in rows
    ]


def fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int):
    """Pull `interval` candles in [start, end]. Returns list of dicts with
    open_time (ms), open, high, low, close."""
    resp = app._binance_get(
        "/api/v3/klines",
        params={
            "symbol":    symbol,
            "interval":  interval,
            "startTime": start_ms,
            "endTime":   end_ms,
            "limit":     1000,
        },
        timeout=15,
    )
    rows = resp.json()
    return [
        {
            "open_time": int(r[0]),
            "open":      float(r[1]),
            "high":      float(r[2]),
            "low":       float(r[3]),
            "close":     float(r[4]),
        }
        for r in rows
    ]


# Bar length in ms for each interval we use.
_INTERVAL_MS = {"15m": 15 * 60 * 1000, "1h": 60 * 60 * 1000}


def simulate_sltp(candles: list, entry: float, direction: int,
                  sl_pct: float, tp_pct: float) -> tuple[str, float]:
    """Walk through OHLC candles starting just after entry and return the
    outcome + realised P&L:

      direction: +1 = LONG, -1 = SHORT
      Returns: (outcome, pnl_pct)
        outcome in {"tp", "sl", "timeout"}
        pnl_pct is signed raw % (not levered).
    """
    if not candles or entry <= 0:
        return "timeout", 0.0

    sl_level = entry * (1 - sl_pct / 100.0) if direction == +1 else entry * (1 + sl_pct / 100.0)
    tp_level = entry * (1 + tp_pct / 100.0) if direction == +1 else entry * (1 - tp_pct / 100.0)

    for c in candles:
        high = c["high"]
        low  = c["low"]

        if direction == +1:
            sl_hit = low  <= sl_level
            tp_hit = high >= tp_level
        else:
            sl_hit = high >= sl_level
            tp_hit = low  <= tp_level

        # If both happen in the same candle, conservatively assume SL hit first.
        if sl_hit:
            return "sl", -sl_pct
        if tp_hit:
            return "tp", +tp_pct

    # Horizon elapsed without hitting either level — use final close as P&L.
    final_close = candles[-1]["close"]
    raw_pct = (final_close - entry) / entry * 100.0 * direction
    return "timeout", raw_pct


def simulate_volume_surge_for_symbol(symbol: str, backtest_days: int,
                                      btc_pct24_by_day: dict,
                                      crsi_short: float = 75.0,
                                      crsi_long: float = 25.0):
    """Walk forward through daily candles and identify each day where the
    bot would have fired a volume_surge alert. Returns list of candidate
    signals (without outcome yet — that needs 1h klines)."""
    total_days = backtest_days + CRSI_WARMUP_DAYS
    dailies = fetch_daily_klines(symbol, limit=total_days)
    if len(dailies) < CRSI_WARMUP_DAYS + 2:
        return []

    signals = []
    for i in range(CRSI_WARMUP_DAYS, len(dailies) - 1):
        today = dailies[i]
        yest  = dailies[i - 1]
        if yest["volume_quote"] <= 0 or today["volume_quote"] <= 0:
            continue
        pct = (today["volume_quote"] - yest["volume_quote"]) / yest["volume_quote"] * 100.0
        if pct < app.VOLUME_SURGE_PCT:
            continue
        closes = [d["close"] for d in dailies[: i + 1]]
        crsi = app._calculate_crsi(closes)
        if crsi is None:
            continue
        if crsi >= crsi_short:
            kind, direction = "volume_surge_short", -1
        elif crsi <= crsi_long:
            kind, direction = "volume_surge_long", +1
        else:
            continue
        # Alert moment ≈ close of the surge day = next UTC midnight.
        alert_ms = today["open_time"] + 24 * 60 * 60 * 1000
        pct24 = ((today["close"] - yest["close"]) / yest["close"] * 100.0
                 if yest["close"] > 0 else None)
        btc_pct24 = btc_pct24_by_day.get(today["open_time"])
        score = app.compute_signal_score(
            kind, "sell" if direction == -1 else "buy",
            pct24=pct24, btc_pct24=btc_pct24,
        )
        signals.append({
            "symbol":      symbol,
            "kind":        kind,
            "direction":   direction,
            "alert_ms":    alert_ms,
            "alert_price": today["close"],
            "pct_volume":  pct,
            "crsi":        crsi,
            "pct24":       pct24,
            "btc_pct24":   btc_pct24,
            "score":       score,
        })
    return signals


def _evaluate_one(s, max_horizon_s, sl_pct: float, tp_pct: float):
    """Worker: fetch 1h klines for one signal and compute SL/TP outcome at
    each horizon window.  Earlier horizons use only candles within that window;
    the full-range candles are fetched once and sliced."""
    start = s["alert_ms"]
    try:
        klines_1h = fetch_klines(
            s["symbol"], "1h",
            start,
            start + (max_horizon_s + 3600) * 1000,
        )
    except Exception as e:
        return None, f"{s['symbol']} skip ({e})"

    entry = s["alert_price"]
    direction = s["direction"]
    rec = {**s, "horizons": {}}

    for label, secs in HORIZONS:
        # Keep only candles that open within the horizon window.
        cutoff_ms = start + secs * 1000
        window = [c for c in klines_1h if c["open_time"] < cutoff_ms]
        if not window:
            rec["horizons"][label] = None
            continue
        outcome, pnl_pct = simulate_sltp(window, entry, direction, sl_pct, tp_pct)
        rec["horizons"][label] = {
            "outcome":  outcome,   # "tp" | "sl" | "timeout"
            "pnl_pct":  pnl_pct,
            "win":      outcome == "tp",
            "loss":     outcome == "sl",
            "timeout":  outcome == "timeout",
        }
    return rec, None


def evaluate_signals(signals, sl_pct: float, tp_pct: float, workers: int = 8):
    """Pull klines per signal in parallel; compute SL/TP outcome at each horizon."""
    out = []
    max_horizon_s = max(secs for _, secs in HORIZONS)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_evaluate_one, s, max_horizon_s, sl_pct, tp_pct)
                   for s in signals]
        done = 0
        for f in as_completed(futures):
            rec, err = f.result()
            done += 1
            if err:
                print(f"  [eval {done}/{len(signals)}] {err}", file=sys.stderr)
                continue
            out.append(rec)
            if done % 25 == 0:
                print(f"  evaluated {done}/{len(signals)}", file=sys.stderr)
    return out


def _wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson confidence interval for a binomial proportion."""
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half   = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return ((center - half) * 100.0, (center + half) * 100.0)


def _per_horizon_stats(rs: list, label: str) -> dict | None:
    """Compute SL/TP stats for a list of signals at one horizon label."""
    valid = [r for r in rs if r["horizons"].get(label)]
    if not valid:
        return None
    n        = len(valid)
    tp_hits  = sum(1 for r in valid if r["horizons"][label]["win"])
    sl_hits  = sum(1 for r in valid if r["horizons"][label]["loss"])
    timeouts = sum(1 for r in valid if r["horizons"][label]["timeout"])
    avg_pnl  = sum(r["horizons"][label]["pnl_pct"] for r in valid) / n
    lo, hi   = _wilson_ci(tp_hits, n)
    return {
        "n":          n,
        "tp_hits":    tp_hits,
        "sl_hits":    sl_hits,
        "timeouts":   timeouts,
        "tp_rate":    tp_hits  / n * 100.0,
        "sl_rate":    sl_hits  / n * 100.0,
        "tp_ci95":    [lo, hi],
        "avg_pnl":    avg_pnl,
        # legacy compat
        "win_rate":   tp_hits  / n * 100.0,
        "win_rate_ci95": [lo, hi],
        "avg_signed_pct": avg_pnl,
    }


def summarize(results, sl_pct: float, tp_pct: float):
    by_kind = defaultdict(list)
    for r in results:
        by_kind[r["kind"]].append(r)
    summary = {}
    for kind, rs in by_kind.items():
        per_horizon = {}
        for label, _ in HORIZONS:
            per_horizon[label] = _per_horizon_stats(rs, label)
        summary[kind] = {
            "total_signals": len(rs),
            "sl_pct":        sl_pct,
            "tp_pct":        tp_pct,
            "horizons":      per_horizon,
        }
    return summary


def summarize_crsi_sweep(results, short_grid, long_grid, sl_pct, tp_pct):
    """For each candidate (short, long) CRSI cutoff, filter the already-
    evaluated signals and report SL/TP outcome at every horizon."""
    sweep = []
    for st in short_grid:
        rs = [r for r in results
              if r["kind"] == "volume_surge_short" and r["crsi"] >= st]
        sweep.append(("SHORT", st, _per_kind(rs, sl_pct, tp_pct)))
    for lt in long_grid:
        rs = [r for r in results
              if r["kind"] == "volume_surge_long" and r["crsi"] <= lt]
        sweep.append(("LONG", lt, _per_kind(rs, sl_pct, tp_pct)))
    return sweep


def _per_kind(rs, sl_pct, tp_pct):
    out = {"n_total": len(rs)}
    for label, _ in HORIZONS:
        out[label] = _per_horizon_stats(rs, label)
    return out


def print_sweep(sweep):
    print("CRSI threshold sweep (live default is short=75, long=25):")
    print(f"  {'side':<6} {'thr':>5} {'n':>5}   "
          f"{'24h':>20}   {'3d':>20}   {'7d':>20}")
    print(f"  {'':<6} {'':>5} {'':>5}   "
          f"{'tp%  sl% avg_pnl':>20}   {'tp%  sl% avg_pnl':>20}   {'tp%  sl% avg_pnl':>20}")
    for side, thr, h in sweep:
        cells = []
        for label in ("24h", "3d", "7d"):
            v = h.get(label)
            if v is None:
                cells.append(f"{'—':>20}")
            else:
                cells.append(
                    f"{v['tp_rate']:>5.1f}% {v['sl_rate']:>4.1f}% {v['avg_pnl']:>+6.2f}%"
                )
        print(f"  {side:<6} {thr:>5.0f} {h['n_total']:>5}   "
              f"{cells[0]}   {cells[1]}   {cells[2]}")
    print()


def summarize_by_score_bucket(results, sl_pct, tp_pct):
    buckets = {
        "score>=60": [r for r in results if r.get("score", 0) >= 60],
        "score<60":  [r for r in results if r.get("score", 0) <  60],
    }
    out = {}
    for name, rs in buckets.items():
        per_horizon = {}
        for label, _ in HORIZONS:
            per_horizon[label] = _per_horizon_stats(rs, label)
        out[name] = {"total": len(rs), "horizons": per_horizon}
    return out


def print_table(summary, sl_pct: float, tp_pct: float):
    rr = tp_pct / sl_pct
    print()
    print("=" * 80)
    print(f"  Backtest: SL={sl_pct:.1f}%  TP={tp_pct:.1f}%  R/R=1:{rr:.1f}")
    print(f"  Outcome: 'tp' = TP hit first, 'sl' = SL hit first, "
          f"'timeout' = horizon elapsed (P&L = signed close)")
    print("=" * 80)
    if not summary:
        print("  No signals fired in the backtest window.")
        return
    for kind, data in summary.items():
        print(f"\n  {kind}: {data['total_signals']} signals")
        print(f"  {'horizon':<8} {'n':>5}  {'TP%':>7}  {'SL%':>7}  "
              f"{'tmout%':>7}  {'95% CI (TP)':>15}  {'avg_pnl':>9}")
        for label, h in data["horizons"].items():
            if h is None:
                print(f"  {label:<8} {'—':>5}")
            else:
                ci  = f"[{h['tp_ci95'][0]:.1f},{h['tp_ci95'][1]:.1f}]"
                tmo = h["timeouts"] / h["n"] * 100.0
                print(f"  {label:<8} {h['n']:>5}  "
                      f"{h['tp_rate']:>6.1f}%  {h['sl_rate']:>6.1f}%  "
                      f"{tmo:>6.1f}%  {ci:>15}  {h['avg_pnl']:>+8.2f}%")
    print()


def resolve_symbols(args):
    if args.symbols:
        return [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    print(f"Fetching top {args.top} USDT pairs by 24h volume…", file=sys.stderr)
    resp = app._binance_get("/api/v3/ticker/24hr", timeout=20)
    tickers = resp.json()
    bad_tokens = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
    stables = {"USDCUSDT", "BUSDUSDT", "TUSDUSDT", "FDUSDUSDT", "DAIUSDT"}
    usdt = [
        t for t in tickers
        if t["symbol"].endswith("USDT")
        and not any(b in t["symbol"] for b in bad_tokens)
        and t["symbol"] not in stables
    ]
    usdt.sort(key=lambda t: float(t.get("quoteVolume", 0)), reverse=True)
    return [t["symbol"] for t in usdt[: args.top]]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days",    type=int,   default=90,
                        help="Backtest window in days")
    parser.add_argument("--top",     type=int,   default=50,
                        help="Top N USDT pairs by 24h vol")
    parser.add_argument("--symbols", default="",
                        help="Comma-separated symbols (overrides --top)")
    parser.add_argument("--sl",      type=float, default=DEFAULT_SL_PCT,
                        help=f"Stop-loss %% (default {DEFAULT_SL_PCT})")
    parser.add_argument("--tp",      type=float, default=DEFAULT_TP_PCT,
                        help=f"Take-profit %% (default {DEFAULT_TP_PCT})")
    parser.add_argument("--out",     default="backtest_results.json")
    parser.add_argument("--sweep-crsi", action="store_true",
                        help="Also print a CRSI-threshold sweep table")
    parser.add_argument("--crsi-short", type=float, default=75.0)
    parser.add_argument("--crsi-long",  type=float, default=25.0)
    args = parser.parse_args()

    sl_pct = args.sl
    tp_pct = args.tp

    symbols = resolve_symbols(args)
    print(f"Backtesting {len(symbols)} symbols over ~{args.days} days  "
          f"SL={sl_pct:.1f}%  TP={tp_pct:.1f}%  R/R=1:{tp_pct/sl_pct:.1f}\n"
          f"(thresholds: vol_pct≥{app.VOLUME_SURGE_PCT}%, "
          f"crsi≥{app.CRSI_OVERBOUGHT} or ≤{app.CRSI_OVERSOLD})",
          file=sys.stderr)

    print("Fetching BTC history for score context…", file=sys.stderr)
    btc_daily = fetch_daily_klines("BTCUSDT", limit=args.days + CRSI_WARMUP_DAYS)
    btc_pct24_by_day = {}
    for i in range(1, len(btc_daily)):
        prev_c = btc_daily[i - 1]["close"]
        if prev_c > 0:
            btc_pct24_by_day[btc_daily[i]["open_time"]] = (
                (btc_daily[i]["close"] - prev_c) / prev_c * 100.0
            )

    all_signals: list = []
    def _scan(sym):
        try:
            return sym, simulate_volume_surge_for_symbol(
                sym, args.days, btc_pct24_by_day,
                crsi_short=args.crsi_short, crsi_long=args.crsi_long,
            ), None
        except Exception as e:
            return sym, [], str(e)
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(_scan, s) for s in symbols]
        done = 0
        for f in as_completed(futures):
            sym, sigs, err = f.result()
            done += 1
            if err:
                print(f"  [{done}/{len(symbols)}] {sym}: skip ({err})", file=sys.stderr)
                continue
            if sigs:
                print(f"  [{done}/{len(symbols)}] {sym}: {len(sigs)} candidate alerts",
                      file=sys.stderr)
            all_signals.extend(sigs)

    print(f"\n→ Found {len(all_signals)} candidate alerts.  "
          f"Simulating SL={sl_pct:.1f}% / TP={tp_pct:.1f}% on candle OHLC…",
          file=sys.stderr)
    results = evaluate_signals(all_signals, sl_pct=sl_pct, tp_pct=tp_pct)

    summary = summarize(results, sl_pct=sl_pct, tp_pct=tp_pct)
    print_table(summary, sl_pct=sl_pct, tp_pct=tp_pct)

    score_summary = summarize_by_score_bucket(results, sl_pct=sl_pct, tp_pct=tp_pct)
    print("By score bucket (filter is MIN_ALERT_SCORE=60 in production):")
    for name, data in score_summary.items():
        print(f"\n  {name}: {data['total']} signals")
        print(f"  {'horizon':<8} {'n':>5}  {'TP%':>7}  {'SL%':>7}  {'avg_pnl':>9}")
        for label, h in data["horizons"].items():
            if h is None:
                print(f"  {label:<8} {'-':>5}")
            else:
                print(f"  {label:<8} {h['n']:>5}  "
                      f"{h['tp_rate']:>6.1f}%  {h['sl_rate']:>6.1f}%  {h['avg_pnl']:>+8.2f}%")
    print()

    sweep = None
    if args.sweep_crsi:
        sweep = summarize_crsi_sweep(
            results,
            short_grid=[75, 80, 85, 90, 95],
            long_grid=[25, 20, 15, 10, 5],
            sl_pct=sl_pct,
            tp_pct=tp_pct,
        )
        print_sweep(sweep)

    out_path = os.path.join(os.path.dirname(__file__), args.out)
    with open(out_path, "w") as f:
        payload = {
            "sl_pct":       sl_pct,
            "tp_pct":       tp_pct,
            "summary":      summary,
            "score_summary": score_summary,
            "details":      results,
        }
        if sweep is not None:
            payload["crsi_sweep"] = [
                {"side": side, "threshold": thr, "stats": stats}
                for side, thr, stats in sweep
            ]
        json.dump(payload, f, indent=2)
    print(f"Detailed results written to {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
