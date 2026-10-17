`/v2/stats` exposes purpose-level ingress usage and the latest 24 hourly queue/throughput
snapshots. Purpose usage reads the small `(utc_day, purpose)` primary-key ledger; hourly points
live in the scheduler singleton and are written only by hourly maintenance. Completion, failure,
and retry totals are maintained in existing scheduler writes, without scanning job history.

Consumer contract: burndown starts empty and fills at hourly maintenance. Timestamps are UTC epoch
milliseconds. Queue depth is point-in-time; ingested, completed, failed, and retried are cumulative
for the current UTC day. Outcome counters start at deployment, with no historical backfill.
