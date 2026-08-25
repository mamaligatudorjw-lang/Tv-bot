# high_rejection_short condition analysis

Read-only analysis. No production code, thresholds, or database rows were changed.

## Reconciliation

- Loaded **79** rows; eligible fixed-window rows: **71**.
- TP-first: **15**; SL-first: **27**; unresolved/ambiguous: **29**.
- Runtime log matches: **79/79**.
- `range_pct`, `% from high`, and volume are available only as rounded runtime log values; they are not persisted in `demo_positions`.
- The current implementation has no RSI gate for `high_rejection_short`; RSI below is diagnostic reconstruction only.
- Exact TP-vs-SL statistical comparison is **not permitted yet** because TP-first has fewer than 20 rows.

## Weekly resolved WR

| ISO week | n | TP | SL | unresolved | ambiguous | resolved WR | n≥20 |
|---|---:|---:|---:|---:|---:|---:|:---:|
| 2026-W34 | 60 | 13 | 18 | 29 | 0 | 41.94% | yes |
| 2026-W35 | 11 | 2 | 9 | 0 | 0 | 18.18% | no |

## Time quartiles

| Quartile | n | TP | SL | unresolved | ambiguous | resolved WR | n≥20 |
|---|---:|---:|---:|---:|---:|---:|:---:|
| Q1 | 17 | 3 | 12 | 2 | 0 | 20.0% | no |
| Q2 | 18 | 3 | 5 | 10 | 0 | 37.5% | no |
| Q3 | 18 | 2 | 1 | 15 | 0 | 66.67% | no |
| Q4 | 18 | 7 | 9 | 2 | 0 | 43.75% | no |

## TP-first vs SL-first condition values

| Field | TP-first n / median (mean) | SL-first n / median (mean) | valid n≥20 comparison |
|---|---:|---:|:---:|
| range_pct_log | 15 / 43.6 (41.686667) | 27 / 30.9 (55.662963) | no |
| dist_from_high_pct_log | 15 / 4.8 (5.546667) | 27 / 5.3 (10.055556) | no |
| volume_ratio_log | 15 / 2.2 (2.326667) | 27 / 2.1 (2.362963) | no |
| rsi_15m_reconstructed | 15 / 48.855989 (52.917435) | 27 / 52.542373 (54.853081) | no |
| bearish_body_pct | 15 / 2.686081 (4.316316) | 27 / 2.174928 (3.918004) | no |

## Decision

No production filter or threshold change is justified. Collect more outcomes until both TP-first and SL-first cohorts meet n≥20, then rerun this exact analysis with persisted/runtime provenance kept separate.
