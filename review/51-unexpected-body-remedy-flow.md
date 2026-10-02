# 51 — Unexpected-body remedy: complete coverage, bounded decisions

**Status: L2, proposed design; future implementation requires final maintainer approval.**
**Date:** 2026-10-02. This document does not change production model routing or enable auto-merge.

## Outcome and scope

Onboard a city's complete available archive once, approve its subscription taxonomy, and encode
that policy in selectors and regression evaluations. Maintenance should then discover genuinely
new bodies and formats, rather than repeatedly rediscovering historical spellings. The target is
manual decisions per established city approaching zero, without hiding unknowns or silently
excluding legitimate recordings.

This is a maintainer-authorized follow-up to PR #1747, outside the usual implementation queue.
The immediate work is four separate reviews: TIF migration (#1973), remedy guard (#1974), historical
coverage recommendations (#1975), and this design. Future implementation is a separate approval.
See [TIF evidence](tif-coverage-2026-10.md) and
[historical inventory](historical-feed-coverage-2026-10.md). Those documents own the census and
individual category recommendations; this document owns the improvement plan.

## Decisions supplied by the maintainer

- Aggregate-only Dallas/Fort Worth TIF subscriptions, preserving archive records and migrated URLs.
- Include every legitimate public meeting recording, including sparse and historical bodies.
  Bond, charter and other time-limited programs use one city feed per family; official titles
  retain program/year. A retired body's recordings remain available.
- Separate city PID aggregates (including the future-city default), city public-input and
  public-briefings aggregates are approved; actual recordings still require
  verification and promotional clips remain excluded.
- Explicitly exclude promotions, ceremonies, staff training and municipal TV shows. Persist the
  reason so recurring detection does not create another proposal.
- Review capacity: 5–8 clear medium/high-confidence PRs per week, at most five decisions each;
  about twelve active directional issues. Rejected automatic PRs become refinement issues.
- Allow independently reviewed, evaluation-qualified alias types to merge automatically;
  medium-confidence decisions get self-contained PRs; low-confidence suggestions get issues.
- Escalate disagreement once to an independent stronger model, then ask the maintainer.
- Routine automation uses free routes. Subscription/frontier-agent work is acceptable for this
  historical cleanup and the initial complete-archive assessment of new cities.
- Evaluation cases and results live in `evals/`, permitting retirement/replacement comparisons.

Pending taxonomy approvals in #1975 are not implicitly approved by this design. The global default
for future-city TIF aggregates still needs an explicit decision.

## Why the existing flow produces too much weak work

PR #1747 accepted 35 decisions, deferred 16, requested manual review for 52 and reported 24
classification failures in a single review surface. Its generation pool allowed Gemini 3.6 Flash,
3.5 Flash and 3.5 Flash Lite, without independent semantic review. The token-overlap validator could
accept a wrong Dallas district because “Dallas” was enough shared identity evidence. Aggregate
subscription policy and identity of a public body were not distinct concepts.

Batching by source order and size can split related families; the compact prompt loses historical
context; later batches do not refresh the proposed feed inventory. Sparse labels are treated as
one-offs without determining whether they are a legitimate body, an alias or non-meeting media.
Unresolved archived labels can recur because a report/PR is not a durable policy disposition.
The audit is scheduled daily; the maintainer's weekly review experience is not its execution cadence.

The historical replay resolves 137 labels through current selectors plus the proposed TIF policy.
The remaining inventory has 680 source/label decisions and 2,011 provider rows. Provider rows are
not necessarily distinct recordings: Fort Worth repeats some clips across views. Counts alone
cannot establish either legitimacy or identity.

## 1. Detection and historical coverage

Fetch every available provider archive page/view before initial classification. Record source,
fetch time, completeness limitations, provider IDs and date ranges. Merge persisted records
append-only; absence from a fresh listing never deletes a recording. Deduplicate evidence by stable
recording identity while retaining all source-view references. A source cap or unavailable page
produces a visible incomplete-coverage status, not a false claim of full coverage.

Group evidence by city, source and candidate policy family before token budgeting. Include current
feeds, approved family policies, existing exclusions, official source evidence, positive/negative
examples, historical counts and unique-recording counts. Names, district numbers and official
metadata remain evidence; the LLM may not rewrite them. A shared city or topic word is never proof
of identity. Missing official evidence means abstention, not fabricated certainty.

Observe newly seen labels even when broad selectors already match them. Unexpected-body detection
alone cannot reveal false inclusions introduced by a broad rule. Replay proposed selectors against
the full frozen archive and adversarial negative cases, recording every newly included recording
and its target. An archived label is resolved only when coverage/exclusion is actually verified.

Use the audit's evidence artifact where possible instead of scraping again for each LLM job.
Before mutation, verify its config/policy hash and freshness; refresh affected source evidence when
stale. Historical cleanup and city onboarding are bounded frontier-assisted projects; maintenance
receives only changes since their confirmed baseline, plus explicit unresolved cases.

## 2. Policy and a durable decision ledger

Extend the existing `remedy_policy` machinery beyond TIF after each category is approved. Keep
subscription aggregation separate from body equivalence. A policy names the city/source scope,
family, permitted selector forms, exclusions, identity constraints, migration behavior and linked
regression cases. Do not introduce a hardcoded city list or infer a global policy from one city's
approval. District-family subscription rules must not imply district renames.

Maintain an append-only decision history keyed by city/source and normalized label/family, with
recording references, evidence hash, config/policy version, prompt/schema version, actual model
route and reasoning effort, independent verdict, PR/issue link and human disposition. Proposed
states: covered, excluded, proposed, in-review, refinement-needed, blocked and resolved. These are
future schema requirements, not existing state names.

A rejected PR creates/updates one refinement issue and suppresses regeneration of the same decision
until material evidence or policy changes. Passage of another week is not new evidence. A closed
issue must retain its decision and reason. Concurrent runs use the repository's existing CAS/claim
conventions; a stale run cannot overwrite a newer disposition. Cheap idempotent bookkeeping is not
subject to the expensive-work stop gate. Exclusions affect selection, never archive retention.

## 3. Model admission, independent judgment and escalation

AA is an admission baseline, not proof of project accuracy. Use the same AA Intelligence Index
version and reasoning variant across comparisons. On 2026-10-02, v4.3.2 reports
[Gemini 3.8 Flash High 41 and Gemini 3.7 Flash High 39](https://artificialanalysis.ai/models/comparisons/gemini-3-8-flash-vs-gemini-3-7-flash).
The initial minimum is the 3.7 High baseline, with 3.8 High preferred. Diversity candidates meeting
that baseline are [Kimi K3 Max 44](https://artificialanalysis.ai/models/kimi-k3),
[GLM 5.3 Flash 42](https://artificialanalysis.ai/models/glm-5-3-flash), and
[DeepSeek V4.1 Flash Max 39](https://artificialanalysis.ai/models/deepseek-v4-1-flash).
These dated scores must be refreshed before admission; effort variants are not interchangeable.

The configured catalog supplies Gemini routes, NVIDIA Kimi/DeepSeek routes and an OrcaRouter GLM
route. Read catalog limits at execution; do not increase ceilings here. Availability and support
for the required reasoning setting have not been live-tested in this work. In particular, the
logical Gemini 3.7 alias can fall back to 3.6/3.8/3.5: admission must inspect the actual upstream
model and effort, not only the pool's name. Remove below-floor fallback from this verb after
project evaluation; exhausted free capacity defers work rather than downgrading judgment.

A proposer produces a structured claim with citations, alternatives, uncertainties, policy and
coverage impact. An independent reviewer from a different model family first classifies the
same evidence without seeing the proposer's rationale, then checks the proposed change. Shared
provider accounts or a second call to the same model are not model diversity. Deterministic
city/source, district-number, ownership, schema and selector-impact checks remain mandatory.
Agreement is supporting evidence, not ground truth. Disagreement gets one independent stronger
adjudicator if an admitted free route is available; otherwise queue human review. Do not create
an unbounded debate or silently use paid capacity.

JEV can judge bounded factual claims after its own project evaluation; it is not a substitute
proposer or exempted general reasoning model. Begin in shadow mode, measure its added value, and
require agreement only after a separate promotion decision. The maintainer identifies GitHub's
`BEATAPI_API_KEY` and a newly added dispatch route. The inspected catalog does not yet expose that
route, so reconcile the route PR before an L3 implementation. Do not create a new direct JEV client
or copy credentials. Reuse [review/49](49-judge-consensus-admission.md)'s judgment contract where applicable.

The current interactive remedy contract is direct-only and same-run (#1231). Preserve it for
proposer/reviewer initially. JEV shadow work may use the approved dispatch integration separately;
a mandatory asynchronous judge would change that contract and requires an explicit later gate.
Coordinate route admission/retirement with review/48 and telemetry with review/50; this plan does
not change dispatch infrastructure, Worker concurrency or provider budgets.

## 4. Rerunnable evaluation and confidence

`evals/remedy/manifest.json` and `gold.json` seed 27 regressions. They separate inputs from verdicts,
identify synthetic cases, retain uncertainty as null, and mark provisional category policies.
They were authored after reviewing #1747: they are training/regression seeds, not an independent
holdout or completed model comparison. No model was run for admission in this change.

P1 adds `scripts/eval_remedy.py`, following the existing evaluation conventions. Planned subcommands
are `freeze`, `run`, `report` and `rescore`; these commands do not exist yet. Freeze captures full
input evidence and immutable source/recording IDs. Run uses existing routing and records actual
upstream model/effort, prompt/schema/policy hashes, commit, raw structured response, errors, latency
and usage. Report separates unknown truth, abstentions, unsupported claims and transport failures;
rescore permits corrected gold with an auditable reason without discarding original results.
Never include gold in model inputs.

Build an independently adjudicated holdout split by source/family/recording, not random near-duplicate
labels. Cover wrong-city/district claims, unmarked joints, program-year variants, legitimate sparse
bodies, exclusions, mixed-format titles and new-city transfer. Gold requires official evidence and
human confirmation where policy is not already approved. Holdout promotion needs enough examples
per admitted alias type; the present seed alone cannot qualify any automatic merge.

Report precision of accepted changes, critical wrong-body/city/false-inclusion errors, recall of
legitimate new bodies, abstention and disagreement rates, and operational failure rate. Measure
review minutes, rejection/regeneration, open directional decisions and manual decisions per city.
Re-run when route/model/effort, prompt, schema, policy or selector machinery changes.

Suggested automatic-merge gate for approval: zero critical errors and a one-sided 95% lower bound
of at least 99% accepted-alias precision on representative independent cases. Under a binomial
assumption, 299 error-free accepted cases meet that numerical bound; correlated titles do not
justify that assumption. Report clustering and do not pool unrelated alias types to hide failures.
Exact gates must be settled before L3; start with shadow evaluation and manually reviewed PRs.

## 5. Review surfaces, feedback and autonomy

A decision is one coherent policy change with its necessary selector, exclusion, migration and
evaluation updates. Up to five independently assessable decisions belong in a PR; prefer one
city/family and avoid mixing outcomes that a reviewer could reasonably approve differently.
Publish 5–8 such PRs per week, counting already open work so a queue cannot grow unbounded.
Keep roughly twelve active directional issues; persist excess candidates with visible backlog
counts rather than dropping them. Use fair source/city scheduling, stable decision IDs, and reuse
existing PRs/issues instead of opening duplicate branches every run.

Each PR shows: official evidence and uncertainty; why this is an alias/new body/aggregate/exclusion;
which policy authorizes it; alternatives rejected; unique historical recordings newly covered;
selector positive/negative replay; independent reviewer verdict and actual routes; eval results;
feed URL/UID/retention consequences. A count of accepted proposals is not evidence of correctness.

High confidence: only evaluated, independently reviewed alias types within approved ownership and
family policy may auto-merge after clean CI. Never auto-create a family, change sources/credentials,
rename official metadata, alter retention/UIDs or resolve an unknown identity. A selector extension
must prove bounded permitted coverage and pass negative tests; widening ownership is a policy
decision even if described as an alias. Medium confidence gets a small human-review PR. Lower
confidence or unresolved disagreement gets a suggestion issue with the exact missing decision.
Rejected PRs enter that third queue and cannot regenerate unchanged.

## Implementation sequence and file/function plan

| Step | Deliverable and acceptance | Planned touch points |
|---|---|---|
| P0 | Finish historical census and approved category rules; all legitimate recordings covered or explicitly investigated; verify migration/exclusion evidence | #1973–#1975, `config/feeds/`, historical inventory, fixtures |
| P1 | Rerunnable route/effort comparison, held-out truth, no below-floor fallback; report unknowns/errors honestly | `evals/remedy/`, new `scripts/eval_remedy.py`, existing model routing/catalog, evaluation tests |
| P2 | Policy-scoped evidence and durable rejection/exclusion history; replay all archive coverage and newly matched labels | `citypods/audit.py`, `citypods/audit_remedy.py`, config validation, existing storage CAS helpers, new ledger module to specify at L3 |
| P3 | Independent family reviewer, once-only escalation, shadow JEV dispatch integration after route reconciliation | `classify_unexpected_bodies`, `validate_proposals`, prompt/schema contracts, existing LLM/judge APIs |
| P4 | Small PRs, issue refinement, queue fairness and review-cap enforcement | remedy workflows, remedy command scripts, `format_remedy_markdown`, ledger feedback consumer |
| P5 | Qualified alias auto-merge pilot and city-onboarding coverage gate; evaluate manual-work trend | remedy workflows, `.github/ADD_CITY.md`, admitted-type policy and holdout |

For each step, produce an L3 breakout/issue with exact file names, function signatures, schemas,
fixtures, tests and rollback before implementation. The table is L2 scope, not permission to invent
production code paths. Reconcile current code/catalog against those specs before promotion.

Required tests include ledger idempotence/CAS and rejection replay, source-scoped exclusions,
archive deduplication, selector false positives, independent-review failures/timeouts, one escalation,
actual-model fallback rejection, five-decision boundaries, weekly/open queue caps, and auto-merge
permission violations. Offline CI must not require credentials; opt-in live evaluations record
cost/quota and never rewrite production feeds. Run whole-repository Ruff and applicable full tests.

Do not modify audio stages, pipeline versions, UID rules, archive retention, Worker scheduling,
provider ceilings, paid fallback or official metadata. Initial operational rollout: shadow only,
then small manually reviewed PRs, then opt-in qualified alias automation. Disable automation on a
critical false inclusion/identity error, retain ledger evidence, revert the feed rule and reopen the
affected decision. Restoring selectors must preserve records and migrated subscription URLs.

## Final decisions before development-ready promotion

Approve the five implementation steps P1–P5 after the historical P0 work, including free-route quality
floors, shadow JEV, durable feedback, bounded PRs/issues and onboarding evaluation. Confirm the
statistical alias gate or choose a simpler manual-pilot gate; decide how incomplete provider
archives block initial onboarding; settle global family defaults. Do not treat a published design
PR as authorization to implement these future steps.
