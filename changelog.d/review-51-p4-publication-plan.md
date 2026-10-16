- **Remedy publication planner, report-only (review/51 P4, first stage).**
  `citypods/remedy_queue.py` plans what the P3 publisher would open, update, reopen or leave
  queued within `config/remedy.yml`'s limits: at most five decisions of one city and feed per
  PR, eight PRs opened or reopened in a rolling week and eight open at once counting every
  marked PR whoever opened it, twelve directional issues, oldest-first round-robin by city,
  stable `<!-- citypods:remedy:v1:<decision_id> -->` markers and
  `fix/remedy-<city>-<family>-<hash>` branches, and no regeneration of a rejected or settled
  decision. The publication report carries it as `publication_plan` (`mode: report_only`);
  nothing is written from it yet and the publisher's behaviour is unchanged.
