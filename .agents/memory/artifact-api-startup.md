---
name: Artifact API startup ordering
description: Production startup constraints for the API artifact proxying the Flask bot
---

The API artifact on port 8080 owns `/api/healthz` and proxies `/bot-api/*` to the Flask bot on port 5000. It is part of the routed production surface, not an optional dashboard-only service.

**Why:** The deployment artifact manager waits for port 8080 before considering the runnable service ready, while the combined startup script can delay Node until Flask readiness is confirmed. If that readiness probe uses an authenticated Flask endpoint without its header, it can wait through the full loop and the artifact may report that port 8080 never opened.

**How to apply:** When 8080 fails, distinguish the API bundle from Gunicorn and inspect startup ordering, the Flask probe's auth contract, and the artifact port timeout. Use the lightweight `/ping` route for startup liveness; the state-locked `/health` snapshot can block during a long initial polling cycle. Test the Node bundle directly on an alternate port and `/api/healthz` before attributing the failure to trading logic.

Readiness bounds must be measured as wall-clock deadlines, not attempt counts. A local probe with per-request timeouts and sleeps can make a nominal 20-attempt loop last roughly 50 seconds; the observed Flask cold-start therefore needs an explicit buffer while Node remains available immediately.

**Why:** The first production run reached the old loop's warning after about 50 seconds even though it was labeled `20s`, making the telemetry misleading and leaving no evidence that the worker could become ready just after the loop ended.

**How to apply:** Choose the deadline from the measured cold-start plus a documented reserve, cap each probe by the remaining time, and verify with an isolated delayed `/ping` supervisor test that readiness is logged before the warning threshold.

Production deployment logs can omit some early `start-production` stdout lines even while the combined process continues running. An absent readiness marker is therefore unconfirmed, not proof of success; corroborate it with artifact-manager port events and explicit HTTP response lines.

**Why:** A fresh deployment recorded `Server listening`, `/api/healthz` 200 responses, and ongoing Flask polling, but exposed neither the expected `Flask bot is ready` marker nor a successful `/bot-api` response line.

**How to apply:** Read the complete deployment log file rather than relying only on a filtered cursor, and keep the runtime checklist open until the required readiness marker and proxy evidence are present.

Do not attribute missing startup lines to ordinary concurrent writes without a reproduction: two background processes inheriting one already-open stdout preserved all 2,000 test lines locally, while separate deployment traces omitted different `start-production` lines.

**Why:** The missing-line pattern spans multiple deployments and changes between runs, which is more consistent with deployment log collection/routing behavior than a deterministic Bash pipe race.

**How to apply:** Treat deployment-log absence as an evidence gap, not a runtime success or failure. Prefer artifact-manager HTTP evidence and a dedicated, reliably captured readiness signal before closing an infrastructure checkpoint.

When a VM publish reaches image creation but fails while waiting for deployment readiness, the build can be marked failed without any runtime logs from the new VM; the previously successful deployment may remain live.

**Why:** A failed promote attempt can terminate before startup output is attached to deployment logs, so a failed build status alone does not identify an application crash.

**How to apply:** Classify this as a promote/readiness failure, verify the last live deployment separately, and retry once before changing application code when build logs show both artifact builds and image creation succeeded.