# Outcome report

Fixed window: **24h** after entry; source candles: **Gate.io futures 15m**.
Strategy: **selected strategies**. Signals with valid entry/SL/TP are included according to the selected shadow/live scope.
These outcomes are measured from the recorded `entry_price`. They do not necessarily equal the price a human could see when acting on the Telegram alert if a polling cycle was delayed; snapshot-to-delivery price risk is a separate measurement.
For the oversold live audit, non-shadow rows are intentionally included; this report does not place orders or change bot state.
Unresolved means neither barrier was touched before the window ended; it is not counted as a win or loss.
If a candle touches both barriers, the result is `ambiguous` because OHLC cannot establish intrabar order.
`n = TP-first + SL-first + unresolved + ambiguous`; `WR resolved = TP-first / (TP-first + SL-first)`.
`avg R` includes unresolved signals at the last available price in the fixed window; ambiguous signals have no R.

## Coverage

```json
{
  "signals_loaded": 649,
  "signals_reported": 649,
  "signals_eligible_for_fixed_window_metrics": 620,
  "symbols_loaded": 249,
  "hourly_symbols_loaded": null,
  "symbol_fetch_failures": {},
  "candle_interval": "15m",
  "signal_min_utc": "2026-08-13T11:21:02+00:00",
  "signal_max_utc": "2026-08-23T18:45:16+00:00",
  "analysis_run_utc": "2026-08-23T19:32:10.020497+00:00",
  "window_not_elapsed": 29,
  "missing_price": 0,
  "range_missing": 0,
  "rsi_missing": 649,
  "rsi_engine_snapshot": 0,
  "rsi_reconstructed_legacy": 649,
  "trend_delay_missing": 649,
  "shadow_rows": 649,
  "non_shadow_rows": 0,
  "fix_split_ts": 1787422679,
  "fix_split_utc": "2026-08-22T18:17:59+00:00",
  "reported_by_fix_cohort": {
    "pre_fix": 596,
    "post_fix": 53
  },
  "eligible_by_fix_cohort": {
    "pre_fix": 596,
    "post_fix": 24
  }
}
```

## Overall

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| all eligible | 620 | 214 | 206 | 148 | 52 | 51.0% | 0.440 |

## Before vs after the price-basis fix

Split timestamp: **2026-08-22T18:17:59+00:00** (rows before it are `pre_fix`, rows on/after it are `post_fix`).
All loaded rows by cohort: **{'pre_fix': 596, 'post_fix': 53}**; eligible rows by cohort: **{'pre_fix': 596, 'post_fix': 24}**.

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| pre_fix | 596 | 212 | 187 | 146 | 51 | 53.1% | 0.488 |
| post_fix | 24 | 2 | 19 | 2 | 1 | 9.5% | -0.603 |

## By strategy

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| ema_cross | 330 | 112 | 136 | 82 | 0 | 45.2% | 0.345 |
| ema_cross_confirmed | 290 | 102 | 70 | 66 | 52 | 59.3% | 0.653 |

## SHORT: opposite-direction signal within previous 60 minutes

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| no | 257 | 53 | 87 | 91 | 26 | 37.9% | 0.061 |
| yes — preliminary (<20) | 15 | 4 | 6 | 4 | 1 | 40.0% | 0.062 |

## SHORT: 24h range threshold 50.0%

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| at_or_above_50% | 26 | 10 | 5 | 10 | 1 | 66.7% | 0.415 |
| below_50% | 246 | 47 | 88 | 85 | 26 | 34.8% | 0.022 |

## LONG: RSI at signal time

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|

## LONG: oversold RSI cohort

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|

## LONG: delay from detected consecutive hourly uptrend

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| no_consecutive_trend | 348 | 157 | 113 | 53 | 25 | 58.1% | 0.700 |

## LONG: number of upward closes in the strategy's 12h window

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|

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
