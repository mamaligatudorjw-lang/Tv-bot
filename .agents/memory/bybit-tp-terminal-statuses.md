---
name: Bybit TP terminal statuses
description: Bybit V5 order statuses that affect multi-level TP reconciliation and audit events.
---

`PartiallyFilledCanceled` is a terminal order state: the executed quantity is final and the remaining quantity cannot fill. Reconciliation must classify it as `cancelled`, exclude the leg from future polling, and emit a distinct cancellation event rather than a normal live partial-fill event.

**Why:** Bybit uses a mixed spelling pattern across cancellation statuses, and relying on `executed_qty > 0` alone misclassifies a dead partially filled order as still active.

**How to apply:** Normalize the status and match `"cancel"` as a substring before the partial-fill branch. Keep `tp_leg_cancelled` separate from `tp_leg_partial_fill` for audit and reversal/recovery logic.

During reversal, an exchange response meaning that an order is already missing, filled, or cancelled is an expected terminal outcome and should be reconciled rather than treated as failure. Transport errors and unknown cancel responses remain ambiguous and must block the reduce-only close through recovery.

**Why:** Retrying or blocking the wrong class can either leave TP orders live during reversal or create false recovery states on an already-finished order.

**How to apply:** Attempt cancellation for every candidate leg even after one ambiguous error, persist the aggregate counters, reconcile all candidates, and only submit the reversal close when no ambiguous/error or active leg remains.