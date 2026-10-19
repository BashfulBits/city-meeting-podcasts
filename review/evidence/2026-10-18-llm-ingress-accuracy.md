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

All 20 downloaded start/end snapshots from ten post-fix runs had exact agreement between
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

## Why the current total is small

The counter resets at midnight UTC: 7 p.m. America/Chicago on October 18. At October 19 00:09
UTC it was zero; at 01:17 UTC it was 39, all from the locator run above. These are recorded
observations, not a claim about all subsequent traffic or a rolling 24-hour count.

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
budgets, and account limits still control admission. No admission policy or counters change.

Regression tests cover compiled, object, JSON, and legacy override maps; fresh and replayed
batches; per-purpose/global count agreement; cancellation; and midnight rollover. They assert
that `total_changes()` stays unchanged across stats reads. No new DO reads, rows, indexes,
tracking counters, telemetry writes, or data migration are introduced.
