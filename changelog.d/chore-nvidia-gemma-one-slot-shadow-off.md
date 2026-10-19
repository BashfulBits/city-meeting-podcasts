- **NVIDIA Gemma 4 31B goes back to one concurrency slot, and the prelabeler shadow evaluator is
  off.** With two slots the share of the route's calls that hit the 720 s limit rose from 3%
  (2026-10-17 morning) to 28% by the end of 2026-10-18, each hung call holding half the route for
  12 minutes while normal calls run under a minute. `tagging.prelabeler.shadow_enabled` is now
  `false`: the `topic-tags:prelabeler-shadow` lane (Gemma 4 26B, retired by review/53 PR11)
  produced about 160 `output_budget_exhausted` failures and empty replies on 2026-10-18.
