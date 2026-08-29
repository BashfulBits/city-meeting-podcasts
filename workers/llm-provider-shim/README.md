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

## Safety properties

This Worker forwards third-party API keys, so it must never become an open relay:

- The destination is a **fixed allowlist** (`UPSTREAMS` in `src/index.js`), never caller-controlled.
- An unset `SHIM_TOKEN` fails closed (503) rather than accepting unauthenticated traffic.
- A bad token, an unknown provider and a malformed path all return an identical opaque 404.
- Only `authorization`, `content-type` and `accept` are forwarded upstream; Cloudflare-injected
  headers and cookies are dropped.
- Paths containing `..` or `%` are rejected before any fetch.

## Tests

```bash
npm test
```
