### Fixed

- Google Gemini/Gemma dispatch now honors token-quota retry deadlines even after job retry
  exhaustion, and an in-flight success preserves an active provider cooldown. Explicit oversized
  HTTP 429 requests use sibling-route/repacking recovery instead of repeated quota waits.
- Pending Google input reservations remain charged through their lease and settle to actual input
  at response end before their token window expires. This conservatively reduces throughput on
  slow responses while preventing scheduled-start delays from releasing quota early. No schema
  migration, pipeline-version bump, stored-artifact invalidation, or catalog backfill is required.
