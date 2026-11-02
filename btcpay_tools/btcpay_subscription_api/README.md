# Public BTCPay Subscription API

This independent HTTP service creates BTCPay subscriber portal URLs through the
Greenfield API, using its own minimal BTCPay client (`btcpay_client.py`).

## Security warning

The endpoint intentionally accepts an arbitrary caller-supplied email and has
no authentication or email-ownership challenge. Anyone who knows a subscriber
email can obtain a new management URL for that subscriber's configured plan.
This is an accepted account-takeover risk, so do not expose this API directly to
untrusted browser callers. Restrict access to a trusted application or add an
identity-verification layer before public launch. Never treat its in-process
rate limits as authentication or identity protection.

New subscriptions are created with `isTrial=true`, matching the existing
subscription flow. A request for a different plan initiates checkout for the
configured plan and only returns a portal URL if that plan becomes active.
Client retries after a network timeout may create duplicate checkouts because
Greenfield idempotency support is not assumed.

## Configure and run

Copy `.env.example` to `.env` and
`btcpay_subscription.yaml.example` to
`config/btcpay_subscription.yaml` (create the `config` directory first). Put the BTCPay URL, store ID, API key, POS
app ID, and all allowed product plans in the YAML file. Keep the populated
configuration out of version control. To use a config file at another host
path, set `BTCPAY_CONFIG_FILE` to that path for Compose; its container path is
`/app/btcpay_subscription.yaml`. Then run:

```sh
docker compose up --build -d
```

Compose publishes the service on `127.0.0.1:8080` by default. Keep it behind a
TLS-terminating reverse proxy and configure proxy-side body, connection, and
request-rate limits. Change the host binding only when direct access is
intentionally restricted by firewall rules. Do not trust forwarded client-IP
headers; the service rate-limits using the direct TCP peer address. Public
deployments need CDN/proxy or firewall-level volumetric DDoS protection, TLS,
and restrictions preventing direct access to the container from bypassing the
proxy. The Compose limits and in-memory controls apply to one service instance
only and reset when it restarts.

All valid API requests share a moderate per-IP limit (`RATE_LIMIT_REQUESTS`,
default 60 per minute), which protects BTCPay subscriber lookups and portal
issuance. Creating a new subscription is more restrictive: after lookup shows
that checkout is needed, the service applies a separate per-IP limit
(`CREATE_RATE_LIMIT_REQUESTS`, default 2 per hour) and an aggregate
process-wide limit across all IPs (`GLOBAL_CREATE_RATE_LIMIT_REQUESTS`, default
10 per hour). The corresponding `*_WINDOW_SECONDS` variables configure each
window. A matching existing subscription does not consume either creation
quota. Creation limits are in-memory, per process, reset on restart, and are
not shared between replicas; use a single instance or an external shared rate
limiter if these caps must apply across multiple replicas.

The YAML catalog follows the existing application config structure:
`btcpay_base`, `products`, and optional `daemon`. Every configured
`products.<name>[]` plan ID is accepted by the endpoint, and its offering is
resolved from that plan entry. Unknown plan IDs are rejected without contacting
BTCPay. Portal duration defaults to `daemon.portal_duration_minutes` or 1440.
Environment variables and the YAML catalog are validated at startup. The
container is a non-root user with a read-only root filesystem, dropped Linux
capabilities, and CPU/memory limits. Request timeout is bounded and applies to upstream
Greenfield calls; the workflow concurrency ceiling prevents excess upstream
operations. The token-bucket rate limiter has bounded per-IP state and responds
with `429` and `Retry-After` when exceeded.

## API

All endpoints return JSON. Error responses have the form `{"error":"..."}`.
Every route is subject to the global per-IP limit (`RATE_LIMIT_REQUESTS`); the
status route also has its own limit. Malformed requests receive `400`, unknown
plans receive `404`, rate limits receive `429` with `Retry-After`, exhausted
workflow capacity receives `503`, and upstream or activation failures receive
generic `502`/`504` responses without raw Greenfield details.

### `GET /health`

Returns `{"status":"ok"}`.

### `GET /api/subscription-plans`

Returns every plan in the YAML catalog with public Greenfield plan metadata.
Member counts are omitted. The response is cached in-process for
`CATALOG_CACHE_TTL_SECONDS` (default 300; `0` disables caching), so BTCPay is
not queried per request.

```sh
curl http://127.0.0.1:8080/api/subscription-plans
```

```json
{"schema_version":1,"plans":[{"plan_id":"plan-x","name":"Monthly","description":"Standard monthly.","price":"19.99","currency":"USD","recurringType":"Monthly","trialDays":14,"gracePeriodDays":7,"status":"Active","features":["feature-analytics"]}]}
```

### `POST /api/subscriptions/status`

Returns the subscription state for one plan. It never returns a portal URL.
The body is the same as the management-URL endpoint: a `plan_id` from the YAML
catalog and the `buyerEmail`.

```sh
curl http://127.0.0.1:8080/api/subscriptions/status \
  -H 'Content-Type: application/json' \
  -d '{"plan_id":"replace-with-plan-id-from-yaml","buyerEmail":"buyer@example.org"}'
```

```json
{"schema_version":1,"plan_id":"plan-x","status":"active","phase":"normal","isActive":true,"periodEnd":1800000000,"trialEnd":null,"gracePeriodEnd":null,"processingInvoiceId":null}
```

Fields:

- `status`: one of `active`, `trial`, `grace`, `expired`, `suspended`,
  `not_found`, `unknown`. Derived in this order: suspension, then expiry
  (`phase` is `expired` or `isActive` is false), then the labelled phase.
  Unrecognised upstream values map to `unknown`.
- `phase`: the BTCPay lifecycle phase (`expired`, `grace`, `normal`, `trial`), or
  `unknown`. `null` when `status` is `not_found`.
- `isActive`, `periodEnd`, `trialEnd`, `gracePeriodEnd`: passed through from
  BTCPay. Dates are unix timestamps.
- `processingInvoiceId`: the ID of the invoice BTCPay is processing, if any.
  Its meaning is not documented beyond that; clients interpret it.

A subscriber on a different plan reports `not_found` for the requested plan.
The endpoint is limited per IP by `STATUS_RATE_LIMIT_REQUESTS` per
`STATUS_RATE_LIMIT_WINDOW_SECONDS` (default 30 per 60 seconds), in addition to
the global limit.

### `POST /api/subscriptions/management-url`

Returns a BTCPay subscriber portal URL for the plan. Creates a trial checkout
when the buyer has no matching subscription. The creation limits described
above apply.

```sh
curl http://127.0.0.1:8080/api/subscriptions/management-url \
  -H 'Content-Type: application/json' \
  -d '{"plan_id":"replace-with-plan-id-from-yaml","buyerEmail":"buyer@example.org"}'
```

```json
{"management_url":"https://btcpay.example.org/..."}
```

### Schema versioning

Responses carry `schema_version: 1`. Adding optional fields is not a breaking
change. Renaming or changing the meaning of a field requires a new
`schema_version`. Clients should ignore unknown fields and reject unsupported
major versions.
