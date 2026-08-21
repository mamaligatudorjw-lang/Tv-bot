---
name: Gate.io candle analysis constraints
description: Historical futures candle reports must respect Gate.io parameter and rate-limit behavior.
---

Gate.io futures candlesticks rejects requests that include `limit` together with `from` and `to`; long 15m histories must be split into chunks of at most 999 candles. Bounded, low-concurrency fetching is needed to avoid intermittent 429 responses.

**Why:** A first parallel implementation produced an apparently complete report with no price coverage because every request was rejected by the parameter rule; higher concurrency also caused partial symbol coverage through rate limiting.

**How to apply:** Keep historical analysis separate from trading logic, record symbol fetch failures in coverage, and prefer low worker counts or cached candles when reproducibility matters.