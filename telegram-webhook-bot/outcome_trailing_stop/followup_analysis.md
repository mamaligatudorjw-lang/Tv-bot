# Trailing-stop follow-up analysis

**Read-only. Production logic and the trading database were not changed.**

## Baseline reconciliation

The selection table uses the recorded market `exit_price` from `demo_positions`.
The path-simulation baseline uses the nominal TP level for recorded TP outcomes
and the nominal SL level for recorded SL outcomes. Therefore these averages are
not expected to be identical when exits overshoot or undershoot their levels.

| Strategy | n | Selection avg R | Recomputed selection avg R | Path baseline avg R | Observed − path | Exit/level mismatches |
|---|---:|---:|---:|---:|---:|---:|
| high_rejection_short | 53 | 0.246461 | 0.246461 | 0.188679 | 0.057782 | 27 |
| ema_cross | 344 | 0.287268 | 0.287268 | 0.325581 | -0.038313 | 221 |
| overheated_24h | 408 | 0.186497 | 0.186497 | 0.205392 | -0.018895 | 265 |

The reconciliation is systematic in definition, not necessarily in direction:
actual exits can improve TP trades or worsen SL trades depending on slippage and
the recorded close price. `selection_avg_r_recomputed` matches the ranking-table
definition, while `path_baseline_avg_r` is the level-based counterfactual.

## Paired bootstrap at the best in-sample step

Each bootstrap resample keeps the per-trade pairing and resamples the vector
`alt_best_step_r - baseline_r`. The interval is a 95% percentile bootstrap CI.

| Strategy | Best step | n | Mean ΔR | Mean CI 95% | Median ΔR | Median CI 95% | CI excludes 0? |
|---|---:|---:|---:|---|---:|---|---|
| overheated_24h | 8.0% | 408 | 0.112954 | [0.033429, 0.19387] | 0.0 | [0.0, 0.0] | mean=True; median=False |
| ema_cross_confirmed | 6.0% | 129 | 0.159025 | [0.020349, 0.297777] | 0.0 | [0.0, 0.0] | mean=True; median=False |
| overheated_early | 8.0% | 175 | 0.078069 | [-0.073744, 0.228698] | 0.0 | [0.0, 0.0] | mean=False; median=False |
| ema_cross | 5.0% | 344 | 0.033637 | [-0.058587, 0.125416] | 0.0 | [0.0, 0.0] | mean=False; median=False |
| overheated_confirmed | 8.0% | 212 | 0.042305 | [-0.071253, 0.155663] | 0.0 | [0.0, 0.0] | mean=False; median=False |

A CI excluding zero would only indicate that the paired in-sample difference
is unlikely to be explained by resampling noise under this fixed sample and
model. It does **not** replace an out-of-sample test on a separate time period.
Bootstrap assesses whether this observed effect looks like noise; it does not
establish that the trailing rule will work going forward. The best-step choice
itself is also subject to grid-search and selection bias.

```json
{
  "bootstrap_iterations": 20000,
  "bootstrap_seed": 20260826,
  "bootstrap_strategies": [
    "overheated_24h",
    "ema_cross_confirmed",
    "overheated_early",
    "ema_cross",
    "overheated_confirmed"
  ],
  "input": "outcome_trailing_stop",
  "path_rows": 12992,
  "positions_loaded": 1856,
  "reconciliation_strategies": [
    "high_rejection_short",
    "ema_cross",
    "overheated_24h"
  ]
}
```
