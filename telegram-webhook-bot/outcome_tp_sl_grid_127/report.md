# TP/SL distance grid — frozen 127 signals

- Freeze: `2026-09-01T07:34:48+00:00`
- Source: Gate.io USDT futures, completed 1m candle history
- Baseline: each signal's actual persisted or parent-reconstructed TP/SL distance
- narrow-1: 75% of each signal's baseline distance
- narrow-2: 50% of each signal's baseline distance
- WR denominator: `resolved = WIN + LOSS` only
- Same-candle TP+SL: `ambiguous_same_candle`, excluded from WR

## Coverage

- Symbols: 60/60 with history
- Missing symbols: none

## Results

| Strategy | Variant | Total | Resolved | WIN | LOSS | No outcome yet | Ambiguous | WR | ΔWR vs baseline |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| overheated_24h | baseline | 53 | 46 | 20 | 26 | 7 | 0 | 43.48% | +0.00 pp |
| overheated_24h | narrow-1 | 53 | 47 | 18 | 29 | 6 | 0 | 38.30% | -5.18 pp |
| overheated_24h | narrow-2 | 53 | 50 | 21 | 29 | 3 | 0 | 42.00% | -1.48 pp |
| overheated_confirmed | baseline | 40 | 36 | 16 | 20 | 4 | 0 | 44.44% | +0.00 pp |
| overheated_confirmed | narrow-1 | 40 | 37 | 16 | 21 | 3 | 0 | 43.24% | -1.20 pp |
| overheated_confirmed | narrow-2 | 40 | 39 | 17 | 22 | 1 | 0 | 43.59% | -0.85 pp |
| ema_cross_confirmed | baseline | 34 | 13 | 4 | 9 | 21 | 0 | 30.77% | +0.00 pp |
| ema_cross_confirmed | narrow-1 | 34 | 15 | 2 | 13 | 19 | 0 | 13.33% | -17.44 pp |
| ema_cross_confirmed | narrow-2 | 34 | 21 | 3 | 18 | 13 | 0 | 14.29% | -16.48 pp |
