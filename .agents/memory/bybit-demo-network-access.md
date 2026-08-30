---
name: Bybit Demo network access
description: Environment constraint discovered while validating the Bybit Demo Trading API.
---

The current Replit runtime cannot reach Bybit Demo Trading API: `api-demo.bybit.com` returns HTTP 403 from CloudFront with a country-blocking message, including for public market endpoints.

**Why:** A live credential check can fail because of network geography even when the HMAC implementation and credentials are otherwise correct.

**How to apply:** Treat this as an environment limitation, not evidence of an invalid key or a reason to retry order POSTs. Validate the first live order from a runtime/network permitted by Bybit, while keeping the bot's safe disabled/unknown behavior in blocked environments. The production client must fail closed unless an explicit HTTPS relay is configured; never silently fall back to direct access.

For credential diagnosis, an invalid key can make `/v5/order/realtime` return an empty HTTP 401 from CloudFront, while `/v5/user/query-api` returns the actionable Bybit response `retCode=10003`.

**Why:** The empty 401 is indistinguishable from relay authentication failure unless the relay health check and a minimal authenticated Bybit endpoint are tested separately.

**How to apply:** First confirm token-protected relay health, then call `/v5/user/query-api` directly from the allowed relay host. Do not attempt an order until that endpoint returns `retCode=0`.