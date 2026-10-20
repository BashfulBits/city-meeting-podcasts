### Added

- **Gemma quota diagnostics.** Correlate request timing, input estimates, observed usage,
  full/cached/uncached input, response validity, gateway configuration, and bounded quota details.
  This distinguishes
  per-minute exhaustion from confirmed size refusals without logging prompts or full errors.
  Document the investigation and plan for roughly 95% AI Studio packet coverage with larger
  same-model routes handling size overflow. Packing and retry behavior are unchanged; no stored
  artifacts are invalidated or queued payloads rewritten.
