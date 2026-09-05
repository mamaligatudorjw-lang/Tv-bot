---
name: Demo gap boundary interpretation
description: How to interpret demo_positions ID-gap checks over narrow forensic time windows
---

An ID-gap check over a bounded time window can identify missing IDs only when the slice contains at least two rows. Empty and single-row slices cannot establish continuity at either window boundary and must be reported as “not applicable,” not as evidence that no rollback or gap occurred.

**Why:** A windowed query has no information about the immediately preceding or following row unless those boundary rows are queried separately. Treating sparse slices as zero gaps creates false negative conclusions.

**How to apply:** Keep the minimal in-window `id`/`ts_open` query as a descriptive probe. In reports, distinguish “no internal gap found” from “not applicable” for fewer than two rows; add neighboring-boundary queries only if stronger continuity proof is required.