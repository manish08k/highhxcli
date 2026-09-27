# The HighhX platform

The CLI is the developer's interface. The HighhX platform is the service behind it:

| Capability | What it does |
|---|---|
| Accounts & authentication | Email/password accounts, browser device sign-in for the CLI, revocable API tokens |
| Plans & entitlements | HighhX Free and HighhX Pro; the platform decides which features an account has |
| AI access | A streaming AI gateway: provider keys stay on the platform; plan, allowance and provider settings are enforced there |
| Usage | Every gateway request is metered (tokens per provider/model per billing period) |
| Billing | Stripe checkout, billing portal and signature-verified webhooks |
| Projects & agent sessions | Project registry and agent-session metadata sync |
| Settings | Account-wide agent defaults (provider, upstream, model, approval mode) |

Every command except `highhx agent` works without the platform.

## Signing in

```bash
highhx login                                  # opens the browser; enter the code shown
highhx login --no-browser                     # print the link instead
echo "$HIGHHX_TOKEN" | highhx login --with-token   # CI / headless machines
highhx logout                                 # revokes this machine's token
```

Credentials are stored in the user config directory (`credentials.json`, mode 0600),
never in a project. `HIGHHX_TOKEN` in the environment takes precedence.
`HIGHHX_API_URL` (or `highhx login --api-url URL`) points the CLI at a self-hosted or
development platform; plain `http://` is only accepted for localhost.

If the platform is briefly unreachable, the CLI keeps using the account details it
fetched in the last 24 hours; after that it reports the connection problem.

## Plans

```bash
highhx account plans      # compare Free and Pro (no account needed)
highhx account            # your account, plan and usage
highhx account usage      # tokens and requests this period, by model
highhx account upgrade    # HighhX Pro checkout
highhx account billing    # manage the subscription
highhx account settings --provider highhx --upstream anthropic --model claude-opus-5
```

## API (v1)

| Method & path | Purpose |
|---|---|
| `POST /v1/auth/signup` · `POST /v1/auth/login` | Create an account / sign in → access token |
| `POST /v1/auth/device` · `POST /v1/auth/device/token` | Device authorization for `highhx login` |
| `GET /device` · `POST /device` | Browser page where the user approves a device code |
| `POST /v1/auth/logout` | Revoke the current token |
| `GET/POST /v1/tokens` · `DELETE /v1/tokens/{id}` | API tokens |
| `GET /v1/me` · `PATCH /v1/me/settings` | Account document (plan, features, usage, settings) |
| `GET /v1/usage` | Usage in the current billing period |
| `POST/GET /v1/projects` | Project registry |
| `POST/GET /v1/agent/sessions` · `PATCH /v1/agent/sessions/{id}` | Agent session metadata |
| `POST /v1/ai/messages` | Streaming AI gateway (server-sent events; resumable) |
| `POST /v1/ai/messages/{key}/cancel` | Cancel an in-flight model request (idempotent) |
| `POST /v1/auth/refresh` | Rotate the current token (the old one is revoked) |
| `POST /v1/billing/checkout` · `POST /v1/billing/portal` | Stripe checkout / portal links |
| `POST /v1/billing/webhook` | Stripe webhooks (signature-verified, idempotent) |
| `GET /healthz` · `GET /readyz` · `GET /v1/meta` | Liveness, readiness (database + migrations), capabilities |

Errors are JSON: `{"detail": {"code": "...", "message": "...", "hint": "..."}}` with
codes such as `unauthorized` (401), `plan_required` (402), `quota_exceeded` (429) and
`provider_unavailable` (503), `too_many_concurrent` (429), `account_suspended` (403) and
`client_outdated` / `client_unsupported` (426). The CLI turns them into messages with a next step.

### Protocol versioning

Every CLI request carries `X-HighhX-Protocol: <major>.<minor>` (currently 1.1) and
`X-HighhX-Client`. The platform accepts protocol major 1 (any minor ≥ 0) and answers anything
else — including a missing or malformed header — with `426 Upgrade Required`. Every response
carries the platform's protocol; the CLI refuses a platform with a different major instead of
misbehaving. `/v1/meta`, Stripe webhooks and admin endpoints are exempt.

### Authentication lifecycle

Tokens are stored as SHA-256 hashes, expire after 90 days (`HIGHHX_TOKEN_TTL_DAYS`), can be
rotated (`/v1/auth/refresh`) and revoked (logout, `DELETE /v1/tokens/{id}`). Suspended accounts
are rejected on every endpoint. Plans come only from verified billing state (or an admin
grant): a subscription counts while Stripe reports it active, trialing or past due and its paid
period plus a 3-day grace has not ended. Nothing the client sends — body fields, headers, local
config — influences entitlements.

### The AI gateway

`POST /v1/ai/messages` accepts the CLI's provider-neutral request (system prompt,
messages, tool specs, optional model/provider) and streams events:

```text
event: text
data: {"text":"I'll run the tests first."}

event: completed
data: {"message":{...},"stop_reason":"tool_use","usage":{...},"model":"claude-opus-5"}
```

The upstream is chosen as: explicit provider → the model's provider → the account's
`upstream` setting → the platform default; configured fallbacks
(`HIGHHX_FALLBACK_PROVIDERS`) are tried only when the upstream fails before producing output.
Offering computer-use tools to the model requires the Pro computer-use entitlement.

**Idempotency and resume.** Each request carries an `Idempotency-Key`. The platform runs the
model at most once per (account, key) and meters it exactly once — the key is claimed in the
database with a unique constraint, so concurrent duplicates attach to the same stream. Events
carry sequential `id:`s; a client that reconnects with the same key and `Last-Event-ID` receives
only the events it missed. A stream with no connected client is cancelled after 30 s
(`stream_resume_grace`); finished streams are replayable for 10 minutes. The stream registry is
per process: with several platform processes, route a client's requests to the same one
(sticky sessions) for resume to work.

**Limits.** The monthly token allowance is checked before each request; at most 3 requests per
account run concurrently (`HIGHHX_MAX_CONCURRENT_STREAMS`); output is capped per request.
Usage is recorded per attempt with its outcome (`ok`, `error`, `cancelled`); retries use a new
key and are metered as the separate upstream calls they are. A cancelled stream records zero
tokens (providers report usage only at the end of a response).

## Running the platform

See [server/README.md](../server/README.md).
