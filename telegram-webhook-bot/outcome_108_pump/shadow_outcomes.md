# Outcome report

Fixed window: **24h** after entry; source candles: **Gate.io futures 15m**.
Strategy: **pump_24h_fade**. Signals with valid entry/SL/TP are included according to the selected shadow/live scope.
These outcomes are measured from the recorded `entry_price`. They do not necessarily equal the price a human could see when acting on the Telegram alert if a polling cycle was delayed; snapshot-to-delivery price risk is a separate measurement.
For the oversold live audit, non-shadow rows are intentionally included; this report does not place orders or change bot state.
Unresolved means neither barrier was touched before the window ended; it is not counted as a win or loss.
If a candle touches both barriers, the result is `ambiguous` because OHLC cannot establish intrabar order.
`n = TP-first + SL-first + unresolved + ambiguous`; `WR resolved = TP-first / (TP-first + SL-first)`.
`avg R` includes unresolved signals at the last available price in the fixed window; ambiguous signals have no R.

## Coverage

```json
{
  "signals_loaded": 110,
  "signals_reported": 110,
  "signals_eligible_for_fixed_window_metrics": 109,
  "symbols_loaded": 62,
  "hourly_symbols_loaded": null,
  "symbol_fetch_failures": {},
  "candle_interval": "15m",
  "signal_min_utc": "2026-08-12T16:38:55+00:00",
  "signal_max_utc": "2026-08-23T15:58:22+00:00",
  "analysis_run_utc": "2026-08-23T19:32:00.144302+00:00",
  "window_not_elapsed": 1,
  "missing_price": 0,
  "range_missing": 0,
  "rsi_missing": 110,
  "rsi_engine_snapshot": 0,
  "rsi_reconstructed_legacy": 110,
  "trend_delay_missing": 110,
  "shadow_rows": 110,
  "non_shadow_rows": 0,
  "fix_split_ts": 1787422679,
  "fix_split_utc": "2026-08-22T18:17:59+00:00",
  "reported_by_fix_cohort": {
    "pre_fix": 107,
    "post_fix": 3
  },
  "eligible_by_fix_cohort": {
    "pre_fix": 107,
    "post_fix": 2
  }
}
```

## Overall

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| all eligible | 109 | 20 | 57 | 32 | 0 | 26.0% | 0.080 |

## Before vs after the price-basis fix

Split timestamp: **2026-08-22T18:17:59+00:00** (rows before it are `pre_fix`, rows on/after it are `post_fix`).
All loaded rows by cohort: **{'pre_fix': 107, 'post_fix': 3}**; eligible rows by cohort: **{'pre_fix': 107, 'post_fix': 2}**.

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| pre_fix | 107 | 19 | 56 | 32 | 0 | 25.3% | 0.072 |
| post_fix — preliminary (<20) | 2 | 1 | 1 | 0 | 0 | 50.0% | 0.500 |

## By strategy

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| pump_24h_fade | 109 | 20 | 57 | 32 | 0 | 26.0% | 0.080 |

## SHORT: opposite-direction signal within previous 60 minutes

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| no | 59 | 10 | 33 | 16 | 0 | 23.3% | -0.024 |
| yes | 50 | 10 | 24 | 16 | 0 | 29.4% | 0.201 |

## SHORT: 24h range threshold 50.0%

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| at_or_above_50% | 51 | 11 | 29 | 11 | 0 | 27.5% | 0.073 |
| below_50% | 58 | 9 | 28 | 21 | 0 | 24.3% | 0.085 |

## LONG: RSI at signal time

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|

## LONG: oversold RSI cohort

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|

## LONG: delay from detected consecutive hourly uptrend

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|

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
