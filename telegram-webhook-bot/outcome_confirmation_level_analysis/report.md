# Confirmation-level cohort analysis

**Read-only descriptive analysis. Production, alert gates, alert format, and SQLite were not changed.**

The confirmation level is reconstructed from persisted prices: TP/SL distance 2.0x = 1/3, 1.5x = 2/3, and 1.0x = 3/3. Result R uses the persisted entry, SL, and resolved exit. BTC regime uses the last completed BTC 4h candle available at signal time and its EMA50.

- Resolved rows: `448`.
- Strategies: `overheated_confirmed, ema_cross_confirmed`.
- Regime coverage: `83` of `448` rows were absent from the existing snapshot and remain `unknown`.
- Minimum cohort size: `20`; smaller cells are **insufficient**.

## Blended vs level-split cohorts

| Sample | Strategy | Direction | Regime | Level | n | Wins | Losses | WR | avg R | R:R | BE WR | WR − BE | Status |
|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| blended | overheated_confirmed | LONG | bull | all | 221 | 95 | 126 | 42.9864% | 0.283322 |  |  |  | ready |
| confirmation_level | overheated_confirmed | LONG | bull | 1/3 | 200 | 89 | 111 | 44.5000% | 0.353675 | 2.000000 | 33.3333% | 11.1667 pp | ready |
| confirmation_level | overheated_confirmed | LONG | bull | 2/3 | 16 | 4 | 12 | 25.0000% | -0.424351 | 1.500000 | 40.0000% | -15.0000 pp | insufficient |
| confirmation_level | overheated_confirmed | LONG | bull | 3/3 | 5 | 2 | 3 | 40.0000% | -0.266231 | 1.000000 | 50.0000% | -10.0000 pp | insufficient |
| blended | overheated_confirmed | LONG | bear | all | 0 | 0 | 0 |  |  |  |  |  | insufficient |
| confirmation_level | overheated_confirmed | LONG | bear | 1/3 | 0 | 0 | 0 |  |  | 2.000000 | 33.3333% |  | insufficient |
| confirmation_level | overheated_confirmed | LONG | bear | 2/3 | 0 | 0 | 0 |  |  | 1.500000 | 40.0000% |  | insufficient |
| confirmation_level | overheated_confirmed | LONG | bear | 3/3 | 0 | 0 | 0 |  |  | 1.000000 | 50.0000% |  | insufficient |
| blended | overheated_confirmed | LONG | unknown | all | 52 | 15 | 37 | 28.8462% | -0.156907 |  |  |  | ready |
| confirmation_level | overheated_confirmed | LONG | unknown | 1/3 | 47 | 14 | 33 | 29.7872% | -0.118475 | 2.000000 | 33.3333% | -3.5461 pp | ready |
| confirmation_level | overheated_confirmed | LONG | unknown | 2/3 | 4 | 1 | 3 | 25.0000% | -0.397711 | 1.500000 | 40.0000% | -15.0000 pp | insufficient |
| confirmation_level | overheated_confirmed | LONG | unknown | 3/3 | 1 | 0 | 1 | 0.0000% | -1.000000 | 1.000000 | 50.0000% | -50.0000 pp | insufficient |
| blended | overheated_confirmed | SHORT | bull | all | 0 | 0 | 0 |  |  |  |  |  | insufficient |
| confirmation_level | overheated_confirmed | SHORT | bull | 1/3 | 0 | 0 | 0 |  |  | 2.000000 | 33.3333% |  | insufficient |
| confirmation_level | overheated_confirmed | SHORT | bull | 2/3 | 0 | 0 | 0 |  |  | 1.500000 | 40.0000% |  | insufficient |
| confirmation_level | overheated_confirmed | SHORT | bull | 3/3 | 0 | 0 | 0 |  |  | 1.000000 | 50.0000% |  | insufficient |
| blended | overheated_confirmed | SHORT | bear | all | 0 | 0 | 0 |  |  |  |  |  | insufficient |
| confirmation_level | overheated_confirmed | SHORT | bear | 1/3 | 0 | 0 | 0 |  |  | 2.000000 | 33.3333% |  | insufficient |
| confirmation_level | overheated_confirmed | SHORT | bear | 2/3 | 0 | 0 | 0 |  |  | 1.500000 | 40.0000% |  | insufficient |
| confirmation_level | overheated_confirmed | SHORT | bear | 3/3 | 0 | 0 | 0 |  |  | 1.000000 | 50.0000% |  | insufficient |
| blended | overheated_confirmed | SHORT | unknown | all | 0 | 0 | 0 |  |  |  |  |  | insufficient |
| confirmation_level | overheated_confirmed | SHORT | unknown | 1/3 | 0 | 0 | 0 |  |  | 2.000000 | 33.3333% |  | insufficient |
| confirmation_level | overheated_confirmed | SHORT | unknown | 2/3 | 0 | 0 | 0 |  |  | 1.500000 | 40.0000% |  | insufficient |
| confirmation_level | overheated_confirmed | SHORT | unknown | 3/3 | 0 | 0 | 0 |  |  | 1.000000 | 50.0000% |  | insufficient |
| blended | ema_cross_confirmed | LONG | bull | all | 94 | 67 | 27 | 71.2766% | 1.180178 |  |  |  | ready |
| confirmation_level | ema_cross_confirmed | LONG | bull | 1/3 | 89 | 65 | 24 | 73.0337% | 1.250125 | 2.000000 | 33.3333% | 39.7004 pp | ready |
| confirmation_level | ema_cross_confirmed | LONG | bull | 2/3 | 1 | 1 | 0 | 100.0000% | 1.675556 | 1.500000 | 40.0000% | 60.0000 pp | insufficient |
| confirmation_level | ema_cross_confirmed | LONG | bull | 3/3 | 4 | 1 | 3 | 25.0000% | -0.500000 | 1.000000 | 50.0000% | -25.0000 pp | insufficient |
| blended | ema_cross_confirmed | LONG | bear | all | 0 | 0 | 0 |  |  |  |  |  | insufficient |
| confirmation_level | ema_cross_confirmed | LONG | bear | 1/3 | 0 | 0 | 0 |  |  | 2.000000 | 33.3333% |  | insufficient |
| confirmation_level | ema_cross_confirmed | LONG | bear | 2/3 | 0 | 0 | 0 |  |  | 1.500000 | 40.0000% |  | insufficient |
| confirmation_level | ema_cross_confirmed | LONG | bear | 3/3 | 0 | 0 | 0 |  |  | 1.000000 | 50.0000% |  | insufficient |
| blended | ema_cross_confirmed | LONG | unknown | all | 25 | 5 | 20 | 20.0000% | -0.439435 |  |  |  | ready |
| confirmation_level | ema_cross_confirmed | LONG | unknown | 1/3 | 25 | 5 | 20 | 20.0000% | -0.439435 | 2.000000 | 33.3333% | -13.3333 pp | ready |
| confirmation_level | ema_cross_confirmed | LONG | unknown | 2/3 | 0 | 0 | 0 |  |  | 1.500000 | 40.0000% |  | insufficient |
| confirmation_level | ema_cross_confirmed | LONG | unknown | 3/3 | 0 | 0 | 0 |  |  | 1.000000 | 50.0000% |  | insufficient |
| blended | ema_cross_confirmed | SHORT | bull | all | 50 | 11 | 39 | 22.0000% | -0.458501 |  |  |  | ready |
| confirmation_level | ema_cross_confirmed | SHORT | bull | 1/3 | 50 | 11 | 39 | 22.0000% | -0.458501 | 2.000000 | 33.3333% | -11.3333 pp | ready |
| confirmation_level | ema_cross_confirmed | SHORT | bull | 2/3 | 0 | 0 | 0 |  |  | 1.500000 | 40.0000% |  | insufficient |
| confirmation_level | ema_cross_confirmed | SHORT | bull | 3/3 | 0 | 0 | 0 |  |  | 1.000000 | 50.0000% |  | insufficient |
| blended | ema_cross_confirmed | SHORT | bear | all | 0 | 0 | 0 |  |  |  |  |  | insufficient |
| confirmation_level | ema_cross_confirmed | SHORT | bear | 1/3 | 0 | 0 | 0 |  |  | 2.000000 | 33.3333% |  | insufficient |
| confirmation_level | ema_cross_confirmed | SHORT | bear | 2/3 | 0 | 0 | 0 |  |  | 1.500000 | 40.0000% |  | insufficient |
| confirmation_level | ema_cross_confirmed | SHORT | bear | 3/3 | 0 | 0 | 0 |  |  | 1.000000 | 50.0000% |  | insufficient |
| blended | ema_cross_confirmed | SHORT | unknown | all | 6 | 2 | 4 | 33.3333% | -0.004234 |  |  |  | insufficient |
| confirmation_level | ema_cross_confirmed | SHORT | unknown | 1/3 | 6 | 2 | 4 | 33.3333% | -0.004234 | 2.000000 | 33.3333% | 0.0000 pp | insufficient |
| confirmation_level | ema_cross_confirmed | SHORT | unknown | 2/3 | 0 | 0 | 0 |  |  | 1.500000 | 40.0000% |  | insufficient |
| confirmation_level | ema_cross_confirmed | SHORT | unknown | 3/3 | 0 | 0 | 0 |  |  | 1.000000 | 50.0000% |  | insufficient |

## Hypothesis by strategy

The verdict is deliberately per strategy. `supported` requires both late levels (2/3 and 3/3) to have at least 20 resolved trades, WR below their own breakeven, and negative avg R. `partial_support` means only some ready late levels meet that rule.

| Strategy | Blended n | 1/3 n | 1/3 avg R | 1/3 Δ pp | 2/3 n | 2/3 avg R | 2/3 Δ pp | 3/3 n | 3/3 avg R | 3/3 Δ pp | Negative late levels | Verdict |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| overheated_confirmed | 273 | 247 | 0.263833 | 8.367072 | 20 | -0.419023 | -15.000000 | 6 | -0.388526 | -16.666667 | 2/3 | partial_support |
| ema_cross_confirmed | 175 | 170 | 0.454852 | 15.490196 | 1 | 1.675556 | 60.000000 | 4 | -0.500000 | -25.000000 |  | insufficient |

## Interpretation guardrails

- This is descriptive in-sample history, not a causal test and not a production gate recommendation.
- Blended rows can hide level-specific negative expectancy because their breakeven WR is not a single number when R:R differs by level.
- Empty LONG/SHORT or bull/bear cells are retained as `insufficient`, not dropped.
- `unknown` regime rows are retained and are not silently treated as bull or bear.
- The report excludes `open`, `ttl_expired`, and other unresolved statuses by design.
