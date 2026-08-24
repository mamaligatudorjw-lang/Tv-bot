---
name: Bounded polling workers
description: Constraints for enforcing a hard polling deadline around slow network-heavy strategies.
---

Strategy execution that must obey a hard cycle deadline cannot use a normal
executor context manager because shutdown can wait for the worker. Use a
daemonized worker and wait only for the remaining cycle budget; the scheduler
thread must be released even if the worker is still alive.

**Why:** The network-heavy overheated scan consistently consumed nearly the
entire cycle budget and caused every later strategy to be skipped.

**How to apply:** Keep network client timeouts bounded as a second layer,
record one terminal abort with the ordered skipped list, and explicitly copy
thread-local cycle telemetry into the worker.

Timed workers also need a shared cancellation event, cycle-ownership checks at
every alert/demo/continuation side-effect boundary, and a strategy-level
overlap gate. A daemon thread stopping its caller is not enough to make its
late writes safe.

**Why:** A timed-out worker can outlive the scheduler call and otherwise send a
late Telegram alert or mutate cooldown/continuation state after the next cycle
has started.

**How to apply:** Set cancellation when the deadline expires, reject stale
cycle writes/delivery, and release the strategy gate only when the worker exits.

Production validation should treat a warmed-cycle sample as valid only when it
comes from one uninterrupted worker/PID and one final code version; startup
cycles and mixed-version history must be excluded. A repeated overlap abort on a
slow strategy is also evidence that the overlap gate is protecting the next
cycle, not merely a test-only condition.

**Why:** A 10-cycle production sample showed the overlap guard blocking the
same slow strategy in alternating cycles while all later target strategies
still entered successfully; earlier mixed-PID reports had obscured this
behavior.

**How to apply:** Record worker provenance and exclude startup/mixed sessions
before calculating pass rates or claiming statistical confirmation.

Prefetch futures are intentionally single-cycle inputs: after the bounded
`as_completed` window, pending futures are cancelled where possible and the
executor is shut down without waiting. An already-running future may finish,
but its result is not retained or consumed by this or a later cycle.

**Why:** Waiting for per-symbol late-result telemetry would misclassify an
unreachable future as a missing production event; safety comes from preventing
the result from entering the signal pipeline.

**How to apply:** Test both the late-valid discard path for results that are
actually consumed after a hard deadline and the unreachable path for futures
that finish after collection timeout.