---
name: BTC regime outcome analysis
description: Methodology for describing signal outcomes by the BTC 4h EMA50 regime.
---

Historical regime comparisons use the last completed BTC Futures 4h candle before each signal. `bull` means close above EMA50, `bear` means below, and equality or insufficient history remains `unknown`; empty bull/bear cohorts are shown explicitly.

**Why:** A forming candle or a missing cohort can create lookahead bias or make a strategy comparison look complete when one side was never observed.

**How to apply:** Keep regime analysis read-only and separate from production. Use resolved recorded entry/SL/exit values for WR and avg R, mark groups below 20 resolved rows as insufficient, and do not turn descriptive differences into filters without a separate decision.