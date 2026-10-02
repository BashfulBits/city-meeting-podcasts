# LLM capacity reconciliation: 800 meetings/day

Analysis for PR #1982, 2026-10-02. This corrects the earlier assumption that five lanes at
800 jobs/day fund 800 fully enriched meetings. A meeting, a candidate, a batch, an admitted job,
a provider attempt and a billed DO row are different units.

## Existing design and observed eligibility

[review/50](50-p0-llm-verb-backlog-trend.md) is the read-only backlog measurement phase of
[review/49](49-judge-consensus-admission.md). It does not implement the new judges. review/49's
500-meeting capacity table does account for judge packing, candidate counts and contested work;
its P1-P7 phases remain L2 until shadow evidence exists. This PR does not skip those gates.

The catalog scan in review/49 covers 26,537 episodes across 42 source keys. It found 32% without
provider chapters overall, about 60% recently; 68% of recent episodes have transcripts; and
8.3 tag candidates per candidate-bearing episode, mostly deterministic rule candidates.
Its planning assumptions are 60% needing agenda/locator, 75% tag-eligible, 12 tag/chapter pairs
per tagged episode including future LLM tags, 10% contested and 10% moments-eligible. These are
more useful for steady-state sizing than assuming every task applies to every meeting.

review/50's backlog fixture also shows most moments backlog is rollout policy, not capacity.
The tag stage's `ran` includes rule-only work: neither it nor the Worker's call counts gives a
reliable completed-LLM-meetings denominator. Live `/v2/stats` was read for this analysis; it
reports attempts, not per-episode batch distributions. Do not call speculative batching means
measured production averages. Use the P0 trend and P1 shadow records to revise them.

## Today's implementation: where calls multiply

- Agenda and locator: one job each when generated chapters are needed. Provider chapters skip them.
- Tagger: chapter groups are greedily split against route input/output/TPM and request-byte limits.
- Production and shadow prelabelers: each candidate batch is a separate job (at most 100
  candidates, often fewer because Gemma's input limit is 10,000 tokens). Both are enabled today.
- Moments: one extraction job per eligible meeting; council requests index seven of the nine
  configured models. The nine-model pool is a conservative ingress sizing bound.
- Judges: `MomentJudgeStage` sends every candidate to every configured judge, pinned to one model.
  Five quotes with three judges means fifteen jobs, not one. Its ingress cost is four units,
  not six. The stage has one shared extraction/judge admission counter.
- Pending polls and exact idempotent replays are not new jobs. Transport retries can add provider
  attempts and lifecycle rows without enqueue units. A new schema-repair job adds all three.

Stress scenario, not a measured mean: all 800 meetings eligible; two tag batches, three production
and shadow prelabel batches, five quotes and three judges. No new schema-repair jobs; 10% extra
provider attempts. Optional research work is excluded, so including it only adds demand.

| Lane | Jobs/eligible meeting | New jobs/day | Enqueue units/job | Units/day |
|---|---:|---:|---:|---:|
| Agenda | 1 | 800 | 6 | 4,800 |
| Locator | 1 | 800 | 4 | 3,200 |
| Tagger | 2 | 1,600 | 6 | 9,600 |
| Prelabeler | 3 | 2,400 | 4 | 9,600 |
| Shadow prelabeler | 3 | 2,400 | 4 | 9,600 |
| Moments | 1 | 800 | at most 12 | 9,600 |
| Three-judge panel | 15 | 12,000 | 4 | 48,000 |
| **Total** | **26** | **20,800** | | **94,400** |

With 10% extra attempts: 22,880 provider calls; at full five-job bundles, at least 4,576 bundles.
The one-model benchmark is about 20 billed lifecycle rows/job at four jobs/bundle, plus four
rows per additional model index: two at enqueue and two when claim deletes the queue indexes
(`coordinator.js` claim path). The latter was missing from review/49. This estimates 460,800
first-try rows, 49,920 retry rows (24 per extra attempt as a planning allowance),
1,440 idle cron rows, and 1,000 operational rows: **513,160 rows/day**.
Both current and consensus scenarios include these same 2,440 overhead rows within the safe
Worker envelope; the 1,000 operational rows are separate from the 10,000 account reserve.
The calculator reads the committed platform/reserve constants and enqueue threshold from the
Worker sources. It does not inspect live environment overrides. Arbitrary retry storms, supersedes and cleanup can cost more.

The shared limits remain 4,000 new jobs, 25,600 enqueue units, 1,400 bundles, and 90,000 safe
account-wide billed rows/day. Raising individual lane caps cannot fund this scenario. The
compiler correctly rejects a lane cap above the global unit envelope. The judge ceiling is
therefore 4,000 jobs, not the 12,000 demanded by this scenario. Reservations protect a fraction
of shared admission; daily lane ceilings are neither reservations nor completion commitments.

This PR sizes batch-lane ceilings to the stated allowances, corrects judges to `per_model`, and
sums extraction and judge producer allowances into the shared R6 counter. Tag per-run caps count
episode reservations, so they stay `ceil(800/3)` rather than being multiplied by batch count.
All global row, queue, provider, concurrency and free-only controls remain authoritative.

## Planned consensus path at 800 meetings/day

Scale review/49's 500-meeting table by 1.6, rounding each lane upward. Its job quantities already
contain probe/contested allowances; rows/job include a separate three-row retry allowance.
Update locator from six indexed models to one size-selected model. Also correct review/49's claim cost for deletion of each extra model index: two rows
per additional model, beyond its two enqueue rows. Pooled three-model jobs thus project 31 rows,
pinned jobs 23, four-model jobs 35, and two-model jobs 27, including the retry allowance. Retire prelabeler, shadow and legacy panel only after P7's migration gates.
The four-model moments pool and two-model sibling/adjudicator pools below are proposed, not live.

| Lane | Per-episode jobs/day | Per-episode rows/day | Across-episode jobs/day | Across-episode rows/day |
|---|---:|---:|---:|---:|
| Agenda | 480 | 14,880 | 480 | 14,880 |
| Locator, pinned DS4/Kimi | 480 | 11,040 | 480 | 11,040 |
| Tagger | 661 | 20,491 | 661 | 20,491 |
| Moments, trimmed four-model pool | 104 | 3,640 | 104 | 3,640 |
| JEV anchor judge | 749 | 17,227 | 186 | 4,278 |
| Independent sibling judge | 749 | 20,223 | 370 | 9,990 |
| Packed adjudicator | 192 | 5,184 | 192 | 5,184 |
| **Jobs / lifecycle rows** | **3,415** | **92,685** | **2,473** | **69,503** |
| Idle cron + operational allowance | | 2,440 | | 2,440 |
| **Total billed rows/day** | | **95,125** | | **71,943** |

Reproduce both columns with `python scripts/llm_capacity_plan.py --consensus episode` and
`python scripts/llm_capacity_plan.py --consensus packed`, respectively.

Enqueue demand is 17,195 units for per-episode packing versus 13,048 across episodes.
A 20% lane headroom allowance makes the latter 15,658 units, below today's 25,600 envelope.
The per-episode plan does not fit: 95,125 exceeds the 90,000 safe budget.
Across-episode packing leaves about 18,057 safe rows before other account usage and deviations.

Important packing sensitivity: review/49 assumes roughly 14k sibling packets; today's two
high-quota Gemma routes have a 10k input ceiling. A rough proportional correction increases
370 sibling jobs to `ceil(370*14/10)=518`, adding 3,996 rows: about **75,939 rows/day**.
At an 8k usable packet budget it becomes 648 sibling jobs and about **79,449 rows/day**.
These projections need a refreshed pooled-job workerd benchmark; the existing benchmark uses
one-model lifecycle jobs. This is a sensitivity estimate, not evidence that uniform token density or quality holds.
Packing must enforce the real route ceiling, learned token ratio and output reserve.

During P1 shadow, old and new judging coexist. That costs more than the steady-state table;
allocate a bounded shadow slice from spare row headroom rather than doubling the full pipeline.
Queue deduplication and retirement of old evaluators are required before claiming steady state.

## Assignment recommendations and provider bottlenecks

| Lane | Recommendation | Capacity reasoning / remaining gate |
|---|---|---|
| Agenda | Keep Nemotron / HY3 / Gemini 3.1 pool | Evaluation supports the pool; no observed need to replace it. |
| Locator | Keep baseline DS4 small / Kimi overflow | At 60% eligibility, about 480 new jobs; enforce the measured size gate. |
| Tagger | Keep current pool initially | Gemini 3.1 quota is shared with agenda; use HY3/Nemotron there to leave room for tagging. |
| New anchor | JEV packed by evidence/token budget | About 186/day before packet changes versus configured design allowance 1,440/day. P1 shadow first. |
| New sibling | Gemma 31b / 26b pool, independent of JEV | Two 14,400-RPD keys per model; 10k input limit and TPM matter more than RPD. Calibrate quality. |
| Adjudicator | Qwen for small packets; evaluate GLM 5.3 Flash for larger packets | Qwen has 1,000 RPD, 7k input and 1k output tokens/minute; pack about 4-6 questions. GLM needs judge-task evidence, not locator scores alone. |
| Moments | Test a four-model pool from existing quality-qualified routes | GLM for fitting packets, Kimi for overflow, scarce Gemini as supplementary; choose the fourth using task-specific shadow results. Do not invalidate the recipe just to shrink indexes. |
| Legacy Gemini 3.6 panel | Move to small calibration/overflow role in P4/P7 | Forty free calls/day shared with moments cannot support an all-candidate routine panel. |

Qwen's 1,000 RPD also cannot serve 4,000 unbatched quote judgments; at 700 output tokens/call,
its 1k output-TPM budget implies roughly 1,440/0.7 = 2,057 calls/day before RPD, other work or
reasoning. Treat these as configured/observed allowances, not guaranteed sustained provider yield.
GLM's configured 800 RPD is shared across tasks; large adjudication overflow can instead test
DS4.1, with its observed slow latency. Using the locator evaluation's 27/39 small-packet mix illustratively, 480 generated-chapter
meetings split into about 332 DS4 and 148 Kimi jobs before retries. That cohort was council-heavy,
so the split is not a measured all-catalog mix. At 15% extra attempts it is about 382 DS4 and
171 Kimi calls: DS4's 800 RPD leaves roughly 418 for other tasks, while Kimi's one-slot capacity
must be shared with moments. Use mean elapsed time and timeout/retry incidence for sustained
capacity, not the successful-response median alone.

No new route or paid setting is wired by this analysis.

## Cloudflare checks

Verified 2026-10-02 against official documentation:

| Resource | Workers Free allowance | Implication |
|---|---|---|
| DO billed writes | 100k/day account-wide; repo safe budget 90k | Binding for the legacy unbatched plan; packed consensus appears feasible. |
| DO reads / storage | 5M/day / 5 GB | Indexed read regression tests help; measure live projected traffic and retention. |
| DO requests / duration | 100k/day / 13k GB-s/day | Every stub RPC counts; provider I/O is outside DO, not one DO-minute per model-minute. |
| Worker incoming requests | 100k/day account-wide | Cron 1,440 plus submit/poll/retire/admin and shim traffic; no one-job/one-request assumption. |
| Worker invocation | 10ms CPU, 50 subrequests, six outgoing connections | Keep five-job bundles and bounded cleanup; raising daily quotas cannot raise these limits. |
| AI Gateway | Core analytics/cache/rate limiting free | Adds no provider RPD; legacy log capacity is storage retention, not daily inference capacity. |
| R2 Standard free | 1M Class A + 10M Class B/month, 10 GB-month | Dispatch payload/results are B2, not R2; include separate coordination traffic. |

A request illustration only: 2,473 jobs with one unbatched submit, one poll and one retire each
would yield 7,419 Worker requests plus 1,440 cron ticks; repeated polls, stats, proxy/shim hops,
retries and other account workers add to this. DO methods are separate metered sessions.
Without account-wide analytics this is not a verified request/CPU/read budget.

Sources: [DO pricing](https://developers.cloudflare.com/durable-objects/platform/pricing/),
[Workers limits](https://developers.cloudflare.com/workers/platform/limits/),
[AI Gateway pricing](https://developers.cloudflare.com/ai-gateway/reference/pricing/),
[R2 pricing](https://developers.cloudflare.com/r2/pricing/).

## Reproduce and acceptance

Run `python scripts/llm_capacity_plan.py` for today's all-eligible stress scenario;
`--consensus episode` and `--consensus packed` reproduce the revised design tables. Override
eligibility, quotes and batch sizes on the current plan to test a measured cohort. The calculator
makes no provider calls or queue changes. Golden unit tests protect job/row/unit distinctions.

Before claiming 800 completed meetings/day: complete review/49 P1 shadow evidence and P2-P7 gates;
measure per-episode batch/candidate counts, new repair jobs and extra attempts; verify account-wide
DO writes/reads, CPU, request count and route latency over sustained days. Show completed eligible
meetings and backlog/drain trends separately from accepted jobs. Billing or policy changes need a
separate chosen trade-off; this PR keeps the free-only policy and makes no paid upgrade.
