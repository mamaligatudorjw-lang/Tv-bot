# Historical signal outcomes by BTC regime

**Read-only descriptive analysis. Production logic and the SQLite database were not changed.**

Regime is based on BTC Futures 4h. For each signal, only the last candle with `candle_open + 4h <= ts_open` is used. EMA50 is calculated from the chronological candle history available up to that completed candle.

`bull` means BTC close > EMA50; `bear` means BTC close < EMA50; equality or insufficient candle history is reported as `unknown` and is not silently assigned.

Outcomes are resolved `demo_positions` rows with status `tp` or `sl`. WR is `tp / (tp + sl)`. avg R uses the recorded exit price and the original entry-to-SL risk. It is not a reconstruction of intrabar order.

## Coverage

```json
{
  "resolved_rows_loaded": 3667,
  "resolved_rows_reported": 3667,
  "signals_by_direction": {
    "LONG": 2549,
    "SHORT": 1118
  },
  "signals_by_strategy": {
    "oversold_24h": 516,
    "overheated_24h": 431,
    "confluence": 494,
    "streak_1h": 13,
    "pump_24h_fade": 114,
    "bb_squeeze": 909,
    "ema_cross": 385,
    "vwap_reversion": 9,
    "overheated_early": 197,
    "liq_reversal": 4,
    "pump_fade_confirmed": 22,
    "ema_cross_confirmed": 144,
    "overheated_confirmed": 221,
    "high_rejection_short": 75,
    "range_breakout_long": 8,
    "low_rejection_long": 35,
    "bb_squeeze_inverted_test": 90
  },
  "trend_regime_counts": {
    "bull": 2430,
    "bear": 1237
  },
  "regime_reason_counts": {
    "close_vs_ema50": 3667
  },
  "btc_fetch": {
    "status": "ok",
    "requested_start_ts": 1782703309,
    "requested_end_ts": 1787851958,
    "requested_start_utc": "2026-06-29T03:21:49+00:00",
    "requested_end_utc": "2026-08-27T17:32:38+00:00",
    "candles_received": 359,
    "candle_first_ts": 1782691200,
    "candle_last_ts": 1787846400,
    "candle_first_utc": "2026-06-29T00:00:00+00:00",
    "candle_last_utc": "2026-08-27T16:00:00+00:00"
  },
  "signal_min_ts": 1786303309,
  "signal_max_ts": 1787851958,
  "signal_min_utc": "2026-08-09T19:21:49+00:00",
  "signal_max_utc": "2026-08-27T17:32:38+00:00",
  "analysis_run_utc": "2026-08-27T18:01:55.936419+00:00"
}
```

## Overall by direction and regime

| Strategy | Direction | Regime | n | WR resolved | avg R | Sample |
|---|---|---|---:|---:|---:|---|
| ALL | LONG | bear | 751 | 37.5% | 0.117 | ready |
| ALL | LONG | bull | 1798 | 45.0% | 0.297 | ready |
| ALL | SHORT | bear | 486 | 29.0% | -0.215 | ready |
| ALL | SHORT | bull | 632 | 23.6% | -0.541 | ready |

## By strategy, direction and regime

| Strategy | Direction | Regime | n | WR resolved | avg R | Sample |
|---|---|---|---:|---:|---:|---|
| bb_squeeze | LONG | bear | 173 | 35.8% | 0.267 | ready |
| bb_squeeze | LONG | bull | 300 | 29.7% | -0.182 | ready |
| bb_squeeze | SHORT | bear | 168 | 33.9% | -0.103 | ready |
| bb_squeeze | SHORT | bull | 268 | 15.3% | -1.009 | ready |
| bb_squeeze_inverted_test | LONG | bear | 0 | — | — | INSUFFICIENT (<20; n=0) |
| bb_squeeze_inverted_test | LONG | bull | 90 | 84.4% | 0.530 | ready |
| confluence | LONG | bear | 218 | 36.7% | 0.098 | ready |
| confluence | LONG | bull | 30 | 23.3% | -0.377 | ready |
| confluence | SHORT | bear | 236 | 26.3% | -0.296 | ready |
| confluence | SHORT | bull | 10 | 10.0% | -0.800 | INSUFFICIENT (<20; n=10) |
| ema_cross | LONG | bear | 62 | 37.1% | 0.006 | ready |
| ema_cross | LONG | bull | 162 | 57.4% | 0.745 | ready |
| ema_cross | SHORT | bear | 27 | 29.6% | -0.111 | ready |
| ema_cross | SHORT | bull | 134 | 31.3% | -0.161 | ready |
| ema_cross_confirmed | LONG | bear | 0 | — | — | INSUFFICIENT (<20; n=0) |
| ema_cross_confirmed | LONG | bull | 94 | 71.3% | 1.180 | ready |
| ema_cross_confirmed | SHORT | bear | 0 | — | — | INSUFFICIENT (<20; n=0) |
| ema_cross_confirmed | SHORT | bull | 50 | 22.0% | -0.459 | ready |
| high_rejection_short | SHORT | bear | 0 | — | — | INSUFFICIENT (<20; n=0) |
| high_rejection_short | SHORT | bull | 75 | 32.0% | -0.067 | ready |
| liq_reversal | LONG | bear | 1 | 0.0% | -1.000 | INSUFFICIENT (<20; n=1) |
| liq_reversal | LONG | bull | 3 | 66.7% | 0.307 | INSUFFICIENT (<20; n=3) |
| low_rejection_long | LONG | bear | 0 | — | — | INSUFFICIENT (<20; n=0) |
| low_rejection_long | LONG | bull | 35 | 37.1% | 0.008 | ready |
| overheated_24h | LONG | bear | 57 | 33.3% | -0.143 | ready |
| overheated_24h | LONG | bull | 374 | 42.2% | 0.276 | ready |
| overheated_confirmed | LONG | bear | 0 | — | — | INSUFFICIENT (<20; n=0) |
| overheated_confirmed | LONG | bull | 221 | 43.0% | 0.283 | ready |
| overheated_early | LONG | bear | 2 | 50.0% | 0.515 | INSUFFICIENT (<20; n=2) |
| overheated_early | LONG | bull | 195 | 45.6% | 0.436 | ready |
| oversold_24h | LONG | bear | 235 | 40.4% | 0.103 | ready |
| oversold_24h | LONG | bull | 281 | 42.0% | 0.257 | ready |
| pump_24h_fade | SHORT | bear | 45 | 24.4% | -0.277 | ready |
| pump_24h_fade | SHORT | bull | 69 | 33.3% | -0.106 | ready |
| pump_fade_confirmed | SHORT | bear | 1 | 100.0% | 2.015 | INSUFFICIENT (<20; n=1) |
| pump_fade_confirmed | SHORT | bull | 21 | 33.3% | -0.022 | ready |
| range_breakout_long | LONG | bear | 0 | — | — | INSUFFICIENT (<20; n=0) |
| range_breakout_long | LONG | bull | 8 | 25.0% | -0.276 | INSUFFICIENT (<20; n=8) |
| streak_1h | LONG | bear | 2 | 50.0% | 0.813 | INSUFFICIENT (<20; n=2) |
| streak_1h | LONG | bull | 2 | 0.0% | -1.020 | INSUFFICIENT (<20; n=2) |
| streak_1h | SHORT | bear | 8 | 12.5% | -0.804 | INSUFFICIENT (<20; n=8) |
| streak_1h | SHORT | bull | 1 | 0.0% | -1.078 | INSUFFICIENT (<20; n=1) |
| vwap_reversion | LONG | bear | 1 | 100.0% | 1.705 | INSUFFICIENT (<20; n=1) |
| vwap_reversion | LONG | bull | 3 | 33.3% | 0.028 | INSUFFICIENT (<20; n=3) |
| vwap_reversion | SHORT | bear | 1 | 100.0% | 2.823 | INSUFFICIENT (<20; n=1) |
| vwap_reversion | SHORT | bull | 4 | 0.0% | -1.368 | INSUFFICIENT (<20; n=4) |

## Special cohorts

The requested cohorts are repeated below so their regime comparison is easy to audit.

| Strategy | Direction | Regime | n | WR resolved | avg R | Sample |
|---|---|---|---:|---:|---:|---|
| bb_squeeze | SHORT | bear | 168 | 33.9% | -0.103 | ready |
| bb_squeeze | SHORT | bull | 268 | 15.3% | -1.009 | ready |
| high_rejection_short | SHORT | bear | 0 | — | — | INSUFFICIENT (<20; n=0) |
| high_rejection_short | SHORT | bull | 75 | 32.0% | -0.067 | ready |

## Interpretation guardrails

- Groups with fewer than 20 resolved rows are marked **INSUFFICIENT**; their percentages are descriptive only.
- The report compares cohorts and does not establish causation.
- Results are subject to multiple comparisons, strategy heterogeneity, fees/slippage, and the recorded-position cohort.
- No signal was filtered, replayed into production, or changed because of this report.
- `unknown` rows remain in the audit CSV and coverage counts; they are not dropped silently.
