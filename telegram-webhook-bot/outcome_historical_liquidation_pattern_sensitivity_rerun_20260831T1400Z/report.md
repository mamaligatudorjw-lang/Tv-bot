# Historical liquidation threshold sensitivity

**Read-only comparison. The baseline scan and production behavior are unchanged.**

- Source scan generated: **2026-08-31T14:14:25+00:00**
- Source report SHA-256: `75969e7ba47b0eeee68891646c24c8f62ff4d31fc9be2b0f1d2e0e531cc800c1`
- Downstream replays fetched from Gate: **15**

## Fixed preregistered grid

| Threshold | Minimum USD | Hourly fraction | Role |
|---|---:|---:|---|
| $25,000 / 0.5% | $25,000 | 0.5% | sensitivity |
| $50,000 / 1% | $50,000 | 1.0% | sensitivity |
| $100,000 / 2% (baseline) | $100,000 | 2.0% | baseline |
| $150,000 / 3% | $150,000 | 3.0% | sensitivity |

## Cumulative primary results

| Threshold | Event rows | Resolved n | Continuation | Retest/hold | Failure | No outcome | Success rate | Sufficiency | Decision |
|---|---:|---:|---:|---:|---:|---:|---:|---|---|
| $25,000 / 0.5% | 180 | 14 | 13 | 1 | 0 | 0 | 1.000 | descriptive_only | descriptive_only |
| $50,000 / 1% | 180 | 13 | 13 | 0 | 0 | 0 | 1.000 | descriptive_only | descriptive_only |
| $100,000 / 2% (baseline) | 180 | 7 | 7 | 0 | 0 | 0 | 1.000 | descriptive_only | descriptive_only |
| $150,000 / 3% | 180 | 5 | 5 | 0 | 0 | 0 | 1.000 | descriptive_only | descriptive_only |

## Cumulative controls

| Threshold | Cohort | Event rows | Resolved n | Continuation | Retest/hold | Failure | No outcome |
|---|---|---:|---:|---:|---:|---:|---:|
| $25,000 / 0.5% | control | 1 | 0 | 0 | 0 | 0 | 0 |
| $25,000 / 0.5% | control:BTCUSDT | 0 | 0 | 0 | 0 | 0 | 0 |
| $25,000 / 0.5% | control:ETHUSDT | 1 | 0 | 0 | 0 | 0 | 0 |
| $25,000 / 0.5% | control:SOLUSDT | 0 | 0 | 0 | 0 | 0 | 0 |
| $50,000 / 1% | control | 1 | 0 | 0 | 0 | 0 | 0 |
| $50,000 / 1% | control:BTCUSDT | 0 | 0 | 0 | 0 | 0 | 0 |
| $50,000 / 1% | control:ETHUSDT | 1 | 0 | 0 | 0 | 0 | 0 |
| $50,000 / 1% | control:SOLUSDT | 0 | 0 | 0 | 0 | 0 | 0 |
| $100,000 / 2% (baseline) | control | 1 | 0 | 0 | 0 | 0 | 0 |
| $100,000 / 2% (baseline) | control:BTCUSDT | 0 | 0 | 0 | 0 | 0 | 0 |
| $100,000 / 2% (baseline) | control:ETHUSDT | 1 | 0 | 0 | 0 | 0 | 0 |
| $100,000 / 2% (baseline) | control:SOLUSDT | 0 | 0 | 0 | 0 | 0 | 0 |
| $150,000 / 3% | control | 1 | 0 | 0 | 0 | 0 | 0 |
| $150,000 / 3% | control:BTCUSDT | 0 | 0 | 0 | 0 | 0 | 0 |
| $150,000 / 3% | control:ETHUSDT | 1 | 0 | 0 | 0 | 0 | 0 |
| $150,000 / 3% | control:SOLUSDT | 0 | 0 | 0 | 0 | 0 | 0 |

## Adjacent incremental bands

Each band contains rows that pass the softer threshold and fail the immediately stricter threshold. `no_outcome_in_window` is excluded from `resolved n`; controls are not pooled.

| Softer threshold | Stricter threshold | Cohort | Rows | Resolved n | Success | Failure | No outcome | Success rate | Quality |
|---|---|---|---:|---:|---:|---:|---:|---:|---|
| $25,000 / 0.5% | $50,000 / 1% | primary | 7 | 1 | 1 | 0 | 0 | 1.000 | not_clearly_better |
| $25,000 / 0.5% | $50,000 / 1% | control | 0 | 0 | 0 | 0 | 0 | — | controls_not_pooled |
| $25,000 / 0.5% | $50,000 / 1% | control:BTCUSDT | 0 | 0 | 0 | 0 | 0 | — | controls_not_pooled |
| $25,000 / 0.5% | $50,000 / 1% | control:ETHUSDT | 0 | 0 | 0 | 0 | 0 | — | controls_not_pooled |
| $25,000 / 0.5% | $50,000 / 1% | control:SOLUSDT | 0 | 0 | 0 | 0 | 0 | — | controls_not_pooled |
| $50,000 / 1% | $100,000 / 2% (baseline) | primary | 8 | 6 | 6 | 0 | 0 | 1.000 | not_clearly_better |
| $50,000 / 1% | $100,000 / 2% (baseline) | control | 0 | 0 | 0 | 0 | 0 | — | controls_not_pooled |
| $50,000 / 1% | $100,000 / 2% (baseline) | control:BTCUSDT | 0 | 0 | 0 | 0 | 0 | — | controls_not_pooled |
| $50,000 / 1% | $100,000 / 2% (baseline) | control:ETHUSDT | 0 | 0 | 0 | 0 | 0 | — | controls_not_pooled |
| $50,000 / 1% | $100,000 / 2% (baseline) | control:SOLUSDT | 0 | 0 | 0 | 0 | 0 | — | controls_not_pooled |
| $100,000 / 2% (baseline) | $150,000 / 3% | primary | 3 | 2 | 2 | 0 | 0 | 1.000 | not_clearly_better |
| $100,000 / 2% (baseline) | $150,000 / 3% | control | 0 | 0 | 0 | 0 | 0 | — | controls_not_pooled |
| $100,000 / 2% (baseline) | $150,000 / 3% | control:BTCUSDT | 0 | 0 | 0 | 0 | 0 | — | controls_not_pooled |
| $100,000 / 2% (baseline) | $150,000 / 3% | control:ETHUSDT | 0 | 0 | 0 | 0 | 0 | — | controls_not_pooled |
| $100,000 / 2% (baseline) | $150,000 / 3% | control:SOLUSDT | 0 | 0 | 0 | 0 | 0 | — | controls_not_pooled |

## Decision

- Global decision: **descriptive_only_no_threshold_optimization**
- Baseline remains **descriptive_only** with resolved `n=7` and success rate 1.000.
- A larger `n` alone is not treated as better quality; the comparison uses incremental resolved success/failure rates.
- No production scoring, filters, whitelist, execution, TP/SL, polling, reserve protection, or Telegram behavior changed.

## Replay coverage

| Replay key | Coverage |
|---|---|
| `('WLDUSDT', 1780596900, 1780600500, 1780599600)` | `{"five_minute_reason": "candle_fetch_error:Gate request failed for /candlesticks: 400 Client Error: Bad Request for url: https://api.gateio.ws/api/v4/futures/usdt/candlesticks?contract=WLD_USDT&interval=5m&from=1780517700&to=1780625699", "five_minute_status": "incomplete", "outcome_15m_reason": "", "outcome_15m_status": "not_requested"}` |
| `('WLDUSDT', 1780503300, 1780538400, 1780505100)` | `{"five_minute_reason": "candle_fetch_error:Gate request failed for /candlesticks: 400 Client Error: Bad Request for url: https://api.gateio.ws/api/v4/futures/usdt/candlesticks?contract=WLD_USDT&interval=5m&from=1780455600&to=1780563599", "five_minute_status": "incomplete", "outcome_15m_reason": "", "outcome_15m_status": "not_requested"}` |
| `('WLDUSDT', 1780479000, 1780538400, 1780501500)` | `{"five_minute_reason": "candle_fetch_error:Gate request failed for /candlesticks: 400 Client Error: Bad Request for url: https://api.gateio.ws/api/v4/futures/usdt/candlesticks?contract=WLD_USDT&interval=5m&from=1780455600&to=1780563599", "five_minute_status": "incomplete", "outcome_15m_reason": "", "outcome_15m_status": "not_requested"}` |
| `('WLDUSDT', 1780601400, 1780604100, 1780603200)` | `{"five_minute_reason": "candle_fetch_error:Gate request failed for /candlesticks: 400 Client Error: Bad Request for url: https://api.gateio.ws/api/v4/futures/usdt/candlesticks?contract=WLD_USDT&interval=5m&from=1780521300&to=1780629299", "five_minute_status": "incomplete", "outcome_15m_reason": "", "outcome_15m_status": "not_requested"}` |
| `('PROMUSDT', 1787329800, 1787330700, 1787329800)` | `{"five_minute_reason": "", "five_minute_status": "complete", "outcome_15m_reason": "", "outcome_15m_status": "complete"}` |
| `('TRUMPUSDT', 1787364900, 1787397300, 1787396400)` | `{"five_minute_reason": "", "five_minute_status": "complete", "outcome_15m_reason": "", "outcome_15m_status": "complete"}` |
| `('SUIUSDT', 1787371200, 1787374800, 1787371200)` | `{"five_minute_reason": "", "five_minute_status": "complete", "outcome_15m_reason": "", "outcome_15m_status": "complete"}` |
| `('WLDUSDT', 1787371200, 1787374800, 1787373900)` | `{"five_minute_reason": "", "five_minute_status": "complete", "outcome_15m_reason": "", "outcome_15m_status": "not_requested"}` |
| `('DOGEUSDT', 1787373000, 1787374800, 1787373000)` | `{"five_minute_reason": "", "five_minute_status": "complete", "outcome_15m_reason": "", "outcome_15m_status": "complete"}` |
| `('SUIUSDT', 1787373000, 1787374800, 1787373900)` | `{"five_minute_reason": "", "five_minute_status": "complete", "outcome_15m_reason": "", "outcome_15m_status": "complete"}` |
| `('TRUMPUSDT', 1781256600, 1781271000, 1781270100)` | `{"five_minute_reason": "candle_fetch_error:Gate request failed for /candlesticks: 400 Client Error: Bad Request for url: https://api.gateio.ws/api/v4/futures/usdt/candlesticks?contract=TRUMP_USDT&interval=5m&from=1781188200&to=1781296199", "five_minute_status": "incomplete", "outcome_15m_reason": "", "outcome_15m_status": "not_requested"}` |
| `('ZHIPUUSDT', 1782091800, 1782092700, 1782091800)` | `{"five_minute_reason": "candle_fetch_error:Gate request failed for /candlesticks: 400 Client Error: Bad Request for url: https://api.gateio.ws/api/v4/futures/usdt/candlesticks?contract=ZHIPU_USDT&interval=5m&from=1782009900&to=1782117899", "five_minute_status": "incomplete", "outcome_15m_reason": "", "outcome_15m_status": "not_requested"}` |
| `('TRUMPUSDT', 1781275500, 1781288100, 1781278200)` | `{"five_minute_reason": "candle_fetch_error:Gate request failed for /candlesticks: 400 Client Error: Bad Request for url: https://api.gateio.ws/api/v4/futures/usdt/candlesticks?contract=TRUMP_USDT&interval=5m&from=1781205300&to=1781313299", "five_minute_status": "incomplete", "outcome_15m_reason": "", "outcome_15m_status": "not_requested"}` |
| `('TRUMPUSDT', 1787491800, 1787530500, 1787491800)` | `{"five_minute_reason": "", "five_minute_status": "complete", "outcome_15m_reason": "", "outcome_15m_status": "complete"}` |
| `('PROMUSDT', 1787546700, 1787574600, 1787573700)` | `{"five_minute_reason": "", "five_minute_status": "complete", "outcome_15m_reason": "", "outcome_15m_status": "complete"}` |
