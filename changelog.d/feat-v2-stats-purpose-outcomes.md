`/v2/stats` now always returns the six core production purposes in `ingress.by_purpose`, even when
some have no ledger activity that day. Each row includes `jobs_completed` and `jobs_failed`.
The coordinator aggregates terminal outcomes per purpose in each completion batch and upserts the
current UTC day's small ingress ledger once per purpose. This avoids scanning retained jobs and
avoids a counter write for every completed job. Existing ledger rows gain zero-default counters;
outcome totals begin at deployment without historical backfill.
The fixed overview list is `chapter-agenda`, `chapter-locator`, `topic-tags:tagger`,
`topic-tags:prelabeler`, `r6-moments`, and `r6-judge`; shadow, benchmark, tournament, and review
purposes are excluded.
