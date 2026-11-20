- **NVIDIA MiniMax ceiling, retired DeepSeek V4.1 route, no NVIDIA TPM.** A 2026-11-19 direct
  probe admitted 1,034,675 prompt tokens on MiniMax M3.1 Flash (HTTP 400 `context_length` above
  1,048,576), so the route's `hard_input_ceiling` is 1,034,675 in a 1,048,576 window. The
  chapter locator's MiniMax overflow cap is now derived from that route (ceiling / input-token
  ratio = 650,738 estimated tokens) instead of the stale 500,000 constant. NVIDIA's
  `deepseek-ai/deepseek-v4.1-flash` returns HTTP 410 (end of life 2026-11-16), so its route is
  removed; V4.1 stays on the two BeatAPI routes. NVIDIA has no token-rate limit, so the provider
  and its routes drop `tpm` and pace on RPM alone; the old 100,000 x 5 reservation gate was
  refusing every large MiniMax job.
- **Upstream capacity refusals retry without limit.** The Worker classifies HTTP 529 "overloaded"
  as `upstream_capacity`, and an `upstream_capacity` failure now requeues without spending the
  job's retry budget (the route cooldown and health ladder pace it). MiniMax reads a bare 429 as
  upstream capacity.
