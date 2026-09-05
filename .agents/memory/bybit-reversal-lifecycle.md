---
name: One-way reversal safety
description: Durable safety rules for controlled direction changes on a one-way derivatives position.
---

The exchange-reported one-way position is the source of truth for close quantity and whether a reversal lifecycle has ended. The local ledger provides durable intent, claim, and audit state, but must not infer a flat position from an order response alone.

**Why:** A signal can arrive after a manual size change, a partial fill, a restart, or an ambiguous network result. Treating the local signal quantity or a single POST response as authoritative can double-submit a close or open an unintended opposite position.

**How to apply:** Claim a symbol atomically at the database level, close with a distinct deterministic idempotency key, reconcile the order/executions and live position before another pass, and enter recovery without a blind retry when reconciliation is ambiguous. Permit a second opposite-direction reversal only after the new position is confirmed flat.

Expired `CLOSING` and `OPEN_PENDING` lifecycles must be moved to `RECOVERY_REQUIRED` by a DB-only conditional watchdog; the watchdog must never submit an exchange order.

**Why:** A restart can leave a reversal lifecycle without an active signal worker, while a network action from a recovery scanner could create a duplicate close or open.

**How to apply:** Scan by state and deadline, update only if the same state is still current, write the recovery event in the same transaction, and leave operator resolution to a separate recovery flow.