# Runtime-contract challenge — expected outcomes awaiting review

This packet tests the actual P3 prompt/schema/role plan on twelve real historical source findings.
It is a bounded challenge, with source-family overlap and some reused recordings, not an
independent production-admission holdout. No expected answer or call budget is approved yet.

Retain `cases-draft.json` (curated selections and draft expectations) and the reusable builder:

```bash
PYTHONPATH="$PWD" python evals/remedy/runtime_qualification.py --output /tmp/p3-runtime-review
```

The output directory must not exist. The builder verifies the retained inventory/cohort hashes,
binds current source configs and runtime code/catalog hashes, checks the proposed decisions
through the real deterministic validator, and creates:

- `REVIEW.md`: case-by-case actions, complete feed sets, actual recording links, proof limitations
  and explicit current validator holds. Start with its summary table.
- `expected-decisions-draft.json`: unapproved expected decisions, separate from inference inputs.
- `model-inputs.json`: runtime evidence, actual `_chat_job` outputs and direct measurement payloads
  using shared route shaping. No expected answer, historical recommendation or acceptance enters
  the request. These are runtime selector/policy-aware inputs, not blind-owner discovery inputs.
- `record-provenance.json`: original UIDs/GUIDs and mirrored-view aliases. Native clips are counted
  once for this challenge; no archived meeting record is removed or changed.
- `run-plan-draft.json`: exact hashes, runtime bindings and unapproved request budget.

Generated files stay local/ignored. The builder has no execution mode, reads no credentials and
makes no provider requests. Rebuilding into a new directory is deterministic for the same sources.
A source/config/prompt/schema change requires a rebuilt packet and new reviewed hashes.

## How to review

For each case, confirm the action, complete feed set, exact recording scope, and whether the
runtime-visible evidence justifies the expectation. Use approve / change / omit by case ID.
Rationale wording is not an answer key. New-feed wording/slug alternatives require semantic
review; do not automatically classify equivalent wording as a new error or acceptance.

Four decisions need particular attention:

- **RQ04/RQ05:** the proposed Addison joint-event memberships preserve two/three owning feeds.
  The current validator rejects P&Z/CPAC abbreviations. Choose whether to accept a manual runtime
  fallback for this pilot or require a separate validator correction before calls. Do not drop a
  correct feed just to make the validator pass.
- **RQ07:** four distinct Dallas Joint Education Task Force recordings support a proposed new
  body/feed direction. Confirm its public-body identity and scope, or change the expectation to
  manual pending stronger proof. This review does not create a feed or authorize publication.
- **RQ12:** the prior candidate-forum archive decision has no literal runtime action, and that
  archive policy is absent from the initial prompt. Decide whether manual fallback is a fair
  conservative expectation for this input, or omit/revise the control before inference.

Other useful controls include RQ08 (four mirrored rows are only two native clips), RQ09 (Citizen's
Council is not automatically the municipal council or Minority/Women committee), and RQ11 (an
unnamed joint board is not enough to invent ownership). All draft expectations remain unapproved.

## Proposed measurement budget and scope

Twelve cases × Flash/DeepSeek = **24 initial requests**. Only actual disagreements may receive
Gemini/JEV = **up to 24 additional requests**, for **48 provider attempts maximum**. No retries,
HTTP redirects, SDK/Gateway retry loops, paid fallback or secondary BeatAPI account are included.
Maximum split: OrcaRouter 12; primary BeatAPI 36. If a request fails, preserve the failure rather
than retrying outside the budget. A new retry allowance would need separate approval.

The measurement transport is direct, single-attempt delivery of the runtime-generated payloads.
This keeps the attempt budget enforceable: the normal Worker queue has automatic retries and
cannot honestly be described as a 48-attempt cap. Worker end-to-end integration remains for the
later monitored run. Preserve exact route/endpoint/model/profile and identity checks, owned
provider pause/drain/restore, exact registered-route reservations, 65-second single-flight
spacing, default reasoning/no temperature, 16,384 chat output tokens and the 720-second hard
response deadline. JEV keeps its native systemone wire and has no chat output controls.

The Gemini/JEV payloads must be built from actual initial responses using runtime `_chat_job`,
`_adjudication_context` and `_jev_job`; never use draft expected answers as proposals. Their runtime
bindings are frozen in the run plan. No actual disagreement means no new qualification evidence
for those adjudicator roles. Do not force a fabricated disagreement to fill coverage.

Score semantic decisions, deterministic validator outcomes and final exact-consensus/manual
outcomes separately. All-manual is not a passing result on positive cases; schema validity alone
is not success. Preserve incorrect agreements, invalid outputs, identity failures and all holds.
No passing evaluation envelope, AA score, independent-holdout count or production admission is
created by preparation or by approval of this challenge. Publication and activation stay gated.

The offline preflight also corrected stale Sol/GLM option captions in the selected runtime JEV
plan to neutral initial A/B captions; legacy captions stay unchanged. That exact updated runtime
contract is what this packet binds, not the previous evaluated body-selection/JEV question set.
