# Forward-shadow — frozen `ema_cross_confirmed` narrow-SL rule

**Read-only audit. No production scoring, gating, whitelist, TP/SL, execution, or Telegram behavior is changed.**

- Frozen threshold: **SL distance ≤ 3.567173%**.
- Cutoff: **2026-08-30T16:31:24+00:00** (`1788107484`); only `ts_open > cutoff` is included.
- Exploratory SHORT cohort is excluded; cohorts are overall and LONG.
- Sufficiency rule: **at least 20 TP and 20 SL outcomes** per cohort.
- The threshold and cutoff are inherited, not re-selected on forward data.
- Generated: **2026-08-30T18:16:17+00:00**

## Verdict

| Cohort | Forward n | Candidate n | Control n | TP mean risk | SL mean risk | TP−SL mean | 95% CI | p | Verdict |
|---|---:|---:|---:|---:|---:|---:|---|---:|---|
| overall | 0 | 0 | 0 | — | — | — | [—, —] | — | **INSUFFICIENT** |
| LONG | 0 | 0 | 0 | — | — | — | [—, —] | — | **INSUFFICIENT** |

## Candidate vs control descriptive metrics

| Cohort | Candidate TP/SL | Candidate TP-rate | Control TP/SL | Control TP-rate | Δ TP-rate pp |
|---|---:|---:|---:|---:|---:|
| overall | 0/0 | —% | 0/0 | —% | — |
| LONG | 0/0 | —% | 0/0 | —% | — |

## Verdict details

- **overall: INSUFFICIENT** — Requires at least 20 TP and 20 SL forward outcomes; observed TP=0, SL=0.
- **LONG: INSUFFICIENT** — Requires at least 20 TP and 20 SL forward outcomes; observed TP=0, SL=0.

## Guardrails

- The forward window contains only rows after the persisted exploratory cutoff; exploratory rows are not counted twice.
- Open, invalid, and unresolved rows are excluded from the resolved outcome statistics.
- `risk_pct` is derived from persisted entry and SL prices; it is not a reconstructed candle proxy.
- Even a CONFIRMED result is not permission to change production trading. Applying a rule requires a separate decision and checklist item.
