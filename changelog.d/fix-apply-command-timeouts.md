- **`/apply` can no longer hang silently until the job timeout.** A run on 2026-11-23 sat in the
  apply step for the whole 120 minutes with nothing logged, because every command ran with its
  output captured and no time limit. Each command now has one (5 minutes, 20 for tests), each
  `gh api` download has 3 minutes with its retries, and the log names each stage. A stalled
  command fails the `/apply` and says so on the issue instead.
