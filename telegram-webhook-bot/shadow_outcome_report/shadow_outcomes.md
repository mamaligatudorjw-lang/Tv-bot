# Shadow outcome report

Fixed window: **24h** after entry; source candles: **Gate.io futures 15m**.
These outcomes are measured from the recorded `entry_price`. They do not necessarily equal the price a human could see when acting on the Telegram alert if a polling cycle was delayed; snapshot-to-delivery price risk is a separate measurement.
Only `demo_positions.is_shadow=1` signals with valid entry/SL/TP are included.
Unresolved means neither barrier was touched before the window ended; it is not counted as a win or loss.
If a candle touches both barriers, the result is `ambiguous` because OHLC cannot establish intrabar order.

## Coverage

```json
{
  "signals_loaded": 2157,
  "signals_reported": 2157,
  "signals_eligible_for_fixed_window_metrics": 1999,
  "symbols_loaded": 497,
  "symbol_fetch_failures": {},
  "candle_interval": "15m",
  "signal_min_utc": "2026-08-09T19:21:50+00:00",
  "signal_max_utc": "2026-08-21T11:00:20+00:00",
  "analysis_run_utc": "2026-08-21T11:03:03.092353+00:00",
  "window_not_elapsed": 154,
  "missing_price": 4,
  "range_missing": 4
}
```

## Overall

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| all eligible | 1999 | 587 | 839 | 394 | 179 | 41.2% | 0.222 |

## By strategy

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| bb_squeeze | 498 | 152 | 308 | 36 | 2 | 33.0% | 0.022 |
| confluence | 293 | 18 | 68 | 201 | 6 | 20.9% | 0.059 |
| ema_cross | 254 | 95 | 101 | 58 | 0 | 48.5% | 0.426 |
| ema_cross_confirmed | 209 | 88 | 41 | 39 | 41 | 68.2% | 0.937 |
| high_rejection_short — preliminary (<20) | 15 | 2 | 12 | 1 | 0 | 14.3% | -0.523 |
| liq_reversal — preliminary (<20) | 1 | 0 | 1 | 0 | 0 | 0.0% | -1.000 |
| low_rejection_long — preliminary (<20) | 7 | 4 | 3 | 0 | 0 | 57.1% | 0.714 |
| overheated_24h | 228 | 93 | 117 | 14 | 4 | 44.3% | 0.297 |
| overheated_confirmed | 236 | 56 | 68 | 8 | 104 | 45.2% | 0.390 |
| overheated_early | 60 | 34 | 19 | 7 | 0 | 64.2% | 0.845 |
| oversold_24h | 82 | 27 | 49 | 6 | 0 | 35.5% | -0.085 |
| pump_24h_fade | 78 | 13 | 47 | 18 | 0 | 21.7% | -0.081 |
| pump_fade_confirmed | 33 | 3 | 3 | 5 | 22 | 50.0% | 0.560 |
| range_breakout_long — preliminary (<20) | 3 | 0 | 2 | 1 | 0 | 0.0% | -0.643 |
| vwap_reversion — preliminary (<20) | 2 | 2 | 0 | 0 | 0 | 100.0% | 1.747 |

## SHORT: opposite-direction signal within previous 60 minutes

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| no | 662 | 121 | 270 | 238 | 33 | 30.9% | 0.020 |
| yes | 70 | 7 | 29 | 22 | 12 | 19.4% | -0.028 |

## SHORT: 24h range threshold 50.0%

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| at_or_above_50% | 71 | 13 | 35 | 12 | 11 | 27.1% | 0.019 |
| below_50% | 661 | 115 | 264 | 248 | 34 | 30.3% | 0.015 |

## Interpretation guardrails

- Win rate is TP-first among resolved TP/SL outcomes only; unresolved and ambiguous are shown separately.
- `avg R` includes unresolved signals at the last available price in the fixed window.
- Groups with fewer than 20 signals are preliminary and are not a basis for changing filters.
- This report does not change trading behavior or add filters.
