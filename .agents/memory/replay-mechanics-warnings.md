---
name: Replay mechanics warnings
description: Rules for annotating historical Telegram replay messages when strategy mechanics changed.
---

Only annotate a replay when the strategy has an explicit, evidence-backed fix timestamp and description in the strategy-specific registry. Unknown strategies remain unlabelled.

**Why:** Historical replay is informational, but removing old rows or silently presenting them as equivalent to current mechanics would distort the audit trail. The replay queue and delivery deduplication must remain unchanged.

**How to apply:** Compare the resolved position's `ts_close` to each mapped fix timestamp; warn only when it is strictly earlier. Do not use `ts_open`, and do not add broad global fixes to a strategy unless the mapping is confirmed.