# Forward trailing shadow report

**Forward-only rolling report. Production execution was not changed.**

Freeze: **2026-08-26T11:50:58+00:00**.
Source path: **the same live price snapshots used by `check_demo_positions`**, not historical Gate.io 5m candles.
The original `demo_positions` row remains the baseline; shadow rows are independent and never change its barriers or status.

## Frozen configurations

| Strategy | Step | Activation | Pairs | Minimum | Bootstrap | Δ avg R | Mean CI 95% |
|---|---:|---|---:|---:|---|---:|---|
| overheated_24h | 8.0% | +0.5R | 76 | 20 | ready | -0.05112021 | [-0.25449684, 0.1458342] |
| ema_cross_confirmed | 6.0% | any_profit | 31 | 20 | ready | -0.1825268 | [-0.49946129, 0.08593613] |

Bootstrap is recomputed as a rolling update after new resolved pairs. No step or activation re-selection is performed.

Before both strategies reach the minimum sample, the report is explicitly insufficient and no CI is presented. A ready CI is a paired resampling interval for `shadow_r - baseline_r`; it is not a production approval.

```json
{
  "all_strategies_ready": true,
  "bootstrap_iterations": 20000,
  "bootstrap_seed": 20260826,
  "by_strategy": {
    "ema_cross_confirmed": {
      "n_pairs": 31,
      "ready_for_bootstrap": true
    },
    "overheated_24h": {
      "n_pairs": 76,
      "ready_for_bootstrap": true
    }
  },
  "freeze_utc": "2026-08-26T11:50:58+00:00",
  "generated_utc": "2026-08-30T07:35:32.113765+00:00",
  "historical_candles_used": false,
  "minimum_forward_pairs_per_strategy": 20,
  "rolling_update": true,
  "source": "live_price_snapshots_from_check_demo_positions",
  "total_resolved_pairs": 107
}
```
