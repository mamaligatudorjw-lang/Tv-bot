# RSI threshold sensitivity — overheated_24h LONG shadow signals

Outcome sample: **271** signals with complete 24h windows and reconstructed RSI; 24 rows are excluded from outcome metrics (mostly immature windows).

| Cutoff | Cohort | n | TP-first | SL-first | Unresolved | Ambiguous | Resolved WR | Avg R |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 70 | RSI ≥70 | 228 | 91 | 122 | 11 | 4 | 42.7% | 0.259 |
| 70 | RSI <70 | 43 | 17 | 19 | 6 | 1 | 47.2% | 0.384 |
| 75 | RSI ≥75 | 189 | 74 | 103 | 9 | 3 | 41.8% | 0.232 |
| 75 | RSI <75 | 82 | 34 | 38 | 8 | 2 | 47.2% | 0.387 |
| 80 | RSI ≥80 | 126 | 46 | 74 | 4 | 2 | 38.3% | 0.128 |
| 80 | RSI <80 | 145 | 62 | 67 | 13 | 3 | 48.1% | 0.410 |
| 85 | RSI ≥85 | 82 | 33 | 46 | 1 | 2 | 41.8% | 0.239 |
| 85 | RSI <85 | 189 | 75 | 95 | 16 | 3 | 44.1% | 0.296 |

## Potential signal-volume proxy

- Real `overheated_24h` LONG positions in DB: **0**.
- Shadow `overheated_24h` LONG positions in DB: **312**.
- The proxy below is based on the analyzed rows with reconstructed RSI, not real production signals.

| RSI gate | Shadow rows ≥ gate | Change vs ≥70 |
|---:|---:|---:|
| ≥70 | 246 | 0.0% fewer than ≥70 |
| ≥75 | 204 | 17.1% fewer than ≥70 |
| ≥80 | 134 | 45.5% fewer than ≥70 |
| ≥85 | 88 | 64.2% fewer than ≥70 |

## Conclusion

- ≥75 improves separation only slightly versus ≥70; the lower-RSI cohort remains stronger.
- ≥80 is the clearest observed split in this sample, but ≥85 does not improve WR versus ≥80 and leaves a much smaller cohort.
- No trading threshold was changed. Real-volume impact needs a period with non-shadow production signals or a controlled shadow/live comparison.
