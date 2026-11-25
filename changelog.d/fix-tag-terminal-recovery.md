- **Recover exhausted topic tags with actionable failure evidence.** Allow up to 200 suggestions per
  multi-chapter response without changing the tag recipe or invalidating existing results. Terminal
  dispatch markers retain the safe route, model, HTTP status, outcome, class, and attempt count.
  An audited, generation-fenced reset requeues exhausted tag recipes without replaying old failed
  Worker jobs; deploy the producer and Worker before running the reset. Existing stored tag
  artifacts remain valid and are not automatically backfilled.
