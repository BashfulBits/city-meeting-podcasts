# review/47 — Durable Object row-read budget guard for maximum LLM dispatch

**Maturity: L3 development-ready · optimization implementation ready for review ·
authored 2026-09-22**

Owner: LLM dispatch Worker maintainers. Scope: recurring v2 scheduler observation and its
queue-depth accounting. It does not change prompt construction, model routing, provider pacing,
or any configured LLM throughput cap.

## Problem and evidence

Cloudflare Durable Objects includes 5 million SQLite rows read per UTC day on the Free plan. The
meter resets at 00:00 UTC. Producer workflows started calling `GET /v2/stats` at their start and
finish, so the first calls immediately after the reset made the day-over-day rise visible.

The scheduled producer set makes roughly 82 snapshots per day. The former default endpoint made
all of these history-dependent reads:

- `SELECT state, COUNT(*) FROM jobs GROUP BY state` walked retained terminal rows.
- The queued-without-index diagnostic counted all queued jobs.
- The per-model diagnostic joined and grouped the queue.
- A no-selection claim used `COUNT(*)` over all queued jobs only to distinguish empty from
  non-empty.

At the observed backlog of about two thousand queued jobs, the snapshots and repeated mixed
admission claims can consume material row reads without dispatching one more LLM call. Retaining
completed rows for 38 days made the first query worse as normal throughput accumulated history.

## Decision

The automatic endpoint is a bounded summary. It contains the existing producer essentials:
queued depth, active bundles/calls, daily admissions, and the last claim reason. It reads only the
singleton `scheduler` row and active-bundle index population, which is bounded by
`MAX_ACTIVE_BUNDLES`.

The historical investigation response remains available only with `GET /v2/stats?detail=1`.
It is intentionally an operator action, not workflow telemetry. It may inspect retained queue
history, so it must not be used for polling, scheduled summaries, or alerting.

`scheduler.queued_job_count` is the summary's source of truth. Three SQLite triggers adjust it on
job insert, delete, and any state transition into or out of `queued`; this also preserves the
counter after an approved direct Data Studio state edit. The first new deployment access performs
one exact indexed `COUNT(*)` of legacy queued work, records the value, and marks the counter
initialized. This is a one-off migration cost, never a recurring diagnostic read.

`claimDispatchWindow` reads that singleton counter rather than counting queued rows when no job is
selected. It therefore retains its exact `no_queued_work` classification without paying queue
depth cost on a rejected claim.

## Throughput and quota budget

Production's configured dispatch ceiling stays unchanged:

| Guard | Value | Effect |
| --- | ---: | --- |
| `MAX_BUNDLE_JOBS` | 5 | Maximum calls in one claimed bundle |
| `MAX_BUNDLES_PER_UTC_DAY` | 1,400 | Maximum daily claimed bundles |
| Configured mechanical ceiling | 7,000 calls/day | No reduction in the dispatch ceiling |
| `MAX_ACTIVE_BUNDLES` | 3 | Bounded active-bundle summary population |
| `MAX_IN_FLIGHT_LLM_CALLS` | 8 | Existing provider-call concurrency guard |

At that ceiling the automatic snapshots are constant in historical queue depth. About 82 daily
snapshots now read a few bounded control rows each rather than repeatedly walking `jobs` and
`job_models`. Empty claims similarly read one `scheduler` row. This leaves the five-million-row
budget for bounded admission, polling, terminal-feed, and maintenance operations that actually
support throughput.

Do not raise `PURGE_BATCH_LIMIT` as part of this change. Its current value of 15 consumes at most
30 B2 deletion subrequests; a five-job dispatch can consume 10 more, exactly at the configured
40-subrequest safety bound. Faster terminal cleanup needs a new measured B2 and DO-write budget,
not a row-read reaction that risks reducing LLM throughput. Consumption acknowledgements already
remove ordinary completed work before the 38-day retention fallback.

## Implementation plan and completed work

1. Add a migration-safe, trigger-maintained `queued_job_count` singleton counter.
   - Completed in `workers/llm-dispatch-v2/src/coordinator.js`.
   - The migration is metadata-only. It neither changes an LLM job's content nor reprocesses any
     stored artifact, so no pipeline-version bump or catalog backfill is involved.
2. Make `/v2/stats` return bounded producer telemetry, move historical inspection behind
   `?detail=1`, and point the Python telemetry client at the default summary.
   - Completed in `coordinator.js`, `src/index.js`, and `scripts/llm_submission_telemetry.py`.
3. Replace the no-selection queued `COUNT(*)` with the singleton counter.
   - Completed in `coordinator.js`.
4. Extend the scale guard to call the recurring summary, seed its migrated counter, and prove
   the count remains exact through a claim.
   - Completed in `workers/llm-dispatch-v2/test/rows-read.test.js`.
5. Preserve detailed, manual diagnosis tests by calling `detailedStats`, and test the explicit
   HTTP opt-in.
   - Completed in the v2 coordinator, dispatch, and index tests.
6. Add a real billed-meter alert before relying on any quota percentage in operations.
   - Blocked on a supported Cloudflare billing-usage credential or native dashboard notification.
     On 2026-09-22, the available `CLOUDFLARE_API_TOKEN` authenticated to the GraphQL analytics
     API, but `AccountDurableObjectsSqlStorageGroups` exposed only `storedBytes`; it did not
     expose rows read. Cloudflare documents analytics as an operational measure rather than a
     billing source. The account billing-usage endpoint, which documents daily metered records,
     returned HTTP 403 for the available token and is documented as Alpha/Restricted. No
     `CLOUDFLARE_BILLING_API_TOKEN` repository secret exists.
   - Do not schedule GraphQL as though it were a billed-row meter. Once Cloudflare supplies an
     accepted billing credential or a durable usage notification, add a 30-minute external check
     with deduplicated warnings at 3.5M and 4.0M rows/day. Never use the bounded summary as a
     substitute for a billing meter.

## Acceptance and rollback

- `node --test workers/llm-dispatch-v2/test/rows-read.test.js` exercises `stats()` at 10x
  retained history and rejects cost that scales with history.
- Existing coordinator, dispatch, and HTTP tests preserve all job-state and diagnostics behavior.
- Rollback is safe: restore the producer to `?detail=1` only for a short manual investigation;
  do not reinstate it as scheduled workflow telemetry. The added columns and triggers are
  forward-compatible metadata and require no destructive schema operation.

## Operational follow-up

After deployment, compare Cloudflare's billed rows-read graph for the same UTC window with the
prior day, while watching producer summaries for stable `queued`, `active_calls`, and claim-reason
fields. If the real meter still trends toward 3.5M, inspect the bounded RPC list and Data Studio
queries before changing LLM bundle or concurrency controls. The configured 7,000-call ceiling is
preserved unless independent throughput validation authorizes a different write-budget envelope.
