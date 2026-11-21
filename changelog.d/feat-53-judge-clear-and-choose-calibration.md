- **Review/53 judge recovery tool and `best` calibration.** `python -m citypods.judging.clear`
  archives and removes stored judgments so the planner asks for them again: by default only rows
  asked of evidence that differs from what the current builder produces (the truncated moment
  window fixed on 2026-11-18), or everything before a date. It is a dry run unless `--apply`,
  refuses to run during a judge workflow run, archives to `state/archive/<name>/`, and restores
  with `--restore`. `choose_due` now treats an anchor winner that changes with the option order
  as contested, and samples the calibration share, even when no sibling answered, so
  `moment:best` can collect adjudicator data.
