---
name: Bybit fill state semantics
description: The existing Bybit Demo ledger separates exchange fill evidence from its general order status.
---

The ledger records a confirmed exchange fill through `executed_qty` and `ts_filled`; the general order `status` can remain `submitted` under the existing reconciliation contract.

**Why:** A global status remap changes legacy polling behavior and breaks timestamp/latency reporting. Multi-TP setup must use confirmed executed quantity as its fill gate without changing the legacy status mapping.

**How to apply:** When adding execution-dependent behavior, read `executed_qty` and the persisted fill timestamps/order status fields. Do not change `_map_order_status()` globally unless the whole ledger contract is intentionally migrated.