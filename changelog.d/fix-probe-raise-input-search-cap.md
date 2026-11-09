- **LLM rate probe phase 2 now searches past 250k tokens.** The input-ceiling search was capped
  at a hard-coded 250,000, so 1M-context routes (OrcaRouter DeepSeek and GLM, Nemotron Ultra) could
  never be shown to accept more. The cap is now `--max-search-tokens` (default 1,000,000, still
  bounded by each route's advertised context), probes scale their request timeout with size
  instead of failing at 15s, and `llm-rate-probe.yml` gains `route` and `max_search_tokens`
  inputs. The weekly provider-catalog context scan uses its own search (524,288 tokens per call)
  and is unchanged.
- **Probe workflow inputs can no longer add CLI arguments, and endurance mode honours the cap.**
  `llm-rate-probe.yml` now builds its command as a Bash array (a value such as
  `1000000 --apply` could previously turn a dry run live), validates `max_search_tokens` as a whole
  number, and allows 25 minutes of probing inside the job's 30-minute timeout. `--endurance` now
  passes `--max-search-tokens` to its ceiling phase.
