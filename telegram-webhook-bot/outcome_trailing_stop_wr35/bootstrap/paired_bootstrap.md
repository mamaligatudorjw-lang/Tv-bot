# WR≥35% trailing paired bootstrap stability

**Read-only descriptive analysis over the frozen #150 artifacts.**
No SQLite, production logic, or forward window was used.

## Method

- Paired value: `trailing alt_r - filtered fixed baseline_r`.
- Resampling unit: one unique signal ID; the seven step rows for one signal are never sampled independently.
- CI: 95% percentile bootstrap, 20,000 iterations per strategy/step.
- This does not correct the best-of-7 selection bias; it reports every step and compares the selected step descriptively with adjacent steps.

## Results

| Strategy | Step | n signals | Δavg R | 95% CI | Width | Crosses 0 | Neighbors overlap | Same sign |
|---|---:|---:|---:|---:|---:|---|---|---|
| ema_cross_confirmed | 2% | 42 | -0.390279 | [-0.763187, -0.023326] | 0.739861 | no | yes | yes |
| ema_cross_confirmed | 3% | 42 | -0.227509 | [-0.551121, 0.079325] | 0.630446 | yes | yes | no |
| ema_cross_confirmed | 4% | 42 | 0.063747 | [-0.247631, 0.368072] | 0.615702 | yes | yes | no |
| ema_cross_confirmed | 5% | 42 | 0.177981 | [-0.106783, 0.458798] | 0.565581 | yes | yes | yes |
| ema_cross_confirmed | 6% | 42 | 0.216747 | [0.001476, 0.449535] | 0.448059 | no | yes | yes |
| ema_cross_confirmed | 8% | 42 | 0.146214 | [-0.044597, 0.365397] | 0.409994 | yes | yes | yes |
| ema_cross_confirmed | 10% | 42 | 0.207127 | [0.043529, 0.425545] | 0.382017 | no | yes | yes |
| overheated_24h | 2% | 263 | -0.010248 | [-0.174299, 0.150435] | 0.324735 | yes | yes | no |
| overheated_24h | 3% | 263 | 0.048311 | [-0.106370, 0.204948] | 0.311319 | yes | yes | no |
| overheated_24h | 4% | 263 | 0.156462 | [0.016128, 0.297689] | 0.281561 | no | yes | yes |
| overheated_24h | 5% | 263 | 0.187859 | [0.061020, 0.314856] | 0.253836 | no | yes | yes |
| overheated_24h | 6% | 263 | 0.187838 | [0.072148, 0.305697] | 0.233548 | no | yes | yes |
| overheated_24h | 8% | 263 | 0.215924 | [0.113906, 0.322852] | 0.208946 | no | yes | yes |
| overheated_24h | 10% | 263 | 0.182788 | [0.080351, 0.290114] | 0.209764 | no | yes | yes |

## Selected-step gate

A selected step is not recommended for the new forward window when its CI crosses zero or its CI overlaps an adjacent step. The neighbor comparison is descriptive, not a multiple-comparison correction.

| Strategy | Selected step | CI crosses 0 | Neighbor CI overlap | Recommendation |
|---|---:|---|---|---|
| overheated_24h | 8% | no | yes | do_not_spend_forward_not_distinct_from_neighbors |
| ema_cross_confirmed | 6% | no | yes | do_not_spend_forward_not_distinct_from_neighbors |

## Interpretation guardrail

A positive CI on a selected in-sample step is not evidence that the step generalizes: the step was selected from seven candidates. Confidence in a forward candidate requires both a CI above zero and a meaningful separation from neighboring steps under this descriptive check.
