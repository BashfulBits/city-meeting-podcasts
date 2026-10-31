- **Review/48 daily run: new-model canaries and Worker availability signals.** The Worker records
  a `route_not_found` telemetry cell for each 404 (alongside `upstream_capacity`, which still
  drives behavior) and serves `GET /v2/stats?availability_failures=1` with the day's
  `route_unavailable` (410/403/401) and `route_not_found` counters and their last status. The daily
  incremental run health-checks any route with two 410/403 or three 404 failures today straight away
  (an older Worker without the endpoint just means no signal), and canaries up to six models newly
  listed since the last catalog snapshot, only for providers that already have a snapshot.
