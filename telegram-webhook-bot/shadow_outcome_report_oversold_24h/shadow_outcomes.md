# Outcome report

Fixed window: **24h** after entry; source candles: **Gate.io futures 15m**.
Strategy: **oversold_24h**. Signals with valid entry/SL/TP are included according to the selected shadow/live scope.
For the oversold live audit, non-shadow rows are intentionally included; this report does not place orders or change bot state.
Unresolved means neither barrier was touched before the window ended; it is not counted as a win or loss.
If a candle touches both barriers, the result is `ambiguous` because OHLC cannot establish intrabar order.
`n = TP-first + SL-first + unresolved + ambiguous`; `WR resolved = TP-first / (TP-first + SL-first)`.
`avg R` includes unresolved signals at the last available price in the fixed window; ambiguous signals have no R.

## Coverage

```json
{
  "signals_loaded": 385,
  "signals_reported": 385,
  "signals_eligible_for_fixed_window_metrics": 338,
  "symbols_loaded": 169,
  "hourly_symbols_loaded": 169,
  "symbol_fetch_failures": {},
  "candle_interval": "15m",
  "signal_min_utc": "2026-08-09T19:21:49+00:00",
  "signal_max_utc": "2026-08-22T19:28:03+00:00",
  "analysis_run_utc": "2026-08-22T19:53:12.568233+00:00",
  "window_not_elapsed": 46,
  "missing_price": 1,
  "range_missing": 2,
  "rsi_missing": 2,
  "rsi_engine_snapshot": 0,
  "rsi_reconstructed_legacy": 385,
  "trend_delay_missing": 291,
  "shadow_rows": 0,
  "non_shadow_rows": 385,
  "fix_split_ts": 1787422679,
  "fix_split_utc": "2026-08-22T18:17:59+00:00",
  "reported_by_fix_cohort": {
    "pre_fix": 382,
    "post_fix": 3
  },
  "eligible_by_fix_cohort": {
    "pre_fix": 338,
    "post_fix": 0
  }
}
```

## Overall

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| all eligible | 338 | 91 | 174 | 64 | 9 | 34.3% | -0.044 |

## Before vs after the price-basis fix

Split timestamp: **2026-08-22T18:17:59+00:00** (rows before it are `pre_fix`, rows on/after it are `post_fix`).
All loaded rows by cohort: **{'pre_fix': 382, 'post_fix': 3}**; eligible rows by cohort: **{'pre_fix': 338, 'post_fix': 0}**.

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| pre_fix | 338 | 91 | 174 | 64 | 9 | 34.3% | -0.044 |
| post_fix — preliminary (<20) | 0 | 0 | 0 | 0 | 0 | — | — |

## By strategy

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| oversold_24h | 338 | 91 | 174 | 64 | 9 | 34.3% | -0.044 |

## SHORT: opposite-direction signal within previous 60 minutes

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|

## SHORT: 24h range threshold 50.0%

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|

## LONG: RSI at signal time

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| rsi_lt_80 | 337 | 91 | 173 | 64 | 9 | 34.5% | -0.041 |

## LONG: oversold RSI cohort

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| rsi_gt_30 | 70 | 24 | 33 | 11 | 2 | 42.1% | 0.153 |
| rsi_le_30 | 267 | 67 | 140 | 53 | 7 | 32.4% | -0.092 |

## LONG: delay from detected consecutive hourly uptrend

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| 0-3.9h | 71 | 31 | 27 | 12 | 1 | 53.5% | 0.403 |
| 4-7.9h — preliminary (<20) | 6 | 1 | 4 | 1 | 0 | 20.0% | -0.367 |
| no_consecutive_trend | 261 | 59 | 143 | 51 | 8 | 29.2% | -0.161 |

## LONG: number of upward closes in the strategy's 12h window

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| 1 — preliminary (<20) | 15 | 4 | 5 | 6 | 0 | 44.4% | 0.149 |
| 2 | 33 | 13 | 11 | 8 | 1 | 54.2% | 0.407 |
| 3 | 69 | 18 | 39 | 11 | 1 | 31.6% | -0.105 |
| 4 | 105 | 25 | 61 | 15 | 4 | 29.1% | -0.185 |
| 5 | 81 | 24 | 40 | 15 | 2 | 37.5% | 0.038 |
| 6 | 26 | 4 | 14 | 7 | 1 | 22.2% | -0.352 |
| 7 — preliminary (<20) | 9 | 3 | 4 | 2 | 0 | 42.9% | 0.195 |

## Interpretation guardrails

- Win rate is TP-first among resolved TP/SL outcomes only; unresolved and ambiguous are shown separately.
- `avg R` includes unresolved signals at the last available price in the fixed window.
- Groups with fewer than 20 signals are preliminary and are not a basis for changing filters.
- For subgroups with n=5–6, one signal moves resolved WR by roughly 15–20 percentage points; these comparisons are directional only and are not a basis for setting a filter threshold.
- This report does not change trading behavior or add filters.
- RSI is reconstructed from the 14 latest completed 1h candles before the signal; the live/incomplete candle is excluded.
- For rows created after the RSI snapshot migration, `rsi_source=engine_snapshot` is the exact RSI used by the gate. Legacy rows remain `rsi_source=reconstructed` and may differ near a threshold boundary.
- Trend delay uses the consecutive up-close run ending at the last completed 1h candle. A missing run is reported as `no_consecutive_trend`, not assigned an artificial delay.
- The current bot configuration has a 12h duration window but `min_up=0`; this report measures the observed up-close count and does not reinterpret it as an active gate.
