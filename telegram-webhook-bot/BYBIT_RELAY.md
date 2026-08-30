# Bybit Demo relay operations

The bot can send only its Bybit Demo API requests through a dedicated relay.
When `BYBIT_RELAY_URL` is absent, the client keeps its direct-API behavior for
compatibility; when a relay URL is present, it never falls back to the direct
Bybit host.

## Required configuration

Set these values as environment secrets on the bot:

- `BYBIT_RELAY_URL` — HTTPS origin of the relay, without credentials, query
  string, or fragment.
- `BYBIT_RELAY_TOKEN` — high-entropy shared token. The same value must be set
  on the relay host.
- `BYBIT_DEMO_MAX_EXPOSURE_USD` — maximum nominal open linear exposure before a
  new order; defaults to `500`.
- `BYBIT_DEMO_EQUITY_RESERVE_USD` — minimum account equity for a new order;
  defaults to `100`.

The existing `BYBIT_DEMO_API_KEY` and `BYBIT_DEMO_API_SECRET` remain the
Bybit credentials. They are signed by the bot and forwarded only to the
fixed Demo upstream; they are never used as relay credentials.

## Relay deployment contract

`bybit_relay.py` is intended to run in a Bybit-permitted region behind an
HTTPS terminator. A minimal process command is:

```text
gunicorn --bind 127.0.0.1:8080 bybit_relay:app --log-level info
```

The public HTTPS terminator must forward only to this local service. The
relay itself:

- accepts only `GET` and `POST` under `/v5/`;
- forwards only to the fixed `https://api-demo.bybit.com` upstream;
- requires `X-Bybit-Relay-Token`;
- rejects plain HTTP when deployed directly or when
  `X-Forwarded-Proto` is not `https`;
- forwards only Bybit `X-BAPI-*` and basic content headers;
- strips the relay token and hop-by-hop headers before the upstream request;
- never logs request headers or credential values.

`/healthz` is token-protected and may be used by an external uptime check. It
does not contact Bybit.

## Progressive validation

1. From an allowed relay region, check `/healthz` over HTTPS.
2. Check `GET /v5/market/time` through the relay; expect the Bybit JSON response.
3. Run a signed read-only Demo request.
4. Only then allow one approved signal to submit the `$50` market order with
   TP/SL and verify the local ledger plus polling.

Before every new order, the bot performs two additional signed read-only
requests through the relay:

- `GET /v5/account/wallet-balance` for the unified USDT wallet balance.
- `GET /v5/position/list?category=linear&settleCoin=USDT` for all linear
  positions.

The order is allowed only when both independent checks pass:

```text
nominal_open_exposure + 50 <= max_exposure
balance + sum(unrealized_pnl) >= equity_reserve
```

Equality at either boundary is allowed. Invalid reserve environment values or
incomplete account/position responses fail closed and do not send
`/v5/order/create`. The decision and observed numeric values are recorded in
the separate `bybit_demo_positions` ledger and safe status snapshot; no raw
Bybit payload or credential is exposed.

An upstream timeout or relay `502/504` is treated as an uncertain result for
order submission. The ledger records the failure and the bot does not
automatically repeat the POST. Repair the relay and reconcile the order by its
deterministic link ID before taking any further action.

## Production checklist

- Relay host is in the user's permitted jurisdiction and is not an open proxy.
- Firewall or mTLS/IP restriction permits only the bot's egress.
- TLS certificate is valid and automatic renewal is monitored.
- `BYBIT_RELAY_TOKEN` is stored only in the relay secret store and Replit
  Secrets; never place it in source control.
- Access/error logs exclude `X-Bybit-Relay-Token` and all `X-BAPI-*` headers.
- A relay availability alert is configured separately from order polling.