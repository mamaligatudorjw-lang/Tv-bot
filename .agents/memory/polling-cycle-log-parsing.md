---
name: Polling cycle log parsing
description: bot_debug.log is an append-only aggregate across restarts and has no PID field.
---

Do not pair cycle start and finish lines across the whole log. The same file contains multiple scheduler sessions, and a cycle may end with either a normal completion line or a deadline-abort line.

**Why:** Global FIFO pairing falsely turns independent restarts into multi-day cycles and produces meaningless duration statistics.

**How to apply:** Segment analysis by the scheduler initialization markers (`Adding job tentatively` groups), then pair starts only with terminal events inside each session. Treat missing terminal lines as an incomplete session/cycle, not as a long-running cycle.