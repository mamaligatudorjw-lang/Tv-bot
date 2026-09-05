---
name: Bybit TP recovery states
description: Recovery-state distinction for multi-TP setup failures after an entry has already been submitted.
---

Deterministic TP placement rejects use a terminal manual-recovery state, while ambiguous or transient setup failures remain in the retryable recovery state.

**Why:** Repeating the same rejected TP request cannot repair the order and creates log noise; ambiguous failures may still succeed after an exchange lookup or later retry.

**How to apply:** Keep manual-recovery rows out of automatic polling selection. Preserve the entry ledger as submitted even when post-entry TP setup fails, and provide a separate operator-controlled recovery path before enabling live multi-TP use.

For breakeven SL, persist the calculated entry-based BE target separately from any more-protective native remote SL. An ambiguous or duplicate write is confirmed only by protective readback; pending state is bounded by timeout/readback limits, and a live-direction change fails closed to operator recovery.

**Why:** The exchange's remote stop is the safety truth, while the ledger's BE target is the strategy intent. Treating any existing stop as confirmation can silently accept the wrong protection, and retrying after a reversal can mutate the next position.

**How to apply:** Compare LONG stops as `remote >= target` and SHORT stops as `remote <= target`. Never downgrade a protective remote stop, and expose manual BE retry through the dedicated recovery-token path.

There is a bounded residual TOCTOU risk after the final reversal re-check: the local reversal state is not locked through the symbol-scoped `set_trading_stop()` call, so an unusually fast complete close+open reversal could theoretically make the old ledger's BE request target the new position.

**Why:** Holding the database lock across the exchange mutation would block reversal progress and would not make a symbol-scoped exchange API inherently ledger-scoped. The implementation therefore uses detect-and-back-off rather than claiming mutual exclusion.

**How to apply:** Treat the final re-check as a fail-safe reduction, not proof of mutual exclusion. Before enabling live BE broadly, add an exchange-position identity/ledger binding or an explicit per-symbol mutation protocol if this residual window is unacceptable.