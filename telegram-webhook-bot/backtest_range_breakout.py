"""
Backtest frequency analysis for range_breakout_long strategy.

Strategy logic:
  1. Look at last 8-48h of 1h candles before the current bar.
  2. Find the LONGEST contiguous segment ending at bar i-1 where
     (max_high - min_low) / min_low <= 12% (tight consolidation).
  3. Segment must be >= 8 bars (hours) long.
  4. In the 48h BEFORE the segment start, there must be a drop of >= 15%
     from the local max to the segment's lower boundary
     (ensures base follows a real correction, not random sideways).
  5. Breakout bar (bar i):
     - close > range upper boundary (seg_high)
     - volume >= 1.5x avg volume during the range
  6. 8-hour cooldown per symbol.

Run: python backtest_range_breakout.py
"""

import json, urllib.request, time, concurrent.futures
from collections import defaultdict

GATE_BASE = "https://api.gateio.ws/api/v4/futures/usdt"

def get_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

def klines_1h(symbol, limit=1000):
    """Fetch 1h candles from Gate.io futures."""
    contract = symbol.replace("USDT", "_USDT")
    url = f"{GATE_BASE}/candlesticks?contract={contract}&interval=1h&limit={limit}"
    raw = get_json(url)
    return sorted([
        [int(c["t"]), float(c["o"]), float(c["h"]), float(c["l"]),
         float(c["c"]), float(c.get("v", 0))]
        for c in raw
    ], key=lambda x: x[0])

def gateio_tickers():
    return get_json(f"{GATE_BASE}/tickers", timeout=15)


# ── Strategy parameters ───────────────────────────────────────────────────────
MAX_LOOKBACK  = 48    # max range duration to search backwards (bars = hours)
MIN_SEG_LEN   = 8     # min range length (hours)
MAX_WIDTH     = 0.12  # max range width: (high-low)/low <= 12%
MIN_DROP      = 0.15  # pre-range drop: (local_max - range_low)/local_max >= 15%
PRE_DROP_WIN  = 48    # window (bars) before range start to find the prior high
VOL_FACTOR    = 1.5   # breakout bar volume >= 1.5x avg range volume
COOLDOWN      = 8     # hours between signals on same symbol

MIN_HISTORY   = MAX_LOOKBACK + PRE_DROP_WIN  # 96 bars needed before first check
FETCH_LIMIT   = 1000  # bars to fetch (~41.7 days of 1h data)
MIN_VOL_USDT  = 50_000


# ── Core signal checker ───────────────────────────────────────────────────────
def count_signals(candles: list) -> dict:
    """
    Slide through bars starting at MIN_HISTORY.
    Returns dict with signal_count and signal_bars.
    """
    bars = candles
    n = len(bars)
    if n < MIN_HISTORY + 2:
        return {"skip": True}

    signal_bars = []
    last_sig    = -999

    for i in range(MIN_HISTORY, n):
        e = i - 1  # last bar of the range (just before potential breakout)

        # ── Extend range backwards from e ────────────────────────────────────
        seg_high  = bars[e][2]
        seg_low   = bars[e][3]
        seg_start = e

        for k in range(1, MAX_LOOKBACK):
            s = e - k
            if s < 0:
                break
            new_high = max(seg_high, bars[s][2])
            new_low  = min(seg_low,  bars[s][3])
            if new_low <= 0:
                break
            if (new_high - new_low) / new_low > MAX_WIDTH:
                break  # adding this bar blows up the range
            seg_high  = new_high
            seg_low   = new_low
            seg_start = s

        seg_len = e - seg_start + 1
        if seg_len < MIN_SEG_LEN:
            continue  # too short

        # ── Breakout: close strictly above range high ─────────────────────────
        if bars[i][4] <= seg_high:
            continue

        # ── Volume filter ─────────────────────────────────────────────────────
        seg_vols = [bars[j][5] for j in range(seg_start, e + 1)]
        avg_vol  = sum(seg_vols) / len(seg_vols) if seg_vols else 0
        if avg_vol > 0 and bars[i][5] < VOL_FACTOR * avg_vol:
            continue

        # ── Pre-range drop check ─────────────────────────────────────────────
        pre_start = max(0, seg_start - PRE_DROP_WIN)
        if pre_start >= seg_start:
            continue
        pre_high = max(bars[j][2] for j in range(pre_start, seg_start))
        if pre_high <= 0:
            continue
        drop = (pre_high - seg_low) / pre_high
        if drop < MIN_DROP:
            continue

        # ── Cooldown ─────────────────────────────────────────────────────────
        if i - last_sig < COOLDOWN:
            continue

        signal_bars.append(i)
        last_sig = i

    return {
        "skip": False,
        "total_bars":   n,
        "signal_count": len(signal_bars),
        "signal_bars":  signal_bars,
    }


# ── Per-symbol fetch + analyse ────────────────────────────────────────────────
def analyse_symbol(symbol: str) -> dict | None:
    try:
        candles = klines_1h(symbol, FETCH_LIMIT)
    except Exception as exc:
        return {"symbol": symbol, "error": str(exc)}
    if len(candles) < MIN_HISTORY + 10:
        return None
    result = count_signals(candles)
    if result.get("skip"):
        return None
    return {"symbol": symbol, **result}


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    print("Fetching Gate.io tickers...")
    try:
        tickers = gateio_tickers()
    except Exception as exc:
        print(f"ERROR: {exc}")
        return

    liquid = [
        t["contract"].replace("_USDT", "USDT")
        for t in tickers
        if "_USDT" in t.get("contract", "")
        and float(t.get("volume_24h_quote", 0) or 0) >= MIN_VOL_USDT
        and not any(x in t["contract"] for x in ["BTC_", "ETH_", "USDC_", "DAI_", "TUSD_"])
    ]
    for base in ["BTCUSDT", "ETHUSDT"]:
        if base not in liquid:
            liquid.append(base)

    print(f"Pairs to analyse: {len(liquid)}")
    print(f"Parameters:")
    print(f"  Range:     {MIN_SEG_LEN}–{MAX_LOOKBACK}h, width ≤{MAX_WIDTH*100:.0f}%")
    print(f"  Pre-drop:  ≥{MIN_DROP*100:.0f}% in {PRE_DROP_WIN}h window before range")
    print(f"  Breakout:  close above range_high + vol ≥{VOL_FACTOR}× avg range vol")
    print(f"  Cooldown:  {COOLDOWN}h per symbol")
    print()

    results = []
    errors  = 0
    t0      = time.time()

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        futures = {pool.submit(analyse_symbol, sym): sym for sym in liquid}
        done    = 0
        for fut in concurrent.futures.as_completed(futures):
            done += 1
            r = fut.result()
            if r is None:
                continue
            if "error" in r:
                errors += 1
                continue
            results.append(r)
            if done % 100 == 0:
                sigs = sum(x["signal_count"] for x in results)
                print(f"  Progress: {done}/{len(liquid)} done, signals so far: {sigs}")

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.1f}s  (errors: {errors})")

    # ── Aggregate ─────────────────────────────────────────────────────────────
    total_signals = sum(r["signal_count"] for r in results)
    triggered     = [r for r in results if r["signal_count"] > 0]

    if results:
        avg_bars  = sum(r["total_bars"] for r in results) / len(results)
        days_approx = (avg_bars - MIN_HISTORY) / 24
    else:
        days_approx = 1

    rate_per_day = total_signals / days_approx if days_approx > 0 else 0

    print(f"\n{'='*60}")
    print(f"RESULTS — range_breakout_long frequency backtest")
    print(f"{'='*60}")
    print(f"Pairs analysed:           {len(results)}")
    print(f"Backtest period:          ~{days_approx:.0f} days (1h candle history)")
    print(f"Total qualifying signals: {total_signals}")
    print(f"Coins triggered:          {len(triggered)} / {len(liquid)}  "
          f"({len(triggered)/max(len(liquid),1)*100:.0f}%)")
    print(f"Estimated rate:           {rate_per_day:.1f} signals/day  "
          f"({total_signals/max(days_approx/7,1):.1f}/week)")
    print()

    triggered_sorted = sorted(triggered, key=lambda x: -x["signal_count"])
    print("Top coins by signal frequency:")
    for r in triggered_sorted[:25]:
        days_r = (r["total_bars"] - MIN_HISTORY) / 24
        rate_w = r["signal_count"] / max(days_r / 7, 1)
        print(f"  {r['symbol']:<18}  {r['signal_count']:>3} signals  "
              f"({rate_w:.1f}/week, {days_r:.0f}d history)")

    print()
    dist: dict[int, int] = defaultdict(int)
    for r in results:
        dist[r["signal_count"]] += 1
    dist[0] += len(liquid) - len(results)  # pairs with 0 signals

    print("Distribution by signals per coin:")
    for k in sorted(dist)[:20]:
        bar_str = "█" * min(dist[k], 50)
        print(f"  {k:>3} signals: {dist[k]:>4} coins  {bar_str}")

    # ── Sensitivity hint ──────────────────────────────────────────────────────
    print()
    if rate_per_day > 10:
        print(f"⚠  Rate {rate_per_day:.1f}/day is HIGH (like v1 support_reclaim ~18/day).")
        print("   Suggest tightening: raise MIN_DROP to 0.20, raise MIN_SEG_LEN to 12,")
        print("   or lower MAX_WIDTH to 0.08 — then re-run for second-pass calibration.")
    elif rate_per_day > 5:
        print(f"⚡  Rate {rate_per_day:.1f}/day is MODERATE — consider one tightening pass.")
    else:
        print(f"✓  Rate {rate_per_day:.1f}/day is in calibrated range (target ~2-4/day).")

    print("\nDone.")


if __name__ == "__main__":
    main()
