### Changed

- The approved varied P1 comparison now includes six models (678 questions). Gemini 3.7/3.8
  calls are discontinued because of low daily quotas; their observations and Astra history
  remain immutable. The controller and scorer exclude these models. Existing daily attempt
  caps, production headroom, timeouts and production route settings remain unchanged.
- Evaluation scoring retains hashed provenance for results outside the executing checkout.
- A separately approved one-off allowance permits five immediate additional attempts on
  exactly two full GLM and three Sol questions, capped at 25 total. Immutable manifest/pair
  binding and restart hash checks prevent broader retry expansion or refill after reset.
