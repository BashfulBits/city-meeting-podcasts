# LLM backend setup

The R2 adapter is configuration-only until a calling stage is enabled. It supports these
LiteLLM and multi-provider routes:

| Provider | Environment secret | Sign-up / Dashboard | Usage Mode | Notes |
| --- | --- | --- | --- | --- |
| **Google AI Studio** | `GEMINI_API_KEY`, `GEMINI_API_KEY_SECONDARY` | [aistudio.google.com](https://aistudio.google.com) | Direct & Dispatch | Gemma 4 26B/31B (29k Free RPD), Gemini 3.5/3.1 Flash Lite (1k Free RPD), Flash Burst |
| **Groq** | `GROQ_API_KEY` | [console.groq.com](https://console.groq.com) | Direct & Dispatch | Llama 3.3 70B (30 RPM / 1,000 Free RPD) |
| **SambaNova** | `SAMBANOVA_API_KEY` | [cloud.sambanova.ai](https://cloud.sambanova.ai) | Direct & Dispatch | Llama 3.3 70B & Qwen 2.5 72B (20 RPM / 1,000 Free RPD) |
| **Mistral AI** | `MISTRAL_API_KEY`, `MISTRAL_API_KEY_SECONDARY` | [console.mistral.ai](https://console.mistral.ai) | Direct & Dispatch | Mistral Large, Mistral Small 2603, Codestral, Devstral, Medium (account-specific monthly pools) |
| **Z.AI (Zhipu AI)** | `ZAI_API_KEY` | [z.ai](https://z.ai) | Direct & Dispatch | GLM-4.7-Flash & GLM-4.5-Flash (15 RPM / 500 Free RPD) |
| **SiliconFlow** | `SILICONFLOW_API_KEY` | [cloud.siliconflow.com](https://cloud.siliconflow.com) (**global site — not `.cn`**, see note below) | Direct & Dispatch | **Paid only for us:** DeepSeek-V4-Flash ($0.049/M promo) & Qwen 2.5 72B ($0.07/M). No free route — the routes stop working at a zero balance |
| **DeepSeek Direct** | `DEEPSEEK_API_KEY` | [platform.deepseek.com](https://platform.deepseek.com) | Direct & Dispatch | DeepSeek-V4-Flash ($0.14/M base, $0.0028 cache, $0.07 off-peak), DeepSeek-V4-Pro |
| **OpenRouter** | `OPENROUTER_API_KEY` | [openrouter.ai](https://openrouter.ai) | Direct & Dispatch | Curated Gemma 4, Nemotron 550B/120B free endpoints and frontier models |
| **Kilo Code** | `KILO_API_KEY` | [app.kilo.ai](https://app.kilo.ai) | Direct & Dispatch | StepFun Step-3.7-Flash & NVIDIA Nemotron-3-Ultra 550B (20 RPM / 200 Free RPD) |
| **OpenCode Zen** | `OPENCODE_API_KEY` | [opencode.ai/auth](https://opencode.ai/auth) | Direct & Dispatch | DeepSeek-V4-Flash (1M Context), MiMo-V2.5, Nemotron 3 Ultra |
| **NVIDIA build.nvidia.com** | `NVIDIA_API_KEY` | [build.nvidia.com](https://build.nvidia.com) | Direct & Dispatch | Kimi K3, DeepSeek V4 Pro/Flash, Gemma 4 31B, GPT-OSS 120B, Nemotron 3 Ultra/Super/Nano-Omni, Riva Translate 4B — **no published rate-limit table**; self-imposed 4 RPM and 40k TPM per route in `config/provider_limits.yml` (2 RPM / 18k TPM after `token_estimate_buffer` and `split_cap_multiplier`), plus `concurrency: 1`. Rate was never the binding limit — at 1.15 req/min we still took 57% 429s, ~35x under the ~40 RPM community-reported baseline; NVIDIA rejects *overlap*, so concurrency is the real cap. See the `nvidia` provider block |

> **SiliconFlow runs two separate platforms.** `siliconflow.com` (global) is the one
> `config/provider_limits.yml` calls, and `siliconflow.cn` (China) is a distinct service with its own
> accounts — a `.com` key is rejected by `.cn` with `401 "Api key is invalid"`. Only the `.cn` platform
> offers free models, and those are gated behind 实名认证 (Chinese real-name ID verification), so they
> are not reachable for this project. Every SiliconFlow route here is therefore `free: false` by
> design; SiliconFlow earns its place as the *cheapest paid* leg of `deepseek/deepseek-v4-flash`
> ($0.049/M vs DeepSeek Direct's $0.14/M), not as free capacity. With a zero balance its routes
> return `402 {"code":30001}` on every model — verified 2026-08-29, including
> `Qwen/Qwen2.5-7B-Instruct`, the model `.cn` documents as its free example.

For the full model evaluation matrix, quality ratings, and recommended task mappings, see the canonical [LLM Model Catalog & Decision Matrix in ARCHITECTURE.md](ARCHITECTURE.md#llm-model-catalog--decision-matrix).

Install the optional dependency with `pip install -e ".[llm]"`. Select any compiled logical route with
`LLM_MODEL=gemini/gemini-3-flash-preview` and `LLM_MODE=direct`. Python loads the generated
`citypods/compute/llm_routes.json` catalog, selects the physical provider/account route, and passes
its LiteLLM model selector, `api_base`, and environment-keyed credential to LiteLLM; do not put keys
in YAML, source, or episode records. Re-run `python scripts/compile_llm_limits.py` after changing
`config/provider_limits.yml`.

For the paced dispatch path (and multi-provider routing), deploy the Worker and set `LLM_MODE=dispatch`,
`LLM_DISPATCH_URL=https://<worker-domain>`, and `LLM_DISPATCH_AUTH_TOKEN`. The Worker's own provider
credentials and routing policies are defined in [`config/provider_limits.yml`](config/provider_limits.yml) and
compiled into `workers/llm-dispatch-proxy/src/dispatch_limits.json` (review/41). `DISPATCH_AUTH_TOKEN` is a
plain Worker secret matching the client token.

`LLM_MODE=direct` calls LiteLLM directly and prefers the direct transport. `LLM_MODE=dispatch` submits
to the Cloudflare Worker and never relies on runner provider credentials. A direct-capable caller can
set `LLMRequestPolicy(allow_dispatch_overflow=True)` to reach the Worker’s independent provider/account
pool; otherwise it remains direct. `ROUTES` is the logical-model view, while the generated physical
route registry preserves duplicate models across providers and accounts for selection and CAS ledger
keys.

Account and secret checklist (performed by the maintainer, never pasted into chat or committed):

1. Create API keys in Google AI Studio, Groq, SambaNova, Mistral Console, Z.AI, SiliconFlow, DeepSeek, OpenRouter, Kilo Code, OpenCode, and/or NVIDIA build.nvidia.com.
2. For local testing, export the corresponding keys in your shell.
3. For GitHub Actions, add the keys as repository/environment secrets (e.g., `gh secret set GROQ_API_KEY`,
   `gh secret set NVIDIA_API_KEY`).
4. For the Cloudflare Worker, from `workers/llm-dispatch-proxy/`, run `npx wrangler secret put DISPATCH_AUTH_TOKEN`
   plus `npx wrangler secret put <NAME>` for every `api_key_env` declared in [`config/provider_limits.yml`](config/provider_limits.yml)
   (`GEMINI_API_KEY`, `GEMINI_API_KEY_SECONDARY`, `GROQ_API_KEY`, `SAMBANOVA_API_KEY`,
   `MISTRAL_API_KEY`, `MISTRAL_API_KEY_SECONDARY`, `ZAI_API_KEY`,
   `SILICONFLOW_API_KEY`, `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY`, `KILO_API_KEY`, `OPENCODE_API_KEY`,
   `NVIDIA_API_KEY`). The same `npx wrangler secret put NVIDIA_API_KEY` must also be run from
   `workers/llm-dispatch-v2/` (review/44's coexisting v2 executor Worker reads the same `api_key_env`
   names from its own copy of the compiled catalog).
