- **Early slot release in the dispatch Worker (review/58 WP4).** A bundle's settled calls are now
  committed through a partial completion (`completePartial`) while slower siblings still run, so a
  fast call frees its in-flight slot and route lease instead of holding them until the bundle's
  slowest call ends. The DO settles a partial exactly as `completeBatch` would and records the
  bundle's settled jobs, so a retried partial or a repeated job settles once. Defaults: one partial
  per bundle after 60 s, at most 1,000 partial rows a day (prorated), none while the day's rows are
  ahead of an 85,000/day pace; review/58's 20 s / three commits would pass 85,000 rows at 6,000
  results a day (measured: one row per partial, two when a packed telemetry shard is shared).
  `bundle_completed` adds `partial_flushes`, `commits` and per call `released_by`; `hold_ms` ends
  at the releasing commit. Off switch: `EARLY_SLOT_RELEASE_ENABLED: 0` in
  `config/dispatch_tuning.yml`. Additive column `scheduler.early_release_json`.
