# Reproduce remedy evaluations or add a model

Keep unique inputs and actual outputs, not an expanded run directory. All commands below are
offline unless explicitly described as execution. Historical approvals do not authorize new calls.

## Retained evidence and generated views

- Frozen 60-case cohort/gold and the original prompt/schema remain in their existing locations.
- `results/accepted-pairs-2026-10-24/evidence.json` retains 298 initial answers, 24 judge answers,
  two invalid full-GLM response texts and attempt/profile/identity metadata with original hashes.
- `results/accepted-pairs-2026-10-24/judging-inputs.json` retains only additional retrieved evidence,
  changes from original cases and blinded proposal references. Both expanded packets are rebuilt
  from these inputs and the actual retained answers; their original approved byte hashes must match.
- `qualification/runtime-v1/{answers,continuation-answers}.json` and runtime-v2 `answers.json`
  retain every actual normalized runtime answer, status, identity, usage and source-result hash.
  Shared request profiles are stored once. Derived scores, case summaries and comparison tables
  are recomputed rather than duplicated in each observation bundle.
- `qualification/runtime-v2/conclusions.json` records compact completion facts; maintainer case,
  budget, retry, identity and manual-production approvals remain separate from raw gold.

Normalized files are not original transport receipts. Original hashes identify local sources;
no claim is made that normalization preserves raw receipt bytes or proves physical serving weights.
Generated packets/requests, score snapshots, readiness bundles, dated polling/restart helpers,
logs, downloaded pages, controller state and raw responses remain local and ignored. Cleanup removes
these files only from Git's index; it does not delete the local archive. Re-fetching websites would
produce new evidence, not reproduce the historical snapshot.

## Rescore the original cohort and adjudications

```bash
PYTHONPATH="$PWD" python evals/remedy/accepted_pairs.py score > /tmp/remedy-pairs.json
```

This checks cohort/gold/prompt/schema identities, validates retained answers and recomputes all ten
model pairs on identical answered-case intersections. Coverage remains 60 each for Sol, Flash,
DeepSeek and Gemini, and 58 for full GLM plus two contract holds. The approved pair cascades remain
four correct resolutions/three manual disagreements and five correct resolutions respectively.
Original scores and explicitly accepted exceptions remain separate.

Rebuild both expanded judging packets into a NEW directory:

```bash
PYTHONPATH="$PWD" python evals/remedy/evidence_archive.py packets \
  --output /tmp/remedy-rebuilt-packets
```

The output mirrors repository-relative paths. Its packets must match the SHA256 values in the
unchanged `adjudication-approval.json`. To inspect the historical 24 job payloads in a separate clean
checkout, copy those rebuilt packet files into their matching paths, then run the retained
`results/accepted-pairs-2026-10-24/run.py` **without** `--execute`. It validates 24 jobs without
network/credentials. Do not execute an old approved run against its original output directory.
The retained `prepare.py`/`expand.py` scripts generate disagreements and retrieve evidence for a
new run; the archive builder reproduces old inputs without re-fetching websites.

## Rescore runtime prompts and the selected human filter

Generate fresh local inputs using the real runtime factories and retained twelve-case spec:

```bash
PYTHONPATH="$PWD" python evals/remedy/runtime_qualification.py --output /tmp/remedy-runtime
PYTHONPATH="$PWD" python evals/remedy/score_runtime_qualification.py \
  --plan /tmp/remedy-runtime/run-plan-draft.json \
  --compare-baseline evals/remedy/qualification/runtime-v1/answers.json \
  --compare-baseline evals/remedy/qualification/runtime-v1/continuation-answers.json \
  --candidate evals/remedy/qualification/runtime-v2/answers.json \
  --acceptance evals/remedy/qualification/runtime-v1/rq06-observed-acceptance.json
PYTHONPATH="$PWD" python evals/remedy/replay_human_review.py \
  --observed evals/remedy/qualification/runtime-v2/answers.json \
  --inputs /tmp/remedy-runtime/model-inputs.json
```

When no generated approved-expectation view exists, comparison verifies the rebuilt draft's hash
against the original maintainer-reviewed expectations before scoring. It grants no call authority.
The selected policy replay remains four PR candidates (three correct, one shared wrong RQ05) and
eight human issues. These reused hard examples are not independent production qualification.
Original model decisions are never repaired to improve scores. RQ06 acceptance is scoped and
separate from raw gold; other wrong outputs are not implicitly accepted.

## Evaluate an additional model

```bash
PYTHONPATH="$PWD" python evals/remedy/accepted_pairs.py prepare \
  --model exact-upstream-id --output-cap 16384 --output-dir /tmp/new-model-requests
PYTHONPATH="$PWD" python evals/remedy/accepted_pairs.py score \
  --additional-model new-model-profile=/tmp/new-model-answers.json
```

Preparation generates all 60 original-prompt requests/schema/hashes with no gold in model messages.
The scoring input is a JSON list of `{case_id, answer}` rows. Use a new label for each provider or
profile; never replace historical answers. Failed/invalid attempts affect coverage, not answered-case
quality. A valid wrong answer must be retained. Keep actual new outputs and minimal provenance.

For live execution, obtain approval for the exact route/account/endpoint, returned IDs, input/output
limits, case count and deadline. Create a fresh directory and wrapper around the retained common
chat runner `results/gemini-replacement-2026-10-24/run.py`, binding OUT, MODEL, BASE_ROUTE_ID,
CANDIDATE_ROUTE_ID, PROVIDER_OVERRIDE, API_BASE_OVERRIDE and the approved profile. The small full-GLM
provider wrappers illustrate partitioning and streaming; they are not new-run authorization.
Use the existing structured shaping/schema checks. Native JEV job construction is retained in
`results/worker-qualification-2026-10-19/observe.py` and the shared adjudication builders.
For a runtime-prompt challenge use `runtime_qualification.py`, `prepare_runtime_reevaluation.py`,
`run_runtime_qualification.py` and `score_runtime_qualification.py`. Those tools bind the selected
runtime role map; testing a different role/model requires a newly reviewed mapping and fresh hashes,
not substituting a model ID in a previously approved packet. Old approvals are immutable history;
a source/hash mismatch must not be bypassed or silently refreshed to reuse past call authority.

Primary accounts only; the secondary BeatAPI account is banned. Use suppressed `citypods-env`
credentials, existing provider pauses/drain/owned-only restoration, exact registered-route
reservations and no false charging for unregistered candidates. Preserve 65-second provider
single-flight spacing, the approved output cap and 720-second deadline; no paid fallback or
implicit retries. Never duplicate an active controller or unknown request. Stop forbidden/access
errors; retry only under explicit run authority. Measurements do not authorize production routing.

## Archive a new completed run and validate changes

`evidence_archive.py archive-observations --source ORIGINAL --output NEW` factors shared profiles
while preserving all actual response fields and source hashes. `archive-packets --output NEW`
is the one-time extraction command for the original judging snapshots. Neither overwrites files.
`accepted_pairs.py compact` similarly extracts initial answers from an existing complete local run;
`materialize --output-dir NEW` rebuilds normalized scorer inputs for historical adapter inspection.

Run `ruff check .`, `ruff format --check .`, `python scripts/compile_changelog.py --check` and
`pytest -q`. The retention regression checks packet hashes, answer/profile reconstruction and
actual clean-checkout scoring. Production's hash-bound manual PR/issue authorization is separate
from general model qualification; automatic remedy merging and alias grants remain disabled.
