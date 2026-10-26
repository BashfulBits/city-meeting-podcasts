# Runtime-contract measurement — stopped; transfer not qualified

The approved pre-merge run stopped on October 26 after 31 of 48 allowed attempts.
JEV rejected RQ10 with HTTP 400: input too long, explicitly non-retryable. RQ09's JEV
input was held locally before sending. No retries, payload reductions or fallback calls ran.
Both owned provider pauses were restored successfully; there are no unresolved intents.
RQ11 and RQ12 were never attempted. RQ10 has chat answers but no final cascade summary.

`observed-results.json` retains compact normalized observations and original-result hashes.
Raw receipts, generated requests, pause/reservation logs and controller files remain local and
ignored. These normalized observations are not transport attestations. The reusable read-only
`evals/remedy/score_runtime_qualification.py` scores the local frozen measurement ledger.

## Observed quality and cascade behavior

On the identical ten answered initial cases, Flash and DeepSeek each scored 5 correct / 5 wrong
against the approved expectations. Gemini scored 2 correct / 4 wrong on six conditional cases;
that conditional subset is not comparable to the ten-case initial denominator. Four JEV judgments
completed; one was held before sending and one returned HTTP 400. All 30 completed judgments
passed their exact returned-model identity checks, including the approved Flash alias.

Nine case summaries exist: six automatic PR candidates, two issue candidates and one policy hold.
Only three of the six automatic candidates meet approved semantics. The remaining three are new
wrong resolutions, not covered by prior pair acceptance or the approved manual fallbacks:

| Case | Actual automatic result | Approved requirement |
|---|---|---|
| RQ04 | City Council union; P&Z omitted | Exact recording in Council and P&Z, or manual fallback |
| RQ05 | City Council union; P&Z and CPAC omitted | Exact recording in all three feeds, or manual |
| RQ06 | Union into Council and Library Board | Exact recording inclusion in both feeds |

RQ04 and RQ06 were selected by both adjudicators; RQ05 was an initial wrong agreement.
These are failures of the tested end-to-end contract: each automatic candidate passed current
validation. RQ06 is a scope/action failure, not a missing-feed failure. No selector was changed.
The visible taxonomy influenced the models toward existing broad union remedies. That explanation
is an inference from their rationales, not proof of a particular model-internal cause.

RQ01–RQ03 resolved correctly. RQ02 had equivalent correct initial semantics but redundant
`episode_ids` differed despite `all_observed_episodes=true`, causing avoidable adjudication.
RQ07's three chat answers all proposed the approved new feed, yet wording differences and exact
proposal matching ended in an issue candidate. RQ08 correctly chose manual review and became
an issue candidate. RQ09's chat proposals were wrong against the approved manual expectation;
its JEV input-profile hold left a policy hold. RQ10's chat proposals also contradicted the manual
expectation; its JEV HTTP failure stopped the controller before a cascade summary was written.

OrcaRouter returned ten HTTP 200 responses (median 16.58 seconds). BeatAPI returned twenty HTTP
200 responses and one HTTP 400 (median across responses 11.04 seconds). These descriptive times
include the failed response and do not establish maximum capacity. The 65-second spacing and
owned provider pauses constrain this experiment's throughput.

## Disposition and next review

Do not qualify the evaluated-to-runtime transfer or activate this role plan from these results.
Keep production shadow/report_only with empty admissions. This reused historical challenge is
incomplete and is not an independent production holdout. Prior original-prompt results and
pair-specific exceptions remain valid for their original scope; they do not waive these failures.

Before further calls, agree a concrete repair scope: clarify joint ownership and recording-only
remedies in the runtime contract, guard incomplete joint feed sets and broad selector changes,
canonicalize semantically irrelevant scope fields, and address JEV input sizing without silently
truncating evidence. Preserve this failed baseline. A changed prompt/schema/comparator/validator
or JEV payload needs a fresh hash-bound reviewed packet and execution authority; the unspent
17-attempt ceiling is not permission to repeat completed answers or bypass the input hold.
Review RQ04–RQ06 first, then exact-consensus behavior on RQ02/RQ07 and the RQ09/RQ10 JEV limits.

Validation of the bounded executor: 6,917 offline tests passed, 17 deselected. Whole-repository
Ruff and format checks passed after adding the read-only scorer. No deployment, merge or
production admission occurred.

## Subsequent maintainer acceptance — observed RQ06

The maintainer accepted the actual RQ06 union into both Denton feeds after reviewing the scope
mismatch. `rq06-observed-acceptance.json` records this case-specific exception separately; the
original expectations, outputs and raw scores above remain unchanged. RQ04/RQ05 remain errors.
With RQ06 accepted, four of six automatic candidates are acceptable and two are wrong.
Gemini has three wrong decisions among six completed judgments (50%): RQ04/RQ09/RQ10.
JEV selected a wrong proposal once among four completed judgments (25%): RQ04. Its RQ09 local
input hold and RQ10 HTTP400 are coverage failures, excluded from semantic error denominators.
Originally scored errors were Gemini4/6 and JEV2/4; acceptance is not a gold rewrite. The subsets
are small and differ, so these rates do not establish that one adjudicator is more accurate.
