---
name: Overheated early bootstrap provenance
description: Provenance and timestamp caveats for the post-price-basis-fix overheated_early promotion analysis
---

For the historical overheated_early promotion review, the exact per-signal fixed-window audit behind the headline `n=227` / post-fix `n=121` was not retained. The available frozen backup can reproduce a 121-row post-fix resolved cohort only when the split is applied to `ts_close`.

**Why:** The price-basis correction takes effect when `demo_positions` is created, so `ts_open` is the causal field. Using `ts_close` is a compatibility reconstruction for the requested historical cohort, not proof that the old fixed-window audit has been recovered.

**How to apply:** Keep the frozen source, timestamp field, cohort size, and observed values explicit in any bootstrap or promotion report. Do not use the changing live `alerts.db` or silently present the backup reconstruction as an exact reproduction of the old headline.