# LLM pending accounting investigation — 2026-10-18

## Evidence and limits

The producer's `llm-pending` count measures client handles, not live coordinator rows.
A large Gemma sibling backlog cannot establish that another lane lacks ingress capacity.

The October 18 deferred sweep
([run 39137214653](https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/39137214653))
captured 7,017 queued jobs, open enqueue/claim admission, and 9,981 billed writes against the
90,000-row safe stop. Its queued-model index contained Gemini 3.6 Flash, Gemma 31B, Qwen 3.8,
Kimi K3, and DeepSeek V4.1 Flash; none of the agenda lane's configured models appeared.
This distinguishes the agenda producer's pending state from an agenda queue awaiting capacity.

A read-only full canonical-registry classification on reviewed main
([run 39153472920](https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/39153472920))
read all 47,252 records without omissions or unavailable reads. It found exactly 667 agenda
handles, all submitted-v2, all naming Nemotron Ultra. Their original creation dates range from
September 25 through October 7 (278–564 hours old at observation). No handles were cancelled or
removed by this classification. Individual DO states still require a keyed batch observation;
age alone never authorizes recovery.

## Confirmed defects

1. In a fresh process, `_register_known_contracts()` omitted `moment-judge` and
   `judge-answers-v1`. A completed judge response therefore failed local resolution before schema
   validation. The terminal-feed path converted generic result-resolution errors, including local
   setup/storage errors, into provider-terminal failures. It could discard valid handles, spend
   retries, and cause unnecessary re-enqueues. Registration and error classification are fixed.
   The same audit found the tournament/R5 comparison, P3 chat, and title-equivalence contracts
   missing too; startup now registers those as well. Direct-only body decisions stay outside
   this dispatch-registration scope.
2. The terminal feed alone cannot repair a submitted client handle whose terminal event was
   already passed or whose reverse pointer was lost. Producers trust that canonical handle and
   suppress another submission. A bounded rotating audit complements the feed rather than replacing
   it with full-registry polling. Weekly maintenance also refreshes pending pointers while it
   already has the full canonical snapshot, covering the case where every advisory pointer was lost.
3. Four producer reservation gates latched exhaustion when committed plus merely reserved slots
   filled the cap. A failed or cached reservation could release its slot without reopening the lane.
   Only committed dispatches now latch exhaustion; explicit ingress closures still remain closed.
4. Batched tagger, production prelabeler, and shadow visits could spend episode allowances on
   already-submitted handles. They now distinguish newly prepared batches from pending replays.
   Shadow slots are also returned on cached completion or failure. Agenda now mirrors the locator's
   current-recipe lookup before reserving new-work allowance.
5. A consumer could see a completed DO row after another consumer persisted its result and removed
   its B2 result object. Polling now prefers an already-completed canonical record in that race.

Sibling outstanding backpressure remains per model; a Gemma backlog does not close other models.
Runner telemetry now shows both outstanding packets and remaining room per current sibling model.
No route policy, model choice, provider quota, or configured producer cap is raised.

## Retained accounting telemetry

The existing Actions JSONL artifact and step summary now carry these observations without new
coordinator counters, tables, or telemetry writes:

- Final runner slot cap, used slots, unsettled reservations, and closure flag per producer.
  Leftover reservations, cap overrun, and closure below cap are explicit diagnostics. A closure
  below cap can be an intentional ingress stop; correlate its recorded ingress reason before
  treating it as a reservation bug. Prepared slots are not fresh coordinator admissions.
- Judge outstanding packets and room per model, including old models with pending packets;
  retired models have no active limit. Anchor/adjudicator purpose gauges and remaining run caps
  separate outstanding backpressure from a tighter producer cap.
- Per-task poll outcomes: completed, pending, explicitly missing, failed, and unknown. Repeated
  observations are labeled rather than presented as distinct jobs. Missing observations are
  separate from successfully cleared client handles, preserving race-protection visibility.
- Indexed audit selection/loaded counts, task breakdown, omitted and unavailable records;
  unresolved sweep reads are flagged rather than counted as proven live coordinator work.
- Start/end UTC-day ingress counts, purpose quotas, DO row usage, and enqueue/claim gates from
  the already-requested lightweight stats response. UTC-day labels prevent interpreting a
  midnight counter reset as negative ingress. No additional stats requests are introduced.

Stage `reused` counts include no-op/out-of-scope visits and are now labeled accordingly; the
25,188 reused moment stage items were not evidence of 25,188 completed extraction calls.
Telemetry contains grouping identifiers and counts, without prompts, outputs, or job ids.

## Cost and recovery contract

- The ordinary terminal feed remains the fast completion path.
- Each sweep additionally reads at most 1,000 indexed pending recipes, rotating a cursor on B2.
  Listing advisory keys does not read prompt bodies or scan the DO job table. Audit observations
  remain bounded even when terminal history grows. Unavailable observations retain the handle;
  rotation prevents one unavailable record permanently starving unrelated recipes.
- Batch polling uses existing primary-key SQL reads. Its additive `missing` list proves that a
  requested id is absent without adding tombstones, per-job counters, or DO row writes. Old Workers
  that omit the list retain the conservative unknown/retry behavior.
- Only explicit missing ids can release their matching client handle without spending a provider
  retry. Deletion is fenced by the existing per-recipe coordination lease and expected reference.
  A concurrent replacement or completed result is preserved. Queued and leased jobs remain intact.
- Completed results use the existing durable-save-before-retirement path. Typed provider failures
  and malformed model responses keep their existing bounded recovery policies. Generic local
  read/setup failures retain both the handle and terminal-feed position.
- `--pending-audit-limit 20000` is a manual catch-up option under the normal wall-clock budget;
  scheduled runs remain at 1,000. The Actions input is passed through a quoted environment variable.
- No pipeline version or recipe changes; no catalog invalidation or blanket backfill. Recovering a
  genuinely missing job merely lets its producer reconsider the original immutable recipe under
  existing admission limits. Existing failure markers are not indiscriminately reset.

## Verification and operational acceptance

Regressions cover clean-process contract registration, missed reverse pointers/events, cursor
rotation, entirely lost indexes, explicit absence versus incomplete responses, replacement races,
transient result-read errors, temporary reservation saturation, and cached producer work.
The coordinator test asserts `total_changes()` is unchanged by polling present and missing ids.

After deployment, run the bounded catch-up sweep and inspect `llm_pending_audit`,
`missing_cleared`, terminal results, unknown observations, and fresh admissions in subsequent
producer runs. Keep any unresolved ids for diagnosis; do not cancel the real Gemma or legacy
moment-judge backlog to make counters look smaller. The read-only classification above is not
itself evidence that live recovery has completed.
