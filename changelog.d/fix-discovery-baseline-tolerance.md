### Fixed
- Discovery confirms a carried input bound when the provider's count of the re-tested prompt falls
  within the bound's width, not only at or above it. The provider counts an identical prompt a few
  tokens differently per call, so `gemini-flash-lite-latest` spent a whole six-probe session
  re-testing 84,950 and never climbed. A bound is never restored above what a fresh probe admitted.
- A canary that stays inconclusive records its report-safe reason and HTTP status in canary memory,
  so a route such as `gemini-flash-latest` can be diagnosed.
