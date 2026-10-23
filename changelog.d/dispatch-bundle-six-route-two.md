### Changed

- LLM dispatch: `MAX_BUNDLE_JOBS` 5 -> 6 (and `MAX_JOBS_PER_MODEL_CLAIM` 5 -> 6, which must not
  trail it) to use all six outbound connections a Worker invocation allows;
  `MAX_JOBS_PER_ROUTE_PER_BUNDLE` 3 -> 1. The per-route cap already counts each `route_id`
  separately, so AI Studio's primary and secondary routes each get 1 lease per cron. Daily
  `MAX_LEASES_PER_UTC_DAY` (7,000) is unchanged and stays under 1,400 x 6.
- `PURGE_BATCH_LIMIT` 15 -> 14: the larger bundle (12 dispatch B2 calls) plus cleanup's two deletes
  per job must stay within `validateConfig`'s 40-subrequest budget (28 + 12 = 40). Cleanup drains
  2,016 jobs/day instead of 2,160.
