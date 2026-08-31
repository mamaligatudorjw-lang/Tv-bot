# Historical liquidation pattern review

- Source scan generated: **2026-08-31T09:47:19+00:00**
- `n` definition: **n counts only success_continuation, success_retest_hold, and failure_breakdown; no_outcome_in_window is excluded**

| Cohort | Events | Resolved n | Success | Failure | No outcome | Precondition unresolved | Success rate | Sufficiency |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| primary | 254 | 6 | 6 | 0 | 0 | 248 | 1.000 | descriptive_only |
| control | 1 | 0 | 0 | 0 | 0 | 1 | — | controls_any_n |
| control:BTCUSDT | 0 | 0 | 0 | 0 | 0 | 0 | — | controls_any_n |
| control:ETHUSDT | 1 | 0 | 0 | 0 | 0 | 1 | — | controls_any_n |
| control:SOLUSDT | 0 | 0 | 0 | 0 | 0 | 0 | — | controls_any_n |

`no_outcome_in_window` is excluded from resolved n and is never reclassified as success or failure.

The `Success` column is the sum of the distinct `success_continuation` and `success_retest_hold` buckets; both remain available as separate fields in `review.json` and `review.csv`.

## Control cohorts by symbol

| Symbol | Events | Resolved n | Continuation | Retest and hold | Failure | No outcome | Success rate |
|---|---:|---:|---:|---:|---:|---:|---:|
| BTCUSDT | 0 | 0 | 0 | 0 | 0 | 0 | — |
| ETHUSDT | 1 | 0 | 0 | 0 | 0 | 0 | — |
| SOLUSDT | 0 | 0 | 0 | 0 | 0 | 0 | — |

## Coverage validation

- Coverage rows: **28**
- Symbols with incomplete coverage: **11** (AKEUSDT, BTRUSDT, DRAMUSDT, ENAUSDT, PROMUSDT, SOXLUSDT, SPCXUSDT, TRUMPUSDT, WLDUSDT, ZECUSDT, ZHIPUUSDT)

| Control | 15m status | Liquidation status | 5m status | Reason |
|---|---|---|---|---|
| BTCUSDT | complete | not_requested | not_requested | — |
| ETHUSDT | complete | not_requested | complete | — |
| SOLUSDT | complete | not_requested | not_requested | — |


## Unresolved precondition stages

| Cohort | Stage | Count |
|---|---|---:|
| primary | correction_not_found_in_12h | 78 |
| primary | liquidation_burst_stage | 154 |
| primary | large_5m_flow_stage | 16 |
| primary | resolved_outcome | 6 |
| control | correction_not_found_in_12h | 1 |

Control stage breakdown by symbol:

| Control | Stage | Count |
|---|---|---:|
| BTCUSDT | — | 0 |
| ETHUSDT | correction_not_found_in_12h | 1 |
| SOLUSDT | — | 0 |

`liquidation_burst_stage` includes the $100,000 / 2% threshold and any incomplete liquidation-hour coverage. `large_5m_flow_stage` includes missing 5m coverage and not-found flow.
