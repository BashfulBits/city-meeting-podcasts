- **Tag batches are now split to the primary route's tested capacity, and the oversize gate uses
  tested ceilings.** The chapter-batch splitter grew batches until they stopped fitting any route,
  so routes advertising 1M context produced 212k-715k-token batches that no measured route can
  take. It now cuts at 93% of the smallest measured capacity among the primary model's routes
  (about 92k units for Gemini 3.5 Flash Lite: its 218,875-token ceiling and 225k/minute cap, scaled
  by the measured 2.15 token ratio), so every batch fits the lane's best-quota route. A single
  chapter larger than that may still go to a larger route that carries a `hard_input_ceiling`
  (Nemotron 3.5 Super VL, ~249k). Routes with no measured ceiling (OrcaRouter DeepSeek) no longer
  widen what may be sent; if no candidate has a measured ceiling the advertised limits apply.
