"""
Backtest frequency analysis for support_reclaim_long strategy.

Conditions checked (as specified):
  1. zone_low = min of lows over last 10 days (60 × 4h candles)
  2. Zone band: [zone_low, zone_low × 1.03]
  3. ≥2 distinct touches (bar.low in zone band) separated by ≥24h (≥6 bars apart)
  4. No new low below zone_low in last 24h (last 6 completed 4h bars)
  5. Current price 8-20% above zone_low
  6. Current price ≥15% below the 10-day high

Signal event: condition flips False→True, OR last signal on the same symbol was >48h ago
(simulating a per-symbol cooldown so we count realistic daily fire-rate, not bar streaks).

Run: python backtest_support_reclaim.py
"""

import time
import sys
import json
import urllib.request
import concurrent.futures
from collections import defaultdict

# ── Gate.io futures klines ────────────────────────────────────────────────────
GATE_BASE = "https://api.gateio.ws/api/v4/futures/usdt"

def gateio_get(path: str, params: dict | None = None, timeout: int = 10):
    import urllib.parse
    url = f"{GATE_BASE}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

def gateio_klines(symbol: str, interval: str, limit: int) -> list:
    """Return list of [ts, open, high, low, close, volume] OHLCV (Binance-compat)."""
    contract = symbol.replace("USDT", "_USDT")
    raw = gateio_get("/candlesticks",
                     {"contract": contract, "interval": interval, "limit": limit})
    return sorted([
        [int(c["t"]), float(c["o"]), float(c["h"]), float(c["l"]),
         float(c["c"]), float(c.get("v", 0))]
        for c in raw
    ], key=lambda x: x[0])

def gateio_tickers() -> list[dict]:
    return gateio_get("/tickers", timeout=15)


# ── Strategy parameters (from spec, do not change) ───────────────────────────
WINDOW_BARS   = 60        # 10 days × 6 bars/day (4h candles)
ZONE_WIDTH    = 1.03      # zone = [zone_low, zone_low × 1.03]
MIN_TOUCHES   = 2         # need ≥ this many distinct touches
MIN_GAP_BARS  = 6         # ≥24h between touches (6 × 4h = 24h)
NO_BREAK_BARS = 6         # "no new low in last 24h" = last 6 bars
BOUNCE_MIN    = 1.08      # price ≥ zone_low × 1.08  (+8%)
BOUNCE_MAX    = 1.20      # price ≤ zone_low × 1.20  (+20%)
BELOW_HIGH    = 0.85      # price ≤ 10d high × 0.85  (≥15% below high)
COOLDOWN_BARS = 12        # 48h cooldown between signals per symbol (for rate calc)

MIN_VOL_USDT  = 50_000    # 24h volume filter (same as bot)
FETCH_LIMIT   = 360 + 10  # ~60 days of 4h data


# ── Core checker ─────────────────────────────────────────────────────────────
def count_signals(candles: list) -> dict:
    """
    Slide through bars starting at bar WINDOW_BARS.
    Returns stats dict.
    """
    bars = candles
    n = len(bars)
    if n < WINDOW_BARS + 2:
        return {"skip": True}

    signal_bars = []          # bar indices where all conditions fire
    last_signal_bar = -999    # cooldown tracking

    for i in range(WINDOW_BARS, n - 1):
        # -1 to not use the currently-forming bar (same logic as live bot)
        window = bars[i - WINDOW_BARS : i]   # 60 completed bars

        # ── Condition 1: zone_low ────────────────────────────────────────────
        zone_low  = min(b[3] for b in window)   # min LOW in window
        zone_high = zone_low * ZONE_WIDTH        # +3% band top
        win_high  = max(b[2] for b in window)   # 10-day high (max HIGH)

        # ── Condition 3: count qualifying touches ────────────────────────────
        touch_bars = [
            j for j, b in enumerate(window)
            if zone_low <= b[3] <= zone_high    # low is IN the zone (not broken below)
        ]
        # Count touches that are ≥MIN_GAP_BARS apart
        valid_touches = []
        for tb in touch_bars:
            if not valid_touches or (tb - valid_touches[-1]) >= MIN_GAP_BARS:
                valid_touches.append(tb)
        if len(valid_touches) < MIN_TOUCHES:
            continue

        # ── Condition 4: no new low below zone_low in last 24h ──────────────
        recent = window[-NO_BREAK_BARS:]
        if any(b[3] < zone_low for b in recent):
            continue   # zone broken recently

        # ── Use close of bar i as "current price" ───────────────────────────
        price = bars[i][4]   # close price at bar i (simulates live ticker)

        # ── Condition 5: price 8-20% above zone_low ─────────────────────────
        if not (zone_low * BOUNCE_MIN <= price <= zone_low * BOUNCE_MAX):
            continue

        # ── Condition 6: price ≥15% below 10-day high ───────────────────────
        if price > win_high * BELOW_HIGH:
            continue

        # ── Cooldown: simulate ~48h gap between signals ──────────────────────
        if i - last_signal_bar < COOLDOWN_BARS:
            continue   # within cooldown window — would not fire in production

        # ── All conditions met ───────────────────────────────────────────────
        signal_bars.append(i)
        last_signal_bar = i

    return {
        "skip": False,
        "total_bars":   n,
        "signal_count": len(signal_bars),
        "signal_bars":  signal_bars,
    }


# ── Fetch + analyse one symbol ─────────────────────────────────────────────
def analyse_symbol(symbol: str) -> dict | None:
    try:
        candles = gateio_klines(symbol, "4h", FETCH_LIMIT)
    except Exception as exc:
        return {"symbol": symbol, "error": str(exc)}
    if len(candles) < WINDOW_BARS + 10:
        return None
    result = count_signals(candles)
    if result.get("skip"):
        return None
    return {"symbol": symbol, **result}


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("Fetching Gate.io tickers to build liquid pairs list...")
    try:
        tickers = gateio_tickers()
    except Exception as exc:
        print(f"ERROR fetching tickers: {exc}")
        sys.exit(1)

    liquid = [
        t["contract"].replace("_USDT", "USDT")
        for t in tickers
        if "_USDT" in t.get("contract", "")
        and float(t.get("volume_24h_quote", 0) or 0) >= MIN_VOL_USDT
        and not any(x in t["contract"] for x in ["BTC_", "ETH_", "USDC_", "DAI_", "TUSD_"])
        # keep only altcoin/USDT pairs, strip BTC/ETH pairs
    ]
    # Also include BTC and ETH themselves for calibration
    for base in ["BTCUSDT", "ETHUSDT"]:
        if base not in liquid:
            liquid.append(base)

    print(f"Liquid pairs to analyse: {len(liquid)}")
    print(f"Parameters: window={WINDOW_BARS} bars (10d), zone=+{(ZONE_WIDTH-1)*100:.0f}%, "
          f"touches≥{MIN_TOUCHES} (gap≥{MIN_GAP_BARS}bars), "
          f"bounce {int((BOUNCE_MIN-1)*100)}-{int((BOUNCE_MAX-1)*100)}%, "
          f"≥{int((1-BELOW_HIGH)*100)}% below high, cooldown={COOLDOWN_BARS} bars")
    print()

    results = []
    errors = 0
    t0 = time.time()

    with concurrent.futures.ThreadPoolExecutor(max_workers=15) as pool:
        futures = {pool.submit(analyse_symbol, sym): sym for sym in liquid}
        done = 0
        for fut in concurrent.futures.as_completed(futures):
            done += 1
            sym = futures[fut]
            r = fut.result()
            if r is None:
                continue
            if "error" in r:
                errors += 1
                continue
            results.append(r)
            if done % 50 == 0:
                print(f"  Progress: {done}/{len(liquid)} pairs done, "
                      f"signals so far: {sum(x['signal_count'] for x in results)}")

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.1f}s. Errors: {errors}")

    # ── Aggregate ────────────────────────────────────────────────────────────
    total_signals = sum(r["signal_count"] for r in results)
    triggered     = [r for r in results if r["signal_count"] > 0]

    # Backtest period: use median total_bars across results
    if results:
        avg_bars    = sum(r["total_bars"] for r in results) / len(results)
        days_approx = (avg_bars - WINDOW_BARS) / 6   # 6 bars/day on 4h
    else:
        days_approx = 0

    rate_per_day = total_signals / days_approx if days_approx > 0 else 0

    print(f"\n{'='*60}")
    print(f"RESULTS — support_reclaim_long frequency backtest")
    print(f"{'='*60}")
    print(f"Pairs analysed:           {len(results)}")
    print(f"Backtest period:          ~{days_approx:.0f} days (4h candle history)")
    print(f"Total qualifying signals: {total_signals}")
    print(f"Unique coins triggered:   {len(triggered)}")
    print(f"Estimated rate:           {rate_per_day:.1f} signals/day  "
          f"({total_signals / max(days_approx/7, 1):.1f}/week)")
    print()

    # Top coins by signal count
    triggered_sorted = sorted(triggered, key=lambda x: -x["signal_count"])
    print(f"Top coins by signal frequency (cooldown={COOLDOWN_BARS} bars = 48h):")
    for r in triggered_sorted[:25]:
        bars_approx = r["total_bars"] / 6   # days
        rate = r["signal_count"] / (bars_approx - 10) * 7  # per week
        print(f"  {r['symbol']:<18}  {r['signal_count']:>3} signals  "
              f"({rate:.1f}/week, {bars_approx:.0f}d history)")

    print()
    print("Distribution by signal count per coin:")
    dist: dict[int, int] = defaultdict(int)
    for r in results:
        dist[r["signal_count"]] += 1
    for k in sorted(dist):
        bar = "█" * min(dist[k], 50)
        print(f"  {k:>3} signals: {dist[k]:>4} coins  {bar}")

    # ── Condition breakdown: how many coins pass each filter ─────────────────
    print()
    print("Condition sensitivity — checking current snapshot (last bar only):")
    cond_counts = {
        "zone_exists": 0,
        "zone+2touches": 0,
        "zone+touches+nobreak": 0,
        "zone+touches+nobreak+bounce": 0,
        "all_5": 0,
    }
    for r in results:
        candles = None
        try:
            candles = gateio_klines(r["symbol"], "4h", WINDOW_BARS + 5)
        except Exception:
            continue
        if not candles or len(candles) < WINDOW_BARS + 1:
            continue
        window = candles[-WINDOW_BARS - 1 : -1]
        price = candles[-1][4]
        zone_low  = min(b[3] for b in window)
        zone_high = zone_low * ZONE_WIDTH
        win_high  = max(b[2] for b in window)

        cond_counts["zone_exists"] += 1

        touch_bars = [j for j, b in enumerate(window) if zone_low <= b[3] <= zone_high]
        valid_touches = []
        for tb in touch_bars:
            if not valid_touches or (tb - valid_touches[-1]) >= MIN_GAP_BARS:
                valid_touches.append(tb)
        if len(valid_touches) < MIN_TOUCHES:
            continue
        cond_counts["zone+2touches"] += 1

        recent = window[-NO_BREAK_BARS:]
        if any(b[3] < zone_low for b in recent):
            continue
        cond_counts["zone+touches+nobreak"] += 1

        if not (zone_low * BOUNCE_MIN <= price <= zone_low * BOUNCE_MAX):
            continue
        cond_counts["zone+touches+nobreak+bounce"] += 1

        if price > win_high * BELOW_HIGH:
            continue
        cond_counts["all_5"] += 1

    print(f"  Zone exists (always true):                 {cond_counts['zone_exists']:>4}")
    print(f"  + ≥2 touches with 24h gap:                 {cond_counts['zone+2touches']:>4}")
    print(f"  + no break in last 24h:                    {cond_counts['zone+touches+nobreak']:>4}")
    print(f"  + price 8-20% above zone_low:              {cond_counts['zone+touches+nobreak+bounce']:>4}")
    print(f"  + price ≥15% below 10d high (all 5 met):  {cond_counts['all_5']:>4}")

    print()
    print("Done.")


if __name__ == "__main__":
    main()
