---
name: Private production verification
description: Production endpoint checks for private VM deployments
---

Private published VM endpoints may redirect unauthenticated requests to Replit's ReplShield instead of reaching the app.

**Why:** A deployment can be live and its runtime logs can be verified while direct agent HTTP checks of `/status` or bot API routes remain unavailable.

**How to apply:** Treat deployment logs as sufficient for scheduler/runtime evidence, but do not claim live endpoint payloads, replay delivery, or before/after position state without an authenticated production request or an equivalent production-side audit.

Published external access tokens can be deployment-scoped and expire; an old token may still be present as a secret but only produce a ReplShield 307 redirect for a current private deployment.

**Why:** A production status check used both the application status token and the stored external access token, but ReplShield rejected the latter after the published VM changed.

**How to apply:** When a private published endpoint redirects despite correct app auth, mint/update a Production external access token in Publishing settings before retrying the endpoint or claiming a published ledger report.