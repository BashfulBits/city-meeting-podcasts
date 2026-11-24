### Fixed
- A planned size search now retries its reachability check after spaced waits when the provider
  answers busy (429, 503, timeout), instead of losing the day's session to one overloaded response
  (Gemini `hydrogen` and `flash-latest` were skipped this way).
- A partial input bound left by a search cut short by 429s (`quota_deferred`) is re-measured by the
  daily plan, not treated as a finished input measurement (`flash-lite` kept an ~85k lower bound).
