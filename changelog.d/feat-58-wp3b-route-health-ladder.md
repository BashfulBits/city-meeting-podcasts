- **Route health ladder in the dispatch coordinator (review/58 WP3b).** A route that fails the same
  way repeatedly, with no success between, is stood down for longer each time instead of being
  re-picked every tick: one response timeout, two upstream-capacity failures or three request
  defects in a row put it on a 60 s / 300 s / 900 s / 3600 s cooldown ladder (its existing
  `blocked_until`), followed by a single half-open probe; the first success resets it. Jobs move
  to sibling routes or stay queued and never fail because of the ladder; a single request defect
  still fails only its own job. State is one sparse map on the scheduler row, written by the
  existing accounting update and read by the existing authority read: no extra rows for healthy
  routes, no new reads, subrequests or alarms. `route_health` log events on every rung change and
  reset, `admissible_calls` in the claim diagnostics and the `scheduled_claim` log, and
  `health_rung` in `/v2/stats?routes=1`. Tunable in `config/dispatch_tuning.yml`;
  `ROUTE_HEALTH_LADDER_ENABLED: 0` switches it off.
