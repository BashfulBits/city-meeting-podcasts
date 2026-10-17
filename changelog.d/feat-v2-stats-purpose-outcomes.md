`/v2/stats` now includes `jobs_completed` and `jobs_failed` in each `ingress.by_purpose` row.
The coordinator aggregates terminal outcomes per purpose in each completion batch and upserts the
current UTC day's small ingress ledger once per purpose. This avoids scanning retained jobs and
avoids a counter write for every completed job. Existing ledger rows gain zero-default counters;
outcome totals begin at deployment without historical backfill.
