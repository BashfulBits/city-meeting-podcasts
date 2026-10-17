- **A route whose provider keeps saying its pool is full now backs off to one call a day.** After
  an `upstream_capacity` failure the route's cooldown doubles from 60 s up to 24 hours (it was
  15 s up to 5 minutes, about 288 probes a day). On 2026-10-17 the OpenRouter free Gemma routes
  returned 116 such failures and no successes. The first success resets the ladder; gateway limits
  and unusable replies keep the 5 minute ceiling (`UPSTREAM_CAPACITY_SATURATED_*` in
  `config/dispatch_tuning.yml`).
