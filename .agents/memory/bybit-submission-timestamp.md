---
name: Bybit submission timestamp integrity
description: Why Bybit gate classification must use the first order-attempt timestamp rather than polling timestamps
---

The ledger's `ts_submitted` is the first order-attempt timestamp, not a last-seen or last-polled timestamp. Gate classification must preserve that value during reconciliation; historical rows created before the gate cutoff may need a one-time repair when an older poller overwrote it.

**Why:** Using a later polling timestamp falsely turns valid pre-fix historical shadow exceptions into post-fix leaks and produces misleading alerts/status.

**How to apply:** When adding or changing Bybit polling/recovery, retain the existing `ts_submitted` once set, and classify with the original placement time. Treat missing or unrecoverable timestamps as explicitly uncertain rather than as a leak.