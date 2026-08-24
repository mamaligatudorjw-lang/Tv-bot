# Outcome report

Fixed window: **24h** after entry; source candles: **Gate.io futures 15m**.
Strategy: **bb_squeeze**. Signals with valid entry/SL/TP are included according to the selected shadow/live scope.
These outcomes are measured from the recorded `entry_price`. They do not necessarily equal the price a human could see when acting on the Telegram alert if a polling cycle was delayed; snapshot-to-delivery price risk is a separate measurement.
For the oversold live audit, non-shadow rows are intentionally included; this report does not place orders or change bot state.
Unresolved means neither barrier was touched before the window ended; it is not counted as a win or loss.
If a candle touches both barriers, the result is `ambiguous` because OHLC cannot establish intrabar order.
`n = TP-first + SL-first + unresolved + ambiguous`; `WR resolved = TP-first / (TP-first + SL-first)`.
`avg R` includes unresolved signals at the last available price in the fixed window; ambiguous signals have no R.

## Coverage

```json
{
  "signals_loaded": 709,
  "signals_reported": 709,
  "signals_eligible_for_fixed_window_metrics": 673,
  "symbols_loaded": 374,
  "hourly_symbols_loaded": null,
  "symbol_fetch_failures": {
    "USELESSUSDT": "429 Client Error: Too Many Requests for url: https://api.gateio.ws/api/v4/futures/usdt/candlesticks?contract=USELESS_USDT&interval=15m&from=1787205355&to=1787378155",
    "USTCUSDT": "429 Client Error: Too Many Requests for url: https://api.gateio.ws/api/v4/futures/usdt/candlesticks?contract=USTC_USDT&interval=15m&from=1787504428&to=1787598633",
    "USUALUSDT": "429 Client Error: Too Many Requests for url: https://api.gateio.ws/api/v4/futures/usdt/candlesticks?contract=USUAL_USDT&interval=15m&from=1787410976&to=1787583776",
    "USUSDT": "429 Client Error: Too Many Requests for url: https://api.gateio.ws/api/v4/futures/usdt/candlesticks?contract=US_USDT&interval=15m&from=1786724516&to=1787131578",
    "UVXYUSDT": "429 Client Error: Too Many Requests for url: https://api.gateio.ws/api/v4/futures/usdt/candlesticks?contract=UVXY_USDT&interval=15m&from=1787379471&to=1787552271",
    "VANAUSDT": "429 Client Error: Too Many Requests for url: https://api.gateio.ws/api/v4/futures/usdt/candlesticks?contract=VANA_USDT&interval=15m&from=1787072334&to=1787245134",
    "VELVETUSDT": "429 Client Error: Too Many Requests for url: https://api.gateio.ws/api/v4/futures/usdt/candlesticks?contract=VELVET_USDT&interval=15m&from=1786597630&to=1787390432",
    "VIRTUALUSDT": "429 Client Error: Too Many Requests for url: https://api.gateio.ws/api/v4/futures/usdt/candlesticks?contract=VIRTUAL_USDT&interval=15m&from=1786727089&to=1787550412"
  },
  "candle_interval": "15m",
  "signal_min_utc": "2026-08-13T11:15:00+00:00",
  "signal_max_utc": "2026-08-24T19:09:05+00:00",
  "analysis_run_utc": "2026-08-24T19:10:54.395684+00:00",
  "window_not_elapsed": 25,
  "missing_price": 11,
  "range_missing": 11,
  "rsi_missing": 709,
  "rsi_engine_snapshot": 0,
  "rsi_reconstructed_legacy": 709,
  "trend_delay_missing": 709,
  "shadow_rows": 709,
  "non_shadow_rows": 0,
  "fix_split_ts": 1787422679,
  "fix_split_utc": "2026-08-22T18:17:59+00:00",
  "reported_by_fix_cohort": {
    "pre_fix": 549,
    "post_fix": 160
  },
  "eligible_by_fix_cohort": {
    "pre_fix": 542,
    "post_fix": 131
  }
}
```

## Overall

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| all eligible | 673 | 176 | 452 | 43 | 2 | 28.0% | -0.132 |

## Before vs after the price-basis fix

Split timestamp: **2026-08-22T18:17:59+00:00** (rows before it are `pre_fix`, rows on/after it are `post_fix`).
All loaded rows by cohort: **{'pre_fix': 549, 'post_fix': 160}**; eligible rows by cohort: **{'pre_fix': 542, 'post_fix': 131}**.

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| pre_fix | 542 | 161 | 339 | 40 | 2 | 32.2% | -0.009 |
| post_fix | 131 | 15 | 113 | 3 | 0 | 11.7% | -0.635 |

## By strategy

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| bb_squeeze | 673 | 176 | 452 | 43 | 2 | 28.0% | -0.132 |

## SHORT: opposite-direction signal within previous 60 minutes

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| no | 322 | 69 | 234 | 17 | 2 | 22.8% | -0.292 |
| yes — preliminary (<20) | 3 | 0 | 2 | 1 | 0 | 0.0% | -0.695 |

## SHORT: 24h range threshold 50.0%

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| below_50% | 325 | 69 | 236 | 18 | 2 | 22.6% | -0.296 |

## LONG: RSI at signal time

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|

## LONG: oversold RSI cohort

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|

## LONG: delay from detected consecutive hourly uptrend

| Group | n | TP first | SL first | Unresolved | Ambiguous | WR resolved | avg R |
|---|---:|---:|---:|---:|---:|---:|---:|
| no_consecutive_trend | 348 | 107 | 216 | 25 | 0 | 33.1% | 0.021 |

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
