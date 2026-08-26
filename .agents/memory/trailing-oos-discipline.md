---
name: Trailing OOS discipline
description: Rules for separating trailing-stop model selection from forward validation.
---

Do not call a historical time slice an independent OOS holdout when the prior grid-search used the full stored history. Define the cutoff before the test, freeze the selected step, and use only entries opened after that cutoff. If the forward sample is tiny or empty for a strategy, report it as insufficient rather than filling the gap with a post-hoc holdout.

**Why:** The first forward check after a full-history grid contained only two newly opened resolved positions for one strategy and none for the other; relabeling older already-searched data would have overstated validation strength.

**How to apply:** Store the grid generation cutoff, select by `ts_open > cutoff`, never re-optimize the step on the forward slice, and accumulate enough independent observations before interpreting OOS performance.