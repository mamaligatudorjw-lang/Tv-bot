# TP/SL distance grid — frozen 127 signals

- Freeze: `2026-09-01T07:34:48+00:00`
- Source: Gate.io USDT futures, completed 1m candle history
- Baseline: persisted levels where available; confirmed gaps are explicitly reconstructed
- narrow-1: 75% of each signal's baseline distance
- narrow-2: 50% of each signal's baseline distance
- WR denominator: `resolved = WIN + LOSS` only
- Same-candle TP+SL: `ambiguous_same_candle`, excluded from WR
- Sufficiency tier: `full` if resolved n≥20; `descriptive_only` if 5≤n<20; `case_log_only` if n<5

## Coverage

- Symbols: 60/60 with history
- Missing symbols: none

## Results

| Strategy | Variant | Total | Resolved | WIN | LOSS | No outcome yet | Ambiguous | WR | ΔWR vs baseline | Tier |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| overheated_24h | baseline | 53 | 46 | 20 | 26 | 7 | 0 | 43.48% | +0.00 pp | full |
| overheated_24h | narrow-1 | 53 | 47 | 18 | 29 | 6 | 0 | 38.30% | -5.18 pp | full |
| overheated_24h | narrow-2 | 53 | 50 | 21 | 29 | 3 | 0 | 42.00% | -1.48 pp | full |
| overheated_confirmed | baseline | 40 | 36 | 16 | 20 | 4 | 0 | 44.44% | +0.00 pp | full |
| overheated_confirmed | narrow-1 | 40 | 37 | 16 | 21 | 3 | 0 | 43.24% | -1.20 pp | full |
| overheated_confirmed | narrow-2 | 40 | 39 | 17 | 22 | 1 | 0 | 43.59% | -0.85 pp | full |
| ema_cross_confirmed | baseline | 34 | 13 | 4 | 9 | 21 | 0 | 30.77% | +0.00 pp | descriptive_only |
| ema_cross_confirmed | narrow-1 | 34 | 15 | 2 | 13 | 19 | 0 | 13.33% | -17.44 pp | descriptive_only |
| ema_cross_confirmed | narrow-2 | 34 | 21 | 3 | 18 | 13 | 0 | 14.29% | -16.48 pp | full |

## Confirmed baseline provenance

- Confirmed signals without their own persisted row: 19/74
- Exact persisted confirmed levels: 55
- Parent-persisted reconstruction: 9
- Historical 4h ATR reconstruction: 10

A duplicate-blocked confirmed event is still present in telemetry, but `_demo_open_position` returns before inserting its own `demo_positions` row. The exact runtime confirmed TP/SL is therefore not independently persisted for those events.

### Five validation examples

| Signal | Strategy | Entry | Baseline SL% | Baseline TP% | Parent check SL% | Parent check TP% | Check |
|---|---|---:|---:|---:|---:|---:|---|
| ema_cross_confirmed-log-1788206434.616-UBERUSDT-SHORT | ema_cross_confirmed | 75.62 | 2.50% | 5.00% | 2.50% | 5.00% | signal_price_within_0.5pct |
| ema_cross_confirmed-log-1788206435.278-HUSDT-SHORT | ema_cross_confirmed | 0.07295 | 6.88% | 13.76% | 6.88% | 13.76% | signal_price_within_0.5pct |
| overheated_confirmed-log-1788197431.662-0GUSDT-LONG | overheated_confirmed | 0.2344 | 5.11% | 10.23% | 5.11% | 10.23% | signal_price_within_0.5pct |
| overheated_confirmed-log-1788106759.967-SKRUSDT-LONG | overheated_confirmed | 0.0165 | 6.81% | 13.62% | 6.81% | 13.62% | signal_price_within_0.5pct |
| overheated_confirmed-log-1788097528.418-DOSUSDT-LONG | overheated_confirmed | 0.3304 | 7.87% | 15.75% | 6.05% | 12.10% | nearest_prior_parent_only |

The parent check is an independent comparison only when it says `signal_price_within_0.5pct`; otherwise it is a nearest-parent diagnostic, not proof of the exact runtime confirmed level.
