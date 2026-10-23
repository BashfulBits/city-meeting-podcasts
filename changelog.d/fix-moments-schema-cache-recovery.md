- **Recover moments from invalid cached LLM answers.** Preserve response schemas across batch
  quota refusals, restore the known moments contract on legacy handles, and include missing
  schemas in corrective requests. Moments rejects and conditionally evicts invalid completed
  answers after durable failure accounting, using the existing three-failure recipe limit.
  Exhausted recipes do not consume producer slots. Storage interruptions remain recoverable.
  No pipeline-version bump or catalog-wide backfill occurs; affected recipes retry gradually
  within normal producer and provider budgets.
