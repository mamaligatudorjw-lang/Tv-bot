---
name: Telegram notification allowlist
description: Durable boundary between Telegram delivery and signal/trading telemetry.
---

The Telegram strategy allowlist is a delivery boundary, not a signal-generation
or trading boundary. A strategy outside the allowlist must not call Telegram,
but its alert history, cooldown behavior, demo/shadow position, and forward
experiment telemetry must continue as normal.

**Why:** The bot needs to reduce user-facing noise while preserving comparable
shadow and forward data; filtering earlier would bias experiments and change
trading behavior.

**How to apply:** Route strategy-originated push messages through the allowlist,
while leaving command responses and system notifications unfiltered. Treat a
filtered alert as accepted by downstream signal code, but record its delivery
audit as suppressed.