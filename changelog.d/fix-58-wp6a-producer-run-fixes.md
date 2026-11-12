### Fixed
- Chapter-agenda and chapter-locator runs no longer fail when the shared `chapter-record-write`
  lease is held by another live run: they wait up to 10 minutes, then skip the record commit as a
  clean deferral (log line plus `record_write: skipped_lease_busy` on the run event). Lease
  storage or corruption errors still fail the run (review/58 WP6).
- The chapter-locator workflow now uses the same timeouts as chapter-agenda (job 240, step 225
  minutes) so the 204-minute in-process stop budget fires before the step is killed; it was 45 and
  38 minutes.

### Changed
- The LLM topic-tag producer runs every four hours (`19 */4 * * *`) instead of three times a day.
