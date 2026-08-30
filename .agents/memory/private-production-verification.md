---
name: Private production verification
description: Production endpoint checks for private VM deployments
---

Private published VM endpoints may redirect unauthenticated requests to Replit's ReplShield instead of reaching the app.

**Why:** A deployment can be live and its runtime logs can be verified while direct agent HTTP checks of `/status` or bot API routes remain unavailable.

**How to apply:** Treat deployment logs as sufficient for scheduler/runtime evidence, but do not claim live endpoint payloads, replay delivery, or before/after position state without an authenticated production request or an equivalent production-side audit.