---
name: Frozen regime snapshot joins
description: Methodological rule for historical cohorts that reuse a previously generated BTC regime snapshot.
---

When an analysis is defined against an existing lookahead-safe regime snapshot, join it by signal ID and preserve missing IDs as `unknown` with explicit coverage. Do not refresh candles just to eliminate snapshot missingness.

**Why:** Refreshing the market data changes the analysis contract and can make a frozen cohort appear fully covered even though the saved provenance does not cover every signal.

**How to apply:** Load the snapshot as an ID-keyed map, reject duplicate or invalid regime rows, and emit `snapshot_missing`/`unknown` audit rows rather than silently dropping or reclassifying them.