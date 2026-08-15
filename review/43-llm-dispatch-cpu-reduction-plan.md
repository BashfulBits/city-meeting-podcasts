# LLM dispatch Worker CPU-reduction plan

Status: **planned; Steps 1–3 complete; Step 4 measurement gate**  
Owner: dispatch Worker maintainers  
Scope: `workers/llm-dispatch-proxy`

## Objective

Reduce scheduled-invocation CPU usage enough to restore a durable Free-plan safety margin
below the 10 ms cron limit, while preserving:

- cross-provider/model candidate bypass when one route is throttled;
- durable rate accounting and crash recovery;
- the ability to experiment with batch concurrency later; and
- the existing private-R2 request and result lifecycle.

The Worker’s detailed timing fields are wall-clock measurements. They are useful for finding
I/O overlap and slow phases, but they are not CPU attribution. Production invocation `cpuTime`
and repeatable local CPU profiles are the acceptance signals.

## Current findings

The original backlog-scale problem has been addressed: ready markers and bounded metadata
lookahead prevent the scheduler from scanning and parsing every queued request. The remaining
likely CPU costs are repeated JSON parse/stringify operations, request-payload cloning,
coordination-state churn, route/pricing calculations, and detailed profile/log object creation.

R2/network wait is not itself charged as Worker CPU. Prefetching and parallelizing R2 reads may
reduce wall-clock duration, but should not be treated as the primary CPU optimization.

Current production safety setting is `BATCH_CONCURRENCY=1` and `MAX_TOTAL_REQUESTS=1`. The
concurrent reservation path must remain available behind configuration for later experiments.

## Five-step execution plan

### Step 1 — Baseline and benchmark fixture

Status: **complete**

Create a repeatable, test-only benchmark using representative transcript-sized request records,
provider success/failure responses, and a fake R2/provider boundary. Exercise the scheduled
dispatch path over enough iterations to make function-level hotspots visible. Use the deployed
Worker’s version-tagged `cpuTime` distribution as the production acceptance metric.

Record before/after:

- CPU P50/P90/P99 and any `exceededCpu` outcomes;
- request-record and response sizes;
- route-selection, ledger, and persistence wall-time profiles;
- number of R2 operations and JSON parse/stringify boundaries.

Do not use a one-to-three-sample `wrangler check startup` result as the scheduled-path baseline.

### Step 2 — Low-risk CPU reductions

Status: **complete**

Implement and measure as one bounded change set:

1. Test `minify: true` in Wrangler and compare deployed CPU by version.
2. Move cron-lock owner/expiry data to R2 custom metadata, using metadata-only `head()` and
   ETag-conditional writes while preserving the existing lease semantics.
3. Cache `Intl.DateTimeFormat` instances by timezone and precompute static pricing-period data
   where possible, retaining DST and pricing-window tests.
4. Keep complete profiles for failures and sampled successes, while retaining lightweight
   aggregate dispatch events for every invocation.

Acceptance gate: measurable CPU improvement, no correctness regression, and no repeated
`exceededCpu` outcomes during a representative production canary.

### Step 3 — Serial ledger fast path

Status: **complete; correctness review passed**

For the exact `1/1` configuration, use the global cron lease as the serialization guarantee and
avoid creating/removing an `inflight` reservation around a single request. Commit durable rate
usage before the upstream call and retain the existing reservation path for concurrency greater
than one.

Before implementation:

- audit every writer of `state/dispatch_budget.json`;
- prove crash, timeout, retry, and final-record-write-failure behavior;
- add tests showing that provider attempts cannot double-spend or lose rate budget; and
- keep the path feature-flagged for rollback.

Expected benefit: remove a budget read/write and its associated JSON work from the common 1/1
execution.

### Step 4 — Measure before considering a control/payload split

Status: **blocked on measurement; no migration approved**

The ready-marker custom metadata already separates queue selection from large prompt reads. Do not
assume that a second canonical-record split is justified. First measure the current production
shape with representative canonical record sizes and version-tagged invocations.

Required evidence before changing the R2 record layout:

- at least 100 production invocations on one deployed version with `cpuTime` P50/P90/P99;
- a comparison across small, typical, and large canonical request records;
- local CPU-profile/benchmark runs with the same record-size distribution; and
- a measured CPU reduction target that the proposed split can plausibly achieve.

### Production measurement checkpoint — 2026-08-14

The one-hour Workers Logs export contained eight complete invocation rows (plus eight duplicate
metadata/source rows). All eight ran version `c9e76f6a-790e-4204-b597-f5f5dfdc881f`:

- CPU: `7, 7, 7, 7, 8, 8, 8, 8` ms; average `7.625` ms; P50/P90 `8` ms;
- wall time: `9.753`–`16.618` seconds; average `11.891` seconds; and
- all outcomes were `ok`.

Three live tail samples from that same version were CPU `7`, `7`, and `8` ms. Their wall-time
profiles varied substantially while CPU stayed in the same band. Two successful canonical records
were approximately `13.1 KB` and `15.4 KB` for their request JSON; a live Gemma 400 record was
`62.7 KB`. Those three records were also within the same `7`–`8` ms CPU band, so this sample does
not yet establish a production CPU/record-size slope.

The local dispatch-only CPU profile is more sensitive to payload size, but it is comparative only:

- 250 KB, 1,000 runs: CPU P50 `0.336` ms, P95 `0.774` ms;
- 1 MB, 500 runs: CPU P50 `1.106` ms, P95 `1.859` ms; and
- dense profile samples identify `JSON` parsing and `putJson` serialization as the dominant
  sampled functions, with garbage collection secondary.

Conclusion: canonical JSON work is a credible local hotspot, but the production sample is too
small and too narrow in record size to justify a data-layout migration yet. Continue collecting
version-tagged production samples and larger-record correlations before Step 4.

### Filtered live-tail checkpoint — version `5acd24e2-115b-4ea4-a2dc-8697100acb6e`

The attached five-invocation tail was filtered to `llm_dispatch_batch`, so it excludes scheduled
invocations that produced no dispatch. All five invocations completed with outcome `ok`, count `1`,
and the same deployed version. CPU was `9, 10, 10, 10, 12` ms: average `10.2` ms, P50 `10` ms,
P90 `12` ms, and maximum `12` ms. Wall time was `5.187`–`9.714` seconds, average `6.391` seconds.

Four samples were Gemma 400 failures and one was a Gemini success. For the four detailed Gemma
profiles, the batch phases were:

- `ready_heads_ms`: `188`–`206`;
- `budget_load_ms`: `358`–`388`;
- `candidate_prepare_ms`: `324`–`919`;
- `ledger_write_ms`: `560`–`590`; and
- `reservation_release_ms`: `0` in every sample, confirming the serial-ledger fast path.

The four detailed result profiles had `claim_ms` `324`–`919`, upstream time `582`–`1,145`,
canonical persistence `805`–`841`, and total result time `2.347`–`2.991` seconds. The one
`candidate_prepare_ms=919` sample is a wall-time outlier, not an explanation for the overall CPU
distribution. Because the successful sample was not profile-sampled, these logs do not provide a
full phase profile for the Gemini case.

This is a cleaner measurement than the earlier unfiltered dashboard export, but it is still not a
complete version comparison: the route/outcome mix is four Gemma failures and one Gemini success.
The Worker was subsequently rolled back to `c9e76f6a-790e-4204-b597-f5f5dfdc881f`; these five
samples are historical observations from `5acd24e2`, not the currently deployed version.

### Filtered rollback comparison — version `c9e76f6a-790e-4204-b597-f5f5dfdc881f`

The matching five-invocation tail from the rollback also contained only dispatched invocations and
all five completed with outcome `ok`. CPU was `10, 10, 11, 11, 13` ms: average `11.0` ms, P50
`11` ms, P90 `13` ms, and maximum `13` ms. Wall time was `5.908`–`15.168` seconds, average
`9.126` seconds.

The route mix was two Gemini successes and three Gemma 400 failures. Comparing only the Gemma
failures gives candidate `5acd24e2` CPU `9, 10, 10, 12` ms (average `10.25`) versus rollback
`c9e76f6a` CPU `10, 10, 11` ms (average `10.33`). That is directionally lower for the candidate,
but the sample is too small to treat the difference as significant. Gemini is also inconclusive:
the candidate has one unprofiled `10` ms success, while rollback has two successes at `11` and
`13` ms.

The clearest implementation difference is visible in the wall-time profiles. The candidate's
serial-ledger path reports `reservation_release_ms: 0`; the rollback reports `845`–`1,219` ms for
the three Gemma failures and `1,044`–`1,155` ms for Gemini. The rollback therefore still pays the
reservation cleanup step, but that wall-time saving does not translate into a large CPU reduction
in this sample. The candidate is not demonstrably worse than the rollback, yet neither version
has a safe CPU margin below 10 ms. Keep the rollback deployed while gathering a larger, route-
balanced sample before making another code or record-layout change.

### Current-worktree deployment checkpoint — version `5acd24e2-115b-4ea4-a2dc-8697100acb6e`

The current worktree was deployed on 2026-08-14 with `BATCH_CONCURRENCY=1`,
`MAX_TOTAL_REQUESTS=1`, minification enabled, and the serial ledger path active. Four live
scheduled invocations from that version produced CPU values of `17`, `10`, `12`, and `12` ms;
all reported `ok`, but the latter three are at or above the Free-plan 10 ms limit and the first
exceeded it. The first 58.3 KB Gemma request used 17 ms; a later 13.8 KB Gemma request used 12 ms,
so the initial sample does not indicate payload size is the sole cause.

The serial path is confirmed active in the profiles (`reservation_release_ms: 0`). The candidate
therefore needs a safety review/rollback decision before it remains deployed; Step 4 remains
unapproved because the production evidence currently points to a broader implementation or
runtime regression, not a proven canonical-payload-size problem.

The local benchmark already shows a size trend (50 KB success P50 0.196 ms versus 250 KB success
P50 0.570 ms before Step 3), but those values are Node measurements and are not production CPU
attribution.

Only if canonical parse/stringify work is shown to be material should we split the canonical record
into a small control record and separate request/result data. The scheduler would then parse
scheduling state, not repeatedly deserialize and reserialize the entire transcript prompt and
accumulated result.

The route-specific upstream model substitution requires a designed payload-template or equivalent
trusted transformation. Do not use unvalidated string replacement. Roll out with dual-read/new-
write compatibility, recovery tooling, privacy review, and realistic large-record benchmarks.

### Step 5 — Revisit throughput architecture

Status: **pending; only after CPU margin is restored**

Do not add same-minute cron triggers as a CPU solution: cron is minute-resolution, Free accounts
have a small trigger limit, and the current global lease would serialize same-minute invocations.

If higher throughput is still needed after CPU optimization, evaluate:

- Queues for ready-job delivery and polling removal, subject to Free operation/day and retention
  limits;
- D1 for small transactional quota/lease state while keeping large bodies in R2; or
- a short-lived Durable Object coordinator for admission only, never while waiting on an LLM.

Each option needs a throughput, cost, failure-recovery, and provider-rate-limit review. None
increases the Free Worker invocation CPU limit by itself.

## Operation-shape analysis (2026-08-14)

This section revises the framing in **Current findings** above. That framing is half right: R2 *wait*
is not charged as Worker CPU, but the per-call plumbing and the byte copying on each R2 operation
are. The conclusion drawn from it -- that parallelizing R2 "should not be treated as the primary CPU
optimization" -- pointed the work at JSON micro-optimization and a record-layout migration, and away
from the cheapest available reduction: **the number of R2 operations an invocation performs**.

### The measurement that reframes it

`bench/cpu-profile.js` runs the real scheduled handler against a ledger seeded with every configured
route and a 16-marker ready backlog, and reports V8 self-time per function alongside every R2
operation and byte crossing the binding.

The Worker's own JavaScript costs **~0.4 ms** per dispatch. Production reports **9-13 ms**. Roughly
**95% of production CPU is therefore not the dispatch logic** -- it is work at the runtime boundary,
which scales with operations and bytes rather than with algorithmic cost. This is the single most
important number for prioritizing the remaining steps, and it argues against Step 4 being the next
move.

Baseline operation shape on `main`, per invocation:

| Invocation | R2 operations | Bytes moved | `JSON.parse` |
| --- | --- | --- | --- |
| Idle tick (no ready work) | 7 | 0.8 KB | -- |
| Dispatching one 59 KB request | 14 | 181 KB | 22 calls / 92 KB |

Six of the fourteen were the cron lease alone, including a renewal issued microseconds after
acquiring an 840-second lease and a release that re-read a lock this invocation had just written.

### Confirmed hotspots

1. **Operation count and byte volume (dominant).** As above.
2. **`Intl.DateTimeFormat` construction (secondary, and the likely source of the CPU outliers).**
   `zonedDateKey` was the largest self-time function in the scheduled path at ~25%. Construction
   measures **43 us** against **2 us** to reuse a cached instance. It is called per rate-limited
   route during selection, and `nextLocalMidnightUTC` runs a 17-iteration binary search that
   constructs one per iteration -- a latent ~0.7 ms spike whenever a free route's RPD is exhausted.
3. **Module startup: ruled out.** Parsing the 77 KB `dispatch_limits.json` measures 0.127 ms.

### Changes made

Applied on top of this branch's existing work, which they compose with rather than replace:

- **ETag-carrying cron lease.** `acquireCronLease` hands back the ETag it wrote; renew and release
  CAS onto it instead of re-reading the lock. The CAS is unchanged, so a lease taken over by another
  invocation still fails the write rather than being overwritten.
- **No first-pass renewal.** The lease is held for 840 s against an 820 s run deadline, so it cannot
  be taken over before the run ends. Renewal now fires only once a run passes half the lease.
- **ETag-carrying batch reservation release.** This branch's batched release keeps its single CAS
  for the whole batch and now skips the read that preceded it.
- **Cached `Intl.DateTimeFormat`,** including negative results, bounded by a cache cap.
- **Lazy ready-marker `policy` parsing.** The lookahead still lists 16 markers so a throttled route
  cannot stall the queue head, but a run that dispatches the first marker no longer parses the other
  fifteen. Repair of an unreadable policy moves to the point of use.
- **Shared `TextDecoder`** and no buffer copy for single-chunk bodies.

### Defect found while measuring

The no-candidate ledger write compared `JSON.stringify(budget)` against a second stringify of **the
same object** -- `budget` *is* `budgetLoaded.value`, so the two strings always matched. The branch
could never fire, while still paying for two whole-ledger serializations on every no-capacity
invocation. It now writes when an abandoned reservation was actually reaped; minute/day window
rollover is recomputed from `now` on every load and never needed persisting.

### Result

| Invocation | Before | After |
| --- | --- | --- |
| Idle tick | 7 ops / 0.8 KB | **4 ops / 0.3 KB** |
| Dispatching one 59 KB request | 14 ops / 181 KB | **10 ops / 147 KB** (9 in steady state) |
| `JSON.parse` per dispatch | 22 calls / 92 KB | **4 calls / 74 KB** |

Local V8 CPU per dispatch: P50 `0.395` -> `0.229` ms, P90 `0.722` -> `0.435` ms. These are Node
measurements and remain comparative only.

`test/index.test.js` pins the operation counts, so a change that reintroduces a round trip fails
there rather than in production.

### Revised recommendation for Step 4

Step 4 remains unapproved, and this analysis lowers rather than raises its priority. The completed
canonical write is 60 KB of the remaining 147 KB, but the request must still be *read* in full to
build the upstream payload, so a control/result split saves the write and not the read. Since the
gap between measured JavaScript cost and reported production CPU points at operation count rather
than payload size, exhaust operation reduction and gather a route-balanced production sample before
committing to a record-layout migration.

## Decision gates

- Stop and ask for confirmation before changing rate-ledger semantics or record layout.
- Preserve the concurrent reservation implementation until a separate concurrency canary passes.
- Reject changes that improve wall time but do not reduce measured CPU, unless they are explicitly
  needed for throughput or recovery.
- Treat a stable P90 above roughly 8 ms as insufficient margin for restoring concurrency under
  the Free 10 ms cap.

## Research references

- Cloudflare Workers limits and CPU accounting: <https://developers.cloudflare.com/workers/platform/limits/>
- Workers CPU profiling: <https://developers.cloudflare.com/workers/observability/dev-tools/cpu-usage/>
- R2 Workers API, metadata, and conditional writes: <https://developers.cloudflare.com/r2/api/workers/workers-api-reference/>
- Cron trigger syntax and limits: <https://developers.cloudflare.com/workers/configuration/cron-triggers/>
- Wrangler configuration: <https://developers.cloudflare.com/workers/wrangler/configuration/>
- Queues pricing and limits: <https://developers.cloudflare.com/queues/platform/pricing/>
- D1 pricing: <https://developers.cloudflare.com/d1/platform/pricing/>
- Durable Objects pricing: <https://developers.cloudflare.com/durable-objects/platform/pricing/>

## Progress log

### Step 1 checkpoint

Complete. Added `workers/llm-dispatch-proxy/bench/dispatch-benchmark.js`, which exercises the
real scheduled handler against fake R2 and an immediate provider response. It reports local
process CPU separately from wall time and supports success/failure and payload-size runs. The
benchmark is a comparative signal, not a substitute for deployed Cloudflare `cpuTime`.

Initial baseline on this machine:

- 100 × 50 KB success: CPU P50 0.196 ms, P95 0.669 ms;
- 100 × 50 KB HTTP-400 failure: CPU P50 0.267 ms, P95 0.578 ms; and
- 30 × 250 KB success: CPU P50 0.570 ms, P95 1.143 ms.

The existing 66-test Worker suite also passes.

### Step 2 checkpoint

Complete locally. The low-risk change set now includes:

- cached R2 cron-lease metadata reads with legacy JSON-body fallback;
- cached date/time formatters and per-route active-pricing calculations;
- sampled successful-dispatch profile logging, with failure profiles retained; and
- Wrangler `minify: true` plus an explicit 10% success-profile sample rate.

The Worker suite remains 66/66 passing, the benchmark success baseline remains below 1 ms P95
local CPU for the 50 KB fixture, and the changes have not yet been deployed to Cloudflare.

Step 3 changes rate-ledger semantics in the serial 1/1 path and therefore require maintainer
approval after the writer/crash-state audit described above.

### Step 3 checkpoint

Complete after approval. The audit found that the Worker is the only writer of
`state/dispatch_budget.json`; the cron lease is the single-writer guard. The serial path now
commits request/provider pacing before the upstream call without creating a temporary `inflight`
entry or performing the later cleanup CAS. Any configuration above 1 retains the previous
reservation/release behavior.

The next step is measurement of the current production shape. A control-record/payload split is
not approved until that evidence shows it is worth the migration complexity.

### Rollback comparison checkpoint

The Worker was rolled back to prior version `c9e76f6a-790e-4204-b597-f5f5dfdc881f` for a filtered
`llm_dispatch_batch` comparison. The first matching post-rollback invocation used CPU `19` ms,
wall time `16.725` seconds, completed Gemini successfully, and referenced a `17.2 KB` request
record. A subsequent matching five-invocation tail used CPU `10, 10, 11, 11, 13` ms (average
`11.0`, P50 `11`, P90 `13`). This makes the earlier dashboard/export readings of `7`–`8` ms even
less suitable as a characterization of the prior version: they were unfiltered and are not
reproduced by the like-for-like tail.

### Follow-up after filtered candidate tail

The requested filtered tail was later captured from version `5acd24e2` before rollback. It showed
five dispatched invocations at `9`–`12` ms CPU (average `10.2` ms; P50 `10`; P90 `12`) and
`5.187`–`9.714` seconds wall time. The matching rollback tail is slightly higher overall, but
the Gemma-only means are nearly identical and the route mix is small. The evidence does not
support calling the candidate a CPU regression or declaring either version safe for restored
concurrency. The next useful action is a larger route-balanced observation window, not a
canonical record-layout migration based on these samples alone.

### Operation-shape checkpoint

Added `bench/cpu-profile.js` (V8 self-time per function against a seeded ledger and marker backlog)
and R2 operation-count regression tests. Reduced the scheduled invocation from 14 to 10 R2
operations when dispatching and from 7 to 4 when idle, cached ICU formatters, and fixed the
no-candidate ledger write that could never fire. Deployed for production `cpuTime` measurement; see
the section above for the reframing of **Current findings**.
