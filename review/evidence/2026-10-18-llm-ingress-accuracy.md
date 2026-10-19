# LLM ingress-stat accuracy — 2026-10-18

Supporting evidence for [review/44](../44-bounded-bundled-llm-dispatch.md) and the current
[review/11 follow-up](../11-technical-design-roadmap.md), following merged PR #2487.

## Fresh ingress counts match the observed submissions

The retained Actions JSONL snapshots use the existing lightweight `/v2/stats` endpoint.
They show 182 fresh rows at October 18 19:31 UTC, rising to 617 at 23:21 UTC: 435 additional
rows in about four hours. The improvement is real and appears in the counter.

| Run | Client fresh admissions | Client replays | Stats comparison |
| --- | ---: | ---: | --- |
| [Locator 39173503069](https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/39173503069) | 112 | 22 | October 18: 500 → 612; locator purpose 148 → 260 |
| [Locator 39182256025](https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/39182256025) | 39 | 22 | October 19: 0 → 39; locator purpose 0 → 39 |
| [Agenda 39171421426](https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/39171421426) | 21 | 46 | October 18: 479 → 500; agenda purpose 26 → 47 |

All 22 downloaded start/end snapshots from eleven post-fix runs had exact agreement between
`scheduler.jobs_ingested_today` and the sum of per-purpose `jobs_ingested` values. Concurrent
workflows mean a global run delta need not equal that run's own admissions; the locator and
agenda comparisons above also agree at purpose level.

The 19:31 → 23:21 increase breaks down as agenda +47, locator +112, adjudicator +5, moment
extraction +8, moment judge +8, production prelabeler +2, shadow prelabeler +251, and tagger +2.
Sources include [sweep 39159096610](https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/39159096610)
and [judge 39172888846](https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/39172888846).

The sweep also reported 277 schema-correction resubmissions during this interval. These are
recovery traffic: a new correction row counts as fresh ingress, while a provider retry on the
same row does not. Increased ingress alone does not establish successful or healthy model output.

## Result and recovery reporting

The deployed Worker incremented `retried_today` for newly inserted schema-correction clones,
which mixed new-job admission with same-job rescheduling. The correction removes that increment:

- `jobs_ingested_today`: newly admitted DO rows, including correction clones. Replaying an
  existing admission does not increment it; rescheduling the same job creates no ingress.
- `completed_today`: provider-success settlements. Client schema, grounding, or content checks
  may still reject their results, so this is not a count of accepted content artifacts.
- `failed_today`: settlements that leave the job terminally failed, rather than requeued.
- `retried_today`: completion-time rescheduling of an existing job, including `deferred_late`.
  A new correction clone no longer increments it. Idempotent clone replays change neither count.

These definitions distinguish admissions from retries. Pre-deploy daily retry totals may still
include legacy correction admissions until the next UTC rollover. No counts are reset, and no
historical split is guessed from incomplete client telemetry. Admission policy and DO row-write
cost are unchanged: the correction removes an increment from an existing scheduler UPDATE.

Actions snapshots now carry all three existing result counters, labeling retries as Worker
requeues rather than presenting correction admissions as rescheduling of the same job.

The raw completion outcomes are `success`, `deferred_late`, `retryable_error`, and
`terminal_error`; retry policy determines whether an error becomes queued or failed. The client
poll report separately counts completed, pending, missing, failed, and unknown observations.
These layers are not interchangeable: the same content item can have multiple job attempts.

Schema-correction submission observations get a separate per-run line in Actions summaries,
derived from existing JSONL events without new requests or DO writes. Repeated/idempotent calls
can repeat observations, so this line is not advertised as an exact UTC-day correction count.

## Why the current total is small

The counter resets at midnight UTC: 7 p.m. America/Chicago on October 18. At October 19 00:09
UTC it was zero; at 01:17 UTC it was 39, all from the locator run above. These are recorded
observations, not a claim about all subsequent traffic or a rolling 24-hour count.

The later [sweep 39185424706](https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/39185424706)
recorded 39 at 01:28 UTC and 179 at 02:00 UTC. Its artifact records 140 schema-retry operations,
matching the 140-row increase: shadow prelabeler +131, moment extraction +7, and agenda +2.
The run failed after a B2 `StorageReadUnavailable` while re-reading a canonical record inside
`discard_terminal_failure`'s mutation guard. Reconciliation stopped before its final summary;
the successfully admitted rows remain counted. Follow-up: isolate transient guarded-cleanup
read failures so one unavailable record cannot abort the rest of the bounded sweep. This is
separate from the stats quota correction and does not justify treating reads as missing jobs.

An accepted idempotent replay refers to an existing job and does not create another ledger row.
Superseding an existing row, retrying a provider attempt, and collecting a completion also do not
increment fresh ingress. Queued stock may shrink while cumulative ingress rises, and clearing
completed rows never decrements the ingress counter. A healthy accepted call is not necessarily
new ingress. For example, the agenda run returned 67 accepted jobs but only 21 were fresh.

## Confirmed display bug and correction

`_ingressPurposeReservations()` already returns the purpose-keyed map for both the compiled
configuration and operator overrides. `stats()` looked for another `purposes` or `reservations`
wrapper, then substituted `{}`. Every observed quota and utilization was consequently zero.
The actual admission paths use the flat map correctly; their limits were not zero.

The correction reads the map directly and uses the same four-write-unit minimum as ingress
preflight for legacy overrides lacking `write_units_per_job`. `daily_quota` remains a configured
job-equivalent estimate, not guaranteed capacity: actual model-index write units, other reserved
budgets and account limits still control admission. Admission policy and fresh-ingress counts
are unchanged by the quota-display fix.

Regression tests cover compiled, object, JSON, and legacy override maps; fresh and replayed
batches; per-purpose/global count agreement; cancellation; and midnight rollover. They assert
that `total_changes()` stays unchanged across stats reads. No new DO reads, rows, indexes,
tracking counters, telemetry writes, or data migration are introduced.
