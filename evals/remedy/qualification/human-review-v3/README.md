# Flash + DeepSeek filter, then human review

The maintainer approved this policy after the two runtime challenges: Flash and DeepSeek make
the initial proposals; exact validated agreement may produce a PR requiring human approval.
Disagreement, invalid output, failed requests and validator holds produce issues for human review.
Gemini/JEV are not called or required for this plan. The earlier four-role plans remain available
for historical replay. `decision.json` records the policy change and control-flow replay.

The new plan is `flash-deepseek-human-v3`. The maintainer subsequently explicitly authorized
production PR/issue creation. `production-authorization.json` records the exact instruction,
reviewed evidence hashes and runtime bindings. Config now selects `manual` / `live` with only
Flash and DeepSeek admitted as human-reviewed proposers. This supersedes the earlier shadow-only
policy in `decision.json`, which remains unchanged as historical evidence. These changes are
pending in PR #2541; they are not a claim of deployment or a completed production run.

This scoped permission does not assert AA scores, independent holdout qualification or automatic
feed publication. General model qualification remains unchanged. Changed prompts, profiles,
catalog, policy or authorization hashes fail closed. Qualified alias types remain empty and
all remedy PR merges require human approval. Existing PR/issue budgets and R2 CAS remain in force.

The runner stores a terminal failure as an explicitly marked request-bound observation with
`upstream_model: unverified`; it never manufactures a model vote or copies secret-bearing exception
text. Only the failure class and recipe/request bindings are retained in this failure envelope;
underlying Worker/local receipts preserve original call evidence. Repeated passes reuse that
failure instead of submitting the unchanged request again. A failed route can produce a manual
issue while its peer's existing Worker handle is pending; it never resubmits that pending request.
Storage/CAS bookkeeping failures still propagate. No exception is treated as a validated agreement.

## Offline policy replay

Generate source inputs into a new scratch folder using the retained Python builder (no credentials):

```bash
PYTHONPATH="$PWD" python evals/remedy/runtime_qualification.py --output /tmp/human-policy-inputs
PYTHONPATH="$PWD" python evals/remedy/replay_human_review.py \
  --observed evals/remedy/qualification/runtime-v2/observed-results.json \
  --inputs /tmp/human-policy-inputs/model-inputs.json
```

This reuses recorded initial answers only, runs the real validator and consensus with assumed
route eligibility in a sandbox, and performs zero provider calls. It does not make old observations
current admissions or qualify the changed operational plan. The twelve-case result is4 PR
candidates (RQ01/RQ02/RQ03 correct; RQ05 wrong) and8 issue candidates. RQ04's wrong proposal
now goes to an issue because the initial assignments disagree. Shared wrong agreements remain
possible; human PR approval remains required. Exact matching is deliberately unchanged, including
conservative issue fallback for otherwise equivalent scope fields or new-feed descriptions.

## Remaining steps for review/51

1. Finish CI/review and merge PR #2541. The implementation is not Shipped until merged.
2. Verify the normal Worker deployment and scheduled caller use the exact two registered routes,
   then inspect a bounded production cycle's observations, PR/issue creation and recovery.
3. Record operational results before declaring review/51's manual flow complete. Deployment and
   a production cycle have not been performed by this authorization change.

The maintainer's explicit production permission supersedes the previous separate publication
approval gate. It accepts shared-error risk because proposed feed changes require human merge;
it does not relax validation, enable adjudicators or qualify automatic alias actions.

P5's qualified-alias automation remains disabled and separately gated. This change does not
qualify alias types or enable automatic merging. Completing the manual flow is not a claim that
all P5 or all historical taxonomy research is complete. Another broad model bakeoff or the
review/49 judge league is not the next gate for this chosen human-review workflow.

The publisher independently rechecks the report's explicit role plan against trusted config and
recomputes consensus/selected assignment under that plan before the existing feed validator.
A report cannot select a different plan by changing untrusted metadata. Legacy reports retain
the old default. A real two-model agreement is tested through this publication boundary.

Verification:the full offline suite passed6,933 tests (17 deselected);57 latest focused checks
passed, including10 human-flow/failure scenarios and the publication-boundary test. Whole-repository
Ruff/format/diff checks passed. No source recording, media pipeline version or feed was changed;
old observations are retained and separated by the new plan/config/request fingerprints.
