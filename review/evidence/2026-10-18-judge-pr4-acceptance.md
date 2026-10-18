# review/53 PR4: acceptance with exceptions (2026-10-18)

The maintainer accepted the PR4 evidence gate of [review/53](../53-judge-stack-tags-and-moments.md)
on 2026-10-18 before the three-day observation completed, to release PR5 and the review/51 P4
stack (#2457 to #2460). First pass: [2026-10-15](2026-10-15-judge-pr4-first-pass.md).

## Evidence (monitoring reports, 2026-10-17 UTC)

| Gate | Observed |
|---|---|
| Queued not rising | 8,009 (08:34) to 7,476 (23:47), down at every report |
| Outstanding sibling packets | 2,823 (07:11) to 2,384 (22:38 run) after Gemma 4 26B left the lane |
| `replanned` near zero | **Not met:** 3,458 (15:55 run) and 3,702 (22:38 run); 5,152 at 12:23 |
| JEV `job_unroutable` / `input_over_route_ceiling` | none in the day's route failures |
| Bundle-slot pressure | `active_bundle_limit` stayed at 51 from the 08:33 deploy of #2475 |
| NVIDIA `upstream_capacity` 429s | none; `server_error` 45 of 201 Gemma calls at the last count |
| Per-task, per-judge counts, display before/after | **Not produced** |
| Durable Object reads | one metered detail call (about 79k rows) at most every four hours |

## Exceptions carried forward

1. Fewer than three observed days. Re-check `replanned` and the queue over the next three days.
2. `replanned` is still large; expected to fall as stranded packets are used up.
3. Display comparison and per-task counts still owed.

Gemma 4 26B (usable replies under 40%, repetition loops; replay in PR #2476) was removed from the
sibling lane during this window, so day-over-day comparisons start from 2026-10-17 12:23 UTC.
