---
name: Gate.io 1m history coverage
description: Historical 1m futures candles are not uniformly available across older contracts.
---

Gate.io may return HTTP 400 for historical 1m candle ranges on older or less-supported contracts even when the same symbols have usable 5m history. Treat 1m failure as missing coverage, not as a market outcome.

**Why:** A broad trailing-stop path test produced widespread 1m request failures while the equivalent chunked 5m requests covered the same symbol set, so silently falling back would have changed the sample.

**How to apply:** Run a coverage probe first; use 5m as the reproducible fallback when the task allows it, and report/exclude symbols without complete path coverage rather than interpreting failed fetches as price behavior.