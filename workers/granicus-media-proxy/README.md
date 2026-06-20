# Granicus media proxy

This Cloudflare Worker is a narrow, authenticated experiment for Granicus media fetches that return
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

Production Audio does not use this Worker yet. First deploy it, run the isolated GitHub-hosted
`worker` probe, and confirm that Worker-routed curl and ffmpeg succeed where the same runner's direct
requests receive 403.

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

The base URL is kept as a secret alongside the token so the experimental endpoint is not advertised
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

The result is sufficient to consider a production fallback only when:

- direct requests from the same runner receive 403;
- Worker curl ranges succeed with HTTP 206;
- Worker ffmpeg audio reads succeed;
- one bounded full download passes local ffprobe and local ffmpeg processing.

If the Worker also receives 403, do not deploy a production fallback; use a different egress design.

## 8. Rotate or remove

Rotate the bearer token by updating Cloudflare first, then the GitHub secret:

```bash
npx wrangler@4 secret put PROXY_TOKEN
gh secret set GRANICUS_PROXY_TOKEN
```

Delete the experimental Worker:

```bash
npx wrangler@4 delete
```

Then remove both GitHub secrets:

```bash
gh secret delete GRANICUS_PROXY_BASE_URL
gh secret delete GRANICUS_PROXY_TOKEN
```
