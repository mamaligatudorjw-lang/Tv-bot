---
name: Bybit submission timestamp integrity
description: Why Bybit gate classification must use the first order-attempt timestamp rather than polling timestamps
---

The ledger's `ts_submitted` is the first order-attempt timestamp, not a last-seen or last-polled timestamp. Gate classification must preserve that value during reconciliation; historical rows created before the gate cutoff may need a one-time repair when an older poller overwrote it.

**Why:** Using a later polling timestamp falsely turns valid pre-fix historical shadow exceptions into post-fix leaks and produces misleading alerts/status.

**How to apply:** When adding or changing Bybit polling/recovery, retain the existing `ts_submitted` once set, and classify with the original placement time. Treat missing or unrecoverable timestamps as explicitly uncertain rather than as a leak.

The ledger's `ts_filled` is currently a last-observed-filled timestamp: reconciliation rewrites it on every poll that returns `orderStatus=Filled`. It cannot establish when the fill was first detected. For that question, use immutable exchange `createdTime`/`execTime` evidence and retained DB snapshots, or add a separate first-observed transition field.

**Why:** A market IOC order can execute immediately on the exchange while the mutable local field later makes it look as though reconciliation discovered the fill much later.

**How to apply:** Do not calculate stale-fill latency from `ts_filled` alone; preserve first-observed fill separately from the ongoing `last_polled` heartbeat.