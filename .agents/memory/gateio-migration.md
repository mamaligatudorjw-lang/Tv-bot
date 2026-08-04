---
name: Gate.io Futures migration
description: Why and how the bot switched from Binance Spot to Gate.io Futures as its market-data source.
---

## Rule
All market data comes from Gate.io Futures (`https://api.gateio.ws/api/v4/futures/usdt`). Binance Spot and Binance Futures are both geo-blocked (403/timeout) from Replit servers.

**Why:** Binance Spot returns 403 from all Replit server IPs. Gate.io, MEXC, and OKX are accessible. Gate.io was chosen because it has the deepest USDT perp coverage (878 contracts) and clean REST API.

**How to apply:**
- Symbol conversion: internal `AKEUSDT` ↔ Gate.io contract `AKE_USDT` via `_to_gate()` / `_from_gate()`.
- `_gateio_klines(symbol, interval, limit)` returns data in Binance-compatible list format `[ts, o, h, l, c, v, ts, sum]` — all downstream RSI/EMA/ATR logic works unchanged.
- `_gateio_ticker(symbol)` returns Binance-compatible dict with `lastPrice`, `priceChangePercent`, `quoteVolume`, `highPrice`, `lowPrice`.
- Available intervals: `1m`, `5m`, `15m`, `1h`, `4h`, `1d` (all needed intervals present).
- First live cycle: 878 pairs fetched, 444 passed $50k volume filter, 0 errors, completed in 4.2s.
- `_binance_get()` and `_binance_futures_get()` functions are still present in code but not called by any data pipeline (only the dead-code functions remain).
