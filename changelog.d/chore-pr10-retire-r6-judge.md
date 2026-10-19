- **The R6 judge panel is retired (review/53 PR10a).** `MomentJudgeStage`,
  `citypods/moment_judging.py`, the `moment-judge` task and prompt, the `r6-judge` lane (its
  daily write units are released; `ingress_reservations.json` is now 14 lanes), the
  `r6-moment-review` workflow and the `moments.judges` config block are removed. Display is
  unchanged (`scripts/check_retirement_display.py`: 0 differences over 172,526 tag candidates).
  The 5,075 queued R6 judge jobs were cancelled first; their stale deferred handles must be
  archived and deleted, or the next `llm-deferred-sweep` would resubmit them. The
  human-calibrated admission policy path in `citypods/moment_evaluation.py` stays for PR10b.
