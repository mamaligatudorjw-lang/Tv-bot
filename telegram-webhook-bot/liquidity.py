"""
Модуль умного входа по ликвидности (liquidity_entry).

Работает ТОЛЬКО в демо-режиме как параллельная стратегия.
Находит зоны ликвидности по 15м свечам и определяет оптимальный вход:
  - лимитный ордер у зоны (зона < 1.5% от цены)
  - рыночный вход с умным SL   (зона 1.5–3% от цены)
  - пропуск сделки              (зона > 3% от цены)
"""

import math


# ---------------------------------------------------------------------------
# Candle helpers  (Gate.io format: [ts, o, h, l, c, v, ts, sum])
# ---------------------------------------------------------------------------

def _lows(candles):  return [float(c[3]) for c in candles]
def _highs(candles): return [float(c[2]) for c in candles]
def _closes(candles):return [float(c[4]) for c in candles]


# ---------------------------------------------------------------------------
# Zone detection
# ---------------------------------------------------------------------------

def find_liquidity_zones(candles: list) -> list[tuple[float, int]]:
    """
    Find liquidity zones from the last 100 candles (15m preferred).

    Returns a list of (price, weight) sorted ascending by price.
    Zones within 0.2% of each other are merged (weights summed).

    Zone types:
      - Equal lows / equal highs  (≥2 extremes within 0.15%) : weight 3
      - Swing low / swing high    (3 candles each side)       : weight 2
      - Round levels (ends 0 or 5 in significant digit)       : weight 1
    """
    if len(candles) < 7:
        return []

    lows   = _lows(candles)
    highs  = _highs(candles)
    closes = _closes(candles)
    n = len(candles)

    raw: list[tuple[float, int]] = []

    # 1. Equal lows
    visited_lo: set[int] = set()
    for i in range(n):
        if i in visited_lo:
            continue
        cluster = [i]
        for j in range(i + 1, n):
            if j not in visited_lo and abs(lows[j] - lows[i]) / max(lows[i], 1e-12) <= 0.0015:
                cluster.append(j)
        if len(cluster) >= 2:
            avg = sum(lows[k] for k in cluster) / len(cluster)
            raw.append((avg, 3))
            visited_lo.update(cluster)

    # 2. Equal highs
    visited_hi: set[int] = set()
    for i in range(n):
        if i in visited_hi:
            continue
        cluster = [i]
        for j in range(i + 1, n):
            if j not in visited_hi and abs(highs[j] - highs[i]) / max(highs[i], 1e-12) <= 0.0015:
                cluster.append(j)
        if len(cluster) >= 2:
            avg = sum(highs[k] for k in cluster) / len(cluster)
            raw.append((avg, 3))
            visited_hi.update(cluster)

    # 3. Swing lows (3 candles each side strictly higher)
    for i in range(3, n - 3):
        if all(lows[i] < lows[i - k] for k in range(1, 4)) and \
           all(lows[i] < lows[i + k] for k in range(1, 4)):
            raw.append((lows[i], 2))

    # 4. Swing highs
    for i in range(3, n - 3):
        if all(highs[i] > highs[i - k] for k in range(1, 4)) and \
           all(highs[i] > highs[i + k] for k in range(1, 4)):
            raw.append((highs[i], 2))

    # 5. Round levels within ±5% of last close
    if closes:
        ref = closes[-1]
        try:
            mag   = 10 ** math.floor(math.log10(ref))
            step  = mag * 0.1      # one decimal below magnitude
            lo    = ref * 0.95
            hi    = ref * 1.05
            level = math.floor(lo / step) * step
            while level <= hi * 1.001:
                digit = round(level / step) % 10
                if digit in (0, 5):
                    raw.append((level, 1))
                level += step
        except (ValueError, ZeroDivisionError):
            pass

    if not raw:
        return []

    # Merge zones within 0.2% of each other
    raw.sort(key=lambda z: z[0])
    merged: list[tuple[float, int]] = []
    i = 0
    while i < len(raw):
        p0, w0 = raw[i]
        group_prices = [p0]
        group_weight = w0
        j = i + 1
        while j < len(raw) and abs(raw[j][0] - p0) / max(p0, 1e-12) <= 0.002:
            group_prices.append(raw[j][0])
            group_weight += raw[j][1]
            j += 1
        merged.append((sum(group_prices) / len(group_prices), group_weight))
        i = j

    return merged


# ---------------------------------------------------------------------------
# Entry decision
# ---------------------------------------------------------------------------

def liquidity_entry_decision(
    direction: str,
    price: float,
    zones: list[tuple[float, int]],
) -> dict | None:
    """
    Decide how to enter based on liquidity zones.

    LONG: nearest zone with weight>=3 BELOW price.
    SHORT: nearest zone with weight>=3 ABOVE price.

    Returns dict with keys:
      entry_type : "limit" | "market" | "skip"
      + type-specific fields (limit_price / entry_price, sl_price, tp_price,
                               zone_price, zone_weight)
    Returns None if no qualifying zone found.
    """
    if not zones or not price:
        return None

    if direction == "LONG":
        candidates = [(p, w) for p, w in zones if p < price and w >= 3]
        if not candidates:
            return None
        zone_price, zone_weight = max(candidates, key=lambda z: z[0])  # nearest = highest below
        dist_pct = (price - zone_price) / price * 100.0

        if dist_pct <= 1.5:
            # Limit order slightly above zone so it fills on the bounce
            limit_price = zone_price * 1.001
            sl_price    = zone_price * 0.995       # 0.5% below zone
            sl_dist     = limit_price - sl_price
            tp_price    = limit_price + 2.0 * sl_dist  # R:R >= 2
            return dict(entry_type="limit", limit_price=limit_price,
                        sl_price=sl_price, tp_price=tp_price,
                        zone_price=zone_price, zone_weight=zone_weight)
        else:
            # Market entry with smart SL
            sl_price = zone_price * 0.995
            sl_pct   = (price - sl_price) / price * 100.0
            if sl_pct > 3.0:
                return dict(entry_type="skip", reason="skipped_far_sl")
            tp_price = price + 2.0 * (price - sl_price)
            return dict(entry_type="market", entry_price=price,
                        sl_price=sl_price, tp_price=tp_price,
                        zone_price=zone_price, zone_weight=zone_weight)

    else:  # SHORT
        candidates = [(p, w) for p, w in zones if p > price and w >= 3]
        if not candidates:
            return None
        zone_price, zone_weight = min(candidates, key=lambda z: z[0])  # nearest = lowest above
        dist_pct = (zone_price - price) / price * 100.0

        if dist_pct <= 1.5:
            limit_price = zone_price * 0.999
            sl_price    = zone_price * 1.005
            sl_dist     = sl_price - limit_price
            tp_price    = limit_price - 2.0 * sl_dist
            return dict(entry_type="limit", limit_price=limit_price,
                        sl_price=sl_price, tp_price=tp_price,
                        zone_price=zone_price, zone_weight=zone_weight)
        else:
            sl_price = zone_price * 1.005
            sl_pct   = (sl_price - price) / price * 100.0
            if sl_pct > 3.0:
                return dict(entry_type="skip", reason="skipped_far_sl")
            tp_price = price - 2.0 * (sl_price - price)
            return dict(entry_type="market", entry_price=price,
                        sl_price=sl_price, tp_price=tp_price,
                        zone_price=zone_price, zone_weight=zone_weight)
