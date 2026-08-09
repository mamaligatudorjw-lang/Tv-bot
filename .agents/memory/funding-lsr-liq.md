---
name: Funding / LSR / Liquidation veto factors
description: Three new signal-scoring factors added on 2026-08-08; all tunable via constants; results tracked in alerts table.
---

# Funding / LSR / Liquidation veto

## Constants (top of app.py, ~line 320)
- `FUNDING_SHORT_THR_1/2`, `FUNDING_SHORT_PTS_1/2` — SHORT bonus for positive funding
- `FUNDING_LONG_THR_NEG`, `FUNDING_LONG_PTS_BON` — LONG bonus for negative funding
- `FUNDING_LONG_THR_BAD`, `FUNDING_LONG_PTS_PEN` — LONG penalty for crowded longs
- `LSR_SHORT_THR`, `LSR_LONG_THR`, `LSR_BONUS_PTS` — crowd-positioning bonus
- `LIQ_VETO_WINDOW`, `LIQ_VETO_VOL_FRAC` — liquidation veto threshold (relative to 24h vol)

## Key helpers
- `_get_contract_stats_cached(symbol)` → (lsr, funding); cached STATS_CACHE_TTL=60s
- `_get_gate_liq_data(symbol)` → (liq_short_usd, liq_long_usd); cached LIQ_CACHE_TTL=300s
- `_check_liq_veto(symbol, side, vol_24h)` → (bool, reason); suppressed signals open shadow demo position
- `_funding_lsr_score_and_text(symbol, side)` → (delta, text, funding_pts, lsr_pts)

## DB
`alerts` table has `factor_funding_pts INTEGER` and `factor_lsr_pts INTEGER` columns (idempotent migration).
`/demo` command shows "Факторы за 7 дней" breakdown.

**Why:** After one week, check `/demo` to see if signals with funding/LSR bonus have higher winrate. Task #25 tracks this.
