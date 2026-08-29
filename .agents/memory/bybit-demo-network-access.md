---
name: Bybit Demo network access
description: Environment constraint discovered while validating the Bybit Demo Trading API.
---

The current Replit runtime cannot reach Bybit Demo Trading API: `api-demo.bybit.com` returns HTTP 403 from CloudFront with a country-blocking message, including for public market endpoints.

**Why:** A live credential check can fail because of network geography even when the HMAC implementation and credentials are otherwise correct.

**How to apply:** Treat this as an environment limitation, not evidence of an invalid key or a reason to retry order POSTs. Validate the first live order from a runtime/network permitted by Bybit, while keeping the bot's safe disabled/unknown behavior in blocked environments.