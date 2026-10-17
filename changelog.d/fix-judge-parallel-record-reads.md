- **Judge reads pending-packet records in parallel, with a bounded wait.** Collecting about 2,800
  pending records one at a time took 14 minutes on a healthy store (0.3 s each) and stalled for
  hours when runner reads to storage hung (2026-10-17). Records are now fetched by 8 workers within
  a 10-minute budget (unread ones stay pending for a later pass), each read gets a 30 s deadline so
  a stalled connection fails fast instead of waiting out 60 s socket timeouts and retries, and the
  pass logs progress and failed or timed-out reads.
