# Historical liquidation pattern review

- Source scan generated: **2026-08-31T09:47:19+00:00**
- `n` definition: **n counts only success_continuation, success_retest_hold, and failure_breakdown; no_outcome_in_window is excluded**

| Cohort | Events | Resolved n | Success | Failure | No outcome | Precondition unresolved | Success rate | Sufficiency |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| primary | 254 | 6 | 6 | 0 | 0 | 248 | 1.000 | descriptive_only |
| control | 1 | 0 | 0 | 0 | 0 | 1 | — | controls_any_n |

`no_outcome_in_window` is excluded from resolved n and is never reclassified as success or failure.
