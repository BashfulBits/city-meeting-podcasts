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

## Production measurement and concurrency headroom — 2026-08-14

Deployed the merged branch and measured `cpuTime` by version with
`wrangler tail --format=json --sampling-rate=0.999`. Cold starts (the first one to two invocations
after a deploy) are excluded from the warm figures; they land at `17`–`20` ms and are not
representative.

| Version | Config | Warm samples | P50 | Mean | Max | CPU per request |
| --- | --- | --- | --- | --- | --- | --- |
| `c9e76f6a` (prior rollback) | 1/1 | 5 | 11 | 11.0 | 13 | 11.0 |
| `5acd24e2` (prior candidate) | 1/1 | 5 | 10 | 10.2 | 12 | 10.2 |
| `c58e8472` (merged branch) | 1/1 | 11 | **8** | **8.2** | 12 | 8.2 |
| `23164f2f` (merged branch) | 2/2 | 10 | 11 | 11.8 | 16 | **5.9** |

Every invocation reported outcome `ok`; no `exceededCpu` occurred at either configuration. Batch
counts were verified from the logs (`count: 1` at 1/1, `count: 2` at 2/2), so the comparison is
between genuinely different batch sizes rather than a configuration that failed to take effect.

### The cost model is operation-linear

Pairing measured CPU against the R2 operation counts the benchmark reports for the same
configurations (`N=1` -> 10 operations, `N=2` -> 14) gives a consistent per-operation cost:

- `8.2 ms / 10 ops` = `0.82` ms/op;
- `11.8 ms / 14 ops` = `0.84` ms/op; and
- marginal `3.6 ms / 4 ops` = `0.90` ms/op.

Fitting the two operating points gives `cpu_ms ~= 0.90 * operations - 0.8` over the 10-20 operation
range. The negative intercept means this is an interpolation, not a physical decomposition, and it
should not be extrapolated far below 10 operations.

This corroborates the operation-driven model over a byte-driven one, which matters because the two
models recommend opposite work. Supporting evidence: the Worker's own JavaScript costs ~0.4 ms of
the 8.2 ms, and the earlier checkpoint above recorded `13.1 KB`, `15.4 KB`, and `62.7 KB` canonical
records all landing in the same `7`-`8` ms band -- a 4.8x size range with no CPU signal.

### Concurrency amortizes fixed cost

The benchmark's fixed/marginal split is **7.5 operations and 34 KB per invocation** plus **3.1
operations and 121 KB per request**. Because more than half the cost of a 1/1 invocation is fixed,
raising concurrency improves CPU *per request* even as it raises CPU *per invocation*: `8.2` ms/req
at `N=1` against `5.9` ms/req at `N=2`, a 28% efficiency gain for double the throughput.

Projected against the fit, with the current data model:

| N | R2 operations | Projected CPU | CPU per request |
| --- | --- | --- | --- |
| 1 | 10 | 8.2 ms | 8.2 |
| 2 | 14 | 11.8 ms | 5.9 |
| 3 | 17 | 14.5 ms | 4.8 |
| 4 | 20 | 17.2 ms | 4.3 |

**With the current data model, `N=1` is the only configuration under 10 ms.** `N=2` is measured at
`11.8` ms mean / `11` P50 -- roughly 18% over a soft average limit, with no `exceededCpu` observed
in eleven invocations. Nothing beyond `N=2` is defensible without storage changes.

### What each storage change would buy

Removable operations, in increasing order of migration cost:

- **A -- generalize the up-front budget commit to `N>1`.** The cron lease already guarantees no
  other invocation is dispatching, so an `inflight` reservation only protects across invocations,
  never within one. Committing usage up front for all `N` candidates is exactly what the serial
  path already does for `N=1`. Removes the batch release CAS: **-1 fixed operation**, no migration,
  no record-layout change.
- **B -- remove the per-request ready-marker delete.** Requires replacing the R2 `ready/` marker
  index: **-1 operation per request**.
- **C -- remove the `ready/` list.** Falls out of the same index replacement: **-1 fixed operation**.
- **D -- remove the cron lease.** Only possible if delivery and rate serialization move off the
  global lease (Queues for delivery plus CAS retries or a Durable Object for the ledger):
  **-3 fixed operations**.

Projected CPU (`*` marks above 10 ms):

| N | +A | +A+B | +A+B+C | +A+B+C+D |
| --- | --- | --- | --- | --- |
| 1 | 8.2 | 7.3 | 6.4 | 3.7 |
| 2 | 10.9* | **9.1** | 8.2 | 5.5 |
| 3 | 13.6* | 10.9* | 10.0* | 7.3 |
| 4 | 16.3* | 12.7* | 11.8* | **9.1** |
| 6 | 21.7* | 16.3* | 15.4* | 12.7* |

Read off the table:

- **A alone does not unlock `N=2`** (10.9 ms). It is still worth doing: it is nearly free, removes a
  write, and is a prerequisite for the rest.
- **A+B unlocks `N=2` at 9.1 ms.** This is the cheapest route to double throughput.
- **A+B+C+D (the Queues-based Step 5) unlocks `N=4` at 9.1 ms** and is the only option that reaches
  meaningful concurrency.

### Step 4 is now recommended against

A control/result split reduces **bytes**, and bytes are not the constraint. It also *adds* an
operation per request unless the control update is folded into the result object's custom metadata,
in which case it is operation-neutral at best. On an operation-linear cost model it therefore
unlocks no concurrency and does not reduce single-request CPU materially, while carrying a dual-read
migration, recovery tooling, and a privacy review.

**Recommendation: close Step 4 as not-worth-doing on current evidence, and reprioritize Step 5
(queue-delivery replacement of the `ready/` index) as the change that actually buys throughput.**
Do change A first as a standalone, low-risk improvement.

### Caveats

- The per-operation constant is fitted from two operating points that differ in both operation count
  and code path (serial ledger at 1/1 against the reservation path at 2/2). It is consistent across
  both, but linearity is not yet proven; an `N=3` canary would discriminate.
- Warm samples are 11 at 1/1 and 10 at 2/2. That is enough to separate `8` from `11`-`12`, and not
  enough for a reliable P99. The single `10` ms sample recorded against the post-revert version
  `de4a8651` is that deployment's cold start, not a 1/1 steady-state figure.
- Cold starts remain `17`-`20` ms. They are infrequent, but a deploy during a backlog will produce
  a burst of them.

## Batch-size measurement and the throughput ceiling — 2026-08-14

Five production configurations were measured by deployed version. Cold starts are excluded; note
that warm-up after a deploy is gradual over roughly seven invocations (progressive JIT tiering),
not a single cold invocation.

| Config | R2 ops | P50 | Mean | Max | **CPU per request** |
| --- | --- | --- | --- | --- | --- |
| `N=1` | 10 | 8 | 8.2 | 12 | **8.2** |
| `N=2`, before A+B | 14 | 11 | 11.8 | 16 | 5.9 |
| `N=2`, with A+B | 12 | 10 | **9.9** | 11 | **4.95** |
| `N=3`, before A+B | 17 | 16 | 17.0 | 20 | 5.7 |
| `N=3`, with A+B | 14 | 15 | 15.5 | 22 | 5.2 |

### Cost is superlinear in batch size

The earlier two-point fit (~`0.90` ms per R2 operation) holds from `N=1` to `N=2` -- A+B removed two
operations and bought `1.9` ms, almost exactly as predicted -- but **fails above that**. Going
`N=2` -> `N=3` with A+B adds two operations and costs `5.6` ms, roughly `2.8` ms/op. `N=3` is also
markedly noisier (`13`-`22` ms).

The likely mechanism is live-set pressure rather than operation count: at `N=3` three canonical
records, three upstream payloads, and three responses are resident simultaneously, and garbage
collection was already the secondary cost in local profiling. `candidate_prepare_ms` also rises from
`680` to `1,062` ms.

**Consequence: `N=2` is the optimum on both axes.** It is the cheapest per request (`4.95` ms against
`8.2` at `N=1`, a 40% efficiency gain) and `N=3` is *worse per request* than `N=2` while running 55%
over the limit. Batch size is now set from this measurement in `wrangler.jsonc`.

### The limit is per-invocation, with tolerance for occasional overage

Cloudflare documents the Free-plan cron CPU limit as `10` ms enforced **per invocation**, not as an
average: isolates carry "built-in flexibility to accommodate occasional overages", but a Worker that
hits the limit *consistently* is terminated. Observed tolerance is well above the nominal limit --
`N=3` ran at a `15.5` ms mean and a `26` ms maximum across 14 invocations with every outcome `ok`
and no `exceededCpu` -- but that is tolerance, not headroom, and should not be designed against.
`N=2` at a `9.9` ms mean sits inside the limit rather than relying on that tolerance.

### The binding constraint is invocations, not provider capacity

Aggregate free-tier LLM capacity across the configured free routes is **66,120 requests/day**
(~46/minute), against which:

| Delivery | Ceiling | Share of LLM capacity |
| --- | --- | --- |
| cron `N=1` | 1,440/day | 2.2% |
| cron `N=2` | 2,880/day | 4.4% |
| Queues on the Free plan | ~3,333/day | 5.0% |

Provider rate limits are roughly **23x** from binding. The constraint is the number of Worker
invocations: a Cron Trigger is minute-resolution, so throughput is `BATCH_CONCURRENCY` per minute
and nothing else.

Longer invocations do not help. The CPU allowance is granted *per invocation*, so an invocation that
runs for 13 minutes forfeits the allowance of every cron tick it blocks into a cheap `lease_busy`
return. Dispatching 28 requests across one 14-minute invocation averages ~`12` ms per invocation
against `9.9` ms for the same throughput at `N=2` once per minute.

### Steps C and D are recommended against on the Free plan

Cloudflare Queues **is** available on the Workers Free plan, at **10,000 operations/day** with 24-hour
retention. At three operations per message (write, read, delete) that is ~**3,333 requests/day** --
only **16% above** what cron at `N=2` already delivers, in exchange for replacing the ready-marker
index, the cron lease, and the delivery path.

**Recommendation: do not implement C or D on the Free plan.** They were justified by a
linear-in-operations cost model that the `N=3` measurements falsify, and their own delivery quota
caps them barely above the current ceiling.

### What would actually raise throughput

The Free-plan architecture is within ~2x of its ceiling. Materially more throughput requires
changing a platform constraint, not the Worker:

1. **Workers Paid (`$5`/month).** Raises the cron CPU limit from `10` ms to `30` seconds -- roughly
   3,000x -- which retires this entire workstream, and raises Queues to 1M operations/month
   (~33,000/day). Throughput would then be bounded by provider limits at ~46/minute rather than by
   CPU at 2/minute: a ~23x increase for `$5`. **This is by far the best return available.**
2. **An external invocation driver on the Free plan.** Invocations, not CPU, are scarce: the Free
   plan allows 100,000 requests/day against the cron's 1,440. A scheduled GitHub Actions job (this
   repository already runs many) polling an authenticated dispatch endpoint every ~10 seconds would
   raise invocation count without changing the Worker's data model. This trades Actions minutes and
   an external dependency for throughput and has not been prototyped or costed.

## Scaling beyond the cron ceiling — backend evaluation (2026-08-14)

Step 5 above listed Queues, D1 and a Durable Object as candidates without costing them. This
section settles that, and finds that the binding constraint is neither CPU nor storage.

### Every Free-plan ceiling, in the order they bind

| Ceiling | Limit | Binds at |
| --- | --- | --- |
| Cron invocation rate | 1/minute | **2,880 jobs/day** at `N=2` |
| **Worker requests** | **100,000/day** | **~20,000 jobs/day** at today's request shape |
| Worker CPU | 10 ms/invocation | `N=2` per invocation |
| Durable Object requests | 100,000/day | ~50,000 jobs/day at a 3.5s alarm |
| Durable Object row writes | 100,000/day | reached before the request cap; persist on a timer |
| R2 Class A operations | 1M/month free | ~6,600 jobs/day before it costs money |

The cron rate is what binds today. **The next one is the Worker request cap, not CPU or storage** --
and polling, not dispatch, is what consumes it:

| Scenario | enqueue | dispatch | poll | total |
| --- | --- | --- | --- | --- |
| Today's request shape, 50k jobs/day | 50,000 | 25,000 | 125,000 | 200,000 (over) |
| Today's request shape, 25k jobs/day | 25,000 | 12,500 | 62,500 | 100,000 (at cap) |
| + batch poll (10 ids per call) | 50,000 | 25,000 | 12,500 | 87,500 |
| + batch enqueue and batch poll | 5,000 | 25,000 | 12,500 | **42,500** |

At roughly 2.5 polls per job, polling is ~60% of the request budget. A batch-poll endpoint is
therefore a prerequisite for any target above ~20,000 jobs/day, independent of storage or
coordination choices.

### Coordination backend

R2 is a poor fit for small, hot coordination state: every write is a Class A operation. Three of
the four R2 consumers exist only to emulate a single writer.

| R2 consumer today | Purpose | Under a Durable Object |
| --- | --- | --- |
| `locks/cron.json` | make one invocation the sole dispatcher | removed -- the DO *is* the serialization point |
| `state/dispatch_budget.json` | share rate state across invocations | removed -- in DO memory, reads free |
| `ready/` list and markers | find eligible work without scanning | removed -- DO SQLite index |
| `requests/*.json` | the prompts and responses themselves | **stays in object storage** |

That takes Class A operations from ~5 to ~2 per job. Durable Objects are available on the Free plan
(SQLite backend only): 100,000 requests/day, 13,000 GB-s/day, 5M row reads and 100,000 row writes
per day, 5 GB.

DO **alarms** are millisecond-resolution and self-scheduling, so they also replace the external
sub-minute trigger: a 10s alarm is 8,640 DO requests/day (8.6% of cap).

**Design constraint:** the DO must coordinate, never dispatch. DO duration is billed on wall-clock
while the object is active, so a DO that awaits an LLM call inherits the problem that rules out
Cloud Run below -- a 20s wait at 128 MiB is ~2.6 GB-s, and 17,000 jobs/day would be ~44,000 GB-s
against 13,000 free. The DO decides admission in ~5 ms; a plain Worker performs the R2/B2 reads and
the provider call, where I/O wait is not billed.

**Do not put payloads in the DO.** The 5 GB looks sufficient, but row writes are the real limit
(50,000 jobs x 2 = exactly the 100,000/day cap, before any size multiplier), a single-threaded DO
becomes the throughput bottleneck, and payload transfer inflates the duration bill.

D1 is also Free-plan available (5M row reads, 100,000 row writes/day) and is an adequate home for
the ledger alone, but it provides no serialization -- concurrent Workers would race -- and does not
solve the trigger problem. Workers KV is disqualified: 1,000 writes/day and eventual consistency.

Cloudflare Queues on the Free plan is 10,000 operations/day, ~3,333 jobs/day at three operations per
message: 16% above the cron ceiling, for a full delivery rewrite. Rejected.

### Object storage: B2 rather than R2

Backblaze B2 charges **nothing** for Class A (writes), Class B (reads) or Class C (listing)
transactions on pay-as-you-go. Class D covers service operations such as Event Notifications
outbound calls, which this workload does not use. Egress to Cloudflare is free under the Bandwidth
Alliance, and this project already stores audio in B2.

| Jobs/day | R2 Class A cost | B2 |
| --- | --- | --- |
| 12,000 | $7.16/mo | $0 transactions |
| 28,800 | $14.94/mo | $0 transactions |
| 66,240 | $40.21/mo | $0 transactions |

B2 leaves storage as the only charge: ~3.5 GB/day of records is **$0.17-$0.73/month** depending on
retention. Requests must be signed (SigV4) rather than using a binding; measured at **0.033 ms** per
request with a cached signing key -- 0.3% of the CPU budget, immaterial.

### Google Cloud Run is the wrong platform for dispatch

Workers do not bill I/O wait; Cloud Run's request-based billing does, and this workload is ~95%
waiting on provider generation. The free tier sustains only 0.154 vCPU and 79 MiB continuously,
below Cloud Run's own minimums, so an always-on dispatcher does not fit. Cloud Run is viable purely
as a sub-minute *ticker* (a 10s tick is 6% of the free vCPU-second allowance), but a Durable Object
alarm does the same job without the external dependency.

### Recommended order

1. **Batch poll and batch enqueue endpoints.** Small, no new infrastructure, and a prerequisite for
   anything above ~20,000 jobs/day.
2. **B2 for canonical records.** Transactions free, credentials already exist.
3. **Durable Object for coordination plus its alarm.** Removes the lease, the ledger and the ready
   index; replaces any external ticker.

Together these reach ~50,000 jobs/day for roughly **$0.50/month**.

**The alternative to weigh first:** Workers Paid at `$5`/month raises the request cap to ~333,000/day
and cron CPU from 10 ms to 30 seconds, which retires the request cap, the CPU ceiling and most of
the reason for the Durable Object in one step -- leaving B2 as the only item still worth doing on
its own merits. If the throughput target is steady-state rather than a backlog burn-down, price that
against the engineering before starting.

## Phasing: R2 quota relief and the path to higher throughput (2026-08-14)

### Remaining low-hanging fruit: fold the cron lease into the ledger

One clear win is left in the current architecture. `locks/cron.json` and
`state/dispatch_budget.json` are both small CAS objects written by the same single writer, in the
same sequence, every invocation. Merging the lease *into* the ledger object collapses five
operations into three:

| | today | merged |
| --- | --- | --- |
| acquire | `head` lock + `put` lock | (part of the ledger read) |
| read ledger | `get` budget | `get` budget+lease |
| reserve | `put` budget | `put` budget + lease acquired |
| release | `put` lock | `put` lease released |
| **total** | **5** | **3** |

A held lease is then detectable from the same read that loads the ledger, so a `lease_busy` tick
costs **one** operation instead of three. The acquire CAS becomes a single conditional write that
both takes the lease and commits reservations, which is *stronger* than today's acquire-then-work
window.

Effect: `N=2` drops 12 -> 10 R2 operations, roughly `9.9` -> `8.2` ms — real headroom at a
configuration currently sitting on the limit — and R2 Class A per invocation falls from 3 to 1.

It does **not** unlock `N=3`: that ceiling is the superlinear live-set cost of three resident
canonical records, not operation count.

**Not done in this PR deliberately.** It changes the lease/ledger coordination contract, which is
the system's single-runner guarantee, and would invalidate the production `cpuTime` measurements
recorded above. It wants its own change and its own canary.

### R2 Class A pressure is the near-term driver, not CPU

R2 Class A operations bill **per account**, not per bucket, so the dispatch Worker's
`citypods-llm-dispatch` bucket competes with the H17 coordination plane (compute budget, work-lease
ledger, provider slots, catalog manifest) for one 1M/month free tier.

| Dispatch throughput | Class A/month | Share of the account free tier |
| --- | --- | --- |
| 2,880/day (today, `N=2`) | 0.43M | **43%** |
| 17,280/day | 2.59M | 259% |
| 50,000/day | 7.50M | 750% |

**LLM dispatch alone consumes ~43% of the shared allowance at today's throughput.** That is the
reason to act, and it is independent of any throughput increase.

### The Worker is the one component not following the project's own storage policy

`citypods/storage/routing.py` states the rule already: state that genuinely needs compare-and-swap
routes to R2; everything else routes to B2. H17 PR6 applied it to the provider lease pool
specifically because a per-poll `list` was a Class A operation. The dispatch Worker puts *all* of
its state on R2, including multi-megabyte prompts that never need CAS.

Auditing every conditional write in `workers/llm-dispatch-proxy/src/index.js`:

| Object | Conditional write? | Purpose | Destination |
| --- | --- | --- | --- |
| `state/dispatch_budget.json` | yes (`etagMatches`) | multi-writer rate ledger | **stays on R2** |
| `locks/cron.json` | yes (`etagMatches`, `etagDoesNotMatch`) | single-runner lease | **fold into the ledger** |
| `requests/<id>.json` | **mixed** | see split below | **split** |
| `ready/<key>.json` | **no** — plain put, list, delete | queue index | **B2** |

The canonical record conflates two different kinds of state, which is why it must be *split* rather
than simply moved:

| Part | Conditional write? | Size | Destination |
| --- | --- | --- | --- |
| control (`id`, `status`, `model`, `attempts`, `available_at`, `policy`, `error`) | **yes** — `etagDoesNotMatch: "*"` on enqueue is the idempotency guard across concurrent Actions runners, which hold no lease | ~1 KB | **R2** |
| `request.messages` (the prompt) | no — immutable after enqueue | 13-63 KB | **B2** |
| `response` | no — written once | 5-20 KB | **B2** |

The `etagMatches` on the *terminal* write is belt-and-braces: the cron lease already guarantees a
single finalizer. The load-bearing conditional write is the enqueue create.

Note this reverses the earlier recommendation to close Step 4. That recommendation was correct on
its own terms — a control/payload split buys no CPU and unlocks no concurrency — but it was
answering the wrong question. The justification here is **R2 Class A quota shared with the
coordination plane**, not CPU.

B2 charges nothing for Class A/B/C transactions, and requests need SigV4 rather than a binding
(measured `0.033` ms with a cached signing key). B2 has no conditional-write support, which is
precisely why only the non-CAS objects move.

### Recommended phasing

| Phase | Change | Trigger | Effect |
| --- | --- | --- | --- |
| **0** | Merge this branch (`N=2`, blocked-head fix) | done | P50 `11` -> `8` ms, 2x throughput |
| **1** | Fold the cron lease into the ledger | next | `N=2` `9.9` -> `8.2` ms; R2 Class A 3 -> 1 per invocation |
| **2** | Split the canonical record; prompts, results and markers to B2 | **R2 quota — act on this now** | dispatch falls from **43% -> ~4%** of the shared free tier |
| **3** | Batch enqueue + batch poll endpoints | throughput above ~15,000 jobs/day | addresses the *Workers request* cap (100k/day), a different quota |
| **4** | Workers Paid (`$5`) **or** Durable Object coordination | when Phase 3 is no longer enough | see the backend evaluation above |

After Phases 1 and 2, dispatch uses ~0.04M Class A/month at today's throughput and **0.75M at
50,000 jobs/day** — inside the free tier, with room left for the coordination plane.

### On Phase 3, since its purpose is easy to mistake

Batch enqueue and batch poll have nothing to do with R2 or storage. They address the **Workers
request cap of 100,000/day**, which counts every HTTP call to the Worker: one enqueue, roughly half
a dispatch invocation (at `N=2`), and — dominantly — every poll.

At today's 2,880 jobs/day that is ~10,000 Worker requests/day, 10% of the cap: **not a concern, and
not worth building yet.** It becomes binding around 20,000 jobs/day, at which point polling alone is
~60% of the budget. Build it when throughput approaches that, not before.

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

### Production concurrency checkpoint

Measured the merged branch in production at 1/1 (P50 `8` ms, mean `8.2`) and, as an authorized
time-boxed canary, at 2/2 (P50 `11` ms, mean `11.8`, no `exceededCpu`). Reverted to 1/1 afterwards.
The measurements establish an operation-linear cost model (~`0.90` ms per R2 operation), show that
concurrency improves CPU per request by 28%, and reverse the priority of Steps 4 and 5.

### Batch-size and delivery checkpoint

Implemented A (commit durable usage up front at every batch size; enforce a route's concurrency
ceiling in memory) and B (remove finished markers in one keyed R2 delete). Marginal cost fell from
`3.1` to `2.0` operations per request. Measured `N=1`/`N=2`/`N=3` before and after, found cost
superlinear above `N=2`, and set `BATCH_CONCURRENCY=2` from that measurement. Established that
provider capacity is ~23x from binding and that Queues on the Free plan caps only 16% above the
current ceiling, so Steps C/D are recommended against; Workers Paid is the change that would
actually matter.

Trade-off accepted in A: a crashed invocation no longer holds a durable concurrency slot for its
in-flight request, so a `concurrency`-limited route can be briefly over-dispatched after a crash.
Durable rpm/rpd accounting is unaffected -- committed before the provider call, never rolled back --
and the provider rejects the excess with a retryable 429.
