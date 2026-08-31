# Historical pump → liquidation pattern scan

**Read-only report. Production scoring, filters, whitelist, execution, polling, TP/SL, reserve protection, and Telegram behavior are unchanged.**

- Generated: **2026-08-31T09:47:19+00:00**
- Window: **2026-06-01T09:15:00+00:00 → 2026-08-31T09:15:00+00:00**
- Lookback requested: **91 days**
- Primary universe: top **50** by 24h quote notional, then event-capable filter
- Controls: **BTCUSDT, ETHUSDT, SOLUSDT**

## Mandatory liquidation sign preflight

- Result: **PASS**
- Calibrated field: `size`
- Mapping: `{'1': 'long'}`
- Reason: inferred from 1 externally-verified events
- Pre-registration created: **2026-08-31T09:15:51+00:00**

The mapping is calibrated from operator-supplied, independently recognizable examples. It is not inherited from production code.

## Definitions

- Pump: completed 15m close is at least **+15%** versus 32 bars earlier.
- Adjacent pump detections are one episode; only the first timestamp creates downstream work.
- Correction: first low at least **8% below pump high** in the next 12h.
- Long liquidation: real Gate `/liq_orders` records whose calibrated size sign maps to `long`; notional is `abs(size) × fill_price`.
- Liquidation threshold: `max($100,000, 2% × hourly futures notional)`.
- `large_5m_flow`: 5m quote notional at least **3×** the previous 24h median and `close > open`; this is not proof of one large buyer.
- Support: flush low, the minimum completed 15m low from the correction candle through the end of the one-hour liquidation window.
- Success: within 24h after the end of the large-flow candle, either a 15m high exceeds that flow candle's high (continuation), or price touches flush low and closes at or above it (retest-and-hold).
- Failure: a completed 15m candle closes below flush low before either success condition.
- `no_outcome_in_window`: the 24h window ends without continuation, retest-and-hold, or breakdown; it is not success or failure.

## Aggregate results

- Pump episodes: **255**
- Successful outcomes: **6**
- Resolved outcomes (denominator for #178): **6**
- No outcome in window: **0**
- Primary events: **254**
- Control events: **1**

## Event rows

| Symbol | Cohort | Pump UTC | Liquidation USD | Flow UTC | Outcome | Reason |
|---|---|---|---:|---|---|---|
| WLDUSDT | primary | 2026-06-01T19:30:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| WLDUSDT | primary | 2026-06-01T21:15:00+00:00 | 7,622 | — | not_reached | long_liquidation_threshold_not_met |
| WLDUSDT | primary | 2026-06-01T22:45:00+00:00 | 7,622 | — | not_reached | long_liquidation_threshold_not_met |
| WLDUSDT | primary | 2026-06-03T07:15:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| WLDUSDT | primary | 2026-06-03T08:45:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| WLDUSDT | primary | 2026-06-03T09:30:00+00:00 | 46,623 | — | not_reached | long_liquidation_threshold_not_met |
| ENAUSDT | primary | 2026-06-03T10:15:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| ENAUSDT | primary | 2026-06-03T14:00:00+00:00 | 3,694 | — | not_reached | long_liquidation_threshold_not_met |
| WLDUSDT | primary | 2026-06-03T16:15:00+00:00 | 46,623 | — | not_reached | long_liquidation_threshold_not_met |
| PROMUSDT | primary | 2026-06-03T18:45:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| WLDUSDT | primary | 2026-06-04T16:30:00+00:00 | 14,085 | — | not_reached | long_liquidation_threshold_not_met |
| WLDUSDT | primary | 2026-06-04T18:15:00+00:00 | 45,537 | — | not_reached | long_liquidation_threshold_not_met |
| WLDUSDT | primary | 2026-06-04T19:30:00+00:00 | 28,085 | — | not_reached | long_liquidation_threshold_not_met |
| ZECUSDT | primary | 2026-06-05T15:00:00+00:00 | 1,783,745 | — | not_reached | missing_5m_candle:1780676100 |
| ZECUSDT | primary | 2026-06-05T20:00:00+00:00 | 2,165,417 | — | not_reached | missing_5m_candle:1780723800 |
| ZECUSDT | primary | 2026-06-05T23:30:00+00:00 | 871,438 | — | not_reached | missing_5m_candle:1780719300 |
| ZECUSDT | primary | 2026-06-06T02:15:00+00:00 | 871,438 | — | not_reached | missing_5m_candle:1780719300 |
| ZECUSDT | primary | 2026-06-06T02:45:00+00:00 | 871,438 | — | not_reached | missing_5m_candle:1780719300 |
| WLDUSDT | primary | 2026-06-07T01:15:00+00:00 | 11,591 | — | not_reached | long_liquidation_threshold_not_met |
| ZECUSDT | primary | 2026-06-07T06:15:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| WLDUSDT | primary | 2026-06-07T18:00:00+00:00 | 5,277 | — | not_reached | long_liquidation_threshold_not_met |
| SOXLUSDT | primary | 2026-06-08T15:00:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| WLDUSDT | primary | 2026-06-08T18:00:00+00:00 | 200,798 | — | not_reached | missing_5m_candle:1780956900 |
| SOXLUSDT | primary | 2026-06-10T00:00:00+00:00 | 0 | — | not_reached | long_liquidation_threshold_not_met |
| SOXLUSDT | primary | 2026-06-11T20:15:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| SOXLUSDT | primary | 2026-06-11T21:15:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| SOXLUSDT | primary | 2026-06-11T22:45:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| SOXLUSDT | primary | 2026-06-12T00:15:00+00:00 | 2,553 | — | not_reached | long_liquidation_threshold_not_met |
| SOXLUSDT | primary | 2026-06-12T00:45:00+00:00 | 2,553 | — | not_reached | long_liquidation_threshold_not_met |
| TRUMPUSDT | primary | 2026-06-12T09:30:00+00:00 | 56,677 | — | not_reached | long_liquidation_threshold_not_met |
| TRUMPUSDT | primary | 2026-06-12T13:45:00+00:00 | 15,581 | — | not_reached | long_liquidation_threshold_not_met |
| TRUMPUSDT | primary | 2026-06-12T14:45:00+00:00 | 45,775 | — | not_reached | long_liquidation_threshold_not_met |
| TAOUSDT | primary | 2026-06-13T11:15:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| TAOUSDT | primary | 2026-06-13T13:15:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| ZHIPUUSDT | primary | 2026-06-15T01:30:00+00:00 | 6,063 | — | not_reached | long_liquidation_threshold_not_met |
| WLDUSDT | primary | 2026-06-15T03:00:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| WLDUSDT | primary | 2026-06-15T05:45:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| SPCXUSDT | primary | 2026-06-15T22:15:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| SPCXUSDT | primary | 2026-06-15T23:15:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| SPCXUSDT | primary | 2026-06-16T00:15:00+00:00 | 1,398,362 | — | not_reached | missing_5m_candle:1781577000 |
| SPCXUSDT | primary | 2026-06-16T02:00:00+00:00 | 382,670 | — | not_reached | missing_5m_candle:1781591400 |
| SPCXUSDT | primary | 2026-06-16T03:00:00+00:00 | 382,670 | — | not_reached | missing_5m_candle:1781591400 |
| ZHIPUUSDT | primary | 2026-06-17T05:30:00+00:00 | 0 | — | not_reached | long_liquidation_threshold_not_met |
| ZHIPUUSDT | primary | 2026-06-17T06:45:00+00:00 | 0 | — | not_reached | long_liquidation_threshold_not_met |
| ZHIPUUSDT | primary | 2026-06-18T02:00:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| AKEUSDT | primary | 2026-06-20T13:00:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| BTRUSDT | primary | 2026-06-21T01:45:00+00:00 | 0 | — | not_reached | long_liquidation_threshold_not_met |
| ZHIPUUSDT | primary | 2026-06-22T01:30:00+00:00 | 33,179 | — | not_reached | long_liquidation_threshold_not_met |
| ZHIPUUSDT | primary | 2026-06-22T02:00:00+00:00 | 114,617 | — | not_reached | missing_5m_candle:1782099000 |
| ZHIPUUSDT | primary | 2026-06-22T03:30:00+00:00 | 0 | — | not_reached | long_liquidation_threshold_not_met |
| ZHIPUUSDT | primary | 2026-06-22T04:00:00+00:00 | 2,858 | — | not_reached | long_liquidation_threshold_not_met |
| ZHIPUUSDT | primary | 2026-06-22T07:15:00+00:00 | 0 | — | not_reached | long_liquidation_threshold_not_met |
| ZHIPUUSDT | primary | 2026-06-22T09:00:00+00:00 | 0 | — | not_reached | long_liquidation_threshold_not_met |
| SOXLUSDT | primary | 2026-06-24T21:30:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| MUUSDT | primary | 2026-06-24T23:00:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| SOXLUSDT | primary | 2026-06-24T23:30:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| MUUSDT | primary | 2026-06-25T01:00:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| SOXLUSDT | primary | 2026-06-25T01:15:00+00:00 | 35,343,558 | — | not_reached | missing_5m_candle:1782398700 |
| SOXLUSDT | primary | 2026-06-25T02:45:00+00:00 | 35,343,558 | — | not_reached | missing_5m_candle:1782398700 |
| DRAMUSDT | primary | 2026-06-25T03:00:00+00:00 | 4,884 | — | not_reached | long_liquidation_threshold_not_met |
| SOXLUSDT | primary | 2026-06-29T22:00:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| UNIUSDT | primary | 2026-07-02T17:15:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| ZHIPUUSDT | primary | 2026-07-03T01:30:00+00:00 | 0 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-03T10:30:00+00:00 | 0 | — | not_reached | long_liquidation_threshold_not_met |
| ZHIPUUSDT | primary | 2026-07-08T02:00:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| ZHIPUUSDT | primary | 2026-07-08T03:00:00+00:00 | 0 | — | not_reached | long_liquidation_threshold_not_met |
| SOXLUSDT | primary | 2026-07-08T17:00:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| SOXLUSDT | primary | 2026-07-08T18:00:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| SOXLUSDT | primary | 2026-07-08T19:15:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| ZHIPUUSDT | primary | 2026-07-09T01:30:00+00:00 | 0 | — | not_reached | long_liquidation_threshold_not_met |
| ZHIPUUSDT | primary | 2026-07-09T03:30:00+00:00 | 0 | — | not_reached | long_liquidation_threshold_not_met |
| ZHIPUUSDT | primary | 2026-07-09T04:30:00+00:00 | 0 | — | not_reached | long_liquidation_threshold_not_met |
| ZHIPUUSDT | primary | 2026-07-09T08:00:00+00:00 | 0 | — | not_reached | long_liquidation_threshold_not_met |
| PUMPUSDT | primary | 2026-07-15T02:00:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| PUMPUSDT | primary | 2026-07-15T03:45:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| PUMPUSDT | primary | 2026-07-15T04:30:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| AKEUSDT | primary | 2026-07-15T04:45:00+00:00 | 33 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-15T18:00:00+00:00 | 10 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-15T19:30:00+00:00 | 0 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-16T02:30:00+00:00 | 128 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-16T03:15:00+00:00 | 128 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-16T04:00:00+00:00 | 128 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-16T05:30:00+00:00 | 8 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-16T10:15:00+00:00 | 4 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-16T14:45:00+00:00 | 4 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-16T15:15:00+00:00 | 4 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-16T17:45:00+00:00 | 37 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-17T08:15:00+00:00 | 0 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-17T16:45:00+00:00 | 27 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-17T17:15:00+00:00 | 27 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-17T17:45:00+00:00 | 27 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-17T18:30:00+00:00 | 27 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-17T22:45:00+00:00 | 4 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-18T02:00:00+00:00 | 2 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-18T04:15:00+00:00 | 5 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-18T06:00:00+00:00 | 690 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-18T10:30:00+00:00 | 6 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-18T19:45:00+00:00 | 7 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-18T20:15:00+00:00 | 89 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-18T22:30:00+00:00 | 0 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-18T23:00:00+00:00 | 11 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-19T01:15:00+00:00 | 13 | — | not_reached | long_liquidation_threshold_not_met |
| PROMUSDT | primary | 2026-07-19T18:45:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| PROMUSDT | primary | 2026-07-19T19:15:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| PROMUSDT | primary | 2026-07-19T19:45:00+00:00 | 9,455 | — | not_reached | long_liquidation_threshold_not_met |
| PUMPUSDT | primary | 2026-07-19T21:15:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| PUMPUSDT | primary | 2026-07-19T21:45:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| PUMPUSDT | primary | 2026-07-19T22:15:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| PROMUSDT | primary | 2026-07-19T23:00:00+00:00 | 2,968 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-20T00:30:00+00:00 | 60 | — | not_reached | long_liquidation_threshold_not_met |
| PROMUSDT | primary | 2026-07-20T13:00:00+00:00 | 0 | — | not_reached | long_liquidation_threshold_not_met |
| PROMUSDT | primary | 2026-07-20T19:00:00+00:00 | 0 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-21T02:30:00+00:00 | 2 | — | not_reached | long_liquidation_threshold_not_met |
| ZHIPUUSDT | primary | 2026-07-21T03:00:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| PROMUSDT | primary | 2026-07-22T11:15:00+00:00 | 398 | — | not_reached | long_liquidation_threshold_not_met |
| PROMUSDT | primary | 2026-07-22T13:45:00+00:00 | 14,926 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-23T10:15:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| AKEUSDT | primary | 2026-07-23T11:15:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| AKEUSDT | primary | 2026-07-23T14:45:00+00:00 | 26 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-23T16:00:00+00:00 | 26 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-23T18:30:00+00:00 | 18 | — | not_reached | long_liquidation_threshold_not_met |
| PROMUSDT | primary | 2026-07-23T19:45:00+00:00 | 197,137 | — | not_reached | missing_5m_candle:1784868300 |
| AKEUSDT | primary | 2026-07-23T20:00:00+00:00 | 12 | — | not_reached | long_liquidation_threshold_not_met |
| PROMUSDT | primary | 2026-07-23T20:15:00+00:00 | 197,137 | — | not_reached | missing_5m_candle:1784868300 |
| AKEUSDT | primary | 2026-07-24T10:15:00+00:00 | 19 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-24T16:30:00+00:00 | 32 | — | not_reached | long_liquidation_threshold_not_met |
| PROMUSDT | primary | 2026-07-24T18:00:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| PROMUSDT | primary | 2026-07-24T20:00:00+00:00 | 10,504 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-25T05:45:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| AKEUSDT | primary | 2026-07-25T06:30:00+00:00 | 1 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-25T07:30:00+00:00 | 1 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-25T08:15:00+00:00 | 1 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-25T09:15:00+00:00 | 1 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-25T09:45:00+00:00 | 0 | — | not_reached | long_liquidation_threshold_not_met |
| PROMUSDT | primary | 2026-07-25T11:45:00+00:00 | 0 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-27T06:00:00+00:00 | 57 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-27T08:30:00+00:00 | 3 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-27T16:00:00+00:00 | 2 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-27T22:00:00+00:00 | 56 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-28T07:15:00+00:00 | 15 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-28T08:15:00+00:00 | 3 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-29T13:15:00+00:00 | 0 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-29T14:15:00+00:00 | 3 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-29T14:45:00+00:00 | 3 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-29T15:45:00+00:00 | 23 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-29T17:00:00+00:00 | 0 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-30T12:30:00+00:00 | 3 | — | not_reached | long_liquidation_threshold_not_met |
| SOXLUSDT | primary | 2026-07-30T13:15:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| SNDKUSDT | primary | 2026-07-30T13:30:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| DRAMUSDT | primary | 2026-07-30T14:15:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| INTCUSDT | primary | 2026-07-30T14:15:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| MUUSDT | primary | 2026-07-30T14:15:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| DRAMUSDT | primary | 2026-07-30T15:15:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| INTCUSDT | primary | 2026-07-30T15:45:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| INTCUSDT | primary | 2026-07-30T16:45:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| SOXLUSDT | primary | 2026-07-30T19:30:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| MUUSDT | primary | 2026-07-30T20:15:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| SOXLUSDT | primary | 2026-07-30T20:15:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| ZHIPUUSDT | primary | 2026-07-31T01:45:00+00:00 | 0 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-31T20:00:00+00:00 | 0 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-31T20:45:00+00:00 | 0 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-07-31T22:15:00+00:00 | 0 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-08-01T15:00:00+00:00 | 28 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-08-01T16:15:00+00:00 | 49 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-08-02T01:45:00+00:00 | 15 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-08-02T14:45:00+00:00 | 21 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-08-02T15:15:00+00:00 | 24 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-08-02T21:30:00+00:00 | 0 | — | not_reached | long_liquidation_threshold_not_met |
| ZHIPUUSDT | primary | 2026-08-07T02:15:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| ZHIPUUSDT | primary | 2026-08-07T07:30:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| ZHIPUUSDT | primary | 2026-08-07T08:15:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| SPCXUSDT | primary | 2026-08-07T19:45:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| PROMUSDT | primary | 2026-08-10T23:30:00+00:00 | 1,127,308 | 2026-08-11T03:15:00+00:00 | success_continuation | continuation_high_above_large_flow_candle |
| BTRUSDT | primary | 2026-08-11T04:30:00+00:00 | 2 | — | not_reached | long_liquidation_threshold_not_met |
| PROMUSDT | primary | 2026-08-11T10:30:00+00:00 | 0 | — | not_reached | long_liquidation_threshold_not_met |
| PROMUSDT | primary | 2026-08-12T01:00:00+00:00 | 377 | — | not_reached | long_liquidation_threshold_not_met |
| PROMUSDT | primary | 2026-08-12T11:30:00+00:00 | 108,117 | 2026-08-12T17:30:00+00:00 | success_continuation | continuation_high_above_large_flow_candle |
| AKEUSDT | primary | 2026-08-13T10:15:00+00:00 | 1 | — | not_reached | long_liquidation_threshold_not_met |
| SNDKUSDT | primary | 2026-08-13T15:15:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| SNDKUSDT | primary | 2026-08-13T16:00:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| SNDKUSDT | primary | 2026-08-13T17:00:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| SNDKUSDT | primary | 2026-08-13T18:15:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| SNDKUSDT | primary | 2026-08-13T21:15:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| PROMUSDT | primary | 2026-08-13T23:45:00+00:00 | 495 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-08-14T01:30:00+00:00 | 0 | — | not_reached | long_liquidation_threshold_not_met |
| PROMUSDT | primary | 2026-08-14T01:45:00+00:00 | 18,630 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-08-14T02:30:00+00:00 | 3 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-08-14T05:30:00+00:00 | 12 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-08-14T08:00:00+00:00 | 9 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-08-14T08:30:00+00:00 | 7 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-08-14T10:00:00+00:00 | 95 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-08-18T13:00:00+00:00 | 1 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-08-18T14:00:00+00:00 | 1 | — | not_reached | long_liquidation_threshold_not_met |
| MSTRXUSDT | primary | 2026-08-19T16:15:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| HYPEUSDT | primary | 2026-08-19T19:00:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| BMNRUSDT | primary | 2026-08-19T21:00:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| ETHUSDT | control | 2026-08-19T21:00:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| TRUMPUSDT | primary | 2026-08-19T21:00:00+00:00 | 3,847 | — | not_reached | long_liquidation_threshold_not_met |
| WLDUSDT | primary | 2026-08-19T21:45:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| PUMPUSDT | primary | 2026-08-20T15:00:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| PROMUSDT | primary | 2026-08-20T15:45:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| PROMUSDT | primary | 2026-08-20T16:45:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| XRPUSDT | primary | 2026-08-20T17:30:00+00:00 | 7,834 | — | not_reached | long_liquidation_threshold_not_met |
| ENAUSDT | primary | 2026-08-20T21:45:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| ENAUSDT | primary | 2026-08-21T00:00:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| ENAUSDT | primary | 2026-08-21T03:45:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| PROMUSDT | primary | 2026-08-21T04:00:00+00:00 | 0 | — | not_reached | long_liquidation_threshold_not_met |
| ENAUSDT | primary | 2026-08-21T04:15:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| ENAUSDT | primary | 2026-08-21T05:45:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| ENAUSDT | primary | 2026-08-21T08:30:00+00:00 | 357 | — | not_reached | long_liquidation_threshold_not_met |
| ZECUSDT | primary | 2026-08-21T10:45:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| ENAUSDT | primary | 2026-08-21T11:00:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| PROMUSDT | primary | 2026-08-21T14:00:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| PROMUSDT | primary | 2026-08-21T15:00:00+00:00 | 6,657 | — | not_reached | long_liquidation_threshold_not_met |
| ENAUSDT | primary | 2026-08-21T15:15:00+00:00 | 3,856 | — | not_reached | long_liquidation_threshold_not_met |
| PROMUSDT | primary | 2026-08-21T16:30:00+00:00 | 32,725 | — | not_reached | long_liquidation_threshold_not_met |
| ZECUSDT | primary | 2026-08-22T01:45:00+00:00 | 48,412,581 | 2026-08-22T08:30:00+00:00 | success_continuation | continuation_high_above_large_flow_candle |
| TRUMPUSDT | primary | 2026-08-22T02:15:00+00:00 | 100,770 | — | not_reached | long_liquidation_threshold_not_met |
| ENAUSDT | primary | 2026-08-22T04:00:00+00:00 | 20,181 | — | not_reached | long_liquidation_threshold_not_met |
| SUIUSDT | primary | 2026-08-22T04:00:00+00:00 | 157,016 | — | not_reached | long_liquidation_threshold_not_met |
| WLDUSDT | primary | 2026-08-22T04:00:00+00:00 | 228,285 | — | not_reached | long_liquidation_threshold_not_met |
| XRPUSDT | primary | 2026-08-22T04:00:00+00:00 | 374,726 | — | not_reached | long_liquidation_threshold_not_met |
| PEPEUSDT | primary | 2026-08-22T04:15:00+00:00 | 0 | — | not_reached | long_liquidation_threshold_not_met |
| ADAUSDT | primary | 2026-08-22T04:30:00+00:00 | 22,480 | — | not_reached | long_liquidation_threshold_not_met |
| DOGEUSDT | primary | 2026-08-22T04:30:00+00:00 | 555,947 | — | not_reached | long_liquidation_threshold_not_met |
| SUIUSDT | primary | 2026-08-22T04:30:00+00:00 | 157,016 | — | not_reached | long_liquidation_threshold_not_met |
| PUMPUSDT | primary | 2026-08-22T07:00:00+00:00 | 0 | — | not_reached | long_liquidation_threshold_not_met |
| PUMPUSDT | primary | 2026-08-22T07:45:00+00:00 | 0 | — | not_reached | long_liquidation_threshold_not_met |
| TRUMPUSDT | primary | 2026-08-22T11:30:00+00:00 | 249,182 | 2026-08-22T13:35:00+00:00 | success_continuation | continuation_high_above_large_flow_candle |
| ENAUSDT | primary | 2026-08-23T10:00:00+00:00 | 408 | — | not_reached | long_liquidation_threshold_not_met |
| ENAUSDT | primary | 2026-08-23T11:00:00+00:00 | 857 | — | not_reached | long_liquidation_threshold_not_met |
| TRUMPUSDT | primary | 2026-08-23T11:15:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| TRUMPUSDT | primary | 2026-08-23T12:45:00+00:00 | — | — | not_reached | correction_not_found_in_12h |
| TRUMPUSDT | primary | 2026-08-23T13:30:00+00:00 | 95,982 | — | not_reached | long_liquidation_threshold_not_met |
| TRUMPUSDT | primary | 2026-08-23T14:15:00+00:00 | 1,115,929 | — | not_reached | large_5m_flow_not_found |
| PROMUSDT | primary | 2026-08-24T04:45:00+00:00 | 57,901 | — | not_reached | long_liquidation_threshold_not_met |
| BTRUSDT | primary | 2026-08-24T06:30:00+00:00 | 1 | — | not_reached | long_liquidation_threshold_not_met |
| BTRUSDT | primary | 2026-08-24T10:15:00+00:00 | 1 | — | not_reached | long_liquidation_threshold_not_met |
| PROMUSDT | primary | 2026-08-25T03:15:00+00:00 | 236,764 | 2026-08-25T08:50:00+00:00 | success_continuation | continuation_high_above_large_flow_candle |
| BTRUSDT | primary | 2026-08-25T08:15:00+00:00 | 0 | — | not_reached | long_liquidation_threshold_not_met |
| BTRUSDT | primary | 2026-08-26T03:45:00+00:00 | 221 | — | not_reached | long_liquidation_threshold_not_met |
| BTRUSDT | primary | 2026-08-26T21:00:00+00:00 | 1,972 | — | not_reached | long_liquidation_threshold_not_met |
| PROMUSDT | primary | 2026-08-27T01:45:00+00:00 | 10,184 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-08-27T03:00:00+00:00 | 3 | — | not_reached | long_liquidation_threshold_not_met |
| BTRUSDT | primary | 2026-08-27T04:30:00+00:00 | 5,404 | — | not_reached | long_liquidation_threshold_not_met |
| BTRUSDT | primary | 2026-08-27T06:15:00+00:00 | 288 | — | not_reached | long_liquidation_threshold_not_met |
| PROMUSDT | primary | 2026-08-27T06:15:00+00:00 | 1,594 | — | not_reached | long_liquidation_threshold_not_met |
| BTRUSDT | primary | 2026-08-27T09:30:00+00:00 | 251 | — | not_reached | long_liquidation_threshold_not_met |
| TRUMPUSDT | primary | 2026-08-27T15:15:00+00:00 | 961,497 | 2026-08-27T18:35:00+00:00 | success_continuation | continuation_high_above_large_flow_candle |
| ENAUSDT | primary | 2026-08-27T20:30:00+00:00 | 5,049 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-08-28T05:45:00+00:00 | 0 | — | not_reached | long_liquidation_threshold_not_met |
| PROMUSDT | primary | 2026-08-28T10:15:00+00:00 | 15,610 | — | not_reached | long_liquidation_threshold_not_met |
| AKEUSDT | primary | 2026-08-28T19:15:00+00:00 | 166 | — | not_reached | long_liquidation_threshold_not_met |
| TRUMPUSDT | primary | 2026-08-29T05:30:00+00:00 | 4,378,724 | — | not_reached | large_5m_flow_not_found |
| BTRUSDT | primary | 2026-08-29T12:15:00+00:00 | 10,490 | — | not_reached | long_liquidation_threshold_not_met |

## Coverage and exclusions

| Symbol | Cohort | 15m status | 15m bars | Primary | Event-capable | Reason |
|---|---|---|---:|---:|---:|---|
| BTCUSDT | control | complete | 8768 | False | False | — |
| ETHUSDT | control | complete | 8768 | False | True | — |
| SOLUSDT | control | complete | 8768 | False | False | — |
| ADAUSDT | primary | complete | 8768 | True | True | — |
| AKEUSDT | primary | complete | 8768 | True | True | — |
| BMNRUSDT | primary | complete | 8768 | True | True | — |
| BTRUSDT | primary | complete | 8768 | True | True | — |
| DOGEUSDT | primary | complete | 8768 | True | True | — |
| DRAMUSDT | primary | complete | 8768 | True | True | — |
| ENAUSDT | primary | complete | 8768 | True | True | — |
| HYPEUSDT | primary | complete | 8768 | True | True | — |
| INTCUSDT | primary | complete | 8768 | True | True | — |
| MSTRXUSDT | primary | complete | 8768 | True | True | — |
| MUUSDT | primary | complete | 8768 | True | True | — |
| PEPEUSDT | primary | complete | 8768 | True | True | — |
| PROMUSDT | primary | complete | 8768 | True | True | — |
| PUMPUSDT | primary | complete | 8768 | True | True | — |
| SNDKUSDT | primary | complete | 8768 | True | True | — |
| SOXLUSDT | primary | complete | 8768 | True | True | — |
| SPCXUSDT | primary | complete | 8768 | True | True | — |
| SUIUSDT | primary | complete | 8768 | True | True | — |
| TAOUSDT | primary | complete | 8768 | True | True | — |
| TRUMPUSDT | primary | complete | 8768 | True | True | — |
| UNIUSDT | primary | complete | 8768 | True | True | — |
| WLDUSDT | primary | complete | 8768 | True | True | — |
| XRPUSDT | primary | complete | 8768 | True | True | — |
| ZECUSDT | primary | complete | 8768 | True | True | — |
| ZHIPUUSDT | primary | complete | 8768 | True | True | — |

## Data-quality guardrails

- Gate liquidation history is real exchange liquidation data, not a price/volume liquidation proxy.
- Gate liquidation requests are limited to one hour and use `limit=1000`; a full response is recursively split and never accepted as complete.
- Missing candles, rate-limit failures, and capped liquidation windows remain incomplete statuses; they are not converted to zero.
- BTC/ETH/SOL are controls and do not dilute the primary event-capable cohort.
