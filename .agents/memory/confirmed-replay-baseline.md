---
name: Confirmed replay baseline provenance
description: Frozen continuation-event replays need an explicit fallback when duplicate blocking removed the event's own persisted position row.
---

Event-level continuation confirmations are not guaranteed to have their own `demo_positions` row: the paper-position duplicate guard can block persistence while telemetry still records the whitelisted event. A frozen replay must preserve the event in the fixed cohort, reconstruct its current-form barriers from completed historical 4h ATR when possible, and retain per-signal provenance; a generic global TP/SL fallback would change the experiment.

**Why:** A fixed confirmation cohort can be larger than the set of persisted confirmed rows, so dropping unmatched events or silently substituting one global distance biases the comparison.

**How to apply:** Match exact confirmed rows first, then use historical ATR at the estimated parent-signal time, and only use a nearest persisted parent as an explicitly labeled last resort.