# Ambassade alerts

Small self-hosted service that watches embassy pages and alerts the operator by
email when an alert episode opens. Email delivery uses
[Resend](https://resend.com) through the delivery path described below; this
service starts and stays healthy without any email configuration.

The service has **no runtime dependencies**. It runs on Node.js 24 and only uses
built-in modules (`node:http`, `node:sqlite`, `node:crypto`, `node:test`, and the
global `fetch`).

## Requirements

- Node.js 24 or newer (24.22.2 is the version installed in this repository).

## Scripts

```sh
npm start    # start the HTTP service
npm test     # run the node:test suite, including offline acceptance tests
```

The suite uses temporary databases and an injected fake Resend `fetch`; it never
contacts the provider. There is no build step and no `npm install` is required.

## Configuration

All configuration comes from environment variables.

| Variable | Default | Purpose |
| --- | --- | --- |
| `APP_ENV` | `development` | One of `development`, `test`, `production`. |
| `HOST` | `127.0.0.1` | Interface the HTTP server listens on. |
| `PORT` | `3000` | TCP port. `0` asks the operating system for an ephemeral port. |
| `DATA_DIR` | `./data` | Directory holding the SQLite database. Created automatically. |
| `DATABASE_PATH` | `<DATA_DIR>/alerts.sqlite` | Optional database file path. Relative paths resolve under `DATA_DIR`; `:memory:` is accepted. |
| `RESEND_API_KEY_FILE` | `<DATA_DIR>/resend-api-key` | Optional path of the `0600` file holding the Resend API key. Relative paths resolve under `DATA_DIR`. |
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

The service never stores the Resend API key in the database. The operator sets
it through the settings flow; the value is written atomically to a file with
mode `0600` (by default `<DATA_DIR>/resend-api-key`) and re-read on every send.
Absent or blank form fields keep the stored value, so the masked key can be left
untouched. A missing or unreadable key is reported as an incomplete
configuration and never stops the service.

## Email configuration and composition

Email settings are stored in the `settings` table:

- `email.sender` — the single `From` address.
- `email.recipient` — the single operator address (no recipient lists).
- `email.sending_subdomain` — the sending subdomain of `vlanp.com`, for example
  `alertes`. The operator owns that subdomain and its DNS records.

`EmailSettingsService.getConfigurationState()` reports the missing fields
without contacting Resend. It returns the non-secret settings, whether the API
key file is present, and French labels for every missing field. The key itself
is never returned, rendered in HTML or written to logs or error messages.

`composeAlertEmail()` builds a French message containing every triggered rule,
the modified excerpt, the embassy page URL and the signed acknowledgement URL.
`composeTestEmail()` builds a separate French configuration-test message. All
interpolated values are HTML-escaped, URLs are restricted to absolute `http(s)`
URLs, and the messages make no deliverability, opening or click claim.

## Delivery path and one-attempt guarantee

`EmailService.deliverEpisode()` accepts the result of opening an episode
(`{ episode, acknowledgementUrl }`) and sends the alert email once. Before any
provider request it inserts a durable claim in `email_delivery_claims`, keyed by
the episode id. The claim is committed first, so two rules in one episode, two
concurrent requests, two service instances over the same database, or a restart
after the claim cannot produce a second provider request. In-process calls are
also coalesced by an in-flight map.

The persisted outcome is exactly one of:

- `accepted` — Resend returned a 2xx response and a message id. This means the
  provider accepted the send; it is not a delivery, opening or click
  confirmation.
- `refused` — Resend returned a non-2xx response, or the request failed at the
  network/timeout level. The reason, including the provider status and name, is
  stored.
- `not_attempted` — a required setting or the API key is absent or unreadable.
  No provider request is made and the reason lists the missing fields.

A claim without a persisted attempt (for example a crash during the provider
call) is never retried automatically: later calls return an explicit
indeterminate result with no attempt row and no provider request. Claims and
attempts cascade when their episode is deleted.

`EmailService.getBannerState()` is computed only from persisted rows: it counts
`refused` attempts recorded after the most recent `accepted` attempt, ignoring
`not_attempted`. The banner becomes visible at two refusals, clears on the next
acceptance, and survives a restart because no counter is kept in memory.
`EmailService.sendTestEmail()` records a separate `test` attempt with the same
three states.

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

## Operator interface

The operator interface is served in French and protected by HTTP Basic auth:

- `GET /` — dashboard: configuration completeness, failure banner, episodes with
  their signed acknowledgement-page links, and the persisted email attempts.
- `GET /settings`, `POST /settings` — sender, recipient and sending subdomain,
  plus a write-only API key field. Blank fields keep the stored value and the key
  is never sent back to the browser.
- `GET /episodes/new`, `POST /episodes` — declare one episode with several rules
  in a single submission. The episode is persisted, then one email attempt is
  recorded for it.
- `POST /email/test` — send the separate configuration-test email.

All state-changing forms include the CSRF double-submit token. The dashboard
labels persisted attempts exactly `Accepté par le fournisseur`, `Refusé` or
`Non tenté`, alongside the stored provider reason, and shows the failure banner
only when at least two refusals have been recorded since the last acceptance.
No screen makes a deliverability, opening or click claim.

## Operator setup

1. **Start the service with its environment.** Set `APP_ENV=production`, the
   listening `HOST`/`PORT`, the `DATA_DIR` holding the database and private key
   file, the public `PUBLIC_BASE_URL` used in emailed acknowledgement links, and
   `OPERATOR_USERNAME`/`OPERATOR_PASSWORD` for HTTP Basic auth. Production
   refuses to start if the public URL or operator credentials are missing or
   weak. `ACK_TOKEN_SECRET` is optional: when unset, a random secret is generated
   once and stored in the database so acknowledgement links survive restarts.

   ```sh
   APP_ENV=production \
   HOST=127.0.0.1 PORT=3000 \
   PUBLIC_BASE_URL=https://alertes.example.org \
   DATA_DIR=/var/lib/ambassade-alerts \
   OPERATOR_USERNAME=operateur OPERATOR_PASSWORD='choose-a-long-password' \
   npm start
   ```

2. **Prepare the sending subdomain in Resend.** Create the subdomain of
   `vlanp.com` used for sending (for example `alertes.vlanp.com`) and publish its
   DNS records (SPF, DKIM and DMARC) in the operator's DNS zone. This is the
   operator's responsibility and outside this service.

3. **Configure email through the interface.** Sign in with Basic auth, open
   `/settings`, and fill in the sender (for example
   `alertes@alertes.vlanp.com`), the single recipient, the sending subdomain
   (`alertes`, without the domain) and the Resend API key. Saving writes the key
   atomically to `<DATA_DIR>/resend-api-key` (or `RESEND_API_KEY_FILE`) with mode
   `0600`, outside SQLite. The key is re-read on every send, never rendered back
   to the browser and never written to logs. Leave a field blank to keep its
   stored value.

4. **Send a configuration test.** The dashboard's “Envoyer un email de test”
   action records a separate `test` attempt. A valid configuration normally
   shows `Accepté par le fournisseur` with the provider message id.

5. **Declare episodes.** Open `/episodes/new` and submit every rule triggered at
   the same control in one form. The service persists one episode and makes
   exactly one email attempt for it, however many rules are listed.

## Manual live checks

The automated suite is offline: it injects a fake Resend `fetch` and never
contacts the provider. The following checks are manual and use the operator's
real Resend account.

- **Valid key.** Save a valid sender, recipient, subdomain and key, then send a
  test email. The dashboard must show `Accepté par le fournisseur` and the
  provider message id. This only means Resend accepted the request; the service
  cannot and does not claim delivery, opening or clicks.
- **Invalid key.** Temporarily save a deliberately invalid but well-formed key
  (for example `re_invalid_key`). Send a test email: the state must be `Refusé`
  with the provider's reason visible, for example an HTTP 401/`validation_error`
  message, and the service must stay reachable. A second failure raises the
  “Échecs d’envoi répétés” banner. Restore the valid key and send another test;
  an acceptance clears the banner.
- **One attempt with two rules.** Declare one episode containing two rules.
  Confirm a single row in “Tentatives d’envoi” and a single submission in the
  Resend activity; no second email is sent for the second rule.
- **Missing configuration.** Remove the key file or leave a required setting
  empty, then declare an episode. The state must be `Non tenté` and no provider
  request is made; the reason lists the missing fields.

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

- `settings` — key/value operator settings, including the email sender,
  recipient and sending subdomain.
- `app_secrets` — internal secrets such as the acknowledgement signing secret.
- `episodes` — opened alert episodes, their acknowledgement state, and the
  per-episode nonce plus expiry that bind an acknowledgement link to the
  episode.
- `episode_rules` — rules triggered in an episode, one row per rule.
- `email_attempts` — one attempt per episode (`accepted`, `refused` or
  `not_attempted`), plus independent test sends; a partial unique index enforces
  a single attempt per episode.
- `email_delivery_claims` — durable pre-send reservation keyed by episode id. A
  claim is completed in the same transaction as its final attempt; an unfinished
  claim blocks any further provider request for that episode.

The API key is deliberately absent from this schema; it lives only in the
private key file.

## Security notes

- Serve the service behind TLS (reverse proxy) in production; Basic auth sends
  credentials with every request.
- The `data/` directory contains the database and the `0600` Resend API key
  file. Restrict the directory to the service account.
- Logs are JSON lines and redact credential-like keys and known secret values.

## Email provider constraints

Resend's Free plan limits, as verified on 2026-11-04 on the stored pricing page
(https://resend.com/pricing.md), are **100 emails per day** and **3,000 per
month**. They are not assumed from another source; re-check that stored page
before relying on a different figure.

The service sends at most one alert email per opened episode, plus one email per
manual configuration test. It never retries a refused send automatically and
never batches a digest, so the volume stays far below the daily ceiling. The
operator owns the `vlanp.com` sending subdomain and its DNS records (SPF, DKIM,
DMARC). There is no provider webhook and no deliverability, open or click
tracking; the service records only the provider's accepted/refused response.
