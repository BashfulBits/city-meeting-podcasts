- **Worker-side quota and per-job queue depth throttling.** Replaced client-side
  `max_dispatches_per_run` throttling with Cloudflare Worker coordinator `daily_write_units`
  and `max_queued_jobs` enforcement. Sized lane `max_queued_jobs` to approximately 4x daily
  capacity for an 800 episodes/day throughput baseline. Added cached per-batch queued count checks
  in the coordinator to prevent ingress queue flooding while keeping D1 row reads tiny.
- **Tags stage completion self-healing for historical LLM backfill.**
  Updated `stage_output_pointer` and `stage_is_dirty` to detect episodes with chapters lacking
  `tags_llm_recipe_hash` when tag LLM is enabled, unblocking the rate-limited historical backfill
  of 16,144 chaptered episodes while preserving completion markers for chapterless episodes.
- **Moments lane rollout expansion and source-level filtering.**
  Expanded `rollout_meeting_families` to include Planning and Zoning feeds alongside City Council.
  Pre-filtered non-rollout meeting families at the source level before archive deserialization in
  `SourcePipeline.enrich` and the global queue, eliminating 45 minutes of archive loading overhead
  across 70+ non-participating feeds. Prioritized Council and P&Z feeds first in the moments queue.

