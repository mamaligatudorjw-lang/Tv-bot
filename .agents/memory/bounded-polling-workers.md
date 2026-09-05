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

The final Gate.io range-breakout scan is especially sensitive to this rule: it
performs one sequential 1h/200-candle request per liquid symbol and has no
cancellation check inside the request loop. A timeout therefore commonly leaves
the daemon worker alive for several more minutes, producing the next-cycle
overlap.

**Why:** A measured per-request latency around 0.8 seconds multiplied across
roughly 459 liquid symbols is several minutes, while the cycle only has the
remaining deadline budget when this late shadow stage starts.

**How to apply:** Treat range-stage timeout→overlap pairs as one slow-worker
incident, not two independent API failures; preserve active-strategy checks
before this shadow stage and instrument per-symbol/request progress before
choosing a fix.

At the exact boundary between a timed worker finishing and the caller's
deadline wait expiring, the wrapper may either return the worker result or
raise its deadline exception; both are valid lifecycle outcomes.

**Why:** Thread scheduling can make `finished.wait(timeout)` observe the
worker just before or just after the deadline even when cancellation telemetry
and side-effect guards behave identically.

**How to apply:** Boundary tests should assert cancellation telemetry,
worker cleanup, and absence of late side effects rather than requiring one
specific return-versus-exception branch.

A restart is strongly associated with a scanner hang when the preceding
session has a worker-enter record with no worker-exit, deadline-abort, or
subsequent scheduler cycle until the new PID starts. This proves an
unclosed-worker antecedent, but not the platform's exact restart reason.

**Why:** The runner may expose the PID transition without exposing whether
its health monitor, a manual workflow action, or another supervisor initiated
the termination.

**How to apply:** Separate runner-cause evidence from application-cause
evidence; classify the restart as likely hang-related only when the old
worker lifecycle and scheduler silence support it. Treat later
low-budget timeouts in other strategies as a multi-stage deadline cascade,
not proof that the original long-lived range worker stopped being the
primary bottleneck.