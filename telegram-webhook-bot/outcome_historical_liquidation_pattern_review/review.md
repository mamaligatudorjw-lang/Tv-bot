# Historical liquidation pattern review

- Source scan generated: **2026-08-31T09:47:19+00:00**
- `n` definition: **n counts only success_continuation, success_retest_hold, and failure_breakdown; no_outcome_in_window is excluded**

| Cohort | Events | Resolved n | Success | Failure | No outcome | Precondition unresolved | Success rate | Sufficiency |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| primary | 254 | 6 | 6 | 0 | 0 | 248 | 1.000 | descriptive_only |
| control | 1 | 0 | 0 | 0 | 0 | 1 | — | controls_any_n |

`no_outcome_in_window` is excluded from resolved n and is never reclassified as success or failure.

## Unresolved precondition stages

| Cohort | Stage | Count |
|---|---|---:|
| primary | correction_not_found_in_12h | 78 |
| primary | liquidation_burst_stage | 154 |
| primary | large_5m_flow_stage | 16 |
| primary | resolved_outcome | 6 |
| control | correction_not_found_in_12h | 1 |

`liquidation_burst_stage` includes the $100,000 / 2% threshold and any incomplete liquidation-hour coverage. `large_5m_flow_stage` includes missing 5m coverage and not-found flow.
