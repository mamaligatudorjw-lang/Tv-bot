---
name: Signal filter feature flags
description: Two confluence filters disabled via BEAR_DOWNTREND_FILTER_ENABLED / PUMP_FILTER_ENABLED after demo data showed they blocked profitable signals.
---

# Signal filter feature flags

## The rule
`BEAR_DOWNTREND_FILTER_ENABLED = False` and `PUMP_FILTER_ENABLED = False` at the top of `app.py` (near line 320 after the display-leverage constant).

**Why:** Demo analysis on 2026-08-08 showed:
- `bear_downtrend_5d` shadow win rate = **90%** → it was blocking longs at dip bottoms that recovered
- `pump_filter` shadow win rate = **57%** → it was blocking trend-continuation signals

**How to apply:** To re-enable either filter, set the flag to `True` in `app.py`. Code is fully preserved.

## What DOES work (leave alone)
- **AI veto**: shadow win rate = 14.3% → correctly blocks bad signals
- **bear_downtrend_3d**: shadow win rate = 25% → borderline, keep watching
