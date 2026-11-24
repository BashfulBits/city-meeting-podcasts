### Fixed
- The input-limit search now starts below the route's per-minute token budget (`tpm`) and steps
  down on a 429 at that size, instead of probing 1M tokens and giving up. `tpm` only seeds and
  guides the search; it never caps a route that admits larger requests. Gemini free-tier routes
  such as `gemini-flash-lite-latest` and `gemini-4-hydrogen` could never be measured before.
