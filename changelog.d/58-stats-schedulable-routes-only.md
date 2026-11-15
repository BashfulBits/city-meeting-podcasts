### Fixed

- **`/v2/stats` no longer reports stood-down, deleted or paused routes as live** (review/58
  §12.14).
  `claim.last_empty_diagnostics` (and `claim.last_diagnostics` under `?detail=1`) keeps only the
  scan cursors and per-route diagnostics of models and routes that can be scheduled; a cursor for
  `typesafe/jev-1.13` from 2026-11-11 was stored and reported on every claim because its only
  route is `rpd: 0` and nothing ever clears it. The claim also drops such a cursor from its own
  state, so it is not persisted again. `model_aliases` omits a stood-down route's own names and
  any declared alias whose model has no route left. `routes.all[].rpd_count` is 0 when the
  ledger's day key is not the route's current day, as the pacing code already treats it.
