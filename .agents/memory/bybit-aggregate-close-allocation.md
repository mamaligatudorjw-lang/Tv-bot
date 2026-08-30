---
name: Aggregate Bybit close allocation
description: Reconciliation rules for one exchange close record covering multiple ledger entry rows
---

One Bybit closed-PnL record may represent a netted position formed by multiple entry orders. Split its realized PnL and fees across matching ledger entries by executed quantity, and store the exchange close event time as `ts_closed`.

**Why:** Writing the same aggregate close amount into every entry row double-counts results and makes the local ledger disagree with the exchange.

**How to apply:** Group matching non-terminal entries by symbol and direction during a poll batch. Use exchange order metadata when available, then a same-batch symbol/direction fallback on the first poll; retain the aggregate close payload for auditability.