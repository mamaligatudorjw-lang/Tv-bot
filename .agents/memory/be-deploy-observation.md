---
name: BE deploy observation boundary
description: How to close the disabled BE/multi-TP deployment checkpoint without masking unrelated service warnings.
---

Close the stage-6 deployment checkpoint only for the BE-specific path: verify the BE schema, repeated successful Bybit polling, and explicit disabled TP/BE telemetry. Track unrelated Gate.io or broad service warnings as a separate operational baseline rather than treating them as evidence that the BE path is unhealthy or that the whole deployment is clean.

**Why:** A deployment can be safe and stable for the newly disabled feature while unrelated market-data timeouts or cold-start noise remain present. Combining those signals either blocks a valid checkpoint or falsely claims an error-free service.

**How to apply:** Before enabling multi-TP, require stable `bybit_demo_poll`, repeated `bybit_demo_tp_setup status=disabled`, and the required BE columns in `bybit_demo_positions`. Do not change either flag during this observation; decide on multi-TP separately, with its own post-enable window.