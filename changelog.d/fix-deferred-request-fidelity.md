- **Preserve LLM request policy through deferred sweeps.** Retain backup models and thresholds,
  route pins, priority, remaining policy flags, explicit timeout, and output mode across durable
  storage and reconstruction. Version incomplete legacy moments capsules as needing producer
  replanning; hold them in the sweep and rebuild matching unsubmitted work from current inputs
  within normal producer caps. Keep submitted jobs, newer records, and valid results intact.
  No pipeline-version bump, quota increase, catalog-wide invalidation, or bulk backfill occurs;
  recovery is gradual and remains subject to existing ingress and provider budgets.
