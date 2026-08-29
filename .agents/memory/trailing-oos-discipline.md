---
name: Trailing OOS discipline
description: Rules for separating trailing-stop model selection from forward validation.
---

Do not call a historical time slice an independent OOS holdout when the prior grid-search used the full stored history. Define the cutoff before the test, freeze the selected step, and use only entries opened after that cutoff. If the forward sample is tiny or empty for a strategy, report it as insufficient rather than filling the gap with a post-hoc holdout.

**Why:** The first forward check after a full-history grid contained only two newly opened resolved positions for one strategy and none for the other; relabeling older already-searched data would have overstated validation strength.

**How to apply:** Store the grid generation cutoff, select by `ts_open > cutoff`, never re-optimize the step on the forward slice, and accumulate enough independent observations before interpreting OOS performance.

Frozen in-sample re-analysis should carry the prior artifact's unique signal IDs and
use a read-only database connection when enriching those IDs. This prevents later
live rows or accidental writes from changing a historical result.

**Why:** A live database can contain newer positions than the artifact that defined
the original sample; selecting directly from it silently changes the experiment.

**How to apply:** Treat the artifact as the inclusion list, reject missing or
mismatched IDs, and open SQLite with `mode=ro` for all historical enrichment.