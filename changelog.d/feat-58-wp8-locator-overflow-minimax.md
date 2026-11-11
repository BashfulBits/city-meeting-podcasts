- **Review/58 WP8: chapter-locator overflow moves to MiniMax M3.1 Flash.** Locator packets of
  76,001 to 500,000 estimated tokens now route to `minimaxai/minimax-m3.1-flash`
  (`nvidia_minimaxai_minimax_m3_1_flash`) with the unchanged production prompt; the 76,000 split
  and the V4 Flash band are unchanged. The cap is one constant,
  `LOCATOR_OVERFLOW_MAX_INPUT_TOKENS`, set from the WP8a admission probe (admitted up to 941,663
  provider tokens, rejected 709,545 estimated). Larger packets (7 today, GH#2651) are not
  submitted: they are held as deferred, counted as `locator-over-input-cap` in the producer run
  event, and become eligible on their own if the cap is raised. DeepSeek V4.1 Flash left the
  locator lane only (it stays in `judge:adjudicator`). The MiniMax route's `hard_input_ceiling`
  and input context are now 941,663 with a 1.59 input-ratio prior, and the locator's `max_tokens`
  is clamped to the model's largest route output limit (12,800 for MiniMax; V4 Flash keeps
  20,000). Backfill story: no routing-version bump, so V4 Flash recipes and accepted results are
  untouched, and accepted V4.1 results stay reusable. Overflow packets get new recipes because the
  model is in the recipe hash. A still queued V4.1 locator job is cancelled at the Worker and
  re-planned on MiniMax in the same run (within the producer cap); one already in flight finishes
  and is then re-planned; an unsubmitted V4.1 capsule is retired by the deferred sweep. Takes
  effect when the Worker is deployed with the recompiled config.
