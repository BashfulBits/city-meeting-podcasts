- **The R6 human-calibration path is retired (review/53 PR10b).** `moment_evaluation.py` (review
  ledger, 30-day warm-up, precision policy, judge observations), `moment_review.py`, the
  `r6-review` command and its issue handler, and the `moments.evaluation` and `moments.mode`
  config are removed. `MomentAdmissionStage` now applies only an explicit `manual_status`; every
  other quote stays a hidden shadow, as before. `quote_safety_gate` and `technical_video_gate`
  are unchanged.
