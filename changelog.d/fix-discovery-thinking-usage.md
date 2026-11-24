### Fixed
- Input-size probes accept a usage block whose total includes reasoning tokens that
  `completion_tokens` leaves out (Gemini thinking models such as `hydrogen`), which were rejected
  as `total_invalid` so no size was ever recorded.
- The daily plan re-measures an input axis that stalled (every probe failed verification) instead
  of treating its kept lower bound as a finished measurement (`flash-lite`, ~85k).
