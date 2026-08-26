# Fixed-step forward validation

**Read-only. Production logic and the trading database were not changed.**

Grid cutoff: **2026-08-26T06:09:56+00:00**. Test generated: **2026-08-26T07:19:07.504466+00:00**.

The trailing steps were fixed before this forward slice: `overheated_24h=8%`, `ema_cross_confirmed=6%`. No grid search or step re-selection was performed.

## Available forward results

| Strategy | Fixed step | n resolved | Path usable | Baseline total R | Alt total R | Δ total R | Δ avg R | Baseline WR | Alt positive n |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| overheated_24h | 8.0% | 2 | 2 | 1.0 | 1.0 | 0.0 | 0.0 | 50.0% | 1 |
| ema_cross_confirmed | 6.0% | 0 | 0 |  |  |  |  | % | 0 |

This is currently an **insufficient preliminary forward sample**, not a decision-quality OOS test: the database contains only the newly resolved positions after the cutoff. A strategy with zero rows has no OOS result.
A historical slice before the cutoff would not be a clean unseen holdout because the preceding grid-search used the full stored history.

## In-sample outliers at the fixed steps

These are diagnostics from the original in-sample path simulation, not additional OOS observations.

### overheated_24h (8.0%) — top positive ΔR

| Symbol | Baseline R | Alt R | ΔR | Alt outcome |
|---|---:|---:|---:|---|
| BULLAUSDT | -1.0 | 2.0 | 3.0 | tp |
| BIOUSDT | -1.0 | 2.0 | 3.0 | tp |
| IMXUSDT | -1.0 | 2.0 | 3.0 | tp |
| BERAUSDT | -1.0 | 2.0 | 3.0 | tp |
| THETAUSDT | -1.0 | 2.0 | 3.0 | tp |

### overheated_24h (8.0%) — top negative ΔR

| Symbol | Baseline R | Alt R | ΔR | Alt outcome |
|---|---:|---:|---:|---|
| LAUSDT | 2.0 | -1.0 | -3.0 | sl |
| SOXSUSDT | 2.0 | -1.0 | -3.0 | sl |
| VVVUSDT | 2.0 | -1.0 | -3.0 | sl |
| GPSUSDT | 2.0 | -0.635039 | -2.635039 | trail_stop |
| HEMIUSDT | 2.0 | -0.480839 | -2.480839 | trail_stop |

### ema_cross_confirmed (6.0%) — top positive ΔR

| Symbol | Baseline R | Alt R | ΔR | Alt outcome |
|---|---:|---:|---:|---|
| KORUUSDT | -1.0 | 2.0 | 3.0 | tp |
| BANANAS31USDT | -1.0 | 2.0 | 3.0 | tp |
| TRUMPUSDT | -1.0 | 1.0 | 2.0 | tp |
| 币安人生USDT | -1.0 | 0.996304 | 1.996304 | trail_stop |
| TACUSDT | -1.0 | 0.78783 | 1.78783 | trail_stop |

### ema_cross_confirmed (6.0%) — top negative ΔR

| Symbol | Baseline R | Alt R | ΔR | Alt outcome |
|---|---:|---:|---:|---|
| IDOLUSDT | 2.0 | -0.694487 | -2.694487 | trail_stop |
| TSTBSCUSDT | 2.0 | -0.41335 | -2.41335 | trail_stop |
| GRVTUSDT | 2.0 | -0.38806 | -2.38806 | trail_stop |
| FARTCOINUSDT | 2.0 | -0.2934 | -2.2934 | trail_stop |
| SOXLUSDT | 2.0 | 0.377263 | -1.622737 | trail_stop |

## OOS interpretation

Even a positive result on this forward slice would not establish that the rule works going forward until there are enough independent observations.
The fixed-step test avoids re-optimizing on this slice, but it still needs a predefined minimum sample, a longer time span, and separate monitoring of fees, slippage, market regime, and 5m intrabar ambiguity.

```json
{
  "cutoff_utc": "2026-08-26T06:09:56+00:00",
  "fixed_steps_pct": {
    "ema_cross_confirmed": 6.0,
    "overheated_24h": 8.0
  },
  "forward_positions": 2,
  "forward_symbols_loaded": 1,
  "forward_symbols_requested": 1,
  "generated_utc": "2026-08-26T07:19:07.504466+00:00",
  "historical_grid_generated_utc": "2026-08-26T06:09:56.963415+00:00",
  "outlier_source_rows": 12992,
  "path_interval": "5m",
  "symbol_fetch_failures": {}
}
```
