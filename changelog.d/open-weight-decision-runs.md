### Added

- Add the review/60 Phase 3 evaluation harness and its first results. Request builders read only
  the frozen `manifest.json`; scoring gives accuracy against gold (tag strata re-weighted to the
  production mix), ECE, Brier, agreement with Perplexity, invalid rate and paired cluster-
  bootstrap intervals. Includes an OpenRouter and Gemini decision runner, a cloud worker for Karpo
  (9B, 12B, 31B; FP8 and BF16, chapters grouped and one question per request) and GLiDE 9B,
  elephant-1 (through the Phase 1 adapters), a list-rate cost script, and dated
  results for dev and holdout under `evals/open-weight-decision/results/`.
