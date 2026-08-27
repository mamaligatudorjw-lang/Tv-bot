# Forward TP-vs-SL — frozen `ema_cross_confirmed LONG` experiment

**Forward-shadow telemetry only. No trading filter, score, SL/TP, execution, or Telegram behavior is changed.**

- Frozen start: **2026-08-27T19:40:00+00:00** (`1787859600`)
- Frozen rule: **SL distance ≤ 3.55255%** predicts TP-first.
- Direction: **LONG only**; `ema_cross_confirmed SHORT` is excluded.
- Minimum verdict sample: **20 TP-first and 20 SL-first**.
- Generated: **2026-08-27T19:45:23+00:00**

## Current verdict

**INSUFFICIENT** — Wait for n≥20 TP-first and n≥20 SL-first in the forward sample.

| Cohort | Total | TP-first | SL-first | Unresolved | Resolved WR | avg R |
|---|---:|---:|---:|---:|---:|---:|
| No-rule baseline | 0 | 0 | 0 | 0 | —% | — |
| SL ≤ threshold (candidate) | 0 | 0 | 0 | 0 | —% | — |
| SL > threshold (control) | 0 | 0 | 0 | 0 | —% | — |

## Candidate confusion / precision

- Accuracy on resolved rows: **—**
- Precision TP: **—**
- Precision SL: **—**
- Matrix: TP→TP=0, TP→SL=0, SL→TP=0, SL→SL=0.

## Guardrails

- `unresolved` includes open/non-TP/SL rows and is never counted in WR or avg R.
- No verdict is allowed until both overall forward outcome classes have n≥20.
- The threshold and freeze timestamp are persisted in experiment metadata; startup fails if they drift.
- The tracker mirrors source outcomes and is independent from `demo_positions` decision-making.
