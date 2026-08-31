---
name: Liquidation rerun universe drift
description: How to compare repeat scans when the rank-based top-50 universe changes.
---

When repeating the historical liquidation scan, a later end time does not guarantee a superset of event rows because the current top-50 universe is re-ranked. Preserve the prior report, compare report hashes, and compare semantic event keys such as `(symbol, cohort, pump_ts)` rather than treating total-row deltas as newly observed history.

**Why:** Contracts can enter or leave the primary universe between scans, so a lower event count may reflect universe churn instead of missing data or a changed detector.

**How to apply:** Report added and removed primary contracts, unchanged/added/removed/changed semantic event keys, and only then interpret sensitivity cohort differences.