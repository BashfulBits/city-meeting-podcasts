# Varied backfill stall diagnosis

Inspected 2026-10-13 02:43 UTC (2026-10-12 9:43 PM America/Chicago).
This is a read-only operational investigation plus this report. No provider inference calls,
production settings, pauses, quotas, active processes, or historical observations were changed.
Astra is excluded from the eight-candidate comparison; its historical files remain intact.

## Actual case coverage

The frozen manifest contains 113 questions. Count completed logical model/case pairs once,
pooling Gemini primary and secondary credentials. Failed means a provider attempt did not
produce a validated answer; these are separate from completed-but-incorrect answers.

| Candidate | Answered | Failed and still unanswered | Never called |
| --- | ---: | ---: | ---: |
| Gemini 3.5 Flash-Lite | 113 | 0 | 0 |
| Kimi K3 | 113 | 0 | 0 |
| Full NVIDIA GLM 5.3 | 104 | 9 | 0 |
| OrcaRouter GLM 5.3 Flash | 101 | 12 | 0 |
| BeatAPI GPT 6.1 Sol | 96 | 17 | 0 |
| BeatAPI DeepSeek V4.1 Flash | 68 | 45 | 0 |
| Gemini 3.7 Flash, pooled keys | 8 | 4 | 101 |
| Gemini 3.8 Flash, pooled keys | 11 | 4 | 98 |
| Total | 614 | 91 | 199 |

The latest inspected scored snapshot was `/tmp/varied-monitor-score-20261013T024030Z.json`.
Historical observations are in this directory's `batches/` tree. The prior hourly explanations
overemphasized provider congestion: congestion occurred, but it does not explain candidates
whose entire case set is excluded by the retry-selection logic.

## Proven scheduling defect

`run.py:observations()` returns completed logical pairs and failed physical candidate/case pairs.
Without `--retry-failed`, `main()` skips both. This is deliberate invocation-level protection;
the orchestrator must explicitly choose when to retry failed observations.

The active `/tmp/monitor-varied-gapfill.sh` calls the runner without `--retry-failed`.
Its retry branch is reached only after all eight candidates have 113 completed answers.
A failed answer prevents that condition, while the normal branch permanently skips it.
Therefore the retry branch cannot recover the very failures that prevent entry to it.

An offline inspection of the actual runner's `observations()` and frozen manifest verified:

| Candidate | Eligible under current invocation | Eligible with retry enabled |
| --- | ---: | ---: |
| Full GLM | 0 | 9 |
| Flash GLM | 0 | 12 |
| Sol | 0 | 17 |
| DeepSeek | 0 | 45 |

Completed cases stay excluded in both modes. All four candidates have already been called on
every question. Repeated `Batch ended; provider calls 0` therefore does not establish a quota
or provider-busy condition.

## Gemini daily quota is a separate blocker

Read-only Worker status at 02:43 UTC reports `rpd_used=20`, `rpd_remaining=0` for all four
Gemini 3.7/3.8 primary/secondary routes. Each configured route has a 20-request daily cap.
The evaluator also reserves four requests per route for production, so even an otherwise unused
independent pair of accounts provides at most 32 evaluation requests per model per day.
With 105 and 102 answers outstanding respectively, that is at least four fresh daily allowances
if every call succeeds and the accounts provide independent capacity. Production usage,
failures, or shared provider projects can extend this. Configured distinct account IDs alone
do not prove independent upstream project quotas.

Worker reset timestamps point to 07:00 UTC on October 13, or 2:00 AM America/Chicago.
The shell orchestrator runs only 96 cycles, sleeping five minutes after each cycle. Its current
position and empty eligible sets make it likely to exit before that reset. Its hourly reporting
automation observes results and is explicitly prohibited from starting new calls.
This is not a reliable cross-reset execution strategy.

At the same read-only check, Sol and DeepSeek had 1,000 and 995 daily requests remaining in
the Worker ledger; GLM Flash had 800. These routes were individually idle. Their empty eligible
sets, rather than exhausted daily quota, explain why ordinary batches cannot recover failures.

## Actual route errors

- BeatAPI Sol: all 17 recorded failed observations contain provider `InternalServerError`.
  The response says generation failed temporarily and a failed request is not charged.
- BeatAPI DeepSeek: 42 recorded server errors and four generic attempt-limit errors across
  its history; 45 distinct failed cases remain unanswered. Four later calls succeeded.
- GLM Flash: 12 unanswered cases have generic `immediate provider-attempt limit reached` in
  the JSON. Batch logs expose the underlying HTTP 429, classified `gateway_limit`, rule
  `cf-aig`, with no Retry-After value. The final budget exception obscures this real cause.
- Full GLM: 11 historical request timeouts around 600 seconds and one generic attempt-limit
  failure; nine distinct unanswered cases remain. Other requests succeeded before and after
  failures. Successful end-to-end median is about 125 seconds.
- Gemini: provider 503 high-demand responses, timeouts, attempt-limit errors, and one
  `response case ID mismatch`. Daily quota exhaustion now prevents fresh attempts.

These counts describe immutable attempt observations; repeat observations are not distinct
questions. They do not establish a permanent route outage. Recurring failures do establish
availability concerns that must be retained alongside accuracy.

Estimated prompt input spans 1,402 to 6,482 tokens. Failed full-GLM median input is 3,286 versus
3,081.5 for successes, and failures include a 1,530-token prompt. DeepSeek also succeeded on
a 6,126-token input. This does not support a simple context-limit explanation or automatically
raising the already approved 600-second timeout and 16,384-token output cap.

## Recommended evaluator-only fixes

1. Replace the global all-success retry gate with per-candidate work selection. Exhaust untouched
   cases, then explicitly retry unanswered failures whose cooldown has expired using the existing
   retry option. Keep completed logical pairs protected across both Gemini keys. Preserve every
   prior observation. Bound retry passes and total calls; never add an unconditional retry loop.
2. Schedule quota exhaustion against each route's returned reset time. Persist pending work and
   next eligibility, resume after resets, and stop with an explicit waiting/stopped reason when
   the run horizon ends. Do not use a fixed 96-cycle loop as a substitute for quota resumption.
3. Capture sanitized HTTP status, original provider error, Retry-After, and retry eligibility in
   evaluation observations. Record these before the backend's generic attempt-budget exception
   can obscure them. A busy route, daily exhaustion, transport failure, invalid answer, and no
   eligible cases must have distinct report states.
4. Apply a bounded evaluation cooldown after recurring server errors or gateway rate limits,
   honoring Retry-After when supplied. A 30-minute cooldown is a reasonable initial operational
   proposal for repeated errors; it defers evaluation requests without pausing production.
   Retry full-GLM timeouts once within existing bounds before considering any larger timeout.
5. Retain the orchestrator under `evals/remedy/` with the test set and outputs. Add meaningful
   offline tests proving recovery from a failed case, completed-case deduplication, one-key
   success suppressing the other key, cooldown and quota-reset eligibility, and bounded stopping.
6. Report the eight-candidate denominator of 904, including successes, failed questions awaiting
   retry, never-called questions, and next actionable time. The current shell's printed
   denominator 1,017 still includes Astra even though its completion predicate excludes it.

Priority is the retry deadlock, then reset-aware resumption and error visibility. Retain the
current shared quota accounting, production headroom, model controls, request/output budgets,
and approved frozen cases. Changes in provider limits, production routing, or new paid calls are
not required to fix the verified scheduling defects.
