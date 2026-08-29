---
name: Split-exit fee accounting
description: How to interpret fees and slippage when a simulated full exit becomes two partial exits.
---

For a proportional fee charged on notional, splitting one full close into two 50% closes does not create an extra fee: the two notionals sum to the original notional. A separate sensitivity may model adverse cost on the second execution, but it must not be presented as the total all-in fee or added twice.

**Why:** Partial-TP comparisons can otherwise overstate the cost of a second leg by counting the same exit notional twice, or understate execution risk by assuming the second fill is free.

**How to apply:** Report the source fee/slippage assumption first, then distinguish split-only incremental fee (normally zero under proportional pricing) from an explicitly assumed second-leg execution cost in R.