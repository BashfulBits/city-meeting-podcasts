- **Uncap LLM dispatch runner submissions and optimize R6 moments routing.** Removed
  client-side `max_dispatches_per_run` throttling across all LLM lanes to rely exclusively on
  Cloudflare Dispatch v2 daily write unit budgets and ceilings. Sized R6 moments extraction
  with size-aware routing: small packets (<= 45,000 tokens) use Gemini 3.5 Flash Lite with
  high-capacity routes in backup; large packets (> 45,000 tokens) prioritize GLM 5.3 Flash,
  DeepSeek V4 Flash, and Kimi K3 with remaining models in backup, dropping ingress write units
  from 12 to 6 per job. Removed `gemini/gemini-3.1-flash-lite` from moments to eliminate
  contention with `chapter-agenda`.
