### Changed

- The approved varied P1 comparison now includes six models (678 questions). Gemini 3.7/3.8
  calls are discontinued because of low daily quotas; their observations and Astra history
  remain immutable. The controller and scorer exclude these models. Existing daily attempt
  caps, production headroom, timeouts and production route settings remain unchanged.
- Evaluation scoring retains hashed provenance for results outside the executing checkout.
