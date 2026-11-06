- **Tagger models update and NVIDIA Nemotron 3.5 Super VL route wiring.** Updated
  `topic-tags:tagger` lane in `config/site_config.yml` to retire `kilo/stepfun/step-3.7-flash:free`,
  promote `gemini/gemini-3.5-flash-lite` (AA 11.7, 1,000 RPD) to primary recipe model, add
  `openrouter/nvidia/nemotron-3.5-super-vl:free` (AA 13.6, 400 RPD, pooled across Kilo, OpenRouter,
  and NVIDIA) as secondary spill capacity, retain `deepseek/deepseek-v4-flash` (AA 32.6, 1,800 RPD),
  and demote `gemini/gemini-3.1-flash-lite` to last priority so `chapter-agenda` can more fully
  utilize Flash-Lite 3.1 quota. Wired model aliases in `config/provider_limits.yml` for
  `nvidia/nemotron-3.5-super-vl:free`, `nvidia/nemotron-3.5-super-vl`, and
  `openrouter/nvidia/nemotron-3.5-super-vl` to canonical
  `openrouter/nvidia/nemotron-3.5-super-vl:free`, and set `upstream_429_default: upstream_capacity`
  on the direct NVIDIA route. *Backfill story:* Promoting Flash-Lite 3.5 as primary updates
  `tag_input_fingerprint` and `tag_recipe_hash`, gradually re-tagging catalog episodes on routine
  runs within the lane's daily ingress and queue caps.
