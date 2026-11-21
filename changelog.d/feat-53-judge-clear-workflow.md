- **Review/53 judge clear workflow.** `judge-clear.yml` runs `citypods.judging.clear` from GitHub
  Actions (dry run unless `apply`), in judge.yml's `llm-judge` concurrency group so it never runs
  beside a judge pass. Inputs are the task, the scope (`stale-evidence` or `before` a date), an
  archive name and `apply`.
