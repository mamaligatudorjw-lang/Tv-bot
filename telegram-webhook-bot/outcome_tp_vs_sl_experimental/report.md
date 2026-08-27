# TP vs SL — strong-signal experimental analysis

**Read-only report. No production logic, filters, score, SL/TP, or SQLite rows were changed.**

- Scope: resolved shadow `demo_positions` rows only; loaded **955**.
- Runtime log matches: **904** (94.66%).
- Minimum comparison cohort: **20 TP-first and 20 SL-first**.
- `WR` is TP / (TP + SL); `avg R` uses recorded exit price and original entry-to-SL risk.
- Any rule below is in-sample: the threshold was selected and scored on the same rows.

## Feature provenance

| Field | Provenance | Coverage | Meaning |
|---|---|---:|---|
| SL distance from entry (%) (`risk_pct`) | exact_persisted_derived | 100.0% | abs(entry_price - sl_price) / entry_price |
| TP distance from entry (%) (`reward_pct`) | exact_persisted_derived | 100.0% | abs(tp_price - entry_price) / entry_price |
| TP/SL distance ratio (`reward_risk`) | exact_persisted_derived | 100.0% | reward_pct / risk_pct |
| Directional entry move from signal (%) (`entry_vs_signal_pct`) | exact_persisted_derived | 100.0% | direction-adjusted entry_price vs signal_price |
| EMA cross gap (%) (`ema_gap_pct_log`) | runtime_log_rounded | 40.5% | EMA(9)-EMA(21) gap emitted by the signal path |
| Overheated 24h move (%) (`overheated_pct24_log`) | runtime_log_rounded | 15.8% | pct24 emitted by the overheated early signal path |
| Overheated RSI (`overheated_rsi_log`) | runtime_log_rounded | 15.8% | RSI emitted by the overheated early signal path |
| Confirmation volume ratio (x) (`confirmation_volume_ratio_log`) | runtime_log_rounded | 38.3% | completed-candle volume / 10-bar average |
| Confirmation number (`confirmation_number_log`) | runtime_log_exact_integer | 38.3% | confirmation count emitted by continuation telemetry |
| Confirmation age (minutes) (`confirmation_age_min_log`) | runtime_log_exact_integer | 38.3% | age of the parent signal at confirmation |

## Current strategy performance

| Strategy | Cohort | n | TP | SL | WR resolved | avg R | Status |
|---|---|---:|---:|---:|---:|---:|---|
| ema_cross_confirmed | overall | 144 | 78 | 66 | 54.17% | 0.6112 | ready |
| ema_cross_confirmed | LONG | 94 | 67 | 27 | 71.28% | 1.1802 | ready |
| ema_cross_confirmed | SHORT | 50 | 11 | 39 | 22.00% | -0.4585 | INSUFFICIENT (<20 in TP or SL) |
| overheated_early | overall | 202 | 91 | 111 | 45.05% | 0.4148 | ready |
| overheated_early | LONG | 202 | 91 | 111 | 45.05% | 0.4148 | ready |
| ema_cross | overall | 387 | 166 | 221 | 42.89% | 0.2441 | ready |
| ema_cross | LONG | 226 | 116 | 110 | 51.33% | 0.5266 | ready |
| ema_cross | SHORT | 161 | 50 | 111 | 31.06% | -0.1523 | ready |
| overheated_confirmed | overall | 222 | 95 | 127 | 42.79% | 0.2775 | ready |
| overheated_confirmed | LONG | 222 | 95 | 127 | 42.79% | 0.2775 | ready |

## TP-first vs SL-first comparisons

### ema_cross_confirmed

#### overall

| Feature | TP median (n) | SL median (n) | TP−SL median | Cliff’s δ | 95% CI δ | p |
|---|---:|---:|---:|---:|---|---:|
| SL distance from entry (%) [exact_persisted_derived] | 2.823 (78) | 6.564 (66) | -3.740 | -0.591 | [-0.734, -0.434] | 0.0012 |
| TP distance from entry (%) [exact_persisted_derived] | 5.647 (78) | 13.128 (66) | -7.481 | -0.565 | [-0.712, -0.406] | 0.0012 |
| TP/SL distance ratio [exact_persisted_derived] | 2.000 (78) | 2.000 (66) | 0.000 | 0.071 | [-0.012, 0.163] | 0.1286 |
| Directional entry move from signal (%) [exact_persisted_derived] | 0.000 (78) | 0.000 (66) | 0.000 | -0.140 | [-0.258, -0.037] | 0.0100 |
| EMA cross gap (%) [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated 24h move (%) [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated RSI [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation volume ratio (x) [runtime_log_rounded] | 2.750 (78) | 2.350 (66) | 0.400 | 0.167 | [-0.013, 0.357] | 0.0936 |
| Confirmation number [runtime_log_exact_integer] | 1.000 (78) | 1.000 (66) | 0.000 | -0.020 | [-0.091, 0.038] | 0.4582 |
| Confirmation age (minutes) [runtime_log_exact_integer] | 44.000 (78) | 58.500 (66) | -14.500 | -0.164 | [-0.350, 0.027] | 0.0999 |

#### Experimental candidate

- **SL distance from entry (%) ≤ 3.40695 predicts TP**
- Provenance: **exact_persisted_derived**
- In-sample accuracy: **0.743056**, balanced accuracy: **0.752331**
- Precision TP: **0.847458**; precision SL: **0.670588**
- This is experimental and requires forward-shadow validation; it is not a production rule.

#### LONG

| Feature | TP median (n) | SL median (n) | TP−SL median | Cliff’s δ | 95% CI δ | p |
|---|---:|---:|---:|---:|---|---:|
| SL distance from entry (%) [exact_persisted_derived] | 2.722 (67) | 4.310 (27) | -1.588 | -0.425 | [-0.619, -0.208] | 0.0050 |
| TP distance from entry (%) [exact_persisted_derived] | 5.445 (67) | 8.213 (27) | -2.768 | -0.355 | [-0.570, -0.103] | 0.0087 |
| TP/SL distance ratio [exact_persisted_derived] | 2.000 (67) | 2.000 (27) | 0.000 | 0.155 | [0.010, 0.316] | 0.0337 |
| Directional entry move from signal (%) [exact_persisted_derived] | 0.000 (67) | 0.000 (27) | 0.000 | -0.027 | [-0.148, 0.060] | 0.5618 |
| EMA cross gap (%) [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated 24h move (%) [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated RSI [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation volume ratio (x) [runtime_log_rounded] | 2.800 (67) | 2.100 (27) | 0.700 | 0.291 | [0.032, 0.541] | 0.0225 |
| Confirmation number [runtime_log_exact_integer] | 1.000 (67) | 1.000 (27) | 0.000 | -0.083 | [-0.218, 0.030] | 0.0712 |
| Confirmation age (minutes) [runtime_log_exact_integer] | 48.000 (67) | 113.000 (27) | -65.000 | -0.405 | [-0.641, -0.167] | 0.0012 |

#### Experimental candidate

- **SL distance from entry (%) ≤ 3.55255 predicts TP**
- Provenance: **exact_persisted_derived**
- In-sample accuracy: **0.723404**, balanced accuracy: **0.739635**
- Precision TP: **0.886792**; precision SL: **0.512195**
- This is experimental and requires forward-shadow validation; it is not a production rule.

#### SHORT

**INSUFFICIENT:** TP=11, SL=39; no feature conclusion or candidate is allowed.

### overheated_early

#### overall

| Feature | TP median (n) | SL median (n) | TP−SL median | Cliff’s δ | 95% CI δ | p |
|---|---:|---:|---:|---:|---|---:|
| SL distance from entry (%) [exact_persisted_derived] | 4.845 (91) | 5.728 (111) | -0.883 | -0.145 | [-0.303, 0.022] | 0.0899 |
| TP distance from entry (%) [exact_persisted_derived] | 9.691 (91) | 11.456 (111) | -1.765 | -0.145 | [-0.300, 0.020] | 0.0874 |
| TP/SL distance ratio [exact_persisted_derived] | 2.000 (91) | 2.000 (111) | 0.000 | 0.077 | [-0.060, 0.222] | 0.2959 |
| Directional entry move from signal (%) [exact_persisted_derived] | 0.000 (91) | 0.000 (111) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| EMA cross gap (%) [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated 24h move (%) [runtime_log_rounded] | 18.500 (59) | 18.400 (92) | 0.100 | -0.034 | [-0.220, 0.149] | 0.7204 |
| Overheated RSI [runtime_log_rounded] | 66.000 (59) | 66.750 (92) | -0.750 | -0.109 | [-0.299, 0.090] | 0.2659 |
| Confirmation volume ratio (x) [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation number [runtime_log_exact_integer] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation age (minutes) [runtime_log_exact_integer] | — (0) | — (0) | — | — | [—, —] | — |

#### Experimental candidate

**NO CANDIDATE:** no feature met the predeclared effect, permutation, confidence-interval, coverage, and balanced-accuracy criteria.

#### LONG

| Feature | TP median (n) | SL median (n) | TP−SL median | Cliff’s δ | 95% CI δ | p |
|---|---:|---:|---:|---:|---|---:|
| SL distance from entry (%) [exact_persisted_derived] | 4.845 (91) | 5.728 (111) | -0.883 | -0.145 | [-0.301, 0.004] | 0.0762 |
| TP distance from entry (%) [exact_persisted_derived] | 9.691 (91) | 11.456 (111) | -1.765 | -0.145 | [-0.314, 0.028] | 0.0737 |
| TP/SL distance ratio [exact_persisted_derived] | 2.000 (91) | 2.000 (111) | 0.000 | 0.077 | [-0.071, 0.216] | 0.2871 |
| Directional entry move from signal (%) [exact_persisted_derived] | 0.000 (91) | 0.000 (111) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| EMA cross gap (%) [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated 24h move (%) [runtime_log_rounded] | 18.500 (59) | 18.400 (92) | 0.100 | -0.034 | [-0.222, 0.150] | 0.7453 |
| Overheated RSI [runtime_log_rounded] | 66.000 (59) | 66.750 (92) | -0.750 | -0.109 | [-0.279, 0.075] | 0.2472 |
| Confirmation volume ratio (x) [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation number [runtime_log_exact_integer] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation age (minutes) [runtime_log_exact_integer] | — (0) | — (0) | — | — | [—, —] | — |

#### Experimental candidate

**NO CANDIDATE:** no feature met the predeclared effect, permutation, confidence-interval, coverage, and balanced-accuracy criteria.

### ema_cross

#### overall

| Feature | TP median (n) | SL median (n) | TP−SL median | Cliff’s δ | 95% CI δ | p |
|---|---:|---:|---:|---:|---|---:|
| SL distance from entry (%) [exact_persisted_derived] | 2.692 (166) | 3.502 (221) | -0.809 | -0.231 | [-0.343, -0.115] | 0.0012 |
| TP distance from entry (%) [exact_persisted_derived] | 5.385 (166) | 7.004 (221) | -1.619 | -0.231 | [-0.349, -0.114] | 0.0012 |
| TP/SL distance ratio [exact_persisted_derived] | 2.000 (166) | 2.000 (221) | 0.000 | -0.046 | [-0.152, 0.073] | 0.4257 |
| Directional entry move from signal (%) [exact_persisted_derived] | 0.000 (166) | 0.000 (221) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| EMA cross gap (%) [runtime_log_rounded] | 0.311 (166) | 0.336 (221) | -0.025 | -0.058 | [-0.167, 0.054] | 0.3296 |
| Overheated 24h move (%) [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated RSI [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation volume ratio (x) [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation number [runtime_log_exact_integer] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation age (minutes) [runtime_log_exact_integer] | — (0) | — (0) | — | — | [—, —] | — |

#### Experimental candidate

**NO CANDIDATE:** no feature met the predeclared effect, permutation, confidence-interval, coverage, and balanced-accuracy criteria.

#### LONG

| Feature | TP median (n) | SL median (n) | TP−SL median | Cliff’s δ | 95% CI δ | p |
|---|---:|---:|---:|---:|---|---:|
| SL distance from entry (%) [exact_persisted_derived] | 2.331 (116) | 3.067 (110) | -0.736 | -0.222 | [-0.365, -0.077] | 0.0050 |
| TP distance from entry (%) [exact_persisted_derived] | 4.662 (116) | 6.134 (110) | -1.472 | -0.222 | [-0.372, -0.074] | 0.0050 |
| TP/SL distance ratio [exact_persisted_derived] | 2.000 (116) | 2.000 (110) | 0.000 | -0.025 | [-0.160, 0.122] | 0.7516 |
| Directional entry move from signal (%) [exact_persisted_derived] | 0.000 (116) | 0.000 (110) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| EMA cross gap (%) [runtime_log_rounded] | 0.271 (116) | 0.364 (110) | -0.093 | -0.160 | [-0.311, -0.012] | 0.0362 |
| Overheated 24h move (%) [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated RSI [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation volume ratio (x) [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation number [runtime_log_exact_integer] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation age (minutes) [runtime_log_exact_integer] | — (0) | — (0) | — | — | [—, —] | — |

#### Experimental candidate

**NO CANDIDATE:** no feature met the predeclared effect, permutation, confidence-interval, coverage, and balanced-accuracy criteria.

#### SHORT

| Feature | TP median (n) | SL median (n) | TP−SL median | Cliff’s δ | 95% CI δ | p |
|---|---:|---:|---:|---:|---|---:|
| SL distance from entry (%) [exact_persisted_derived] | 3.774 (50) | 4.434 (111) | -0.660 | -0.124 | [-0.318, 0.080] | 0.2272 |
| TP distance from entry (%) [exact_persisted_derived] | 7.548 (50) | 8.869 (111) | -1.321 | -0.124 | [-0.314, 0.072] | 0.2235 |
| TP/SL distance ratio [exact_persisted_derived] | 2.000 (50) | 2.000 (111) | 0.000 | -0.069 | [-0.254, 0.140] | 0.4082 |
| Directional entry move from signal (%) [exact_persisted_derived] | 0.000 (50) | 0.000 (111) | 0.000 | 0.000 | [0.000, 0.000] | 1.0000 |
| EMA cross gap (%) [runtime_log_rounded] | 0.400 (50) | 0.320 (111) | 0.080 | 0.142 | [-0.056, 0.338] | 0.1361 |
| Overheated 24h move (%) [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated RSI [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation volume ratio (x) [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation number [runtime_log_exact_integer] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation age (minutes) [runtime_log_exact_integer] | — (0) | — (0) | — | — | [—, —] | — |

#### Experimental candidate

**NO CANDIDATE:** no feature met the predeclared effect, permutation, confidence-interval, coverage, and balanced-accuracy criteria.

### overheated_confirmed

#### overall

| Feature | TP median (n) | SL median (n) | TP−SL median | Cliff’s δ | 95% CI δ | p |
|---|---:|---:|---:|---:|---|---:|
| SL distance from entry (%) [exact_persisted_derived] | 3.818 (95) | 3.947 (127) | -0.129 | -0.101 | [-0.255, 0.052] | 0.2210 |
| TP distance from entry (%) [exact_persisted_derived] | 7.326 (95) | 7.827 (127) | -0.501 | -0.075 | [-0.235, 0.072] | 0.3333 |
| TP/SL distance ratio [exact_persisted_derived] | 2.000 (95) | 2.000 (127) | 0.000 | 0.049 | [-0.047, 0.138] | 0.2996 |
| Directional entry move from signal (%) [exact_persisted_derived] | 0.000 (95) | 0.000 (127) | 0.000 | -0.050 | [-0.136, 0.042] | 0.2871 |
| EMA cross gap (%) [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated 24h move (%) [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated RSI [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation volume ratio (x) [runtime_log_rounded] | 2.100 (95) | 2.200 (127) | -0.100 | -0.163 | [-0.312, -0.008] | 0.0325 |
| Confirmation number [runtime_log_exact_integer] | 1.000 (95) | 1.000 (127) | 0.000 | -0.054 | [-0.125, 0.019] | 0.2360 |
| Confirmation age (minutes) [runtime_log_exact_integer] | 12.000 (95) | 16.000 (127) | -4.000 | -0.000 | [-0.151, 0.154] | 1.0000 |

#### Experimental candidate

**NO CANDIDATE:** no feature met the predeclared effect, permutation, confidence-interval, coverage, and balanced-accuracy criteria.

#### LONG

| Feature | TP median (n) | SL median (n) | TP−SL median | Cliff’s δ | 95% CI δ | p |
|---|---:|---:|---:|---:|---|---:|
| SL distance from entry (%) [exact_persisted_derived] | 3.818 (95) | 3.947 (127) | -0.129 | -0.101 | [-0.239, 0.062] | 0.2010 |
| TP distance from entry (%) [exact_persisted_derived] | 7.326 (95) | 7.827 (127) | -0.501 | -0.075 | [-0.234, 0.067] | 0.3571 |
| TP/SL distance ratio [exact_persisted_derived] | 2.000 (95) | 2.000 (127) | 0.000 | 0.049 | [-0.043, 0.135] | 0.3383 |
| Directional entry move from signal (%) [exact_persisted_derived] | 0.000 (95) | 0.000 (127) | 0.000 | -0.050 | [-0.144, 0.041] | 0.3121 |
| EMA cross gap (%) [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated 24h move (%) [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Overheated RSI [runtime_log_rounded] | — (0) | — (0) | — | — | [—, —] | — |
| Confirmation volume ratio (x) [runtime_log_rounded] | 2.100 (95) | 2.200 (127) | -0.100 | -0.163 | [-0.319, -0.003] | 0.0399 |
| Confirmation number [runtime_log_exact_integer] | 1.000 (95) | 1.000 (127) | 0.000 | -0.054 | [-0.132, 0.022] | 0.2622 |
| Confirmation age (minutes) [runtime_log_exact_integer] | 12.000 (95) | 16.000 (127) | -4.000 | -0.000 | [-0.155, 0.157] | 1.0000 |

#### Experimental candidate

**NO CANDIDATE:** no feature met the predeclared effect, permutation, confidence-interval, coverage, and balanced-accuracy criteria.

## Telegram marker decision

No Telegram marker is enabled by this analysis. A marker may only be added behind an explicit default-off control after a candidate is deliberately accepted for forward-shadow testing; it must remain informational and cannot affect signal generation.

## Guardrails

- Runtime-log values are rounded at emission time and are not exact raw market snapshots.
- Missing fields are left missing; no current ticker is substituted for historical signal-time data.
- Statistical summaries are exploratory and subject to multiple-comparison bias.
- No candidate is forward-validated by this report.
