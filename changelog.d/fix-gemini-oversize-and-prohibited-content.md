### Fixed

- **Dispatch v2 Gemini oversize 429 and prohibited content 400 routing.** Classified Gemini 429
  responses with `input_token_count` or `inputtokensperminute` whose scaled input estimate meets
  or exceeds the route ceiling or TPM as `route_input_limit` (`gemini-input-quota-oversize`).
  This populates `routes.input_rejected_min` and excludes the route for that job so sibling routes
  serve it rather than repeatedly retrying.
- **Content policy violation handling.** Classified HTTP 400 responses reporting Generative AI
  Prohibited Use policy violations (`PROHIBITED_CONTENT`) as `content_policy_violation`, excluding
  the failing route for that job and requeuing it without modifying route ceilings or standing
  down the route. When all primary routes are excluded, declared backup models are activated.
