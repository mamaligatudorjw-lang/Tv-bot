---
name: Published VM readiness
description: Transient private-VM 500s during the interval between publish completion and application readiness.
---

After a private VM publish, authenticated application routes and platform healthchecks may briefly return HTTP 500 while the new Gunicorn process is starting. A later request after the startup log appears can return normally.

**Why:** A publish can be reported successful before the serving process has finished restarting, so the first post-publish HTTP result is not sufficient to classify the deployment as broken.

**How to apply:** Verify the published startup log and retry the target route after the new process is listening; distinguish this transient readiness window from a persistent route failure.