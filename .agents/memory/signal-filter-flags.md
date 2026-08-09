---
name: Signal filter feature flags
description: Filter flags for confluence SHORT/LONG and bear_downtrend in app.py — tuned based on demo win-rate analysis.
---

# Signal filter feature flags

## bear_downtrend filter (BEAR_DOWNTREND_FILTER_ENABLED)

**Current state:** `True` (re-enabled with nuanced logic)

**Logic in `_bear_downtrend_blocks_long()`:**
- 3–4 consecutive down days → block LONG (18% WR in shadows = correctly blocking)
- 5+ consecutive down days → ALLOW LONG (90.9% WR in shadows = extreme oversold bounce)

**Why:** Demo analysis showed that after exactly 3-4 down days the coin continues falling (18% WR),
but after 5+ days of consecutive drops the bounce probability jumps to 90.9%. The old `days >= 3`
blanket block was killing the most profitable signals.

## pump_filter (PUMP_FILTER_SHORT_ENABLED / PUMP_FILTER_LONG_ENABLED)

**Current state:** Both `False`

**Why:** Demo analysis showed pump_filter was blocking SHORT signals with 52% WR and LONG signals
with 57% WR — in both cases the filter was removing profitable entries, not protecting from losses.

## AI veto (confluence block)

**Current state:** Applied to LONG only; SHORT direction skipped

**Why:** Demo data showed `confluence/SHORT` signals that passed AI veto had 5.9% WR (−$73),
while signals blocked by AI veto had 52.4% WR (+$108). The AI veto was inverting SHORT quality —
blocking good SHORTs and letting through bad ones. Skipping veto for SHORT was the correct fix.

## oversold_24h direction

**Current state:** LONG (bounce signal)
- RSI ≤ 30 + pct24 ≤ threshold → enter LONG (expect bounce)
- Message: "🟢 ПЕРЕПРОДАННОСТЬ — разворот вверх"
- Co-movers: "Также перепроданы"

**History:** Was LONG originally, flipped to SHORT in one session (user thought it should be
continuation-of-fall), then flipped back to LONG based on demo data (shadow LONG: 48.9% WR,
R:R 2.23; vs shadow SHORT: not enough data to confirm).

## What DOES work (leave alone)
- **Liq veto**: correctly blocks overleveraged scenarios for both LONG and SHORT
- **BTC direction filter**: correctly gates alts based on BTC RSI/pct24
- **bear_downtrend_3d**: 18% WR = correctly blocks LONGs after 3-4 day drops
