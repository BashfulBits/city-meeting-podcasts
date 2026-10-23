# Moments invalid-cache investigation — October 23, 2026

Investigation only. No production cache objects were deleted or resubmitted. Code observations
apply to main at `97770b44`, including merged throughput PR #2531. Two offline reproductions used
in-memory storage and mocked HTTP; neither contacted a provider.

## Evidence and causes

The [13:47 run][earlier] reported 47 moments errors and the [15:46 run][later] reported 89.
Both reported zero newly queued batch jobs. The later producer telemetry classified all 89
observed extraction calls as `cached_complete`, with zero new candidates and no ingress attempts.
Errors included non-JSON content, unexpected `summary` / `results` fields, and missing decision
fields. These are 89 stage errors, not a verified count of unique affected meetings or recipes.
The same run also reported 2,889 pending deferrals; those are a separate population.

Two code defects form a reproducible failure chain:

1. `LiteLLMBackend.enqueue_batch` builds deferred handles without `structured_output` in the
   whole-request HTTP 429 path and the per-job deferred ingress-rejection path. This includes
   `purpose_write_budget_exceeded`. Its preflight-closed path already preserves the contract;
   the remaining refusal paths do not. The issue is generic to structured callers, not moments
   alone.
2. `_job_from_deferred_handle` in the sweep restores a schema only when the handle carries it.
   Missing metadata therefore produces an unstructured replacement request. Reconciliation
   validates against a schema only when its name is present. Wrong-shaped answers can become
   durable completed results. Valid JSON alone does not establish the moments contract.
3. The batching collector, ordinary backend, and batch enqueue all return completed cache hits
   without revalidating them against the caller's current contract. `MomentsStage` later runs
   `response_payload`, rejects the answer, and logs/defer-reports the error, but leaves the cached
   completed object intact. The next run receives the same bad answer again. The pending sweep
   does not revisit it as pending work.

Reproduction A supplied a structured moments job and simulated each refusal independently.
Both produced `handle.structured_output is None`; the sweep rebuilt each with no contract.
Reproduction B cached `{"summary": []}`, called the real backend three times for the same
structured recipe, and validated each result with the moments parser. All three returned the
same cached result and raised validation errors; the cache stayed completed. The existing
`discard_completed_result` helper conditionally removed the exact bad result and made the recipe
eligible for fresh admission. Fresh schema-aware reconciliation rejects that same response.

The mechanism matches the observed schema errors. Individual stored records were not audited,
so this does not establish that every one of the 89 errors originated in the refusal-path bug.

## Impact now

- Affected recipes cannot finalize new summary points, pull quotes, or decisions from these
  answers. Existing candidates, if any, are not replaced by invalid output.
- Later runs repeat input/storage reads and validation without obtaining a new model answer.
  Those cache replays do not themselves spend new provider tokens, request quota, or DO ingress.
- The original malformed generations did spend provider capacity without delivering usable
  moments. Schema-losing deferrals can keep creating more such results.
- A successful workflow status does not mean its moments stage had no errors; per-episode
  exceptions are intentionally isolated. Other meetings and stages continue.
- PR #2531 improves scheduler selection but does not repair response contracts or bad caches.

## Targeted fix and expected effect

Preserve the original schema name on every deferred-handle path and through serialization and
sweep reconstruction. Repair known legacy moments deferrals that lack this metadata before
resubmitting them; use a verified task/contract association, not guessed output-field aliases.

When local moments parsing/schema validation rejects a completed result, persist bounded failure
accounting and conditionally evict only that exact result. Reuse the existing retry gates; do not
create unlimited new calls on every run or count repeated reads of one bad answer as new failures.
Do not evict caches for transport/read errors or intentionally rejected quality/coverage outcomes.
The chapter stages already use the conditional completed-result removal helper.

The next eligible producer visit can then submit genuine replacement work under the same recipe.
Successful replies proceed through unchanged chapter coverage, exact-transcript grounding, and
admission/calibration checks. Better valid completions do not guarantee public clips: those later
checks remain independent. New retries consume real free quota and must wait when ingress or
provider budgets are exhausted. No lane/global ceiling increase is required or implied.

Use a targeted recovery, with no pipeline-version bump or catalog-wide invalidation. Validate
HTTP 429 and per-job refusal round trips, legacy metadata repair, repeated invalid-result retry
limits, concurrent cache replacement protection, and successful same-recipe completion afterward.
Measure fewer repeated cache-validation errors and more finalized moments, separately from new
job admissions, Worker completions, and the existing pending backlog.

[earlier]: https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/39964198701
[later]: https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/39981197227
