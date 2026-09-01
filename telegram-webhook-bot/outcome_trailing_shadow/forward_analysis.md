# Forward trailing shadow report

**Forward-only rolling report. Production execution was not changed.**

Freeze: **2026-08-26T11:50:58+00:00**.
Source path: **the same live price snapshots used by `check_demo_positions`**, not historical Gate.io 5m candles.
The original `demo_positions` row remains the baseline; shadow rows are independent and never change its barriers or status.

## Frozen configurations

| Strategy | Step | Activation | Pairs | Minimum | Bootstrap | Δ avg R | Mean CI 95% |
|---|---:|---|---:|---:|---|---:|---|
| overheated_24h | 8.0% | +0.5R | 129 | 20 | ready | -0.01943709 | [-0.18816987, 0.13739422] |
| ema_cross_confirmed | 6.0% | any_profit | 61 | 20 | ready | -0.00749402 | [-0.23617465, 0.20155353] |

Bootstrap is recomputed as a rolling update after new resolved pairs. No step or activation re-selection is performed.

Before both strategies reach the minimum sample, the report is explicitly insufficient and no CI is presented. A ready CI is a paired resampling interval for `shadow_r - baseline_r`; it is not a production approval.

```json
{
  "all_strategies_ready": true,
  "bootstrap_iterations": 20000,
  "bootstrap_seed": 20260826,
  "by_strategy": {
    "ema_cross_confirmed": {
      "n_pairs": 61,
      "ready_for_bootstrap": true
    },
    "overheated_24h": {
      "n_pairs": 129,
      "ready_for_bootstrap": true
    }
  },
  "freeze_utc": "2026-08-26T11:50:58+00:00",
  "generated_utc": "2026-09-01T06:43:01.276532+00:00",
  "historical_candles_used": false,
  "minimum_forward_pairs_per_strategy": 20,
  "rolling_update": true,
  "source": "live_price_snapshots_from_check_demo_positions",
  "total_resolved_pairs": 190
}
```
