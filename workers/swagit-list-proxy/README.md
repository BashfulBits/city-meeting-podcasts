# Swagit list proxy

This Cloudflare Worker is a narrow, authenticated fallback for Swagit archive **list/view page**
fetches (`SwagitProvider.fetch_episodes`) that return HTTP 403 from GitHub-hosted runners.
Diagnosed in PR #1011: paired local/GitHub-Actions probes plus production Audio #257–#259 showed
GitHub Actions egress gets a consistent 403 (`server: awselb/2.0` -- an AWS load balancer, not a
Cloudflare challenge) from every Swagit tenant, while the same requests succeed cleanly from a
normal residential network under heavier load. Same shape as the already-fixed Granicus media 403
(GH#300/#353), covering a different host class -- this Worker does **not** touch Granicus or the
existing `granicus-media-proxy`.

It is **not** a general URL proxy:

- only `GET` is accepted;
- requests require `Authorization: Bearer <PROXY_TOKEN>`;
- only configured Swagit tenant hostnames are accepted;
- only `/views/...` list-page paths (one or two segments) are accepted -- not video/download pages;
- only a single, bounded `page` query parameter is forwarded -- any other query is refused;
- upstream redirects are refused;
- responses stream without buffering and use `Cache-Control: no-store`.

## 1. Prerequisites

Same as `workers/granicus-media-proxy` -- a Cloudflare account with a Workers subdomain, Node.js 20+,
Wrangler, and GitHub CLI authenticated for `BashfulBits/city-meeting-podcasts`. Commands below pin
an exact Wrangler release (`4.114.0`, the version this Worker was first deployed and verified with)
rather than a floating `wrangler@4`, so a future Wrangler 4.x release can't change deploy/secret
behavior without a repo commit -- bump it deliberately, in one PR, everywhere it appears (this file
and `.github/workflows/swagit-worker-deploy.yml`'s `wranglerVersion`).

```bash
cd workers/swagit-list-proxy
npm test
npx wrangler@4.114.0 login
```

## 2. Generate one shared bearer token

Generate a random token locally (a **different** token from the Granicus proxy's -- do not reuse
it). Do not commit it or paste it into an issue, PR, or workflow input.

```bash
openssl rand -hex 32
```

## 3. Store the Cloudflare secret

From `workers/swagit-list-proxy`:

```bash
npx wrangler@4.114.0 secret put PROXY_TOKEN
```

The non-secret `ALLOWED_HOSTS` list is committed in `wrangler.jsonc`. Update it in code review
whenever another Swagit tenant is onboarded.

## 4. Deploy

```bash
npx wrangler@4.114.0 deploy
```

Record the final origin, for example:

```text
https://citypods-swagit-list-proxy.<your-subdomain>.workers.dev
```

## 5. Smoke-test from the Mac

```bash
export SWAGIT_PROXY_BASE_URL='https://citypods-swagit-list-proxy.<your-subdomain>.workers.dev'
export SWAGIT_PROXY_TOKEN='<the same 64-character token>'

curl --fail --silent --show-error \
  --header "Authorization: Bearer ${SWAGIT_PROXY_TOKEN}" \
  --output /dev/null \
  --write-out 'status=%{http_code}\n' \
  "${SWAGIT_PROXY_BASE_URL}/v1/swagit/austintx.new.swagit.com/views/117/city-council-meetings"
```

Expected: HTTP `200`. A request without the authorization header must return `401`.

## 6. Add GitHub Actions secrets

```bash
printf '%s' "${SWAGIT_PROXY_BASE_URL}" | gh secret set SWAGIT_PROXY_BASE_URL
printf '%s' "${SWAGIT_PROXY_TOKEN}" | gh secret set SWAGIT_PROXY_TOKEN
```

The base URL is kept as a secret alongside the token so the fallback endpoint is not advertised in
workflow logs.

## 7. Rollback

Config-only: unset **both** `SWAGIT_PROXY_BASE_URL` and `SWAGIT_PROXY_TOKEN` to cleanly revert to
direct-only fetches -- `citypods/swagit_proxy.py`'s `SwagitWorkerFallback.from_env()` returns
`None` only when both are unset, and `get_with_worker_fallback()` returns the plain direct
response unchanged. Unsetting only one of the pair is **not** a supported rollback path:
`from_env()` raises `ValueError` ("must be configured together"), which
`get_with_worker_fallback()` catches, logs, and still degrades to the direct (already-fetched)
response -- so it fails safe, but a half-configured pair is a misconfiguration worth cleaning up
rather than relying on.

## 8. Rotate or remove

```bash
npx wrangler@4.114.0 secret put PROXY_TOKEN
gh secret set SWAGIT_PROXY_TOKEN
```

```bash
npx wrangler@4.114.0 delete
gh secret delete SWAGIT_PROXY_BASE_URL
gh secret delete SWAGIT_PROXY_TOKEN
```

## 9. Automatic deployment after merge

`.github/workflows/swagit-worker-deploy.yml` runs only when a push to `main` changes:

- `workers/swagit-list-proxy/src/**`;
- `workers/swagit-list-proxy/wrangler.jsonc`; or
- the deployment workflow itself.

It runs `npm test` and then deploys with Cloudflare's official Wrangler action, reusing the same
`CLOUDFLARE_ACCOUNT_ID`/`CLOUDFLARE_API_TOKEN` secrets the Granicus proxy's deploy workflow already
uses (one Cloudflare account, two Workers). `PROXY_TOKEN` is not passed to the deployment workflow;
it stays an encrypted Worker secret in Cloudflare.
