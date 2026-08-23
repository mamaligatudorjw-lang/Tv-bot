# Outcome report

Fixed window: **24h** after entry; source candles: **Gate.io futures 15m**.
Strategy: **confluence**. Signals with valid entry/SL/TP are included according to the selected shadow/live scope.
These outcomes are measured from the recorded `entry_price`. They do not necessarily equal the price a human could see when acting on the Telegram alert if a polling cycle was delayed; snapshot-to-delivery price risk is a separate measurement.
For the oversold live audit, non-shadow rows are intentionally included; this report does not place orders or change bot state.
Unresolved means neither barrier was touched before the window ended; it is not counted as a win or loss.
If a candle touches both barriers, the result is `ambiguous` because OHLC cannot establish intrabar order.
`n = TP-first + SL-first + unresolved + ambiguous`; `WR resolved = TP-first / (TP-first + SL-first)`.
`avg R` includes unresolved signals at the last available price in the fixed window; ambiguous signals have no R.

## Coverage

```json
{
  "signals_loaded": 295,
  "signals_reported": 295,
  "signals_eligible_for_fixed_window_metrics": 293,
  "symbols_loaded": 234,
  "hourly_symbols_loaded": null,
  "symbol_fetch_failures": {},
  "candle_interval": "15m",
  "signal_min_utc": "2026-08-10T04:50:27+00:00",
  "signal_max_utc": "2026-08-13T10:30:34+00:00",
  "analysis_run_utc": "2026-08-23T19:32:28.563732+00:00",
  "window_not_elapsed": 0,
  "missing_price": 2,
  "range_missing": 2,
  "rsi_missing": 295,
  "rsi_engine_snapshot": 0,
  "rsi_reconstructed_legacy": 295,
  "trend_delay_missing": 295,
  "shadow_rows": 295,
  "non_shadow_rows": 0,
  "fix_split_ts": 1787422679,
  "fix_split_utc": "2026-08-22T18:17:59+00:00",
  "reported_by_fix_cohort": {
    "pre_fix": 295,
    "post_fix": 0
  },
  "eligible_by_fix_cohort": {
    "pre_fix": 293,
    "post_fix": 0
  }
}
```

## Overall

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| all eligible | 293 | 18 | 68 | 201 | 6 | 20.9% | 0.059 |

## Before vs after the price-basis fix

Split timestamp: **2026-08-22T18:17:59+00:00** (rows before it are `pre_fix`, rows on/after it are `post_fix`).
All loaded rows by cohort: **{'pre_fix': 295, 'post_fix': 0}**; eligible rows by cohort: **{'pre_fix': 293, 'post_fix': 0}**.

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| pre_fix | 293 | 18 | 68 | 201 | 6 | 20.9% | 0.059 |
| post_fix — preliminary (<20) | 0 | 0 | 0 | 0 | 0 | — | — |

## By strategy

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| confluence | 293 | 18 | 68 | 201 | 6 | 20.9% | 0.059 |

## SHORT: opposite-direction signal within previous 60 minutes

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| no | 206 | 6 | 38 | 160 | 2 | 13.6% | 0.082 |
| yes — preliminary (<20) | 7 | 0 | 0 | 7 | 0 | — | 0.559 |

## SHORT: 24h range threshold 50.0%

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| at_or_above_50% — preliminary (<20) | 3 | 0 | 2 | 1 | 0 | 0.0% | -0.598 |
| below_50% | 210 | 6 | 36 | 166 | 2 | 14.3% | 0.108 |

## LONG: RSI at signal time

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|

## LONG: oversold RSI cohort

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|

## LONG: delay from detected consecutive hourly uptrend

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| no_consecutive_trend | 80 | 12 | 30 | 34 | 4 | 28.6% | -0.048 |

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
