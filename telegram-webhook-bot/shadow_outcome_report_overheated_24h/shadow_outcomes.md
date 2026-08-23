# Shadow outcome report

Fixed window: **24h** after entry; source candles: **Gate.io futures 15m**.
These outcomes are measured from the recorded `entry_price`. They do not necessarily equal the price a human could see when acting on the Telegram alert if a polling cycle was delayed; snapshot-to-delivery price risk is a separate measurement.
Only `demo_positions.is_shadow=1` signals with valid entry/SL/TP are included.
Unresolved means neither barrier was touched before the window ended; it is not counted as a win or loss.
If a candle touches both barriers, the result is `ambiguous` because OHLC cannot establish intrabar order.
`n = TP-first + SL-first + unresolved + ambiguous`; `WR resolved = TP-first / (TP-first + SL-first)`.
`avg R` includes unresolved signals at the last available price in the fixed window; ambiguous signals have no R.

## Coverage

```json
{
  "signals_loaded": 295,
  "signals_reported": 295,
  "signals_eligible_for_fixed_window_metrics": 271,
  "symbols_loaded": 163,
  "hourly_symbols_loaded": 163,
  "symbol_fetch_failures": {},
  "candle_interval": "15m",
  "signal_min_utc": "2026-08-09T19:42:20+00:00",
  "signal_max_utc": "2026-08-21T14:11:20+00:00",
  "analysis_run_utc": "2026-08-21T19:25:20.388893+00:00",
  "window_not_elapsed": 24,
  "missing_price": 0,
  "range_missing": 0,
  "rsi_missing": 0,
  "trend_delay_missing": 67
}
```

## Overall

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| all eligible | 271 | 108 | 141 | 17 | 5 | 43.4% | 0.279 |

## By strategy

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| overheated_24h | 271 | 108 | 141 | 17 | 5 | 43.4% | 0.279 |

## SHORT: opposite-direction signal within previous 60 minutes

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|

## SHORT: 24h range threshold 50.0%

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|

## LONG: RSI at signal time

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| rsi_ge_80 | 126 | 46 | 74 | 4 | 2 | 38.3% | 0.128 |
| rsi_lt_80 | 145 | 62 | 67 | 13 | 3 | 48.1% | 0.410 |

## LONG: delay from detected consecutive hourly uptrend

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| 0-3.9h | 100 | 36 | 55 | 8 | 1 | 39.6% | 0.169 |
| 12h+ — preliminary (<20) | 2 | 2 | 0 | 0 | 0 | 100.0% | 2.000 |
| 4-7.9h | 91 | 37 | 49 | 3 | 2 | 43.0% | 0.260 |
| 8-11.9h — preliminary (<20) | 17 | 4 | 10 | 2 | 1 | 28.6% | -0.089 |
| no_consecutive_trend | 61 | 29 | 27 | 4 | 1 | 51.8% | 0.528 |

## LONG: number of upward closes in the strategy's 12h window

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| 10 | 29 | 15 | 13 | 1 | 0 | 53.6% | 0.596 |
| 11 — preliminary (<20) | 13 | 3 | 9 | 0 | 1 | 25.0% | -0.350 |
| 12 — preliminary (<20) | 2 | 2 | 0 | 0 | 0 | 100.0% | 2.000 |
| 4 — preliminary (<20) | 4 | 2 | 0 | 0 | 2 | 100.0% | 2.000 |
| 5 — preliminary (<20) | 6 | 1 | 4 | 1 | 0 | 20.0% | -0.324 |
| 6 | 36 | 15 | 20 | 1 | 0 | 42.9% | 0.259 |
| 7 | 63 | 23 | 31 | 9 | 0 | 42.6% | 0.248 |
| 8 | 66 | 25 | 37 | 3 | 1 | 40.3% | 0.199 |
| 9 | 52 | 22 | 27 | 2 | 1 | 44.9% | 0.335 |

## Interpretation guardrails

- Win rate is TP-first among resolved TP/SL outcomes only; unresolved and ambiguous are shown separately.
- `avg R` includes unresolved signals at the last available price in the fixed window.
- Groups with fewer than 20 signals are preliminary and are not a basis for changing filters.
- For subgroups with n=5–6, one signal moves resolved WR by roughly 15–20 percentage points; these comparisons are directional only and are not a basis for setting a filter threshold.
- This report does not change trading behavior or add filters.
- RSI is reconstructed from the 14 latest completed 1h candles before the signal; the live/incomplete candle is excluded.
- Trend delay uses the consecutive up-close run ending at the last completed 1h candle. A missing run is reported as `no_consecutive_trend`, not assigned an artificial delay.
- The current bot configuration has a 12h duration window but `min_up=0`; this report measures the observed up-close count and does not reinterpret it as an active gate.
