# HighhX Platform

The service behind the HighhX CLI: accounts, authentication, Free/Pro entitlements,
the streaming AI gateway, usage metering, projects, agent-session sync and Stripe
billing. FastAPI + SQLAlchemy 2 (SQLite for development, PostgreSQL in production).
API overview: [docs/platform.md](../docs/platform.md).

## Run locally

```bash
pip install -e ".[dev]" -e "./server[dev]"          # from the repository root
export ANTHROPIC_API_KEY=...                         # at least one upstream provider
highhx-platform                                      # http://127.0.0.1:8080 (SQLite file in the cwd)

# point the CLI at it
export HIGHHX_API_URL=http://127.0.0.1:8080
highhx login
```

New accounts are on HighhX Free. Grant Pro without billing (development, support):

```bash
curl -X POST http://127.0.0.1:8080/v1/admin/plan \
  -H "X-Admin-Token: $HIGHHX_ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"email": "you@example.com", "plan": "pro"}'
```

## Configuration

All configuration comes from the environment (see `.env.example`):

| Variable | Default | |
|---|---|---|
| `HIGHHX_DATABASE_URL` | `sqlite:///./highhx-platform.db` | e.g. `postgresql+psycopg://user:pass@host/db` (install `highhx-platform[postgres]`) |
| `HIGHHX_PUBLIC_URL` | `http://localhost:8080` | Base URL users reach (device page, checkout return URLs) |
| `HIGHHX_SIGNUP_ENABLED` | `true` | Allow new accounts |
| `HIGHHX_DEFAULT_PROVIDER` | `anthropic` | Upstream when a request names neither provider nor model |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` | – | Upstream providers offered by the gateway |
| `STRIPE_SECRET_KEY` / `STRIPE_PRICE_PRO` / `STRIPE_WEBHOOK_SECRET` | – | Billing (checkout, portal, webhooks) |
| `HIGHHX_ADMIN_TOKEN` | – | Enables the support endpoints `POST /v1/admin/plan` and `POST /v1/admin/suspend` |
| `HIGHHX_TOKEN_TTL_DAYS` | `90` | Lifetime of CLI/API tokens |
| `HIGHHX_MAX_CONCURRENT_STREAMS` | `3` | Concurrent AI requests per account |
| `HIGHHX_FALLBACK_PROVIDERS` | – | Comma-separated upstreams tried when the chosen one fails before any output |
| `HIGHHX_CORS_ORIGINS` | – | Comma-separated web origins allowed by CORS (the CLI needs none) |
| `HIGHHX_AUTO_MIGRATE` | `true` | Apply database migrations at start-up |
| `HIGHHX_SHUTDOWN_TIMEOUT` | `30` | Seconds to finish in-flight requests on shutdown |
| `HOST` / `PORT` | `127.0.0.1` / `8080` | Listen address (the container listens on `0.0.0.0`) |

## Deploy

```bash
docker build -f server/Dockerfile -t highhx-platform .     # from the repository root
docker run -p 8080:8080 --env-file server/.env highhx-platform
```

Point Stripe webhooks at `https://<public-url>/v1/billing/webhook` for
`checkout.session.completed`, `customer.subscription.*`, `invoice.paid` and
`invoice.payment_failed`. Events are signature-verified, applied atomically once per event id,
and older events never overwrite newer subscription state.
Run behind TLS; set `FORWARDED_ALLOW_IPS` to your proxy's address.

### Production notes

- The schema is managed with Alembic migrations (`highhx_platform/migrations`), applied at
  start-up or with `highhx-platform migrate`. A database created by platform 0.1.0 is detected
  and upgraded in place. `/readyz` fails until the schema is at the latest revision.
- Instances are stateless: run as many replicas as you like behind any load balancer (no
  sticky sessions). AI stream state (events, cancellation requests, client presence, the
  owning instance's heartbeat) and rate-limit counters live in the database, so a client
  that loses its connection can resume on any instance, a cancel sent to any instance stops
  the upstream call, and limits apply across replicas. Keep the instances' clocks in sync
  (NTP); stream timeouts are measured in seconds.
- If an instance dies mid-stream, readers on other instances end that stream with a
  retryable error after `owner_timeout` (15 s) and its usage record is closed.
- Tokens are stored as SHA-256 hashes and passwords with scrypt. Pages send strict
  security headers (CSP, frame denial, no referrer); HSTS is added behind https.

## Tests

```bash
cd server && pytest
```

The suite runs every database test on SQLite **and** PostgreSQL (a real server: set
`HIGHHX_TEST_POSTGRES_URL`, or install `embedded-postgres` + `psycopg` to start one
automatically), and includes live end-to-end tests in which the real `highhx` CLI signs in, checks its plan and runs an agent turn
against a real server process over HTTP and server-sent events.
