# Forward-аудит SHORT — ema_cross_confirmed / ema_cross

**Read-only отчёт. Allowlist, Telegram-видимость, scoring, TP/SL, cooldown и execution не изменяются.**

- Cutoff: **2026-08-28T11:36:58+00:00** (`1787917018`)
- Generated: **2026-08-28T14:55:59+00:00**
- Scope: `is_shadow=1`, `direction=SHORT`, resolved `tp/sl`, `ts_open > cutoff`.
- Verdict threshold: **20 resolved trades per strategy**.
- Rule: negative unrounded forward avg R = **CONFIRMED**; positive = **REFUTED**; exact zero = **AMBIGUOUS**.

## Verdict and comparison

| Strategy | Forward n | TP | SL | WR | Forward avg R | In-sample avg R | Verdict |
|---|---:|---:|---:|---:|---:|---:|---|
| ema_cross_confirmed | 0 | 0 | 0 | —% | — | -0.410295 (-0.41) | **INSUFFICIENT** |
| ema_cross | 0 | 0 | 0 | —% | — | -0.178233 (-0.18) | **INSUFFICIENT** |

## Per-strategy reasons

- **ema_cross_confirmed: INSUFFICIENT** — Requires at least 20 resolved forward SHORT trades; observed n=0. Total resolved rows: 0.
- **ema_cross: INSUFFICIENT** — Requires at least 20 resolved forward SHORT trades; observed n=0. Total resolved rows: 0.

## Guardrails

- The cutoff is persisted in `cutoff.json` and is reused on later runs unless `--cutoff` explicitly replaces it.
- Unresolved/open/TTL rows are excluded from the forward sample and never counted in WR or avg R.
- In-sample baselines are displayed for comparison only; they are not a second verdict threshold.
- No verdict can be `CONFIRMED`, `REFUTED`, or `AMBIGUOUS` before n≥20.
