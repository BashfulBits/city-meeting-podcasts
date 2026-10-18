- **The claim's route-eligibility filter now applies the same learned size gate as pacing.** Both
  call one function (`inputSizeRefusal`): the admission ratio against the larger of the configured
  ceiling and the largest size the route has accepted. The filter had kept the catalog's 1.4 ratio,
  so packets over about 10,285 estimated tokens never reached the AI Studio Gemma routes, whose
  calibrated limit was 13,605 (review/55). Enqueue-time indexing and the structural checks still
  use catalog values.
