# Concise-prompt reevaluation — complete

The full twelve-case run completed on October 27 UTC (October 26 Central). It used 39/48 attempts:
38 completed judgments and the preserved original OrcaRouter 429. The single explicitly approved
retry succeeded. No new transport, input-length, output-contract or identity failures occurred.
Both owned provider pauses were restored; the controller is inactive with no unknown requests.
The completion heartbeat was deleted. No routing policy, admission, deployment or merge changed.

`observed-results.json` retains normalized judgments, usage, actual identity/profile metadata and
original intent/result hashes. `completion-summary.json` records the comparison and conclusions.
Raw requests, provider replies, pause receipts, controllers and generated packets remain local.
The compact record is not a raw transport receipt or a model-weight attestation.

## Prompt correction worked for size, not measured accuracy

Every adjudicator packet contains the taxonomy once. Both formerly oversized JEV cases completed:
RQ09 used 12,024 provider input tokens and RQ10 used 11,858. On the four JEV cases answered in both
runs, reported input tokens fell roughly 71–74%. The full taxonomy and source history were retained.
Generation was Python-driven and two independent eight-file packet builds were byte-identical.

Initial and adjudicator instructions were made concise and the contradictory restriction on
proposed new-feed slugs was clarified. Thus this is a comparison of prompt revisions, not a
controlled causal estimate of duplication alone. The same cases, sources, models, schemas,
validator, exact-consensus comparator and raw expectations were used. This repeated historical
challenge is not an independent production holdout or general production error-rate estimate.

## Semantic results

The reviewed view below preserves the original raw scores and applies the existing RQ06 exception
only where the new structured proposal matches a previously reviewed RQ06 assignment. It does
not accept a new wrong answer or change gold. The matched comparison is computed by the Python
scorer, not copied from an earlier notification.

| Role | Baseline raw correct/wrong | Concise raw correct/wrong | Concise reviewed wrong |
|---|---:|---:|---:|
| GLM Flash initial | 6/6 | 6/6 | 5/12 (41.7%), plus 1 accepted RQ06 exception |
| DeepSeek initial | 6/6 | 6/6 | 5/12 (41.7%), plus 1 accepted RQ06 exception |
| Gemini adjudicator | 2/5 on 7 answers | 2/5 on 7 answers | 4/7 (57.1%), plus 1 accepted exception |
| JEV selected proposal | 2/3 on 5 answers | 2/5 on 7 answers | 4/7 (57.1%), plus 1 accepted exception |

Both concise adjudicators answered the same seven cases: RQ04/RQ06/RQ07/RQ08/RQ09/RQ10/RQ12.
They were both wrong on RQ04/RQ09/RQ10/RQ12. RQ06 matches the accepted union; RQ07/RQ08 are correct.
Neither caught RQ04's omitted P&Z ownership. RQ05 was an initial wrong agreement with no judges.

The apparent JEV headline increase from 2/5 reviewed errors to 4/7 reflects changed coverage:
RQ09/RQ10 previously lacked answers. On each role's baseline/concise answered-case intersection,
quality is unchanged: Gemini 4/6 wrong in both runs; JEV 2/4 wrong in both. All 12 initial cases
are matched and each initial model's semantic verdicts also remain unchanged. Different conditional
case sets must not be ranked as though they were identical samples.

## What would become PRs versus issues

These are sandbox candidates assuming route eligibility, not actual GitHub objects or admissions.

| Case | Final semantic assessment | Concise sandbox output |
|---|---|---|
| RQ01 | Correct existing-feed union | PR candidate |
| RQ02 | Correct existing-feed union; initial proposals now agree exactly | PR candidate |
| RQ03 | Correct existing-feed union | PR candidate |
| RQ04 | Wrong: recording inclusion targets Council only, omitting P&Z | PR candidate |
| RQ05 | Wrong: City Council union omits P&Z and CPAC | PR candidate |
| RQ06 | Both owning feeds; union matches accepted case exception | Issue candidate |
| RQ07 | Correct new-feed semantics, exact metadata does not converge | Issue candidate |
| RQ08 | Correct manual fallback, redundant fields trigger judging | Issue candidate |
| RQ09 | Wrong new-feed proposal against approved manual expectation | Issue candidate |
| RQ10 | Wrong proposals against approved program/manual expectation | Issue candidate |
| RQ11 | Correct manual fallback | Issue candidate |
| RQ12 | Wrong new-feed proposal against approved archive/manual control | Issue candidate |

The concise run would produce **5 PR candidates: 3 correct and 2 wrong (40%)**, plus 7 issue candidates
and 0 holds. Wrong PR proposals remain RQ04/RQ05: 2/12 of all cases (16.7%). The baseline had 6 PR
candidates: 4 acceptable with RQ06 accepted, 2 wrong (33.3%), plus 4 issues and 2 input holds. The
higher wrong fraction among concise PR candidates reflects RQ06 moving to an issue, not another
wrong automatic proposal. Gemini selected A while JEV selected B for RQ06; their all-observed
flags differ despite the same union and owners. Exact convergence still creates manual overhead.

An all-issues policy would require human triage of all 12 cases before generating remedy PRs.
The PR-candidate policy would ask reviewers to catch 2 wrong patches among 5 proposed patches in
this small challenge. Neither policy automatically applies changes. Human review efficacy and
actual production review volume were not measured. The maintainer's output-policy choice remains
pending; no all-issues or PR-producing configuration was activated by this evaluation.

RQ12's archive/manual distinction remains absent from the runtime prompt. Its wrong score is
against the approved control; no archive policy or case-specific gold was inserted into the model
inputs to improve that result. RQ09/RQ10 also preserve their original approved manual targets.

## Identity, profile and descriptive timing

All 38 completed responses passed exact returned-ID checks: Flash `zai-org/glm-5.3-flash` (the
explicitly accepted alias), DeepSeek `deepseek-v4.1-flash-free`, Gemini `gemini-3.8-flash-free`,
and JEV `jev-1.13-free`. Accepted IDs identify the serving response contract, not independently
verified model weights. Models used primary free routes only, default reasoning, no temperature
override, 16,384 chat output tokens and a 720-second deadline; JEV used native systemone controls.

Success-only median latency was Flash 16.26s, DeepSeek 7.85s, Gemini 13.81s and JEV 0.49s. OrcaRouter
had 12 HTTP 200 responses plus the original 429; BeatAPI had 26 HTTP 200 responses. These are
single-flight, spaced observations of different role mixes, not maximum-capacity measurements.

## Reproduce the comparison offline

Generate the frozen packet using the README's Python command. For an existing local ledger,
`score_runtime_qualification.py --freeze <new-file>` retains compact observations only after the
controller exits and all requests have known results. It never overwrites an existing artifact.
Recompute the comparison with:

```bash
run_root=evals/remedy/results/runtime-qualification-2026-10-26/concise-v2
qualification_root=evals/remedy/qualification
PYTHONPATH="$PWD" python evals/remedy/score_runtime_qualification.py \
  --directory "$run_root/measurement" \
  --plan "$run_root/packet/run-plan-draft.json" \
  --authority "$run_root/packet/execution-authority.json" \
  --compare-baseline "$qualification_root/runtime-v1/observed-results.json" \
  --compare-baseline "$qualification_root/runtime-v1/observed-continuation.json" \
  --candidate "$qualification_root/runtime-v2/observed-results.json" \
  --acceptance "$qualification_root/runtime-v1/rq06-observed-acceptance.json"
```

The acceptance view checks matching structured assignments and case ID. Original raw scores
remain visible. The source/schema/prompt manifests, generator and scorer are retained; bulk
execution state is not required to retain the observed judgments or decision record.

Final verification: 6,925 full offline tests passed (17 deselected), including scoped-acceptance
comparison checks. Whole-repository Ruff/format and diff checks passed.

## Counterfactual: require JEV/Gemini agreement for every PR

On the existing observations, exact complete-proposal agreement exists only on RQ04 and RQ08.
RQ04 would produce one incorrect PR: include the recording only in Addison Council, omitting
P&Z. Its concise-run action is single_uid_inclusion, not the baseline run's union. RQ08 correctly
agrees on manual_review and produces no PR. Thus this strict observed-data filter produces 0
correct PRs, 1 wrong PR and 1 correct manual result. Five other judged cases fail exact agreement.
RQ01/RQ02/RQ03/RQ05/RQ11 were never judged; their JEV/Gemini outcomes cannot be inferred.

At the coarser action-and-feed-identity level, the judges match on all seven answered cases.
Ignoring metadata and recording-scope differences would yield 6 proposed changes: RQ06 (accepted)
and RQ07 (correct), versus wrong RQ04/RQ09/RQ10/RQ12. RQ08 remains correct manual review. This is
not the current complete-assignment agreement rule: RQ09 has a real recording-scope difference,
and other cases differ in flags/descriptions. Do not silently discard those fields or activate
this looser policy from this comparison. No new judgments or routing changes were made.
