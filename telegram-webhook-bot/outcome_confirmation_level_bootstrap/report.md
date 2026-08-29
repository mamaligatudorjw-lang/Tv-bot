# Bootstrap CI for overheated_confirmed confirmation levels

**Read-only analysis; no production, SQLite, gate, alert, or forward logic was changed.**

The bootstrap resamples individual resolved signal IDs, uses 20,000 percentile-bootstrap iterations, and reports both avg R and the WR-minus-breakeven delta. Level `2/3` is the n=20 cohort that motivated this check; level `3/3` is shown separately despite its n=6 insufficient sample.

| Level | n | Wins | Losses | WR | BE WR | avg R | avg R 95% CI | WR − BE | delta 95% CI | CI avg R crosses 0 | CI delta crosses 0 |
|---|---:|---:|---:|---:|---:|---:|---|---:|---|---|---|
| 2/3 | 20 | 5 | 15 | 25.0000% | 40.0000% | -0.419023 | [-0.877062, 0.111570] | -15.000000 pp | [-30.000000, 5.000000] pp | yes | yes |
| 3/3 | 6 | 2 | 4 | 33.3333% | 50.0000% | -0.388526 | [-1.110385, 0.333333] | -16.666667 pp | [-50.000000, 16.666667] pp | yes | yes |

## Guardrails

- `2/3` is exactly at the minimum n=20 threshold; the CI is evidence about uncertainty, not an automatic production rule.
- `3/3` has n=6 and remains insufficient regardless of the bootstrap interval.
- The bootstrap is in-sample and does not replace the planned forward check.
