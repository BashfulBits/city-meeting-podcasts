# Review/51 admission integration — concrete design

Date: October24,2026. Status: scope approved; integration foundation implemented locally, not merged. This design completes the
requested code audit and defines the first report-only integration slice. Runtime activation,
Worker deployment, writes to the production observation ledger and merges remain separate.

## Chosen flow and evidence

Use GLM Flash + DeepSeek as independent initial proposers, then Gemini3.8 Flash + JEV for an
actual disagreement. Retain Flash + fullGLM as a task-accepted alternative, not an automatic
fallback or extra production route. Do not silently call more models when one route fails.

The approved60-case measurement yielded55 originally correct outcomes,2 explicit accepted
initial exceptions and3 manual outcomes for this selected cascade, with zero new wrong accepted
adjudications. The fullGLM alternative yielded55/3/2 (correct/accepted exceptions/contract holds).
The accepted exceptions are pairing-specific judgments, not new universal truth. Bind the exact
maintainer-acceptance.json, adjudication-approval.json and all packet/result hashes. The expanded
packets and score-final.json are now local generated views; EXPERIMENTS.md documents reconstruction.
This is reused-cohort task evidence, not an independent heldout precision claim.

Exact active roles proposed:

| Stage | Role | Physical route | Upstream |
|---|---|---|---|
| Initial A | GLM Flash | orcarouter_zai_glm_5_3_flash_free | z-ai/glm-5.3-flash-free |
| Initial B | DeepSeek | beatapi_deepseek_v4_1_flash_free | deepseek-v4.1-flash-free |
| Chat adjudicator | Gemini | beatapi_gemini_3_8_flash_free | gemini-3.8-flash-free |
| Choice adjudicator | JEV | beatapi_jev_1_13_free | jev-1.13-free |

The upstream strings above must match the compiled routes rather than being copied into config
unchecked. GLM's returned zai-org/glm-5.3-flash alias still needs a serving-identity decision.
Gemini candidate is now registered locally in ROUTE_REGISTRY; deployment remains pending.
No unrelated Gemini route substitutes.
FullGLM16k/32k profiles are excluded from this selected runtime mapping.

## Actual code gaps found

1. remedy_p3_runner.py maps Sol/Flash initial votes and DeepSeek/JEV adjudicators. Role names also
   affect model IDs, recipe/request hashes, queue policies, result checks, vote order and reports.
2. remedy_p3_observations.py has strict stage/role/route bindings and version1 observations. A
   route-map-only patch would reject new observations or confuse older DeepSeek adjudications.
3. audit_remedy.py pins P3_INITIAL_ROUTES and P3Adjudication.deepseek. audit_remedy_report.py
   validates old initial routes and maps JEV choiceA to Sol. New mapping must change together.
4. Evaluated BodySelectionAnswer is not runtime BodyDecision/P3ChatResponse. Existing runtime
   prompt asks union/new_feed/single_uid_inclusion; evaluated prompt asks action/feed assignment.
   Their schemas and prompts have distinct hashes. Never relabel one as the other's evidence.
5. p3_enabled passes no evaluation to validate_admission; admission schema requires AA for JEV.
6. scripts/remedy_p3_daily.py requires cas_capable storage and uses R2 maintenance_leases.
   Observation append also enforces CAS-backed lease.storage. Dispatch pause is not that mutex.
7. Worker dispatch already has provider pause/reserve and job lease machinery, not a public
   remedy-ledger CAS API. Do not assume those controls authorize or fence arbitrary B2 writes.

readiness.py records these facts reproducibly without network, credentials or remote writes.

## Implementation slice A: pure role/response contracts and trusted evidence

Files explicitly in scope for review (extension approval requested under AGENTS.md):

- citypods/remedy_p3_runner.py; tests/test_remedy_p3_runner.py.
- citypods/remedy_evaluation.py; tests/test_remedy_evaluation.py.
- citypods/remedy_p3_observations.py; tests/test_remedy_p3_observations.py.
- citypods/audit_remedy.py; its existing P3 consensus tests under tests/test_remedy_actions.py.
- citypods/audit_remedy_report.py; tests/test_audit_remedy_report.py.
- scripts/remedy_p3_daily.py and its daily-caller tests in test_remedy_p3_runner.py.

Contract requirements:

- Introduce a typed, versioned role-plan value containing exactly two initial bindings and one
  independent chat plusJEV adjudicator binding. Bind model family, physical route, upstream,
  request profile and plan hash; reject duplicate routes, self-adjudication and missing bindings.
  Selected plan is Flash/DeepSeek/Gemini/JEV. Keep the legacy plan readable for old records only.
- All route, label, request, admission, response and report operations consume that same plan.
  A/B labels come from stable initial order, never from an old route-name comparison. Gemini is
  the chat adjudicator; do not hide it in a field named deepseek in new serialized records.
- Version2 observations include plan hash and stage-specific role binding. Version1 remains
  validated under its original strict Sol/Flash/DeepSeek/JEV bindings. Old observations must not
  satisfy new-plan requests. Preserve old content-addressed IDs and append-only records.
- Keep model output untrusted. Match opaque case/finding IDs, exact configured feeds, evidence
  IDs, request/profile/plan hashes and returned route/upstream before using a response.

Evaluated-to-runtime boundary:

- Preserve evaluated body-selection prompt/schema as a distinct versioned request profile. An
  adapter may convert assign_existing to a union proposal only after exact source-scoped feed,
  UID, evidence and sticky-policy checks pass. It must not expand omitted feed memberships.
- unresolved and invalid outputs become manual holds. unconfigured_body does not automatically
  create a feed. archive_only/archive_series/exclude_event do not authorize deleting recordings
  or removing prior publication. Unsupported adapter semantics remain manual.
- A different runtime prompt or schema requires separately recorded transfer qualification or
  new bounded evidence. Existing cohort scores cannot mark the old P3ChatResponse qualified.
  Replay existing answers through the proposed adapter first and record every changed/held case.
- No official title, URL, date or transcript can be changed by this adapter. Existing validators
  and policy checks run before a proposal enters a report.

Trusted evaluation loader contract:

- Trust anchor is a reviewed, explicit evidence index with repository-relative paths and exact
  SHA256 values, selected in the approved run plan. A model response, arbitrary path or standalone
  qualified=true JSON cannot create admission. Reject traversal, symlink escape, missing files,
  oversized artifacts and malformed hashes; read bounded UTF8 JSON inside evals/remedy/ only.
- Bind manifest/gold/prompt/schema/policy/catalog, physical model/route, actual request parameters,
  role-plan and adapter versions. Runtime computes expected hashes; do not copy expected values
  from the same untrusted artifact being validated. Parse results and acceptance separately.
- Preserve original critical-error counts and pair-specific accepted exceptions. Exceptions need
  the reviewed acceptance record and exact case/pair/hash bindings. They do not clear new wrong
  adjudications, schema holds, identity mismatches or other models' errors. Do not manufacture
  admission_eligible_cases or relabel reused results as a verified independent holdout.
- Extend validator with a narrowly typed reviewed task-acceptance authority for this manual,
  report-only P3 plan. Generic validate_admission callers retain their current AA/holdout rules.
  This design must never replace evaluation=None with a fabricated passing dictionary.
- JEV's alternative is bounded remedy choice/support evidence on the actual approved packets,
  exact route/protocol/profile,24-observation provenance and reviewed task scope. No synthetic AA
  score and no general chat-model waiver. Other JEV tasks do not inherit this exception.

## Implementation slice B: first monitored report-only cycle

Do not reuse the daily production ledger's R2 requirement by deleting its guard. For the first
monitored cycle, use an explicitly isolated report-only execution plan:

- Existing WorkerDO owns inference claims, pacing, retries, request idempotency and result
  provenance. Registered exact routes only; candidate Gemini requires normal catalog compilation
  and a separately approved Worker deployment before this cycle can exercise the Worker path.
- The monitor persists immutable run-scoped request/response/report artifacts locally in evals/.
  It writes no production P3 observation objects and no P2 disposition events. Therefore it does
  not pretend a provider pause is a source-lease/CAS implementation. One controller lock prevents
  local duplicate execution; Worker recipe identities protect remote inference submissions.
- New CLI option and plan schema must be explicit, not an implicit fallback when B2/R2 is absent.
  Proposed option: --report-only-plan <reviewed-plan.json>; output directory exclusive/run-scoped;
  reject publication_mode live and unknown plan versions before storage or network initialization.
  The ordinary daily caller keeps its current strict CAS requirement until sliceC is approved.
- Preflight all admissions/evidence/route/schema/identity gates before any Worker submission.
  Start with one maintainer-selected finding/source. Persist one intent per recipe before submit;
  reconcile handles, never resubmit unknown outcomes. Stop on access/model/identity failures and
  retain manual outcomes; no paid fallback, model substitutions or secondary BeatAPI account.
- Chat runtime profile:16384output/default reasoning/no temperature/zero client retries under
 720-second Worker response ceiling. Profile hashes include renderer/prompt/schema as well as
  parameters. A bounded single-finding cycle cannot activate publication or auto-merge.

## Slice C: persistent DO-coordinated observation writes (separate contract)

No R2 bucket or broad replacement state system is introduced here. Before ordinary daily mode
can write a shared ledger without R2, specify a WorkerDO-backed append protocol including fenced
ownership, immutable response hashes, idempotency, crash recovery, budget/retention and B2 blob
referencing. Validate ownership atomically where the authoritative append is accepted; checking
an expiring lease and then separately writing mutable B2 state is not a safe replacement CAS.

A new remote lease/append API, Worker tables, migrations, client and tests must receive their own
file/function/API contract before implementation. Do not modify global maintenance_leases or
chapter workflows as part of remedy adoption. This slice is not required merely to save one
isolated report-only cycle, but is required before claiming persistent daily P3 operation works.

Cloudflare storage background: eachDO's durable storage is transactional and strongly consistent;
in-memory locks alone do not survive eviction. Use the existing namespace/bindings and reviewed
server boundaries rather than adding an R2 coordination bucket:
https://developers.cloudflare.com/durable-objects/best-practices/access-durable-objects-storage/

## Verification and acceptance

1. Pure plan tests: selected roles/order, wrong-stage responses, self-adjudication, family and
   route mismatch; old observation replay remains byte/ID compatible; no cross-plan reuse.
2. Loader tests: path/symlink escape, byte/hash tampering, stale prompt/policy/profile/catalog,
   missing authority, wrong pair scope, forged qualified flag and JEV evidence outside its task.
3. Adapter replay: all60 initial cases plus12 actual disagreement packets; report unsupported
   actions as holds without changing gold. Preserve55/2/3 selected-cascade baseline separately.
4. Offline orchestration: pending handle/restart, intent-before-submit, no duplicate unknown
   calls, wrong returned identity, malformed response and JEV neither/insufficient =>manual.
5. Isolated-cycle test: shadow/default caller remains closed; missing plan/evidence prevents
   network and state initialization; report-only plan cannot reach publisher or production ledger.
6. Run whole Ruff/format, targeted P3/evaluation/observation/report tests and full offline suite
   after source changes. Worker catalog route tests accompany any later registration slice.
7. Human-review design/schema differences and static readiness report. After code review and
   separately approved Worker registration/identity disposition, authorize one monitored finding.

Do not modify current config/remedy.yml mode/admissions/publication_mode, production provider
limits/catalog, Worker deployment, paid policies, official metadata, gold or original raw results.
A passing offline test is not a production-admission decision. Keep P4/P5 and merge gates intact.

## Superseding adoption policy — October26 Central

The maintainer approved `flash-deepseek-human-v3`: two initial roles only, no automated judge
round. The implementation delta touches the already approved consensus/report, runner/observation,
config and daily-caller modules. `P3RolePlan.chat=None` identifies the two-role contract; strict
config/report literals include the new immutable plan. Older role maps remain replayable.
`P3ReportInput.initial_failures` is optional and allowed only for the human-review plan; empty
values are excluded from legacy report hashes. Failed request observations use explicit failure
kind and unverified returned identity instead of fabricating a vote. Reports include request/recipe
references and produce issues even while a peer's existing handle is pending. Worker/local raw
receipts remain separate. Bookkeeping failures propagate; unchanged terminal failures are not reasked.

The config selects the plan behind shadow/report_only/empty admissions. Two proposers alone need
fresh admission evidence; the older four-role review bundle cannot grant it. No new benchmark
score, route qualification, live run, deployment or publication authority is manufactured here.
Current tests cover agreement, disagreement, validator holds, invalid/failed responses, pending-peer
failure, repeated runs without resubmission, rejection of judge observations, and legacy replay.
See `evals/remedy/qualification/human-review-v3/README.md` for the exact next operational gates.

The user-approved agreement-to-PR behavior also requires the existing publication boundary:
citypods/remedy_p3_publication.py and scripts/remedy_p3_publish.py now receive the explicit
configured role plan; audit_remedy_report emits it for nonlegacy reports. The boundary rejects
a mismatched plan and recomputes the actual initial pair. Legacy reports retain their default.
This narrow plumbing correction is covered by tests/test_remedy_p3_publication.py and does not
enable live publication or alter validators/budgets.
