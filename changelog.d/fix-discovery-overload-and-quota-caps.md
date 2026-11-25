### Fixed
- A 429 backoff now stops after three waits when the last session's halved retry was refused too
  (`gemini-4-hydrogen` and `hy4-flash-free` repeated seven sessions on a spent quota), instead of
  walking the whole 30 s to 10 min ladder.
- A canary answered with an unmatched 5xx is reported as a provider server error rather than an
  unrecognized response (`gemini-flash-latest` returns 503 for days).
