# Partial TP50 follow-up

**Read-only report over the frozen partial-TP artifact; no SQLite, production, or forward state was changed.**

The source simulation includes no fees or slippage. With a purely proportional fee, splitting one full close into two 50% closes does not add commission on the same total exit notional. The sensitivity columns instead show an explicitly hypothetical adverse cost on the second 50% close only, averaged over all 408 signals.

| Sample | Step | n | ΣR | avg R | WR | TP branch | Floor exits | Trailing exits | Branch rows | Extra 5bps cost ΣR | Extra 5bps cost avg R | avg R after extra 5bps | Extra 10bps cost ΣR | Extra 10bps cost avg R | avg R after extra 10bps |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| baseline_fixed |  | 408 | 83.800000 | 0.205392 | 40.4412% | 165 | 0 | 0 | 0 | 0.000000 | 0.000000 | 0.205392 | 0.000000 | 0.000000 | 0.205392 |
| partial_tp50 | 2.0 | 408 | 95.530509 | 0.234143 | 40.4412% | 165 | 140 | 25 | 165 | 1.223799 | 0.003000 | 0.231144 | 2.447597 | 0.005999 | 0.228144 |
| partial_tp50 | 3.0 | 408 | 93.432080 | 0.229000 | 40.4412% | 165 | 145 | 20 | 165 | 1.222749 | 0.002997 | 0.226003 | 2.445499 | 0.005994 | 0.223006 |
| partial_tp50 | 4.0 | 408 | 98.578503 | 0.241614 | 40.4412% | 165 | 151 | 14 | 165 | 1.225323 | 0.003003 | 0.238611 | 2.450645 | 0.006006 | 0.235607 |
| partial_tp50 | 5.0 | 408 | 98.172684 | 0.240619 | 40.4412% | 165 | 153 | 12 | 165 | 1.225120 | 0.003003 | 0.237617 | 2.450240 | 0.006005 | 0.234614 |
| partial_tp50 | 6.0 | 408 | 97.828858 | 0.239777 | 40.4412% | 165 | 154 | 11 | 165 | 1.224948 | 0.003002 | 0.236774 | 2.449896 | 0.006005 | 0.233772 |
| partial_tp50 | 8.0 | 408 | 103.532466 | 0.253756 | 40.4412% | 165 | 155 | 10 | 165 | 1.227800 | 0.003009 | 0.250747 | 2.455599 | 0.006019 | 0.247737 |
| partial_tp50 | 10.0 | 408 | 103.874596 | 0.254595 | 40.4412% | 165 | 157 | 8 | 165 | 1.227971 | 0.003010 | 0.251585 | 2.455941 | 0.006019 | 0.248575 |

## Branch totals

- TP branch-step rows: `1155`.
- Floor exits at the TP floor: `1055`.
- Real trailing exits above the floor: `100`.

The floor and trailing counts sum to the TP branch count for every step. Floor exits have the same modeled R as the baseline TP exit; trailing exits are the branch-step combinations with a modeled exit above the TP floor.

## Commission/slippage interpretation

- The source artifact has no explicit commission or slippage model.
- A proportional fee charged on notional does not become larger merely because the exit is split: `fee(50%) + fee(50%) = fee(100%)`.
- The sensitivity is intentionally conservative as an incremental second-leg execution cost and should not be added to an all-in fee model a second time.
