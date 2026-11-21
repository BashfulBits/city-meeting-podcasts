- **Review/53 anchor acceptance check.** `python -m citypods.judging.calibrate --evaluate` scores
  any anchor model (a retired one, a Cloud Run endpoint, a new free route) at the thresholds
  already in `judging.tasks.<task>.thresholds`, over that model's own era (`--anchor-model`,
  `--since`, `--until`). It reports agreement, the false-accept upper bound and whether the
  thresholds are accepted for that model, for validate, gate, grade and choose questions, and
  `--record` stores the result under `evals/judge/acceptance/`. Nothing is fitted or written to
  the site config.
