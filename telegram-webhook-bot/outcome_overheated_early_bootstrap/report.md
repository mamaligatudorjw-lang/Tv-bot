# Bootstrap CI for overheated_early post-fix cohort

**Read-only analysis. No production logic, promotion switch, or SQLite row was changed.**

## Frozen source and cohort

- Source: `alerts_db_backups/alerts_20260828_185322.db`.
- Price-basis fix boundary: **2026-08-22T18:17:59+00:00**.
- Cohort rule: `ts_close >= fix timestamp` (explicit compatibility choice for the requested frozen n=121 cohort).
- Post-fix cohort: **n=121 unique signal IDs**; wins=44, losses=77.
- `ts_close` is not the causal creation-time field. The live fix protects the `demo_positions` persistence boundary; `ts_close` is used here only because the historical headline specified n=121 under that frozen split.

## Bootstrap method

- **20,000** percentile-bootstrap iterations, seed `20260830`.
- Each iteration resamples whole unique signal IDs with replacement.
- The same resample is used for avg R and WR-minus-breakeven, preserving their pairing.
- Breakeven WR for the 2:1 target/stop geometry: **33.3333%**.

| Cohort | n | Wins | Losses | WR | WR − BE | avg R | avg R 95% CI | WR 95% CI | delta 95% CI |
|---|---:|---:|---:|---:|---:|---:|---|---|---|
| post-fix by ts_close | 121 | 44 | 77 | 36.3636% | 3.0303 pp | 0.087708 | [-0.190885, 0.374357] | [28.0992%, 45.4545%] | [-5.2342, 12.1212] pp |

## Decision

- avg R CI crosses zero: **yes**.
- WR-minus-breakeven CI crosses zero: **yes**.
- Recommendation: **`do_not_promote_collect_more_data`**.
- The post-fix result is not statistically separated from breakeven, so the strategy should not be promoted on this cohort.

## Reconciliation with the earlier headline

The earlier review reported overall resolved n=227, WR=43.61%, and post-fix n=121, WR=35.64%, avg R=0.084.
That historical per-signal fixed-window audit is not present in the workspace. The frozen backup used here contains a different resolved context, so its observed values are reported honestly rather than relabeled as the old audit.

## Files

- `post_fix_cohort.csv`: exact 121 signal IDs and per-signal R values used.
- `bootstrap.csv`: observed metrics and percentile intervals.
- `report.json`: machine-readable provenance and results.
