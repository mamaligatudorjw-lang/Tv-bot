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