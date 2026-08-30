# TP vs SL — strong-signal experimental analysis

**Read-only report. No production logic, filters, score, SL/TP, or SQLite rows were changed.**

- Scope: all valid target shadow `demo_positions` rows; loaded **1950**, with **1261** resolved.
- Runtime log matches: **1461** (74.92%).
- Historical 1h candle coverage: **421** of **421** symbols.
- Minimum comparison cohort: **20 TP-first and 20 SL-first**.
- `WR` is TP / (TP + SL); `avg R` uses recorded exit price and original entry-to-SL risk.
- Any rule below is in-sample: the threshold was selected and scored on the same rows.

## Feature provenance

The report keeps exact persisted signal-time fields separate from runtime-log observations and reconstructed historical proxies.

### Exact signal-time fields

| Field | Provenance | Coverage | Meaning |
|---|---|---:|---|
| SL distance from entry (%) (`risk_pct`) | exact_persisted_derived | 100.0% | abs(entry_price - sl_price) / entry_price |
| TP distance from entry (%) (`reward_pct`) | exact_persisted_derived | 100.0% | abs(tp_price - entry_price) / entry_price |
| TP/SL distance ratio (`reward_risk`) | exact_persisted_derived | 73.3% | reward_pct / risk_pct |
| Directional entry move from signal (%) (`entry_vs_signal_pct`) | exact_persisted_derived | 100.0% | direction-adjusted entry_price vs signal_price |
| RSI at signal (`rsi_at_signal`) | exact_persisted | 0.0% | RSI value persisted with the position at signal creation |

### Runtime-log observations

| Field | Provenance | Coverage | Meaning |
|---|---|---:|---|
| EMA cross gap (%) (`ema_gap_pct_log`) | runtime_log_rounded | 29.3% | EMA(9)-EMA(21) gap emitted by the signal path |
| Overheated 24h move (%) (`overheated_pct24_log`) | runtime_log_rounded | 12.4% | pct24 emitted by the overheated early signal path |
| Overheated RSI (`overheated_rsi_log`) | runtime_log_rounded | 12.4% | RSI emitted by the overheated early signal path |
| Confirmation volume ratio (x) (`confirmation_volume_ratio_log`) | runtime_log_rounded | 33.2% | completed-candle volume / 10-bar average |
| Confirmation number (`confirmation_number_log`) | runtime_log_exact_integer | 33.2% | confirmation count emitted by continuation telemetry |
| Confirmation age (minutes) (`confirmation_age_min_log`) | runtime_log_exact_integer | 33.2% | age of the parent signal at confirmation |

### Reconstructed historical proxies

| Field | Provenance | Coverage | Meaning |
|---|---|---:|---|
| Directional price change, 1h (%) (`price_return_1h_pct`) | reconstructed_historical_gateio_1h | 100.0% | direction-adjusted close-to-close return over the last completed 1h candle |
| Directional price change, 2h (%) (`price_return_2h_pct`) | reconstructed_historical_gateio_1h | 100.0% | direction-adjusted close-to-close return over the last two completed 1h candles |
| Directional price change, 4h (%) (`price_return_4h_pct`) | reconstructed_historical_gateio_1h | 100.0% | direction-adjusted close-to-close return over the last four completed 1h candles |
| Candle range, 1h (%) (`range_1h_pct`) | reconstructed_historical_gateio_1h | 100.0% | high-low range of the last completed 1h candle divided by its low |
| Window range, 2h (%) (`range_2h_pct`) | reconstructed_historical_gateio_1h | 100.0% | high-low range across the last two completed 1h candles |
| Window range, 4h (%) (`range_4h_pct`) | reconstructed_historical_gateio_1h | 100.0% | high-low range across the last four completed 1h candles |
| Distance above recent low, 24h (%) (`distance_to_recent_low_24h_pct`) | reconstructed_historical_gateio_1h | 100.0% | latest completed close above the low of the prior 24 completed 1h candles; raw price distance, not direction-adjusted |
| Distance below recent high, 24h (%) (`distance_to_recent_high_24h_pct`) | reconstructed_historical_gateio_1h | 100.0% | recent 24h high above the latest completed close, divided by that high; raw price distance, not direction-adjusted |
| Realized volatility, 2h (%) (`realized_vol_2h_pct`) | reconstructed_historical_gateio_1h | 100.0% | population standard deviation of completed 1h log returns in the 2h window |
| Realized volatility, 4h (%) (`realized_vol_4h_pct`) | reconstructed_historical_gateio_1h | 100.0% | population standard deviation of completed 1h log returns in the 4h window |
| Volume ratio, 1h vs prior 24h (`volume_ratio_1h_vs_24h`) | reconstructed_historical_gateio_1h | 100.0% | latest completed 1h volume divided by the mean of the preceding 24 completed 1h volumes |
| Volume change, last 1h (%) (`volume_change_1h_pct`) | reconstructed_historical_gateio_1h | 99.8% | latest completed 1h volume change versus the preceding completed 1h candle |
| Volume acceleration (%) (`volume_acceleration_pct`) | reconstructed_historical_gateio_1h | 99.8% | change in volume growth rate across the last three completed 1h candles |
| Momentum acceleration (%) (`momentum_acceleration_pct`) | reconstructed_historical_gateio_1h | 100.0% | directional 1h return minus the average directional return per hour over 2h |
| Momentum decay ratio (`momentum_decay_ratio`) | reconstructed_historical_gateio_1h | 99.4% | absolute directional 1h return divided by absolute directional 4h return per hour |

## Current strategy performance

| Strategy | Cohort | total | resolved | TP | SL | WR resolved | avg R | Status |
|---|---|---:|---:|---:|---:|---:|---:|---|
| ema_cross_confirmed | overall | 535 | 192 | 91 | 101 | 47.40% | 0.3965 | READY |
| ema_cross_confirmed | LONG | 276 | 128 | 78 | 50 | 60.94% | 0.8388 | READY |
| ema_cross_confirmed | SHORT | 259 | 64 | 13 | 51 | 20.31% | -0.4880 | INSUFFICIENT_TP_OR_SL |
| overheated_early | overall | 294 | 273 | 117 | 156 | 42.86% | 0.3578 | READY |
| overheated_early | LONG | 294 | 273 | 117 | 156 | 42.86% | 0.3578 | READY |
| overheated_early | SHORT | 0 | 0 | 0 | 0 | —% | — | INSUFFICIENT_TP_OR_SL |
| ema_cross | overall | 571 | 508 | 192 | 316 | 37.80% | 0.0986 | READY |
| ema_cross | LONG | 305 | 287 | 135 | 152 | 47.04% | 0.4100 | READY |
| ema_cross | SHORT | 266 | 221 | 57 | 164 | 25.79% | -0.3057 | READY |
| overheated_confirmed | overall | 550 | 288 | 117 | 171 | 40.62% | 0.2150 | READY |
| overheated_confirmed | LONG | 550 | 288 | 117 | 171 | 40.62% | 0.2150 | READY |
| overheated_confirmed | SHORT | 0 | 0 | 0 | 0 | —% | — | INSUFFICIENT_TP_OR_SL |

## Direction summary across all strategies

| Strategy | Direction | total | resolved | TP | SL | WR resolved | avg R | Status |
|---|---|---:|---:|---:|---:|---:|---:|---|
| ema_cross_confirmed | LONG | 276 | 128 | 78 | 50 | 60.94% | 0.8388 | READY |
| ema_cross_confirmed | SHORT | 259 | 64 | 13 | 51 | 20.31% | -0.4880 | INSUFFICIENT_TP_OR_SL |
| overheated_early | LONG | 294 | 273 | 117 | 156 | 42.86% | 0.3578 | READY |
| overheated_early | SHORT | 0 | 0 | 0 | 0 | —% | — | INSUFFICIENT_TP_OR_SL |
| ema_cross | LONG | 305 | 287 | 135 | 152 | 47.04% | 0.4100 | READY |
| ema_cross | SHORT | 266 | 221 | 57 | 164 | 25.79% | -0.3057 | READY |
| overheated_confirmed | LONG | 550 | 288 | 117 | 171 | 40.62% | 0.2150 | READY |
| overheated_confirmed | SHORT | 0 | 0 | 0 | 0 | —% | — | INSUFFICIENT_TP_OR_SL |

## Retrospective candidate volume and precision

| Strategy | Cohort | Candidate | Baseline/day | Selected/day | Selected signals | TP precision | TP recall | Selected WR |
|---|---|---|---:|---:|---:|---:|---:|---:|
| ema_cross_confirmed | overall | risk_pct | 38.21 | 24.71 | 346 | 0.781 | 0.626 | 78.08% |
| ema_cross_confirmed | LONG | risk_pct | 19.71 | 13.71 | 192 | 0.833 | 0.641 | 83.33% |
| ema_cross_confirmed | SHORT | NO CANDIDATE | 18.50 | 0.00 | 0 | — | — | —% |
| overheated_early | overall | NO CANDIDATE | 22.62 | 0.00 | 0 | — | — | —% |
| overheated_early | LONG | NO CANDIDATE | 22.62 | 0.00 | 0 | — | — | —% |
| overheated_early | SHORT | NO CANDIDATE | — | — | 0 | — | — | —% |
| ema_cross | overall | NO CANDIDATE | 31.72 | 0.00 | 0 | — | — | —% |
| ema_cross | LONG | NO CANDIDATE | 16.94 | 0.00 | 0 | — | — | —% |
| ema_cross | SHORT | NO CANDIDATE | 14.78 | 0.00 | 0 | — | — | —% |
| overheated_confirmed | overall | NO CANDIDATE | 36.67 | 0.00 | 0 | — | — | —% |
| overheated_confirmed | LONG | NO CANDIDATE | 36.67 | 0.00 | 0 | — | — | —% |
| overheated_confirmed | SHORT | NO CANDIDATE | — | — | 0 | — | — | —% |

## TP-first vs SL-first comparisons

### ema_cross_confirmed

#### overall

| Feature | TP mean (n) | SL mean (n) | TP−SL mean | 95% CI mean diff | TP median (n) | SL median (n) | Cliff’s δ | 95% CI δ | p |
|---|---:|---:|---:|---|---:|---:|---:|---|---:|
| SL distance from entry (%) [exact_persisted_derived] | 4.166 (91) | 7.208 (101) | -3.042 | [-4.271, -1.877] | 3.052 (91) | 5.869 (101) | -2.817 | -0.523 | [-0.656, -0.376] | 0.0012 |
| TP distance from entry (%) [exact_persisted_derived] | 8.182 (91) | 14.308 (101) | -6.126 | [-8.811, -3.889] | 5.856 (91) | 11.738 (101) | -5.882 | -0.514 | [-0.648, -0.365] | 0.0012 |
| TP/SL distance ratio [exact_persisted_derived] | 1.967 (91) | 1.970 (101) | -0.003 | [-0.052, 0.046] | 2.000 (91) | 2.000 (101) | 0.000 | 0.035 | [-0.051, 0.125] | 0.4919 |
| Directional entry move from signal (%) [exact_persisted_derived] | 0.904 (91) | 0.763 (101) | 0.140 | [-0.655, 1.122] | 0.000 (91) | 0.000 (101) | 0.000 | -0.209 | [-0.336, -0.073] | 0.0037 |
| RSI at signal [exact_persisted] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| EMA cross gap (%) [runtime_log_rounded] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated 24h move (%) [runtime_log_rounded] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated RSI [runtime_log_rounded] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation volume ratio (x) [runtime_log_rounded] | 3.160 (91) | 2.854 (101) | 0.306 | [-0.139, 0.756] | 2.700 (91) | 2.100 (101) | 0.600 | 0.189 | [0.023, 0.350] | 0.0350 |
| Confirmation number [runtime_log_exact_integer] | 1.066 (91) | 1.059 (101) | 0.007 | [-0.086, 0.103] | 1.000 (91) | 1.000 (101) | 0.000 | 0.014 | [-0.038, 0.077] | 0.6966 |
| Confirmation age (minutes) [runtime_log_exact_integer] | 62.648 (91) | 82.248 (101) | -19.599 | [-39.821, 2.149] | 55.000 (91) | 62.000 (101) | -7.000 | -0.151 | [-0.323, 0.015] | 0.0537 |
| Directional price change, 1h (%) [reconstructed_historical_gateio_1h] | 0.000 (91) | 0.000 (101) | 0.000 | [0.000, 0.000] | 0.000 (91) | -0.000 (101) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| Directional price change, 2h (%) [reconstructed_historical_gateio_1h] | 0.724 (91) | 1.060 (101) | -0.337 | [-0.919, 0.142] | 0.429 (91) | 0.570 (101) | -0.141 | -0.061 | [-0.229, 0.100] | 0.4632 |
| Directional price change, 4h (%) [reconstructed_historical_gateio_1h] | 1.286 (91) | 1.597 (101) | -0.311 | [-1.255, 0.658] | 0.725 (91) | 1.021 (101) | -0.296 | -0.069 | [-0.225, 0.104] | 0.4232 |
| Candle range, 1h (%) [reconstructed_historical_gateio_1h] | 2.088 (91) | 2.547 (101) | -0.459 | [-1.536, 0.360] | 1.370 (91) | 1.702 (101) | -0.332 | -0.064 | [-0.233, 0.105] | 0.4669 |
| Window range, 2h (%) [reconstructed_historical_gateio_1h] | 3.120 (91) | 3.363 (101) | -0.243 | [-1.411, 0.862] | 1.984 (91) | 2.142 (101) | -0.158 | -0.026 | [-0.184, 0.128] | 0.7441 |
| Window range, 4h (%) [reconstructed_historical_gateio_1h] | 4.979 (91) | 6.131 (101) | -1.151 | [-2.986, 0.867] | 3.263 (91) | 3.105 (101) | 0.158 | -0.059 | [-0.228, 0.101] | 0.4894 |
| Distance above recent low, 24h (%) [reconstructed_historical_gateio_1h] | 9.080 (91) | 9.031 (101) | 0.049 | [-2.377, 2.457] | 7.434 (91) | 6.048 (101) | 1.386 | 0.110 | [-0.057, 0.285] | 0.2035 |
| Distance below recent high, 24h (%) [reconstructed_historical_gateio_1h] | 2.931 (91) | 7.200 (101) | -4.268 | [-5.808, -2.675] | 1.558 (91) | 5.151 (101) | -3.593 | -0.397 | [-0.542, -0.245] | 0.0012 |
| Realized volatility, 2h (%) [reconstructed_historical_gateio_1h] | 0.000 (91) | 0.000 (101) | 0.000 | [0.000, 0.000] | 0.000 (91) | 0.000 (101) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| Realized volatility, 4h (%) [reconstructed_historical_gateio_1h] | 1.212 (91) | 1.259 (101) | -0.047 | [-0.446, 0.397] | 0.658 (91) | 0.787 (101) | -0.130 | -0.079 | [-0.251, 0.077] | 0.3571 |
| Volume ratio, 1h vs prior 24h [reconstructed_historical_gateio_1h] | 1.347 (91) | 2.138 (101) | -0.791 | [-2.944, 0.602] | 0.740 (91) | 0.715 (101) | 0.026 | 0.079 | [-0.089, 0.251] | 0.3546 |
| Volume change, last 1h (%) [reconstructed_historical_gateio_1h] | 100.191 (91) | 251.638 (100) | -151.447 | [-633.745, 96.391] | 11.924 (91) | 1.659 (100) | 10.265 | 0.003 | [-0.159, 0.165] | 0.9713 |
| Volume acceleration (%) [reconstructed_historical_gateio_1h] | -329.593 (91) | 201.373 (100) | -530.965 | [-1505.011, 104.823] | 38.022 (91) | 24.724 (100) | 13.298 | -0.009 | [-0.179, 0.154] | 0.9189 |
| Momentum acceleration (%) [reconstructed_historical_gateio_1h] | -0.362 (91) | -0.530 (101) | 0.168 | [-0.095, 0.453] | -0.215 (91) | -0.285 (101) | 0.071 | 0.061 | [-0.106, 0.235] | 0.4769 |
| Momentum decay ratio [reconstructed_historical_gateio_1h] | 0.000 (90) | 0.000 (100) | 0.000 | [0.000, 0.000] | 0.000 (90) | 0.000 (100) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |

#### Experimental candidate

- **SL distance from entry (%) ≤ 3.56717 predicts TP**
- Provenance: **exact_persisted_derived**
- In-sample accuracy: **0.739583**, balanced accuracy: **0.733979**
- Precision TP: **0.780822**; precision SL: **0.714286**
- TP recall: **0.626374**; SL recall: **0.841584**
- Retrospective selected volume: **346** signals, **24.7143**/day.
- This is experimental and requires forward-shadow validation; it is not a production rule.

#### LONG

| Feature | TP mean (n) | SL mean (n) | TP−SL mean | 95% CI mean diff | TP median (n) | SL median (n) | Cliff’s δ | 95% CI δ | p |
|---|---:|---:|---:|---|---:|---:|---:|---|---:|
| SL distance from entry (%) [exact_persisted_derived] | 4.005 (78) | 4.827 (50) | -0.822 | [-1.783, 0.236] | 2.921 (78) | 4.159 (50) | -1.239 | -0.373 | [-0.552, -0.180] | 0.0012 |
| TP distance from entry (%) [exact_persisted_derived] | 7.835 (78) | 9.437 (50) | -1.602 | [-3.345, 0.511] | 5.696 (78) | 8.064 (50) | -2.367 | -0.347 | [-0.511, -0.170] | 0.0025 |
| TP/SL distance ratio [exact_persisted_derived] | 1.962 (78) | 1.940 (50) | 0.022 | [-0.050, 0.101] | 2.000 (78) | 2.000 (50) | 0.000 | 0.079 | [-0.049, 0.212] | 0.2347 |
| Directional entry move from signal (%) [exact_persisted_derived] | 1.047 (78) | 0.902 (50) | 0.146 | [-0.785, 1.245] | 0.000 (78) | 0.000 (50) | 0.000 | -0.280 | [-0.433, -0.115] | 0.0012 |
| RSI at signal [exact_persisted] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| EMA cross gap (%) [runtime_log_rounded] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated 24h move (%) [runtime_log_rounded] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated RSI [runtime_log_rounded] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation volume ratio (x) [runtime_log_rounded] | 3.214 (78) | 2.724 (50) | 0.490 | [-0.075, 0.986] | 2.700 (78) | 2.100 (50) | 0.600 | 0.249 | [0.049, 0.459] | 0.0150 |
| Confirmation number [runtime_log_exact_integer] | 1.077 (78) | 1.120 (50) | -0.043 | [-0.187, 0.114] | 1.000 (78) | 1.000 (50) | 0.000 | -0.010 | [-0.088, 0.068] | 0.7840 |
| Confirmation age (minutes) [runtime_log_exact_integer] | 61.744 (78) | 100.720 (50) | -38.976 | [-64.330, -11.436] | 56.000 (78) | 108.000 (50) | -52.000 | -0.300 | [-0.494, -0.106] | 0.0100 |
| Directional price change, 1h (%) [reconstructed_historical_gateio_1h] | 0.000 (78) | 0.000 (50) | 0.000 | [0.000, 0.000] | 0.000 (78) | 0.000 (50) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| Directional price change, 2h (%) [reconstructed_historical_gateio_1h] | 0.832 (78) | 1.077 (50) | -0.245 | [-0.948, 0.384] | 0.527 (78) | 0.646 (50) | -0.119 | -0.061 | [-0.254, 0.168] | 0.5755 |
| Directional price change, 4h (%) [reconstructed_historical_gateio_1h] | 1.308 (78) | 1.479 (50) | -0.171 | [-1.397, 1.040] | 0.667 (78) | 1.032 (50) | -0.365 | -0.046 | [-0.252, 0.158] | 0.6929 |
| Candle range, 1h (%) [reconstructed_historical_gateio_1h] | 2.195 (78) | 2.646 (50) | -0.452 | [-1.322, 0.559] | 1.384 (78) | 1.864 (50) | -0.479 | -0.179 | [-0.375, 0.026] | 0.0811 |
| Window range, 2h (%) [reconstructed_historical_gateio_1h] | 3.303 (78) | 3.628 (50) | -0.325 | [-1.618, 0.962] | 1.992 (78) | 2.628 (50) | -0.637 | -0.136 | [-0.336, 0.071] | 0.2185 |
| Window range, 4h (%) [reconstructed_historical_gateio_1h] | 5.132 (78) | 6.734 (50) | -1.602 | [-4.134, 0.916] | 3.322 (78) | 3.899 (50) | -0.577 | -0.113 | [-0.320, 0.099] | 0.2722 |
| Distance above recent low, 24h (%) [reconstructed_historical_gateio_1h] | 9.781 (78) | 12.550 (50) | -2.769 | [-5.637, 0.038] | 7.836 (78) | 10.370 (50) | -2.535 | -0.190 | [-0.395, 0.018] | 0.0649 |
| Distance below recent high, 24h (%) [reconstructed_historical_gateio_1h] | 2.090 (78) | 2.616 (50) | -0.527 | [-1.690, 0.494] | 1.038 (78) | 1.581 (50) | -0.543 | -0.082 | [-0.268, 0.129] | 0.4382 |
| Realized volatility, 2h (%) [reconstructed_historical_gateio_1h] | 0.000 (78) | 0.000 (50) | 0.000 | [0.000, 0.000] | 0.000 (78) | 0.000 (50) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| Realized volatility, 4h (%) [reconstructed_historical_gateio_1h] | 1.250 (78) | 1.349 (50) | -0.099 | [-0.615, 0.383] | 0.657 (78) | 0.811 (50) | -0.154 | -0.107 | [-0.322, 0.089] | 0.3233 |
| Volume ratio, 1h vs prior 24h [reconstructed_historical_gateio_1h] | 1.466 (78) | 1.483 (50) | -0.016 | [-0.638, 0.696] | 0.840 (78) | 1.073 (50) | -0.233 | -0.062 | [-0.263, 0.160] | 0.5356 |
| Volume change, last 1h (%) [reconstructed_historical_gateio_1h] | 117.217 (78) | 78.062 (49) | 39.155 | [-49.827, 142.766] | 30.856 (78) | 18.774 (49) | 12.082 | 0.001 | [-0.200, 0.219] | 0.9913 |
| Volume acceleration (%) [reconstructed_historical_gateio_1h] | 62.196 (78) | -17.485 (49) | 79.681 | [-127.901, 321.323] | 38.955 (78) | 36.661 (49) | 2.294 | 0.013 | [-0.192, 0.211] | 0.9251 |
| Momentum acceleration (%) [reconstructed_historical_gateio_1h] | -0.416 (78) | -0.539 (50) | 0.123 | [-0.199, 0.497] | -0.263 (78) | -0.323 (50) | 0.060 | 0.061 | [-0.162, 0.263] | 0.5718 |
| Momentum decay ratio [reconstructed_historical_gateio_1h] | 0.000 (78) | 0.000 (49) | 0.000 | [0.000, 0.000] | 0.000 (78) | 0.000 (49) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |

#### Experimental candidate

- **SL distance from entry (%) ≤ 3.55255 predicts TP**
- Provenance: **exact_persisted_derived**
- In-sample accuracy: **0.703125**, balanced accuracy: **0.720513**
- Precision TP: **0.833333**; precision SL: **0.588235**
- TP recall: **0.641026**; SL recall: **0.8**
- Retrospective selected volume: **192** signals, **13.7143**/day.
- This is experimental and requires forward-shadow validation; it is not a production rule.

#### SHORT

**INSUFFICIENT_TP_OR_SL:** Requires TP>= 20 and SL>= 20; observed TP=13, SL=51. No feature conclusion or candidate is allowed.

### overheated_early

#### overall

| Feature | TP mean (n) | SL mean (n) | TP−SL mean | 95% CI mean diff | TP median (n) | SL median (n) | Cliff’s δ | 95% CI δ | p |
|---|---:|---:|---:|---|---:|---:|---:|---|---:|
| SL distance from entry (%) [exact_persisted_derived] | 6.912 (117) | 7.060 (156) | -0.148 | [-1.146, 0.900] | 5.605 (117) | 5.967 (156) | -0.362 | -0.099 | [-0.236, 0.046] | 0.1511 |
| TP distance from entry (%) [exact_persisted_derived] | 13.824 (117) | 14.120 (156) | -0.295 | [-2.398, 1.850] | 11.210 (117) | 11.934 (156) | -0.724 | -0.099 | [-0.244, 0.031] | 0.1610 |
| TP/SL distance ratio [exact_persisted_derived] | 2.000 (117) | 2.000 (156) | 0.000 | [0.000, 0.000] | 2.000 (117) | 2.000 (156) | 0.000 | 0.083 | [-0.037, 0.212] | 0.1760 |
| Directional entry move from signal (%) [exact_persisted_derived] | 0.000 (117) | 0.000 (156) | 0.000 | [0.000, 0.000] | 0.000 (117) | 0.000 (156) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| RSI at signal [exact_persisted] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| EMA cross gap (%) [runtime_log_rounded] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated 24h move (%) [runtime_log_rounded] | 22.421 (85) | 22.874 (136) | -0.452 | [-3.644, 2.864] | 18.100 (85) | 18.400 (136) | -0.300 | -0.006 | [-0.171, 0.151] | 0.9438 |
| Overheated RSI [runtime_log_rounded] | 64.558 (85) | 64.863 (136) | -0.306 | [-1.403, 0.775] | 66.100 (85) | 65.850 (136) | 0.250 | -0.067 | [-0.219, 0.100] | 0.3795 |
| Confirmation volume ratio (x) [runtime_log_rounded] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation number [runtime_log_exact_integer] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation age (minutes) [runtime_log_exact_integer] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| Directional price change, 1h (%) [reconstructed_historical_gateio_1h] | 0.000 (117) | 0.000 (156) | 0.000 | [0.000, 0.000] | 0.000 (117) | 0.000 (156) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| Directional price change, 2h (%) [reconstructed_historical_gateio_1h] | 0.179 (117) | 0.046 (156) | 0.132 | [-1.128, 1.202] | -0.142 (117) | -0.081 (156) | -0.061 | 0.049 | [-0.085, 0.185] | 0.4906 |
| Directional price change, 4h (%) [reconstructed_historical_gateio_1h] | 2.034 (117) | 2.274 (156) | -0.240 | [-2.072, 1.551] | 1.127 (117) | 1.299 (156) | -0.172 | 0.016 | [-0.116, 0.161] | 0.8265 |
| Candle range, 1h (%) [reconstructed_historical_gateio_1h] | 5.890 (117) | 8.069 (156) | -2.179 | [-3.830, -0.428] | 3.385 (117) | 4.866 (156) | -1.480 | -0.206 | [-0.343, -0.065] | 0.0025 |
| Window range, 2h (%) [reconstructed_historical_gateio_1h] | 8.798 (117) | 11.191 (156) | -2.393 | [-4.807, -0.013] | 5.286 (117) | 7.646 (156) | -2.360 | -0.187 | [-0.324, -0.047] | 0.0150 |
| Window range, 4h (%) [reconstructed_historical_gateio_1h] | 11.818 (117) | 15.869 (156) | -4.051 | [-6.993, -1.170] | 8.285 (117) | 11.530 (156) | -3.245 | -0.200 | [-0.334, -0.057] | 0.0100 |
| Distance above recent low, 24h (%) [reconstructed_historical_gateio_1h] | 25.070 (117) | 27.094 (156) | -2.024 | [-5.481, 1.464] | 20.436 (117) | 23.175 (156) | -2.738 | -0.109 | [-0.255, 0.033] | 0.1423 |
| Distance below recent high, 24h (%) [reconstructed_historical_gateio_1h] | 5.952 (117) | 6.875 (156) | -0.923 | [-2.407, 0.594] | 4.031 (117) | 5.956 (156) | -1.925 | -0.132 | [-0.273, 0.007] | 0.0637 |
| Realized volatility, 2h (%) [reconstructed_historical_gateio_1h] | 0.000 (117) | 0.000 (156) | 0.000 | [0.000, 0.000] | 0.000 (117) | 0.000 (156) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| Realized volatility, 4h (%) [reconstructed_historical_gateio_1h] | 3.015 (117) | 4.169 (156) | -1.155 | [-1.990, -0.348] | 2.167 (117) | 2.582 (156) | -0.414 | -0.137 | [-0.275, 0.003] | 0.0549 |
| Volume ratio, 1h vs prior 24h [reconstructed_historical_gateio_1h] | 1.672 (117) | 3.000 (156) | -1.327 | [-2.171, -0.548] | 0.990 (117) | 1.377 (156) | -0.387 | -0.185 | [-0.322, -0.062] | 0.0125 |
| Volume change, last 1h (%) [reconstructed_historical_gateio_1h] | 25.505 (117) | 126.987 (156) | -101.482 | [-231.981, -18.497] | -12.919 (117) | -5.836 (156) | -7.082 | -0.056 | [-0.195, 0.078] | 0.4282 |
| Volume acceleration (%) [reconstructed_historical_gateio_1h] | -53.201 (117) | -216.512 (156) | 163.310 | [-83.527, 477.786] | -26.996 (117) | -2.143 (156) | -24.853 | -0.076 | [-0.206, 0.052] | 0.2759 |
| Momentum acceleration (%) [reconstructed_historical_gateio_1h] | -0.089 (117) | -0.023 (156) | -0.066 | [-0.680, 0.578] | 0.071 (117) | 0.041 (156) | 0.030 | -0.049 | [-0.194, 0.097] | 0.4919 |
| Momentum decay ratio [reconstructed_historical_gateio_1h] | 0.000 (115) | 0.000 (155) | 0.000 | [0.000, 0.000] | 0.000 (115) | 0.000 (155) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |

#### Experimental candidate

**NO CANDIDATE — EFFECT CRITERIA:** TP/SL cohorts were sufficient, but no feature met the predeclared effect, permutation, confidence-interval, coverage, and balanced-accuracy criteria.

#### LONG

| Feature | TP mean (n) | SL mean (n) | TP−SL mean | 95% CI mean diff | TP median (n) | SL median (n) | Cliff’s δ | 95% CI δ | p |
|---|---:|---:|---:|---|---:|---:|---:|---|---:|
| SL distance from entry (%) [exact_persisted_derived] | 6.912 (117) | 7.060 (156) | -0.148 | [-1.215, 0.951] | 5.605 (117) | 5.967 (156) | -0.362 | -0.099 | [-0.234, 0.033] | 0.1798 |
| TP distance from entry (%) [exact_persisted_derived] | 13.824 (117) | 14.120 (156) | -0.295 | [-2.338, 1.785] | 11.210 (117) | 11.934 (156) | -0.724 | -0.099 | [-0.237, 0.043] | 0.1823 |
| TP/SL distance ratio [exact_persisted_derived] | 2.000 (117) | 2.000 (156) | 0.000 | [0.000, 0.000] | 2.000 (117) | 2.000 (156) | 0.000 | 0.083 | [-0.043, 0.202] | 0.2135 |
| Directional entry move from signal (%) [exact_persisted_derived] | 0.000 (117) | 0.000 (156) | 0.000 | [0.000, 0.000] | 0.000 (117) | 0.000 (156) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| RSI at signal [exact_persisted] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| EMA cross gap (%) [runtime_log_rounded] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated 24h move (%) [runtime_log_rounded] | 22.421 (85) | 22.874 (136) | -0.452 | [-3.655, 2.614] | 18.100 (85) | 18.400 (136) | -0.300 | -0.006 | [-0.158, 0.164] | 0.9426 |
| Overheated RSI [runtime_log_rounded] | 64.558 (85) | 64.863 (136) | -0.306 | [-1.442, 0.842] | 66.100 (85) | 65.850 (136) | 0.250 | -0.067 | [-0.223, 0.085] | 0.4444 |
| Confirmation volume ratio (x) [runtime_log_rounded] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation number [runtime_log_exact_integer] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation age (minutes) [runtime_log_exact_integer] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| Directional price change, 1h (%) [reconstructed_historical_gateio_1h] | 0.000 (117) | 0.000 (156) | 0.000 | [0.000, 0.000] | 0.000 (117) | 0.000 (156) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| Directional price change, 2h (%) [reconstructed_historical_gateio_1h] | 0.179 (117) | 0.046 (156) | 0.132 | [-0.926, 1.309] | -0.142 (117) | -0.081 (156) | -0.061 | 0.049 | [-0.090, 0.186] | 0.4919 |
| Directional price change, 4h (%) [reconstructed_historical_gateio_1h] | 2.034 (117) | 2.274 (156) | -0.240 | [-1.946, 1.652] | 1.127 (117) | 1.299 (156) | -0.172 | 0.016 | [-0.127, 0.163] | 0.8277 |
| Candle range, 1h (%) [reconstructed_historical_gateio_1h] | 5.890 (117) | 8.069 (156) | -2.179 | [-4.056, -0.404] | 3.385 (117) | 4.866 (156) | -1.480 | -0.206 | [-0.333, -0.070] | 0.0075 |
| Window range, 2h (%) [reconstructed_historical_gateio_1h] | 8.798 (117) | 11.191 (156) | -2.393 | [-4.751, 0.096] | 5.286 (117) | 7.646 (156) | -2.360 | -0.187 | [-0.328, -0.050] | 0.0062 |
| Window range, 4h (%) [reconstructed_historical_gateio_1h] | 11.818 (117) | 15.869 (156) | -4.051 | [-6.890, -1.079] | 8.285 (117) | 11.530 (156) | -3.245 | -0.200 | [-0.329, -0.061] | 0.0037 |
| Distance above recent low, 24h (%) [reconstructed_historical_gateio_1h] | 25.070 (117) | 27.094 (156) | -2.024 | [-5.361, 1.728] | 20.436 (117) | 23.175 (156) | -2.738 | -0.109 | [-0.255, 0.040] | 0.1261 |
| Distance below recent high, 24h (%) [reconstructed_historical_gateio_1h] | 5.952 (117) | 6.875 (156) | -0.923 | [-2.245, 0.361] | 4.031 (117) | 5.956 (156) | -1.925 | -0.132 | [-0.266, 0.009] | 0.0674 |
| Realized volatility, 2h (%) [reconstructed_historical_gateio_1h] | 0.000 (117) | 0.000 (156) | 0.000 | [0.000, 0.000] | 0.000 (117) | 0.000 (156) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| Realized volatility, 4h (%) [reconstructed_historical_gateio_1h] | 3.015 (117) | 4.169 (156) | -1.155 | [-2.069, -0.309] | 2.167 (117) | 2.582 (156) | -0.414 | -0.137 | [-0.271, 0.007] | 0.0537 |
| Volume ratio, 1h vs prior 24h [reconstructed_historical_gateio_1h] | 1.672 (117) | 3.000 (156) | -1.327 | [-2.161, -0.511] | 0.990 (117) | 1.377 (156) | -0.387 | -0.185 | [-0.310, -0.042] | 0.0125 |
| Volume change, last 1h (%) [reconstructed_historical_gateio_1h] | 25.505 (117) | 126.987 (156) | -101.482 | [-221.192, -9.932] | -12.919 (117) | -5.836 (156) | -7.082 | -0.056 | [-0.202, 0.086] | 0.4257 |
| Volume acceleration (%) [reconstructed_historical_gateio_1h] | -53.201 (117) | -216.512 (156) | 163.310 | [-96.204, 489.680] | -26.996 (117) | -2.143 (156) | -24.853 | -0.076 | [-0.202, 0.067] | 0.2959 |
| Momentum acceleration (%) [reconstructed_historical_gateio_1h] | -0.089 (117) | -0.023 (156) | -0.066 | [-0.708, 0.536] | 0.071 (117) | 0.041 (156) | 0.030 | -0.049 | [-0.188, 0.091] | 0.5044 |
| Momentum decay ratio [reconstructed_historical_gateio_1h] | 0.000 (115) | 0.000 (155) | 0.000 | [0.000, 0.000] | 0.000 (115) | 0.000 (155) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |

#### Experimental candidate

**NO CANDIDATE — EFFECT CRITERIA:** TP/SL cohorts were sufficient, but no feature met the predeclared effect, permutation, confidence-interval, coverage, and balanced-accuracy criteria.

#### SHORT

**INSUFFICIENT_TP_OR_SL:** Requires TP>= 20 and SL>= 20; observed TP=0, SL=0. No feature conclusion or candidate is allowed.

### ema_cross

#### overall

| Feature | TP mean (n) | SL mean (n) | TP−SL mean | 95% CI mean diff | TP median (n) | SL median (n) | Cliff’s δ | 95% CI δ | p |
|---|---:|---:|---:|---|---:|---:|---:|---|---:|
| SL distance from entry (%) [exact_persisted_derived] | 3.563 (192) | 4.123 (316) | -0.561 | [-1.011, -0.071] | 2.887 (192) | 3.515 (316) | -0.627 | -0.219 | [-0.330, -0.107] | 0.0012 |
| TP distance from entry (%) [exact_persisted_derived] | 7.125 (192) | 8.247 (316) | -1.122 | [-1.991, -0.124] | 5.775 (192) | 7.029 (316) | -1.254 | -0.219 | [-0.324, -0.117] | 0.0012 |
| TP/SL distance ratio [exact_persisted_derived] | 2.000 (192) | 2.000 (316) | -0.000 | [-0.000, 0.000] | 2.000 (192) | 2.000 (316) | 0.000 | -0.069 | [-0.176, 0.032] | 0.1835 |
| Directional entry move from signal (%) [exact_persisted_derived] | 0.000 (192) | 0.000 (316) | 0.000 | [0.000, 0.000] | 0.000 (192) | 0.000 (316) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| RSI at signal [exact_persisted] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| EMA cross gap (%) [runtime_log_rounded] | 0.423 (192) | 0.433 (316) | -0.010 | [-0.076, 0.067] | 0.286 (192) | 0.320 (316) | -0.034 | -0.062 | [-0.155, 0.043] | 0.2497 |
| Overheated 24h move (%) [runtime_log_rounded] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated RSI [runtime_log_rounded] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation volume ratio (x) [runtime_log_rounded] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation number [runtime_log_exact_integer] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation age (minutes) [runtime_log_exact_integer] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| Directional price change, 1h (%) [reconstructed_historical_gateio_1h] | 0.000 (192) | 0.000 (316) | 0.000 | [0.000, 0.000] | 0.000 (192) | -0.000 (316) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| Directional price change, 2h (%) [reconstructed_historical_gateio_1h] | 0.590 (192) | 0.555 (316) | 0.035 | [-0.389, 0.567] | 0.251 (192) | 0.359 (316) | -0.107 | -0.030 | [-0.134, 0.071] | 0.5368 |
| Directional price change, 4h (%) [reconstructed_historical_gateio_1h] | 1.538 (192) | 2.055 (316) | -0.517 | [-1.185, 0.257] | 1.051 (192) | 1.362 (316) | -0.311 | -0.090 | [-0.192, 0.019] | 0.0562 |
| Candle range, 1h (%) [reconstructed_historical_gateio_1h] | 2.776 (192) | 3.024 (316) | -0.248 | [-1.060, 0.638] | 1.790 (192) | 1.779 (316) | 0.011 | -0.045 | [-0.149, 0.047] | 0.4270 |
| Window range, 2h (%) [reconstructed_historical_gateio_1h] | 4.081 (192) | 4.888 (316) | -0.807 | [-1.925, 0.358] | 2.419 (192) | 2.792 (316) | -0.373 | -0.107 | [-0.208, -0.000] | 0.0362 |
| Window range, 4h (%) [reconstructed_historical_gateio_1h] | 6.506 (192) | 7.223 (316) | -0.717 | [-2.218, 0.686] | 3.942 (192) | 4.267 (316) | -0.326 | -0.061 | [-0.161, 0.047] | 0.2372 |
| Distance above recent low, 24h (%) [reconstructed_historical_gateio_1h] | 8.958 (192) | 8.214 (316) | 0.744 | [-0.767, 2.350] | 7.112 (192) | 5.531 (316) | 1.582 | 0.140 | [0.034, 0.249] | 0.0050 |
| Distance below recent high, 24h (%) [reconstructed_historical_gateio_1h] | 5.264 (192) | 7.409 (316) | -2.145 | [-3.293, -0.939] | 2.861 (192) | 5.809 (316) | -2.948 | -0.242 | [-0.340, -0.146] | 0.0012 |
| Realized volatility, 2h (%) [reconstructed_historical_gateio_1h] | 0.000 (192) | 0.000 (316) | 0.000 | [0.000, 0.000] | 0.000 (192) | 0.000 (316) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| Realized volatility, 4h (%) [reconstructed_historical_gateio_1h] | 1.627 (192) | 1.674 (316) | -0.047 | [-0.394, 0.329] | 0.961 (192) | 1.050 (316) | -0.089 | -0.050 | [-0.152, 0.063] | 0.3558 |
| Volume ratio, 1h vs prior 24h [reconstructed_historical_gateio_1h] | 2.438 (192) | 2.670 (316) | -0.232 | [-2.264, 1.505] | 0.991 (192) | 0.928 (316) | 0.064 | 0.015 | [-0.097, 0.114] | 0.7478 |
| Volume change, last 1h (%) [reconstructed_historical_gateio_1h] | 461.839 (191) | 489.628 (316) | -27.789 | [-1143.281, 761.397] | -1.533 (191) | -13.852 (316) | 12.319 | 0.067 | [-0.032, 0.174] | 0.2285 |
| Volume acceleration (%) [reconstructed_historical_gateio_1h] | 333.138 (191) | 193.568 (316) | 139.570 | [-950.294, 978.210] | 3.595 (191) | -13.932 (316) | 17.527 | 0.068 | [-0.043, 0.178] | 0.1885 |
| Momentum acceleration (%) [reconstructed_historical_gateio_1h] | -0.295 (192) | -0.278 (316) | -0.018 | [-0.257, 0.230] | -0.126 (192) | -0.179 (316) | 0.054 | 0.030 | [-0.076, 0.136] | 0.5493 |
| Momentum decay ratio [reconstructed_historical_gateio_1h] | 0.000 (190) | 0.000 (315) | 0.000 | [0.000, 0.000] | 0.000 (190) | 0.000 (315) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |

#### Experimental candidate

**NO CANDIDATE — EFFECT CRITERIA:** TP/SL cohorts were sufficient, but no feature met the predeclared effect, permutation, confidence-interval, coverage, and balanced-accuracy criteria.

#### LONG

| Feature | TP mean (n) | SL mean (n) | TP−SL mean | 95% CI mean diff | TP median (n) | SL median (n) | Cliff’s δ | 95% CI δ | p |
|---|---:|---:|---:|---|---:|---:|---:|---|---:|
| SL distance from entry (%) [exact_persisted_derived] | 3.238 (135) | 3.627 (152) | -0.389 | [-0.891, 0.201] | 2.416 (135) | 3.140 (152) | -0.724 | -0.221 | [-0.339, -0.100] | 0.0012 |
| TP distance from entry (%) [exact_persisted_derived] | 6.477 (135) | 7.254 (152) | -0.777 | [-1.880, 0.354] | 4.831 (135) | 6.279 (152) | -1.448 | -0.221 | [-0.349, -0.091] | 0.0025 |
| TP/SL distance ratio [exact_persisted_derived] | 2.000 (135) | 2.000 (152) | -0.000 | [-0.000, 0.000] | 2.000 (135) | 2.000 (152) | 0.000 | -0.042 | [-0.169, 0.079] | 0.5069 |
| Directional entry move from signal (%) [exact_persisted_derived] | 0.000 (135) | 0.000 (152) | 0.000 | [0.000, 0.000] | 0.000 (135) | 0.000 (152) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| RSI at signal [exact_persisted] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| EMA cross gap (%) [runtime_log_rounded] | 0.371 (135) | 0.475 (152) | -0.103 | [-0.177, -0.032] | 0.259 (135) | 0.366 (152) | -0.107 | -0.210 | [-0.339, -0.082] | 0.0037 |
| Overheated 24h move (%) [runtime_log_rounded] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated RSI [runtime_log_rounded] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation volume ratio (x) [runtime_log_rounded] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation number [runtime_log_exact_integer] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation age (minutes) [runtime_log_exact_integer] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| Directional price change, 1h (%) [reconstructed_historical_gateio_1h] | 0.000 (135) | 0.000 (152) | 0.000 | [0.000, 0.000] | 0.000 (135) | 0.000 (152) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| Directional price change, 2h (%) [reconstructed_historical_gateio_1h] | 0.412 (135) | 0.467 (152) | -0.055 | [-0.690, 0.544] | 0.212 (135) | 0.303 (152) | -0.091 | 0.009 | [-0.128, 0.142] | 0.9064 |
| Directional price change, 4h (%) [reconstructed_historical_gateio_1h] | 1.190 (135) | 2.349 (152) | -1.159 | [-2.234, -0.135] | 0.990 (135) | 1.327 (152) | -0.337 | -0.118 | [-0.249, 0.023] | 0.0737 |
| Candle range, 1h (%) [reconstructed_historical_gateio_1h] | 2.483 (135) | 4.236 (152) | -1.752 | [-2.876, -0.763] | 1.827 (135) | 2.327 (152) | -0.500 | -0.211 | [-0.336, -0.080] | 0.0062 |
| Window range, 2h (%) [reconstructed_historical_gateio_1h] | 3.878 (135) | 6.924 (152) | -3.046 | [-4.741, -1.498] | 2.506 (135) | 3.594 (152) | -1.087 | -0.262 | [-0.386, -0.129] | 0.0012 |
| Window range, 4h (%) [reconstructed_historical_gateio_1h] | 5.949 (135) | 9.670 (152) | -3.721 | [-5.763, -1.789] | 3.923 (135) | 5.845 (152) | -1.921 | -0.227 | [-0.363, -0.101] | 0.0037 |
| Distance above recent low, 24h (%) [reconstructed_historical_gateio_1h] | 10.181 (135) | 12.724 (152) | -2.544 | [-4.480, -0.557] | 8.088 (135) | 9.756 (152) | -1.668 | -0.167 | [-0.296, -0.031] | 0.0112 |
| Distance below recent high, 24h (%) [reconstructed_historical_gateio_1h] | 2.586 (135) | 3.403 (152) | -0.818 | [-1.695, -0.023] | 1.628 (135) | 1.933 (152) | -0.305 | -0.139 | [-0.269, -0.008] | 0.0449 |
| Realized volatility, 2h (%) [reconstructed_historical_gateio_1h] | 0.000 (135) | 0.000 (152) | 0.000 | [0.000, 0.000] | 0.000 (135) | 0.000 (152) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| Realized volatility, 4h (%) [reconstructed_historical_gateio_1h] | 1.478 (135) | 2.217 (152) | -0.739 | [-1.198, -0.208] | 0.988 (135) | 1.340 (152) | -0.352 | -0.193 | [-0.316, -0.052] | 0.0062 |
| Volume ratio, 1h vs prior 24h [reconstructed_historical_gateio_1h] | 1.714 (135) | 4.374 (152) | -2.660 | [-5.808, -0.726] | 1.094 (135) | 1.404 (152) | -0.310 | -0.223 | [-0.363, -0.093] | 0.0025 |
| Volume change, last 1h (%) [reconstructed_historical_gateio_1h] | 106.448 (134) | 981.991 (152) | -875.543 | [-2644.940, 60.122] | -9.232 (134) | -10.676 (152) | 1.444 | -0.002 | [-0.134, 0.132] | 0.9763 |
| Volume acceleration (%) [reconstructed_historical_gateio_1h] | -41.100 (134) | 610.373 (152) | -651.473 | [-2604.081, 616.325] | 0.145 (134) | -14.751 (152) | 14.896 | 0.039 | [-0.101, 0.175] | 0.5643 |
| Momentum acceleration (%) [reconstructed_historical_gateio_1h] | -0.206 (135) | -0.233 (152) | 0.027 | [-0.256, 0.326] | -0.106 (135) | -0.152 (152) | 0.046 | -0.009 | [-0.144, 0.122] | 0.9051 |
| Momentum decay ratio [reconstructed_historical_gateio_1h] | 0.000 (133) | 0.000 (152) | 0.000 | [0.000, 0.000] | 0.000 (133) | 0.000 (152) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |

#### Experimental candidate

**NO CANDIDATE — EFFECT CRITERIA:** TP/SL cohorts were sufficient, but no feature met the predeclared effect, permutation, confidence-interval, coverage, and balanced-accuracy criteria.

#### SHORT

| Feature | TP mean (n) | SL mean (n) | TP−SL mean | 95% CI mean diff | TP median (n) | SL median (n) | Cliff’s δ | 95% CI δ | p |
|---|---:|---:|---:|---|---:|---:|---:|---|---:|
| SL distance from entry (%) [exact_persisted_derived] | 4.331 (57) | 4.584 (164) | -0.253 | [-1.040, 0.556] | 3.812 (57) | 4.077 (164) | -0.265 | -0.081 | [-0.240, 0.102] | 0.3858 |
| TP distance from entry (%) [exact_persisted_derived] | 8.662 (57) | 9.168 (164) | -0.505 | [-2.027, 1.244] | 7.623 (57) | 8.154 (164) | -0.531 | -0.081 | [-0.262, 0.106] | 0.3583 |
| TP/SL distance ratio [exact_persisted_derived] | 2.000 (57) | 2.000 (164) | -0.000 | [-0.000, 0.000] | 2.000 (57) | 2.000 (164) | 0.000 | -0.095 | [-0.244, 0.068] | 0.2210 |
| Directional entry move from signal (%) [exact_persisted_derived] | 0.000 (57) | 0.000 (164) | 0.000 | [0.000, 0.000] | 0.000 (57) | 0.000 (164) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| RSI at signal [exact_persisted] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| EMA cross gap (%) [runtime_log_rounded] | 0.545 (57) | 0.395 (164) | 0.150 | [0.001, 0.326] | 0.385 (57) | 0.279 (164) | 0.106 | 0.182 | [0.001, 0.367] | 0.0437 |
| Overheated 24h move (%) [runtime_log_rounded] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated RSI [runtime_log_rounded] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation volume ratio (x) [runtime_log_rounded] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation number [runtime_log_exact_integer] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation age (minutes) [runtime_log_exact_integer] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| Directional price change, 1h (%) [reconstructed_historical_gateio_1h] | 0.000 (57) | 0.000 (164) | 0.000 | [0.000, 0.000] | -0.000 (57) | -0.000 (164) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| Directional price change, 2h (%) [reconstructed_historical_gateio_1h] | 1.013 (57) | 0.637 (164) | 0.376 | [-0.565, 1.561] | 0.345 (57) | 0.383 (164) | -0.038 | -0.084 | [-0.263, 0.089] | 0.3458 |
| Directional price change, 4h (%) [reconstructed_historical_gateio_1h] | 2.363 (57) | 1.783 (164) | 0.580 | [-0.683, 2.108] | 1.063 (57) | 1.362 (164) | -0.299 | -0.025 | [-0.186, 0.173] | 0.7765 |
| Candle range, 1h (%) [reconstructed_historical_gateio_1h] | 3.470 (57) | 1.901 (164) | 1.569 | [-0.164, 3.989] | 1.665 (57) | 1.519 (164) | 0.146 | 0.069 | [-0.109, 0.239] | 0.4370 |
| Window range, 2h (%) [reconstructed_historical_gateio_1h] | 4.563 (57) | 3.001 (164) | 1.562 | [-0.247, 3.954] | 2.142 (57) | 2.268 (164) | -0.126 | -0.015 | [-0.195, 0.172] | 0.8627 |
| Window range, 4h (%) [reconstructed_historical_gateio_1h] | 7.826 (57) | 4.955 (164) | 2.870 | [0.286, 5.728] | 3.960 (57) | 3.684 (164) | 0.276 | 0.067 | [-0.125, 0.258] | 0.4469 |
| Distance above recent low, 24h (%) [reconstructed_historical_gateio_1h] | 6.062 (57) | 4.035 (164) | 2.027 | [-0.318, 4.916] | 2.043 (57) | 1.261 (164) | 0.782 | 0.199 | [0.021, 0.366] | 0.0287 |
| Distance below recent high, 24h (%) [reconstructed_historical_gateio_1h] | 11.607 (57) | 11.121 (164) | 0.486 | [-1.655, 3.122] | 9.827 (57) | 8.396 (164) | 1.431 | 0.032 | [-0.133, 0.228] | 0.7228 |
| Realized volatility, 2h (%) [reconstructed_historical_gateio_1h] | 0.000 (57) | 0.000 (164) | 0.000 | [0.000, 0.000] | 0.000 (57) | 0.000 (164) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| Realized volatility, 4h (%) [reconstructed_historical_gateio_1h] | 1.980 (57) | 1.171 (164) | 0.809 | [0.090, 1.628] | 0.893 (57) | 0.898 (164) | -0.005 | 0.045 | [-0.157, 0.247] | 0.6192 |
| Volume ratio, 1h vs prior 24h [reconstructed_historical_gateio_1h] | 4.152 (57) | 1.091 (164) | 3.061 | [-0.279, 8.792] | 0.713 (57) | 0.567 (164) | 0.146 | 0.092 | [-0.089, 0.269] | 0.3109 |
| Volume change, last 1h (%) [reconstructed_historical_gateio_1h] | 1297.319 (57) | 33.291 (164) | 1264.028 | [106.862, 2982.268] | 2.222 (57) | -20.933 (164) | 23.155 | 0.116 | [-0.059, 0.294] | 0.2085 |
| Volume acceleration (%) [reconstructed_historical_gateio_1h] | 1212.928 (57) | -192.738 (164) | 1405.666 | [224.899, 2977.001] | 17.119 (57) | -12.642 (164) | 29.761 | 0.118 | [-0.061, 0.291] | 0.1923 |
| Momentum acceleration (%) [reconstructed_historical_gateio_1h] | -0.506 (57) | -0.319 (164) | -0.188 | [-0.836, 0.262] | -0.173 (57) | -0.192 (164) | 0.019 | 0.084 | [-0.105, 0.256] | 0.3596 |
| Momentum decay ratio [reconstructed_historical_gateio_1h] | 0.000 (57) | 0.000 (163) | 0.000 | [0.000, 0.000] | 0.000 (57) | 0.000 (163) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |

#### Experimental candidate

**NO CANDIDATE — EFFECT CRITERIA:** TP/SL cohorts were sufficient, but no feature met the predeclared effect, permutation, confidence-interval, coverage, and balanced-accuracy criteria.

### overheated_confirmed

#### overall

| Feature | TP mean (n) | SL mean (n) | TP−SL mean | 95% CI mean diff | TP median (n) | SL median (n) | Cliff’s δ | 95% CI δ | p |
|---|---:|---:|---:|---|---:|---:|---:|---|---:|
| SL distance from entry (%) [exact_persisted_derived] | 5.589 (117) | 6.103 (171) | -0.513 | [-1.410, 0.474] | 4.221 (117) | 4.925 (171) | -0.704 | -0.103 | [-0.226, 0.038] | 0.1336 |
| TP distance from entry (%) [exact_persisted_derived] | 10.956 (117) | 11.877 (171) | -0.921 | [-2.807, 0.889] | 8.223 (117) | 8.898 (171) | -0.674 | -0.087 | [-0.228, 0.054] | 0.1960 |
| TP/SL distance ratio [exact_persisted_derived] | 1.953 (117) | 1.933 (171) | 0.020 | [-0.024, 0.065] | 2.000 (117) | 2.000 (171) | 0.000 | 0.064 | [-0.014, 0.156] | 0.1448 |
| Directional entry move from signal (%) [exact_persisted_derived] | 1.526 (117) | 1.902 (171) | -0.376 | [-1.540, 0.808] | 0.000 (117) | 0.000 (171) | 0.000 | -0.006 | [-0.115, 0.100] | 0.9076 |
| RSI at signal [exact_persisted] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| EMA cross gap (%) [runtime_log_rounded] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated 24h move (%) [runtime_log_rounded] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated RSI [runtime_log_rounded] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation volume ratio (x) [runtime_log_rounded] | 2.532 (117) | 2.726 (171) | -0.195 | [-0.487, 0.110] | 2.100 (117) | 2.300 (171) | -0.200 | -0.105 | [-0.234, 0.046] | 0.1398 |
| Confirmation number [runtime_log_exact_integer] | 1.094 (117) | 1.135 (171) | -0.040 | [-0.131, 0.049] | 1.000 (117) | 1.000 (171) | 0.000 | -0.034 | [-0.101, 0.027] | 0.3945 |
| Confirmation age (minutes) [runtime_log_exact_integer] | 41.043 (117) | 45.959 (171) | -4.916 | [-17.157, 9.123] | 19.000 (117) | 11.000 (171) | 8.000 | 0.029 | [-0.095, 0.159] | 0.6604 |
| Directional price change, 1h (%) [reconstructed_historical_gateio_1h] | 0.000 (117) | 0.000 (171) | 0.000 | [0.000, 0.000] | 0.000 (117) | 0.000 (171) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| Directional price change, 2h (%) [reconstructed_historical_gateio_1h] | 3.092 (117) | 3.540 (171) | -0.447 | [-1.590, 0.613] | 1.792 (117) | 2.397 (171) | -0.605 | -0.027 | [-0.165, 0.094] | 0.7029 |
| Directional price change, 4h (%) [reconstructed_historical_gateio_1h] | 4.954 (117) | 6.033 (171) | -1.079 | [-2.618, 0.384] | 3.741 (117) | 4.708 (171) | -0.967 | -0.114 | [-0.251, 0.010] | 0.1011 |
| Candle range, 1h (%) [reconstructed_historical_gateio_1h] | 5.597 (117) | 6.886 (171) | -1.289 | [-2.641, 0.108] | 4.023 (117) | 4.927 (171) | -0.903 | -0.157 | [-0.280, -0.016] | 0.0212 |
| Window range, 2h (%) [reconstructed_historical_gateio_1h] | 7.417 (117) | 9.328 (171) | -1.911 | [-3.622, -0.257] | 5.151 (117) | 7.165 (171) | -2.013 | -0.168 | [-0.290, -0.030] | 0.0137 |
| Window range, 4h (%) [reconstructed_historical_gateio_1h] | 10.599 (117) | 12.839 (171) | -2.240 | [-4.327, -0.258] | 7.795 (117) | 10.275 (171) | -2.480 | -0.172 | [-0.312, -0.034] | 0.0125 |
| Distance above recent low, 24h (%) [reconstructed_historical_gateio_1h] | 24.022 (117) | 24.956 (171) | -0.934 | [-3.797, 2.411] | 20.046 (117) | 20.957 (171) | -0.911 | -0.058 | [-0.190, 0.082] | 0.4407 |
| Distance below recent high, 24h (%) [reconstructed_historical_gateio_1h] | 2.484 (117) | 2.799 (171) | -0.315 | [-1.097, 0.438] | 1.163 (117) | 1.682 (171) | -0.519 | -0.097 | [-0.241, 0.048] | 0.1973 |
| Realized volatility, 2h (%) [reconstructed_historical_gateio_1h] | 0.000 (117) | 0.000 (171) | 0.000 | [0.000, 0.000] | 0.000 (117) | 0.000 (171) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| Realized volatility, 4h (%) [reconstructed_historical_gateio_1h] | 2.575 (117) | 3.106 (171) | -0.531 | [-1.122, 0.037] | 1.885 (117) | 2.293 (171) | -0.408 | -0.105 | [-0.239, 0.032] | 0.1124 |
| Volume ratio, 1h vs prior 24h [reconstructed_historical_gateio_1h] | 2.537 (117) | 5.206 (171) | -2.669 | [-6.685, -0.007] | 1.485 (117) | 1.695 (171) | -0.210 | -0.116 | [-0.253, 0.019] | 0.0999 |
| Volume change, last 1h (%) [reconstructed_historical_gateio_1h] | 148.466 (117) | 292.829 (170) | -144.362 | [-431.852, 72.060] | 24.741 (117) | 36.687 (170) | -11.946 | 0.015 | [-0.121, 0.148] | 0.8177 |
| Volume acceleration (%) [reconstructed_historical_gateio_1h] | 143.270 (117) | 149.180 (170) | -5.910 | [-318.758, 283.627] | 43.181 (117) | 32.021 (170) | 11.160 | 0.078 | [-0.052, 0.214] | 0.2959 |
| Momentum acceleration (%) [reconstructed_historical_gateio_1h] | -1.546 (117) | -1.770 (171) | 0.224 | [-0.367, 0.813] | -0.896 (117) | -1.198 (171) | 0.303 | 0.027 | [-0.103, 0.165] | 0.6979 |
| Momentum decay ratio [reconstructed_historical_gateio_1h] | 0.000 (116) | 0.000 (171) | 0.000 | [0.000, 0.000] | 0.000 (116) | 0.000 (171) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |

#### Experimental candidate

**NO CANDIDATE — EFFECT CRITERIA:** TP/SL cohorts were sufficient, but no feature met the predeclared effect, permutation, confidence-interval, coverage, and balanced-accuracy criteria.

#### LONG

| Feature | TP mean (n) | SL mean (n) | TP−SL mean | 95% CI mean diff | TP median (n) | SL median (n) | Cliff’s δ | 95% CI δ | p |
|---|---:|---:|---:|---|---:|---:|---:|---|---:|
| SL distance from entry (%) [exact_persisted_derived] | 5.589 (117) | 6.103 (171) | -0.513 | [-1.446, 0.418] | 4.221 (117) | 4.925 (171) | -0.704 | -0.103 | [-0.234, 0.034] | 0.1373 |
| TP distance from entry (%) [exact_persisted_derived] | 10.956 (117) | 11.877 (171) | -0.921 | [-2.808, 0.847] | 8.223 (117) | 8.898 (171) | -0.674 | -0.087 | [-0.228, 0.046] | 0.2010 |
| TP/SL distance ratio [exact_persisted_derived] | 1.953 (117) | 1.933 (171) | 0.020 | [-0.025, 0.065] | 2.000 (117) | 2.000 (171) | 0.000 | 0.064 | [-0.022, 0.143] | 0.1573 |
| Directional entry move from signal (%) [exact_persisted_derived] | 1.526 (117) | 1.902 (171) | -0.376 | [-1.531, 0.908] | 0.000 (117) | 0.000 (171) | 0.000 | -0.006 | [-0.116, 0.100] | 0.8964 |
| RSI at signal [exact_persisted] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| EMA cross gap (%) [runtime_log_rounded] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated 24h move (%) [runtime_log_rounded] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated RSI [runtime_log_rounded] | — (0) | — (0) | — | [—, —] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation volume ratio (x) [runtime_log_rounded] | 2.532 (117) | 2.726 (171) | -0.195 | [-0.483, 0.112] | 2.100 (117) | 2.300 (171) | -0.200 | -0.105 | [-0.237, 0.026] | 0.1373 |
| Confirmation number [runtime_log_exact_integer] | 1.094 (117) | 1.135 (171) | -0.040 | [-0.125, 0.043] | 1.000 (117) | 1.000 (171) | 0.000 | -0.034 | [-0.098, 0.037] | 0.4107 |
| Confirmation age (minutes) [runtime_log_exact_integer] | 41.043 (117) | 45.959 (171) | -4.916 | [-18.504, 8.138] | 19.000 (117) | 11.000 (171) | 8.000 | 0.029 | [-0.113, 0.154] | 0.6816 |
| Directional price change, 1h (%) [reconstructed_historical_gateio_1h] | 0.000 (117) | 0.000 (171) | 0.000 | [0.000, 0.000] | 0.000 (117) | 0.000 (171) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| Directional price change, 2h (%) [reconstructed_historical_gateio_1h] | 3.092 (117) | 3.540 (171) | -0.447 | [-1.617, 0.666] | 1.792 (117) | 2.397 (171) | -0.605 | -0.027 | [-0.159, 0.114] | 0.6704 |
| Directional price change, 4h (%) [reconstructed_historical_gateio_1h] | 4.954 (117) | 6.033 (171) | -1.079 | [-2.521, 0.325] | 3.741 (117) | 4.708 (171) | -0.967 | -0.114 | [-0.249, 0.023] | 0.0911 |
| Candle range, 1h (%) [reconstructed_historical_gateio_1h] | 5.597 (117) | 6.886 (171) | -1.289 | [-2.701, 0.092] | 4.023 (117) | 4.927 (171) | -0.903 | -0.157 | [-0.303, -0.024] | 0.0287 |
| Window range, 2h (%) [reconstructed_historical_gateio_1h] | 7.417 (117) | 9.328 (171) | -1.911 | [-3.718, -0.186] | 5.151 (117) | 7.165 (171) | -2.013 | -0.168 | [-0.308, -0.028] | 0.0175 |
| Window range, 4h (%) [reconstructed_historical_gateio_1h] | 10.599 (117) | 12.839 (171) | -2.240 | [-4.371, -0.181] | 7.795 (117) | 10.275 (171) | -2.480 | -0.172 | [-0.303, -0.027] | 0.0062 |
| Distance above recent low, 24h (%) [reconstructed_historical_gateio_1h] | 24.022 (117) | 24.956 (171) | -0.934 | [-3.712, 2.214] | 20.046 (117) | 20.957 (171) | -0.911 | -0.058 | [-0.191, 0.071] | 0.3958 |
| Distance below recent high, 24h (%) [reconstructed_historical_gateio_1h] | 2.484 (117) | 2.799 (171) | -0.315 | [-1.077, 0.392] | 1.163 (117) | 1.682 (171) | -0.519 | -0.097 | [-0.232, 0.044] | 0.1598 |
| Realized volatility, 2h (%) [reconstructed_historical_gateio_1h] | 0.000 (117) | 0.000 (171) | 0.000 | [0.000, 0.000] | 0.000 (117) | 0.000 (171) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| Realized volatility, 4h (%) [reconstructed_historical_gateio_1h] | 2.575 (117) | 3.106 (171) | -0.531 | [-1.106, 0.082] | 1.885 (117) | 2.293 (171) | -0.408 | -0.105 | [-0.244, 0.014] | 0.1149 |
| Volume ratio, 1h vs prior 24h [reconstructed_historical_gateio_1h] | 2.537 (117) | 5.206 (171) | -2.669 | [-7.545, -0.097] | 1.485 (117) | 1.695 (171) | -0.210 | -0.116 | [-0.255, 0.013] | 0.0936 |
| Volume change, last 1h (%) [reconstructed_historical_gateio_1h] | 148.466 (117) | 292.829 (170) | -144.362 | [-440.892, 78.908] | 24.741 (117) | 36.687 (170) | -11.946 | 0.015 | [-0.115, 0.156] | 0.8377 |
| Volume acceleration (%) [reconstructed_historical_gateio_1h] | 143.270 (117) | 149.180 (170) | -5.910 | [-359.504, 271.009] | 43.181 (117) | 32.021 (170) | 11.160 | 0.078 | [-0.060, 0.216] | 0.2734 |
| Momentum acceleration (%) [reconstructed_historical_gateio_1h] | -1.546 (117) | -1.770 (171) | 0.224 | [-0.320, 0.826] | -0.896 (117) | -1.198 (171) | 0.303 | 0.027 | [-0.106, 0.162] | 0.6729 |
| Momentum decay ratio [reconstructed_historical_gateio_1h] | 0.000 (116) | 0.000 (171) | 0.000 | [0.000, 0.000] | 0.000 (116) | 0.000 (171) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |

#### Experimental candidate

**NO CANDIDATE — EFFECT CRITERIA:** TP/SL cohorts were sufficient, but no feature met the predeclared effect, permutation, confidence-interval, coverage, and balanced-accuracy criteria.

#### SHORT

**INSUFFICIENT_TP_OR_SL:** Requires TP>= 20 and SL>= 20; observed TP=0, SL=0. No feature conclusion or candidate is allowed.

## Telegram marker decision

No Telegram marker is enabled by this analysis. A marker may only be added behind an explicit default-off control after a candidate is deliberately accepted for forward-shadow testing; it must remain informational and cannot affect signal generation.

## Guardrails

- Runtime-log values are rounded at emission time and are not exact raw market snapshots.
- Missing fields are left missing; no current ticker is substituted for historical signal-time data.
- Statistical summaries are exploratory and subject to multiple-comparison bias.
- No candidate is forward-validated by this report.
