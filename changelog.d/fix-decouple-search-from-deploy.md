- **Static search no longer gates or duplicates the Pages deploy.** `deploy.yml` publishes the
  render directly on every `main` push, and a new `search.yml` owns the bounded, resumable index
  build on its own 4-hour cron and `pages-search` concurrency group. A run that spent its whole
  budget indexing used to hold the `pages` slot — and was torn down at its edge — so merges queued
  behind it and the site stopped publishing. Search now stages only its index and asks `deploy.yml`
  to publish; that run overlays the index before rendering, so the publisher always composes the
  newest content with the newest completed index and a search run can never republish a two-hour-old
  site. Every publish resolves the index from the newest successful search run instead of the
  evictable `build-state-` cache, so a cache eviction can't publish a site without search, and the
  search run saves its resumable checkpoint explicitly so a late upload or dispatch failure keeps
  the indexing progress. The search job's ceiling moves from 150 to 180 minutes over the unchanged
  120-minute indexing budget.
