---
name: Gate.io liquidation history
description: Gate.io public futures liquidation and trade history supports read-only historical event analysis.
---

Gate.io `/futures/usdt/liq_orders` returns real historical liquidation records with timestamp, signed size, and fill price; the project's `size < 0` → long mapping is an inferred calibration, not an explicit provider-documented sign contract. The endpoint accepts at most one hour per `from`/`to` request and can hit the `limit=1000` cap, so full hours need overflow checks and finer subranges. Spot checks established at least roughly 91 days of accessible history, but not the provider's absolute retention period.

**Why:** Historical liquidation events are materially stronger evidence than a price/volume liquidation proxy, while silently treating capped responses as complete would bias event counts downward.

**How to apply:** Require an externally evidenced, fail-closed sign calibration before interpreting liquidation direction; keep liquidation evidence separate from any large-5m-flow proxy, record incomplete coverage explicitly, and calculate the existing Gate notional as `abs(size) * fill_price` unless the analysis intentionally changes that convention.