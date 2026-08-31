---
name: Liquidation threshold sensitivity
description: Durable rules for comparing historical liquidation thresholds without contaminating the baseline.
---

The fixed liquidation-threshold grid is a read-only sensitivity analysis. The `$100,000 / 2%` result is the immutable baseline; softer or stricter thresholds are separate cumulative cohorts, with adjacent incremental bands reported explicitly.

**Why:** A softer threshold can admit rows whose downstream 5m flow or 15m outcome history is unavailable. Counting those rows as failures or successes would turn exchange coverage problems into a strategy conclusion, while rewriting the baseline would destroy comparability.

**How to apply:** Reuse the frozen baseline facts, fetch downstream candles only for newly admitted rows, keep incomplete coverage unresolved and outside resolved `n`, and treat a larger sample as insufficient evidence unless incremental quality is clearly better.