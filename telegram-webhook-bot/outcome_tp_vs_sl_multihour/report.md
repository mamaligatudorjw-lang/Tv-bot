# TP vs SL — strong-signal experimental analysis

**Read-only report. No production logic, filters, score, SL/TP, or SQLite rows were changed.**

- Scope: all valid target shadow `demo_positions` rows; loaded **1604**, with **1025** resolved.
- Runtime log matches: **1207** (75.25%).
- Historical 1h candle coverage: **390** of **390** symbols.
- Minimum comparison cohort: **20 TP-first and 20 SL-first**.
- `WR` is TP / (TP + SL); `avg R` uses recorded exit price and original entry-to-SL risk.
- Any rule below is in-sample: the threshold was selected and scored on the same rows.

## Feature provenance

| Field | Provenance | Coverage | Meaning |
|---|---|---:|---|
| SL distance from entry (%) (`risk_pct`) | exact_persisted_derived | 100.0% | abs(entry_price - sl_price) / entry_price |
| TP distance from entry (%) (`reward_pct`) | exact_persisted_derived | 100.0% | abs(tp_price - entry_price) / entry_price |
| TP/SL distance ratio (`reward_risk`) | exact_persisted_derived | 74.6% | reward_pct / risk_pct |
| Directional entry move from signal (%) (`entry_vs_signal_pct`) | exact_persisted_derived | 100.0% | direction-adjusted entry_price vs signal_price |
| EMA cross gap (%) (`ema_gap_pct_log`) | runtime_log_rounded | 29.6% | EMA(9)-EMA(21) gap emitted by the signal path |
| Overheated 24h move (%) (`overheated_pct24_log`) | runtime_log_rounded | 12.6% | pct24 emitted by the overheated early signal path |
| Overheated RSI (`overheated_rsi_log`) | runtime_log_rounded | 12.6% | RSI emitted by the overheated early signal path |
| Confirmation volume ratio (x) (`confirmation_volume_ratio_log`) | runtime_log_rounded | 33.0% | completed-candle volume / 10-bar average |
| Confirmation number (`confirmation_number_log`) | runtime_log_exact_integer | 33.0% | confirmation count emitted by continuation telemetry |
| Confirmation age (minutes) (`confirmation_age_min_log`) | runtime_log_exact_integer | 33.0% | age of the parent signal at confirmation |
| Directional price change, 1h (%) (`price_return_1h_pct`) | reconstructed_historical_gateio_1h | 100.0% | direction-adjusted close-to-close return over the last completed 1h candle |
| Directional price change, 2h (%) (`price_return_2h_pct`) | reconstructed_historical_gateio_1h | 100.0% | direction-adjusted close-to-close return over the last two completed 1h candles |
| Directional price change, 4h (%) (`price_return_4h_pct`) | reconstructed_historical_gateio_1h | 100.0% | direction-adjusted close-to-close return over the last four completed 1h candles |
| Candle range, 1h (%) (`range_1h_pct`) | reconstructed_historical_gateio_1h | 100.0% | high-low range of the last completed 1h candle divided by its low |
| Window range, 2h (%) (`range_2h_pct`) | reconstructed_historical_gateio_1h | 100.0% | high-low range across the last two completed 1h candles |
| Window range, 4h (%) (`range_4h_pct`) | reconstructed_historical_gateio_1h | 100.0% | high-low range across the last four completed 1h candles |
| Realized volatility, 2h (%) (`realized_vol_2h_pct`) | reconstructed_historical_gateio_1h | 100.0% | population standard deviation of completed 1h log returns in the 2h window |
| Realized volatility, 4h (%) (`realized_vol_4h_pct`) | reconstructed_historical_gateio_1h | 100.0% | population standard deviation of completed 1h log returns in the 4h window |
| Volume ratio, 1h vs prior 24h (`volume_ratio_1h_vs_24h`) | reconstructed_historical_gateio_1h | 100.0% | latest completed 1h volume divided by the mean of the preceding 24 completed 1h volumes |
| Volume change, last 1h (%) (`volume_change_1h_pct`) | reconstructed_historical_gateio_1h | 99.8% | latest completed 1h volume change versus the preceding completed 1h candle |
| Volume acceleration (%) (`volume_acceleration_pct`) | reconstructed_historical_gateio_1h | 99.8% | change in volume growth rate across the last three completed 1h candles |
| Momentum acceleration (%) (`momentum_acceleration_pct`) | reconstructed_historical_gateio_1h | 100.0% | directional 1h return minus the average directional return per hour over 2h |
| Momentum decay ratio (`momentum_decay_ratio`) | reconstructed_historical_gateio_1h | 99.3% | absolute directional 1h return divided by absolute directional 4h return per hour |

## Current strategy performance

| Strategy | Cohort | total | resolved | TP | SL | WR resolved | avg R | Status |
|---|---|---:|---:|---:|---:|---:|---:|---|
| ema_cross_confirmed | overall | 410 | 150 | 79 | 71 | 52.67% | 0.5663 | READY |
| ema_cross_confirmed | LONG | 226 | 99 | 67 | 32 | 67.68% | 1.0694 | READY |
| ema_cross_confirmed | SHORT | 184 | 51 | 12 | 39 | 23.53% | -0.4103 | INSUFFICIENT_TP_OR_SL |
| overheated_early | overall | 254 | 226 | 103 | 123 | 45.58% | 0.4214 | READY |
| overheated_early | LONG | 254 | 226 | 103 | 123 | 45.58% | 0.4214 | READY |
| overheated_early | SHORT | 0 | 0 | 0 | 0 | —% | — | INSUFFICIENT_TP_OR_SL |
| ema_cross | overall | 475 | 410 | 170 | 240 | 41.46% | 0.2056 | READY |
| ema_cross | LONG | 267 | 237 | 118 | 119 | 49.79% | 0.4858 | READY |
| ema_cross | SHORT | 208 | 173 | 52 | 121 | 30.06% | -0.1782 | READY |
| overheated_confirmed | overall | 465 | 239 | 101 | 138 | 42.26% | 0.2633 | READY |
| overheated_confirmed | LONG | 465 | 239 | 101 | 138 | 42.26% | 0.2633 | READY |
| overheated_confirmed | SHORT | 0 | 0 | 0 | 0 | —% | — | INSUFFICIENT_TP_OR_SL |

## Direction summary across all strategies

| Strategy | Direction | total | resolved | TP | SL | WR resolved | avg R | Status |
|---|---|---:|---:|---:|---:|---:|---:|---|
| ema_cross_confirmed | LONG | 226 | 99 | 67 | 32 | 67.68% | 1.0694 | READY |
| ema_cross_confirmed | SHORT | 184 | 51 | 12 | 39 | 23.53% | -0.4103 | INSUFFICIENT_TP_OR_SL |
| overheated_early | LONG | 254 | 226 | 103 | 123 | 45.58% | 0.4214 | READY |
| overheated_early | SHORT | 0 | 0 | 0 | 0 | —% | — | INSUFFICIENT_TP_OR_SL |
| ema_cross | LONG | 267 | 237 | 118 | 119 | 49.79% | 0.4858 | READY |
| ema_cross | SHORT | 208 | 173 | 52 | 121 | 30.06% | -0.1782 | READY |
| overheated_confirmed | LONG | 465 | 239 | 101 | 138 | 42.26% | 0.2633 | READY |
| overheated_confirmed | SHORT | 0 | 0 | 0 | 0 | —% | — | INSUFFICIENT_TP_OR_SL |

## Retrospective candidate volume and precision

| Strategy | Cohort | Candidate | Baseline/day | Selected/day | Selected signals | TP precision | TP recall | Selected WR |
|---|---|---|---:|---:|---:|---:|---:|---:|
| ema_cross_confirmed | overall | risk_pct | 34.17 | 21.75 | 261 | 0.833 | 0.633 | 83.33% |
| ema_cross_confirmed | LONG | risk_pct | 18.83 | 13.25 | 159 | 0.870 | 0.701 | 87.04% |
| ema_cross_confirmed | SHORT | NO CANDIDATE | 15.33 | 0.00 | 0 | — | — | —% |
| overheated_early | overall | NO CANDIDATE | 23.09 | 0.00 | 0 | — | — | —% |
| overheated_early | LONG | NO CANDIDATE | 23.09 | 0.00 | 0 | — | — | —% |
| overheated_early | SHORT | NO CANDIDATE | — | — | 0 | — | — | —% |
| ema_cross | overall | NO CANDIDATE | 29.69 | 0.00 | 0 | — | — | —% |
| ema_cross | LONG | NO CANDIDATE | 16.69 | 0.00 | 0 | — | — | —% |
| ema_cross | SHORT | NO CANDIDATE | 13.00 | 0.00 | 0 | — | — | —% |
| overheated_confirmed | overall | NO CANDIDATE | 35.77 | 0.00 | 0 | — | — | —% |
| overheated_confirmed | LONG | NO CANDIDATE | 35.77 | 0.00 | 0 | — | — | —% |
| overheated_confirmed | SHORT | NO CANDIDATE | — | — | 0 | — | — | —% |

## TP-first vs SL-first comparisons

### ema_cross_confirmed

#### overall

| Feature | TP median (n) | SL median (n) | TP−SL median | Cliff’s δ | 95% CI δ | p |
|---|---:|---:|---:|---:|---|---:|
| SL distance from entry (%) [exact_persisted_derived] | 2.824 (79) | 6.367 (71) | -3.543 | -0.565 | [-0.723, -0.403] | 0.0012 |
| TP distance from entry (%) [exact_persisted_derived] | 5.647 (79) | 12.734 (71) | -7.087 | -0.542 | [-0.686, -0.387] | 0.0012 |
| TP/SL distance ratio [exact_persisted_derived] | 2.000 (79) | 2.000 (71) | 0.000 | 0.079 | [-0.011, 0.176] | 0.1174 |
| Directional entry move from signal (%) [exact_persisted_derived] | 0.000 (79) | 0.000 (71) | 0.000 | -0.159 | [-0.283, -0.036] | 0.0125 |
| EMA cross gap (%) [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated 24h move (%) [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated RSI [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation volume ratio (x) [runtime_log_rounded] | 2.800 (79) | 2.500 (71) | 0.300 | 0.153 | [-0.034, 0.336] | 0.1211 |
| Confirmation number [runtime_log_exact_integer] | 1.000 (79) | 1.000 (71) | 0.000 | -0.017 | [-0.073, 0.038] | 0.4894 |
| Confirmation age (minutes) [runtime_log_exact_integer] | 48.000 (79) | 58.000 (71) | -10.000 | -0.142 | [-0.349, 0.028] | 0.1149 |
| Directional price change, 1h (%) [reconstructed_historical_gateio_1h] | 0.000 (79) | -0.000 (71) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| Directional price change, 2h (%) [reconstructed_historical_gateio_1h] | 0.487 (79) | 0.720 (71) | -0.234 | -0.092 | [-0.270, 0.091] | 0.3408 |
| Directional price change, 4h (%) [reconstructed_historical_gateio_1h] | 0.609 (79) | 1.021 (71) | -0.412 | -0.125 | [-0.308, 0.059] | 0.1773 |
| Candle range, 1h (%) [reconstructed_historical_gateio_1h] | 1.376 (79) | 1.702 (71) | -0.325 | -0.110 | [-0.288, 0.077] | 0.2372 |
| Window range, 2h (%) [reconstructed_historical_gateio_1h] | 1.930 (79) | 2.118 (71) | -0.188 | -0.083 | [-0.275, 0.094] | 0.3920 |
| Window range, 4h (%) [reconstructed_historical_gateio_1h] | 3.076 (79) | 3.816 (71) | -0.739 | -0.146 | [-0.324, 0.055] | 0.1011 |
| Realized volatility, 2h (%) [reconstructed_historical_gateio_1h] | 0.000 (79) | 0.000 (71) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| Realized volatility, 4h (%) [reconstructed_historical_gateio_1h] | 0.657 (79) | 0.752 (71) | -0.096 | -0.123 | [-0.303, 0.070] | 0.2110 |
| Volume ratio, 1h vs prior 24h [reconstructed_historical_gateio_1h] | 0.733 (79) | 0.634 (71) | 0.099 | 0.101 | [-0.096, 0.279] | 0.2971 |
| Volume change, last 1h (%) [reconstructed_historical_gateio_1h] | 11.924 (79) | -5.494 (70) | 17.418 | 0.009 | [-0.189, 0.200] | 0.9101 |
| Volume acceleration (%) [reconstructed_historical_gateio_1h] | 36.427 (79) | 29.675 (70) | 6.752 | -0.025 | [-0.207, 0.150] | 0.7778 |
| Momentum acceleration (%) [reconstructed_historical_gateio_1h] | -0.243 (79) | -0.360 (71) | 0.117 | 0.092 | [-0.092, 0.294] | 0.3159 |
| Momentum decay ratio [reconstructed_historical_gateio_1h] | 0.000 (78) | 0.000 (70) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |

#### Experimental candidate

- **SL distance from entry (%) ≤ 3.40695 predicts TP**
- Provenance: **exact_persisted_derived**
- In-sample accuracy: **0.74**, balanced accuracy: **0.746033**
- Precision TP: **0.833333**; precision SL: **0.677778**
- TP recall: **0.632911**; SL recall: **0.859155**
- Retrospective selected volume: **261** signals, **21.75**/day.
- This is experimental and requires forward-shadow validation; it is not a production rule.

#### LONG

| Feature | TP median (n) | SL median (n) | TP−SL median | Cliff’s δ | 95% CI δ | p |
|---|---:|---:|---:|---:|---|---:|
| SL distance from entry (%) [exact_persisted_derived] | 2.722 (67) | 4.400 (32) | -1.678 | -0.432 | [-0.624, -0.216] | 0.0012 |
| TP distance from entry (%) [exact_persisted_derived] | 5.445 (67) | 8.323 (32) | -2.879 | -0.375 | [-0.598, -0.148] | 0.0037 |
| TP/SL distance ratio [exact_persisted_derived] | 2.000 (67) | 2.000 (32) | 0.000 | 0.160 | [0.014, 0.302] | 0.0275 |
| Directional entry move from signal (%) [exact_persisted_derived] | 0.000 (67) | 0.000 (32) | 0.000 | -0.109 | [-0.273, 0.046] | 0.1099 |
| EMA cross gap (%) [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated 24h move (%) [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated RSI [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation volume ratio (x) [runtime_log_rounded] | 2.800 (67) | 2.100 (32) | 0.700 | 0.230 | [-0.007, 0.471] | 0.0624 |
| Confirmation number [runtime_log_exact_integer] | 1.000 (67) | 1.000 (32) | 0.000 | -0.065 | [-0.184, 0.028] | 0.2072 |
| Confirmation age (minutes) [runtime_log_exact_integer] | 48.000 (67) | 110.500 (32) | -62.500 | -0.346 | [-0.568, -0.110] | 0.0125 |
| Directional price change, 1h (%) [reconstructed_historical_gateio_1h] | 0.000 (67) | 0.000 (32) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| Directional price change, 2h (%) [reconstructed_historical_gateio_1h] | 0.542 (67) | 0.907 (32) | -0.365 | -0.106 | [-0.348, 0.150] | 0.3783 |
| Directional price change, 4h (%) [reconstructed_historical_gateio_1h] | 0.593 (67) | 0.936 (32) | -0.343 | -0.050 | [-0.299, 0.215] | 0.7029 |
| Candle range, 1h (%) [reconstructed_historical_gateio_1h] | 1.392 (67) | 2.011 (32) | -0.619 | -0.283 | [-0.522, -0.053] | 0.0187 |
| Window range, 2h (%) [reconstructed_historical_gateio_1h] | 1.836 (67) | 2.928 (32) | -1.092 | -0.281 | [-0.509, -0.039] | 0.0424 |
| Window range, 4h (%) [reconstructed_historical_gateio_1h] | 3.076 (67) | 3.899 (32) | -0.822 | -0.194 | [-0.448, 0.083] | 0.1198 |
| Realized volatility, 2h (%) [reconstructed_historical_gateio_1h] | 0.000 (67) | 0.000 (32) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| Realized volatility, 4h (%) [reconstructed_historical_gateio_1h] | 0.645 (67) | 0.874 (32) | -0.230 | -0.187 | [-0.445, 0.079] | 0.1323 |
| Volume ratio, 1h vs prior 24h [reconstructed_historical_gateio_1h] | 0.833 (67) | 0.971 (32) | -0.139 | -0.103 | [-0.351, 0.150] | 0.4020 |
| Volume change, last 1h (%) [reconstructed_historical_gateio_1h] | 28.837 (67) | 26.986 (31) | 1.851 | 0.019 | [-0.218, 0.301] | 0.8939 |
| Volume acceleration (%) [reconstructed_historical_gateio_1h] | 36.427 (67) | 15.761 (31) | 20.667 | 0.068 | [-0.187, 0.300] | 0.6055 |
| Momentum acceleration (%) [reconstructed_historical_gateio_1h] | -0.271 (67) | -0.453 (32) | 0.182 | 0.106 | [-0.152, 0.386] | 0.3858 |
| Momentum decay ratio [reconstructed_historical_gateio_1h] | 0.000 (67) | 0.000 (31) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |

#### Experimental candidate

- **SL distance from entry (%) ≤ 3.55255 predicts TP**
- Provenance: **exact_persisted_derived**
- In-sample accuracy: **0.727273**, balanced accuracy: **0.741371**
- Precision TP: **0.87037**; precision SL: **0.555556**
- TP recall: **0.701493**; SL recall: **0.78125**
- Retrospective selected volume: **159** signals, **13.25**/day.
- This is experimental and requires forward-shadow validation; it is not a production rule.

#### SHORT

**INSUFFICIENT_TP_OR_SL:** Requires TP>= 20 and SL>= 20; observed TP=12, SL=39. No feature conclusion or candidate is allowed.

### overheated_early

#### overall

| Feature | TP median (n) | SL median (n) | TP−SL median | Cliff’s δ | 95% CI δ | p |
|---|---:|---:|---:|---:|---|---:|
| SL distance from entry (%) [exact_persisted_derived] | 5.128 (103) | 5.773 (123) | -0.645 | -0.114 | [-0.268, 0.040] | 0.1348 |
| TP distance from entry (%) [exact_persisted_derived] | 10.256 (103) | 11.546 (123) | -1.290 | -0.114 | [-0.268, 0.044] | 0.1286 |
| TP/SL distance ratio [exact_persisted_derived] | 2.000 (103) | 2.000 (123) | 0.000 | 0.091 | [-0.032, 0.229] | 0.1873 |
| Directional entry move from signal (%) [exact_persisted_derived] | 0.000 (103) | 0.000 (123) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| EMA cross gap (%) [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated 24h move (%) [runtime_log_rounded] | 18.100 (71) | 18.550 (104) | -0.450 | -0.029 | [-0.196, 0.139] | 0.7653 |
| Overheated RSI [runtime_log_rounded] | 66.200 (71) | 66.800 (104) | -0.600 | -0.067 | [-0.249, 0.105] | 0.4557 |
| Confirmation volume ratio (x) [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation number [runtime_log_exact_integer] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation age (minutes) [runtime_log_exact_integer] | — (0) | — (0) | — | — | [—, —] | — |
| Directional price change, 1h (%) [reconstructed_historical_gateio_1h] | 0.000 (103) | 0.000 (123) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| Directional price change, 2h (%) [reconstructed_historical_gateio_1h] | -0.153 (103) | -0.148 (123) | -0.005 | 0.049 | [-0.104, 0.197] | 0.5243 |
| Directional price change, 4h (%) [reconstructed_historical_gateio_1h] | 0.733 (103) | 1.081 (123) | -0.348 | 0.013 | [-0.144, 0.174] | 0.8427 |
| Candle range, 1h (%) [reconstructed_historical_gateio_1h] | 3.300 (103) | 4.573 (123) | -1.273 | -0.227 | [-0.363, -0.068] | 0.0062 |
| Window range, 2h (%) [reconstructed_historical_gateio_1h] | 4.894 (103) | 7.530 (123) | -2.636 | -0.205 | [-0.338, -0.058] | 0.0075 |
| Window range, 4h (%) [reconstructed_historical_gateio_1h] | 8.023 (103) | 11.365 (123) | -3.342 | -0.226 | [-0.358, -0.072] | 0.0037 |
| Realized volatility, 2h (%) [reconstructed_historical_gateio_1h] | 0.000 (103) | 0.000 (123) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| Realized volatility, 4h (%) [reconstructed_historical_gateio_1h] | 1.994 (103) | 2.453 (123) | -0.459 | -0.137 | [-0.291, 0.022] | 0.0999 |
| Volume ratio, 1h vs prior 24h [reconstructed_historical_gateio_1h] | 0.990 (103) | 1.400 (123) | -0.410 | -0.194 | [-0.335, -0.041] | 0.0112 |
| Volume change, last 1h (%) [reconstructed_historical_gateio_1h] | -9.671 (103) | -8.636 (123) | -1.034 | -0.031 | [-0.178, 0.124] | 0.6754 |
| Volume acceleration (%) [reconstructed_historical_gateio_1h] | -26.996 (103) | -8.866 (123) | -18.130 | -0.039 | [-0.192, 0.134] | 0.6080 |
| Momentum acceleration (%) [reconstructed_historical_gateio_1h] | 0.076 (103) | 0.074 (123) | 0.002 | -0.049 | [-0.189, 0.115] | 0.5181 |
| Momentum decay ratio [reconstructed_historical_gateio_1h] | 0.000 (101) | 0.000 (122) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |

#### Experimental candidate

**NO CANDIDATE — EFFECT CRITERIA:** TP/SL cohorts were sufficient, but no feature met the predeclared effect, permutation, confidence-interval, coverage, and balanced-accuracy criteria.

#### LONG

| Feature | TP median (n) | SL median (n) | TP−SL median | Cliff’s δ | 95% CI δ | p |
|---|---:|---:|---:|---:|---|---:|
| SL distance from entry (%) [exact_persisted_derived] | 5.128 (103) | 5.773 (123) | -0.645 | -0.114 | [-0.263, 0.032] | 0.1448 |
| TP distance from entry (%) [exact_persisted_derived] | 10.256 (103) | 11.546 (123) | -1.290 | -0.114 | [-0.268, 0.039] | 0.1548 |
| TP/SL distance ratio [exact_persisted_derived] | 2.000 (103) | 2.000 (123) | 0.000 | 0.091 | [-0.032, 0.218] | 0.1773 |
| Directional entry move from signal (%) [exact_persisted_derived] | 0.000 (103) | 0.000 (123) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| EMA cross gap (%) [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated 24h move (%) [runtime_log_rounded] | 18.100 (71) | 18.550 (104) | -0.450 | -0.029 | [-0.204, 0.141] | 0.7253 |
| Overheated RSI [runtime_log_rounded] | 66.200 (71) | 66.800 (104) | -0.600 | -0.067 | [-0.231, 0.107] | 0.4632 |
| Confirmation volume ratio (x) [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation number [runtime_log_exact_integer] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation age (minutes) [runtime_log_exact_integer] | — (0) | — (0) | — | — | [—, —] | — |
| Directional price change, 1h (%) [reconstructed_historical_gateio_1h] | 0.000 (103) | 0.000 (123) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| Directional price change, 2h (%) [reconstructed_historical_gateio_1h] | -0.153 (103) | -0.148 (123) | -0.005 | 0.049 | [-0.097, 0.209] | 0.5381 |
| Directional price change, 4h (%) [reconstructed_historical_gateio_1h] | 0.733 (103) | 1.081 (123) | -0.348 | 0.013 | [-0.134, 0.157] | 0.8727 |
| Candle range, 1h (%) [reconstructed_historical_gateio_1h] | 3.300 (103) | 4.573 (123) | -1.273 | -0.227 | [-0.362, -0.069] | 0.0037 |
| Window range, 2h (%) [reconstructed_historical_gateio_1h] | 4.894 (103) | 7.530 (123) | -2.636 | -0.205 | [-0.354, -0.055] | 0.0100 |
| Window range, 4h (%) [reconstructed_historical_gateio_1h] | 8.023 (103) | 11.365 (123) | -3.342 | -0.226 | [-0.378, -0.067] | 0.0050 |
| Realized volatility, 2h (%) [reconstructed_historical_gateio_1h] | 0.000 (103) | 0.000 (123) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| Realized volatility, 4h (%) [reconstructed_historical_gateio_1h] | 1.994 (103) | 2.453 (123) | -0.459 | -0.137 | [-0.289, 0.016] | 0.0599 |
| Volume ratio, 1h vs prior 24h [reconstructed_historical_gateio_1h] | 0.990 (103) | 1.400 (123) | -0.410 | -0.194 | [-0.339, -0.043] | 0.0125 |
| Volume change, last 1h (%) [reconstructed_historical_gateio_1h] | -9.671 (103) | -8.636 (123) | -1.034 | -0.031 | [-0.179, 0.127] | 0.6916 |
| Volume acceleration (%) [reconstructed_historical_gateio_1h] | -26.996 (103) | -8.866 (123) | -18.130 | -0.039 | [-0.184, 0.117] | 0.6205 |
| Momentum acceleration (%) [reconstructed_historical_gateio_1h] | 0.076 (103) | 0.074 (123) | 0.002 | -0.049 | [-0.184, 0.090] | 0.5581 |
| Momentum decay ratio [reconstructed_historical_gateio_1h] | 0.000 (101) | 0.000 (122) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |

#### Experimental candidate

**NO CANDIDATE — EFFECT CRITERIA:** TP/SL cohorts were sufficient, but no feature met the predeclared effect, permutation, confidence-interval, coverage, and balanced-accuracy criteria.

#### SHORT

**INSUFFICIENT_TP_OR_SL:** Requires TP>= 20 and SL>= 20; observed TP=0, SL=0. No feature conclusion or candidate is allowed.

### ema_cross

#### overall

| Feature | TP median (n) | SL median (n) | TP−SL median | Cliff’s δ | 95% CI δ | p |
|---|---:|---:|---:|---:|---|---:|
| SL distance from entry (%) [exact_persisted_derived] | 2.727 (170) | 3.753 (240) | -1.026 | -0.241 | [-0.361, -0.120] | 0.0012 |
| TP distance from entry (%) [exact_persisted_derived] | 5.455 (170) | 7.506 (240) | -2.051 | -0.241 | [-0.356, -0.135] | 0.0012 |
| TP/SL distance ratio [exact_persisted_derived] | 2.000 (170) | 2.000 (240) | 0.000 | -0.051 | [-0.161, 0.066] | 0.3446 |
| Directional entry move from signal (%) [exact_persisted_derived] | 0.000 (170) | 0.000 (240) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| EMA cross gap (%) [runtime_log_rounded] | 0.291 (170) | 0.333 (240) | -0.041 | -0.065 | [-0.175, 0.053] | 0.2672 |
| Overheated 24h move (%) [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated RSI [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation volume ratio (x) [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation number [runtime_log_exact_integer] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation age (minutes) [runtime_log_exact_integer] | — (0) | — (0) | — | — | [—, —] | — |
| Directional price change, 1h (%) [reconstructed_historical_gateio_1h] | 0.000 (170) | 0.000 (240) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| Directional price change, 2h (%) [reconstructed_historical_gateio_1h] | 0.232 (170) | 0.354 (240) | -0.122 | -0.021 | [-0.133, 0.095] | 0.6891 |
| Directional price change, 4h (%) [reconstructed_historical_gateio_1h] | 1.055 (170) | 1.421 (240) | -0.366 | -0.101 | [-0.215, -0.002] | 0.0687 |
| Candle range, 1h (%) [reconstructed_historical_gateio_1h] | 1.746 (170) | 1.811 (240) | -0.065 | -0.088 | [-0.201, 0.034] | 0.1223 |
| Window range, 2h (%) [reconstructed_historical_gateio_1h] | 2.311 (170) | 2.789 (240) | -0.477 | -0.142 | [-0.262, -0.019] | 0.0137 |
| Window range, 4h (%) [reconstructed_historical_gateio_1h] | 3.888 (170) | 4.406 (240) | -0.518 | -0.111 | [-0.225, 0.015] | 0.0687 |
| Realized volatility, 2h (%) [reconstructed_historical_gateio_1h] | 0.000 (170) | 0.000 (240) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| Realized volatility, 4h (%) [reconstructed_historical_gateio_1h] | 0.931 (170) | 1.070 (240) | -0.140 | -0.088 | [-0.203, 0.027] | 0.1323 |
| Volume ratio, 1h vs prior 24h [reconstructed_historical_gateio_1h] | 0.976 (170) | 0.924 (240) | 0.053 | 0.001 | [-0.107, 0.115] | 0.9825 |
| Volume change, last 1h (%) [reconstructed_historical_gateio_1h] | -1.533 (169) | -17.251 (240) | 15.718 | 0.073 | [-0.043, 0.190] | 0.2122 |
| Volume acceleration (%) [reconstructed_historical_gateio_1h] | 3.595 (169) | -14.751 (240) | 18.346 | 0.073 | [-0.045, 0.189] | 0.2060 |
| Momentum acceleration (%) [reconstructed_historical_gateio_1h] | -0.116 (170) | -0.177 (240) | 0.061 | 0.021 | [-0.098, 0.130] | 0.6991 |
| Momentum decay ratio [reconstructed_historical_gateio_1h] | 0.000 (168) | 0.000 (239) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |

#### Experimental candidate

**NO CANDIDATE — EFFECT CRITERIA:** TP/SL cohorts were sufficient, but no feature met the predeclared effect, permutation, confidence-interval, coverage, and balanced-accuracy criteria.

#### LONG

| Feature | TP median (n) | SL median (n) | TP−SL median | Cliff’s δ | 95% CI δ | p |
|---|---:|---:|---:|---:|---|---:|
| SL distance from entry (%) [exact_persisted_derived] | 2.331 (118) | 3.142 (119) | -0.811 | -0.245 | [-0.384, -0.108] | 0.0012 |
| TP distance from entry (%) [exact_persisted_derived] | 4.662 (118) | 6.284 (119) | -1.622 | -0.245 | [-0.393, -0.093] | 0.0025 |
| TP/SL distance ratio [exact_persisted_derived] | 2.000 (118) | 2.000 (119) | 0.000 | -0.031 | [-0.169, 0.106] | 0.6754 |
| Directional entry move from signal (%) [exact_persisted_derived] | 0.000 (118) | 0.000 (119) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| EMA cross gap (%) [runtime_log_rounded] | 0.270 (118) | 0.367 (119) | -0.098 | -0.185 | [-0.334, -0.037] | 0.0162 |
| Overheated 24h move (%) [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated RSI [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation volume ratio (x) [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation number [runtime_log_exact_integer] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation age (minutes) [runtime_log_exact_integer] | — (0) | — (0) | — | — | [—, —] | — |
| Directional price change, 1h (%) [reconstructed_historical_gateio_1h] | 0.000 (118) | 0.000 (119) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| Directional price change, 2h (%) [reconstructed_historical_gateio_1h] | 0.220 (118) | 0.320 (119) | -0.100 | 0.022 | [-0.115, 0.169] | 0.7990 |
| Directional price change, 4h (%) [reconstructed_historical_gateio_1h] | 0.989 (118) | 1.185 (119) | -0.195 | -0.095 | [-0.260, 0.056] | 0.2010 |
| Candle range, 1h (%) [reconstructed_historical_gateio_1h] | 1.790 (118) | 2.300 (119) | -0.510 | -0.229 | [-0.369, -0.083] | 0.0050 |
| Window range, 2h (%) [reconstructed_historical_gateio_1h] | 2.342 (118) | 3.587 (119) | -1.244 | -0.269 | [-0.407, -0.128] | 0.0012 |
| Window range, 4h (%) [reconstructed_historical_gateio_1h] | 3.869 (118) | 5.733 (119) | -1.864 | -0.240 | [-0.381, -0.076] | 0.0037 |
| Realized volatility, 2h (%) [reconstructed_historical_gateio_1h] | 0.000 (118) | 0.000 (119) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| Realized volatility, 4h (%) [reconstructed_historical_gateio_1h] | 0.949 (118) | 1.328 (119) | -0.379 | -0.223 | [-0.361, -0.089] | 0.0012 |
| Volume ratio, 1h vs prior 24h [reconstructed_historical_gateio_1h] | 1.035 (118) | 1.316 (119) | -0.281 | -0.212 | [-0.351, -0.052] | 0.0062 |
| Volume change, last 1h (%) [reconstructed_historical_gateio_1h] | -7.529 (117) | -13.881 (119) | 6.352 | 0.038 | [-0.103, 0.184] | 0.6092 |
| Volume acceleration (%) [reconstructed_historical_gateio_1h] | 3.333 (117) | -21.213 (119) | 24.545 | 0.072 | [-0.074, 0.209] | 0.3296 |
| Momentum acceleration (%) [reconstructed_historical_gateio_1h] | -0.110 (118) | -0.160 (119) | 0.050 | -0.022 | [-0.163, 0.143] | 0.8015 |
| Momentum decay ratio [reconstructed_historical_gateio_1h] | 0.000 (116) | 0.000 (119) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |

#### Experimental candidate

**NO CANDIDATE — EFFECT CRITERIA:** TP/SL cohorts were sufficient, but no feature met the predeclared effect, permutation, confidence-interval, coverage, and balanced-accuracy criteria.

#### SHORT

| Feature | TP median (n) | SL median (n) | TP−SL median | Cliff’s δ | 95% CI δ | p |
|---|---:|---:|---:|---:|---|---:|
| SL distance from entry (%) [exact_persisted_derived] | 3.906 (52) | 4.434 (121) | -0.528 | -0.120 | [-0.299, 0.070] | 0.2135 |
| TP distance from entry (%) [exact_persisted_derived] | 7.812 (52) | 8.869 (121) | -1.057 | -0.120 | [-0.302, 0.078] | 0.2447 |
| TP/SL distance ratio [exact_persisted_derived] | 2.000 (52) | 2.000 (121) | 0.000 | -0.069 | [-0.244, 0.120] | 0.4507 |
| Directional entry move from signal (%) [exact_persisted_derived] | 0.000 (52) | 0.000 (121) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| EMA cross gap (%) [runtime_log_rounded] | 0.385 (52) | 0.310 (121) | 0.074 | 0.143 | [-0.063, 0.334] | 0.1298 |
| Overheated 24h move (%) [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated RSI [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation volume ratio (x) [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation number [runtime_log_exact_integer] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation age (minutes) [runtime_log_exact_integer] | — (0) | — (0) | — | — | [—, —] | — |
| Directional price change, 1h (%) [reconstructed_historical_gateio_1h] | -0.000 (52) | -0.000 (121) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| Directional price change, 2h (%) [reconstructed_historical_gateio_1h] | 0.299 (52) | 0.359 (121) | -0.060 | -0.105 | [-0.304, 0.089] | 0.2709 |
| Directional price change, 4h (%) [reconstructed_historical_gateio_1h] | 1.060 (52) | 1.570 (121) | -0.510 | -0.076 | [-0.260, 0.106] | 0.4220 |
| Candle range, 1h (%) [reconstructed_historical_gateio_1h] | 1.701 (52) | 1.569 (121) | 0.132 | 0.037 | [-0.162, 0.232] | 0.6916 |
| Window range, 2h (%) [reconstructed_historical_gateio_1h] | 2.130 (52) | 2.291 (121) | -0.161 | -0.054 | [-0.274, 0.143] | 0.5905 |
| Window range, 4h (%) [reconstructed_historical_gateio_1h] | 3.936 (52) | 3.900 (121) | 0.036 | 0.013 | [-0.190, 0.211] | 0.9001 |
| Realized volatility, 2h (%) [reconstructed_historical_gateio_1h] | 0.000 (52) | 0.000 (121) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| Realized volatility, 4h (%) [reconstructed_historical_gateio_1h] | 0.893 (52) | 0.926 (121) | -0.033 | 0.021 | [-0.177, 0.235] | 0.8340 |
| Volume ratio, 1h vs prior 24h [reconstructed_historical_gateio_1h] | 0.769 (52) | 0.573 (121) | 0.196 | 0.095 | [-0.106, 0.280] | 0.3695 |
| Volume change, last 1h (%) [reconstructed_historical_gateio_1h] | 2.684 (52) | -21.306 (121) | 23.990 | 0.077 | [-0.114, 0.264] | 0.4444 |
| Volume acceleration (%) [reconstructed_historical_gateio_1h] | 17.032 (52) | -10.228 (121) | 27.261 | 0.081 | [-0.095, 0.275] | 0.4045 |
| Momentum acceleration (%) [reconstructed_historical_gateio_1h] | -0.150 (52) | -0.180 (121) | 0.030 | 0.105 | [-0.074, 0.295] | 0.2597 |
| Momentum decay ratio [reconstructed_historical_gateio_1h] | 0.000 (52) | 0.000 (120) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |

#### Experimental candidate

**NO CANDIDATE — EFFECT CRITERIA:** TP/SL cohorts were sufficient, but no feature met the predeclared effect, permutation, confidence-interval, coverage, and balanced-accuracy criteria.

### overheated_confirmed

#### overall

| Feature | TP median (n) | SL median (n) | TP−SL median | Cliff’s δ | 95% CI δ | p |
|---|---:|---:|---:|---:|---|---:|
| SL distance from entry (%) [exact_persisted_derived] | 3.877 (101) | 4.282 (138) | -0.405 | -0.114 | [-0.267, 0.038] | 0.1186 |
| TP distance from entry (%) [exact_persisted_derived] | 7.711 (101) | 8.257 (138) | -0.546 | -0.091 | [-0.245, 0.065] | 0.2409 |
| TP/SL distance ratio [exact_persisted_derived] | 2.000 (101) | 2.000 (138) | 0.000 | 0.049 | [-0.050, 0.138] | 0.3184 |
| Directional entry move from signal (%) [exact_persisted_derived] | 0.000 (101) | 0.000 (138) | 0.000 | -0.040 | [-0.144, 0.061] | 0.4557 |
| EMA cross gap (%) [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated 24h move (%) [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated RSI [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation volume ratio (x) [runtime_log_rounded] | 2.100 (101) | 2.300 (138) | -0.200 | -0.145 | [-0.292, 0.014] | 0.0462 |
| Confirmation number [runtime_log_exact_integer] | 1.000 (101) | 1.000 (138) | 0.000 | -0.046 | [-0.118, 0.030] | 0.2547 |
| Confirmation age (minutes) [runtime_log_exact_integer] | 14.000 (101) | 12.000 (138) | 2.000 | 0.013 | [-0.127, 0.168] | 0.8577 |
| Directional price change, 1h (%) [reconstructed_historical_gateio_1h] | 0.000 (101) | 0.000 (138) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| Directional price change, 2h (%) [reconstructed_historical_gateio_1h] | 1.762 (101) | 2.463 (138) | -0.701 | -0.051 | [-0.196, 0.097] | 0.4856 |
| Directional price change, 4h (%) [reconstructed_historical_gateio_1h] | 3.633 (101) | 4.770 (138) | -1.137 | -0.167 | [-0.323, -0.019] | 0.0225 |
| Candle range, 1h (%) [reconstructed_historical_gateio_1h] | 3.242 (101) | 4.864 (138) | -1.622 | -0.231 | [-0.377, -0.090] | 0.0050 |
| Window range, 2h (%) [reconstructed_historical_gateio_1h] | 4.914 (101) | 7.305 (138) | -2.391 | -0.272 | [-0.419, -0.124] | 0.0012 |
| Window range, 4h (%) [reconstructed_historical_gateio_1h] | 6.970 (101) | 10.357 (138) | -3.388 | -0.268 | [-0.400, -0.122] | 0.0012 |
| Realized volatility, 2h (%) [reconstructed_historical_gateio_1h] | 0.000 (101) | 0.000 (138) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| Realized volatility, 4h (%) [reconstructed_historical_gateio_1h] | 1.553 (101) | 2.253 (138) | -0.700 | -0.197 | [-0.344, -0.067] | 0.0075 |
| Volume ratio, 1h vs prior 24h [reconstructed_historical_gateio_1h] | 1.384 (101) | 1.718 (138) | -0.334 | -0.158 | [-0.312, -0.016] | 0.0287 |
| Volume change, last 1h (%) [reconstructed_historical_gateio_1h] | 24.741 (101) | 37.309 (138) | -12.568 | 0.002 | [-0.136, 0.166] | 0.9750 |
| Volume acceleration (%) [reconstructed_historical_gateio_1h] | 43.181 (101) | 29.362 (138) | 13.819 | 0.065 | [-0.063, 0.208] | 0.4095 |
| Momentum acceleration (%) [reconstructed_historical_gateio_1h] | -0.881 (101) | -1.232 (138) | 0.351 | 0.051 | [-0.086, 0.196] | 0.5206 |
| Momentum decay ratio [reconstructed_historical_gateio_1h] | 0.000 (100) | 0.000 (138) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |

#### Experimental candidate

**NO CANDIDATE — EFFECT CRITERIA:** TP/SL cohorts were sufficient, but no feature met the predeclared effect, permutation, confidence-interval, coverage, and balanced-accuracy criteria.

#### LONG

| Feature | TP median (n) | SL median (n) | TP−SL median | Cliff’s δ | 95% CI δ | p |
|---|---:|---:|---:|---:|---|---:|
| SL distance from entry (%) [exact_persisted_derived] | 3.877 (101) | 4.282 (138) | -0.405 | -0.114 | [-0.251, 0.035] | 0.1236 |
| TP distance from entry (%) [exact_persisted_derived] | 7.711 (101) | 8.257 (138) | -0.546 | -0.091 | [-0.233, 0.062] | 0.2110 |
| TP/SL distance ratio [exact_persisted_derived] | 2.000 (101) | 2.000 (138) | 0.000 | 0.049 | [-0.041, 0.136] | 0.3258 |
| Directional entry move from signal (%) [exact_persisted_derived] | 0.000 (101) | 0.000 (138) | 0.000 | -0.040 | [-0.146, 0.062] | 0.4794 |
| EMA cross gap (%) [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated 24h move (%) [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated RSI [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation volume ratio (x) [runtime_log_rounded] | 2.100 (101) | 2.300 (138) | -0.200 | -0.145 | [-0.297, 0.000] | 0.0462 |
| Confirmation number [runtime_log_exact_integer] | 1.000 (101) | 1.000 (138) | 0.000 | -0.046 | [-0.119, 0.025] | 0.2285 |
| Confirmation age (minutes) [runtime_log_exact_integer] | 14.000 (101) | 12.000 (138) | 2.000 | 0.013 | [-0.136, 0.156] | 0.8427 |
| Directional price change, 1h (%) [reconstructed_historical_gateio_1h] | 0.000 (101) | 0.000 (138) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| Directional price change, 2h (%) [reconstructed_historical_gateio_1h] | 1.762 (101) | 2.463 (138) | -0.701 | -0.051 | [-0.185, 0.096] | 0.4994 |
| Directional price change, 4h (%) [reconstructed_historical_gateio_1h] | 3.633 (101) | 4.770 (138) | -1.137 | -0.167 | [-0.306, -0.020] | 0.0325 |
| Candle range, 1h (%) [reconstructed_historical_gateio_1h] | 3.242 (101) | 4.864 (138) | -1.622 | -0.231 | [-0.371, -0.104] | 0.0037 |
| Window range, 2h (%) [reconstructed_historical_gateio_1h] | 4.914 (101) | 7.305 (138) | -2.391 | -0.272 | [-0.416, -0.131] | 0.0037 |
| Window range, 4h (%) [reconstructed_historical_gateio_1h] | 6.970 (101) | 10.357 (138) | -3.388 | -0.268 | [-0.390, -0.130] | 0.0012 |
| Realized volatility, 2h (%) [reconstructed_historical_gateio_1h] | 0.000 (101) | 0.000 (138) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| Realized volatility, 4h (%) [reconstructed_historical_gateio_1h] | 1.553 (101) | 2.253 (138) | -0.700 | -0.197 | [-0.331, -0.056] | 0.0125 |
| Volume ratio, 1h vs prior 24h [reconstructed_historical_gateio_1h] | 1.384 (101) | 1.718 (138) | -0.334 | -0.158 | [-0.310, -0.005] | 0.0399 |
| Volume change, last 1h (%) [reconstructed_historical_gateio_1h] | 24.741 (101) | 37.309 (138) | -12.568 | 0.002 | [-0.137, 0.169] | 0.9825 |
| Volume acceleration (%) [reconstructed_historical_gateio_1h] | 43.181 (101) | 29.362 (138) | 13.819 | 0.065 | [-0.088, 0.216] | 0.4157 |
| Momentum acceleration (%) [reconstructed_historical_gateio_1h] | -0.881 (101) | -1.232 (138) | 0.351 | 0.051 | [-0.085, 0.198] | 0.5081 |
| Momentum decay ratio [reconstructed_historical_gateio_1h] | 0.000 (100) | 0.000 (138) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |

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
