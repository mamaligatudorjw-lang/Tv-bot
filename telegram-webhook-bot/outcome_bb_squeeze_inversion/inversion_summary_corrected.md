# BB squeeze direction inversion — corrected read-only analysis

Barrier touch uses candle high/low; unresolved R uses the last close. TP-first is +2R, SL-first is -1R; ambiguous has no R.

| Original | Tested | n | TP-first | SL-first | Unresolved | Ambiguous | Reconciliation | WR resolved | Avg R |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| SHORT | LONG | 325 | 122 | 166 | 36 | 1 | 325 | 42.36% | 0.2868 |
| LONG | SHORT | 348 | 104 | 210 | 33 | 1 | 348 | 33.12% | 0.0509 |

Unresolved R ranges:
- former SHORT → LONG: -0.9244 to 1.4534 (mean 0.4145)
- former LONG → SHORT: -0.5683 to 1.668 (mean 0.5954)
