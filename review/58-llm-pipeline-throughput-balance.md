# 58 — LLM pipeline throughput balance

Status: **L2 — measured, design proposed, awaiting maintainer go-ahead** (2026-11-11).
Evidence: [`evidence/2026-11-11-llm-pipeline-throughput.md`](evidence/2026-11-11-llm-pipeline-throughput.md)
(queries, sources, data limits, pause log). Window: 2026-11-04 03:31Z to 2026-11-11 03:31Z unless
stated. Production was read only; no pause or resume was issued.

## 1. Summary

The dispatcher is the binding stage, and it is bound by how it holds slots, not by any quota.

Of every in-flight slot-second the Worker held in the 7-day window (7.06M; mean 11.7 of 20 slots):

| Use of a held slot | Slot-seconds | Share |
|---|---:|---:|
| A call that returned an accepted 200 | 925,761 | 13% |
| A call that failed | 2,064,859 | 29% |
| … of which ran to the 720 s response timeout | 1,527,844 | 22% |
| Nothing: the call had ended, the slot waited for the bundle's slowest call | 4,066,819 | 58% |

Measured against the configured 20 slots, productive use is 7.7%. The queue never ran empty (the
smallest depth logged on an empty tick is 554), so this is unsaturated capacity with eligible work
waiting, which is the starvation the owner does not accept.

The five largest bottlenecks, in order of lost throughput:

1. **Slots are released per bundle, not per call** (58% of held slot-time is idle hold).
2. **Doomed calls run to 720 s** (22% of held slot-time; 2,122 timeouts, 1,556 of them on Google AI
   Studio Gemma, whose successes finish in under 120 s).
3. **Broken or saturated routes are re-picked every tick** (JEV 400s, BeatAPI 524s, Gemma 503s, a
   100%-timeout NVIDIA DeepSeek route).
4. **Results are collected about three times a day** (the deferred sweep is the only collector; it
   gathers about 1,600 results a day against about 2,100 to 3,300 settled).
5. **GitHub runs about a third of the scheduled producer crons**, and two lanes hold eligible work
   behind producer gates (locator stale-recipe backfill, tagger daily budget).

Expected effect of the fixes: slot-seconds per accepted result fall from 473 to about 130, which
moves the ceiling from about 2,100 accepted results a day to the 6,000 jobs/day ingress cap and the
90,000-row DO budget. Those two, plus the Gemma provider pool for `judge:sibling`, are the named
limits that remain.

## 2. Flow as measured

```
upstream (starvation here is acceptable)
  audio.yml      21 of 42 scheduled runs/week      1,161 episodes without audio
  asr.yml        20 of ~35                         12,641 with audio, no transcript; 0-4 transcribed/run
  agenda text    --                                6,374 non-provider episodes without agenda text
        |
producers (GitHub Actions; enqueue and exit, never poll)
  chapter-agenda   12/day scheduled -> 3.9/day run   p50 14 min   enqueue p50 1, max 493
  chapter-locator  12/day -> 3.9/day                 p50 15 min   enqueue p50 10.5, max 425
  judge            12/day -> 3.9/day                 p50 43 min   enqueue p50 129, max 1,237
  moments           9/day -> 8.9/day                 p50 21 min   enqueue p50 0, max 110
  tag               3/day -> 2/day                   p50 126 min  enqueue p50 529, max 4,958
        |  POST /v2/jobs:enqueue-batch (129 calls/week)   lane write-unit budgets, 6,000 jobs/day
        v
DO queue: 554 to 3,215 queued all week, never empty
        |  one claim per 60 s tick; <= 6 routes; 1 job per route; in-flight cap 20
        v
bundle (mean 3.3 jobs; 77% of ticks claim)
  calls run concurrently, one lane per route
  ONE completeBatch when the slowest call ends: p50 108 s, p90 723 s; 24% of bundles run >= 700 s
        |  results to B2; job row completed/failed
        v
return path: llm-deferred-sweep only (4/day scheduled -> 3/day run, median start delay 201 min)
  per run p50: 402 completed + 123 failed collected, 200 to 1,450 handles left pending
        |  B2 completed record
        v
producer's next run finalizes the artifact (median gap between runs about 6 h)
```

Lane dependencies: agenda text → `chapter-agenda` → (+ transcript) → `chapter-locator`; provider or
generated chapters → `topic-tags:tagger`; transcript → `r6-moments`; tag and moment candidates →
`judge:anchor` / `judge:sibling` → `judge:adjudicator`. `remedy:p3` makes no LLM calls today.

## 3. Measured capacity by stage

| Stage | Sustainable rate (measured) | Demand placed on it | Binding? |
|---|---|---|---|
| Producers, enqueue | Up to 4,958 jobs in one run; 3,494 ingested on 11-10 | Eligible backlog below | Not the rate; cadence and gates are (B5) |
| Ingress caps | 6,000 jobs/day; lane units below | 3,494 peak day | Tagger lane only, on 11-06 and 11-07 |
| DO queue | 20,000 jobs | 554 to 3,215 | No |
| Dispatcher, claims | 3,682 claimed jobs/day (25,774/week) | Queue never empty | **Yes** |
| Dispatcher, accepted results | 2,133/day (14,934 gateway 200s/week) | Queue never empty | **Yes** |
| DO rows | 90,000/day safe stop | 39k to 60k/day; 10.7 rows per claimed job | No |
| Cron invocation | 900 s wall; all 10,161 ended `ok`; max 750 s | — | No |
| Return path | about 1,600 results/day (525 per sweep × 3 runs) | 2,100 to 3,300 settled/day | **Yes** |

Little's law per route family, successes only (AI Gateway, 7 days). "Slot cost" is what the
dispatcher actually pays per success once failures on that route are added (Worker logs).

| Route family | Successes | p50 / p90 / p99 / max (s) | Failures that held slots |
|---|---:|---|---|
| Gemini 3.1 Flash Lite (AI Studio) | 3,635 | 3 / 21 / 131 / 553 | negligible |
| Gemini 3.5 Flash Lite (AI Studio) | 2,453 | 3 / 12 / 74 / 198 | negligible |
| Nemotron 3.5 Super (NVIDIA, Kilo, OpenRouter) | 3,759 | 54 / 150 / 290 / 603 | few |
| Gemma 4 31B (AI Studio, two accounts) | 2,011 | 63 / 89 / 201 / 683 | 6,934 × 503 (26 s mean), 1,556 × 720 s |
| DeepSeek V4 Flash (OrcaRouter) | 1,323 | 68 / 153 / 241 / 311 | few |
| Gemma 4 31B (NVIDIA) | 809 | 122 / 418 / 664 / 713 | 299 × 720 s |
| GLM 5.3 Flash (OrcaRouter) | 535 | 280 / 527 / 702 / 712 | 59 × 720 s |
| Nemotron 3 Ultra (NVIDIA, Kilo, OpenRouter) | 224 | 203-310 / 496-538 / 668 / 673 | 102 × 720 s |
| DeepSeek V4.1 Flash (NVIDIA) | 6 | 242 / 435 / 608 / 608 | 103 × 720 s |
| GLM 5.3 Flash (BeatAPI, both accounts) | 0 logged | — | 721 × 524, median 391 s held |
| JEV 1.13 (BeatAPI) | 26 | 1 / 2 / 2 / 2 | 717 × 400 |

Which ceiling binds: `in_flight_call_limit` (1,100 empty ticks) and `mixed_admission_limits` (923),
then `active_bundle_limit` (243, all before the 11-05 change to 12). `MAX_ACTIVE_BUNDLES` 12 is
unreachable: 20 in-flight is reached at about four bundles (peak active bundles 9, mean 3.3 to 3.7).
`route_lane_limit`, `route_bundle_limit` and `dispatch_window_capacity` rejected nothing. The row
budget, the bundle and job day caps, CPU (p99 65 ms, max 182 ms, no `exceededCpu`) and subrequests
did not bind.

Tick utilization: cron wall time p50 68 s, p90 722 s, max 750 s; 19% of invocations run past 600 s;
3.2 invocations overlap on average (max 9). Claim RPC p50 265 ms (max 14.6 s); first call starts
p50 1.5 s, p90 15.6 s, max 39 s after invocation start.

## 4. Eligibility funnel per lane (as of 2026-11-11 03:00 to 03:30Z)

Upstream, retained catalog of 26,788 episodes: 25,627 have audio; 12,986 have a transcript (12,641
wait on ASR, 43 of them published in the last 30 days); of 13,994 non-provider-chapter episodes
7,620 have agenda text and 7,434 have agenda chapters; 3,332 are locator-eligible. New arrivals are
small: 22 episodes published in 7 days, 174 in 30. Every eligible-not-done backlog below is deep
archive (median publish age 5 to 9 years).

| Stage | agenda | locator | tagger | moments | judge (anchor / sibling / adjudicator) |
|---|---:|---:|---:|---:|---|
| 1 In scope | 13,128 | 13,128 | 16,903 | 8,138 | 16,907 |
| 2 Upstream ready | 6,993 | 2,974 | 16,903 | 3,920 | 16,907 |
| 3 Eligible, not done | 152 | 2,436 | 11,324 | 145 | ≤ 16,508 (upper bound) |
| 4 Submitted, pending handle | 66 | 642 | 0 last run | 99 | 175 / 301 / 62 packets |
| 5 Queued in DO | 0 | 274 | 337 | 6 | 193 / 246 / 46 |
| 6 In flight | not observable | not observable | not observable | not observable | not observable |
| 7 Completed, not collected | not observable | not observable | not observable | not observable | not observable |
| 8 Durable | 6,841 | 538 | 5,579 | 3,775 | 399 episodes |
| 9 Errored / blocked upstream | 86 / 6,135 | 71 / 10,154 | 0 / 0 | 46 / 4,218 | 0 / 0 |

Stages 6 and 7 are global only (14 leased; about 440 finished-but-unreconciled handles, from 1,558
deferred-index refs against about 1,116 queued or leased). No eligibility timestamp exists, so
oldest age and median wait are not derivable; publish-to-durable medians for recent episodes are
33 h (agenda), 62 h (tags) and 98 h (moments). `remedy:p3` emits no telemetry.

Classification:

| Lane | Class | Evidence |
|---|---|---|
| chapter-agenda | (a) input-starved | 152 eligible; 6,135 blocked on agenda text, flat all week. Was gated until 11-10 (493 of 512 candidates behind the terminal-retry gate), then 493 enqueued in one run |
| chapter-locator | (b) producer-limited, then (c) | 1,723 stale-recipe episodes sit "ready" and are never submitted; six runs failed on `model_not_in_lane` (11-09 to 11-10); the >76k-token band has no working route (B3) |
| topic-tags:tagger | (c) dispatcher-limited; (e) budget-capped on 11-06 and 11-07 | 11,324 eligible; done grew 1,676, 3,074, ~600, ~300 per day; `purpose_write_budget_exceeded` deferred 639 to 753 on three runs, 2,205 refused once |
| r6-moments | (a) input-starved | 145 eligible; 4,218 blocked on transcripts |
| judge:* | (c) dispatcher-limited, (d) return-path-limited | stops on outstanding-packet caps (11 of 28 runs) and adjudication budget (15 of 28) while uncollected results count as outstanding; sibling lane 1.56M failed slot-seconds; anchor route returns 400 |

## 5. Ranked bottlenecks

### B1. In-flight slots are held until the bundle's slowest call ends

- Evidence: one `completeBatch` per bundle after `Promise.allSettled` (`index.js` scheduled
  handler); `active_call_count` is set at claim and never decremented, and route and provider leases
  are deleted only in `completeBatch` (`coordinator.js`). Call end to commit: p50 20 s, p90 678 s,
  p99 725 s over 23,007 logged calls. 24% of bundles last 700 s or more.
- Lost: 4.07M slot-seconds, 58% of all held slot-time. A Flash Lite call that returns in 3 s can
  hold its slot, and its route's single lane, for 12 minutes.
- Root cause: bundle-level settlement predates slow routes; it was chosen to bound DO writes.
- Fix (WP4): commit settled calls early. The Worker flushes settled results through a partial
  completion when at least one call has settled and either all have or 20 s have passed since the
  last flush, at most three commits per bundle. The DO decrements `active_call_count` and releases
  those jobs' route and provider leases. Lease expiry and stale-token handling are unchanged for
  the remaining calls.
- Implemented (WP4, branch `feat/58-wp4-early-slot-release`): `completePartial` on the DO runs
  the same settlement as `completeBatch`; the bundle entry keeps `settled_job_ids`, so a retried
  partial or a final that repeats a job settles nothing twice; the final is never delayed by a
  partial. The 20 s / three-commit setting above does not fit the row budget at 6,000 results a day
  (section 8), so the defaults are one partial per bundle after 60 s
  (`EARLY_SLOT_RELEASE_DEBOUNCE_SECONDS` 60, `EARLY_SLOT_RELEASE_MAX_COMMITS` 2), and the DO refuses
  partials past `EARLY_SLOT_RELEASE_DAILY_ROWS` (1,000 rows a day, prorated) or while the day is
  ahead of the 85,000-row pace. `EARLY_SLOT_RELEASE_ENABLED: 0` switches it off. `bundle_completed`
  adds `partial_flushes`, `commits` and per call `released_by`; `hold_ms` now ends at the commit
  that released the call.

### B2. Doomed calls run to the 720 s response timeout

- Evidence: 2,122 `provider_response_timeout` attempts, 1.53M slot-seconds. AI Studio Gemma: 1,556
  timeouts, yet of its 2,011 successes only 11 took more than 300 s (p99 201 s). Timeout share does
  not vary with input size where size is known (13% under 10k tokens, 16% at 10k to 25k).
- Lost: 22% of held slot-time, plus the B1 hold these calls impose on bundle-mates.
- Root cause: one global timeout for routes whose healthy latency differs by 100×; no route streams,
  so there is no idle detection.
- Fix (WP2): a per-route `response_timeout_seconds` in `config/provider_limits.yml`, capped by
  `MAX_RESPONSE_SECONDS`. Initial values: AI Studio Gemma 240; Gemini Flash Lite 300; Nemotron 3.5
  Super 420; DeepSeek V4 Flash 420; all others unset (720). For Gemma this gives up 0.5% of
  successes and returns 480 s on each of 1,556 timeouts (747k slot-seconds a week).
- Implemented (WP2): values set on `gemma_4_31b_primary` and `gemma_4_31b_secondary` (240); the two
  Gemini 3.5 and two 3.1 Flash Lite routes (300); `kilo_nemotron_3_5_super_vl_free`,
  `openrouter_nvidia_nemotron_3_5_super_vl_free`, `nvidia_nemotron_3_5_super_vl_120b_a12b_free`,
  `orcarouter_deepseek_v4_flash_free` and `beatapi_deepseek_v4_flash_free` (420). Left unset:
  every DeepSeek V4.1 route, Nemotron 3 Ultra, other-provider Gemma routes (NVIDIA, OpenRouter,
  SambaNova, Gemma 4 26B) and MiniMax M3.1 Flash. The evidence file has per-route latency only
  for Gemma; the 300 and 420 values are the review's initial figures to tune from the new
  `response_timeout_ms` and `call_duration_ms` fields of `bundle_completed`.

### B3. Failing routes are re-selected every tick

- Evidence and cause, per route:
  - `beatapi_jev_1_13_free` (`judge:anchor`): 717 × 400 against 26 successes. `request_defect` is
    terminal for the job and sets no route cooldown; 0 of 882 jobs whose first failure was a request
    defect recovered. Its `yields_to` rule also holds back the `beatapi_*` backup routes. Cause
    (WP3c, 2026-11-11): not a request defect. BeatAPI withdrew `jev-1.13-free` from the key (HTTP
    400 `bad_request`, "not available to this key", `retryable: false`); the Worker classes this
    route-level availability failure as `request_defect`.
  - AI Studio Gemma (`judge:sibling`): 6,934 × 503 `upstream_capacity`, route cooldown 5 s or less,
    up to 8 requeues; 7.5% of attempts succeeded in the last 48 h.
  - BeatAPI GLM 5.3 Flash (both accounts, `judge:adjudicator`): 721 × 524. BeatAPI's edge ends a
    non-streaming call at about 125 s and streaming is disabled for CPU, so any call needing more
    than about 125 s cannot succeed. 264k slot-seconds.
  - `nvidia_deepseek_v4_1_flash_free` (`chapter-locator` above 76k tokens, and some
    `judge:adjudicator`): 6 successes (AI Gateway) against 103 timeouts (Worker log), about 5%.
    The only other V4.1 route is BeatAPI (128k context, same 125 s edge limit), so this locator
    band has no dependable route. 398 results were pending on this model at 03:30Z. Owner decision
    2 in section 10 moved this band to MiniMax M3.1 Flash (WP8, done 2026-11-11): packets of
    76,001 to 500,000 estimated tokens now go to `nvidia_minimaxai_minimax_m3_1_flash`, V4.1 is out
    of the locator lane (it stays in `judge:adjudicator`), and the 7 larger packets are held as
    deferred. Takes effect when the owner deploys the Worker with the recompiled config.
- Retries after a failure go to the same route 58% of the time, another route of the same model
  36%, another model 5%. Preferred/backup fallback (review/46) is not what recovers jobs; repetition
  is.
- Fix (WP3a config, WP3b coordinator): stand down the routes that cannot succeed; add a route health
  ladder that escalates cooldown on consecutive same-class failures with no intervening success
  (request defect, response timeout, upstream capacity), reset by the first success.
- WP3b as built (PR open, branch `feat/58-wp3b-route-health-ladder`): thresholds timeout 1,
  capacity 2, request defect 3 consecutive; rungs 60 s / 300 s / 900 s / 3600 s on the route's
  `blocked_until`, then one half-open probe; the first success resets. A single request defect
  still fails only its job. The "not available to this key" 400 is still classed `request_defect`
  (`classify.js` rule 10): a route-scoped class is not a one-line change, because the auth block
  stands down the whole account, so the three-in-a-row rule is what stands JEV down. AI Gateway
  retries: the Worker sends `cf-aig-max-attempts: 1` only for AI Studio Gemma (both keys) and
  SambaNova; every other route uses the gateway's own policy (3 attempts, 5 s exponential), so one
  Worker-visible failure there is up to three provider attempts. For Google Gemma the ladder
  supersedes review/56's 5 s availability cap from the second consecutive 503 on a route.

### B4. The return path collects about three times a day

- Evidence: producers never poll (`BatchingDispatchBackend.poll_batch` returns `{}`); 64
  `poll-batch` and 40 `terminal-feed` calls in 7 days. `llm-deferred-sweep` ran 21 of 28 scheduled
  times with a median start delay of 201 minutes; each run leaves 200 to 1,450 handles pending.
- Lost: results wait up to a sweep interval plus a producer interval (typically 6 to 12 h) before a
  later lane can use them; judge admission is throttled because uncollected packets count as
  outstanding; ingress-deferred jobs wait for the next sweep even after the 00:00 UTC budget reset.
- Fix (WP5): schedule the sweep hourly with the existing concurrency group and a 45-minute budget.
  At one third delivery that is at least eight runs a day, about 4,200 results a day at the measured
  525 per run, and more per run once dispatch is faster.

### B5. Producer cadence and gates

- GitHub executed 3.9 of 12 scheduled runs a day for agenda, locator and judge, 14 of 21 for tag,
  21 of 28 for the sweep. Median gap between runs is about 6 h, worst 14.5 h. No run was cancelled
  by a concurrency group.
- Agenda and locator spend about 85% of each run on restore, load and a 7 to 8 minute state push.
- Locator: 1,723 stale-recipe episodes are never submitted; a busy `chapter-record-write` lease
  fails the run; the step timeout is 38 minutes against a 204-minute stop budget.
- Tagger: 316 to 353 oversized chapter batches per run until 11-09 (17 since); the lane's
  2,057 jobs/day ceiling closed ingress on two days.
- Fix (WP6): treat a busy lease as a clean skip; align the locator step timeout with its budget;
  submit the stale-recipe locator backfill; tag every four hours. The cron drop rate itself needs an
  owner decision (section 10).

### Not bottlenecks (measured)

DO row budget; job and bundle day caps; `MAX_ACTIVE_BUNDLES`; cron wall and CPU limits; subrequests;
per-route start rate (one start per route per minute is 1,440 a day per route, above every free
route's daily request limit); enqueue rate; `MAX_QUEUED_JOBS`; dispatch pauses.

## 6. Retry and failure remediation

Definition. The "~50% retried" figure is `retried_today ÷ completed_today` (1,426 ÷ 2,979 = 48% on
2026-11-10). That counter also includes jobs handed back for missing the dispatch window and
excludes in-window and 429 retries. The stable measure is per attempt: 12,474 failed of 27,408
provider attempts (45.5%), and 25,774 claims for 14,934 accepted results (1.73 claims per result).

| Failure class | Attempts | Slot-seconds | Recovers on retry? | Remedy |
|---|---:|---:|---|---|
| `provider_response_timeout` | 2,122 | 1,527,845 | Rarely on the same route (47 of 230 Gemma jobs) | WP2 route timeouts; WP3b ladder |
| `server_error` (BeatAPI 524) | 716 | 271,902 | No, for calls over ~125 s | WP3a stand-down |
| `upstream_capacity` (Gemma 503) | 7,206 | 186,489 | About half (693 of 1,473 jobs) | WP3b ladder |
| `structured_output_invalid` | 669 | 45,447 | About a third | Leave; one corrective resubmit exists |
| `request_defect` (JEV 400) | 1,061 | 14,283 | Never | WP3a stand-down; WP3c diagnosis |
| `output_budget_exhausted` | 53 | 8,757 | — | Leave |

Targets after WP2 to WP4: failed attempts under 20% of attempts (from 45.5%); timeouts under 3% of
attempts (from 7.7%); claims per accepted result at or under 1.3 (from 1.73). The largest single
gain is success rate on `judge:sibling`, which today spends 1.56M failed slot-seconds for 88k
successful.

## 7. Timeout recommendation

Measured inputs: finalization (`completeBatch`) p50 156 ms, p99 300 ms, max 4.4 s; commit to
invocation end at most 3.2 s; first call start at most 39 s after invocation start; no invocation
longer than 750 s.

- **Do not raise the global timeout now.** Successes between 600 s and 720 s: 61 of 14,934 (32
  OrcaRouter GLM, 16 NVIDIA Gemma, 7 Nemotron 3 Ultra). Extrapolating each tail by another 60 s
  recovers roughly 25 of the 460 timeouts on those three families per week. Applied to all routes
  it would add 2,122 × 60 s = 127k slot-seconds a week of doomed waiting, and more through B1.
- **Shorten per route first** (WP2); that is where the measured gain is.
- **Then, optionally, permit 780 s on the three slow-generation families only** (WP7), with this
  set, which satisfies `validateConfig`:

  | Knob | Now | Proposed |
  |---|---:|---:|
  | `DISPATCH_WINDOW_SECONDS` | 30 | 30 |
  | `MAX_RESPONSE_SECONDS` (ceiling for route values) | 720 | 780 |
  | `FINALIZATION_RESERVE_SECONDS` | 90 | 45 |
  | `LEASE_DURATION_SECONDS` | 840 | 870 |

  30 + 780 + 45 = 855 ≤ 870 < 900. Worst invocation: 39 s to start + 780 + 5 s to commit = 824 s,
  76 s under the cron limit. The 45 s reserve is ten times the largest finalization observed. Net
  modeled effect: about 25 more results a week for 28k slot-seconds (0.4%), so it ships last and
  only if B1 is fixed.
- **Not recommended: moving calls off the cron lifetime.** A DO alarm also has a 15-minute wall
  limit, and holding the DO active for calls would use about 10,800 of the 13,000 GB-s daily Free
  duration allowance. Queues on Free allow 10,000 operations a day. No measured limit of the cron
  invocation is binding.

## 8. Balanced target design

| Stage | Setting | Steady state |
|---|---|---|
| Dispatcher slots | `MAX_IN_FLIGHT_LLM_CALLS` 20 counts live calls (WP4); `MAX_ACTIVE_BUNDLES` 12 unchanged | Slot-seconds per accepted result ≤ 150 (from 473) |
| Route timeouts | Per route (WP2) | Timeouts < 3% of attempts |
| Route health | Ladder on consecutive failures (WP3b) | Failed attempts < 20% |
| Ingress | 6,000 jobs/day, 38,400 units unchanged | Tagger lane ceiling reviewed after WP4 (section 10) |
| Return path | Sweep hourly, 45-minute budget (WP5) | Collection lag p90 ≤ 2 h |
| Producers | Tag every 4 h; lease-busy skip; locator backfill (WP6) | No run fails on a busy lease |

Capacity arithmetic: at the measured mean of 11.7 held slots, 11.7 × 86,400 ÷ 150 ≈ 6,700 accepted
results a day; at 62 s (pure success latency) it would be 16,000. So after the fixes the binding
limits are, in order:

1. `MAX_JOBS_PER_UTC_DAY` 6,000.
2. DO rows: 10.7 rows per claim × 1.3 claims per result ≈ 14 rows per result (18.5 today). At
   6,000 results a day that is about 84k rows before WP4, against the 90,000 safe stop. WP4's cost,
   measured in `row-accounting.test.js`: a partial commit adds exactly one row per bundle in legacy
   and packed modes (completion rows for a three-call bundle with 0, 1, 2 partials: legacy 13, 14,
   15; packed 7, 8, 9), and two in packed mode when the partial and the final touch routes on the
   same telemetry shard. At 2,364 bundles a day, two partials per bundle would add 4,700 to 9,500
   rows (88.7k to 93.5k), and even one per bundle 2,400 (86.4k): the proposed 20 s / three commits
   does not fit 85k. No limit was raised. Defaults instead: one partial per bundle after 60 s, and a
   daily partial budget of 1,000 rows (`EARLY_SLOT_RELEASE_DAILY_ROWS`, charged at two rows each,
   prorated over the day), so a day ends at most 1,000 rows above its other work, about 85,000 at
   6,000 results; partials also stop while the day is ahead of the 85,000-row pace. At today's
   1,600 to 3,300 results (39k to 60k rows) the budget allows about 500 partials a day, roughly one
   for every bundle that runs past 60 s. To spend more, raise `EARLY_SLOT_RELEASE_DAILY_ROWS` only
   with headroom (each 1,000 results/day under 6,000 frees about 14,000 rows), or lower the job cap
   as this section already provides. Worst-case DO RPCs per bundle: claim, one per authorized 429
   retry, `MAX_COMMITS − 1` partials, final (one more than before at the default); per tick the same,
   since a tick runs one bundle.
3. Provider pools: AI Studio Gemma for `judge:sibling` (2,011 successes a week at a 7.5% recent
   success rate) and free-tier daily request limits per route.
4. Upstream: 12,641 episodes waiting on ASR and 6,374 on agenda text.

DO reads: WP1 adds none. The hourly sweep adds about 20 `terminal-feed` and poll calls a day to the
104 a week measured today.

## 9. Permanent observability and SLOs

Signals (WP1a Worker logs, WP1b producer telemetry and dashboard), none of which add DO rows:

- Worker log `scheduled_claim` on every claimed tick (jobs, routes, in-flight, active bundles) and
  `bundle_completed` (duration, and per call: route, purpose, outcome, duration, hold). Successful
  calls on non-Gemma routes are not logged at all today.
- Producer run-event `llm_work`: split `queued` into waiting at the Worker and completed-not-
  collected (the poll response already distinguishes them); add oldest-eligible age and first-seen
  timestamps; add per-purpose admitted, replayed and deferred counts; give `remedy:p3` and judge
  packet-level counts.
- `/admin/status` lane table: for each lane, the class from section 4 computed from those fields
  ("waiting on transcripts", "waiting on agenda text", "eligible backlog N, oldest X h"), slot
  productivity, and failed-attempt share.

SLOs, measured over a UTC day while the DO queue is non-empty:

| SLO | Target | Today |
|---|---|---|
| Slot productivity (successful call-seconds ÷ held slot-seconds) | ≥ 50% | 13% |
| Idle hold share | ≤ 10% | 58% |
| Minutes with fewer than 10 live calls while queued > 0 | ≤ 10% | 39.5% |
| Failed attempts ÷ attempts | < 20% | 45.5% |
| Response timeouts ÷ attempts | < 3% | 7.7% |
| Collection lag, Worker completion to B2 record, p90 | ≤ 2 h | not measurable; up to 6 h+ by cadence |

"In-flight ÷ admissible" cannot be computed until WP1a logs admissible capacity per tick; that SLO
is added then.

## 10. Owner decisions (2026-11-11)

1. **Producer triggering: decided.** The Worker's cron fires `repository_dispatch` for the producer
   workflows. This needs a GitHub token as a Worker secret (owner sets it) and one subrequest per
   trigger. Added to WP6.
2. **Locator packets above 76k tokens: decided, MiniMax M3.1 Flash.**
   - Why it matters: 494 of the 716 pending locator packets (69%) are above the 76,000 cutoff, and
     the only approved route for them, V4.1 on NVIDIA, succeeded on about 5% of calls this week.
   - Scored on 2026-11-11 on the 12 long meetings (item and time within 60 s, baseline prompt):
     MiniMax M3.1 Flash 88.0% with 12 of 12 valid at a 56 s median; Nemotron 3.5 Super VL 82.4%;
     Nemotron 3 Ultra 82.6% on the 9 it answered, with three timeouts; Gemini 3.1 Flash Lite 74.5%
     on 11; Gemini 3 Flash Preview 94.2% on 7 but limited to 20 requests a day; HY3 rejected every
     packet as too large. Reference: Kimi K3 90.7% (retired), V4.1 89.8% at a 502 s median. Full
     tables are in `evals/chapter-locator/manual-review-v2/README.md`.
   - Decision: route packets above 76,000 estimated tokens to `minimaxai/minimax-m3.1-flash`
     (`nvidia_minimaxai_minimax_m3_1_flash`) with the production prompt. Remove V4.1 from the
     locator lane. This is WP8.
   - Cap: **500,000 estimated tokens** (`LOCATOR_OVERFLOW_MAX_INPUT_TOKENS`, next to the 76,000
     split in `citypods/chapter_locator.py`), set from the WP8a probe: MiniMax admitted up to
     941,663 provider tokens (610,831 estimated) and rejected 709,545 estimated with HTTP 400
     (evidence file, "MiniMax input-size probe (WP8a) and pause log"). The cap covers 487 of the
     494 overflow packets (342 up to 191,294 plus 145 from 191,295 to 500,000).
   - The 7 packets above 500,000 are held as deferred (counted as `locator-over-input-cap` in the
     producer's run event) and become eligible on their own if the cap is raised. The largest is
     862,960 estimated tokens and no free route admits it as one request; it needs a split-packet
     design, tracked in [GH#2651](https://github.com/BashfulBits/city-meeting-podcasts/issues/2651)
     and not in this plan.
   - Risks: accuracy above 191,294 estimated tokens is unmeasured, the probe checked admission
     only, and 2 of its 5 admitted large answers were invalid (duplicate locator unit; the producer
     makes one corrective resubmit, then gives up). If large-packet quality proves poor, lower the
     one constant (for example to 300,000). The score rests on 12 meetings and one pass; MiniMax's
     output limit is 12,800 tokens (about 2.6 times the ~4,900 an 84-item agenda needs); the route
     allows one call at a time at 1 RPM (at most 1,440 calls a day; observed latency 44 to 225 s)
     and shares NVIDIA's three slots.
   - No free Kimi K3 route exists on any configured provider (checked 2026-11-11).
3. **JEV diagnosis: approved, done 2026-11-11 (WP3c).** BeatAPI withdrew the free model, so the
   400s are not a defect in our request (see the evidence file's "JEV 400 diagnosis (WP3c)").
   Restoring anchor judging now needs an owner decision on the anchor model: wait for a free jev
   model, approve a paid `jev-1.13`/`jev-1.14` route, or choose a different anchor.
4. **Tagger daily ceiling: approved** to raise once WP4 shows the dispatcher drains it, within the
   6,000 global cap.
5. **AI Gateway read permission: granted** on the ops token. Gateway retry, rate-limit and error
   bodies are to be read before WP3b is briefed.

## 11. Work packages

Order is the production-safety order. Only one coordinator-touching PR is in flight at a time
(WP3b, then WP4, then WP7).

| # | Package | Model | Depends on | Expected gain | Verification |
|---|---|---|---|---|---|
| WP1a | Worker logs: `scheduled_claim`, `bundle_completed` (`index.js` only) | Sonnet | — | Measurement for all later gates | Workers Logs: both events present on every claimed tick **Status: implemented in `feat/58-wp1a-dispatch-worker-logs` (PR open); owner deploys the Worker.** Admissible capacity is not in the claim response, so it is not logged; the in-flight ÷ admissible SLO still needs a coordinator change |
| WP1b | Producer funnel telemetry + `/admin/status` lane table (**implemented, PR pending merge**: producer events carry `queue`, `admission`, `packets`, run-local first-seen times; the status table classes every lane; the waiting-vs-completed split, slot productivity and failed-attempt share read "not measured" because producers do not poll and those figures are dispatcher-side) | Sonnet | — | Closes the visibility gap | Status page shows a class for every lane |
| WP2 | Per-route response timeout (config, compile script, `index.js`) | Sonnet, Opus review | WP1a | ~750k slot-seconds/week | Gemma timeouts end at 240 s; timeout share < 3% **Status: implemented in `feat/58-wp2-per-route-response-timeout` (PR open); merging deploys the Worker.** `bundle_completed` calls carry `response_timeout_ms` |
| WP3a | Stand down routes that cannot succeed; fix stale tuning comments. **Status: implemented in PR (branch `fix/58-wp3a-stand-down-failing-routes`), awaiting merge and owner deploy.** `rpd: 0` on `beatapi_jev_1_13_free` and both `beatapi*_glm_5_3_flash_free`; the primary BeatAPI chat routes' `yields_to` JEV removed (re-add when WP3c restores JEV). `judge:adjudicator` keeps GLM via OrcaRouter and DeepSeek V4.1 via NVIDIA, BeatAPI primary and secondary. | Sonnet | — | ~350k slot-seconds/week; stops 882 terminal failures | Zero attempts on the stood-down routes |
| WP3b | Route health ladder (coordinator). **Status: implemented in PR (branch `feat/58-wp3b-route-health-ladder`), awaiting merge; merging deploys the Worker.** Ladder state on `scheduler.route_health_json` (no extra row for a healthy route, no extra read); `admissible_calls` added to the claim diagnostics and `scheduled_claim`; rung in `/v2/stats?routes=1`; off switch `ROUTE_HEALTH_LADDER_ENABLED: 0` | Opus | WP1a | Failed attempts < 20% | `attempt_outcome` counts per route per day; `route_health` events |
| WP3c | JEV 400 diagnosis. **Done 2026-11-11:** BeatAPI withdrew `jev-1.13-free` from the key; restoring needs an owner decision on the anchor model (free model returns, paid route, or other anchor). | Opus | owner Q3 | Restores `judge:anchor` | Anchor success rate |
| WP4 | Early slot release (coordinator + `index.js`). **Status: implemented in PR (branch `feat/58-wp4-early-slot-release`), awaiting merge; merging deploys the Worker.** `completePartial` with per-bundle dedupe; defaults one partial after 60 s and 1,000 partial rows/day (section 8: 20 s / three commits does not fit 85k at 6,000 results); off switch `EARLY_SLOT_RELEASE_ENABLED: 0` | Opus | WP1a, WP3b merged | Removes the 58% idle hold | Hold share ≤ 10%; rows/day < 85k; `bundle_completed` `released_by`, `partial_flushes` |
| WP5 | Hourly sweep (**implemented 2026-11-11**: cron `57 * * * *`, concurrency unchanged, budget 32 < step 41 < job 45 min; about 20 more terminal-feed/poll calls a day) | Sonnet | — | Collection lag ≤ 2 h | `poll-batch` calls/day; pending handles per run |
| WP6 | Producer fixes (lease-busy skip, locator timeout and backfill, tag cadence, Worker-cron `repository_dispatch` trigger) | Sonnet; Opus for the backfill | — | No failed runs; locator backlog drains | Run conclusions; locator `ready` count falls |
| WP7 | Optional 780 s for three slow families | Opus | WP4 verified | ~25 results/week | Successes with duration 720 to 780 s |
| WP8 | **Done 2026-11-11.** Locator overflow to MiniMax M3.1 Flash: lane model list, size split 76,001 to 500,000 estimated tokens (cap from WP8a), hold larger packets as deferred, drop V4.1 from the lane, route output limit check. Result: locator lane is V4 Flash + MiniMax; V4.1 stays only in `judge:adjudicator`; locator `max_tokens` clamps to MiniMax's 12,800; queued V4.1 locator jobs are cancelled and re-planned; no routing-version bump, so V4 Flash results stand | Opus | — (owner deploys the compiled route config) | 487 of 494 overflow packets get a working route; 7 held | Locator results on `nvidia_minimaxai_minimax_m3_1_flash`; no locator attempts on V4.1; `locator-over-input-cap` deferred count reported (expected 7) |
| WP8a | **Done 2026-11-11.** MiniMax input-size probe (approved by the owner 2026-11-11): with the route paused, send real pending locator packets of increasing size above 191,294 estimated tokens through the evaluation runner until one is rejected or times out; record admitted and rejected provider token counts and latency; update `hard_input_ceiling` and the WP8 cap from the result. Result: admitted up to 941,663 provider tokens (610,831 estimated), rejected 709,545 estimated (HTTP 400); `hard_input_ceiling` 941,663, cap 500,000; 2 of 5 admitted large answers invalid; two route pauses logged and released | Lead (holds the pause) | — | 145 more packets (191,295 to 500,000) | Evidence file, "MiniMax input-size probe (WP8a) and pause log" |

**WP6 status.** Part A is in the `fix/58-wp6a-producer-run-fixes` PR: the chapter-lane record-write
lease skip (bounded wait, then a clean deferral stamped `record_write: skipped_lease_busy` on the
run event), the locator timeouts (job 240 and step 225 minutes against the 204-minute stop budget,
matching chapter-agenda) and the tag cadence (`19 */4 * * *`). Still open under WP6: the stale-recipe
locator backfill and the Worker-cron `repository_dispatch` trigger.
