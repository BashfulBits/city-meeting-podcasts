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


## Stale-recipe locator results (2026-11-13, review/58 §12.3)

Source: the 2026-11-10 local record snapshot (read-only) and the chapter-locator run logs
(`gh run view --log`). Non-provider episodes with a completed agenda and a transcript, by
`locator_status`: completed 2,396, pending 817, none 119. Of the 2,396 completed, 572 had current
labels and 1,824 stale ones.

| Stale record labels (model, prompt, routing) | Episodes |
| --- | ---: |
| Gemini 3.5 Flash Lite, `locator-v2`, `size-split-v1` | 1,815 |
| none, none, none | 8 |
| Kimi K3, `locator-baseline-v3`, `size-split-v1` | 1 |

| What the stored result shows | Episodes | Policy fingerprint | Accepted items | Marker date |
| --- | ---: | --- | --- | --- |
| Chapters from V4 Flash under `locator-baseline-v3` | 1,631 | v3, V4 Flash + Kimi K3 | > 0 | 2026-11-09 |
| No chapters | 144 | v3, V4 Flash + Kimi K3 | > 0 | 2026-11-09 |
| Chapters from V4.1 under `locator-baseline-v3` | 41 | v3, V4 Flash + Kimi K3 | > 0 | 11-09 (40), 11-11 (1) |
| Chapters from Gemini under `locator-v1` | 8 | none | 0 | 2026-08-27/28 |

- All 1,824: chapter hash equals `locator_recipe`; boundary key names that recipe.
- Completion markers: 1,814 stamped 2026-11-09; 1,802 matched the 11-10 input fingerprint.
- Canonical `ep.chapters` present on 709 (635, 39, 34 and 1 by the rows above).
- Meeting age: over five years 1,664; one to five years 104; 31 days to one year 56.

Locator runs after WP8 (#2656):

| Run start (UTC) | Stage ran / reused / deferred / errors | V4 Flash fresh / cached complete | MiniMax fresh |
| --- | --- | --- | ---: |
| 11-12 11:26 | 1,689 / 0 / 780 / 41 | 14 / 1,689 | 95 |
| 11-12 20:54 | 133 / 1,690 / 535 / 153 | 2 / 125 | 370 |
| 11-13 01:54 | 54 / 1,743 / 555 / 80 | 78 / 1 | 15 |
| 11-13 11:13 | 31 / 1,813 / 331 / 127 | 23 / 45 | 5 |

The 11-12 11:26Z errors were all "terminal retry limit or structural recovery gate blocks this
recipe". The logs do not name episodes, so the overlap between the 95 MiniMax submissions and the
41 V4.1-served results is not established.

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

**Jobs per day by lane** (sum of the `queued` job outcomes in run summaries), and backlog at the last run. Superseded as a sizing basis by the per-meeting derivation below: this count can include jobs carried over from earlier runs, and the locator's 11-11/12 days were replays of the stale-recipe episodes (§12.3).

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

**Per-meeting shares (revision, 365-day snapshot of 2,616 meetings).**

| Measure | Count | Share |
|---|---:|---:|
| No provider chapters, has an agenda URL | 212 | 8.1% |
| Agenda status set: 198 completed + 2 pending; the rest `not_applicable` 1,371, unset 1,045 | 200 | 7.6% |
| Locator status on those 200: 93 completed, 27 pending, 78 unset; `not_applicable` 1,371 elsewhere | 200 | 7.6% |
| Chapters > 0 | 1,593 | 61% |
| LLM tag candidates | 1,645 | 63% |
| Council or P&Z | 849 | 32.5% |
| Council or P&Z with a transcript | 556 | 21% |
| With judging subjects (16,136 subjects, mean 12.5) | 1,295 | 49.5% |

Daily arrivals over the last 90 days: median 5, p90 14, maximum 28, mean 6.0.

**Tagger batches per episode.** The 11-11 15:17Z and 11-12 01:28Z runs queued 251 + 174 = 425 jobs
for 194 + 166 = 360 dispatched episodes, or 1.18 per episode.

**Judge runs since 11-11** (run-event quality counts):

| Window | items submitted | judgments appended | contested | calibration | packets anchor / sibling / adjudicator |
|---|---:|---:|---:|---:|---|
| 11-11 02:54Z to 11-13 04:57Z (9 runs) | 53,074 | 7,064 | 2,444 | 189 | 587 / 1,163 / 130 |
| 11-13 07:15Z to 10:57Z (3 runs, Gemini anchor) | 8,973 | 8,376 | 56 | 6 | 12 / 343 / 5 |

On the 11-13 03:55Z re-plan, 33,462 items went into 1,040 packets (anchor 240, sibling 800), about
32 per packet. Since the anchor change, contested subjects are 56 / (8,376 / 2) = 1.3%.

**Backlogs at the last runs before 23:00Z.**

- agenda: 49–54 queued; errored flat at 51.
- locator: 200–222 queued; nothing "ready" since #2676 (19:26Z and 21:20Z runs).
- tagger: 10,810 ready.
- moments: 14 queued.
- judge planner at 22:59Z: 11,839 subjects ready to plan, 5,060 pending at the Worker, backlog
  16,899.

## Judge lanes: siblings, decision models, adjudicator (2026-11-13, review/58 12.6 to 12.8)

These are read-only production sources: Workers Logs `bundle_completed` events from 2026-11-12 04Z
to 11-13 11:40Z, AI Gateway logs for the 7 days to 11-13, and one judge-run log at 10:33Z.
Free-endpoint calls went only to OpenRouter decision endpoints that production does not route
through. No request or response body is committed.

- **Judge run 10:33Z.** `no_independent_anchor` 3,332 and `no_independent_sibling` 897.
  Outstanding sibling packets: Gemma 4 31B 142, Nemotron 3.5 Super 665.
- **Sibling calls by route (Workers Logs).**

  | Route | Succeeded | Notes |
  |---|---:|---|
  | `gemma_4_31b_primary` | 27 of 697 | |
  | `gemma_4_31b_secondary` | 28 of 665 | |
  | `nvidia_gemma_4_31b_it_free` | 21 of 35 | |
  | `openrouter_google_gemma_4_31b_it_free` | 0 of 2 | |
  | Nemotron 3.5 Super (NVIDIA, OpenRouter, Kilo) | 97–98% | p50 about 116 s |

- **Adjudicator calls by route (Workers Logs).**

  | Route | Succeeded | Notes |
  |---|---:|---|
  | OrcaRouter GLM 5.3 Flash | 71 of 97 | 25 timeouts, p50 479 s |
  | NVIDIA V4.1 Flash | 2 of 4 | p50 598 s; also 0 of 58 queued chapter-locator jobs |
  | BeatAPI V4.1 Flash | 1 of 1 | 92 s |
  | BeatAPI secondary GLM | 0 of 18 | HTTP 524 |

  The two Gemini Decision anchor routes succeeded on 14 of 16 calls each, about 0.8 s.
- **AI Gateway, 7 days.**

  | Route | Results | Latency |
  |---|---|---|
  | BeatAPI `deepseek-v4.1-flash-free` | 47 x 200, 2 x 524, 2 x 429, 1 x 503 | p50 95 s, p90 246 s |
  | BeatAPI `deepseek-v4-flash-0731-free` | 4 x 404 (retired upstream) | |
  | NVIDIA V4.1 | 5 x 200, 2 x 500 | |
  | OrcaRouter V4 Flash | 1,155 x 200 | |
  | OrcaRouter GLM | 592 x 200 | |

- **Production OpenRouter requests per day** (AI Gateway): 271 on 11-10, 53 on 11-11, 14 on 11-12,
  and 152 on 11-13 by 13:20Z.
- **Evaluation calls.**
  - 58 OpenRouter request attempts this round, all to free models:
    - Perplexity Decider V1.3 and Flash: 30 x 200 (smoke, plus a replay of 8 stored Gemini
      requests and 6 stored JEV requests against both models);
    - Mercury Decide: 21, from 11:24Z to 14:19Z, all 404 "No endpoints found";
    - Respan: 2 (400);
    - AI Gateway path checks: 5 (3 refused at the Cloudflare edge with 1010, 2 x 404).
  - Every replay call was accepted.
  - Response headers carried only `X-Generation-Id` and `X-Provider-Name`, with no rate-limit
    headers.

## Google location refusals and the anchor block (2026-11-13 to 11-14, review/58 12.10, #2672)

Sources: AI Gateway logs for provider `google-ai-studio`, 2026-11-13 00:00Z to 11-14 08:58Z
(636 requests, listed in 6-hour windows and deduplicated by log ID), with the response bodies of the
400s; Workers Logs `bundle_completed` and `route_health` for 2026-11-13.

**Google 400s by Gateway colo.**

| Gateway colo | Requests | HTTP 400 | Other statuses |
|---|---:|---:|---:|
| HKG | 19 | 19 | 0 |
| All other colos (27) | 617 | 0 | 617 |

All 19 bodies say "User location is not supported for the API use.". On `/v1beta/decisions` the
body is `{"error":{"message":…,"code":"invalid_request"}}`. On the chat path it is a one-element
array with `"status":"FAILED_PRECONDITION"`. The decisions 400s took about 15.5 s each, against
about 0.7 s for a 200.

**What the Worker did with them (2026-11-13).**

| Route kind | Refusals | Classified | Effect |
|---|---:|---|---|
| `gemini_decision_1_primary/_secondary` | 8 (4 times, both keys in the same second) | `route_unavailable` (`decisions-model-unavailable`) | Both routes blocked 6 h each time |
| Gemma 4 31B, Gemini 3.1 Flash Lite (chat) | 6 | `request_defect` | Job failed |

**Anchor activity windows (Decisions calls through the Gateway).**

| Window (UTC) | Calls | 200 | Ended by |
|---|---:|---:|---|
| 11-13 03:48 to 04:04 | 24 | 22 | HKG 400 at 04:04:06 |
| 11-13 10:05 to 10:11 | 8 | 6 | HKG 400 at 10:11:07 |
| 11-13 16:12 to 16:21 | 14 | 12 | HKG 400 at 16:21:08 |
| 11-13 22:22 to 23:16 | 64 | 62 | HKG 400 at 23:16:07 |
| 11-14 05:17 to 05:21 | 6 | 4 | HKG 400 at 05:21:07 |

Each window opens about 6 h after the previous refusal, when the block lapses. In about 33 h the
anchor made 106 successful calls, against a nominal 2 x 10,000 a day.

## Tag producer stall (2026-11-12 to 11-13, review/58 12.13, #2671)

Sources: tag workflow run logs (GitHub Actions), Workers Logs `claim_failed_oversize_jobs`
events, a read of 520 deferred-failure markers in B2 (list plus GETs, read-only), the 11-11 stats
cache, and the 2026-11-10 record snapshot (offline). No production writes.

**Tag runs.** All errors in both runs are the structural recovery gate's refusal.

| Run (start, UTC) | Stages visited | `completion-cache` skips | Queued | Errors | Global queue |
|---|---:|---:|---:|---:|---:|
| 11-12 16:50 (43263036985) | 255 | 19,196 | 175 | 54 | 19,451 |
| 11-13 01:05 (43342177719) | 232 | 19,196 | 19 | 204 | 19,428 |

**Worker oversize reaps on 11-12** (`claim_failed_oversize_jobs`, failed as `unadmissible`).

| Window (UTC) | Jobs failed |
|---|---:|
| 01:17 to 01:32 | 60 |
| 11:59 to 13:01 | 270 |
| 17:14 | 1 |
| Total | 331 |

**Deferred-failure markers** written 11-12 20Z to 11-13 02Z (520 read):

| Marker | Count |
|---|---:|
| Tag, `structural_blocked` / `unadmissible`, catalog digest `b9c6f49c` | 258 |
| ...of which name a job id from the reaper events above | 258 |
| ...of which are chapter-batch child recipes (`-tag-batch-`) | 68 |
| Tag, `exhausted` | 87 |

Estimated input of the 258 refused requests: 120,063 minimum, 174,145 median, 185,560 maximum.

**Route sizing for the tagger lane.**

| Route | Static ceiling | Static ratio | Largest raw input that fits | Learned (11-11 stats cache) |
|---|---:|---:|---:|---|
| Gemini 3.5 Flash Lite (primary, x2) | 218,875 | 2.15 | 101,802 (batch target 94,676) | accepted up to 357,487 |
| Nemotron 3.5 Super VL (x3) | 249,027 | 1.0 | 249,027 | accepted up to 203,402 to 223,042 |
| DeepSeek V4 Flash, OrcaRouter | none | 1.0 | unmeasured | prompt-cap estimate 69,855 |
| Gemini 3.1 Flash Lite (x2) | 218,875 | 1.6 | 136,796 | not read |

**`ready` versus visited** (2026-11-10 snapshot, episodes with served chapters, 19,438):

| Stored tag fingerprint | Episodes |
|---|---:|
| Matches the previous primary's route (Gemini 3.1 Flash Lite), recipe set | 10,681 |
| Matches the current primary's route (Gemini 3.5 Flash Lite), recipe set | 6,117 |
| None stored, no chapter tags | 1,101 |
| Other | 1,539 |

## Free decision models and sibling capacity (2026-11-14, review/58 12.9)

Sources: OpenRouter's public model and endpoint listings and the account's key endpoint (read
only), six tiny synthetic model calls with no transcript text, the records snapshot of 2026-11-10
for the producer mix, and the 00:33Z judge-run counts on 11-14. No request or response body is
committed.

- **Mercury Decide listings.** `endpoints: []` for `inception/mercury-decide:free`,
  `inception/mercury-decide` and `inception/mercury-decide-20260930:free` at 11-13 23:2xZ and
  11-14 09:2xZ; the model is in neither `/api/v1/models` listing.
- **Mercury Decide calls.**

  | Time (UTC) | Id | Path | HTTP |
  |---|---|---|---:|
  | 11-13 23:20:21 | `inception/mercury-decide:free` | `/api/alpha/decisions` | 404 |
  | 11-13 23:20:21 | `inception/mercury-decide:free` | `/api/v1/chat/completions` | 400 |
  | 11-13 23:20:21 | `inception/mercury-decide-20260930:free` | `/api/alpha/decisions` | 404 |
  | 11-13 23:20:21 | `inception/mercury-decide-20260930:free` | `/api/v1/chat/completions` | 400 |
  | 11-14 09:21:59 | `inception/mercury-decide:free` | `/api/alpha/decisions` | 404 |

  Bodies: 404 "No endpoints found for inception/mercury-decide:free."; 400 "<id> is a decisions
  model and cannot be used with the chat/completions endpoint. Use the /api/alpha/decisions
  endpoint instead."
- **Path check.** `https://openrouter.ai/api/v1/decisions` with the Decider Flash id: 404.
- **Perplexity Decider V1.3 endpoint (11-14).** One endpoint, Perplexity; prompt and completion
  price 0; context 262,144.
- **Account.** `free_model_daily_requests` 179 of 1,000 used at 09:2xZ on 11-14 (266 of 1,000 at
  23:2xZ on 11-13). The configured OpenRouter routes total 440 rpd.
- **Producer mix.** 168,651 judge subjects in 17,841 episodes: rule 158,783 (94.1%), google 7,383
  (4.4%; Gemini 3.1 Flash Lite 6,596, Gemini 3.5 Flash Lite 787), nvidia 1,421 (0.8%, all Nemotron
  3.5 Super on OpenRouter), deepseek 1,064 (0.6%). Published within 30 days: 1,238 (rule 1,163,
  google 60, deepseek 11, nvidia 4). Within 90 days: 3,646 (rule 3,473, google 129, nvidia 32,
  deepseek 12). No subject had an unknown producer family.
- **Judge run 11-14 00:33Z** (first after #2678): 78 sibling packets to Nemotron 3.5 Super, 36 to
  Qwen3.8; `no_independent_anchor` 3,191.

## Slow route families and the 780 s ceiling (R2-F, 2026-11-13 to 11-14, review/58 §12.5)

Sources: AI Gateway logs since 2026-10-01 (per-call duration and usage), Workers Logs
`bundle_completed` and `attempt_outcome` since 2026-11-12, 80 sampled request bodies and 6 timed-out
request payloads read from B2 (about 75 GETs, no writes), and 44 direct calls on paused routes.
Request and response bodies are not committed.

**T1. AI Gateway successes since 2026-10-01** (duration is the provider call).

| Family | Successes | p50 / p90 / p99 / max (s) | Notes |
|---|---:|---|---|
| GLM 5.3 Flash, OrcaRouter | 3,096 (plus 85 × 429) | 220 / 441 / 662 / 719 | 419 over 400 s; 66 in 600 to 720 s |
| Nemotron 3 Ultra, NVIDIA | 327 | 112 / 475 / 655 / 703 | |
| Nemotron 3 Ultra, Kilo | 195 | 137 / 453 / 590 / 591 | |
| Nemotron 3 Ultra, OpenRouter | 198 (plus 21 × 499) | 167 / 459 / 676 / 718 | |
| Gemma 4 31B, NVIDIA | 2,136 | 142 / 475 / 693 / 718 | 309 over 400 s |

**T2. Where the time goes.**

| Family | Output tokens p50 | Reasoning share of output (p50) | Tokens/s p50 | corr(duration, output) | corr(duration, input) |
|---|---:|---:|---:|---:|---:|
| GLM | 16,384 (p90 33.6k) | 92% | 81 | 0.81 | 0.36 |
| Ultra, Kilo and OpenRouter | about 1.0k to 2.3k visible | 73 to 76% | about 55 | 0.70 to 0.88 | low |
| Gemma, NVIDIA | 1,276 | 0 (no reasoning) | 8.9 (down to about 1 on slow calls) | 0.15 | — |

786 GLM outputs at exactly 16,384 tokens (2026-10-24 to 11-03) reflect an older `max_tokens` cap.

**T3. Request bodies** (80 sampled, half over 400 s).

- GLM, `judge:adjudicator` (36): all `finish_reason: stop`, `max_tokens` 65,536, `json_object`.
  Content about 1.7k tokens against 10k to 56k reasoning tokens; the 600 to 706 s calls had 30k to
  56k reasoning tokens.
- GLM, `r6-moments` (4): 21k to 58k reasoning tokens.
- Ultra, `chapter-agenda` (all three legs): all `stop`, `max_tokens` 65,536, `json_schema`;
  reasoning 2k to 34k tokens.

**T4. Workers Logs attempt outcomes since 2026-11-12 04:00Z.**

| Route | Lane | Outcomes |
|---|---|---|
| GLM, OrcaRouter | judge:adjudicator | 71 ok, 25 timeouts at 720 s, 1 own_rpd; ok p50 479 s, p90 613 s; 12 ok over 600 s |
| Ultra, NVIDIA | chapter-agenda | 10 ok, 11 timeouts |
| Ultra, Kilo | chapter-agenda | 10 ok, 3 timeouts, 1 `structured_output_empty` |
| Ultra, OpenRouter | chapter-agenda | 13 ok |
| Gemma, NVIDIA | judge:sibling | 21 ok, 13 timeouts, 1 upstream_capacity |

**T5. Timing margins** (588 bundles since 2026-11-13 02:15Z, with early release and the ladder
live).

- Invocation start to the claim: at most 10.7 s.
- Invocation start to `bundle_completed`: p50 151 s, p99 722.8 s, max 733.0 s.
- Last call end to the final commit: p50 0.73 s, p99 1.72 s. One commit took 125.5 s because a B2
  result PUT had no timeout (`b2.putJson`); one took 6.3 s.

**Direct-call experiment** (plan fixed at 2026-11-13 18:20Z, before the first call). The same
fixed samples ran in every arm, with a direct-call timeout of 840 s so that the 720 to 780 s tail
would show. Arms: A0 production request; A1 lowest documented reasoning control; A2 `max_tokens`
16,384; A3 a "reason briefly, under about 4,000 tokens" sentence appended to the system message.

Ultra on NVIDIA (`chapter-agenda`, 10 samples: 8 production successes of 467 to 668 s and 2 jobs
that timed out at 720 s; validity under `finalize_agenda_job`):

| Arm | Finished | Valid | Timeouts at 840 s | Notes |
|---|---:|---:|---:|---|
| A0 control | 1 (273 s) | 1 | 9 | |
| A1 `chat_template_kwargs.enable_thinking: false` | 5 (118 to 542 s) | 2 | 5 | 0 reasoning tokens on every finished call |
| A2 `max_tokens` 16,384 | 5 (102 to 608 s) | 0 | 5 | 3 ended `length` with truncated JSON |
| A3 length sentence | 2 (263, 296 s) | 1 | 8 | reasoning 6.4k and 10.7k tokens, above the 4k asked |

- The first five samples timed out in every arm, including A1 with no reasoning. That points to
  provider queueing on those requests, not generation length.
- No call in any arm finished between 720 and 840 s.
- On the one request that A0 and A1 both finished, A1 was not faster (295 s against 273 s).
- A1 recall against the production answers for the same requests: line coverage 0.86, 0.84 and
  0.44, with fewer agenda items (25 against 30, 33 against 40). On one timed-out payload A1
  returned 11 items.
- Of the 8 production answers in the sample, 3 pass today's validator. Ultra corrects OCR text
  (for example "ltems" to "Items"), and the corrected text fails the quote check.
- A4 (combination) was skipped by the plan's rule: A3 did not cut latency and A2 truncated.

GLM on OrcaRouter: no usable data. The 4 calls on 2026-11-13 each returned 429 "Free model
capacity is limited" after about 120 s, and the run stopped. On 2026-11-14 the OrcaRouter daily
pool was spent, so no further GLM call was made. The Kilo and OpenRouter `reasoning_effort` checks
were not run. Gemma was not tested: it serves no lane after #2678, and no request lever applies to
provider throughput.

**Pause log** (every pause, renewal and resume made by this package, plus the status checks).
Only single routes were paused; no provider-wide or global pause was made.

| Time (UTC) | Action | Target | Reason | Measured effect |
|---|---|---|---|---|
| 11-13 11:22, 13:19 | status | orcarouter, nvidia, kilo, openrouter | before planning | no pauses |
| 11-13 13:28 | status | route GLM, OrcaRouter | before planning | no pauses; 1 in flight |
| 11-13 18:12 to 19:00 | status (8 checks) | provider:nvidia, then provider:openrouter | — | Not mine: "provider catalog reconciliation" pauses from the catalog workflow, renewed about every 3 minutes until about 19:00. Not touched; no pause started while they were active |
| 11-13 23:19:14 | status | all | before the run | no pauses |
| 11-13 23:20:34 | pause, 900 s | route `orcarouter_zai_glm_5_3_flash_free` | GLM arms | drained at once, 0 in flight |
| 11-13 23:25:01 | resume (manual) | route `orcarouter_zai_glm_5_3_flash_free` | every call returned 429 | paused 4.5 minutes |
| 11-13 23:25:26 | pause, 900 s | route `nvidia_nemotron_3_ultra_550b_a55b_free` | Ultra arms | drained at once, 0 in flight |
| 11-13 23:30 to 11-14 01:20 | renew, 900 s, every 300 s (23 times) | same route | calls still running | — |
| 11-14 01:21:28 | resume | same route | arms done | resumed before expiry; paused 1 h 56 min |
| 11-14 19:28:50 | status | all | before finishing | no pauses |
| 11-14 19:40:20 | status | all | before the PR | no pauses; 3 in flight (production) |

While the Ultra route was paused, `chapter-agenda` kept its Kilo and OpenRouter legs. Every direct
call was reserved against its route first. No pause made by this package is outstanding.

## Slow reasoning routes: GLM effort, Ultra reasoning off, Kilo and OpenRouter (#2689, 2026-11-15)

Run early in the UTC day (01:32 to 02:25Z), when the OrcaRouter pool had room (764 of 800 requests
left). Every call was a direct call on its own paused route, reserved first, with a response
timeout of 840 s. Prices were checked first: all four routes are `free: true` with zero input and
output prices. Request and response bodies are not committed. Plan fixed before the first call:
GLM 10 requests × 2 arms (at most 20 calls), Ultra 10 new requests × 1 arm (at most 10), Kilo and
OpenRouter 2 requests per route per form (at most 8).

**Calls made:** OrcaRouter GLM 20, NVIDIA Ultra 10, Kilo 2, OpenRouter 2. Total 34 of the 38 to
48 allowed (the fallback `reasoning: {effort: "none"}` form was not needed, so 4 reserved calls
were not made). No call returned 429.

### Item 1. GLM 5.3 Flash, `reasoning_effort: low` (the 10-request R2-F sample)

The sample: 6 production successes of 236 to 706 s and 4 adjudicator jobs that timed out at 720 s.
A0 is the production request; A1 adds `reasoning_effort: "low"` and changes nothing else.

| Arm | Finished | Valid | Latency p50 (s) | Latency max (s) | Reasoning tokens p50 | Output tokens p50 |
|---|---:|---:|---:|---:|---:|---:|
| A0 as today | 10 of 10 | 9 | 411 | 838 | 27,624 | 29,183 |
| A1 `low` | 10 of 10 | 9 | 37 | 86 | 1,656 | 3,111 |

- Every call ended `stop`. Reasoning fell about 17-fold and latency about 11-fold. A0 had 2 calls
  over the 780 s ceiling (781 s and 838 s); A1's slowest was 86 s.
- The one invalid reply in each arm was a different request: A0 answered 19 of 20 ids, A1
  answered the 20 ids plus one unrequested id. Both are validator rejections, not parse failures.
- Verdict agreement of A1 with A0, on the 8 requests where both arms were valid: 140 of 160 items
  (87.5%), per request 16 to 19 of 20. All 160 items are `validate` kind.
- Noise floor: for the 5 sample requests that have a stored production answer, production against
  A0 agrees on 88 of 100 items, and production against A1 on 89 of 100. A1 is as close to
  production as A0 is.
- Direction of the 20 A0-to-A1 flips: 16 items went from "not supported" to "supported" and 4
  the other way. A1 marks 59 of 160 items supported against 47 for A0 (production on its subset:
  33 of 100). `low` leans lenient by about 7 points (sign test on the flips, p about 0.01), which
  matters for a lane that is told to try to disprove the claim. A0 itself leans strict against
  production (9 flips to "not supported", 3 the other way).
- Verdict: the effort control works and the agreement holds at the noise floor. The lenient
  lean is the thing to watch after deploy.

### Item 2. Nemotron 3 Ultra on NVIDIA, `chat_template_kwargs.enable_thinking: false`, chapter-agenda

Ten new `chapter-agenda` requests, never used before: NVIDIA production successes of 111 to 491 s
with a stored production answer. One arm (reasoning off), timeout 840 s, scored against the
production answer for the same request. Recall is the share of the production answer's source
lines that the new answer's items also cover (items' `line_start` to `line_end`); the item ratio
is new items over production items.

| Sample | Latency (s) | Reasoning tokens | Production items | New items | Item ratio | Line recall | Valid (new) | Valid (production) |
|---|---:|---:|---:|---:|---:|---:|---|---|
| gw:01a1c3c5 | 32 | 0 | 16 | 11 | 0.69 | 1.00 | no | no |
| gw:01a1c2dc | 49 | 0 | 32 | 19 | 0.59 | 1.00 | no | yes |
| gw:01a1c64c | 16 | 0 | 13 | 9 | 0.69 | 0.88 | no | yes |
| gw:01a1c94d | 89 | 0 | 36 | 35 | 0.97 | 0.96 | no | no |
| gw:01a1c413 | 127 | 0 | 41 | 81 | 1.98 | 0.66 | no | yes |
| gw:01a1cc97 | 89 | 0 | 27 | 26 | 0.96 | 1.00 | yes | no |
| gw:01a1cfbf | 32 | 0 | 12 | 18 | 1.50 | 0.50 | no | no |
| gw:01a1cca0 | 103 | 0 | 19 | 15 | 0.79 | 1.00 | yes | no |
| gw:01a1cfc8 | 45 | 0 | 17 | 15 | 0.88 | 1.00 | no | no |
| gw:01a1cfd9 | 37 | 0 | 18 | 16 | 0.89 | 0.80 | no | no |

- All 10 finished (16 to 127 s, p50 41 s), with 0 reasoning tokens. The 2026-11-13 run finished 5
  of 10 with the same switch; there the first five samples timed out in every arm.
- Line recall: mean 0.88, median 0.98; 6 of 10 at 0.96 or above, 4 below 0.9 (0.88, 0.80, 0.66,
  0.50). Item count: 231 production items against 245 new, but per request the new answer has
  fewer items in 7 of 10 (ratio 0.59 to 0.89) and more in 2 (1.50 and 1.98).
- Validity under today's `finalize_agenda_job`: new 2 of 10, production 3 of 10. The failures are
  the quote check on OCR-corrected text (6 new, 7 production), one over-wide line range and one
  out-of-range line. The validator, not reasoning, rejects most of both arms (#2689 item 2's
  separate finding).
- Not enabled in any lane. The owner decides.

### Item 3. Kilo and OpenRouter Ultra legs, `reasoning_effort: "none"`

The two smallest requests of the item 2 sample (17.6k and 22.8k characters), shaped for each
route, one call each.

| Route | Request | Latency (s) | Reasoning tokens | Output tokens | Finish |
|---|---|---:|---:|---:|---|
| `kilo_nvidia_nemotron_3_ultra_550b_a55b_free` | gw:01a1c3c5 | 5.4 | 0 | 588 | stop |
| `kilo_nvidia_nemotron_3_ultra_550b_a55b_free` | gw:01a1c64c | 14.3 | 0 | 1,412 | stop |
| `openrouter_nvidia_nemotron_3_ultra_550b_a55b_free` | gw:01a1c3c5 | 4.8 | 0 | 588 | stop |
| `openrouter_nvidia_nemotron_3_ultra_550b_a55b_free` | gw:01a1c64c | 14.5 | 0 | 1,377 | stop |

- `reasoning_effort: "none"` takes effect on both legs: 0 reasoning tokens and no reasoning text,
  where production on these legs reasons for 2k to 34k tokens and takes 100 s or more. The
  `reasoning: {effort: "none"}` fallback was not needed and not called.
- Both answers failed `finalize_agenda_job` on the quote check, as production answers do.
  Declared as `reasoning_controls.off`; no lane uses it.

### Pause log (2026-11-15, every pause, renewal and resume made by this run)

Only single routes were paused; no provider-wide or global pause was made. Each pause was
requested for 900 s and renewed every 300 s while calls ran (the GLM run renewed 9 times).

| Time (UTC) | Action | Target | Reason | Measured effect |
|---|---|---|---|---|
| 01:31:49 | status | all | before the run | no pauses |
| 01:32:34 | pause, 900 s | route `orcarouter_zai_glm_5_3_flash_free` | item 1, 20 calls | drained in 51 s (2 production calls in flight at the start) |
| 01:33 to 02:18 | renew, 900 s (9 times) | same route | calls running | none |
| 02:18:55 | resume | same route | item 1 done | paused 46 min; 20 calls made |
| 02:19:52 | status | provider nvidia, Ultra route | before item 2 | no pauses; 3 in flight on the provider |
| 02:19:54 | pause, 900 s | route `nvidia_nemotron_3_ultra_550b_a55b_free` | item 2, 10 calls | drained at once, 0 in flight |
| 02:23:38 | resume | same route | item 2 done | paused 3.7 min; 10 calls made |
| 02:24:33 | pause, 900 s | route `kilo_nvidia_nemotron_3_ultra_550b_a55b_free` | item 3, 2 calls | drained at once |
| 02:24:54 | resume | same route | item 3 done | paused 21 s; 2 calls made |
| 02:25:06 | pause, 900 s | route `openrouter_nvidia_nemotron_3_ultra_550b_a55b_free` | item 3, 2 calls | drained at once |
| 02:25:28 | resume | same route | item 3 done | paused 22 s; 2 calls made |
| 02:34:02 to 02:34:07 | status | the four routes | before finishing | no pauses on any of them; 2 production calls in flight on GLM |

While one Ultra leg was paused, `chapter-agenda` kept its other two legs. No pause made by this
run is outstanding.
