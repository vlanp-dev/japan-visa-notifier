# Ambassade alerts

Small self-hosted service that watches embassy pages and alerts the operator by
email when an alert episode opens. Email delivery uses
[Resend](https://resend.com) later in the delivery path; this foundation starts
and stays healthy without any email configuration.

The service has **no runtime dependencies**. It runs on Node.js 24 and only uses
built-in modules (`node:http`, `node:sqlite`, `node:crypto`, `node:test`, and the
global `fetch`).

## Requirements

- Node.js 24 or newer (24.22.2 is the version installed in this repository).

## Scripts

```sh
npm start    # start the HTTP service
npm test     # run the node:test suite with temporary databases
```

There is no build step and no `npm install` is required.

## Configuration

All configuration comes from environment variables.

| Variable | Default | Purpose |
| --- | --- | --- |
| `APP_ENV` | `development` | One of `development`, `test`, `production`. |
| `HOST` | `127.0.0.1` | Interface the HTTP server listens on. |
| `PORT` | `3000` | TCP port. `0` asks the operating system for an ephemeral port. |
| `DATA_DIR` | `./data` | Directory holding the SQLite database. Created automatically. |
| `DATABASE_PATH` | `<DATA_DIR>/alerts.sqlite` | Optional database file path. Relative paths resolve under `DATA_DIR`; `:memory:` is accepted. |
| `PUBLIC_BASE_URL` | `http://127.0.0.1:<PORT>` | Public origin used to build absolute links (acknowledgement, embassy page). Required in production and must be `http` or `https`. |
| `OPERATOR_USERNAME` | `operator` in development/test | HTTP Basic auth user. Required in production. |
| `OPERATOR_PASSWORD` | `development-password` in development/test | HTTP Basic auth password. Required in production (at least 12 characters). |
| `ACK_TOKEN_SECRET` | random, persisted in `app_secrets` | Optional HMAC secret used to sign acknowledgement links. When unset, a random secret is generated once and stored in the database. In production an explicit value must be at least 32 characters. |
| `LOG_LEVEL` | `info` | `debug`, `info`, `warn`, `error` or `silent`. |
| `MAX_BODY_BYTES` | `65536` | Maximum accepted request body size. |

Production **fails closed**: startup is refused when the operator credentials are
missing, use the development defaults, are too short, or when
`PUBLIC_BASE_URL` is missing. The development defaults exist only so that a local
service can start without configuration.

The service never stores the Resend API key in the database. A later task writes
it to a `0600` file; this foundation does not require it.

## HTTP surface

- `GET /health` is public, unauthenticated, and returns `{"status":"ok","database":"ok"}`.
- `GET /ack/<token>` and `POST /ack/<token>` are public signed links. The GET
  shows a confirmation page and never changes state; the POST acknowledges an
  open episode. A missing, malformed, tampered, expired or mismatched token
  returns an error page and changes nothing, as does a replay. The HMAC-signed
  token is the capability, so these two routes do not use Basic auth or the CSRF
  token.
- Every other route is protected by HTTP Basic auth.
- Other state-changing requests use a CSRF double-submit token. A `csrf_token`
  cookie is issued on safe requests and must be echoed in the `x-csrf-token`
  header or in the `_csrf` form field.
- Security headers are applied to every response, request bodies are capped by
  `MAX_BODY_BYTES`, and unexpected errors return a generic 500 without internal
  details.

## Acknowledgement links

Opening an episode stores a random nonce and an expiry (seven days) on the
episode and returns an absolute link built from `PUBLIC_BASE_URL`. The token is
`base64url(payload).base64url(HMAC-SHA256)`, where the payload carries the
episode id, the nonce and the expiry. A link is therefore single-episode and
single-use: once the episode is acknowledged, the same link only shows an
“already acknowledged” page. No link is stored in plaintext; the signing secret
comes from `ACK_TOKEN_SECRET` or from `app_secrets` when that variable is unset.

## Database

SQLite is opened through `node:sqlite` with foreign keys enabled, WAL mode and
idempotent migrations. The schema contains:

- `settings` — key/value operator settings (sender, recipient, sending
  subdomain in later tasks).
- `app_secrets` — internal secrets such as the acknowledgement signing secret.
- `episodes` — opened alert episodes, their acknowledgement state, and the
  per-episode nonce plus expiry that bind an acknowledgement link to the
  episode.
- `episode_rules` — rules triggered in an episode, one row per rule.
- `email_attempts` — one attempt per episode (`accepted`, `refused` or
  `not_attempted`), plus independent test sends; a partial unique index enforces
  a single attempt per episode.

The API key is deliberately absent from this schema.

## Security notes

- Serve the service behind TLS (reverse proxy) in production; Basic auth sends
  credentials with every request.
- The `data/` directory contains the database and, in later tasks, the Resend
  API key file. Restrict its permissions to the service account.
- Logs are JSON lines and redact credential-like keys and known secret values.

## Email provider constraints

Resend's Free plan limits, verified on the stored pricing page
(https://resend.com/pricing.md), are 100 emails per day and 3,000 per month. The
operator owns the `vlanp.com` sending subdomain and its DNS records (SPF, DKIM,
DMARC). No deliverability, open or click tracking is available or claimed.
