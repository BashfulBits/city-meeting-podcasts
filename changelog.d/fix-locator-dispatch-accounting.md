Fix chapter-locator producer accounting: check the current recipe's deferred record before
reserving a new-work slot, so cached completions and already-submitted handles do not exhaust
the per-run allowance or get blocked behind it when episode metadata is stale. Client-only
requests that have not reached the Worker still require a slot. Worker-side admission limits
remain authoritative; Worker-only replays without a client record still count conservatively.
No pipeline-version bump, recipe change, automatic invalidation, or backfill is introduced.
