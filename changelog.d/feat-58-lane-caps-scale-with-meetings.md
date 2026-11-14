- **LLM lane caps scale with meetings per day (review/58 section 12.11).** The seven production
  lanes' `reserved_write_units`, `daily_write_units` and `max_queued_jobs` are now computed by
  `scripts/compile_llm_lanes.py` (and the client's `load_lanes`) from per-lane `sizing` inputs and
  one shared figure in `llm_lane_sizing`: new meetings a day, the measured p90 of 14 scaled by the
  growth of active feeds in `config/feeds` (never below 14). Adding cities raises the caps on the
  next compile without a hand edit. The compiler now fails, instead of clamping, if every ceiling
  at once would exceed the Worker's job or unit guard or the 85,000-row pace. Compiled output for
  today's inputs is unchanged.
