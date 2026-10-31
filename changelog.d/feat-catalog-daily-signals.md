- **Review/48 daily run: new-model canaries and Worker availability signals.** The Worker records
  a per-status telemetry cell (`route_not_found`, `route_gone`, `route_forbidden`) for each 404, 410
  and 403 (alongside the classes that still drive behavior) and serves `GET /v2/stats?availability_failures=1` with the day's
  per-status counters. The daily
  incremental run health-checks any route with two 410/403 or three 404 failures today straight away
  (an older Worker without the endpoint just means no signal), and canaries up to six models newly
  listed since the last catalog snapshot, only for providers that already have a snapshot.
