Record approved remedy JEV Worker observations with exact returned route/upstream evidence.
Recover the accepted first job after the optional Python deferred registry failed; use the DO
queue with local receipts and B2 payloads for this evaluation lane. No R2 bucket is required.
Clarify that JEV's output reservation is scheduler-only; systemone accepts no generation controls.
Admissions and shadow/report-only settings remain unchanged.
All four observations completed in one attempt each, with all twelve answers parsed.
Record the approved chat identity probes: Sol remains pending after two Worker dispatch attempts;
GLM Flash and DeepSeek remain unsubmitted. Preserve receipts and admission integration blockers.
No cancellation, runtime validation change or production admission is authorized by this record.
Record maintainer-approved cancellation and identical Sol availability retry, preserving both
job histories. Transient availability does not penalize accepted model-calibration evidence.
Record the October 21 operational status: Sol retry failed without an answer; GLM enqueue was
refused by the Worker's reserved ingress write budget, and DeepSeek remains unsubmitted.
No calibration-quality penalty, budget bypass or production activation follows these outcomes.
Schedule the explicitly approved one-shot midnight local diagnostic with exact-route pauses,
owned-pause restoration, frozen request settings and no provider retries or production activation.
Clarify UTC Worker/OrcaRouter day resets versus BeatAPI's per-minute free-call limit.
Verify the approved 23:50 UTC route pauses with zero in-flight jobs and one-hour expiry;
no LLM calls were made before the separately scheduled local-run phase.
Record the midnight no-call holds: provider accounts remained busy despite selected-route
pauses. Verify all owned pauses restored; preserve calibration evidence without a score penalty.


October 24 follow-up: the maintainer renewed the request to pause the three routes and submit
outstanding jobs. Direct execution remained blocked by other provider routes. The three frozen
requests were therefore accepted by the existing Worker DO queue, which retains account pacing:
Sol `71fa5a0c-6a39-4699-8efd-891d818a59c4`, GLM Flash
`bb603c63-0468-4866-93bb-c8e13b2d2cfc`, DeepSeek
`31160a2c-5fe9-4c56-a247-0ad4a9daca00`. All three were queued at readback; acceptance is not
completion or calibration evidence. Temporary exact-route pauses were restored, with zero owned
pauses remaining. No provider/JEV/global pause or production admission changed. Receipts are in
`evals/remedy/results/worker-qualification-2026-10-19/queue-followup-2026-10-24/`.


October 24 completion investigation: the maintainer requested persistent exact-route pauses and
periodic progress until the outstanding observations finish. All three queued evaluation jobs
were confirmed cancelled before direct calls, avoiding duplicate execution. The direct Sol
request returned HTTP 404 (`not_found`, `retryable: false`); an authenticated model-list read
also omitted the pinned `gpt-6.1-sol-free` ID. This is availability evidence, not calibration
quality evidence. GLM Flash and DeepSeek remain under the local completion controller. Exact
route pauses are renewed with expiring one-hour leases; no provider/global/JEV pause was added.
Raw bounded contention snapshots, cancellation and response receipts are preserved under
`evals/remedy/results/worker-qualification-2026-10-19/completion-investigation/`.
A five-minute thread heartbeat reports progress and safely resumes only without an active
controller or existing call intent. No production admission or merge has occurred.


October 24, 00:47 UTC completion readback: all three direct requests have returned; the controller
has exited. GLM Flash returned HTTP 200 and a schema-valid answer, but its response model was
`zai-org/glm-5.3-flash`, differing from pinned `z-ai/glm-5.3-flash-free`; identity acceptance
remains held. DeepSeek returned its exact pinned identity. GLM selected only the dedicated
Streets feed; DeepSeek selected both the dedicated and Bond Program family feeds. Sol returned
a non-retryable unavailable-model 404. None of these observations creates an admission or
serving-family attestation. Pauses remain active pending maintainer disposition of both holds;
the heartbeat now renews pauses and reports status without restarting the finished controller.


### Approved Sol replacement — October 23, 2026 Central

The maintainer approved the proposed reassignment: Gemini 3.8 Flash Free on BeatAPI replaces
Sol as initial assignment A; GLM Flash remains initial B, and DeepSeek V4.1 Flash plus JEV 1.13
remain adjudicators. This supersedes the October 15 pinned Sol role. Sol historical results
remain preserved; its current unavailability is not a calibration failure.

The maintainer also approved a temporary BeatAPI provider pause, explicitly including JEV,
for the new candidate's frozen 60-case evaluation. The candidate is not registered in the
deployed Worker and is not falsely charged to another route. The evaluation uses provider
pause/drain, single-flight, 65-second spacing, zero hidden retries and immutable request intents.
See `evals/remedy/results/gemini-replacement-2026-10-24/`. The original assignment prompt and
all 60 cases are frozen; gold is used only offline. Existing GLM original-prompt observations
supply paired comparisons. Any new disagreement must bind its actual changed A/B proposals;
old adjudicator answers cannot be relabeled as judgments of Gemini's response.

This approves the assignment change and necessary evaluation, not production admission.
Provider-default is not a named High/Max effort claim. Exact route support, identity, task
quality and trusted admission loading remain evidence gates. Production stays shadowed.


The maintainer additionally approved pausing the secondary BeatAPI API key to parallelize the
evaluation. The original controller stopped between calls after two results with no unresolved
intent. The remaining cases are split deterministically: even manifest indices use primary, odd
indices use secondary. There are 60 total cases, not 60 per account. Each account keeps separate
provider pause ownership, single-flight, 65-second spacing, controller lock and response provenance.
Existing results remain immutable; the secondary key does not repeat primary results.


October 23, 2026 Central (02:28 UTC October 24): the replacement Gemini route completed all
60 frozen original-prompt cases with schema-valid answers. Final initial-role score is 36/40
assignment cases, 15/15 policy controls and 3/5 unknown-owner guardrails. Against the existing
GLM original-prompt answers, action/feed scoring gives 53 correct agreements, four shared wrong
agreements and three disagreements. These are reused historical cases, not a new held-out
qualification. Four wrong consensus outcomes remain a limitation of disagreement-only escalation.
The secondary account stopped after a provider ban; no secondary retry occurred. The maintainer
explicitly authorized finishing with primary, which completed the remaining cases and transient
timeout retry. Primary collection ended and restored its owned provider pause.

The three actual Gemini/GLM disagreement packets were frozen using the existing deterministic
evidence expansion: 12 retrieved URLs, no retrieval errors. DeepSeek/JEV adjudication is now
running on primary only, with bounded requests and DO route reservations under an approved
provider pause. Earlier Sol/GLM judgments are not reused for changed proposals. Route admission,
identity acceptance, production activation and P4/P5 remain gated. See final initial-pair score
and adjudication artifacts under `evals/remedy/results/gemini-replacement-2026-10-24/`.


October 23 Central: full GLM 5.3 evaluation authorized by maintainer after the all-pair
comparison showed GLM Flash/DeepSeek had fewer shared errors than Gemini/GLM Flash. The exact
BeatAPI `glm-5.3-free` ID is enabled for primary; 60 original-prompt frozen cases are running
under the previously approved provider-pause coordination pattern and existing parameters.
`evals/remedy/results/glm-full-2026-10-24/score.py` compares full GLM and Flash against DeepSeek
and Gemini on the same completed-case intersection. This is measurement, not a role promotion.

The prior Gemini/GLM disagreement adjudication completed: all three were correctly resolved
by DeepSeek/JEV agreement. Retrospective action/feed model gates therefore yield 56 correct
outcomes and four incorrect shared agreements, not production qualification. The four initial
shared errors bypass adjudication and remain a limitation. Full GLM testing examines whether
a different initial pair reduces these errors without discarding earlier raw evidence.


Maintainer explicitly approved both NVIDIA and BeatAPI provider pauses and distributed full
GLM measurement. NVIDIA catalogue verifies full `z-ai/glm-5.3`; BeatAPI uses `glm-5.3-free`.
Cases are split by original manifest index: even BeatAPI, odd NVIDIA,60distinct cases total.
Both use original messages/schema, prompt-only formatting,16384output/default reasoning and
720-second deadline. Serving identity remains separate perprovider. Reports preserve source
paths/provider partitions; pooling candidate task scores does not admit both physical routes.
`capacity.py` reports observed throughput, errors and latency under singleflight65s pacing;
it does not establish either provider's hardmaximum. Disjoint halves differ in input lengths.
The first BeatAPI request returned524 after125seconds and remains an immutable availability
failure eligible for authorized retry. NVIDIA and BeatAPI controllers have separate locks.


The maintainer approved32,768-token fullGLM profile after a16,384-token NVIDIA response
ended with finish_reason:length and no final answer. Keep720-second deadline/defaultreasoning/
no temperature override. Controllers switch only between requests. Previously valid16,384-cap
answers remain unchanged; empty truncated answers may receive a distinct larger-cap attempt.
The scorer records each selected answer's actual output cap, provider, intent hash and source.
This is mixed-profile evidence, not a silent rewrite of the original16,384-token experiment.


Full GLM cohort measurement completed October 24, 2026: all 60 cases have terminal outcomes,
58 valid answers and two completed NVIDIA contract failures; 51/58 valid answers were correct.
On the identical 58-case intersection, full GLM + DeepSeek had 50 correct agreements, four
shared wrong agreements and four disagreements, versus Flash's 50/1/7. Full GLM + Gemini had
51/5/2 versus Flash's 52/3/3. Full GLM increases accepted shared errors; do not promote it over
Flash. Changed role/cascade proposals still require fresh disagreement evaluation and approval.
BeatAPI completed 30 valid cases in 47 attempts; NVIDIA completed 28 valid plus two contract
failures in 40 attempts. Observed throughput was about 4.92 valid cases/hour for each under
single-flight controls; disjoint inputs and profile/transport changes prevent a hard capacity
ranking. Preserve mixed-profile provenance (11 selected 16k answers, 47 selected 32k answers),
720-second deadline and all immutable failures. Both controllers finished with no unresolved
intents. Restore only owned investigation pauses. Production remains shadow/empty admissions;
trusted evaluation loading, JEV evidence alternatives and serving identity gates remain open.
Final evidence and recommendations: `evals/remedy/results/glm-full-2026-10-24/final-report.md`.


October 24 offline all-pair comparison now records all ten original-prompt pairings across Sol,
GLM Flash, DeepSeek, Gemini 3.8 Flash and current full GLM. Each pair reports all 60 outcomes,
including two full-GLM contract holds, plus a shared 58-case comparison for fair ranking.
Among available pairs Flash + DeepSeek has the fewest shared wrong agreements: two on all 60,
one on the common 58. Historical Sol + Flash remains zero, but Sol is unavailable. Full GLM
adds no lower-error pairing. JEV choice judgments cannot supply independent 60-case answers;
the older seven-model audit remains separate on its 40-case common intersection/profile.
Case-level provenance and reproducible scorer:
the retained compact `accepted-pairs-2026-10-24/evidence.json`; regenerate the tables with
`python evals/remedy/accepted_pairs.py score`.
No provider calls, admission changes or production changes were made for this comparison.


### Maintainer accepts two initial pairings — October 24, 2026

After reviewing the seven shared-error cases, the maintainer explicitly accepted Flash +
DeepSeek and Flash + full GLM as acceptable pairings and requested progression. Record:
`evals/remedy/results/accepted-pairs-2026-10-24/maintainer-acceptance.json`. This supersedes the
prior task-quality rejection of Flash/full GLM and Gemini/Flash initial-pair preference.
Original raw answers, gold and scores remain unchanged. Accepted shared-error case sets are
pair-specific: Addison Citizen Advisory and June 13 Dallas for Flash/DeepSeek; the three Dallas
subcommittee cases for Flash/full GLM. This does not waive ownership/identity/admission gates or
full GLM's two contract failures, and does not approve the other four available pairings.

Next-step preparation freezes seven actual Flash/DeepSeek disagreements and five Flash/full-GLM
disagreements, with blinded proposals and exact source hashes. New judge mapping needs approval:
proposed JEV + Gemini for Flash/DeepSeek avoids self-adjudication; DeepSeek + JEV remains suitable
for Flash/full GLM. A bounded 24-judgment plan and subsequent admission-integration file/test scope
are concrete in `evals/remedy/results/accepted-pairs-2026-10-24/next-step.md`. No initial answers
are rerun; no production admission, deployment or merge follows from this acceptance.


October 24: maintainer explicitly approved both accepted-pair adjudication runs: JEV + Gemini
for seven Flash/DeepSeek disagreements and DeepSeek + JEV for five Flash/full-GLM disagreements,
24 judgments total. Approval and packet hashes are in
`evals/remedy/results/accepted-pairs-2026-10-24/adjudication-approval.json`. Local primary-only
controller uses the approved BeatAPI provider pause, exact DO reservations for registered
DeepSeek/JEV, and provider-pause coordination for unregistered Gemini. Original 60-case answers
are never rerun. Preflight validated 24 unique independent jobs and schema/case/model bindings;
whole Ruff/format passed. Five-minute progress monitoring continues as requested. New wrong
adjudications remain separately scored; acceptance of initial shared errors does not waive them.
Production stays shadow/empty admissions; no role activation, deploy or merge.


Accepted-pair disagreement measurement completed October24,2026: all24 new judgments returned
HTTP200 with exact requested model IDs and valid schemas; no transport retries were needed.
Flash/DeepSeek with Gemini+JEV resolved four of seven disagreements correctly, leaving three
manual and no new wrong accepted resolutions. Complete60-case model-gate accounting:55 original-
gold correct, two maintainer-accepted initial exceptions, three manual. Flash/fullGLM with
DeepSeek+JEV resolved all five disagreements correctly:55 original-gold correct, three accepted
initial exceptions and two initial contract holds. No new wrong accepted resolution occurred.
These reused-cohort results keep original truth and scoped acceptance separate. Flash/fullGLM
shares an initial model family; fullGLM provider/profile provenance remains explicit. Remaining
adoption gate is the narrow reviewed role/admission/evidence/DO-coordination integration design;
no production route is activated. Raw requests, results, hashes and final report are in
`evals/remedy/results/accepted-pairs-2026-10-24/`. Controller finished, owned pause released and
monitor deleted after finalization. Whole Ruff/format and diff checks passed.


### Selected-cascade admission integration design — October24,2026

Maintainer requested proceeding with the Flash/DeepSeek initial pair and Gemini/JEV adjudication
integration. The code audit and concrete file/function/test contract are in
`evals/remedy/results/accepted-pairs-2026-10-24/adoption/integration-design.md`; reproducible
read-only readiness facts are in that directory's `readiness.json`. The runtime ledger, report
and consensus contracts hard-code old roles beyond the two initially named modules, and runtime
BodyDecision/P3ChatResponse differs from evaluated BodySelectionAnswer. Preserve those distinct
hashes rather than declaring evaluation transfer automatically. An AGENTS-required scope
extension request is pending for those additional contracts and the daily caller.

The first proposed monitored cycle is explicitly isolated/report-only with WorkerDO inference
coordination and local immutable run receipts; it writes neither the production observation
ledger nor P2 events. It does not drop R2/CAS guards from ordinary daily mode or treat provider
pauses as ledger mutexes. PersistentDO ledger append/fencing remains a separate scoped contract.
Current config is verified shadow/report_only/empty admissions. Gemini's exact candidate route
is absent from the local registry; GLM alias identity and JEV's narrow evidence alternative stay
explicit admission gates. No production code/config/deploy/credentials/state changed during this
design audit. Source implementation follows approval of the expanded scope and reviewed contract.


### Admission integration foundation implemented locally — October24,2026

The maintainer explicitly approved extending scope to observation/consensus/report contracts
and the daily caller. `adoption/scope-approval.json` records that decision. The implementation
adds one immutable `flash-deepseek-v2` role plan, version2 observation plan binding with unchanged
legacy version1 payloads/IDs, plan-aware A/B consensus and generic chat adjudication, a bounded
reviewed project-evidence loader and a narrow manual/report-only JEV evidence alternative.
The validator now receives trusted runtime-bound evaluation evidence; no qualified dictionary is
fabricated from accepted cohort results. Prompt/schema/catalog/profile/identity mismatches fail
closed. Legacy exports and replay remain available; current production config is unchanged.

The explicit daily `--report-only-plan`/`--plan-sha256` path takes a frozen local manual plan,
requires exact admissions before backend/state initialization, then uses local immutable
observations and intent/receipt files with queue-only Worker inference. It neither creates R2 nor
removes the ordinary daily caller's CAS guard. A plan-hash artifact directory supports later
reconciliation; unknown intents do not resubmit. The checked preflight receipt is `policy_hold`
before any provider or production-storage call. Exact Gemini route registration, GLM serving
alias disposition and reviewed evaluated-to-runtime schema/prompt transfer still block a real
monitored Worker cycle. No production admission, deploy, state write or merge occurred. This is
implemented-but-unmerged foundation work, not Shipped or runtime adoption. See the adoption
readiness/implementation artifacts for current validation and remaining gates.


Admission-integration verification completed:6743 full offline tests passed,17 deselected;
143 targeted P3/evidence/observation/report/action tests passed. Whole Ruff and format checks
passed (600 Python files); diff checks clean. Frozen adjudication evidence hashes all unchanged.
The explicit report-only preflight produced a policy_hold before backend/storage initialization;
no monitored live inference cycle was submitted, and production remains shadow/empty admissions.


October25,2026: maintainer authorized local steps1and2. Added exact primary BeatAPI free Gemini
route and regenerated both catalogs; all51 pre-existing routes/provider controls remain unchanged.
Uses evaluated16k output/prompt-only profile, backup/JEV priority,1RPM/singleflight and no invented
rpd. Context remains an explicitly unmeasured conservative placeholder. Provider-specific format
verification stays external to avoid compiler propagation into other Gemini routes. No deployment.
Saved public model-catalogue metadata and official provider naming evidence for GLM's requested
z-ai/glm-5.3-flash-free versus returned zai-org/glm-5.3-flash; naming alignment is supported but
serving-identity acceptance remains held. No alias whitelist or model-identity gate was relaxed.
Offline replay projected322 saved outputs:195 bounded per-recording candidates requiring source
policy validation and127 manual semantic holds. Original truth/scores/UIDs and task exceptions
unchanged; two invalid fullGLM outputs remain contract holds. This is an adapter/schema replay,
not qualification of the different runtime prompt. Results, source hashes and reproduction script:
`evals/remedy/results/accepted-pairs-2026-10-24/adoption/route-replay-2026-10-25/REPORT.md`.
No new LLM requests, credential reads, production admissions or Worker deployment occurred.


October25 route/replay verification:6747 offline tests passed,17 deselected;131 targeted
catalog/discovery/runtime-policy/adapter tests passed. Whole Ruff/format (601files) and diff checks
passed. The free logical key is `gemini/gemini-3.8-flash-free`, isolated from the shared Gemini and
discovery pools; all existing51 route rows, provider controls and model-route pools are unchanged.
All322 replayed outputs validate as P3ChatResponse wrappers;195 are bounded candidates requiring
source policy checks and127 are semantic holds, not revised accuracy counts. Replay and catalog
compilation are deterministic. No deployment, new LLM calls or production admission occurred.

### October 25 pre-run PR preparation

The four-role candidate admission bundle is prepared at
`evals/remedy/results/accepted-pairs-2026-10-24/adoption/admission-review-bundle.json`.
Its exact current runtime bindings and supporting evidence hashes are reviewable; all candidates
remain unqualified. The identity disposition retains the GLM alias hold, and the contract review
accepts representation replay only, without claiming runtime-prompt qualification. See
`adoption/pre-run-review.md` for the remaining gates. The Worker deploy workflow automatically runs
on a merge to main that changes the route catalog. Successful deployment must be verified before
an authorized monitored run. Production remains shadow/report_only with empty admissions.

October26 PR validation after integrating main9fc43b03:6,902 Python tests passed
(17 deselected),572 Worker tests passed; whole Ruff/format and diff checks passed. All51
existing main route rows and52 frozen final-evidence hashes are preserved. Each candidate
admission remains held with explicit rejection reasons; production is shadow/empty admissions.
Rerun instructions and retention validation are in `evals/remedy/EXPERIMENTS.md`.

### Experiment retention correction (October 26)

The PR now retains reusable scripts, frozen cohort/judge inputs, pair-specific maintainer
acceptance, final conclusions and a compact normalized model-output record. Generated requests,
score/replay snapshots, retrieved pages, SSE frames, pause/poll receipts and controller state
are untracked and ignored; all original local files remain. The maintained rerun and additional-
model instructions are `evals/remedy/EXPERIMENTS.md`; `evals/remedy/accepted_pairs.py` scores
without the raw archive and reconstructs disposable historical scorer inputs on demand.
Historical raw receipt hashes identify local originals; normalized evidence is not byte-identical
transport attestation. Identity/runtime admission holds and production shadow status are unchanged.

### Exact GLM alias accepted (October 26)

The maintainer explicitly approved requested `z-ai/glm-5.3-flash-free` returning
`zai-org/glm-5.3-flash` on `orcarouter_zai_glm_5_3_flash_free`, for both report-only and
production P3 identity checks. The exact instruction and scope are preserved in
`evals/remedy/results/accepted-pairs-2026-10-24/adoption/glm-alias-acceptance.json`. This
supersedes the earlier alias-disposition hold. The maintainer's endpoint/allowance explanation is
recorded as rationale, not a verified provider contract or serving-weight attestation.

P3 binds the accepted returned IDs into reviewed runtime evidence, checks the actual provider
response model (Worker upstream metadata can be catalog-derived), and records the actual returned
ID in new observations. Unknown/missing returned IDs, other physical routes, other requested IDs
and full-GLM variants receive no alias exception. Original receipts and raw identity flags remain
unchanged. Contract-transfer qualification, ordinary admission eligibility and activation remain
separate gates; config stays shadow/report_only/empty admissions. No live call or deployment.

Alias-policy verification:117 targeted tests and6,911 full offline tests passed
(17 deselected); whole-repository Ruff/format and diff checks passed. Production remains
shadow/report_only with no admissions; compact experimental outcomes were not rewritten.

### Runtime-contract challenge prepared for review (October 26)

Prepared twelve real historical source/finding cases using retained recording/inventory data
and current source-scoped feed policy: three unions, three exact-recording joint inclusions,
one proposed new body/feed direction, and five manual controls. The curated draft and rerun guide
are `evals/remedy/qualification/runtime-v1/{cases-draft.json,README.md}`; the reusable offline
builder is `evals/remedy/runtime_qualification.py`. Generated review cards, inputs, provenance,
validator checks and hash-bound run plan stay ignored/local, not committed run artifacts.

Expected outcomes and budget remain unapproved. RQ04/RQ05 expose existing P&Z/CPAC taxonomy
validator holds without dropping correct feed memberships. RQ07 requires review of the new body
direction; RQ12 requires reviewing the archive-policy/manual boundary absent from the prompt.
Mirrored native clips are counted once for evidence frequency, with all original identities
retained; no stored recording is deleted or rewritten. Source overlap/reused observations are
explicit: this is a bounded challenge, not an independent admission holdout.

Proposed budget is24 initial plus at most24 conditional judge requests,48 provider attempts
maximum with no retries. Direct single-attempt measurement of runtime-generated payloads keeps
that cap enforceable; ordinary Worker queue retries are not silently included. Worker end-to-end
validation remains separate. Adjudicators receive actual proposals only and may have no coverage
if there are no disagreements. No live calls, credentials, deployment or activation occurred.

Offline preflight corrected stale Sol/GLM captions in the selected runtime JEV A/B options to
neutral initial-proposal captions; the legacy plan remains byte-compatible. Runtime evidence
bindings were refreshed; this does not qualify any role.29 focused tests and6,913 full offline
tests passed (17 deselected); whole Ruff/format passed. Contract qualification and admissions
remain held pending reviewed expectations, authorized measurement and assessed results.

### Runtime challenge expectations approved (October 26)

The maintainer confirmed RQ01–RQ12; exact instruction and reviewed spec/input/expectation/review
hashes are in `evals/remedy/qualification/runtime-v1/outcome-acceptance.json`. RQ04/RQ05 accept
the current manual fallback and also a correct automatic resolution with complete feed/recording
scope. No validator adjustment or guard bypass is requested now. RQ07's new-feed expectation and
RQ12's manual-control fallback are confirmed; no feed is created by this review decision.

The offline builder can attach the approval after checking exact reviewed bytes and generate
approved expectation metadata. Original draft and gold remain unchanged; no model answer is
repaired. Expected-outcome approval is separate from execution: the 48-attempt budget remains
unapproved, no inference ran, and runtime qualification, admissions and activation remain held.

### Runtime measurement budget approved — pre-merge (October 26)

The maintainer approved48 provider attempts. Exact authority and frozen plan/expectation/executor
hashes are in `evals/remedy/qualification/runtime-v1/budget-approval.json`; the reusable bounded
executor is `evals/remedy/run_runtime_qualification.py`. Run from this branch before merge so
results can be reviewed in PR #2541. Deployed Worker integration remains separately gated.

One immutable case/role request, no repeats, retries, redirects, paid fallback or secondary
account. A single approved output ledger and nonblocking controller lock enforce the cap; unknown
requests or access/profile/identity holds stop new calls. Exact route reservations and owned
provider pauses coordinate direct measurement; an undeployed Gemini candidate uses its provider
pause without falsely charging another route. Keep raw files ignored/local. Current production
config, admission eligibility and runtime-transfer decisions remain unchanged by call approval.

Record the pre-merge runtime challenge:31 attempts,30 completed judgments and a non-retryable
JEV input-length HTTP400; owned pauses restored and no unknown requests. Three of six sandbox
automatic candidates violate approved joint ownership/scope. Contract transfer remains unqualified;
retain compact observations and reusable scoring, with bulk artifacts local/ignored. No activation.

Observed RQ06 was subsequently accepted by the maintainer as a case-specific union exception;
original scores remain unchanged. Runtime challenge optional adjudicator errors after this
acceptance: Gemini3/6, JEV1/4 completed judgments, on differing conditional subsets. RQ04/RQ05
remain wrong automatic outcomes; JEV input holds and incomplete coverage remain unresolved.
See `evals/remedy/qualification/runtime-v1/rq06-observed-acceptance.json`. No activation or calls.

Record the explicit untouched-case continuation authority for RQ11/RQ12 within the original
48-attempt ledger. Preserve the exact terminal input-length failures and all prior outputs;
use the shared controller/lock and source/profile checks. No remedy output policy is activated.
Continuation safety checks passed; full offline suite:6,920 passed,17 deselected.

Complete the remaining runtime challenge cases under the approved cumulative budget:37 attempts,
36 judgments and two terminal JEV input holds/failures. With RQ06 accepted,2/6 proposed PRs are
wrong; both adjudicators are wrong2/5 on their same answered cases. Keep original scores and
compact observations; production routing and remedy creation remain unchanged pending review.

Remove repeated evidence from the selected runtime adjudication prompts and make instructions
concise. Preserve full taxonomy, legacy prompts, existing scoring and production shadow gates.
Version JEV request identity and bind context construction to reviewed evidence. Add repeatable
Python packet generation and duplication audits for a fresh, approved twelve-case evaluation.

Preserve the concise evaluation's initial429 as availability evidence. Record the separately
approved one-retry exception after the provider cooldown, scheduled continuation, shared48-attempt
cap and stop-on-new-hold behavior. Both original evaluation pauses were restored; no quality
judgment or production activation is claimed from the blocked attempt.

Final concise-prompt/retry validation:6,924 full offline tests passed (17 deselected),40 focused
prompt/safety checks passed, and whole-repository Ruff/format/diff checks passed. Two independently
generated eight-file packets were byte-identical. No inference beyond the single held429 attempt.

Complete concise-prompt reevaluation:all12 cases,39 attempts including the approved429 retry,
38 judgments and no input holds. JEV size failures resolved; matched semantic quality unchanged.
Sandbox output remains wrong on RQ04/RQ05:2/5 proposed PRs, plus7 issue candidates. Preserve
original scores, scoped RQ06 acceptance and compact observations; output policy stays unselected.
Restore owned pauses and remove the completed automation. No production admission or activation.

Completion validation:6,925 full offline tests passed (17 deselected); whole-repository
Ruff/format/diff checks passed. The comparison test preserves original wrong scores, accepts only
matching reviewed RQ06 assignments, and rejects transferring that exception to another case.

Implement the approved two-model human-review plan:validated Flash/DeepSeek agreements propose
reviewed PRs; disagreements, invalid responses, validator holds and failed calls become issues.
Remove automated adjudication/admission requirements from this plan, preserve historical plans,
and persist terminal failure evidence without fabricated identity/votes or repeated requests.
Select the plan behind unchanged shadow/report-only/empty-admission gates. Offline policy replay
gives4 PR candidates (3 correct,1 wrong shared agreement) and8 issues; no new provider calls.

Human-review flow verification:6,932 offline tests passed (17 deselected), including7 focused
flow/failure scenarios; whole Ruff/format/diff passed. Offline replay validates original route,
requested/returned identity and f0 contract before reusing an answer; it fabricates no model vote.

The publication boundary now carries the explicit role plan from the report and trusted config
into consensus/selected-vote revalidation. New-plan PR candidates can reach the existing validator
without being mistaken for Sol/Flash; mismatched plans fail closed. The end-to-end publication
unit test revalidates a real two-model agreement and rejects it under the wrong plan.

Final human-review validation:6,933 full offline tests passed (17 deselected);57 latest focused
checks passed, including unknown finding IDs, mismatched request recipes and actual returned-model
mismatches becoming human issues. Ten human-flow/failure scenarios cover no judge calls, no
resubmission and explicit request references. Whole Ruff/format/diff checks passed.

Enable maintainer-authorized bounded manual PR/issue publication for Flash + DeepSeek. Record a
hash-bound authorization separately from general model qualification; preserve all limits and
R2 CAS. Exact validated agreements may propose PRs, with disagreements/failures sent to human
issues. All remedy merges require human review; automatic alias types remain disabled. No new
model calls, direct feed changes or media pipeline version changes accompany this configuration.
Deployment and production-cycle verification remain pending until the implementation is merged.

Production-authorization verification:6,947 full offline tests passed (17 deselected), plus28
focused publication/authorization checks including the publisher's repository trust-root wiring.
Whole-repository Ruff, formatting and diff checks passed. No live model or publication calls ran.
