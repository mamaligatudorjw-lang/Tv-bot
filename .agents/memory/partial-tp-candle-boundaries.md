---
name: Partial TP candle boundaries
description: Historical partial-TP replays must separate a confirmed frozen TP event from usable post-TP OHLC.
---

When a frozen outcome confirms TP before the first completed 5m candle after entry, use the frozen event as the TP trigger but never reuse that trigger candle's OHLC to update the trailing remainder.

**Why:** The database outcome can prove that a barrier was reached inside an incomplete candle, while its OHLC cannot safely reveal the intrabar sequence after that trigger.

**How to apply:** Start trailing on the next completed candle, preserve the frozen cutoff, and keep coverage explicit rather than inventing an intrabar trailing exit.