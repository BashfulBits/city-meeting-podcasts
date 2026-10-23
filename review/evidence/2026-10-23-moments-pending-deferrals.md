# Moments pending-deferral investigation — October 23, 2026

Follows the [invalid-cache investigation](2026-10-23-moments-invalid-cache.md).
The cache/schema recovery patch is [PR #2532][pr], open separately from this investigation.
No production objects were changed, cancelled, reconciled, or resubmitted by this audit.

## What the 2,889 count means

The [15:46 moments run][moments] reported 2,889 `llm-pending` stage deferrals. Its
`_admit_r6_dispatch` considers any stored `JobHandle` pending, including client-side deferred
requests that have never reached the coordinator. It skips fresh producer submission in either
case. The figure therefore is not a count of in-flight calls or even of admitted DO jobs. It is
also an older stage-observation count, not an authoritative current unique-recipe inventory.

## Read-only live audit at approximately 18:12 UTC

Listed B2 pending-pointer keys and selected a bounded sample across five relevant model pools:
300 from Gemini 3.5 Flash Lite, up to 150 each from Gemini 3.1 Flash Lite, GLM, DeepSeek, and Kimi.
Deduplication left 767 canonical reads; all succeeded. Of those records, 509 were pending moments
records. This model-stratified sample is not an unbiased census; do not extrapolate its
percentages to the earlier 2,889 observations.

One batched DO status read covered the 183 submitted moments references:

| Current classification | Sample records |
| --- | ---: |
| Client deferred, not submitted to DO | 326 |
| DO queued | 154 |
| DO leased | 1 |
| DO completed, still pending client-side | 21 |
| DO failed, still pending client-side | 7 |
| Submitted reference missing from DO | 0 |
| Total | 509 |

All 154 sampled queued jobs had zero attempts: they were waiting for a first claim, not abandoned
in-flight calls. The seven failures exposed only generic `job_failed`, with no typed terminal
reason, so their provider-specific causes are not established here. Completed/failed entries
need normal reconciliation; they are not evidence that the coordinator lost their jobs.

508 of the 509 client records originated October 22 and one October 23. B2 modification times
ranged from October 22 to October 23 17:16. Of the 509 pending records, 438 lacked the schema
name. All 326 unsubmitted moments capsules lacked schema and output-mode metadata, retained a
16,384-token output budget, and had no serialized backup models. Missing output-mode metadata on
an already-submitted handle alone is not diagnostic: its durable request payload is authoritative.

Eight queued request payloads were then read at their source-defined B2 paths: five lacked both
schema and `route_max`, while three retained both. All eight carried `max_tokens: 16384`.
With `route_max`, that is a scheduling reservation; without it, it becomes an answer-length cap.
Prompt/content text was not emitted or retained in the audit results.

## Why the client-side backlog is not submitting now

The bounded moments ingress preflight reported:

- Purpose closed: `purpose_write_budget_exceeded`.
- Moments usage: 4,798/4,800 daily write units, only two remaining.
- Moments queued depth: 1,403/3,200; its queue-depth ceiling was not binding.
- Global ingress: 2,761/4,000 jobs. DO rows: 52,850/90,000 safe stop. Those global gates were open.

Released shared reservations do not override the independent lane ceiling. The remaining two
units cannot fund a fresh job. More claim lookahead cannot read unsubmitted B2 capsules into the
DO or bypass this admission gate. The lane's UTC-day budget must reopen before further admission.

## The sweep is making progress, but also encounters obsolete policies

The [15:48 deferred sweep][sweep] admitted 493 fresh moments jobs (247 + 13 + 218 + 15 across its
batches), deferred 967 attempts for purpose write budgets across moments/tags, and rejected 800
legacy moments attempts with `model_not_in_lane`. These are operation observations, not additive
unique-record counts. Its telemetry also observed 410 completed moments polls and 24 failed
moments polls; retries can repeat observations. No missing submitted refs were reported there.

The old immutable policy capsules can contain models removed from the current lane. Current
sweep code classifies such rejections as terminal, removes matching pending records with audit
markers, and leaves the producer to re-plan. Retrying the same obsolete capsule indefinitely is
not a solution. This audit did not independently verify every one of the 800 retirements.

The rotating safety audit selected 1,000 total recipes, including 582 moments records. It is a
complement to the normal terminal feed, not a full all-lane pending census. The sweep is scheduled
six-hourly at 05:30/11:30/17:30/23:30 UTC; Actions delays and serialization affect actual starts.
Some terminal/client-state lag between runs is expected. No larger audit or shorter cadence is
justified by a missing-reference problem in this sample.

## Additional reproducible metadata-loss defects

These are distinct from PR #2532's schema repair:

1. `_serialize_policy` / `_deserialize_policy` omit `backup_models` and
   `backup_after_attempts`. A policy with a real backup and threshold round-trips to an empty
   backup pool and no threshold. Other policy fields also need a fidelity audit.
2. `_with_sweep_deadline` constructs a new `DeferredLLMRequest` without its existing `timeout`
   or `max_tokens_mode`. It should preserve the capsule while changing only the intended policy.
3. `_job_from_deferred_handle` rebuilds messages, reservation, policy, and schema but does not
   carry the explicit timeout or output mode into the submitted job.

An offline reproduction started with a moments primary plus GLM backup, threshold two,
`route_max`, and timeout 720. Persistence lost backup/threshold; deadline adjustment lost mode and
timeout; rebuilding yielded `max_tokens: 16384` with no mode or explicit timeout. Without an
explicit timeout, the backend may still apply its configured default; this is metadata loss,
not proof that a request becomes unbounded.

## Follow-up

Preserve the full request/policy capsule through persistence, deadline adjustment, and rebuild.
Test a complete refuse/persist/reload/sweep/submit round trip, including output mode and backup
eligibility. Keep request sizing and provider accounting conservative: restoring route_max must
not silently treat an outdated reservation as adequate for every route.

For legacy records, re-plan from authoritative producer inputs under the current lane rather
than guessing lost route policy, widening physical pins, or reviving prohibited models. Use
conditional replacement only for proven unsubmitted deferrals; retain genuinely submitted jobs
and valid completed artifacts. Classify pending work separately as unsubmitted, queued, leased,
terminal-awaiting-reconciliation, and unknown. Do not treat all pending observations as running.

This follow-up is not implemented in PR #2532. No quota, concurrency, scan, model-policy ceiling,
or production-cadence change was made during this investigation.

[pr]: https://github.com/BashfulBits/city-meeting-podcasts/pull/2532
[moments]: https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/39981197227
[sweep]: https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/39981389351
