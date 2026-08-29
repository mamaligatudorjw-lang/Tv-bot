# Partial TP 50% + trailing on remainder

**Read-only in-sample analysis. Production, SQLite, demo positions, and forward-shadow state were not changed.**

The sample is frozen from #136/#150 and candle processing stops at the original frozen artifact cutoff. A baseline SL signal never enters the partial branch. A baseline TP signal closes 50% at TP and trails the other 50% with a hard TP floor.

The TP-trigger candle is not reused to update the trailing stop; trailing updates begin on the next completed 5m candle. Stop-first semantics are used when a candle touches the current stop.

## Coverage and baseline

- Frozen target: `408` unique `overheated_24h` signals.
- Baseline TP reach: `165/408` (40.4412%).
- Historical candle cutoff: `2026-08-26T06:09:56.963415+00:00`.
- Symbols requested/loaded: `203/203`.

## Grid summary

| Sample | Step | n resolved | Signals | TP branch | Unresolved | ΣR | avg R | WR | TP reach | Trail exits | Floor exits | Status |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| baseline_fixed |  | 408 | 408 | 165 | 0 | 83.800000 | 0.205392 | 40.4412% | 40.4412% | 0 | 0 | ready |
| partial_tp50 | 2.0 | 408 | 408 | 165 | 0 | 95.530509 | 0.234143 | 40.4412% | 40.4412% | 25 | 140 | ready |
| partial_tp50 | 3.0 | 408 | 408 | 165 | 0 | 93.432080 | 0.229000 | 40.4412% | 40.4412% | 20 | 145 | ready |
| partial_tp50 | 4.0 | 408 | 408 | 165 | 0 | 98.578503 | 0.241614 | 40.4412% | 40.4412% | 14 | 151 | ready |
| partial_tp50 | 5.0 | 408 | 408 | 165 | 0 | 98.172684 | 0.240619 | 40.4412% | 40.4412% | 12 | 153 | ready |
| partial_tp50 | 6.0 | 408 | 408 | 165 | 0 | 97.828858 | 0.239777 | 40.4412% | 40.4412% | 11 | 154 | ready |
| partial_tp50 | 8.0 | 408 | 408 | 165 | 0 | 103.532466 | 0.253756 | 40.4412% | 40.4412% | 10 | 155 | ready |
| partial_tp50 | 10.0 | 408 | 408 | 165 | 0 | 103.874596 | 0.254595 | 40.4412% | 40.4412% | 8 | 157 | ready |

## Paired bootstrap CI

The paired delta is partial-TP total R minus the fixed baseline R for the same signal. Resampling is by unique signal ID. Unresolved-at-cutoff rows are excluded from the realized paired metric and remain visible in `n_unresolved`.

| Step | n paired | Unresolved | Δavg R | 95% CI | Width | Crosses 0 |
|---:|---:|---:|---:|---:|---:|---|
| 2% | 408 | 0 | 0.028751 | [0.014046, 0.047092] | 0.033046 | no |
| 3% | 408 | 0 | 0.023608 | [0.010575, 0.039404] | 0.028830 | no |
| 4% | 408 | 0 | 0.036222 | [0.009613, 0.076009] | 0.066395 | no |
| 5% | 408 | 0 | 0.035227 | [0.010392, 0.073336] | 0.062944 | no |
| 6% | 408 | 0 | 0.034384 | [0.009801, 0.070708] | 0.060906 | no |
| 8% | 408 | 0 | 0.048364 | [0.012973, 0.094565] | 0.081592 | no |
| 10% | 408 | 0 | 0.049202 | [0.010337, 0.102118] | 0.091780 | no |

## Limitations

- This is an in-sample simulation and does not justify production enablement.
- Positions whose second-half trailing exit was not observed by the frozen cutoff are censored, not silently treated as profitable or as TP exits.
- Historical 5m OHLC cannot resolve intrabar ordering beyond the conservative stop-first rule.
- No fees or slippage are added beyond the source #136 R semantics.
