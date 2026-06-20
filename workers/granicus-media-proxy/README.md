# Granicus media proxy

This Cloudflare Worker is a narrow, authenticated fallback for Granicus media fetches that return
HTTP 403 from GitHub-hosted runners but succeed from other networks.

It is **not** a general URL proxy:

- only `GET` and `HEAD` are accepted;
- requests require `Authorization: Bearer <PROXY_TOKEN>`;
- only configured Granicus tenants are accepted;
- the upstream host is hard-coded to `archive-video.granicus.com`;
- filenames must be tenant-prefixed `.mp4` archive objects;
- queries, encoded paths, traversal, and upstream redirects are refused;
- only selected range/cache validators are forwarded;
- responses stream without buffering and use `Cache-Control: no-store`.

Production Audio always tries the canonical Granicus archive object directly first. Only an
immediate HTTP 403 can trigger one Worker attempt, under the same Granicus local limiter,
distributed lease, and circuit admission. Before activating a changed fallback, run the isolated
GitHub-hosted `worker` probe and its optional full production-recipe encode.

## 1. Prerequisites

- A Cloudflare account with a Workers subdomain.
- Node.js 20 or newer.
- Cloudflare Wrangler (the commands below use `npx wrangler@4`).
- GitHub CLI authenticated for `BashfulBits/city-meeting-podcasts`.

From this directory:

```bash
cd workers/granicus-media-proxy
npm test
npx wrangler@4 login
```

## 2. Generate one shared bearer token

Generate a random token locally. Do not commit it or paste it into an issue, PR, or workflow input.

```bash
openssl rand -hex 32
```

Copy the resulting 64-character value. The same value is stored independently in Cloudflare and
GitHub.

## 3. Store the Cloudflare secret

From `workers/granicus-media-proxy`:

```bash
npx wrangler@4 secret put PROXY_TOKEN
```

Paste the token when Wrangler prompts. It is encrypted by Cloudflare and is not written to
`wrangler.jsonc`.

The non-secret `ALLOWED_TENANTS` list is committed in `wrangler.jsonc`. Update that list in code
review whenever another native Granicus archive tenant is added.

## 4. Deploy

```bash
npx wrangler@4 deploy
```

The first deployment may ask you to create a `workers.dev` subdomain. Record the final origin, for
example:

```text
https://citypods-granicus-media-proxy.<your-subdomain>.workers.dev
```

Use the origin only—no `/v1/archive/...` path and no trailing query string.

## 5. Smoke-test from the Mac

```bash
export GRANICUS_PROXY_BASE_URL='https://citypods-granicus-media-proxy.<your-subdomain>.workers.dev'
export GRANICUS_PROXY_TOKEN='<the same 64-character token>'

curl --fail --silent --show-error \
  --header "Authorization: Bearer ${GRANICUS_PROXY_TOKEN}" \
  --range 0-1048575 \
  --output /dev/null \
  --write-out 'status=%{http_code} bytes=%{size_download}\n' \
  "${GRANICUS_PROXY_BASE_URL}/v1/archive/fortworthgov/fortworthgov_e4cc067f-6b2d-11f1-9494-005056a89546.mp4"
```

Expected: HTTP `206` and approximately 1 MiB downloaded. A request without the authorization header
must return `401`.

## 6. Add GitHub Actions secrets

From the repository root:

```bash
printf '%s' "${GRANICUS_PROXY_BASE_URL}" | gh secret set GRANICUS_PROXY_BASE_URL
printf '%s' "${GRANICUS_PROXY_TOKEN}" | gh secret set GRANICUS_PROXY_TOKEN
```

The base URL is kept as a secret alongside the token so the fallback endpoint is not advertised
in workflow logs. Neither value is included in the uploaded probe artifact.

## 7. Run the isolated GitHub-hosted probe

Use the Actions UI:

1. Open **Actions → Granicus probe → Run workflow**.
2. Select `worker`.
3. Keep the initial limits at 16 MiB, 512 MiB, and one full download.
4. Run the workflow.

Or use GitHub CLI:

```bash
gh workflow run granicus-probe.yml \
  -f probe_kind=worker \
  -f range_mib=16 \
  -f full_download_max_mib=512 \
  -f full_download_count=1
```

The workflow shares Audio's concurrency group and verifies no Audio run is active or queued. Download
the `granicus-probe-results` artifact and inspect `granicus-worker-results.json`.

The result is sufficient to activate or retain the production fallback only when:

- direct requests from the same runner receive 403;
- Worker access returns media successfully (HTTP 206 when the object honors Range, or an authenticated
  HTTP 200 classified as `range_unsupported` when the upstream sends the whole object);
- Worker ffmpeg audio reads succeed;
- one bounded full download passes local ffprobe and local ffmpeg processing.
- before a new production activation, one Arlington or Pflugerville object completes the optional
  full production-recipe encode.

If the Worker also receives 403, do not deploy a production fallback; use a different egress design.

Once activated, evaluate the fallback over the three post-activation `audio.yml` runs required by
GH#337 using the per-tenant Worker-fallback counters in each run's build log
(`provider granicus.com granicus worker fallback: N attempts, …`) and the run summary's
`provider_rate_limit_telemetry`: it is effective when `worker_fallback_successes` ≈
`worker_fallback_attempts` with failures ≈ 0 per Granicus tenant, Granicus circuit trips/deferrals
fall to ~0, and episode URLs/keys/durations are unchanged. Rollback is config-only — unset
`GRANICUS_PROXY_BASE_URL`/`GRANICUS_PROXY_TOKEN` to revert to direct-only fetches. Full criteria live
in `review/12` §Granicus follow-up.

## 8. Rotate or remove

Rotate the bearer token by updating Cloudflare first, then the GitHub secret:

```bash
npx wrangler@4 secret put PROXY_TOKEN
gh secret set GRANICUS_PROXY_TOKEN
```

Delete the Worker:

```bash
npx wrangler@4 delete
```

Then remove both GitHub secrets:

```bash
gh secret delete GRANICUS_PROXY_BASE_URL
gh secret delete GRANICUS_PROXY_TOKEN
```

## 9. Automatic deployment after merge

The repository workflow `.github/workflows/granicus-worker-deploy.yml` runs only when a push to
`main` changes:

- `workers/granicus-media-proxy/src/**`;
- `workers/granicus-media-proxy/wrangler.jsonc`; or
- the deployment workflow itself.

It runs `npm test` and then deploys with Cloudflare's official Wrangler action. Ordinary repository
changes do not redeploy the Worker. The workflow can also be run manually from the Actions UI.

Create a narrowly scoped Cloudflare API token:

1. In Cloudflare, open **My Profile → API Tokens → Create Token**.
2. Use **Edit Cloudflare Workers**.
3. Restrict account resources to the account containing this Worker.
4. Copy the token once; Cloudflare will not show it again.

Find the account ID with:

```bash
npx wrangler@4 whoami
```

Add both deployment credentials to GitHub:

```bash
printf '%s' '<Cloudflare account ID>' | gh secret set CLOUDFLARE_ACCOUNT_ID
printf '%s' '<scoped Edit Workers API token>' | gh secret set CLOUDFLARE_API_TOKEN
gh secret list
```

`PROXY_TOKEN` is deliberately not passed to the deployment workflow. It remains an encrypted Worker
secret in Cloudflare and survives ordinary source/config deployments. Rotate it separately using
`wrangler secret put` only when credential rotation is intended.
