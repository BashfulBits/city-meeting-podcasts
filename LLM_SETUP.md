# LLM backend setup

The R2 adapter is configuration-only until a calling stage is enabled. It supports these
LiteLLM routes:

| Route | Environment secret |
| --- | --- |
| `gemini/gemini-3-flash-preview` | `GEMINI_API_KEY` |
| `deepseek/deepseek-v4-flash` / `deepseek/deepseek-v4-pro` | `DEEPSEEK_API_KEY` |
| `mistral/mistral-large-latest` | `MISTRAL_API_KEY` |

Install the optional dependency with `pip install -e ".[llm]"`. Select a direct route with
`LLM_MODEL=gemini/gemini-3-flash-preview` and `LLM_MODE=direct`. LiteLLM reads the provider key from the
matching environment variable; do not put keys in YAML, source, or episode records.

For the paced Mistral path, deploy the existing Worker and set `LLM_MODE=dispatch`,
`LLM_DISPATCH_URL=https://<worker-domain>`, and `LLM_DISPATCH_AUTH_TOKEN`. The Worker has its own
secrets (`DISPATCH_AUTH_TOKEN` and `UPSTREAM_API_KEY`) configured with `wrangler secret put`; those
values must match the client token and the provider/proxy key respectively. The Worker’s
`MODEL_ID` must match the provider-qualified route selected by the Python adapter.

Account and secret checklist (performed by the maintainer, never pasted into chat or committed):

1. Create an API key in Google AI Studio, DeepSeek Platform, and/or the Mistral Console for the
   providers you intend to use. Enable billing/quotas according to the provider’s current terms.
2. For local testing, export the corresponding key in the shell running the pipeline.
3. For GitHub Actions, add the key as a repository/environment secret (for example, `gh secret set
   GEMINI_API_KEY` prompts securely for the value). Use an environment-scoped secret for production.
4. For the Cloudflare Worker, from `workers/llm-dispatch-proxy/`, run `npx wrangler secret put
   DISPATCH_AUTH_TOKEN` and `npx wrangler secret put UPSTREAM_API_KEY`; do not pass either value as a
   command-line argument.

The first safe activation is Gemini direct mode with a small test job. DeepSeek can remain optional
until a budget is chosen. Mistral should use the Worker route because its rate limit is paced there.
