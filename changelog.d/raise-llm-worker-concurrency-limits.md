- **LLM worker dispatch concurrency tuning.** Raised `MAX_ACTIVE_BUNDLES` from 3 to 5 and
  `MAX_IN_FLIGHT_LLM_CALLS` from 12 to 20 in `config/dispatch_tuning.yml` to reduce empty claim
  ticks and relieve in-flight concurrency bottlenecks when slow provider calls overlap.
