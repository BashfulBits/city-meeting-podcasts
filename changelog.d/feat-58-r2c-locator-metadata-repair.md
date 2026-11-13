- **Review/58 §12.3 locator labels: metadata, not work.** The chapter-locator lane now owns every
  locator key in the shared agenda record (result labels and retry bookkeeping were missing), so
  the chapter-agenda lane no longer writes stale labels over current results. About 1,800
  completed results labelled with an old model and prompt version are restamped with no LLM call,
  only when the stored boundary file, chapters and policy fingerprint prove them current; anything
  unproven is planned as before. Switch: `CITYPODS_LOCATOR_METADATA_REPAIR` (default on). The lane
  prefilter, census and completion marker now agree on "ready"; agendas with zero accepted items
  are no longer counted as ready. Run events and summaries carry repaired, not-provable and
  zero-agenda counts. Replaces the planned capped stale-recipe backfill (WP6).
