---
name: Multi-level TP price basis
description: Authoritative formula and provenance contract for multi-level TP prices.
---

Multi-level TP prices use `entry ± multiplier × ATR`, with the actual opening ATR snapshot passed explicitly to the calculator. The finalized SL may be validated for correct geometry but must not supply the distance used for TP prices.

**Why:** The user directly confirmed this requirement after rejecting an unverified claim that R-from-SL had superseded it.

**How to apply:** Keep the calculator's ATR input mandatory and ensure the persisted ATR provenance describes the same value used in the price formula. Do not reintroduce fixed R-from-SL multipliers without new direct approval.