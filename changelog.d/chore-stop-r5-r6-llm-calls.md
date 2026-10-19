- **The R5 pre-labeler and R6 judge panel are switched off.** `tagging.prelabeler.enabled` and
  `moments.judges.enabled` are `false`, and the weekly R5/R6 review workflows (`llm-tag-review`,
  `llm-tournament`, `tournament-tag-backfill`, `r6-moment-review`) no longer run on a schedule
  (manual dispatch remains until review/53 PR10 and PR11 delete them). Display is unchanged:
  none of the 115 R5 calibration rows has ever qualified and the R6 policy has never admitted a
  moment, checked by `scripts/check_retirement_display.py` over all 172,526 stored tag candidates.
  Queued R6 jobs (4,835) are cancelled with PR10.
