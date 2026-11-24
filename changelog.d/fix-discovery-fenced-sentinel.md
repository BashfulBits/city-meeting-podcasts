### Fixed
- The input-size search accepts a model answer wrapped in one markdown code fence when grading
  the sentinels. Gemini `flash-lite` fenced its JSON, so every probe failed verification and the
  search stalled at a stale ~85k lower bound.
