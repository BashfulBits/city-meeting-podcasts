# LLM pipeline throughput evidence — 2026-11-11

Supports [review/58](../58-llm-pipeline-throughput-balance.md). Raw pulls and reduction scripts were
kept outside the repository and are not committed; this file records the sources, the queries, the
reduced numbers and the limits of each source.

## Method and access

- Window: 2026-11-04 03:31Z to 2026-11-11 03:31Z (Workers Logs returned 7 days). GitHub: runs
  created from 2026-11-04 00:00Z.
- Production was read only apart from the pauses in the pause log below. During the measurement
  window itself no `dispatch:pause` or `dispatch:resume` was issued; `dispatch:pause-status` at
  2026-11-11 03:25Z: no pauses, 20 in flight.
- `/v2/stats` reads, all at 2026-11-11 03:25Z, once each: default, `?lanes=1`, `?routes=1`,
  `?rate_failures=1`, `?availability_failures=1`, and `?detail=1&limit=100` (once for the whole
  effort). `ingress-status` once. `/v2/calibration` returned 400 without parameters.
- Collection was done by Sonnet subagents from Workers Logs, the Cloudflare GraphQL analytics API,
  `gh`, and B2 state; code-path analysis by an Opus subagent. The lead spot-checked the commit path
  (`index.js` scheduled handler; `coordinator.js` `_setActiveBundle`) and recomputed the slot
  accounting from the reduced files.

## Sources and their limits

| Source | Query | Limits |
|---|---|---|
| Workers Logs | Observability telemetry query API, `view=events`, raw events in 30-minute windows (127,132 events) | Aggregations are sampled and rounded, so only raw events were used. A 24 h events query silently truncates. No event exists for a successful claim or for a successful non-Gemma call, so in-flight, active bundles and bundle size are reconstructed from claim and `completeBatch` timing (matches logged values on empty ticks 84% to 96% of the time) |
| AI Gateway | GraphQL `aiGatewayRequestsAdaptiveGroups`, 24,746 unsampled rows | Duration is recorded only for 200s and client aborts (499). No error text, no job, lane or route metadata. The REST logs and gateway-config endpoints returned an authentication error for the ops token |
| GitHub Actions | `gh run list`, `gh run view --json jobs`, `gh run view --log` | Phase times come from log line timestamps. Cron-delay figures are approximate when slots are skipped |
| B2 state | 181 `state/run_events` files, 48 per-source `episodes.json` records, deferred index listing (232 GETs) | No eligibility timestamp; ages are publish-date ages. Judge unit is the episode while work is packets |
| Worker stats | Cached responses above | A single instant; burndown covers 24 h only |
| Cloudflare docs | Fetched 2026-11-11 | Read through a summarizing fetch; re-verify exact wording before relying on a quoted sentence |

## Slot accounting by day

Held slot-seconds = reconstructed in-flight integrated per minute. Successful seconds = AI Gateway
200 durations. Failed seconds = Worker `attempt_outcome` and Gemma attempt durations. Idle hold =
remainder.

| UTC day | Mean in-flight | Accepted (200) | Success s | Failed attempts | Failed s | Timeouts | Idle hold share |
|---|---:|---:|---:|---:|---:|---:|---:|
| 11-04 (from 03:31) | 3.4 | 837 | 74,757 | 544 | 121,420 | 110 | 22% |
| 11-05 | 9.4 | 1,824 | 85,225 | 2,270 | 259,856 | 266 | 58% |
| 11-06 | 13.5 | 2,644 | 182,259 | 1,740 | 292,067 | 319 | 59% |
| 11-07 | 13.9 | 2,549 | 150,057 | 2,015 | 341,061 | 328 | 59% |
| 11-08 | 13.1 | 3,161 | 130,267 | 1,462 | 310,539 | 320 | 61% |
| 11-09 | 8.6 | 663 | 80,303 | 2,211 | 337,335 | 357 | 44% |
| 11-10 | 18.1 | 3,096 | 201,412 | 1,886 | 331,788 | 345 | 66% |
| 11-11 (to 03:31) | 14.0 | 160 | 21,477 | 346 | 70,789 | 77 | 47% |
| **Total** | **11.7** | **14,934** | **925,761** | **12,474** | **2,064,859** | **2,122** | **58%** |

Held total 7,057,440 slot-seconds; configured capacity (20 slots) 12,093,600.

## Claims and ticks

| UTC day | Ticks | Claimed | Empty: in-flight limit | Empty: bundle limit | Empty: mixed limits | Mean jobs per bundle |
|---|---:|---:|---:|---:|---:|---:|
| 11-04 | 1,238 | 617 | 0 | 3 | 569 | 1.59 |
| 11-05 | 1,439 | 1,198 | 9 | 150 | 81 | 3.02 |
| 11-06 | 1,441 | 1,129 | 198 | 90 | 24 | 3.74 |
| 11-07 | 1,444 | 1,269 | 160 | 0 | 15 | 3.48 |
| 11-08 | 1,443 | 1,187 | 181 | 0 | 73 | 3.83 |
| 11-09 | 1,470 | 1,297 | 28 | 0 | 132 | 2.05 |
| 11-10 | 1,463 | 936 | 507 | 0 | 20 | 5.21 |

- Cron invocations: 10,161, all `ok`. Wall p50/p90/p99/max 68/722/746/750 s. CPU 16/38/65/182 ms.
- Bundles: 7,813; claim to commit p50 108 s, p90 723 s; 1,893 lasted 700 s or more.
- Call end to commit start: p50 20 s, p90 678 s, p99 725 s (23,007 logged calls, a failure- and
  Gemma-biased subset).
- `completeBatch` RPC: p50 156 ms, p99 300 ms, max 4.4 s. Claim RPC: p50 265 ms, p99 1.1 s, max
  14.6 s. Invocation start to first call: p50 1.5 s, p90 15.6 s, max 39 s.
- In-flight below 10 for 39.5% of minutes (7 days), 32.2% (last 48 h). Longest such period after the
  11-05 tuning change: 101 minutes from 11-08 19:25Z.
- DO rows per day (DO counter peaks): about 60k on 11-06, 52k on 11-07, 60k on 11-10. Rows per
  claimed job 10.7 (claim 3.36, completion 3.79, remainder enqueue, retire and purge).
- Fetch traffic, 7 days: enqueue-batch 129, poll-batch 64, ack-batch 500, retire-batch 53,
  terminal-feed 40, stats 2,805, pause 2,400, pause-status 2,429, reserve 1,032.

## Failures

By class (Worker logs, 7 days):

| Class | Attempts | Slot-seconds | Mean s |
|---|---:|---:|---:|
| `provider_response_timeout` | 2,122 | 1,527,845 | 720 |
| `server_error` | 716 | 271,902 | 380 |
| `upstream_capacity` | 7,206 | 186,489 | 26 |
| `structured_output_invalid` | 669 | 45,447 | 68 |
| `request_defect` | 1,061 | 14,283 | 13 |
| `output_budget_exhausted` | 53 | 8,757 | 165 |

By route, failed slot-seconds: `gemma_4_31b_secondary` 695k, `gemma_4_31b_primary` 649k,
`nvidia_gemma_4_31b_it_free` 218k, `beatapi_secondary_glm_5_3_flash_free` 188k,
`beatapi_glm_5_3_flash_free` 76k, `nvidia_deepseek_v4_1_flash_free` 75k,
`nvidia_nemotron_3_ultra_550b_a55b_free` 53k, `orcarouter_zai_glm_5_3_flash_free` 46k.

By lane, failed against successful slot-seconds: `judge:sibling` 1.56M against 88k (logged);
`judge:adjudicator` 342k; `chapter-agenda` 82k; `chapter-locator` 44k.

Gateway status mix (24,746 requests): 200 14,934; 503 4,003; 500 3,361; 400 1,063; 524 727;
429 595; 410 20; 499 14; other 29. No request is recorded past 720.011 s; the longest success is
713 s. Successes over 600 s: 61; over 660 s: 30.

Retry behaviour: next attempt after a failure went to the same route 5,559 times (58%), a different
route of the same model 3,483 (36%), a different model 515 (5%). Gemma-only jobs: 2,404 jobs, 9,662
attempts, 1,279 completed at 2.78 attempts each.

## Producers and return path (GitHub Actions, 7 days)

| Workflow | Scheduled | Ran | Wall p50 / max (s) | Enqueued per run p50 / max | Notes |
|---|---:|---:|---|---|---|
| chapter-agenda | 84 | 28 | 838 / 1,145 | 1 / 493 | 493 of ~512 candidates gated until 11-10 00:03Z |
| chapter-locator | 84 | 28 | 897 / 1,306 | 10.5 / 425 | 6 failures on `model_not_in_lane`; 1 on a busy lease |
| judge | 84 | 28 | 2,577 / 3,787 | 129 / 1,237 | Stops on adjudication budget (15) or outstanding caps (11) |
| moments | 63 | 62 | 1,263 / 1,711 | 0 / 110 | 1 failure on `model_not_in_lane` |
| tag | 21 | 14 | 7,575 / 7,908 | 529 / 4,958 | Budget closed on 3 runs; 316 to 353 oversized batches/run until 11-09 |
| llm-deferred-sweep | 28 | 21 | 1,027 / 6,310 | — | Per run p50: 402 completed, 123 failed, ~750 left pending |
| audio | 42 | 21 | 897 / 2,510 | — | 1 to 12 episodes per run |
| asr | ~35 | 20 | 2,256 / 5,971 | — | 0 to 4 transcribed per run; scan budget exhausted in 8 of 8 sampled |

No sampled run hit its wall-clock stop, saw a dispatch pause, a full queue, or a Worker HTTP error on
enqueue.

## Platform limits checked (Cloudflare docs, Workers Free)

Cron wall time 15 minutes at every interval; CPU 10 ms documented (measured p99 65 ms without
kills); 50 subrequests per invocation; 6 simultaneous connections waiting for headers; `waitUntil`
30 s; DO 100,000 rows written, 5M rows read, 100,000 requests and 13,000 GB-s per day; DO alarm wall
time 15 minutes; Queues 10,000 operations a day; Workers Logs 200,000 events a day.

## Locator overflow scoring and pause log (2026-11-11 09:39Z to 12:01Z)

Six baseline-prompt arms were run with `evals/chapter-locator/manual-review-v2/run_model_arm.py` on
the 12 long meetings, one after another, with owner approval. The runner pauses its provider or
route, waits for in-flight calls to drain, calls the provider directly and records each call with
`dispatch:reserve`. Scores and failure causes are in the
[eval README](../../evals/chapter-locator/manual-review-v2/README.md).

Every pause was for one provider or one route, set by the runner and released by it at the end of
the run unless noted. `pause-status` was read before and after each; all twelve reads before and
after the first pass showed no other pause and 0 in flight for the target.

| Paused (UTC) | Released (UTC) | Target | Reason | Measured effect |
|---|---|---|---|---|
| 09:39 | 09:42 | provider `orcarouter` | HY3 arm | 12 direct calls, all rejected in about 16 s as too long for the free tier; 0 in flight at start |
| 09:42 | 10:03 | provider `kilo` | Nemotron 3.5 Super VL arm | 14 direct calls, 12 valid; 0 in flight at start |
| 10:03 | 10:43 | provider `kilo` | Nemotron 3 Ultra arm | 17 direct calls, 9 valid, 1 timeout; 0 in flight at start |
| 10:43 | 10:59 | route `gemini_3_1_flash_lite_primary` | Gemini 3.1 Flash Lite arm | 13 direct calls, 11 valid |
| 10:59 | 11:20 | route `gemini_3_flash_preview_primary` | Gemini 3 Flash Preview arm | 16 direct calls, 7 valid, 1 daily-quota 429 |
| 11:20 | 11:47 | route `nvidia_minimaxai_minimax_m3_1_flash` | MiniMax M3.1 Flash arm | 14 direct calls, 12 valid; route showed 14 requests used afterwards |
| 11:47 | 12:01 (manual resume) | provider `kilo` | Nemotron 3 Ultra retry round | 2 completed direct calls, both timed out at about 301 s, and a third was in flight when stopped. The owner cancelled the retry pass; stopping the runner left the pause set until 12:57Z, so it was resumed by hand at 12:01:11Z and `pause-status` at 12:01:12Z showed no pauses |

The provider-wide pauses on `kilo` and `orcarouter` stopped dispatcher traffic to every route of
that provider for the periods shown; the later arms used route pauses to avoid this. No pause is
outstanding.

Other calls made for this work, none through the dispatcher: one `/v1/models` listing per configured
provider (16), and three Airforce chat calls at 09:41 to 09:43Z, which returned HTTP 402
`paid_model_required` for `kimi-k3` and `kimi-k3.1-pro`.

## Pending locator packet sizes (2026-11-11)

Real locator requests were built from the CDN word transcripts for the 720 meetings with a pending
locator result; 4 have no accepted agenda items. Estimated input tokens for the other 716: up to
76,000: 222; 76,001 to 191,294: 342; 191,295 to 500,000: 145; above 500,000: 7. Median 107,252, p90
284,516, p99 474,974, largest 862,960. The estimate undercounts provider prompt tokens by 1.28 to
2.29 times depending on the tokenizer (measured on the 12 scored meetings).

## MiniMax input-size probe (WP8a) and pause log

To set the MiniMax M3.1 Flash ceiling for review/58 WP8, real production locator packets (baseline
prompt) of increasing size were sent directly to route `nvidia_minimaxai_minimax_m3_1_flash`, each
reserved against the route. "Estimated tokens" is the producer's `estimate_tokens` figure; "provider
prompt tokens" is the provider's reported `usage.prompt_tokens`.

| Estimated tokens | Provider prompt tokens | Result | Latency |
|---:|---:|---|---:|
| 219,129 | 348,229 | admitted, structurally valid, 6 anchors | 44.0 s |
| 299,155 | 430,156 | admitted, valid, 27 anchors | 225.3 s |
| 402,534 | 564,797 | admitted, INVALID: duplicate locator unit (items 17 and 18) | 155.1 s |
| 501,310 | 755,581 | admitted, valid, 13 anchors | 123.8 s |
| 610,831 | 941,663 | admitted, INVALID: duplicate locator unit (items 12 and 13) | 127.7 s |
| 709,545 | not reported | REJECTED: HTTP 400 "Invalid request" after 20.8 s | — |

The largest pending packet (862,960) was not sent. The provider/estimate ratio was 1.40 to 1.59.
The largest admitted input, 941,663 provider tokens, is the route's `hard_input_ceiling`; the
production cap is 500,000 estimated tokens (795,000 at the 1.59 prior plus 12,800 output). The
probe measured admission only: accuracy above 191,294 estimated tokens is unscored, and 2 of the 5
admitted answers were invalid. Raw probe output is not committed.

Pause log. Both passes paused only route `nvidia_minimaxai_minimax_m3_1_flash`, reason "review/58
WP8a MiniMax input-size probe"; no other route or provider was paused.

| Pass | Paused (UTC) | Released (UTC) | `pause-status` before | `pause-status` after | Calls |
|---|---|---|---|---|---|
| 1 | 22:13:21 | 22:23:46 | 22:11:01: no pauses, 0 in flight | 22:23:53: no pauses | 4 direct, each reserved against the route |
| 2 | 22:25:57 | 22:28:26 | 22:23:53: no pauses | 22:28:32: no pauses | 2 direct, each reserved against the route |

Measured effect: 0 in flight at each pause (drained in 0.1 s); 19 requests had been used on the
route that UTC day before the probe. No pause is outstanding.

## Correction to the V4.1 figures

An earlier draft said V4.1 on NVIDIA had no successes. The Worker does not log successful non-Gemma
calls; AI Gateway shows 6 successes (p50 242 s, longest 608 s) against 103 timeouts in the Worker
log. The gateway does not record the timed-out requests, so their sizes are unknown.

## JEV 400 diagnosis (WP3c, 2026-11-11 22:3xZ)

Read from AI Gateway logs and BeatAPI's `GET /v1/models` (no request bodies or keys recorded).

- Every `jev-1.13-free` call since 2026-11-05 returns HTTP 400 with body code `bad_request`,
  message "The requested model is not available to this key: check the model id against
  GET /v1/models, or retry in a few minutes if it worked before.", `retryable: false`, after about
  16 to 18 s.
- The last success was 2026-11-04T17:43Z, after 2,649 successes.
- BeatAPI's model list for the primary key no longer contains `jev-1.13-free`; it lists `jev-1.13`
  and `jev-1.14` (no free variant). The secondary key lists no jev model.
- The same message appeared sporadically from 2026-10-11.
- Conclusion: BeatAPI withdrew the free model from the key; the 400s are not a defect in our
  request. The Worker classes this as `request_defect` (terminal for the job) although it is a
  route-level availability failure.

## Gemini Decision 1 as anchor (WP9, 2026-11-12)

Canonical: [review/49, "Gemini Decision 1 as the anchor"](../49-judge-consensus-admission.md#gemini-decision-1-as-the-anchor-2026-11-12-review58-wp9).
This entry records only the figures this effort relied on.

- BeatAPI retired the free JEV tier on 2026-11-05; the anchor is now `gemini-decision-1`, in the
  same shadow state.
- Replay of 6 stored JEV anchor requests (201 `noul` questions) against Gemini Decision 1:
  same side of 0.5 as JEV on 166 (82.6%), none at opposite extremes; absolute difference median
  0.087, p90 0.363; 64% of Gemini answers at or beyond 0.2/0.8 versus 38% for JEV; 0.6 to 0.8 s
  per request; input tokens within about 3% of JEV's. This is agreement with JEV, not accuracy.
- Free-tier limits per project, one key per project (200 RPM, 50,000 TPM, 10,000 RPD) are Google's published values, not
  measured. Stored anchor requests are 19k to 41k tokens, so TPM allows about one request a minute
  per key.

## Deployment and first measurement (2026-11-12 to 2026-11-13)

Eleven pull requests merged with merge commits. Each one that changed the Worker or its compiled
config deployed `citypods-llm-dispatch-v2` through the deploy workflow; every deploy run succeeded.

| PR | Package | Merged (UTC) | Worker deploy |
|---|---|---|---|
| #2653 | WP5 hourly sweep | 2026-11-12 03:52 | none |
| #2654 | WP1a Worker logs | 2026-11-12 04:00 | yes |
| #2658 | WP3a stand-downs, WP3c diagnosis | 2026-11-12 08:12 | yes |
| #2655 | WP6a producer fixes | 2026-11-12 08:15 | none |
| #2657 | WP1b funnel telemetry | 2026-11-12 08:16 | none |
| #2656 | WP8 locator overflow to MiniMax | 2026-11-12 08:23 | yes |
| #2660 | WP2 per-route timeouts | 2026-11-13 01:03 | yes |
| #2661 | WP3b route health ladder | 2026-11-13 01:16 | yes |
| #2659 | WP9 Gemini Decision anchor | 2026-11-13 01:28 | yes |
| #2665 | WP6b Worker-cron producer trigger | 2026-11-13 01:45 | yes |
| #2666 | WP4 early slot release | 2026-11-13 02:13 | yes |

### Checks made from Workers Logs and run history

- **Stand-downs (WP3a).** 215 bundles between 2026-11-12 08:25Z and 13:12Z: no attempt on
  `beatapi_jev_1_13_free`, `beatapi_glm_5_3_flash_free` or `beatapi_secondary_glm_5_3_flash_free`
  (41 of the 200 claims before the deploy had named one of them).
- **Per-route timeouts (WP2).** The first AI Studio Gemma timeouts after the 01:03Z deploy ended at
  240 s with `response_timeout_ms: 240000`.
- **Locator overflow (WP8).** The 2026-11-12 11:26Z locator run submitted 95 jobs to MiniMax M3.1
  Flash and 14 to V4 Flash and held 2 packets as `locator-over-input-cap`. In the first 90 minutes
  MiniMax returned 6 successes and 8 HTTP 503 `upstream_capacity`; in the later window below it
  returned 100 successes and no failures.
- **Producer trigger (WP6b).** The owner set `GITHUB_DISPATCH_TOKEN` at about 01:48Z. The first
  trigger (deferred sweep, scheduled 01:52:48Z) returned HTTP 204 in 351 ms and its run started 4 s
  later. Through 06:03Z all 11 triggers returned 204 and every dispatched run succeeded: the sweep
  ran at 01:52, 02:52, 03:52, 04:52 and 05:52. Between 2026-11-12 03:52Z and 13:00Z, on GitHub's
  schedule alone, the hourly sweep had run once.
- **Gemini Decision anchor (WP9).** The 02:33Z judge run submitted 240 anchor packets to
  `gemini/gemini-decision-1` and 800 sibling packets to Nemotron 3.5 Super VL. Under the family
  independence rule it gave no anchor to 2,913 subjects (`no_independent_anchor`, Google-produced
  entries) and found no sibling for 740 (`no_independent_sibling`). The first call on each key
  returned HTTP 400 "User location is not supported for the API use." (classed
  `route_unavailable`, retried); the next 22 calls succeeded.

### Window 2026-11-13 02:15Z to 06:22Z (all packages deployed)

203 bundles and 441 provider attempts, from `bundle_completed` and `scheduled_claim`. The attempt
count is every call the Worker logged: 439 final attempts plus 2 in-window retries that were
superseded (both HTTP 503). Jobs requeued to a later bundle appear again as new attempts.

| Measure | 7 days to 2026-11-11 | This window | Target |
|---|---:|---:|---:|
| Slot productivity | 13% | 60% | ≥ 50% |
| Idle hold share | 58% | 23% | ≤ 10% |
| Failed attempts ÷ attempts | 45.5% | 10.0% (44) | < 20% |
| Response timeouts ÷ attempts | 7.7% | 5.2% (23) | < 3% |

- Early release: about half of the bundles made their one partial commit (102 of 202 at the first
  count) and 128 calls were released by a partial; no partial was refused for rows.
- Claims saw a mean of 5.4 calls in flight against 20 admissible and 2.7 active bundles, so the
  dispatcher was not slot-bound in this window.
- Rows: 14,785 billed rows by 06:22Z, a pace of about 55,700 a day.
- By route: MiniMax 100 of 100; Nemotron 3.5 Super (NVIDIA) 74 successes and 5 timeouts; V4 Flash
  (OrcaRouter) 56 of 57; GLM 5.3 Flash (OrcaRouter) 22 successes and 7 timeouts; AI Studio Gemma 2
  successes in 20 attempts across both keys.
- Ladder: GLM on OrcaRouter and Nemotron 3.5 Super on NVIDIA each entered the 60 s rung five or six
  times and were reset by a success each time; `gemma_4_31b_secondary` reached the 60-minute rung.

Limits of this reading: it is four hours, and the traffic mix differs from the baseline week. With
a Google anchor the sibling judge for most entries is now Nemotron 3.5 Super, so far fewer calls go
to AI Studio Gemma, which produced most of the baseline's failures. The idle-hold and timeout
targets are not met.

### JEV anchor job cancellation (2026-11-13 01:33:53Z, owner-approved)

The 240 `typesafe/jev-1.13` anchor jobs queued at the Worker could not drain after the stand-down.
Their job ids were read from the B2 pending index (`state/llm_deferred_index/pending/typesafe/jev-1.13/`,
one listing and 240 record reads) and sent to `/v2/jobs:cancel-batch`: 240 cancelled, 0 in flight,
0 not found. The B2 pointers and the judge ledger were not edited; the next judge run dropped the
pending JEV markers and planned those subjects for Gemini Decision 1.


## Lane submission caps (R2-D, 2026-11-13)

Read-only. Sources:

- Workers Logs `do_row_write_budget`, `bundle_completed` and `scheduled_claim`, 00:00–13:20Z;
- Cloudflare GraphQL `durableObjectsPeriodicGroups` (hourly `rowsWritten`);
- 62 `run_events` objects since 2026-11-11 (one listing);
- the cached Worker stats from 06:22Z;
- the 2026-11-10 catalog snapshot (`episodes_reduced.csv`, 32,173 rows).

The analysis is in review/58 section 12.4.

**Billed DO rows per UTC day (GraphQL).**

| Day | rowsWritten |
|---|---:|
| 2026-11-10 | 60,299 |
| 2026-11-11 | 28,940 |
| 2026-11-12 | 26,899 |
| 2026-11-13 (to ~13:20Z) | 27,019 |

**06:22Z check point.** The coordinator counter read 14,785. Logged deltas sum to 8,779. The
remaining 6,006 is a single unlogged jump at 03:47–03:48Z (#2673).

**Logged deltas by RPC.**

| RPC | rows to 06:22Z | rows to 13:19Z |
|---|---:|---:|
| claimDispatchWindow | 2,174 | 5,203 |
| enqueueBatch | 1,696 | 4,749 (+6,006 unlogged) |
| completeBatch | 1,771 | 3,989 |
| completePartial | 534 | 1,595 |
| purgePendingBatch | 558 | 1,558 |
| retireConsumed | 463 | 1,306 |
| confirmPurge | 570 | 1,101 |
| cancelBatch (240 JEV, one-off) | 703 | 703 |
| schemaRetry (deploys) | 209 | 308 |
| ackResults | 69 | 102 |
| recountQueuedJobs | 28 | 50 |
| authorizeRetry | 4 | 6 |

**Rows per successful call to 13:19Z.** There were 1,281 successful calls out of 1,459 final
attempts, in 653 bundles.

| Phase | Rows per successful call |
|---|---:|
| Claim | 4.06 |
| Completion, including partials | 4.36 |
| Retire / ack | 1.10 |
| Cleanup | 2.11 |
| **Total** | **11.6** |

Enqueue costs about 1.5 rows per write unit: about 6 rows per single-model job, ~9 for agenda,
~10.5 for the tagger and ~7.5 for moments.

**Route supply** (`bundle_completed`, 02:15Z + 9.14 h). Capacity is concurrency ÷ p50 latency,
bounded by the requests-per-day limit.

| Route | Lane | ok | fail | p50 s | capacity/day |
|---|---|---:|---:|---:|---:|
| nvidia nemotron-3.5-super | sibling | 254 | 8 | 120 | ~2,870 (shared NVIDIA 4) |
| nvidia minimax-m3.1-flash | locator | 203 | 0 | 42 | 1,440 (1 rpm) |
| orcarouter deepseek-v4-flash | locator | 142 | 2 | 139 | 800 |
| openrouter nemotron-3.5-super | sibling | 108 | 2 | 116 | 200 |
| kilo nemotron-3.5-super | sibling | 107 | 2 | 115 | 200 |
| gemini-3.1-flash-lite ×2 | agenda | 110 | 9 | 8–9 | 2 × 500 |
| orcarouter glm-5.3-flash | adjudicator | 28 | 7 | 401 | ~430 |
| nvidia gemma-4-31b | sibling | 21 | 13 | 231 | ~374 |
| AI Studio gemma-4-31b ×2 | sibling | 2 | 36 | n/a | n/a |
| gemini-decision-1 ×2 | anchor | 28 | 4 | 1 | 2 × 10,000 nominal (route_unavailable blocks, #2672) |
| nemotron-3-ultra (3 routes) | agenda | 20 | 10 | 249–303 | NVIDIA pool + 2 × 200 |
| nvidia deepseek-v4.1-flash | adjudicator | 1 | 0 | 598 | ~144 |

**Jobs submitted per day by lane** (sum of `queued` in run summaries), and backlog at the last run.

| Lane | 11-11 | 11-12 | 11-13 | last ready / queued |
|---|---:|---:|---:|---|
| chapter-agenda | 176 | 68 | 149 | n/a / 61 |
| chapter-locator | 743 | 527 | 182 | 1,812 / 230 |
| topic-tags:tagger | 251 | 338 | 2 | 10,810 / 1 |
| r6-moments | 78 | 5 | 0 | n/a / 14 |
| judge:anchor | 133 | 175 | 291 | 13,463 subjects |
| judge:sibling | 193 | 167 | 1,146 | 13,461 subjects |
| judge:adjudicator | 75 | 49 | 11 | (as above) |

**Arrivals** (catalog snapshot):

- Published per day: 4.7 (7 d), 6.2 (30 d), 6.0 (90 d), 7.2 (365 d).
- Weekly: median 42, p90 71, maximum 78.
- 40 sources had arrivals in 90 days.
- Last 365 days (2,616 episodes):
  - 64% have provider chapters;
  - of the 940 without, 23% have an agenda URL;
  - 69% have a transcript.
- Per episode: chapters median 5 / p90 20; judging subjects median 8 / p90 29 / mean 12.5; tag
  candidates median 7 / p90 32.
- The whole catalog holds 145,783 judging subjects across 11,120 episodes.
