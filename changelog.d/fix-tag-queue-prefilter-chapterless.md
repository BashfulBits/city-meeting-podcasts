- **Tag lane no longer queues episodes that can never need an LLM tag.** `episode_needs_tagging`
  counted every chapterless episode as "LLM pending" forever (it can never resolve a recipe hash),
  so the lane's global queue held ~19k of ~20.5k candidates that `stage_is_dirty` then skipped as
  `completion-cache` (2026-11-08 run). The pre-filter now mirrors the stage's trigger (an LLM tag
  is pending only when the episode has chapters), so the candidate window and run budget go to
  real work.
- **Fixed the tag oversize gate rejecting batches the splitter had built for uncapped routes.**
  The chapter-batch splitter counted routes with no per-minute token cap (OrcaRouter DeepSeek V4
  Flash, 1M context; the Kilo/OpenRouter Nemotron routes) as able to fit, but the later "exceeds
  every tpm-capped route" gate looked only at capped routes, so 212k-715k-token batches were
  deferred as `tag-llm-oversized` forever (353 of them on 2026-11-08). Both now share one
  `_tag_route_fits` predicate (input context, output context, and a whole per-minute cap when the
  route has one).
