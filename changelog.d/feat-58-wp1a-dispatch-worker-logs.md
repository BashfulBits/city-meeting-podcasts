- **Dispatch Worker slot-use logs (review/58 WP1a).** The Worker logs `scheduled_claim` (jobs,
  routes, in-flight calls and active bundles before the claim) on every claimed tick and
  `bundle_completed` (bundle duration; per call route, purpose, outcome, failure class, duration
  and hold) once per bundle, for every route including successful non-Gemma calls. No Durable
  Object reads, rows or subrequests are added. Deploy the Worker to start emitting them.
