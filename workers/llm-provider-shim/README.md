# citypods-llm-provider-shim

A thin URL-rewriting shim between Cloudflare AI Gateway and providers whose API path the gateway
cannot express.

## Why this exists

AI Gateway rewrites the **last path segment** of a Custom Provider's registered Base URL to a
hardcoded `v1` before appending the caller's path. This is undocumented and contradicts the
[documented](https://developers.cloudflare.com/ai-gateway/configuration/custom-providers/)
`{base_url}/{provider-path}` join. Verified 2026-08-29 with a throwaway custom provider pointed at
an echo service:

| registered `base_url` | actual upstream prefix |
| --- | --- |
| `https://host/anything/prefix` | `https://host/anything/**v1**` |
| `https://host/anything/v1` | `https://host/anything/v1` |
| `https://host/anything/a/b` | `https://host/anything/a/**v1**` |

A provider whose API prefix does not end in `v1` is therefore unreachable through the gateway.
z.ai's `/api/paas/v4` becomes `/api/paas/v1`, which 404s; no `v1`-containing path serves its API,
so no Base URL can express it. Registering this Worker instead — and letting it restore the real
prefix — keeps such providers inside AI Gateway's logging rather than bypassing the gateway.

OpenCode is routed here too, for a cause never identified from outside: its own prefix (`/zen/v1`)
survives the `v1` substitution unchanged, so its gateway URL was already correct, yet direct gateway
calls still 404'd — and replaying the gateway's full header set on a direct call would not
reproduce it. Going through the shim resolves it. That also disproved the leading theory that
opencode.ai rejects Cloudflare-edge traffic, since this shim is itself a Worker.

Providers that *can* be expressed directly should be, and are not routed here. NVIDIA and SambaNova
only needed their `ai_gateway_chat_path` to carry the base path; Kilo only needs its Base URL
registered as `https://api.kilo.ai/api/gateway/v1`, because Kilo genuinely serves that path.

## Registering it

The trailing segment of the Base URL is **deliberately sacrificial** — the gateway eats it and
substitutes `v1`, which is what makes the rewrite predictable rather than accidental:

```
https://<worker-host>/<SHIM_TOKEN>/<provider>/x
  -> gateway calls: /<SHIM_TOKEN>/<provider>/v1/<caller path>
  -> shim calls:    <real upstream prefix>/<caller path>
```

Set the secret first, then register the Custom Provider with the resulting URL:

```bash
npx wrangler secret put SHIM_TOKEN
```

The token lives in the URL path because AI Gateway **strips `cf-aig-authorization`** before the
upstream sees it (confirmed by the same echo probe), leaving no gateway-supplied credential to
authenticate against. Treat the registered Base URL as a secret.

## Rotating `SHIM_TOKEN`

Rotation is a coupled change: update the deployed Worker's `SHIM_TOKEN` **and both** AI Gateway
Custom Provider Base URLs (`zai` and `opencode`) to the same new token. Updating only the secret
or only the registrations produces opaque 404s for the mismatched routes. The Worker accepts
only one token, so these independent updates cannot provide a zero-downtime rotation.

1. Schedule a short maintenance window and pause/drain dispatch traffic using both shim routes,
   including in-flight requests and manual callers. Keep the previous secret and both complete
   Base URLs in a secure place for rollback; never paste them into issues or logs.
2. From `workers/llm-provider-shim`, run `npx wrangler secret put SHIM_TOKEN` and enter the new
   token at the interactive prompt. This updates the deployed Worker; the normal deployment
   workflow does not rotate this secret. See the [Wrangler secret documentation](https://developers.cloudflare.com/workers/wrangler/commands/general/#secret-put).
3. In AI Gateway, replace the token segment in **both** registered Base URLs:
   `https://<worker-host>/<new-token>/zai/x` and
   `https://<worker-host>/<new-token>/opencode/x`. Preserve the host, provider segment, and
   sacrificial trailing `/x`; changing provider API keys is not part of this rotation.
4. Probe each route through AI Gateway with its existing provider credentials. The existing
   `tests/live/test_ai_gateway_contract.py::test_custom_provider_route_reaches_its_upstream`
   contract test is the reference for detecting routing failures. Verify both probes actually
   ran (missing credentials cause skips), reach the expected upstream JSON response, and do not
   return the shim's opaque 404. Also verify the old token no longer authenticates to the Worker.
5. Resume dispatch only after both routes pass. If validation fails, restore the previous Worker
   secret **and both** previous Base URLs, validate both routes again, then resume. Retire the old
   secret from the rollback store after the successful rotation is confirmed.

## Safety properties

This Worker forwards third-party API keys, so it must never become an open relay:

- The destination is a **fixed allowlist** (`UPSTREAMS` in `src/index.js`), never caller-controlled.
- An unset `SHIM_TOKEN` fails closed (503) rather than accepting unauthenticated traffic.
- A bad token, an unknown provider and a malformed path all return an identical opaque 404.
- Only `authorization`, `content-type` and `accept` are forwarded upstream; Cloudflare-injected
  headers and cookies are dropped.
- Paths containing `..` or `%` are rejected before any fetch.
- Upstream redirects are **refused**, not followed (`redirect: "manual"`, then 502). Workers'
  `fetch` defaults to `follow` and — unlike a browser — replays every header, `Authorization`
  included, to the redirect target even cross-origin, so following one would hand the provider's
  API key to whatever host the `Location` named. Same guard as `workers/granicus-media-proxy`.

## Tests

```bash
npm test
```
