- **Review/53 PR7 judge calibration report.** `python -m citypods.judging.calibrate --task <task>`
  fits each judged question from the adjudicated set (contested items plus the 3% calibration
  sample, weighted to the population) on Gemini Decision 1 anchor judgments since 2026-11-12. For
  `validate`, it picks the admit/reject pair with the highest agreement whose upper 95% bound on
  the weighted false-accept rate is at most 5%. For `gate`, it picks the anchor flag level that
  misses no adjudicator flag. `grade` and `choose` agreement are reported. Items whose deciding
  sibling quote lies outside the adjudicator's evidence window are excluded and counted.
  `--write` stores the proposal under `judging.tasks.<task>.thresholds`; tasks stay in shadow mode.
