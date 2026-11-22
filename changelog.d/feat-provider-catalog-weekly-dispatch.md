- **Manual weekly-full provider catalog run.** The reconciliation workflow takes a `weekly_full`
  dispatch input that runs the weekly path by hand from main: new-route discovery and the
  configured-route cap re-measure (review/54 §8.14), with the weekly 360-minute budget and
  discovery evidence retained. It refuses to combine with the due-only, incremental, discovery or
  context-canary modes, and does not take the scheduled rate sample. It spends real provider quota
  and updates the same rolling issue as the scheduled run.
