# LLM backend setup

The R2 adapter is configuration-only until a calling stage is enabled. It supports these
LiteLLM routes:

| Route | Environment secret |
| --- | --- |
| `gemini/gemini-3-flash-preview` | `GEMINI_API_KEY` |
| `deepseek/deepseek-v4-flash` / `deepseek/deepseek-v4-pro` | `DEEPSEEK_API_KEY` |
| `mistral/mistral-large-2512` | `MISTRAL_API_KEY` |

Install the optional dependency with `pip install -e ".[llm]"`. Select a direct route with
`LLM_MODEL=gemini/gemini-3-flash-preview` and `LLM_MODE=direct`. LiteLLM reads the provider key from the
matching environment variable; do not put keys in YAML, source, or episode records.

For the paced Mistral path (and, on explicit opt-in only, Gemini overflow — see below), deploy the
Worker and set `LLM_MODE=dispatch`, `LLM_DISPATCH_URL=https://<worker-domain>`, and
`LLM_DISPATCH_AUTH_TOKEN`. The Worker's own provider credentials/routing are no longer Wrangler
config — see [`workers/llm-dispatch-proxy/README.md`](workers/llm-dispatch-proxy/README.md) and
[`config/provider_limits.yml`](config/provider_limits.yml) (review/41). `DISPATCH_AUTH_TOKEN` is still
a plain Worker secret matching the client token.

**A route that also offers `direct` (today only Gemini) is never automatically sent through the
Worker** — a caller must set `LLMRequestPolicy(allow_dispatch_overflow=True)` to reach a
Worker-only-visible account (e.g. `GEMINI_API_KEY_SECONDARY`) once its own direct route's quota is
exhausted; otherwise it always calls Gemini directly, even when `LLM_DISPATCH_URL` happens to be
configured for an unrelated reason. Mistral has no direct route at all, so it always dispatches.

Account and secret checklist (performed by the maintainer, never pasted into chat or committed):

1. Create an API key in Google AI Studio, DeepSeek Platform, and/or the Mistral Console for the
   providers you intend to use. Enable billing/quotas according to the provider’s current terms. A
   second Google AI Studio project/key (`GEMINI_API_KEY_SECONDARY`) is optional, only needed if the
   Worker's Gemini overflow route is in use.
2. For local testing, export the corresponding key in the shell running the pipeline.
3. For GitHub Actions, add the key as a repository/environment secret (for example, `gh secret set
   GEMINI_API_KEY` prompts securely for the value). Use an environment-scoped secret for production.
4. For the Cloudflare Worker, from `workers/llm-dispatch-proxy/`, run `npx wrangler secret put
   DISPATCH_AUTH_TOKEN` plus `npx wrangler secret put <NAME>` for every `api_key_env` named in
   `config/provider_limits.yml`'s `accounts` blocks (e.g. `MISTRAL_API_KEY`, `GEMINI_API_KEY`,
   `GEMINI_API_KEY_SECONDARY`, `DEEPSEEK_API_KEY`); do not pass any value as a command-line argument.

The first safe activation is Gemini direct mode with a small test job. DeepSeek can remain optional
until a budget is chosen. Mistral should use the Worker route because its rate limit is paced there.
