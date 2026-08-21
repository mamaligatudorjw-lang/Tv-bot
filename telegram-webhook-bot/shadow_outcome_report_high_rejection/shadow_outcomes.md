# Shadow outcome report

Fixed window: **24h** after entry; source candles: **Gate.io futures 15m**.
Only `demo_positions.is_shadow=1` signals with valid entry/SL/TP are included.
Unresolved means neither barrier was touched before the window ended; it is not counted as a win or loss.
If a candle touches both barriers, the result is `ambiguous` because OHLC cannot establish intrabar order.
`n = TP-first + SL-first + unresolved + ambiguous`; `WR resolved = TP-first / (TP-first + SL-first)`.
`avg R` includes unresolved signals at the last available price in the fixed window; ambiguous signals have no R.

## Coverage

```json
{
  "signals_loaded": 23,
  "signals_reported": 23,
  "signals_eligible_for_fixed_window_metrics": 16,
  "symbols_loaded": 20,
  "symbol_fetch_failures": {},
  "candle_interval": "15m",
  "signal_min_utc": "2026-08-18T09:36:43+00:00",
  "signal_max_utc": "2026-08-21T10:36:48+00:00",
  "analysis_run_utc": "2026-08-21T12:06:15.004931+00:00",
  "window_not_elapsed": 7,
  "missing_price": 0,
  "range_missing": 0
}
```

## Overall

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| all eligible — preliminary (<20) | 16 | 2 | 13 | 1 | 0 | 13.3% | -0.553 |

## By strategy

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| high_rejection_short — preliminary (<20) | 16 | 2 | 13 | 1 | 0 | 13.3% | -0.553 |

## SHORT: opposite-direction signal within previous 60 minutes

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| no — preliminary (<20) | 9 | 2 | 6 | 1 | 0 | 25.0% | -0.205 |
| yes — preliminary (<20) | 7 | 0 | 7 | 0 | 0 | 0.0% | -1.000 |

## SHORT: 24h range threshold 50.0%

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| at_or_above_50% — preliminary (<20) | 5 | 1 | 4 | 0 | 0 | 20.0% | -0.400 |
| below_50% — preliminary (<20) | 11 | 1 | 9 | 1 | 0 | 10.0% | -0.623 |

## Interpretation guardrails

- Win rate is TP-first among resolved TP/SL outcomes only; unresolved and ambiguous are shown separately.
- `avg R` includes unresolved signals at the last available price in the fixed window.
- Groups with fewer than 20 signals are preliminary and are not a basis for changing filters.
- For subgroups with n=5–6, one signal moves resolved WR by roughly 15–20 percentage points; these comparisons are directional only and are not a basis for setting a filter threshold.
- This report does not change trading behavior or add filters.
