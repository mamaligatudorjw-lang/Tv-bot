# Historical liquidation threshold rerun comparison

**Read-only comparison. Production thresholds and behavior were not changed.**

## Reports and windows

- Baseline: `outcome_historical_liquidation_pattern`; generated 2026-08-31T09:47:19+00:00; SHA-256 `86acbeb4ae0d5488782e5871877857f27717d49a89aef81a18c8ea05b4213e4a`.
- Rerun: `outcome_historical_liquidation_pattern_rerun_20260831T1400Z`; generated 2026-08-31T14:14:25+00:00; SHA-256 `75969e7ba47b0eeee68891646c24c8f62ff4d31fc9be2b0f1d2e0e531cc800c1`.
- Baseline window: 2026-06-01T09:15:00+00:00 → 2026-08-31T09:15:00+00:00.
- Rerun window: 2026-06-01T09:15:00+00:00 → 2026-08-31T14:00:00+00:00.
- Fixed grid: `$25k/0.5%`, `$50k/1%`, `$100k/2%` baseline, `$150k/3%`.

## Universe and semantic event rows

- Primary contracts: 25 → 24; added: BE_USDT; removed: AKE_USDT, TAO_USDT.
- Event keys `(symbol, cohort, pump_ts)`: 255 → 181.
- Unchanged semantic keys: 176; added: 5; removed: 79; changed: 0.
- Added by symbol: {'BEUSDT': 4, 'PROMUSDT': 1}.
- Removed by symbol: {'AKEUSDT': 77, 'TAOUSDT': 2}.

The event-count decrease is explained by the changing top-50 universe (not by a changed threshold rule): AKE and TAO left the primary universe, BE entered, and five semantic event keys were added while 79 were removed. All 176 shared keys were semantically unchanged.

## Primary cumulative cohorts

| Run | Threshold | Event rows | Resolved n | Success | Failure | No outcome | Success rate | Sufficiency |
|---|---|---:|---:|---:|---:|---:|---:|---|
| baseline | $25,000 / 0.5% | 254 | 13 | 13 | 0 | 0 | 1.000 | descriptive_only |
| baseline | $50,000 / 1% | 254 | 12 | 12 | 0 | 0 | 1.000 | descriptive_only |
| baseline | $100,000 / 2% (baseline) | 254 | 6 | 6 | 0 | 0 | 1.000 | descriptive_only |
| baseline | $150,000 / 3% | 254 | 5 | 5 | 0 | 0 | 1.000 | descriptive_only |
| rerun | $25,000 / 0.5% | 180 | 14 | 14 | 0 | 0 | 1.000 | descriptive_only |
| rerun | $50,000 / 1% | 180 | 13 | 13 | 0 | 0 | 1.000 | descriptive_only |
| rerun | $100,000 / 2% (baseline) | 180 | 7 | 7 | 0 | 0 | 1.000 | descriptive_only |
| rerun | $150,000 / 3% | 180 | 5 | 5 | 0 | 0 | 1.000 | descriptive_only |

## Primary adjacent incremental bands

| Run | Softer → stricter | Rows | Resolved n | Success | Failure | No outcome | Rate | Quality |
|---|---|---:|---:|---:|---:|---:|---:|---|
| baseline | $25,000 / 0.5% → $50,000 / 1% | 7 | 1 | 1 | 0 | 0 | 1.000 | not_clearly_better |
| baseline | $50,000 / 1% → $100,000 / 2% (baseline) | 8 | 6 | 6 | 0 | 0 | 1.000 | not_clearly_better |
| baseline | $100,000 / 2% (baseline) → $150,000 / 3% | 2 | 1 | 1 | 0 | 0 | 1.000 | not_clearly_better |
| rerun | $25,000 / 0.5% → $50,000 / 1% | 7 | 1 | 1 | 0 | 0 | 1.000 | not_clearly_better |
| rerun | $50,000 / 1% → $100,000 / 2% (baseline) | 8 | 6 | 6 | 0 | 0 | 1.000 | not_clearly_better |
| rerun | $100,000 / 2% (baseline) → $150,000 / 3% | 3 | 2 | 2 | 0 | 0 | 1.000 | not_clearly_better |

## Controls and coverage

- Controls remain separate; they are not pooled into primary results.
- New sensitivity replay count: 15.
- New replay coverage: 8 complete and 7 incomplete 5m; 7 complete, 0 incomplete, and 8 not-requested 15m outcome replays.
- `no_outcome_in_window` remains outside resolved `n`; new primary rows: 0.

## Decision

- Baseline remains `$100,000 / 2%`.
- Baseline resolved `n`: 6 → 7.
- All cumulative success rates are 1.000, but all cohorts remain descriptive-only.
- Every adjacent incremental band remains `not_clearly_better`; no threshold change is justified.
- No production scoring, filters, whitelist, execution, TP/SL, polling, reserve protection, or Telegram behavior changed.
