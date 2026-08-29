# Historical close-reason analysis

**Read-only analysis. Production logic and the SQLite database were not changed.**

The cohort includes every non-open `demo_positions` row whose strategy is in the current Telegram notification allowlist. TP and SL are outcome reasons; `manual` is `admin`; all other non-open statuses are `other`.

WR is calculated as `TP / (TP + SL)`, so admin and other closures remain visible in counts and shares but do not inflate or depress WR. avg R is calculated only for TP/SL rows with a persisted exit price.

- Minimum cohort size: `20`; smaller cells are **insufficient**.
- Frozen regime join: `947` joined, `739` missing and retained as `unknown`.
- Strategies: `ema_cross_confirmed, overheated_early, ema_cross, overheated_confirmed`.

## Coverage

```json
{
  "resolved_rows": 1686,
  "resolved_by_strategy": {
    "ema_cross": 474,
    "overheated_early": 259,
    "overheated_confirmed": 519,
    "ema_cross_confirmed": 434
  },
  "resolved_by_direction": {
    "SHORT": 392,
    "LONG": 1294
  },
  "close_reason_counts": {
    "tp": 492,
    "sl": 694,
    "other": 500
  },
  "persisted_status_counts": {
    "tp": 492,
    "sl": 694,
    "ttl_expired": 500
  },
  "regime_counts": {
    "bear": 91,
    "unknown": 739,
    "bull": 856
  },
  "regime_reason_counts": {
    "close_vs_ema50": 947,
    "snapshot_missing": 739
  },
  "regime_snapshot_path": "/home/runner/workspace/telegram-webhook-bot/trend_regime_analysis/signal_regimes.csv",
  "regime_snapshot_rows": 3667,
  "regime_snapshot_joined_rows": 947,
  "regime_snapshot_missing_rows": 739,
  "telegram_notification_strategies": [
    "ema_cross_confirmed",
    "overheated_early",
    "ema_cross",
    "overheated_confirmed"
  ],
  "strategy_selection_source": "app.py default Telegram notification allowlist",
  "analysis_run_utc": "2026-08-29T19:56:50.214880+00:00"
}
```

## Close-reason mapping

| Report reason | Persisted condition | Included in WR/avg R |
|---|---|---|
| `tp` | `status=tp` | Yes |
| `sl` | `status=sl` | Yes |
| `admin` | `status=manual` or `exit_method=manual` | No |
| `other` | Any other non-open status | No |

## Overall by direction and regime

| Strategy | Direction | Regime | n | TP | SL | Admin | Other | TP share | SL share | Admin share | Other share | WR (TP/(TP+SL)) | avg R | Sample |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| ALL | LONG | bull | 672 | 344 | 328 | 0 | 0 | 51.19% | 48.81% | 0.00% | 0.00% | 51.19% | 0.5642 | ready |
| ALL | LONG | bear | 64 | 24 | 40 | 0 | 0 | 37.50% | 62.50% | 0.00% | 0.00% | 37.50% | 0.0219 | ready |
| ALL | LONG | unknown | 558 | 54 | 139 | 0 | 365 | 9.68% | 24.91% | 0.00% | 65.41% | 27.98% | -0.1735 | ready |
| ALL | SHORT | bull | 184 | 53 | 131 | 0 | 0 | 28.80% | 71.20% | 0.00% | 0.00% | 28.80% | -0.2417 | ready |
| ALL | SHORT | bear | 27 | 8 | 19 | 0 | 0 | 29.63% | 70.37% | 0.00% | 0.00% | 29.63% | -0.1105 | ready |
| ALL | SHORT | unknown | 181 | 9 | 37 | 0 | 135 | 4.97% | 20.44% | 0.00% | 74.59% | 19.57% | -0.4478 | ready |

## By strategy, direction and regime

| Strategy | Direction | Regime | n | TP | SL | Admin | Other | TP share | SL share | Admin share | Other share | WR (TP/(TP+SL)) | avg R | Sample |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| ema_cross_confirmed | LONG | bull | 94 | 67 | 27 | 0 | 0 | 71.28% | 28.72% | 0.00% | 0.00% | 71.28% | 1.1802 | ready |
| ema_cross_confirmed | LONG | bear | 0 | 0 | 0 | 0 | 0 | — | — | — | — | — | — | INSUFFICIENT (<20; n=0) |
| ema_cross_confirmed | LONG | unknown | 148 | 6 | 21 | 0 | 121 | 4.05% | 14.19% | 0.00% | 81.76% | 22.22% | -0.3934 | ready |
| ema_cross_confirmed | SHORT | bull | 50 | 11 | 39 | 0 | 0 | 22.00% | 78.00% | 0.00% | 0.00% | 22.00% | -0.4585 | ready |
| ema_cross_confirmed | SHORT | bear | 0 | 0 | 0 | 0 | 0 | — | — | — | — | — | — | INSUFFICIENT (<20; n=0) |
| ema_cross_confirmed | SHORT | unknown | 142 | 2 | 5 | 0 | 135 | 1.41% | 3.52% | 0.00% | 95.07% | 28.57% | -0.1503 | ready |
| overheated_early | LONG | bull | 195 | 89 | 106 | 0 | 0 | 45.64% | 54.36% | 0.00% | 0.00% | 45.64% | 0.4356 | ready |
| overheated_early | LONG | bear | 2 | 1 | 1 | 0 | 0 | 50.00% | 50.00% | 0.00% | 0.00% | 50.00% | 0.5154 | INSUFFICIENT (<20; n=2) |
| overheated_early | LONG | unknown | 62 | 19 | 43 | 0 | 0 | 30.65% | 69.35% | 0.00% | 0.00% | 30.65% | -0.0890 | ready |
| overheated_early | SHORT | bull | 0 | 0 | 0 | 0 | 0 | — | — | — | — | — | — | INSUFFICIENT (<20; n=0) |
| overheated_early | SHORT | bear | 0 | 0 | 0 | 0 | 0 | — | — | — | — | — | — | INSUFFICIENT (<20; n=0) |
| overheated_early | SHORT | unknown | 0 | 0 | 0 | 0 | 0 | — | — | — | — | — | — | INSUFFICIENT (<20; n=0) |
| ema_cross | LONG | bull | 162 | 93 | 69 | 0 | 0 | 57.41% | 42.59% | 0.00% | 0.00% | 57.41% | 0.7448 | ready |
| ema_cross | LONG | bear | 62 | 23 | 39 | 0 | 0 | 37.10% | 62.90% | 0.00% | 0.00% | 37.10% | 0.0059 | ready |
| ema_cross | LONG | unknown | 50 | 13 | 37 | 0 | 0 | 26.00% | 74.00% | 0.00% | 0.00% | 26.00% | -0.2076 | ready |
| ema_cross | SHORT | bull | 134 | 42 | 92 | 0 | 0 | 31.34% | 68.66% | 0.00% | 0.00% | 31.34% | -0.1608 | ready |
| ema_cross | SHORT | bear | 27 | 8 | 19 | 0 | 0 | 29.63% | 70.37% | 0.00% | 0.00% | 29.63% | -0.1105 | ready |
| ema_cross | SHORT | unknown | 39 | 7 | 32 | 0 | 0 | 17.95% | 82.05% | 0.00% | 0.00% | 17.95% | -0.5012 | ready |
| overheated_confirmed | LONG | bull | 221 | 95 | 126 | 0 | 0 | 42.99% | 57.01% | 0.00% | 0.00% | 42.99% | 0.2833 | ready |
| overheated_confirmed | LONG | bear | 0 | 0 | 0 | 0 | 0 | — | — | — | — | — | — | INSUFFICIENT (<20; n=0) |
| overheated_confirmed | LONG | unknown | 298 | 16 | 38 | 0 | 244 | 5.37% | 12.75% | 0.00% | 81.88% | 29.63% | -0.1291 | ready |
| overheated_confirmed | SHORT | bull | 0 | 0 | 0 | 0 | 0 | — | — | — | — | — | — | INSUFFICIENT (<20; n=0) |
| overheated_confirmed | SHORT | bear | 0 | 0 | 0 | 0 | 0 | — | — | — | — | — | — | INSUFFICIENT (<20; n=0) |
| overheated_confirmed | SHORT | unknown | 0 | 0 | 0 | 0 | 0 | — | — | — | — | — | — | INSUFFICIENT (<20; n=0) |

## Guardrails

- Every configured strategy has explicit LONG and SHORT rows for bull, bear, and unknown regimes; empty or sub-20 cohorts stay **INSUFFICIENT**.
- Missing regime IDs are retained in the audit as `unknown` with `snapshot_missing`; the snapshot is not refreshed.
- This is descriptive historical analysis, not a gate/filter recommendation or a forward test.
