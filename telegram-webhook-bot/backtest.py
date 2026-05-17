#!/usr/bin/env python3
"""
Backtest harness for crypto signals.

Replays the bot's alert logic against historical Binance klines and reports
the winrate per alert type at multiple horizons. Currently covers
`volume_surge_short` / `volume_surge_long` — the newest, least-validated
signal — but the structure is meant to be extended to other alert types.

Usage (from telegram-webhook-bot/):
    TESTING=1 python backtest.py --days 90 --top 50
    TESTING=1 python backtest.py --symbols BTCUSDT,ETHUSDT,SOLUSDT --days 180

Outputs:
    - human-readable summary table to stdout
    - full per-signal detail to backtest_results.json

Win definition:
    "Win" = signed price move ≥ WIN_PCT (default 1.0%) in the predicted
    direction, measured at the close of the chosen horizon. Tighter than
    the bot's actual SL/TP logic but a fair, simple yardstick for comparing
    setups across thresholds.

Important caveats (read before acting on results):
    - Alert price = close of the day with the volume surge. The real bot
      fires intraday on a rolling-24h ticker, so it could enter earlier and
      at a different price. Backtested PnL has a 1-day "look-ahead" because
      the surge is only known at end-of-day in this simulation.
    - Universe = top-N USDT pairs by *current* 24h volume → survivorship /
      selection bias vs the live `broad_pairs` set, which is recomputed
      every cycle from a current-volume floor.
    - Sub-hour horizons use 15m candles; >=1h use 1h candles. Each horizon
      reads the close of the candle ending at-or-just-after the target time.
    - Backtest uses the *same* thresholds the live bot uses (imported from
      app.py). Sweep by editing constants in app.py and re-running.
"""
import os
import sys
import json
import time
import argparse
from collections import defaultdict

# Suppress app.py's module-level scheduler/polling/watchdog so importing
# the module doesn't start a real bot.
os.environ.setdefault("TESTING", "1")

import app  # noqa: E402


WIN_PCT = 1.0
HORIZONS = [
    ("15m", 15 * 60),
    ("1h",  60 * 60),
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
    open_time (ms) and close (float)."""
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
    return [{"open_time": int(r[0]), "close": float(r[4])} for r in rows]


# Bar length in ms for each interval we use.
_INTERVAL_MS = {"15m": 15 * 60 * 1000, "1h": 60 * 60 * 1000}


def price_at(candles, target_ms, interval_ms):
    """Return the close of the candle that ends at-or-just-after target_ms.
    Linear scan — candle lists are small. None if target falls outside the
    fetched range."""
    for r in candles:
        if r["open_time"] + interval_ms >= target_ms:
            return r["close"]
    return None


def simulate_volume_surge_for_symbol(symbol: str, backtest_days: int,
                                      btc_pct24_by_day: dict):
    """Walk forward through daily candles and identify each day where the
    bot would have fired a volume_surge alert. Returns list of candidate
    signals (without win/loss yet — that needs 1h klines)."""
    total_days = backtest_days + CRSI_WARMUP_DAYS
    dailies = fetch_daily_klines(symbol, limit=total_days)
    if len(dailies) < CRSI_WARMUP_DAYS + 2:
        return []

    signals = []
    # Start at CRSI_WARMUP_DAYS so _calculate_crsi has enough history;
    # stop at len-1 so there's at least one day after for win-check anchor.
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
        if crsi >= app.CRSI_OVERBOUGHT:
            kind, direction = "volume_surge_short", -1
        elif crsi <= app.CRSI_OVERSOLD:
            kind, direction = "volume_surge_long", +1
        else:
            continue
        # Alert moment ≈ close of the surge day = next UTC midnight.
        alert_ms = today["open_time"] + 24 * 60 * 60 * 1000
        # Symbol's 24h price change on the surge day (input to score).
        pct24 = ((today["close"] - yest["close"]) / yest["close"] * 100.0
                 if yest["close"] > 0 else None)
        # BTC's 24h price change on the same day, if we have it.
        btc_pct24 = btc_pct24_by_day.get(today["open_time"])
        # Replicate the live scoring call. _winrate_for / _feedback_score will
        # return None (empty test DB), so the score is dominated by BTC drift
        # and the small pct24-overshoot bonus for SHORTs — same as a freshly-
        # deployed bot before it accumulates history.
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


def evaluate_signals(signals):
    """Pull klines per signal, compute win/loss at each horizon.
    Uses 15m candles for sub-hour horizons and 1h candles for ≥1h horizons,
    so e.g. the "15m" bucket actually measures price ~15m after alert
    (previously it shared a candle with the 1h bucket — see git history)."""
    out = []
    max_horizon_s = max(secs for _, secs in HORIZONS)
    for i, s in enumerate(signals, 1):
        start = s["alert_ms"]
        try:
            # 15m candles for short horizons (<1h). 4h × 1000 candles is
            # plenty for a 1h horizon, and 15m × 1000 covers >10 days for
            # the sub-hour windows.
            klines_15m = fetch_klines(s["symbol"], "15m",
                                       start, start + 2 * 3600 * 1000)
            klines_1h  = fetch_klines(s["symbol"], "1h",
                                       start, start + (max_horizon_s + 3600) * 1000)
        except Exception as e:
            print(f"  [eval {i}/{len(signals)}] {s['symbol']} skip ({e})",
                  file=sys.stderr)
            continue
        rec = {**s, "horizons": {}}
        for label, secs in HORIZONS:
            if secs < 3600:
                px = price_at(klines_15m, start + secs * 1000, _INTERVAL_MS["15m"])
            else:
                px = price_at(klines_1h,  start + secs * 1000, _INTERVAL_MS["1h"])
            if px is None:
                rec["horizons"][label] = None
                continue
            delta_pct  = (px - s["alert_price"]) / s["alert_price"] * 100.0
            signed_pct = delta_pct * s["direction"]
            rec["horizons"][label] = {
                "price":      px,
                "delta_pct":  delta_pct,
                "signed_pct": signed_pct,
                "win":        signed_pct >= WIN_PCT,
            }
        out.append(rec)
        time.sleep(0.02)
    return out


def _wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson confidence interval for a binomial proportion.
    Returns (lo_pct, hi_pct). Robust at small n where normal-approx breaks."""
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half   = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return ((center - half) * 100.0, (center + half) * 100.0)


def summarize(results):
    by_kind = defaultdict(list)
    for r in results:
        by_kind[r["kind"]].append(r)
    summary = {}
    for kind, rs in by_kind.items():
        per_horizon = {}
        for label, _ in HORIZONS:
            valid = [r for r in rs if r["horizons"].get(label)]
            if not valid:
                per_horizon[label] = None
                continue
            wins  = sum(1 for r in valid if r["horizons"][label]["win"])
            avg   = sum(r["horizons"][label]["signed_pct"] for r in valid) / len(valid)
            lo, hi = _wilson_ci(wins, len(valid))
            per_horizon[label] = {
                "n":              len(valid),
                "win_rate":       wins / len(valid) * 100.0,
                "win_rate_ci95":  [lo, hi],
                "avg_signed_pct": avg,
            }
        summary[kind] = {"total_signals": len(rs), "horizons": per_horizon}
    return summary


def summarize_by_score_bucket(results):
    """Split signals into score≥60 (live-eligible-ish) vs <60 and report
    per-bucket win rates at each horizon. Useful for judging whether the
    MIN_ALERT_SCORE filter improves outcomes."""
    buckets = {"score>=60": [r for r in results if r.get("score", 0) >= 60],
               "score<60":  [r for r in results if r.get("score", 0) <  60]}
    out = {}
    for name, rs in buckets.items():
        per_horizon = {}
        for label, _ in HORIZONS:
            valid = [r for r in rs if r["horizons"].get(label)]
            if not valid:
                per_horizon[label] = None; continue
            wins = sum(1 for r in valid if r["horizons"][label]["win"])
            avg  = sum(r["horizons"][label]["signed_pct"] for r in valid) / len(valid)
            per_horizon[label] = {"n": len(valid),
                                  "win_rate": wins / len(valid) * 100.0,
                                  "avg_signed_pct": avg}
        out[name] = {"total": len(rs), "horizons": per_horizon}
    return out


def print_table(summary):
    print()
    print("=" * 70)
    print(f"  Backtest: win = signed move ≥ {WIN_PCT:.1f}% within horizon")
    print("=" * 70)
    if not summary:
        print("  No signals fired in the backtest window.")
        return
    for kind, data in summary.items():
        print(f"\n  {kind}: {data['total_signals']} signals")
        print(f"  {'horizon':<8} {'n':>5} {'win_rate':>10} {'95% CI':>15} {'avg_pnl':>10}")
        for label, h in data["horizons"].items():
            if h is None:
                print(f"  {label:<8} {'—':>5} {'—':>10} {'—':>15} {'—':>10}")
            else:
                ci = f"[{h['win_rate_ci95'][0]:.1f},{h['win_rate_ci95'][1]:.1f}]"
                print(f"  {label:<8} {h['n']:>5} "
                      f"{h['win_rate']:>9.1f}% {ci:>15} "
                      f"{h['avg_signed_pct']:>9.2f}%")
    print()


def resolve_symbols(args):
    if args.symbols:
        return [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    print(f"Fetching top {args.top} USDT pairs by 24h volume…", file=sys.stderr)
    resp = app._binance_get("/api/v3/ticker/24hr", timeout=20)
    tickers = resp.json()
    # Drop leveraged/inverse tokens and stablecoin pairs.
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
    parser.add_argument("--days",    type=int, default=90,  help="Backtest window in days")
    parser.add_argument("--top",     type=int, default=50,  help="Top N USDT pairs by 24h vol")
    parser.add_argument("--symbols", default="",            help="Comma-separated symbols (overrides --top)")
    parser.add_argument("--out",     default="backtest_results.json")
    args = parser.parse_args()

    symbols = resolve_symbols(args)
    print(f"Backtesting {len(symbols)} symbols over ~{args.days} days "
          f"(thresholds: vol_pct≥{app.VOLUME_SURGE_PCT}%, "
          f"crsi≥{app.CRSI_OVERBOUGHT} or ≤{app.CRSI_OVERSOLD})", file=sys.stderr)

    # BTC 24h-pct lookup per day — fed into the live scoring function.
    print("Fetching BTC history for score context…", file=sys.stderr)
    btc_daily = fetch_daily_klines("BTCUSDT", limit=args.days + CRSI_WARMUP_DAYS)
    btc_pct24_by_day = {}
    for i in range(1, len(btc_daily)):
        prev_c = btc_daily[i - 1]["close"]
        if prev_c > 0:
            btc_pct24_by_day[btc_daily[i]["open_time"]] = (
                (btc_daily[i]["close"] - prev_c) / prev_c * 100.0
            )

    all_signals = []
    for i, sym in enumerate(symbols, 1):
        try:
            sigs = simulate_volume_surge_for_symbol(sym, args.days, btc_pct24_by_day)
        except Exception as e:
            print(f"  [{i}/{len(symbols)}] {sym}: skip ({e})", file=sys.stderr)
            continue
        if sigs:
            print(f"  [{i}/{len(symbols)}] {sym}: {len(sigs)} candidate alerts",
                  file=sys.stderr)
        all_signals.extend(sigs)
        time.sleep(0.03)

    print(f"\n→ Found {len(all_signals)} candidate alerts. Evaluating outcomes…",
          file=sys.stderr)
    results = evaluate_signals(all_signals)

    summary = summarize(results)
    print_table(summary)
    score_summary = summarize_by_score_bucket(results)
    print("By score bucket (filter is MIN_ALERT_SCORE=60 in production):")
    for name, data in score_summary.items():
        print(f"\n  {name}: {data['total']} signals")
        print(f"  {'horizon':<8} {'n':>5} {'win_rate':>10} {'avg_pnl':>10}")
        for label, h in data["horizons"].items():
            if h is None:
                print(f"  {label:<8} {'-':>5} {'-':>10} {'-':>10}")
            else:
                print(f"  {label:<8} {h['n']:>5} {h['win_rate']:>9.1f}% "
                      f"{h['avg_signed_pct']:>9.2f}%")
    print()

    out_path = os.path.join(os.path.dirname(__file__), args.out)
    with open(out_path, "w") as f:
        json.dump({"summary": summary,
                   "score_summary": score_summary,
                   "details": results}, f, indent=2)
    print(f"Detailed results written to {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
