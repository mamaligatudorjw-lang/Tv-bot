---
name: One-way opposite-direction risk
description: In Bybit one-way mode, opposite-direction signals can reduce or reverse a net position while the ledger treats the signal as an independent position.
---

In Bybit one-way mode, a signal in the opposite direction is not blocked by the current duplicate guards. Its order can reduce or reverse the existing net position, while the ledger may attribute the aggregate close execution to the new signal's independent position record.

**Why:** A one-way exchange position has no separate long/short lots, so signal-direction ledger semantics can diverge from the actual net position and its fills.

**How to apply:** Treat opposite-direction entry behavior as an explicit design decision: either block it while a symbol has an open net position, or implement reversal-aware sizing, TP/SL recalculation, and fill allocation. Until then, keep it as a known risk and do not silently change trading behavior.