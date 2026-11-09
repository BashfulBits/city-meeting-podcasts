# LLM throughput investigation — October 23, 2026

## Scope and observations

Read-only production investigation, approximately 17:25–17:38 UTC. Used bounded v2 stats,
route stats, lane ingress preflight, one guarded detailed snapshot, Actions logs, and account
GraphQL analytics. No production settings, jobs, payloads, or provider quotas were changed.

### Producer ingress after noon

The UTC hourly snapshot recorded 2,186 new admissions at 12:00. By approximately 17:25,
the counter was 2,761: **575 additional admissions**, separate from retries or completions.
The hourly series is sampled, so this is a snapshot comparison, not an exact noon boundary count.

The reservation release works on the **shared** write-unit pool: half of other lanes' unspent
reservations becomes borrowable with 12 hours remaining; all becomes borrowable with six hours.
It does not override a lane's independent `daily_write_units` ceiling, the global 4,000-job /
25,600-unit guard, or the live 90,000-row safe stop. `config/site_config.yml` explicitly defines
these lane ceilings as hard daily limits.

- Tagger: 1,605 admissions, 9,600/9,600 write units. Preflight closed with
  `purpose_write_budget_exceeded`; its queued depth was below its queue ceiling.
- Moments: 1,078 admissions, 4,798/4,800 units. The remaining two units cannot fund a new job.
- Other lanes together consumed only 325 units. Their protected reservations becoming available
  cannot reopen a lane whose own ceiling is exhausted.
- [Tag run 39993218190][tag-run]
  at 17:12 reported closed tag ingress and no ingress attempts. Changes in its shared Worker
  snapshots include concurrent workflows; they are not submissions attributable to that run.
- [Moments run 39981197227][moments-run]
  at 15:46 also reported no ingress attempts: 2,889 pending deferrals and 89 cached completions
  that failed response validation, including invalid JSON and schema fields. Increasing a producer
  cap alone does not turn pending handles or invalid cached responses into fresh candidates.

### Provider dispatch and idle slots

At the detailed snapshot, all eight Gemini 3.5/3.6/3.7/3.8 Flash account routes had consumed
20 requests for the provider day. Both Gemini 3.1 Flash Lite accounts had consumed 500/500.
Those routes cannot dispatch more until their provider-day reset (Gemini uses Los Angeles
midnight, distinct from the scheduler's UTC ingress reset). Gemini 3.5 Flash Lite still had
capacity and twelve calls in flight across its two accounts. No pending calls is not evidence
that a route has remaining request or token quota.

The retained last-empty diagnostic examined 96 candidates and chose none. Its gates included
input above a Gemini minute quota, prompt caps, exhausted daily quotas, blocked routes, an
occupied NVIDIA route, and Google input-window reservations. It already used three extended
scans. This diagnostic predates the latest successful claim and has no original event timestamp;
it must not be read as the reason every later cron was idle. New claim diagnostics include `at`.
The day counters showed 635 successful claim bundles and 135 empty claims: 95 mixed admission,
28 active-bundle limit, and 12 in-flight-call limit. An empty claim while earlier calls are
running does not mean the executor has stopped.

A 32-row setting is not a global scan limit: ceiling-bound models read a head of up to 32 and
one extension of up to 32. Other models previously read a four-row head and an extension
topping up to 32.
Keyset traversal can eventually find smaller jobs behind temporarily unplaceable jobs. Two
implementation gaps reduced that traversal's usefulness, alongside a stale acceptance cap:

1. `MAX_BUNDLE_JOBS` was five but `MAX_JOBS_PER_MODEL_CLAIM` remained four. The latter caps
   accepted jobs, not just the first query: even a 32-row Gemini read could contribute at most
   four jobs from one discovery model. Non-ceiling models also started with only four rows.
   A single usable model could never fill five slots. A regression reproduces four before the
   fix and five afterward. Live Worker settings have no plain-text overrides for these tunables.
2. Any accepted head job stopped extension, even with unused model and bundle allowance.
3. The advancing per-model frontier existed only in memory. Eviction restarted traversal just
   behind the same head rather than continuing from the last persisted claim.

The patch aligns the per-model allowance to the existing five-job bundle limit, allows the
existing extension for partially filled heads and checkpoints frontiers
inside existing claim diagnostics/accounting. Cold instances restore only configured models and
valid cursor tuples. Normal idle-write coalescing remains: up to ten minutes of uncheckpointed
progress can be lost. No additional SQL statement or forced write is needed for checkpointing.
The head remains first, the scan still wraps, and all exact quota/eligibility checks remain.

## Free-tier cost and validation

The account's `durableObjectsPeriodicGroups` GraphQL sums reported **1,102,101 rows read** and
**51,537 rows written**, with zero exceeded-CPU errors. These are account analytics, not the
coordinator's application estimates. Cloudflare's free allowance is
5 million reads and 100,000 writes per UTC day
([pricing](https://developers.cloudflare.com/durable-objects/platform/pricing/)).
Analytics can lag the live scheduler. This snapshot establishes current headroom, not a guarantee
about a later full day's workload or all other Workers account quotas.

The per-model allowance changes from four to five to match the already approved bundle size.
The 32-row lookahead, five-job bundle cap, concurrency limits, per-lane and global ingress ceilings,
provider quotas, and 90,000-row brake remain unchanged. Non-ceiling heads read one more candidate;
their extension shrinks from 28 to 27, keeping the same 32-row maximum. Ceiling-bound models
retain the same 32-row head plus 32-row extension maximum. A regression reproduces one claimed
job before the fix and three afterward with one five-row head plus one bounded extension (seven candidates returned).
Another regression reconstructs the coordinator on the same storage and finds the eligible
job beyond the previously scanned prefix. Existing row-read scale guards remain applicable.
No pipeline versions change; already queued jobs benefit naturally, with no backfill or reset.

## Follow-up options

- Deploy the patch, then compare timestamped empty diagnostics, jobs per bundle, successful
  completions, and daily account rows; do not judge throughput from pending-call counts alone.
- Keep quota-exhausted accounts blocked until their real reset. Split oversized producer work
  using the existing calibrated packing limits rather than relaxing the provider TPM check.
- If more producer ingress is desired after lane exhaustion, design explicit late-day borrowing
  above lane ceilings while retaining global ingress and worst-case finishing-row reserves.
  That changes the documented hard-ceiling policy and needs maintainer confirmation. It is not
  silently bundled into this patch. Additional ingress alone does not fix a backlog of work
  that cannot fit the remaining routes.
- Investigate moments' 89 invalid cached responses separately from scheduler starvation; these
  are response-validation failures, not evidence of missing dispatch capacity.

Related contracts: [review/44](../44-bounded-bundled-llm-dispatch.md),
[review/47](../47-do-row-read-budget.md). The latter's bounded-observation rule still applies;
GraphQL row-read analytics are available on this account as of this investigation.

[tag-run]: https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/39993218190
[moments-run]: https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/39981197227
