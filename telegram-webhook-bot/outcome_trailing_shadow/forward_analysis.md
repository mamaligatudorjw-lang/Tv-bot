# Forward trailing shadow report

**Forward-only rolling report. Production execution was not changed.**

Freeze: **2026-08-26T11:50:58+00:00**.
Source path: **the same live price snapshots used by `check_demo_positions`**, not historical Gate.io 5m candles.
The original `demo_positions` row remains the baseline; shadow rows are independent and never change its barriers or status.

## Frozen configurations

| Strategy | Step | Activation | Pairs | Minimum | Bootstrap | Δ avg R | Mean CI 95% |
|---|---:|---|---:|---:|---|---:|---|
| overheated_24h | 8.0% | +0.5R | 8 | 20 | insufficient | -0.13488378 |  |
| ema_cross_confirmed | 6.0% | any_profit | 1 | 20 | insufficient | 0.0 |  |

Bootstrap is recomputed as a rolling update after new resolved pairs. No step or activation re-selection is performed.

Before both strategies reach the minimum sample, the report is explicitly insufficient and no CI is presented. A ready CI is a paired resampling interval for `shadow_r - baseline_r`; it is not a production approval.

```json
{
  "all_strategies_ready": false,
  "bootstrap_iterations": 20000,
  "bootstrap_seed": 20260826,
  "by_strategy": {
    "ema_cross_confirmed": {
      "n_pairs": 1,
      "ready_for_bootstrap": false
    },
    "overheated_24h": {
      "n_pairs": 8,
      "ready_for_bootstrap": false
    }
  },
  "freeze_utc": "2026-08-26T11:50:58+00:00",
  "generated_utc": "2026-08-27T20:46:05.496723+00:00",
  "historical_candles_used": false,
  "minimum_forward_pairs_per_strategy": 20,
  "rolling_update": true,
  "source": "live_price_snapshots_from_check_demo_positions",
  "total_resolved_pairs": 9
}
```
