# Trailing-stop on dynamic WR≥35% cohorts

**Read-only in-sample analysis. Production logic, Telegram state, and SQLite were not changed.**

This report applies the WR gate only to the frozen signal IDs used by the original #136 grid search. It does not load new current rows and does not reuse a forward/OOS window.

## Method

- Target strategies: `overheated_24h, ema_cross_confirmed`.
- Cohort: `strategy × direction × regime`.
- A signal passes only when at least 20 same-cohort results were already resolved and historical WR is **≥ 35%**.
- Only a preceding position with `ts_close <= current ts_open` contributes to the historical WR; the current result is appended after its decision.
- Regime comes from the saved BTC 4h/EMA50 snapshot and keeps `unknown` explicit.
- Trailing values are reused from the frozen #136 5m grid with the original fixed SL/TP baseline and TP ceiling.

## Frozen input

```json
{
  "frozen_source": "outcome_trailing_stop/trailing_rows.csv",
  "frozen_source_generated_utc": "2026-08-26T06:09:56.963415+00:00",
  "frozen_target_rows": 3759,
  "frozen_target_ids": 537,
  "frozen_target_strategies": [
    "ema_cross_confirmed",
    "overheated_24h"
  ],
  "frozen_steps_pct": [
    2.0,
    3.0,
    4.0,
    5.0,
    6.0,
    8.0,
    10.0
  ]
}
```

## Filter counts by cohort

| Strategy | Direction | Regime | Total | Passed | Excluded | Pass % | No history | Insufficient | Below 35% | Status |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| ema_cross_confirmed | LONG | bear | 0 | 0 | 0 | None% | 0 | 0 | 0 | insufficient |
| ema_cross_confirmed | LONG | bull | 89 | 42 | 47 | 47.191% | 4 | 43 | 0 | ready |
| ema_cross_confirmed | SHORT | bear | 0 | 0 | 0 | None% | 0 | 0 | 0 | insufficient |
| ema_cross_confirmed | SHORT | bull | 40 | 0 | 40 | 0.0% | 22 | 10 | 8 | ready |
| overheated_24h | LONG | bear | 57 | 12 | 45 | 21.0526% | 1 | 25 | 19 | ready |
| overheated_24h | LONG | bull | 351 | 251 | 100 | 71.51% | 1 | 27 | 72 | ready |
| overheated_24h | SHORT | bear | 0 | 0 | 0 | None% | 0 | 0 | 0 | insufficient |
| overheated_24h | SHORT | bull | 0 | 0 | 0 | None% | 0 | 0 | 0 | insufficient |

## Filter-only effect on fixed baseline

This is a descriptive selection comparison: filtered baseline versus all baseline. It is not a causal estimate because the filter selects a subset.

| Strategy | All n | Filtered n | Excluded | All avg R | Filtered avg R | Δ avg R | All WR | Filtered WR | Δ WR pp |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| overheated_24h | 408 | 263 | 145 | 0.205392 | 0.163498 | -0.041894 | 40.4412% | 38.7833% | -1.6579 |
| ema_cross_confirmed | 129 | 42 | 87 | 0.686047 | 0.619048 | -0.066999 | 56.5891% | 54.7619% | -1.8272 |

## Fixed baseline versus trailing on the same filtered sample

| Sample | Strategy | Step | n | Baseline avg R | Trailing avg R | Δ avg R | Baseline WR | Trailing WR | Trail exits | Status |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| all | overheated_24h | 2.0% | 408/408 | 0.205392 | 0.159742 | -0.04565 | 40.4412% | 49.5098% | 331 | ready |
| filtered | overheated_24h | 2.0% | 263/263 | 0.163498 | 0.15325 | -0.010248 | 38.7833% | 48.6692% | 217 | ready |
| all | overheated_24h | 3.0% | 408/408 | 0.205392 | 0.179896 | -0.025497 | 40.4412% | 48.2843% | 304 | ready |
| filtered | overheated_24h | 3.0% | 263/263 | 0.163498 | 0.211809 | 0.048311 | 38.7833% | 49.4297% | 195 | ready |
| all | overheated_24h | 4.0% | 408/408 | 0.205392 | 0.264326 | 0.058934 | 40.4412% | 47.0588% | 250 | ready |
| filtered | overheated_24h | 4.0% | 263/263 | 0.163498 | 0.31996 | 0.156462 | 38.7833% | 50.9506% | 158 | ready |
| all | overheated_24h | 5.0% | 408/408 | 0.205392 | 0.284312 | 0.078919 | 40.4412% | 47.0588% | 212 | ready |
| filtered | overheated_24h | 5.0% | 263/263 | 0.163498 | 0.351357 | 0.187859 | 38.7833% | 50.5703% | 132 | ready |
| all | overheated_24h | 6.0% | 408/408 | 0.205392 | 0.288473 | 0.083081 | 40.4412% | 45.3431% | 180 | ready |
| filtered | overheated_24h | 6.0% | 263/263 | 0.163498 | 0.351336 | 0.187838 | 38.7833% | 48.6692% | 112 | ready |
| all | overheated_24h | 8.0% | 408/408 | 0.205392 | 0.318347 | 0.112954 | 40.4412% | 44.3627% | 102 | ready |
| filtered | overheated_24h | 8.0% | 263/263 | 0.163498 | 0.379422 | 0.215924 | 38.7833% | 46.7681% | 65 | ready |
| all | overheated_24h | 10.0% | 408/408 | 0.205392 | 0.290234 | 0.084842 | 40.4412% | 42.402% | 71 | ready |
| filtered | overheated_24h | 10.0% | 263/263 | 0.163498 | 0.346286 | 0.182788 | 38.7833% | 43.7262% | 46 | ready |
| all | ema_cross_confirmed | 2.0% | 129/129 | 0.686047 | 0.305402 | -0.380644 | 56.5891% | 53.4884% | 100 | ready |
| filtered | ema_cross_confirmed | 2.0% | 42/42 | 0.619048 | 0.228768 | -0.390279 | 54.7619% | 52.381% | 32 | ready |
| all | ema_cross_confirmed | 3.0% | 129/129 | 0.686047 | 0.487267 | -0.19878 | 56.5891% | 58.1395% | 83 | ready |
| filtered | ema_cross_confirmed | 3.0% | 42/42 | 0.619048 | 0.391539 | -0.227509 | 54.7619% | 57.1429% | 28 | ready |
| all | ema_cross_confirmed | 4.0% | 129/129 | 0.686047 | 0.754031 | 0.067985 | 56.5891% | 63.5659% | 62 | ready |
| filtered | ema_cross_confirmed | 4.0% | 42/42 | 0.619048 | 0.682795 | 0.063747 | 54.7619% | 59.5238% | 20 | ready |
| all | ema_cross_confirmed | 5.0% | 129/129 | 0.686047 | 0.803926 | 0.117879 | 56.5891% | 64.3411% | 52 | ready |
| filtered | ema_cross_confirmed | 5.0% | 42/42 | 0.619048 | 0.797029 | 0.177981 | 54.7619% | 64.2857% | 17 | ready |
| all | ema_cross_confirmed | 6.0% | 129/129 | 0.686047 | 0.845071 | 0.159025 | 56.5891% | 62.0155% | 45 | ready |
| filtered | ema_cross_confirmed | 6.0% | 42/42 | 0.619048 | 0.835795 | 0.216747 | 54.7619% | 61.9048% | 14 | ready |
| all | ema_cross_confirmed | 8.0% | 129/129 | 0.686047 | 0.807299 | 0.121252 | 56.5891% | 61.2403% | 35 | ready |
| filtered | ema_cross_confirmed | 8.0% | 42/42 | 0.619048 | 0.765261 | 0.146214 | 54.7619% | 59.5238% | 10 | ready |
| all | ema_cross_confirmed | 10.0% | 129/129 | 0.686047 | 0.810627 | 0.124581 | 56.5891% | 59.6899% | 23 | ready |
| filtered | ema_cross_confirmed | 10.0% | 42/42 | 0.619048 | 0.826175 | 0.207127 | 54.7619% | 59.5238% | 6 | ready |

The `Δ avg R` and `Δ total R` columns compare trailing against the fixed baseline for exactly the same filtered signal IDs. `n` is shown as `path-usable / selected`; groups below 20 usable rows are insufficient.

## Guardrails

- This is an in-sample result and remains subject to grid-search selection bias.
- It does not justify enabling trailing-stop in production.
- No forward/shadow tracker was created and no `demo_positions` row was updated.
- Small, empty, and `unknown` cohorts remain explicit rather than being hidden.
