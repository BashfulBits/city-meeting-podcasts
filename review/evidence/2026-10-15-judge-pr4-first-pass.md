# review/53 PR4: first acceptance pass (2026-10-15)

Supporting evidence for the PR4 acceptance gate in [review/53](../53-judge-stack-tags-and-moments.md).
Sources: the `judge.yml` run logs, the dispatch Worker's read-only endpoints (`/v2/stats`,
`/v2/calibration`), the rolling budget-monitor issue (#1870) and Cloudflare's Durable Object
analytics and Workers Logs. **PR4 is not accepted by this pass.**

Evidence to 2026-10-15 18:25 UTC for the criteria below, later figures where stated:

| Criterion | Result |
|---|---|
| Dry run within every ceiling | **Partly.** Run 38030604078 passed what it measured: 116 anchor and 166 sibling packets within the run caps, `payload_too_large` 0, judge step 37 of 76.5 minutes. It sized packets by item text, so it did not test request-size admission at the Worker, which scheduled runs then failed (finding 4). Open until a run with the new sizing passes |
| Runs inside the window | Met: 18 of 19 scheduled runs since 2026-10-11 succeeded; the cron slips by up to about 5 hours |
| Judgments appended daily, both tasks and both judges | Appended every run (latest 3,527); per-task and per-judge split is not in the run log: **open**, read from the report in the next pass |
| No `display`/`admission`/publication change | **Open**: before/after sampling not yet done |
| No JEV `request_defect` except genuine oversize | **Not met**: about 45 anchor packets a run were failed by the Worker (`job_unroutable` 37, `input_over_route_ceiling` 15 in one monitor window) because packets were sized by item text, not by the request. Fixed by sizing from the serialized request and the learned ratio |
| BeatAPI chat routes admitted only while no JEV job was queued | **Open**: no check recorded |
| DO rows under the enqueue stop each day | **Open**: reads are not metered in the run logs; see the 2026-10-13 failure below |

Findings, with the change that answers each:

1. **2026-10-13 21:32 UTC run failed: the Durable Object's daily read allowance was spent, by an
   evaluation, not by the judge.** Cloudflare's Durable Object analytics show 5,429,389 rows read that
   UTC day against a 5,000,000 free-tier allowance (1.0 to 1.9 million every other day), all of it
   from 17:00 to 19:59 UTC (2.04, 1.58 and 1.03 million an hour against a normal 25,000), then zero
   until the reset. The Workers Logs for that window hold 59 calls to `GET /v2/stats?detail=1&limit=100`
   from `python-requests`, none in the hour before. That snapshot scans every retained and queued job
   (about 79,000 rows a call: 59 calls account for the 4.65 million), and the only caller is
   `token_headroom` in `evals/remedy/results/body-selection-2026-10-13/groq_routes.py`, which polls it
   before each Groq request (review/51 body-selection evaluation, committed 2026-10-13). The judge's
   own 21:32 run then met a Worker that answered HTTP 500 to every call. The judge contributed only
   through queue depth (the snapshot's cost grows with queued jobs). Changes: the snapshot is metered
   and a cheap route view replaces it for the evaluation (the PR beneath this one, `fix/dispatch-detailed-stats-budget`);
   the judge lanes join the run-level ingress preflight, so a Worker that is down or refusing ingress
   starts the pass with no packets (a deferral, not a red run after 50 minutes of planning).
2. **Replans (1,498 items in the 2026-10-15 18:14 run) and `errored:LLMBackendError` (43 to 52 a
   run)** are the failed anchor packets of finding 4 coming back to the planner, which re-built them at
   the same size. Inferred from the monitor counts; the post-fix runs must show both near zero.
3. **A standing Worker queue.** 8,485 jobs queued on 2026-10-15 (all lanes), with 1,346 jobs ingested
   and 1,120 leases so far that UTC day: roughly 230 a day of growth, and about eight days of claims
   ahead of any new job. `judging.max_outstanding_packets` (anchor 240, sibling 480) limits the judge's
   packets waiting at the Worker, so its share cannot grow past about 720 jobs; a pass submits only into
   the room left.
4. **Packets were sized by item text.** The producer's estimate is chars/4 of the serialized
   request, which JSON escaping and per-question structure enlarge. JEV's learned input ratio is 0.92
   (the Worker applies 1.0), so the failures were the producer's own estimate exceeding 58,000, not
   the tokenizer. `JevBackend` now sizes each packet from the serialized questions, scaled by the
   Worker's ratio with a 5% margin (1.25 when the ratio cannot be read).


Remaining to accept PR4 (evidence gate, after this change is deployed): three consecutive days with
the monitor reporting zero `job_unroutable` and `input_over_route_ceiling` for `judge:anchor`, the
judge's `errored` and `replanned` near zero outside genuine cancellations, the Worker's queued count
not rising day over day, a per-task, per-judge judgment count each day, the before/after display
comparison, and the DO rows-read graph under 50% of the daily limit.
