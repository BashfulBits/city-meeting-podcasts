# City request intake Worker

This Worker is the R12 public intake bridge:

```text
Formspark → Worker → private D1 request/contact record
                    → GitHub App creates canonical add-city issue
                    → Discord #city-requests notification
                    → Resend acknowledgement email
```

The Worker validates and acknowledges the Formspark webhook immediately, then completes the D1,
GitHub, Discord, and Resend handoff under `ctx.waitUntil()`. This stays inside Formspark's short
webhook timeout; Formspark does not retry webhook delivery.

The requester email is stored only in D1. It is deliberately excluded from GitHub issue bodies,
Discord payloads, logs, and the R12 discovery state.

Production base URL: `https://citypods-city-request-intake.citypods.workers.dev`.

## Provisioning

Do this with the maintainer when the `citymeetings.fyi` mailbox and form are ready:

1. Create the D1 database: `npx wrangler d1 create citypods-city-requests`; copy its ID into
   `wrangler.jsonc`.
2. Apply the schema: `npx wrangler d1 migrations apply citypods-city-requests --remote`.
3. Apply every D1 migration before deploying a newer Worker. Migration `0002_request_origins.sql`
   stores only canonical issue/origin IDs and per-channel idempotency keys; requester email remains
   in `city_requests` only.
4. Set Worker secrets with `wrangler secret put` (never commit them):
   `FORMSPARK_WEBHOOK_SECRET`, `GITHUB_APP_ID`,
   `GITHUB_APP_INSTALLATION_ID`, `GITHUB_APP_PRIVATE_KEY`, `DISCORD_WEBHOOK_URL`,
   `DISCORD_PUBLIC_KEY`, `STATUS_WEBHOOK_SECRET`, `RESEND_API_KEY`, `MAIL_FROM`, and optionally
   `MAIL_REPLY_TO`.
5. Create a GitHub App with Issues read/write permission, install it only on this repository, and
   use its installation ID. The Worker mints a short-lived installation token; it does not keep a
   long-lived GitHub token. Keep the explicit GitHub `User-Agent` headers in `src/index.js`; GitHub's
   edge returned an empty `403` when they were omitted during the production smoke test.
6. Generate a URL-safe random `FORMSPARK_WEBHOOK_SECRET` and save it with `wrangler secret put`.
   In Formspark, configure its JSON webhook URL as
   `https://<worker>.workers.dev/formspark/<FORMSPARK_WEBHOOK_SECRET>`. Formspark does not sign
   webhooks or support custom headers, so the unguessable URL segment is the shared-secret check
   recommended by its documentation. Never put this URL in public website code, Git, or chat.
7. In Formspark's form settings, choose **Turnstile** under Spam Protection and add the
   Turnstile secret key there. Add the corresponding site-key widget to the public form.
   Formspark verifies the single-use token before sending the webhook; the Worker must not verify
   that same token a second time.
8. Configure the Resend sender/reply address at `citymeetings.fyi`, including the required SPF,
   DKIM, and DMARC records. Set `MAIL_FROM` only after domain verification completes.

The Worker accepts JSON fields `city_state` (`City, ST`), `email`, and optionally `provider`,
`source_url`, `city_website`, and `notes`. It deduplicates the same requester,
city, state, and meeting URL atomically in D1, then returns the canonical GitHub issue URL.

## Community intake and lifecycle callbacks

- Discord uses an HTTP interaction endpoint at `/discord/interactions`. Create a Discord application,
  set its public key as `DISCORD_PUBLIC_KEY`, set the Worker URL as the application's Interactions
  Endpoint URL, and register a guild command named `request-city` with required string options `city`
  and `state`; optional options are `provider`, `source_url`, `city_website`, and `notes`. The Worker
  validates Discord's Ed25519 signature, immediately sends an ephemeral deferred response, creates the
  canonical issue, and edits that response with the issue URL. No gateway process or always-on bot is
  required.
- Generate a separate `STATUS_WEBHOOK_SECRET`. Set it in the Worker, then add the complete secret URL
  `https://citypods-city-request-intake.citypods.workers.dev/status/<secret>` as the repository Actions
  secret `CITY_REQUEST_STATUS_WEBHOOK_URL`. R12 workflows post evidence-ready, research-only, batched,
  applied, and expiry transitions there. Per-channel hashes make repeated workflow delivery idempotent.
- The Discord webhook is executed with `wait=true`; its returned message ID is stored in D1. Later
  lifecycle events edit that same `#city-requests` message rather than adding a noisy stream of posts.
- GitHub Discussions uses the committed `city-requests.yml` category form and
  `r12-discussions.yml`. Enable Discussions and create a non-poll category named **City requests**
  (slug `city-requests`). The Action creates a canonical issue with `source:discussion`, replies with
  its URL, and posts later lifecycle transitions back to the originating Discussion.
- The repository must contain `source:discord` and `source:discussion` labels before those intake paths
  are enabled.

## Production verification

On 2026-07-15, controlled test issue #926 verified Formspark and Turnstile intake, D1 persistence,
GitHub App issue creation and labels, Discord `#city-requests` notification, Resend acknowledgement,
and exclusion of requester email from public surfaces. The issue was closed and its test D1 row was
deleted after verification.

## Email templates

`src/templates.js` carries separate HTML and plaintext content for submission receipt, evidence
ready, review batching, application, missing information, research-only, and evidence-expiry
events. The HTML uses restrained inline tokens rather than hard-coding the future R8 visual system;
the plaintext variant remains complete and authoritative.
