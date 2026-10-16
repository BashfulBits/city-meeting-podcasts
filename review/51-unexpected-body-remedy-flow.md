# 51 — Unexpected-body remedy: complete coverage, bounded decisions

**Status: P0 complete; P1 evidence accepted; P2/P3 implementation shipped in #2444, #2447, and
#2449; runtime route admission/activation and P4–P5 remain gated.**
**Revised:** 2026-10-15 after PR #2449 merged and the evaluated P3 model parameters were recorded.
This specification does not itself change production routing or enable auto-merge.

## Current P0 completion

The [final report](evidence/p0-completion-2026-10-09.md) supersedes the dated P0 status snapshots
below. All 680 baseline patterns have dispositions. The retained snapshot has 26,555 entries.
[Post-merge verification](evidence/p2b-postmerge-verification-2026-10-10.md) corrects the older
P0 helper’s omitted global exclusions: 1,189 entries are unassigned under actual CLI defaults,
compared with its historical count of 411. No production rule or raw record changed. Public search/archive-only filtering and Dallas #2171 publication
are verified. The six carried evidence cases were closed by approved limited-evidence dispositions on
October 9, 2026 Central; see [closeout](evidence/historical-six-case-closeout-2026-10-10.md). This does not complete model admission, P2–P5 or all municipal historical research.

## Outcome and scope

Onboard a city's complete available archive once, approve its subscription taxonomy, and encode
that policy in selectors and regression evaluations. Maintenance should then discover genuinely
new bodies and formats, rather than repeatedly rediscovering historical spellings. The target is
manual decisions per established city approaching zero, without hiding unknowns or silently
excluding legitimate recordings. Approved reusable policy templates and evaluation cases must
serve both maintenance and onboarding; reviewed city/source identities remain local.

This is a maintainer-authorized follow-up to PR #1747, outside the usual implementation queue.
The initial reviews merged as TIF migration (#1973), remedy guard (#1974), historical coverage
recommendations (#1975), and the L2 design (#1976). Historical recommendations still need their
implementation PRs; merging the inventory did not complete P0. The maintainer approved the
P1–P5 structure and final policy choices in chat on 2026-10-02.
See [TIF evidence](tif-coverage-2026-10.md) and
[historical inventory](historical-feed-coverage-2026-10.md). Those documents own the census and
individual category recommendations; this document owns the improvement plan.

## Decisions supplied by the maintainer

- Specific continuing standing committees, including Council subcommittees, have dedicated feeds
  across cities unless official evidence establishes a temporary body. Seasonal schedules and
  Council-only membership do not make a committee temporary. Addison CPC uses this default;
  temporary programs retain the approved family-feed policy. This was directed on 2026-10-04.
- Across all cities, including future onboarding, genuinely jointly convened meetings publish the
  same recording in each officially participating
  body's feed, preserving the recording and stable episode identity. This was explicitly approved
  on 2026-10-04. Membership overlap, a topic mention, or separately convened meetings bundled in
  one video do not prove joint participation. Existing canonical search ownership stays stable;
  unresolved ownership/timeline cases require evidence before activation.
- Addison public-input migration (approved 2026-10-04): preserve the Town Meetings feed URL,
  display `Addison: Public Input`, and limit it to verified town halls/community meetings/open
  houses. Verified educational briefings use a separate Public Briefings feed. Verified ceremonies
  and promotional clips leave podcast feeds while raw records/UIDs remain. Unproved identities
  retain evidence gates; this is a policy decision, not an executable recording assignment.
- Addison BZA/Appeals (approved 2026-10-04): retain one subscription and its existing URL,
  display `Addison: Zoning Adjustment & Appeals`. Official evidence establishes that BZA serves
  as Appeals; shared members alone would not justify combining other institutions.
- Historical uncertainty (maintainer direction 2026-10-04): unresolved sweeps must surface known
  historical recordings and false inclusions as well as apparent new bodies lacking proof.
  Archived labels, current selector matches and age cannot suppress unresolved cases. Each case
  retains stable recording IDs, missing evidence and a concrete next action; closure requires
  an evidenced disposition or explicit approval. Current unexpected-body detection does not
  satisfy this requirement; mature it in the P2 evidence contract before claiming full coverage.
- Aggregate-only Dallas/Fort Worth TIF subscriptions, preserving archive records and migrated URLs.
  One TIF aggregate per city is also the approved default for future cities.
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
- Shared approved policy templates are defaults for new cities and new historical evidence.
  Onboarding and maintenance use the same resolver, replay and case catalog; matching an approved
  rule does not require another LLM decision. This clarification was approved on 2026-10-03.

A–F taxonomy directions in #1975 are approved; individual uncertain identities remain held.
The future-flow structure is approved. Only a phase marked L3 with its predecessor gates satisfied
is executable; exact phase contracts and tests below replace the earlier open design decisions.

## Classification settlement hold — lifted 2026-10-04

The maintainer paused further CodeRabbit requests until the policy is settled, applied across
Addison bodies, spot-checked in other cities, and reflected in stable onboarding goals. The
[all-label Addison audit and shared-policy draft](evidence/addison-body-policy-audit-2026-10-04.md)
records confirmed selector overlaps, gaps, cross-city examples and proposed acceptance rules.
This historical hold superseded the earlier proactive review cadence. The maintainer explicitly
lifted it at 19:50 UTC on 2026-10-04. It no longer restricts review requests; current review scope
and pacing follow the maintainer's later instructions and repository contribution policy. The
dedicated CPC feed was prepared, not evidence that the wider city taxonomy was settled. General
standing-body preference was approved; ambiguous joint/combined proceedings and exact city
migrations remained evidence-gated.

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

Approved reusable defaults are explicit versioned templates, not inferred global ownership.
A template describes a subscription family or body identity type, permitted selector/alias forms,
required official proof, exclusion boundaries and positive/negative/transfer cases. A city/source
instance binds that template to its verified identities and owning feeds. TIF/PID, bond/charter/
redistricting, public-input and public-briefings defaults are cross-city policy approvals; a local
board alias or rejected recording remains local unless separately promoted with transfer evidence.

Both onboarding and maintenance load the same templates, instantiate source-scoped policies,
resolve ownership and replay coverage before model work. Recognized observations update coverage
under an approved rule. Unknown identity, missing proof, conflicting evidence or new policy scope
uses the same escalation path in either flow. Shared words or another city's exact names never
establish a recording's owner. Initial city approval remains a full available-history assessment;
templates do not waive completeness, exclusions or identity verification.

A newly learned local decision may propose an explicit template improvement with independent
cross-city positive/negative cases and approval; it must not silently rewrite global defaults.
This is maintained policy and regression evidence, not training or implicit learning by an LLM.

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

### Proposed unmatched-meeting dispositions — for maintainer review

This is a workflow proposal, not an approved ledger schema or final policy. When a title does not
match a feed, a reviewer should be able to distinguish these outcomes:

1. **Do not include in the catalog.** The item is not useful city-government meeting content, is
   promotional or ceremonial, or has no usable recording. Keep the original provider record and
   explain why it is not being tracked. The existing no-recording flow remains responsible for
   ordinary cancellations and unavailable recordings.
2. **Keep watching.** Evidence shows a real public body or recurring meeting series, but the
   recordings in the catalog are not yet enough to justify a feed. Preserve the unmatched entries
   and attach newly related recordings to the same pending disposition. An unchanged weekly
   scan does not re-ask for a decision. Renewed review requires new related meetings or material
   evidence. This is not a feed assignment or a claim that an unrecorded meeting has audio.
   These keep-watching semantics were approved on 2026-10-10; P2c integration implements them.
3. **Add a rule to an existing feed.** Evidence identifies the meeting as the same body already
   served by a configured feed. Add a reusable, source-scoped title rule when possible and test
   positive examples plus similar titles that should remain out. Use a one-recording exception
   only when the exact event is proven and no safe title rule can express it.
4. **Propose a new feed.** Evidence establishes a distinct public body or recurring series, through
   official records, multiple matching recordings, or both. The reviewer proposes the feed and
   rule for human decision; detection must not create the feed automatically.

The maintainer asked that reviewers be able to choose between “do not include” and “keep watching,”
and still have explicit choices for an existing-feed rule or a proposed new feed. The exact evidence
requirements, recording-count threshold, storage fields, and interaction with the current
five-distinct-UID P0 cutoff remain for a later decision. Do not treat this proposal as authorization
to create a feed or change the P1/P2 implementation contract.

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
route. Read catalog limits at execution; do not increase ceilings here. The selected P3 candidates
were evaluated with provider-default reasoning, meaning no named effort override was submitted.
Named effort support on their exact routes remains unverified. In particular, the
logical Gemini 3.7 alias can fall back to 3.6/3.8/3.5: admission must inspect the actual upstream
model and effort, not only the pool's name. Remove below-floor fallback from this verb after
project evaluation; exhausted free capacity defers work rather than downgrading judgment.

The maintainer replaced the earlier blind-review/JEV-shadow proposal on 2026-10-14 with a
bounded consensus cascade. Two exact routes, Sol and GLM Flash, make the initial assignment
independently from the same evidence and policy. If their normalized decisions agree and pass the
deterministic validator, create the proposal and pull request. If they disagree, send one
expanded, provenance-preserving packet to DeepSeek V4.1 Flash and JEV. The adjudication packet
contains both initial proposals and only additional context selected by reproducible retrieval
rules; it never contains gold or hidden expected labels.

Agreement is evidence for a PR candidate, not ground truth or merge authorization. All routes
must have current physical-model identity, a matching evaluated reasoning setting/hash, maintainer-
accepted P1 evidence, available free capacity, and the required distinct serving families. The
configured catalog must be reconciled with those admissions before use. Deterministic checks for
city/source, owner, policy, schema, and selector impact remain mandatory even when models agree. Every
generated PR remains subject to human review and merge; this policy does not authorize production
feed changes, auto-merge, paid fallback, or quota increases.

JEV's diagnostic used the existing local pilot only after its Worker upload path failed. That
diagnostic does not qualify JEV for production dispatch. Production P3 remains gated on the
documented review/49 dispatch integration, a remedy-specific JEV evaluation/admission, and the
same identity, quota, and free-route checks as the other judges. Do not copy a direct client or
credentials. The local pilot is not an implementation substitute for the dispatch integration.

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

The approved automatic-merge gate is qualification of a narrow alias type, following shadow and
manually reviewed PRs: independently verified truth, adversarial negatives, a diverse reasoning
reviewer, and replay of every affected historical recording. A maintainer explicitly enables each
type and its scope in reviewed config. The 27 seeds alone qualify nothing. No statistical 99%
guarantee, 299-case requirement, minimum calendar period or pooled success threshold applies.
Missing evidence or a failed critical case keeps that type manual. Any critical identity or false
inclusion error suspends the affected type and route/prompt configuration pending review.

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
selector positive/negative replay; initial/adjudication results and actual routes; eval results;
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
| P3 | Sol + GLM Flash initial consensus; one DeepSeek V4.1 Flash + JEV adjudication on disagreement; validated PR candidate or checkbox-based manual issue | `classify_unexpected_bodies`, `validate_proposals`, prompt/schema contracts, admitted route adapters, review/49 dispatch API |
| P4 | Small PRs, issue refinement, queue fairness and review-cap enforcement | remedy workflows, remedy command scripts, `format_remedy_markdown`, ledger feedback consumer |
| P5 | Qualified alias auto-merge pilot and city-onboarding coverage gate; evaluate manual-work trend | remedy workflows, `.github/ADD_CITY.md`, admitted-type policy and holdout |

The build contracts below name the files, interfaces, schemas, tests and rollback for each phase.
P1 can be implemented from its issue. P2–P5 remain predecessor-gated: record the required evidence
and open a scoped issue before marking a phase L3. Unknown live route capability must be reported,
not filled in with invented results. Reconcile current code against these contracts before coding.

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

## Final decisions — resolved

The maintainer approved these choices on 2026-10-02:

1. Build the exact implementation specification into this document; execute the approved P1–P5
   progression through phase issues and evidence gates.
2. Use narrow, explicitly approved alias-type qualification instead of the 99% statistical gate.
3. Permit incomplete-archive onboarding only through a documented maintainer exception; resolve
   all available legitimate recordings and display the limitation. Later history reopens assessment.
4. Use city TIF aggregates by default. PID remains a separate city aggregate. Bond, charter and
   redistricting span programs/years; public input and verified briefings use the approved families.

### Route qualification is outside leagues

`audit-remedy` and `city-onboarding` remain outside review/49's leagues. Low call volume, heterogeneous
source evidence and a shrinking, increasingly difficult residual make production rankings unstable.
Judge agreement is not independent truth. No automatic trial share, periodic promotion/relegation
or minimum-volume incentive is added to this task.

Compare candidate models on identical frozen inputs and report by decision type, source/family,
truth provenance and evidence quality. Replacement is event-driven: retirement, new candidate,
route/effort changes or a detected regression produces a reviewed admission config PR. Keep
coverage/quality checks even when only one candidate is available; if either required initial
judge is unavailable or the two do not agree, use the bounded adjudication or a manual hold rather
than relaxing the consensus rule. Shared JEV reliability may be measured by review/49, but
remedy-specific claim accuracy still needs this lane's verified truth.

## Build contracts

All symbols and files labelled **new** below are implementation deliverables, not existing APIs.
Existing functions retain their current behavior for callers not opting into new contracts.
Use typed dataclasses/Pydantic models; reject unknown persisted/config fields and unsupported
schema versions. Time values are UTC ISO strings; hashes are SHA-256 of canonical JSON. No new
third-party dependency is required.

### P0 — historical policy application boundary

Continue the approved A–F inventory in city/family PRs of at most five decisions. Each contains
source/agenda evidence, exact positive/negative selector replay, remedy guard changes and cases
under `evals/remedy/`. G is investigation-first; no automatic catch-all feed is approved.

P0 guard prerequisite extends existing feed-level `remedy_policy` metadata with optional exact
`identity_names` and the approved `pid`, `bond`, `charter`, `redistricting`, `public_input` and
`public_briefings` aggregate families alongside `tif`. Identity-only named-body policies omit the
family. For assignments, exact reviewed owners override competing holding clues; multiple exact
joint owners remain required. New-feed duplication remains blocked by any relevant policy.
Only reviewed exact identities authorize non-TIF ownership; family/member clues hold new
labels for evidence. Known topic-bearing TIF labels also require reviewed identity; unseen topic
wording can still pass the retained marker rule, so newly matched observation/replay remains P2
work. These local guards
are implemented in `citypods/config.py` and `citypods/audit_remedy.py` with their existing tests;
they do not implement P2's persistent decision ledger or enable model admission/automatic merging.

P0 selector prerequisite: optional `source.body_exact` lists complete normalized labels, rejects
wildcards/blanks and reuses `matches_exact_body_label`, including repeated provider copies. Its
union with `body`, `body_any` and `body_includes` restricts exact-only feeds without changing their
source namespace or UID inputs. It enables separate committee/Open House ownership; it does not
create an automatic ownership approval channel. Named implementation files are `citypods/bodies.py`
(`ExactBodyLabel`, selector construction/matching), `citypods/config.py` (validation/source identity),
`citypods/records.py` (source-key exclusion), `citypods/audit_remedy.py` (evidence, transport exclusion
and redundant-alias no-op), `citypods/search.py` (selector fingerprint), and their existing tests,
including `tests/test_search.py` for
retained-record search ownership. Acceptance includes live/retained selection,
negative suffix/topic labels, mixed selector unions, unchanged UID/source transport and whole-repo
checks. Include `body_exact` in search shard fingerprints only when configured, so exact-rule
changes rebuild cached destinations while legacy selector hashes remain unchanged. This small
cache integration scope was authorized by the maintainer on 2026-10-03. No feed configuration is
changed by this generic prerequisite.

The census used for P0 must include both persisted records and all available provider observations,
with explicitly recorded gaps. Restore historical sources separately where provider migrations
changed namespaces; no new cross-provider UID joins without their reviewed evidence. Count
provider observations and unique recordings separately. Preserve source URL ordering, authors and
existing UID assignments; never deduplicate by body/title/date alone.

P0 exit: every inventory row links to applied coverage/exclusion, an evidence-backed unresolved
issue, or a documented unavailable-source exception. An unresolved legitimate recording is not
“covered.” The current 680-row inventory is a starting snapshot, not a permanently complete census.
P1 can supply evaluation tooling while P0 is being completed; P5 requires an approved city baseline.

### P0 current-selector crosswalk — 2026-10-06

The row-level replay at
[`historical-selector-replay-2026-10-06.csv`](evidence/historical-selector-replay-2026-10-06.csv)
reconciles all 680 frozen inventory IDs against selectors in main and the cached source snapshots.
Input hashes, the code revision, and limitations are recorded in its companion JSON manifest.
It found 14 fully selector-matched labels, one partial label and 665 labels with no match; 40 of
2,028 cached UID/label associations match a feed selector. It links only 20 associations across
nine inventory labels to the 169-case register. This is not completion evidence: the cached inputs
were last written October 3, and selector eligibility does not establish correct ownership or an
approved exclusion. The missing case links and all uncovered labels remain P0 work. Do not equate
the register's 11 open cases with the full historical backlog or close a row by count alone.

Maintainer decision (2026-10-06): create or reuse a case at exact source-key/UID granularity only
after case-by-case evidence confirms a real public recording. Keep unverified associations in the
crosswalk; do not bulk-create cases or infer coverage/exclusion from selector gaps alone.

### P0 completed local rule replay — 2026-10-07 (historical snapshot)

The current working-tree feed rules were replayed against 26,555 cached source entries from 42
source namespaces. The [final report](evidence/p0-forward-rule-replay-2026-10-07.md) and its linked
CSV/JSON outputs record every added placement, removed placement and unmatched source/UID entry.
There are 680 historical title patterns and 2,028 source/UID listings in the frozen baseline. All
680 now have a recorded outcome: 517 patterns/1,664 listings assigned to local feed rules, one
archive-only pattern/listing, and 162 patterns/363 listings marked not to pursue. New bodies meet
the five-UID cutoff except the previously approved Denton Bond Oversight Committee; approved aliases
and additions to existing bodies are allowed below that cutoff. The Oct. 7 follow-up routes Dallas
Subdivision Review Committee recordings to the existing City Planning Commission feed and Denton
Capital Improvement Advisory Committee recordings to the existing Planning and Zoning Commission
feed, based on official city descriptions of each committee's parent.

The replay adds 1,965 feed placements across 1,942 source/UID pairs and removes 30 incorrect Fort
Worth City Council placements for the Minority Leaders and Citizens Council civic-program series.
It leaves 413 source/UID entries with no feed: 405 not to pursue, two ambiguous Dallas CBTF recordings
that remain under open source-binding cases, and six Addison archive-only items awaiting live
visibility verification. These are catalog entries, not necessarily unique underlying videos. Of the
requested follow-up titles, no existing feed represents Dallas's distinct Arts District Sign Advisory
Committee or Building Inspection Advisory, Examining & Appeals Board; those records are still
unassigned rather than being mixed into a different committee's feed.

This local result accounts for the 680-row policy baseline but does not finish P0. The new rules have
not yet been merged or deployed, and 11 evidence cases remain open. Some open cases concern items
already assigned to a feed; the replay count and the case count are not interchangeable. The two
Fort Worth 6334 assignments remain untouched, as the maintainer directed, until exact segment-to-UID
evidence is available.
P0 exit still requires the inventory rows to link to applied coverage/exclusion or a documented
open evidence/unavailable-source case, then live deployment checks. Do not claim an unresolved real
recording is covered merely because its title matches a selector.


### P0 deployed historical-coverage check — 2026-10-07 (historical snapshot)

The 680-pattern rules from the 2026-10-07 replay are now deployed after merge commits in PRs
[#2112](https://github.com/BashfulBits/city-meeting-podcasts/pull/2112) and
[#2111](https://github.com/BashfulBits/city-meeting-podcasts/pull/2111). Build and Deploy run
[#37643318454](https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/37643318454)
passed rendering, feed validation, search processing and Pages deployment. The live check found all
72 expected feed URLs available. Of 1,965 proposed placements, 1,909 appear in RSS. The remaining
56 each have a public page but no hosted audio player, so they are not materialized podcast episodes
and follow the existing no-audio path. This is not proof that no source video ever existed.

The two Fort Worth 3814 UIDs retain their original titles, source records, chapter data and audio.
Both direct pages in both participant feeds show the approved consecutive-proceedings note; CCPD
RSS contains both items. Council RSS is limited to its newest 500 items and begins on 2020-12-15,
after the July 24, 2020 recordings. Those two cases remain open until the maintainer accepts the
permanent page and bounded RSS history as sufficient publication proof. No assignment is removed.

The deployed root `meta.json` reports `search_shards: 0`; `/search/` and
`/data/search/manifest.json` return 404. The job exited successfully but no public search index is
available. Treat search availability and archive-only search-filter verification as a separate open
site limitation; do not claim that a live search index was verified.

The frozen baseline has a recorded outcome for every row: 517 title patterns assigned to feed
rules, one archive-only, and 162 not pursued. The register contains 169 cases: 159 resolved,
10 open. The Citizen Advisory compilation is accepted in Public Input for P0; its exact historical
program-to-recording link remains unknown, but an archive count of 11 does not establish a
missing part. At the time of this snapshot, Dallas GUID272574 had an approved Bond Program and
Public Info placement; the later Bond-only direction and correction contract below supersede it. The other Dallas CBTF recording still
needs exact event proof. The remaining evidence cases are recorded in
[`unresolved-recording-cases.json`](evidence/unresolved-recording-cases.json). Under the P0 exit
rule above, open cases remain explicit evidence work; the counts do not mean those recordings were
reclassified or their evidence was waived.

### P0 post-merge check — PRs #2164 and #2165 (2026-10-08)

PR #2164 merged the Dallas GUID272574 Bond Program-only correction, and PR #2165 merged the
Arlington exact-recording inclusion plus the reusable South Dallas/Fair Park title alias. Build and
Deploy run [#37720810791](https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/37720810791)
passed on merge commit `2f3e26f8`, which contains both changes. The later main build for unrelated
PR #2167 was still running during this check.

Live checks found Arlington's 2012 `Empty` UID on its permanent meeting page and archive page; both
returned HTTP 200 and the page retained the original Granicus clip link and hosted audio URL. The
UID is not in current Council RSS because that 2012 recording is outside the live feed's bounded
history. Dallas GUID272574 / UID `aa65e2aafc90711b` is in Bond Program RSS with the original audio
enclosure, and absent from Public Info RSS, matching the maintainer's final decision. A one-byte
range request to the enclosure returned HTTP 200 (`audio/mp4`); the UID is also absent from the
dedicated Task Force RSS. The South Dallas/Fair Park special
called meeting appears in that board's own RSS under its original UID. Full URLs and observations
are in [`p0-post-merge-check-2026-10-08.md`](evidence/p0-post-merge-check-2026-10-08.md). The
272574 register case is resolved; six evidence cases remain open.

The successful feed-health audit [#37726969713](https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/37726969713)
ran on commit `cb7018eb`, after #2165 and before the unrelated #2167 merge. It leaves issue
[#1623](https://github.com/BashfulBits/city-meeting-podcasts/issues/1623) open with four affected
feed paths and five provider rows: an Addison school ceremony (one), three cached Denton Animal
Shelter Advisory recordings represented by the sampled title (one row in the report), two Dallas
Building Inspection Advisory, Examining & Appeals Board recordings, and one malformed Fort Worth
title. No Dallas Building Inspection feed is configured. Under the accepted five-distinct-UID rule,
the Denton body (three recordings) and Dallas board (two) do not get new feeds in this P0 pass;
preserve their source records. The ceremony is not a meeting, and the malformed Fort Worth title
does not identify a body. These are known unmatched entries, not evidence of a successful selector
match. The current audit still counts them as unexpected; it needs an auditable accepted-disposition
path before it can distinguish reviewed exclusions from new errors. Do not add broad selectors to
make the warning disappear.

The 680-row worksheet has a recorded outcome for every row, but its last full replay predates #2165.
The exact Arlington record changes from not-pursued to assigned, and the Dallas alias adds a
reusable match. Regenerate the full replay from the same frozen inputs and record refreshed totals
before declaring the baseline current. Live `meta.json` still reports zero search shards; `/search/`
and `/data/search/manifest.json` return 404. The six approved Addison archive-only pages are
available, but live search-filter behavior cannot be checked while the public search index is absent.
Do not claim P0 archive-only search verification or overall P0 completion on this evidence.

The remaining six open register cases are the two Fort Worth clip6334 UID-to-segment bindings and
four Dallas Streets and Transportation bond recording/source bindings. Fort Worth assignments stay
unchanged and those two cases stay open under the maintainer's decision. The four Dallas records
remain assigned to their current feed pending dated exact-source proof. Their case details and
source identifiers remain in [`unresolved-recording-cases.json`](evidence/unresolved-recording-cases.json).

### P0 publication selection — inactive machinery shipped; activations separately gated

The maintainer authorized writing this specification; runtime projection changes and affected feed
publication remain gated. Proceed independently with historical batches whose rendered output has
no identity conflict. The held groups are Arlington Foundation [#1986](https://github.com/BashfulBits/city-meeting-podcasts/issues/1986),
Fort Worth PID [#1989](https://github.com/BashfulBits/city-meeting-podcasts/issues/1989) and Addison
CPC [#1991](https://github.com/BashfulBits/city-meeting-podcasts/issues/1991).

**Chosen approach:** finite, evidence-reviewed publication groups choose one **existing UID** per
verified recording for named public outputs. Keep every archived observation, UID, official field
and artifact intact. Do not merge records or assign new UIDs. Foundation/CPC have equal provider
GUIDs but multiple stored UIDs; PID has different view-specific GUIDs for the same verified clip.
Body aliases, titles, dates, model agreement and numeric clip IDs alone do not establish identity.
No automatic global deduplication or cross-provider/source joining is approved.

#### Configuration and proof contract

Use an optional top-level `publication_selection` key in a feed, outside `source` so the namespace
hash stays unchanged. Version 1 contains explicit groups with:

- `id`, `source_key`, `identity_kind` (`same_provider_guid` or `verified_granicus_clip`), source-scoped
  `identity_key`, members `{uid, provider_guid, record_fingerprint}`, fixed `preferred_uid`;
- official `evidence_refs` `{url, retrieved_at, content_hash}`, `approval_ref`, and publication
  `exposure` `{status, artifacts, rationale}`; exposure status distinguishes known-current,
  both-published, never-published and historical-unknown cases;
- explicit `search` opt-in: this feed's lists/RSS use its group, and only approved groups may also
  collapse their exact members in the source search shard. Other feeds' RSS remain unchanged.

Strictly validate keys/types/version, UID shapes, exact member GUIDs, preferred membership and
source key. Overlaps, conflicting winners or inconsistent repeated group declarations fail; input
order never chooses a winner. Fingerprints hash canonical JSON of source key, UID, provider GUID,
official body/title/date and canonical recording URL, excluding stage bookkeeping/audio/transcript
changes. No model output may introduce or edit this configuration.

Same-GUID groups require exact full GUID equality inside one source. Granicus groups require equal
reviewed host/clip identity, explicit views and direct recording/agenda equivalence proof. A future
view or extra UID is not automatically admitted. Date/title corrections, conflicting agendas,
changed fingerprints, missing preferred/member records and extra members on an approved identity
key hold the affected output instead of restoring duplicates silently.

Freeze public RSS/page exposure evidence before selecting existing-feed winners. Preserve the
previously visible UID when only one is evidenced; both/unknown cases need a recorded maintainer
choice rather than latest-date/lowest-UID guesses. Addison's date conflict requires official agenda
verification. The unpublished PID feed can choose a reviewed view per clip explicitly. Missing or
withheld preferred media never switches UID, splices artifacts or bypasses suppression; normal
availability is preserved and a different winner needs a new reviewed decision.

#### Future implementation boundary

Only these files/functions are in scope when the implementation gate opens:

- New `citypods/publication_selection.py`: strict `parse_publication_selection`, conflict-checking
  `load_selection_index(cities)`, `record_identity_fingerprint`, pure `select_feed_publication` and
  `select_search_publication`. Plans return existing objects, selected UIDs and diagnostic counts;
  unconfigured and ungrouped items remain unchanged.
- `citypods/config.py::_build_city` validates the optional metadata in `City.extra`; no UID, author,
  source-ID or transport changes and no new `City` identity fields.
- `citypods/run.py::_build_impl` loads the full configured index; `_process_city` validates before
  writes and separates raw retained episodes from public retained episodes after body selection,
  **before capping**. RSS/index/archive/speaker lists use public selection. `_write_meeting_pages`
  retains raw selected UID pages, preventing old URLs from disappearing; chapter sidecars needed
  by those pages remain usable. `_city_archive_hash` and render cache inputs include policy/proof
  hashes. `SourcePipeline` caches, ingestion and persisted record stores remain full archives.
- `citypods/search.py::build_search_index` preflights configured groups before shard writes and
  projects reviewed source groups before `_record_to_document`. `_city_for_record` uses the group's
  reviewed owning feed for canonical links, leaving ungrouped ownership unchanged. `_shard_hash`
  includes proof hashes/selected UIDs. Source search currently reads raw archives, so RSS-only
  filtering is insufficient. Never combine titles, dates, tags, transcripts or votes across members.
- New `tests/test_publication_selection.py`, existing config/run/search/feed/record tests, sanitized
  fixtures and `evals/remedy/` regression cases for the three issues, approved feed activations and
  lifecycle docs. No provider, record-merge/retention, audio/stage, storage, worker or credential edits.

Invalid selection leaves the affected feed's prior files/cache intact and reports a visible hold;
new feeds publish nothing. Search selection failure retains the prior complete manifest/shards/cache.
Validate before writing or pruning outputs, including tests with nonempty prior publication.
Cheap validation is not gated by the expensive-work stop budget. Static cache-version changes must
state the render/search rebuild story; no audio/ASR pipeline bump or forced artifact backfill.

#### Acceptance and remaining maturation

Ship machinery first with no active groups and prove unchanged existing output. Activate reviewed
city groups separately, with at most five decisions per PR. Required acceptance:

1. Full provider/persisted replay and separate legacy accounting; unchanged source namespaces,
   archived JSON, UIDs, official metadata, audio keys and URLs after rendering.
2. Actual record-backed audio RSS: Foundation's three affected clips appear once each; Addison CPC
   publishes five recordings while retaining six records; PID publishes three while retaining six.
   Selector counts alone do not pass. Verify archive/search/list counts as well.
3. One canonical search document per activated group; old UID pages/chapter URLs still work;
   ungrouped records and unrelated feed projections stay unchanged. Calendar/no-video rows unaffected.
4. Negative tests for wrong-host/source clip collisions, independent recordings with equal dates/
   titles, new/missing/preferred members, conflicting dates/agendas, changed labels, unavailable media,
   overlapping policies, cache changes and reordered input. No fallback UID or imported artifacts.
5. Proof failures preserve prior complete outputs/cache; aliases that would expose duplicates stay
   held until an approved group exists. Whole Ruff/format, offline tests and real CI preview pass.

Before activation L3, select each group's preferred UID with exposure
and official-date evidence, and prove output-preservation/cache failure behavior against current
writer/pruning paths. These are implementation gates, not permission to publish the held feeds now.
Rollback must freeze the last good public outputs or revert selector activation and group activation
**together**: removing an active group alone reintroduces duplicates. Archive records/artifacts and
old UID URLs remain. Winner changes explicitly disclose possible subscriber redownloads.

Risks remain explicit: finite mappings need review on identity changes; a chosen record can have less
complete artifacts than an alternate; past public UID exposure may be unknowable; raw historical UID
pages can show duplicate observations while canonical lists show one recording. Finite groups are a bounded historical safety step, not the steady-state maintenance design.
Before future-member admission L3, define a frozen, evaluated provider/view identity policy
with a sticky published winner. An approved proof type should cover routine repeats without a
new human decision; conflicting evidence or a proposed winner change still escalates. General
provider-wide canonicalization and cross-source joining remain outside this contract.

#### Development-ready split and implementation specification

**Inactive machinery: L3. Foundation historical activation: L3 (approved below).**
**Addison CPC dedicated feed: L3 under validation. PID and future-member admission: L2.**
This split preserves the maintainer-approved specification-first sequence. Machinery ships with
no configured groups. No implicit date repair, identity inference or new publication is enabled.
The approved directions for the three cities remain subject to evidence, not another taxonomy vote.

**Exact v1 data schema.** `publication_selection` is an optional mapping with exactly
`version: 1` and `groups: list[PublicationGroup]`. Empty groups are valid. Each group has exactly
these required fields; unknown keys and missing keys are errors at every nesting level:

| Field | Type and constraints |
|---|---|
| `id` | Nonempty source-local slug, `[a-z0-9][a-z0-9-]{0,63}` |
| `source_key` | Twelve lowercase hexadecimal characters; equals the declaring feed's source key |
| `identity_kind` | `same_provider_guid` or `verified_granicus_clip` |
| `identity_key` | Same-GUID: exact nonempty full GUID. Granicus: `https://host/clip/decimal-id` |
| `members` | At least two mappings, each exactly `uid`, `provider_guid`, `record_fingerprint` |
| `preferred_uid` | One explicitly listed member UID; never computed from sorting or availability |
| `evidence_refs` | Nonempty list of `{url, retrieved_at, content_hash}` |
| `approval_ref` | HTTPS GitHub issue/comment/PR link recording the reviewed decision |
| `exposure` | Exactly `{status, artifacts, rationale}`; nonempty rationale |
| `search` | Boolean; no truthy integer/string coercion |
| `date_resolution` | `null` or exactly `{official_date, evidence_url}` for a verified discrepancy |

Member UIDs are sixteen lowercase hexadecimal characters; fingerprints/content hashes are
64 lowercase hexadecimal SHA-256 strings. Provider GUIDs remain opaque exact strings.
`retrieved_at` is a timezone-aware ISO-8601 timestamp. Evidence URLs must be absolute HTTPS URLs;
configuration parsing performs no network fetch. Evidence retrieval uses `validate_source_url`.
Exposure `status` is one of `known-current`, `both-published`, `never-published`,
`historical-unknown`. `artifacts` lists `{url, retrieved_at, content_hash}` snapshots (may be
empty only for never-published or historical-unknown). A current absence cannot prove never-published;
the rationale must distinguish first activation from incomplete historical observation.

The configured feed is the owning feed; no separately editable owner slug is needed. Repeated
groups are allowed only when their complete canonical declarations agree and `search` is false.
A source group with `search: true` has exactly one owning feed. No member may occur in two groups;
nonidentical declarations of the same source-local group ID or identity key are errors. Sort groups
by `(source_key, id)` and members by UID for hashing; ordering is never precedence.

**Record fingerprint v1.** Hash UTF-8 canonical JSON (`sort_keys=True`, compact separators) of
exactly `{source_key, uid, provider_guid, body, title, published, recording_url}`.
Use the stored official values, including the stored timestamp string; missing values are `null`.
`provider_guid` uses the existing records/body helper's GUID precedence, not the public UID.
`recording_url` is the exact HTTPS provider_guid URL when present, otherwise stored video_url;
current retained records have provider_guid and video_url, not meeting_url. Do not rewrite
URLs or timestamps in records. A normalized URL identity used for proof is separate from this
exact observation fingerprint. Fixtures pin these verified record field names.

For same-GUID proof, compare the entire GUID and require one source namespace. For Granicus proof,
parse each official recording URL with `urllib.parse`: exact approved lowercased hostname, HTTPS,
`MediaPlayer.php`, one decimal `clip_id`, one decimal `view_id`, and no ambiguous duplicate query
keys. The evidence packet lists permitted views by showing the member URLs and official equivalence.
The identity_key excludes view only after that equivalence is reviewed. Same numeric clip on another
host/source is independent. Redirects, alternate provider URL forms or ambiguous GUID conventions
are not silently normalized into this proof type. Evidence must establish body ownership separately.

**Pure API and diagnostics.** In `citypods/publication_selection.py`, frozen dataclasses
`PublicationMember`, `PublicationGroup`, `SelectionIndex`, `SelectionDiagnostic` and
`PublicationPlan` hold parsed values. `PublicationPlan` exposes `held`, `diagnostics`,
`selected_uids`, `suppressed_uids`, `policy_hash`, `raw_items`, `public_items` and, for search,
`owner_by_uid`. Collections are immutable tuples/mappings; episode/record objects are returned
unchanged, never combined or mutated. No network/storage/write operations occur in this module.

`parse_publication_selection(raw, *, source_key, feed_slug)` parses the schema.
`load_selection_index(cities)` checks global declarations. `record_identity_fingerprint`
accepts `(source_key, record)`. `select_feed_publication(index, city, items, records)` validates
against the **full** source records, then suppresses nonpreferred members in body-selected items.
`select_search_publication(index, source_key, records)` projects only search-opted-in groups.
Ungrouped records preserve their ordering and ownership behavior. Feed groups do not invent a
body match: all members must pass that feed's current selector before group activation is accepted.

Syntax errors raise `ValueError` with feed/group context, before a build writes outputs.
Runtime proof failures return held plans with stable diagnostic codes:
`missing-source`, `missing-member`, `missing-preferred`, `fingerprint-changed`,
`guid-mismatch`, `identity-mismatch`, `unexpected-member`, `unresolved-date-conflict`,
`owner-selector-mismatch`, `group-overlap`, `winner-conflict`.
Diagnostics identify source/group/UID, never credential values or unrestricted provider payloads.
Unknown same-GUID or reviewed Granicus-identity members hold the group; do not publish duplicates
or choose a new winner. Different stored dates require non-null `date_resolution`: official_date is ISO YYYY-MM-DD,
evidence_url must occur in evidence_refs, and the preferred observation must match that date.
An approval packet establishes the official date; runtime validates the frozen declaration and
fingerprints, not the authority of remote content. Equal-date groups use null. Records stay unchanged.
No runtime fetch of approval/evidence links and no model-derived acceptance are permitted.

**Writer/cache integration verified against current code.** `_process_city` currently body-filters,
caps, hashes and writes meeting pages, speaker lists, archives, chapter sidecars and RSS. The new
projection runs after body filtering but before capping and **before every write/cache-hit decision**.
Keep `raw_retained_eps` for meeting pages and their sidecars; use `public_retained_eps` for
archive/speakers and its capped subset for RSS/feed lists. Sidecars use the uncapped raw retained
set so suppression or cap changes cannot prune a still-retained UID's chapter URL. Neither page
nor sidecar retention extends to invented/nonretained records.

An invalid feed plan returns `CityResult(status="held")`, `new_entry=None`, and visible diagnostics;
existing city files and that feed's cache entry remain byte-for-byte intact. Existing feed presence
flags derive from its previous output files so the global index does not falsely hide it. A held
new feed creates no directory or RSS. `_prune_stale_dirs` must retain configured held slugs and
aliases; `_write_aliases` must not overwrite held aliases. Add an optional held-slug set to these
existing helpers and their callers. Successful feeds may continue; cheap proof checks ignore stop().

`build_search_index` must validate all configured search groups against full loaded records before
`mkdir`, shard/cache mutation, asset writes or pruning. On any selection hold return `None`, emit
diagnostics, and leave prior complete manifest, shards and passed cache unchanged. Preflight is
performed even when cached hashes match. This guarantees preservation on **selection proof** failure,
not a new filesystem-wide transaction guarantee for unrelated crashes or storage errors. Existing
interrupt-related shard behavior is a separate hardening concern; do not widen this implementation.

`_city_for_record` accepts an optional explicit reviewed owner for projected winners. `_shard_hash`
and `_city_archive_hash` include canonical policy hash and selected UID list when configured.
Extend the existing feed render fingerprint per city with the same values before feed_content_hash.
Unconfigured feeds/shards retain their current cache hashes exactly; no global cache version bump.
A group/proof/winner change invalidates only affected RSS/pages/lists/search rendering. Audio,
ASR, stage versions, archived state and content-addressed keys are not invalidated or backfilled.

**Sticky winner and future policy.** v1's fixed existing preferred UID is the sticky winner.
Absence/unavailable audio holds or suppresses according to normal availability, never promotes an
alternate. A previously public UID cannot change without explicit review and redownload disclosure.
Automatic future-member admission is a separate L2 identity-policy extension, exercised through
P2's shared evidence ledger and frozen evaluations before activation: source/host/view proof,
unchanged official dates/body ownership, initial winner registration and prior exposure evidence
are required. Exact repeat of an existing validated observation needs no human decision. A new
member currently holds; it does not become a new feed proposal. No provider-wide rule, hidden
registry, extra persistent schema, or cross-source join is introduced by inactive v1 machinery.
The future extension must specify its persistent sticky-winner ledger before it becomes L3.

**Implementation slices and acceptance.** Implement inactive v1 machinery through
[#1997](https://github.com/BashfulBits/city-meeting-podcasts/issues/1997), followed by separate
activation work on #1986/#1989/#1991. Only the previously
listed files, these named dataclasses/helpers and existing writer helpers may change; add no deps.

Inactive v1 contract frozen: **Implemented in PR #1999**, merged 2026-10-04.
Parser/index/proof checks, feed/search projection,
raw meeting-page retention and pre-write holds are implemented without active city declarations.
This does not promote activation, future-member admission, or P2–P5 predecessor gates.
Tests use `tests/test_publication_selection.py`, `tests/test_config.py`, `tests/test_run.py`,
`tests/test_search.py`, `tests/test_feeds.py`, `tests/test_records.py`, sanitized fixtures and
`evals/remedy/` cases. Include:

- exact parser/fingerprint vectors; reordered declarations; extra keys/bool-as-version rejection;
  both proof types; wrong host/view/source; full-source extra member not body-selected;
- six-to-three/six-to-five record-backed RSS projections with playable stored audio, uncapped page
  retention, calendar rows, speakers/archive/search counts, no artifact mixing and unavailable winner;
- nonempty prior feed, page, chapter, manifest, shard and cache snapshots remaining identical on
  each proof failure; a matching cache must not bypass validation; first-build hold writes nothing;
- no-config byte-identical feeds/search/cache golden fixtures, unchanged source hashes/UIDs,
  byte-identical archive snapshots, no stage/audio backfill and repeated-run idempotence;
- whole Ruff/format, offline pytest and current-head CI/preview. No credentials/live model quota
  are required for machinery tests. Each activation requires refreshed official proof and current
  public exposure snapshots, actual retained UID selection and its own replay before publication.

Inactive machinery may be implemented from this L3 contract. Do not promote city activation or
future-member auto-admission merely because machinery tests pass. L3 requires the exact reviewed
winner/proof packet. The approved Foundation/Addison packets and exact scopes are recorded below;
PID winner selection and automatic future-member admission remain held.

### Foundation historical activation — shipped

Frozen historical activation contract: **Implemented in PR #2002**, merged 2026-10-04.

Maintainer approval: [#1986 comment 5976979529](https://github.com/BashfulBits/city-meeting-podcasts/issues/1986#issuecomment-5976979529).
The [dated proof packet](evidence/foundation-publication-selection-2026-10-04.md) records
same-source exact provider GUIDs, equal dates and both-published exposure. Keep existing dedicated
RSS winners `52ea444ed933464a`, `c73dbd3dab9b14cc`, `ccee5be42508e88d`.
Change only Foundation and combined Arlington feed configs: both historical labels, three exact
v1 groups with Foundation search ownership and equivalent combined declarations (`search: false`).
Foundation's official October 2026 board agenda establishes ongoing activity; correct its retired
classification without changing provider sources. Raw records, UIDs, audio and all old pages remain.
Tests extend `tests/test_publication_selection.py` with a sanitized retained-record fixture:
actual audio/video RSS, winner enclosures, unique search ownership in either config order,
full-source extra-member holds and unchanged unrelated Arlington projections. Full restored replay
selects Foundation 6→3 and combined/search 1,515→1,512; source namespace remains unchanged.
Rollback reverts both selector and group activation together. No audio/stage version bump or
backfill. This approves these three historical groups, not automatic future-member admission.

### Addison CPC dedicated feed — L3, implementation under validation

Maintainer direction (2026-10-04) supersedes the prior Council-feed placement: create a separate
feed for CPC, research its historical meetings, and encode minimal automatic classification rules.
Specific continuing standing committees, including Council subcommittees, have dedicated feeds
across cities. Seasonal schedules, Council-only membership and sparse recordings do not establish
a temporary body. Time-limited committees retain the previously approved city/program-family policy.
Do not infer body equivalence, copy recordings across cities, or bypass identity/exposure gates.

**Exact implementation scope:** new
`config/feeds/addison-tx-community-partnership-committee.yml`,
`config/feeds/addison-tx-city-council.yml`, `tests/test_publication_selection.py`, the existing
`tests/fixtures/addison-cpc-retained.json` if evidence requires it, the dated proof packet below,
and lifecycle docs. No provider, stage, storage, records or selector-runtime changes.

The new feed has city `addison-tx`, provider `swagit`, existing list URL
`https://addisontx.new.swagit.com/views/128`, and only
`source.body_exact: [Community Partnership Committee]`. Use title
`Addison: Community Partnership Committee`, author `City of Addison, TX`, blank podcast email,
and a description identifying its annual nonprofit-funding review role. Keep the existing source
namespace `abbf5e25e078`; do not switch source views or generate replacement UIDs.
Move CPC's existing `remedy_policy` identity/member guards and publication-selection declaration
from Council to the new feed. Remove Council's CPC `body_exact`, guards and group; other Council
selectors remain unchanged. The dedicated feed owns canonical search links. Ordinary new recordings
with the same exact normalized body label classify automatically; new spelling/format aliases
remain unresolved, and duplicate identity members require the existing proof hold/approval gates.
No LLM classification, substring rule, broad topic wildcard, or automatic UID merging is needed.

The [dated proof packet](evidence/addison-cpc-publication-selection-2026-10-04.md) records official
history and coverage limitations. Its approved same-source GUID `393215` group retains existing
July 9 UID `91efc2425e16e017`, official date `2026-07-09`, historical-unknown exposure and the
unchanged July 10 alternate. All six retained observations remain intact, producing five public
recordings in the dedicated feed. Meetings proven by minutes/agenda but without available recordings
stay visible coverage gaps, not fabricated episodes or a claim of complete recorded history.

Tests load actual repository config and verify five-item RSS/enclosure, six unchanged records,
source-key stability, dedicated search ownership, Council exclusion and unrelated-feed preservation.
Include exact-label positive/negative cases, new unique recording classification, and existing
missing/changed/unexpected duplicate-member holds. Full retained 822-record replay must preserve
Council's original 549 selected records, select six raw/five public CPC items and preserve search's
822 raw/821 public source observations. Rollback removes the dedicated feed and selection together.
No audio/stage version bump or forced artifact backfill; normal processing of newly discovered
available recordings is separate from this retained-record activation.

### P1 — evaluation harness shipped; physical-route admission remains L3

Evaluation harness implemented in PR #1987 (merged 2026-10-04). Its strict offline schemas
satisfy P2’s deterministic predecessor gate. This stamp does not admit any live model route;
physical-route admission still requires the reports and independent review below.

Tracking issue: [#1979](https://github.com/BashfulBits/city-meeting-podcasts/issues/1979).
Implementation: [PR #1987](https://github.com/BashfulBits/city-meeting-podcasts/pull/1987),
merged; offline tooling is shipped. Live comparison/admission remains a separate opt-in activity.
P1 tooling is complete; production route admission is not. The offline harness and strict direct
routing/provenance are implemented, with empty shadow admissions and no live model calls.
The maintainer resolved the three specification clarifications: hidden independently adjudicated
`expected_owner` truth, explicit blind-owner isolation, and mandatory frozen input hashes for
all evaluation candidates are implemented requirements. Named route effort capability metadata is
unverified; `provider-default` is accepted as a distinct setting and does not assert a named effort.
No candidate/admitted route is enabled. Live evaluation requires shared CAS-capable
quota coordination; “no production writes” below means no feed/audio/catalog mutation, not bypass
of shared quota bookkeeping.

**Purpose:** compare actual model/effort configurations on fixed truth, safely and rerunnably.
No production model swap or feed mutation occurs in this phase.

**Files permitted:** new `scripts/eval_remedy.py`, new `citypods/remedy_evaluation.py`,
`evals/remedy/{README.md,manifest.json,gold.json}`, new `evals/remedy/holdout/` and dated `results/`,
new `config/remedy.yml`, `citypods/compute/{base.py,llm_policy.py,llm_scheduler.py,llm.py}`,
`tests/test_compute_llm_scheduler.py`, `tests/test_compute_llm.py`, new
`tests/test_remedy_evaluation.py`, and the mandatory lifecycle docs.
No edits to Worker code, provider catalog/limits or production `REMEDY_MODELS` in P1.

New `RemedyCase` schema v2: `id`, `city`, `source_key`, `decision_type`, `input_kind`,
`policy_version`, `policy_status`, `body_label`, `claim`, `evidence_refs`, `recordings`,
`existing_feeds`, `family_policy`, `completeness`, `split_group`. Evidence refs carry URL,
recording/agenda ID, retrieved date, quoted bounded source span and content hash. Recording rows
carry existing UID when known, provider GUID, verbatim body/title/date and canonical URL.
Synthetic cases are labelled; adapted cases without grounding are regression-only. Existing v1
seeds migrate without changing their claims or inventing evidence. `gold.json` never enters a prompt.

Gold schema v2: case ID, nullable `supported`, rationale, evidence refs, adjudicator/provenance,
policy approval link, critical-error class, revision and superseded revision. Add optional hidden `expected_owner: str | None`
(default None); a non-null value is a reviewed feed slug, not a target inferred from the claim.
Reject blank owner truth. Leave the 27 seed owners unset until independently adjudicated.
Only verified,
approved-policy cases contribute to admission. Holdout groups cannot share recordings or near-
duplicate source/family label variants with tuned seeds. Correcting gold requires a reason and a
new version; prior results remain immutable.

New `RemedyEvalAnswer` wire contract: case ID, nullable supported verdict, proposed owner/action,
evidence-reference IDs, missing-evidence list and bounded rationale. IDs resolve locally against
inputs; unknown IDs fail the response. For blind reviewer experiments, evaluate independent owner
selection as well as claim support, and keep those scores separate.

Maintainer-authorized clarification (2026-10-03): `run --mode claim_support|blind_owner`
(default `claim_support`) selects a distinct experiment and prompt hash. `case_messages` accepts
the same optional mode. Blind inputs use opaque deterministic case IDs mapped back locally,
never semantically named seed IDs; omit claim, decision type, split group and proposer/tuning
metadata. Allow only city/source/body, official evidence/recordings, existing feed taxonomy,
approved policy fields and completeness. Existing-feed taxonomy exposes only slug/city/title,
podcast title/description, provider and body/body-any/exact/GUID selectors; omit proposal metadata.
Support both flat policies and frozen per-feed policy maps, filtered to known feed slugs.
Policy fields are limited to policy ID/version,
approval ref, aggregate family, exact identities and member names; seed evidence is omitted.
Gold is never read by run. Preserve the raw answer and map its opaque ID locally for validation.

`compare_results(..., mode="claim_support")` and the report artifact identify the experiment.
Owner scoring uses hidden `expected_owner`, independently of whether claim truth is known.
Missing owner truth is unknown, a null proposed owner is abstention, and failed/unattempted jobs
never score as correct. Blind-owner results cannot contribute to claim-support accuracy or recall.
Non-null owner truth must be an existing feed slug in the case taxonomy. No truth is inferred
from model agreement, the proposed claim or historical seed names.

Every evaluation candidate must supply manifest/prompt/schema/catalog hashes and match the
frozen run context. Missing hashes fail closed even for dry-run planning. Candidate gold hashes,
reviewed results and production admission refs may be absent until evaluation is complete;
production still requires all five hashes, qualified evidence and independent reviewed admission.
The mode-specific prompt hash prevents comparing differently exposed inputs as the same run.

New interfaces in `citypods/remedy_evaluation.py`:

- `load_cases(manifest_path, gold_path) -> EvaluationSet`: validate schemas and aligned IDs.
- `freeze_cases(evidence, policies, *, split, provenance) -> dict`: freeze inputs without truth.
- `compare_results(results, gold, *, manifest) -> EvaluationReport`: separate abstentions,
  transport/schema failures, critical errors and known truth; no consensus-as-truth scoring.
- `validate_admission(entry, catalog, evaluation=None, *, for_evaluation=False) -> AdmissionCheck`: check physical upstream,
  effort controls, free/direct capability, dated AA evidence and matching version hashes.
  The AA variant must meet the comparable Gemini 3.7 High baseline; approved project results
  and an explicit reviewed admission ref are required before a role is usable in production.

Planned CLI (not available until P1): `freeze --evidence FILE --policy-root config --out DIR`,
`run --manifest FILE --admission FILE --role proposer|reviewer|adjudicator --out FILE`,
`report --manifest FILE --gold FILE --results FILE... --out FILE`, and
`rescore --manifest FILE --gold FILE --results FILE --out FILE`. `run --dry-run` writes a request
plan, performs no network calls and reports zero model observations. Freeze cannot overwrite an
existing set without `--force`; forced output retains the superseded manifest/gold and hashes.
Rescore writes a separate derived report, never replaces raw responses. Live `run` requires
`--live` and positive `--max-cases`, a total logical-case cap across configurations.
Maintainer-approved on 2026-10-03: evaluation jobs set `max_provider_attempts: 2` in their inputs.
The immediate backend shares that budget across correction, capacity and pacing retries, disables
SDK retries, and records actual client invocation counts on results and safe failure rows.
`JobResult.provider_attempts` is optional/additive; ordinary production defaults are unchanged.
Non-immediate/dispatch paths reject this input before side effects. Reports distinguish first-call
and retried completion; remote provider/gateway internal retries are outside the client count.
Quota exhaustion records unattempted cases, rather than retrying indefinitely. No production writes are possible from these commands.

`config/remedy.yml` schema v1 has `mode: shadow|manual|qualified_alias`, `admissions`,
`qualified_alias_types`, `limits`, and `onboarding_exceptions`. Initial mode is `shadow`; admissions
and qualified types start empty. Limits: `decisions_per_pr: 5`, `prs_per_rolling_week: 8`,
`open_prs: 8`, `active_directional_issues: 12`, `evidence_max_age_hours: 24`.
Eight is a ceiling, not a target/minimum. Empty admissions produce a visible policy hold.

Each entry has `status: candidate|admitted` and identifies role, physical route IDs, upstream model,
model family, reasoning setting, effective request-parameter fingerprint, AA version/variant/
score/source/date, manifest/gold/prompt/schema/catalog hashes and reviewed result paths. Candidate
entries may omit result hashes/paths only when
`for_evaluation=True`; they still require the free/direct, AA, physical identity and effective-setting
checks. Live evaluation records candidate status and cannot publish or qualify changes. Production
requires `admitted`, complete version-matched results and the reviewed admission reference. This
avoids requiring a model comparison before a candidate can participate in that comparison.
Reasoning settings may be a verified named level or the explicit value `provider-default`.
`provider-default` means no named reasoning override is applied; it is not a named effort and makes
no claim about the provider's internal effort. Its parameter hash covers the route's compiled
request parameters. Named levels still require verified route controls. Unknown
settings or model identity fail admission. Role names map to `audit-remedy-proposer`,
`audit-remedy-reviewer` and `audit-remedy-adjudicator`; `audit-remedy` remains the protected
purpose for quota admission.

Add optional `allowed_route_ids: tuple[str, ...] | None = None` to `LLMRequestPolicy`.
`select_route` rejects physical IDs outside it **before** overflow/`also_serves` pool substitution
and quota reservation. `None` preserves current behavior; empty tuple admits nothing. P1 uses
this only for `run_immediate`; no deferred serialization or Worker change is introduced.
A non-None allowlist with any `run_inference` or queued/dispatch request is rejected, rather
than silently ignored by a transport that does not implement this gate.

Add optional fields to synchronous `JobResult`: `route_id`, `upstream_model`,
`reasoning_level`, `request_params_hash`. Defaults are None so existing callers remain compatible.
`LiteLLMBackend` fills them from the selected physical route and effective controls, including retry
results. Unknown/mismatched returned identity is an evaluation failure, not an accepted result.
Add validated optional `reasoning_level` to job inputs for immediate calls; `_lane_reasoning_level`
uses it before the existing lane default and `route_reasoning_controls` generates the provider
parameters. Do not silently drop unsupported effort; the admission validator rejects it.

P1 tests: identical inputs across routes; gold never serialized; wrong model through overflow or
`also_serves` blocked before call; None/empty allowlist semantics; actual retry route identity;
unsupported effort; unknown truth and failed calls never counted correct; candidate execution is permitted only in
evaluation mode and cannot satisfy production admission; adapted examples cannot
qualify admission; holdout overlap; missing/disjoint IDs; immutable raw results; offline dry-run.
Also test hidden owner truth never enters prompts, opaque IDs and metadata masking, owner truth
independent of unknown claim truth, owner abstention/failure/unattempted accounting, separate mode
metrics/hashes and missing candidate hashes. Use fake backends/ledger/catalog fixtures; no
credential requirement in CI.

P1 exit: offline tests and full checks pass; the harness writes traceable reports and strictly
constrains direct physical routes. Live evaluation is a separate opt-in run with existing free
quota/deadlines. No admission result or approved production route is assumed by this specification.

### P2 — scoped policies, complete evidence and durable feedback

**Gate:** P1 schemas exist. No model admission result is needed for deterministic P2 work.
**Permitted files:** new `citypods/remedy_policy.py`, new `citypods/remedy_ledger.py`,
`citypods/{config.py,audit.py,audit_remedy.py}`, `scripts/audit_feeds.py`,
`citypods/storage/routing.py`, `config/remedy.yml`, approved `config/feeds/*.yml`,
`citypods/remedy_evaluation.py` (only shared-config/template schema compatibility),
new `tests/test_remedy_policy.py`, new `tests/test_remedy_ledger.py`,
`tests/test_remedy_evaluation.py` (only shared-config compatibility), existing audit/config/storage
routing tests and lifecycle docs. Feed changes are still separate city/family decisions.

Extend validated feed `remedy_policy`: existing `aggregate_family`/`member_names` stay valid;
new optional `policy_id`, `version`, `identity_names`, `positive_case_ids`, `negative_case_ids`,
`approval_ref`, plus optional `template_id` and `template_version` identifying the approved
reusable policy. Supported families: `tif`, `pid`, `bond`, `charter`, `redistricting`,
`public_input`, `public_briefings`. Identity-only policies omit `aggregate_family`; they do not
merge independent bodies. Approved marker recognition belongs in `remedy_policy.py`, never in
an LLM response. Member/topic names are holding clues and cannot establish ownership alone.

P2 adds optional `policy_templates` to shared `config/remedy.yml` and its strict `RemedyConfig`
reader, defaulting to an empty list for existing config compatibility. New strict `PolicyTemplate`
entries contain `id`, `version`, `approval_ref`, approved scope (`cross_city` or `city_source`),
family/identity type, permitted transformations/selector forms, official-proof requirements,
exclusion/migration boundaries and positive/negative/transfer case IDs. Known keys/types only;
unknown templates/versions, unsupported forms and missing required proof fail closed. Templates
never carry a city's exact body names as global truths or automatically qualify a model/merge.
Define these types in `citypods/remedy_policy.py`; do not introduce another independent catalog
or config parser for onboarding. Policy-instance evidence includes template and local revision
hashes, so a template change replays affected instances and invalidates stale decisions.

New `load_policy_templates(config) -> TemplateIndex` and
`instantiate_policies(city, source_key, templates, official_evidence) -> PolicyInstances` live in
`citypods/remedy_policy.py`. Instantiation binds an approved template to verified local ownership;
it emits an evidence-backed unresolved result if required identity/completeness proof is missing.
Cross-city template approval does not authorize a new city's publication without its baseline gate.

New interfaces: `load_policies(feed_paths, *, templates=None) -> PolicyIndex`,
`resolve_owner(label, source_key, policies) -> OwnershipResolution`,
`replay_coverage(recordings, feeds, policies) -> CoverageReplay`, and
`material_evidence_hash(evidence) -> str`. Resolution returns verified owner, ambiguous or unknown,
with evidence refs; conflicting names/numbers never choose a target by token overlap.

Reuse the current TIF guard through this module. `_target_feed_is_compatible` and
`_aggregate_policy_reason` consult source-scoped policies. `gather_unexpected_body_evidence` adds
full completeness, coverage and policy context. All new URL fetches use
`validate_source_url`; URLs, quoted source text and model responses remain untrusted data. `_compact_evidence` retains relevant historical
examples and counts instead of dropping them. `remedy_batches` groups city/source/family before
packing; oversized families use linked shards with shared policy context, never lose findings.

`collect_unexpected_bodies` removes archived-label-as-resolution skipping. New
`collect_body_coverage(episodes, records, *, related_cities, policies, dispositions)` emits all
new/changed labels, including labels already matched by broad selectors. Reuse its unmatched rows
for the old finding interface. `audit_all` and `scripts/audit_feeds.py` write evidence schema v2;
`write_evidence_file` includes config/policy/catalog hashes and observed time. Read v1 artifacts
for diagnostics only: refresh before writes. Config hash mismatch or age >24h refreshes that source;
failed refresh prevents mutation and retains candidates for retry.

Source scope uses current `source_key(city)`. Record deduplication prefers existing persisted UID
and adapter recording identity. If neither establishes a shared identity, retain both observations
and mark uniqueness unknown; no generic GUID stripping or invented cross-provider UID mapping.

New immutable B2 events: `state/remedy/events/<source_key>/<decision_id>/<event_id>.json`.
The maintainer approved the source segment in chat on 2026-10-04: source-scoped reconstruction
can list the relevant event prefix directly, without a global scan or a second authoritative
source-to-decision index. No P2 events exist yet, so this correction requires no migration.
Decision ID hashes city, source key, policy ID and normalized label. Event ID hashes canonical
content including parents and external delivery ID. Event fields: schema version, IDs/parent IDs,
UTC time, actor kind/identity, state, evidence/config/policy hashes, recording refs, model/reviewer
provenance, rationale, PR/issue number+URL and disposition. No credentials or unrestricted model
text enter event keys/issue markers. Events are never deleted during ordinary maintenance.
Duplicate external delivery IDs fold once even if retry timestamps differ; use provider event
time, not processing time, for their identity. A new recording under an approved rule updates
coverage evidence without reopening the policy.

States are `covered`, `excluded`, `proposed`, `in_review`, `refinement_needed`, `blocked`, `resolved`.
A closed PR without merge yields `refinement_needed`; merged is not `covered` until replay confirms
its main config. Unknown or conflicting parent events block automation. Exclusions carry reviewed
format/label rules and negative cases in the policy, separate from episode archive retention.

New ledger interfaces: `append_event(storage, event) -> str`,
`fold_events(events) -> DecisionState`, `load_decisions(storage, *, source_keys) -> dict`,
`reconcile_github(decisions, snapshot) -> list[RemedyEvent]`. Read immutable B2 events to rebuild
truth; a derived `state/remedy/coordinator.json` CAS object on R2 tracks active claims/publication
reservations, not authoritative decisions. Add its prefix and reconstruction rationale to routing.
Losing it triggers reconstruction from events and current marked GitHub artifacts before writing.
No CAS backend means report-only, never unguarded mutation.

Serialize mutations with existing `maintenance-leases/remedy.json` via
`citypods.ops.maintenance_leases.acquire`; use renewable expiry and refuse writes after lease loss.
Global workflow concurrency is a second guard. Append intent before GitHub writes, then completion;
recovery queries stable artifact markers after a crash. Re-read events/config/lease before publishing.
CAS conflicts retry from refreshed state; do not overwrite another run's event or disposition.
No daily maintenance scan of all media; initial event reconstruction is source-scoped on B2 and
cached thereafter. Keep all bookkeeping outside expensive-work deadline gating.

Material change is new official identity evidence, changed approved policy, or a new identity/
format contradiction. Counts, a new date under the same rule, prompt/model changes or another week
alone do not regenerate a rejected proposal. Explicit maintainer reopening is recorded as an event.
Route changes may trigger an eval comparison, not unsolicited reopening of a rejected policy.

P2 shared-default acceptance: the same frozen evidence yields identical ownership, coverage and
exclusion results through onboarding and maintenance entrypoints. Known normalization/rule matches
resolve without an LLM call; unknowns preserve evidence and escalate. Test template instantiation
for a previously unseen city, neighboring wrong-body/topic labels, missing local proof, local
feedback isolation, unknown/stale template revisions and complete regression replay after changes.
Cross-city positive/negative cases live under `evals/remedy/` with ordinary gold provenance and
holdout isolation; city-specific facts are never promoted by agreement alone.

P2 tests: archived-but-unserved labels visible; newly matched false inclusion surfaced; duplicate
views; no UID invention; shared word not identity; source-scoped negatives; v1 refresh and stale
config guards; immutable events; conflicting parents; lost cache recovery; no-CAS report-only;
claim loss/concurrent publication; rejection suppression and explicit reopening; exclusions retain
records. Exit: complete coverage replay and rejection/exclusion persistence work offline without LLMs.

### P2a typed policy contract — frozen, implemented in PR #2213

**Maturity: L3.** The maintainer approved the typed official-evidence packet and exact city/source
binding in chat on 2026-10-09. Instantiation validates declared case references without implicitly
loading evaluation answers. A scoped issue is linked below before implementation. P1 schemas shipped in #1987 satisfy the predecessor. No model admission is required.
This is the first independently reviewable P2 slice. Source-partitioned feedback paths are already
approved, but no ledger or evidence-v2 implementation belongs to this slice.

#### Scope / files

New `citypods/remedy_policy.py`, existing `citypods/config.py`, existing
`citypods/remedy_evaluation.py` only strict shared-config/template compatibility,
`citypods/audit_remedy.py` only existing policy guard call sites, new
`tests/test_remedy_policy.py`, existing `tests/test_config.py`, `tests/test_remedy_evaluation.py`
and remedy guard tests, `evals/remedy/` sanitized policy-transfer cases, lifecycle docs.
Shared `config/remedy.yml` gains `policy_templates: []` only. No active feed edits/template
activations, provider/Worker/catalog changes, new deps, stage versions or archived record writes.
Do not modify audit.py, audit evidence file schema, queues, ledger, storage or onboarding workflow
in P2a. Later phases wire their existing entrypoints to this same API; no duplicate resolver.

#### Strict values and compatibility

Public parse failures are ValueError with feed/template context. Frozen dataclasses or strict
Pydantic models; reject extra keys and coerced booleans/strings. Slug IDs use
[a-z0-9][a-z0-9-]{0,63}; version is strict positive int. Names remain opaque exact official strings,
nonempty after body_key; identity_names forbid * and ?. Canonical hashes use existing
remedy_evaluation.canonical_hash, or identical local canonical JSON without import-cycle changes.
Sort unordered IDs/forms/references for hashes; retain official names unchanged. No network parsing.
HTTPS approval refs are GitHub issue/PR/comment URLs. source_key uses existing records.source_key,
including valid pinned source IDs; do not introduce a 12hex requirement for remedy policy scopes.

Feed remedy_policy preserves current optional aggregate_family/member_names/identity_names.
New optional fields exactly policy_id/version/positive_case_ids/negative_case_ids/approval_ref/
template_id/template_version. Unknown keys rejected. policy_id/version/approval_ref form an
all-or-none reviewed provenance tuple; template_id/template_version form an all-or-none pair and
require that reviewed tuple. Positive/negative case lists are distinct nonempty strings when set.
Identity-only requires nonempty identity_names; aggregate must be one of existing seven families.
Do not silently treat member_names as exact identity names.

Legacy policy declarations continue unchanged, indexed status `legacy_unversioned`, internal ID
`legacy:<feed-slug>` and hash of their actual declaration. Existing exact-owner/TIF protections
continue to work; they do not gain verified evaluation truth, template approval or auto-merge
qualification. No production YAML migration is bundled here.

#### Exact template schema v1

Optional RemedyConfig.policy_templates defaults to []. Define PolicyTemplate in remedy_policy.py;
RemedyConfig references that type rather than creating another config reader. Template fields:

```
id: <slug>
version: <strict positive integer>
approval_ref: <HTTPS GitHub reviewed template decision>
scope: cross_city | city_source
city_source: null | {city: <city-slug>, source_key: <existing-source-key>}
identity_type: aggregate_family | named_body
aggregate_family: tif | pid | bond | charter | redistricting | public_input | public_briefings | null
permitted_transformations: [<closed values below>]
selector_forms: [body | body_any | body_exact | body_includes]
official_proof_requirements: [<closed proof values below>]
exclusion_boundaries: [<closed values below>]
migration_boundaries: [<closed values below>]
positive_case_ids: [<case IDs>]
negative_case_ids: [<case IDs>]
transfer_case_ids: [<case IDs>]
```
All keys required. Aggregate identity requires non-null family; named_body requires null family.
Lists cannot be empty except transfer_case_ids for city_source templates. Duplicate entries error.
No literal city names belong to templates. city_source requires a non-null exact city/source binding; cross_city requires null.
Instantiation checks that binding against its supplied City and existing source_key. It cannot
transfer across sources merely because names match.
Cross-city templates must include transfer cases; parser validates IDs/type, approval is not inferred.

Closed initial transformations:
- normalized_exact_label: existing matches_exact_body_label/body_key normalization only.
- reviewed_label_alias: explicitly approved additional local exact label.
- reviewed_recording_inclusion: existing exact body_includes GUID inclusion with official proof.
- approved_family_marker: existing TIF guarded recognition only; other families fail this form.

No regex DSL, year stripping, fuzzy synonyms or inferred new owner. Runtime rule changes require
new reviewed template revision/negative/transfer cases. Existing body/body_any substring matches
remain selectors; they do not become verified ownership for unknown labels.

Proof enums: official_body_identity, recording_is_public_meeting, same_source_namespace,
complete_available_history, reviewed_positive_negative_cases, exact_provider_guid.
Every template requires first five; GUID inclusion additionally requires exact_provider_guid.
Exclusion boundary enums: promotional, ceremony, staff_training, municipal_tv_show, topic_only,
other_body. All initial templates require six; they require negatives, never execute topic-keyword
exclusions. Migration boundary enums: source_namespace, uid, official_metadata, archived_records,
audio_artifacts; all five required, forbidding these mutations through instantiation.

#### Named immutable API/results

- load_policy_templates(config) -> TemplateIndex. Config is parsed RemedyConfig or equivalent
  strictly validated mapping, not an independent parser. Index by (id,version); duplicate keys
  fail even if byte-identical. Carries canonical template hashes.
- load_policies(feed_paths, *, templates=None) -> PolicyIndex. feed_paths is existing
  feed_paths_by_slug mapping[str,Path]. Load feed models through the existing config loader at the
  shared config root; no hand-derived alternate namespace. Reject paths from mixed config roots.
  Build only requested slugs and verify path/model slug correspondence. Empty mapping valid.
  If templates absent, parse legacy/versioned instance declarations; a referenced template is
  unresolved rather than guessed. No instantiation without official packet.
- instantiate_policies(city, source_key, templates, official_evidence) -> PolicyInstances.
  source_key must equal records.source_key(city), else unresolved source mismatch. Reads the local
  remedy_policy template reference and supplied proof packet only, never scans network/othercities.
  Emits resolved instance plus diagnostics or unresolved diagnostics; never feed YAML or selectors.
- resolve_owner(label,source_key,policies) -> OwnershipResolution. Result fields status
  verified|ambiguous|unknown, owner_slugs tuple, policy_ids tuple, evidence_refs tuple,
  holding_owner_slugs tuple, reasons tuple. Verified can contain multiple exact reviewed owners,
  preserving current intentionally joint subscriptions; it is not silent arbitrary single-owner
  selection. Ambiguous means contradictory identities/instantiation, not merely legitimate exact
  shared subscription already present in configuration.

PolicyInstance fields: policy_id, version (nullable legacy), status legacy_unversioned|approved|
unresolved, owner_slug, city_slug, source_key, aggregate_family, member_names, identity_names,
approval_ref, case IDs, template id/version/hash, local_declaration_hash, evidence_refs,
proof_diagnostics. PolicyIndex stores source->instances; references immutable.
TemplateIndex owns parsed versions/hashes; PolicyInstances owns resolved/unresolved tuples.
OwnershipResolution returns deterministic sorted refs/owner tuples; no secret/provider payloads.

Pure lookup rules match current guards:
1. Exact reviewed identity_names match supersedes weaker other-family holding clues; retain every
   exact reviewed owner for intended joint subscriptions.
2. Existing TIF markers qualify only if current forbidden-topic/other-body token guard passes.
   Reuse current code/rules verbatim, no newly inferred marker semantics.
3. Other-family markers/member_names are holding clues only. Unknown/ambiguous cannot add feeds
   or assign ownership. A body scoped to another source never matches.
4. A nonpolicy feed still uses existing coarse selector compatibility in the old remedy guard;
   do not convert it to proof or widen this slice into all taxonomy inference.
5. `_aggregate_policy_reason` still considers proposal label/new slug/title for protection against
   separate aggregate-member feeds. Use shared resolver/holding results rather than copied regexes.
   `_target_feed_is_compatible` with a policy requires verified membership; unchanged no-policy
   fallback remains. Reasons retain existing useful diagnostics.

#### Approved typed official_evidence contract

The existing contract requires instantiate_policies to consume official_evidence but provides no
serialized proof shape or way to bind names to actual references. This typed packet plus the explicit city_source template binding are the genuinely new
serialized schema decisions in this policy-only slice. The binding is necessary to enforce the
already intended city_source approval restriction; an enum alone cannot identify its approved city. Recommend this smallest frozen packet, with existing
EvidenceRef/Recording schemas unchanged, and explicit binding objects rather than booleans claiming
that proof exists:

```yaml
schema_version: 1
city: example-tx
source_key: example-source
approval_ref: https://github.com/BashfulBits/city-meeting-podcasts/issues/NN#issuecomment-NN
policy_id: example-tx-tif
policy_version: 1
template_id: city-tif
template_version: 1
identity_bindings:
  - identity_name: Downtown TIF Board Meeting
    evidence_ref_ids: [official-board-page, official-agenda]
    recording_guids: [https://example.granicus.com/MediaPlayer.php?view_id=2&clip_id=7]
evidence_refs: [<existing EvidenceRef v2 values with bounded official spans/hash>]
recordings: [<existing Recording v2 values with verbatim body/title/date/GUID>]
completeness:
  status: complete_available
  evidence_ref_ids: [archive-census]
  exception_refs: []
positive_case_ids: [example-tif-meeting]
negative_case_ids: [example-council-tif-topic]
transfer_case_ids: [unseen-city-tif]
```

Require source/city/policy/template identity and version to match declaring feed; packet approval
must match local approval_ref; exact identity names and GUIDs must occur in packet recordings/bindings.
Missing reference/recording/binding produces unresolved. Do not treat official text/model summary as
executable rules. Completeness status enum complete_available|incomplete|unknown; evidence IDs always
resolve. Incomplete/unknown stays unresolved in this slice; detailed exception acceptance belongs to
P5. Full evidence-v2 schema later can embed this same typed packet without changing P1 seed schemas.

Independent truth/case gate: case IDs checked against frozen manifest/gold supplied in the packet's
approval evidence, not manufactured by parser. Since the existing instantiate API has no evaluation
parameter, **recommend do not load gold implicitly**. Validate packet exact case IDs equal the local/
template lists and retain approval provenance. A later replay/qualification verifies actual case
truth. This avoids adding an undocumented eval IO side effect to pure template instantiation.

Alternative: arbitrary dict evidence (today's loose case completeness/family_policy). Simpler but
cannot distinguish an ungrounded `{verified:true}` assertion from auditable local ownership proof.
Typed packet is therefore recommended before implementation. No maintainer vote needed again on
already approved families, source partition, exact names or unchanged guard semantics.

#### Tests / acceptance

Parser: all current configurations valid; malformed/extra/coerced metadata errors; paired fields;
legacy behavior preserved; template unknownrevision unresolved; canonical ordering; no sourceUID
change. Empty policy_templates accepted and initial production remains empty/shadow.
Resolver: existing TIF guard tests and all existing exact joint-owner tests unchanged; topic negatives,
markers only holding for nonTIF, exact identity supersedes holding clue; source-local isolation;
ambiguous proofpacket cannot acquire owner; unsupported forms reject; no model/network calls.
Instantiation: previously unseen city with frozen official packet; no local names copied across
cities; missing reference, incomplete archive, unknown revision, mismatched approval/namespace
unresolved; packet does not emit config mutations; same policy index yields same lookup outcomes
through simulated maintenance and onboarding callers.
Hash compatibility: templates/local revisions contribute hashes only for new policy provenance;
existing no-config feed/cache hashes untouched. Official record values/UIDs/audio keys unchanged.
Sanitized evals: explicit regression/transfer cases with ordinary provenance; do not label these
independent admission truth or qualify auto-merge. No live calls/credentials.
Whole repository Ruff/format, offline pytest, exact-head CI/preview. One clean code PR then
CodeRabbit paced >=65 minutes from last requested review, findings verified/fixed and carried forward.
Docs update review11/CHANGELOG/ARCHITECTURE; stamp only shipped slice, keep P2b/P2c and P3–P5 gated.

#### Deferred exactly

No replay_coverage/material_evidence_hash implementation in P2a. They remain the next complete
coverage slice; no archived-label skip change, fresh evidence requirement enforcement, persistent
feedback events, queue publication or automatic city onboarding activation here. Ledger uses
approved source-partitioned paths later; no remaining question about that path. P3 still requires
actual independently reviewed model admissions. No claim full historical backlog is complete.


### P3 — two-stage consensus and bounded adjudication

**Decision (2026-10-14):** replace the prior blind independent reviewer followed by optional JEV
shadow flow with the following bounded policy. This is a design decision only; it does not admit
routes or change production behavior.

1. **Initial score:** Sol and GLM Flash receive the same evidence, approved policy, and original
   assignment prompt independently. Preserve their raw responses and route/model/effort provenance.
2. **Agreement:** normalize publication action, owner/body identity, and the exact unordered feed
   set. If these fields agree and deterministic policy/evidence validation passes, create a
   proposal and PR candidate. The PR still requires human review and merge.
3. **Disagreement:** make exactly one adjudication round with DeepSeek V4.1 Flash and JEV. Both
   receive the same expanded, hashed packet: original source evidence, both initial proposals
   labeled A/B, and additional title/context evidence selected by a deterministic retrieval
   procedure. Do not include the gold answer or hidden expected labels.
4. **Adjudication agreement:** DeepSeek's normalized assignment must exactly match the determinate
   A/B proposal selected by JEV, and both must pass deterministic validation. Then create a
   proposal and PR candidate, still requiring human review and merge.
5. **Manual disposition:** if adjudicators disagree, either abstains, selects “neither proposal” or
   “insufficient evidence,” fails transport/schema/identity checks, or deterministic validation
   fails, create a GitHub issue with the evidence and explicit include/exclude checkboxes for each
   candidate feed, plus overall `no feed assignment` and `hold for future evidence` choices. A
   human decides what goes in and out; there is no per-feed “possible” disposition. A human may
   defer the whole finding while waiting for additional meetings or other material evidence. Do
   not retry the semantic escalation.

JEV's bounded choice is A, B, neither complete proposal, or insufficient evidence; it also returns
independent support probabilities for A/B. “Neither” and “insufficient evidence” are safe holds,
not assignments. A JEV choice only agrees when it selects A or B and DeepSeek independently returns
the same normalized action, owner, and full feed set. The diagnostic's JEV multiple-choice result
is not full-assignment accuracy.

**Maintainer admission decision (2026-10-14):** the maintainer explicitly accepted the existing P1
evaluation evidence as sufficient to proceed with P3 implementation. This acceptance does not
rewrite observed provider identity or reasoning metadata: GLM's alias/serving-family question,
provider-default evaluation coverage, and JEV's pilot-versus-dispatch distinction remain visible in
the evaluation records. It does not change `config/remedy.yml`, enable runtime routing, publish
feeds, or merge PRs.

**Maintainer acceptance update (2026-10-15):** the evidence and methods already preserved under
`evals/remedy/` satisfy the P1 evaluation acceptance criterion. Do not block P3 on another model
comparison or completion of review/49's separate weekly judge league. P3 reuses the merged review/49
Worker and JEV adapter. This changes the implementation gate only; runtime verification of exact
physical route, serving identity, reasoning setting/hash, and current admission remains required.
The production remedy configuration remains shadowed until those checks pass.

**P3 evaluated model parameters (maintainer direction, 2026-10-15):** use the parameters recorded
by the approved evaluation artifacts as the comparison baseline; do not invent a higher reasoning
setting or require another broad model comparison. The three chat-completion routes were evaluated
with `reasoning: provider-default`, a 900-second request timeout, a 950-second per-case deadline,
and a 16,384-token output cap. No temperature override was recorded, so it remained at the
provider default. The evaluated physical/upstream identities are:

* Initial Sol: `beatapi_gpt_6_1_sol_free` → `gpt-6.1-sol-free`;
  provider-default reasoning.
* Initial GLM Flash: `orcarouter_zai_glm_5_3_flash_free` →
  `z-ai/glm-5.3-flash-free`; provider-default reasoning.
* DeepSeek adjudicator: `beatapi_deepseek_v4_1_flash_free` →
  `deepseek-v4.1-flash-free`; provider-default reasoning.

The 60-recording prompt comparison contains 180 completed observations per route, covering the
three prompt recipes; the eight-case expanded-context diagnostic adds eight completed DeepSeek
observations. See `evals/remedy/results/prompt-comparison-2026-10-14/` and
`evals/remedy/results/disagreement-2026-10-14/`. These values describe the tested settings, not a
claim that provider-default is a named high/max effort mode. JEV's eight successful observations
came from the approved existing local pilot after Worker payload uploads failed. They establish
the diagnostic response only: those records do not persist equivalent reasoning, timeout, or output
cap controls and do not qualify JEV's production Worker route parameters.

The admission contract now supports `provider-default`: the request applies no named reasoning
override, and the fingerprint covers the route's compiled request parameters. Preserve the current
shadow/empty-admission behavior until exact route settings are admitted and JEV's Worker-served
identity and effective parameters are recorded. Do not silently substitute high/max or infer JEV
controls from its local pilot.

**Maintainer route-selection record (2026-10-15):** use these exact physical routes for P3; do
not substitute another route through pool overflow or fallback. This records the approved target
mapping and the evaluated chat configuration. It does not mark a route production-admitted or
change `config/remedy.yml`.

| P3 role | Physical route ID | Catalog upstream model | Evidence and admission status |
|---|---|---|---|
| Initial: Sol | `beatapi_gpt_6_1_sol_free` | `gpt-6.1-sol-free` | 180 scored evaluations; returned upstream matched the request. Serving-family hold remains in every completed observation. |
| Initial: GLM Flash | `orcarouter_zai_glm_5_3_flash_free` | `z-ai/glm-5.3-flash-free` | 180 scored evaluations; identity hold remains in every completed observation. |
| Adjudicator: DeepSeek V4.1 Flash | `beatapi_deepseek_v4_1_flash_free` | `deepseek-v4.1-flash-free` | 180 scored evaluations; returned upstream matched the request. Serving-family hold remains in every completed observation. |
| Adjudicator: JEV 1.13 | `beatapi_jev_1_13_free` | `jev-1.13-free` | Eight local-pilot answers; no Worker receipt or returned route/upstream identity was captured. Not admitted. |

For the three chat evaluations, the recorded request setting was `provider-default`, with no
temperature override, a 900-second provider timeout, a 950-second case deadline, and a 16,384
output-token cap. The compiled route-parameter map was empty (`{}`); its canonical admission
`request_params_hash` is
`44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a`. This hash covers the
compiled route override map only; it does not bind timeouts or output-token limits. The individual
evaluation `route_settings_hash` values and raw observations remain in the linked immutable result
directories above.

The current P3 chat caller is not yet using the evaluated request budgets: `_chat_job` sets
`max_tokens=8192` and `timeout_class=long`; the configured Worker `MAX_RESPONSE_SECONDS` is 720.
JEV's P3 `JevBackend` reserves 1,024 output tokens and also uses the long timeout class. The local
JEV pilot bypassed the Worker and did not persist equivalent effective request parameters. Before
admission, either align runtime chat settings with the evaluated profile and collect a JEV Worker
observation, or explicitly qualify the runtime profile that will actually be used. Preserve shadow
mode and empty admissions until the exact serving identities and effective Worker settings are
verified against that decision. The current route catalog lists JEV's 4,096-token output context
limit; that ceiling is not evidence of the effective per-request reservation.

**P3 dispatch cadence and adapter decision (maintainer, 2026-10-15):** the P3 runner will use the
Worker for all four model calls. Give P3 its own daily scheduled invocation; the existing feed-health
audit is already daily, while review/49's weekly judge audit is a separate workflow and keeps its
cadence. A P3 invocation submits the two initial votes or reconciles their stable recipe hashes; a
later daily invocation submits adjudication only after both initial results are available and
disagree, then a subsequent invocation collects the adjudication and emits the report candidate.
This permits the two model round-trips to finish over two or three days without keeping a GitHub
runner alive or duplicating a deferred Worker job. Persist P3 model observations separately from
the P2 human-decision ledger before enabling this scheduled caller. The maintainer approved this
boundary on 2026-10-15: model votes and Worker receipts are intermediate evidence, not human
dispositions. The P2 ledger remains authoritative for reviewed decisions; its final event may
reference the P3 report artifact.

Use the common adjudicator interface from review/49 and reuse its existing `JevBackend` for JEV's
`systemone` request/response shape. Keep that wire adapter model-specific; do not create a second
JEV client or fork the common orchestration and normalization. The current Worker catalog maps JEV
to one physical route, so an allowed-model constraint is sufficient for it today. DeepSeek V4.1
Flash maps to both NVIDIA and BeatAPI routes; P3's recorded route is
`beatapi_deepseek_v4_1_flash_free`. The Worker request must constrain that physical route as well as
the logical model, and defer rather than fall back if the pinned route is unavailable. This route
allowlist capability is a prerequisite to the P3 caller, not a production catalog change.

**P3 observation persistence contract (maintainer, 2026-10-15):** store each completed Worker
response as a content-addressed object under `state/remedy/p3-observations/<source>/` and a hash of
the finding identity. The strict schema binds finding/source identity, stage (`initial` or
`adjudication`), role, stable Worker `recipe_hash`, request/evidence/config/policy hashes, selected
route, logical/upstream model, applied reasoning level (null when provider-default omits an
override), collection time, structured response and response hash. Roles are restricted to Sol/GLM
Flash in the initial stage and DeepSeek/JEV in
adjudication. Append and reconstruct under the existing remedy maintenance lease; read back and
validate writes, reject corruption, and never replace an existing object. Store completed response
observations only in this log; the existing deferred registry remains authoritative for pending
Worker handles. Stable recipe hashes make enqueue/reconciliation idempotent across daily GitHub
runs. A terminal failure or missing/unknown result cannot be promoted to a vote. Append P2 decision
events only for human dispositions; those events may reference the P3 report artifact.

**P3 daily caller implementation (PR #2447, merged 2026-10-15):** `scripts/remedy_p3_daily.py` runs
from a dedicated daily GitHub Actions workflow. `citypods.remedy_p3_runner` submits the initial
Sol/GLM Flash pair, then one DeepSeek/JEV adjudication round only after a recorded disagreement;
subsequent daily invocations reconcile the existing Worker handle by its stable recipe hash and
never create a second submission for a pending job. Every request is queue-only, free-route only,
and pinned to its configured physical route. Completed results must echo both the expected logical
model and exact route ID before they enter the separate observation log. JEV uses the review/49
`JevBackend` wire adapter, with explicit A/B/C/D options and strict expected-question matching;
malformed choices or probabilities remain raw evidence and resolve to the manual issue candidate.

The merged scheduled caller writes an exclusive Actions report artifact and has no feed-file,
GitHub issue/PR, or P2 ledger write path. It remains closed while `config/remedy.yml` is shadowed or
exact route admissions/reasoning settings fail validation. P1 acceptance removes the need for
another comparison; it does not establish unobserved serving-family identity or JEV dispatch
qualification. PR #2449
implements the bounded proposal/issue publisher and P2 human-disposition command. After that PR
merges, P3 implementation is complete; operational completion still requires authorized runtime
admissions and a monitored end-to-end cycle. No provider calls were made by PR #2447 or #2449.

**P3 implementation contract:** `citypods.audit_remedy.build_p3_review_decision` accepts two
route-bound `P3Vote` records, their deterministic validator outcomes, optional one-round
`P3Adjudication`, evidence/policy hashes, and an explicit set of runtime-admitted route IDs. It
returns `P3ReviewDecision` with both stages' raw-response hashes and route/model/effort provenance,
validator result, and `pr_candidate|issue_candidate|policy_hold`. The caller constructs JEV choices
with A=Sol and B=GLM Flash; DeepSeek supplies its complete assignment. Exact normalized equality
uses action, target feed set, episode IDs, all-observed flag, and new-feed slug. A proposal
candidate requires the initial validated agreement, or a selected A/B proposal that exactly
matches DeepSeek and all validators pass. C/D, missing results, mismatches, and validation failure
cannot produce a PR candidate. Missing route admission returns a policy hold.

`format_p3_manual_issue` renders evidence hashes, caller-prepared evidence, per-feed include/exclude
checkboxes, and the two overall hold choices. “Hold for future evidence” is a finding-level defer,
not a per-feed quality rating; subsequent related meetings or material evidence can renew review
under the existing P2 `watch`/`blocked` disposition. An unchanged scan does not re-open it.
It does not write to GitHub. The caller must supply feed names/slugs from locally validated
evidence. The pure P3 decision engine and local report remain provider-free. A separate daily
caller uses the review/49 Worker dispatch stack and P3 observation log; it may open proposal PRs or
manual-disposition issues according to the agreed P3 policy, but never changes feed configuration
or merges a PR. It does not call JEV directly, fall back to another model/route, or repeat semantic
adjudication. Tests cover normalized consensus, adjudication, mismatches, validator failure,
provenance binding, unavailable routes, JEV abstentions, observation recovery and checkbox
formatting. Keep unrelated P4/P5 controls, historical coverage, and stage/pipeline versions
unchanged.

**Report-only replay contract (2026-10-14):** implement `citypods.audit_remedy_report` and
`scripts/report_p3_remedy.py` as a local replay boundary over already-recorded P3 observations.
This resolves the runner's missing input/output contract without activating provider calls or
publication. The strict input JSON has `schema_version: 1`, `finding_id`, `evidence_hash`,
`policy_hash`, `evidence_summary`, `feeds`, `initial_votes`, `initial_validators`, optional
`adjudication`, and `adjudication_validator_passed`. Each vote/adjudication uses the P3 models
above; each feed has only `slug` and `title`. Reject unknown keys, malformed hashes, duplicate
feed slugs, and a finding/hash mismatch before rendering. Evidence summary is caller-prepared,
bounded plain text; this tool never opens URLs or reads gold.

Run the offline replay:

```bash
PYTHONPATH=. python scripts/report_p3_remedy.py --input recorded-votes.json --out p3-report.json
```

The input can contain recorded outputs from a prior authorized run; the script itself makes no
model calls. The output path must not exist.

The replay caller supplies `admitted_routes` as a runtime argument derived by its trusted
integration; it is deliberately not accepted from the input file. The local CLI supplies an empty
set, so it can never promote a saved vote to a proposal. The report contains schema version,
input hash, the pure `P3ReviewDecision`, and either a proposal candidate summary, a formatted
checkbox issue candidate, or the exact policy hold. The issue body is rendered locally only.
Output creation is exclusive and atomic; raw input and prior reports are never overwritten. The
CLI has no live, apply, GitHub, storage, or route-configuration flags. It performs no network,
provider, credential, GitHub, feed, or durable-ledger operations. This is a report/replay aid, not
the production P3 dispatch integration.

Tests must cover strict input validation, empty-admission holds, exact consensus and adjudication
replay, manual checkbox output, no input/output overwrite, and proof that CLI execution makes no
network or GitHub calls. The report must retain the original route/model/reasoning provenance and
raw response hashes from each vote.

`citypods.remedy_p3_publication.validated_p3_plan` is the report-to-feed mutation boundary. It
accepts only a `pr_candidate` with passing deterministic validators, binds it to the exact current
source evidence hash, recomputes the consensus from route-bound votes using trusted runtime
admissions, resolves episode IDs locally, and re-runs `validate_proposals` against the current feed
catalog. A publisher must apply only this returned plan, run `verify_remedy_mutations`,
and create a human-review PR; it must never push to main or merge. Manual issues must preserve
per-feed options for every locally configured same-source feed, even when no model suggested it,
and provide a structured `/body-disposition` action. The command accepts LF or CRLF between the
command and JSON. The issue must be opened by `github-actions[bot]`; the command must authenticate
a repository writer, read checked boxes only from the generated issue section, bind the action to
the exact issue/evidence and current ledger parents, and use `record_decision_action` so the human
outcome is appended under the remedy lease. Evidence freshness is measured from collection time,
and stale submissions tell maintainers to wait for refreshed evidence. Model votes remain in the
P3 observation log and never become P2 decisions.

PR #2449 (merged 2026-10-15) implements bounded GitHub publication and the P2 issue-command
handoff. A separate,
least-privileged publisher job receives only the current run's short-retention report/evidence
artifact and can create proposal PRs only from the revalidated mutation plan and
human-disposition issues with stable finding/evidence keys; issue refresh preserves maintainer notes
and checked choices only while the evidence binding is unchanged. `/body-disposition` reuses the
existing issue-comment permission check and command recorder, validates every candidate feed
checkbox, and appends typed include/exclude choices under the remedy lease. Legacy events omit the
additive `feed_dispositions` field and retain their prior event IDs. Proposal PRs remain
human-reviewed; terminal human dispositions close the issue, while a future-evidence hold remains
open and its checked hold is preserved across ledger-parent refreshes. No publisher merges or writes
feed files directly on main. Runtime activation
remains separately gated on exact route admissions, serving identity, and the evaluated request
parameters documented above. A monitored authorized production cycle remains the final operational
completion check.

**Evaluation record:** `evals/remedy/results/prompt-comparison-2026-10-14/` preserves the 540-job
three-prompt comparison and scoring tools. On the original prompt, Sol + GLM Flash agreed
correctly on 52/60 cases, had zero shared wrong agreements, and disagreed on 8. The disagreement
follow-up in `evals/remedy/results/disagreement-2026-10-14/` records the broader-context DeepSeek
and JEV diagnostic for those eight cases. These reused challenge cases do not independently
qualify routes or JEV. The maintainer's later feed-by-feed labels are recorded separately from
frozen evaluation gold in `human-dispositions-2026-10-15.json`.

### P4 — bounded PRs, issue refinement and recovery

**Report-only stage (2026-10-15).** `citypods/remedy_queue.py` is the pure planner the gate below
asks to run first. `plan_publications(candidates, ledger, snapshot, limits=..., now=...)` returns what
a run would open, update, reopen and leave queued, and `scripts/remedy_p3_publish.py` writes it to
the publication report as `publication_plan` with `mode: report_only`. **It performs no write and the
publisher keeps its current behaviour**, so the plan is for inspection against what the publisher does.
It enforces the limits in `config/remedy.yml`: PRs of at most `decisions_per_pr` decisions for one city
and feed with one approval outcome, a new body never beside an automatic alias; `prs_per_rolling_week`
counting every marked PR opened or reopened by anyone in the preceding seven days and `open_prs`
counting those open now (closing frees an open slot, never the weekly count); `active_directional_issues`;
oldest-first per city, round-robin across cities; whatever does not fit stays queued with reason and age.
It never republishes: a marked open artifact is updated (no slot), a closed or merged one is left
alone unless `/remedy reopen` asked (a reopening is an admission), and a decision whose current
evidence was rejected or already settled does not regenerate. Markers are
`<!-- citypods:remedy:v1:<decision_id> -->`, one per participating decision; P3's first issues'
`citypods:remedy-p3:key=` still names its decision. Branches are
`fix/remedy-<city>-<family>-<group_hash>` from the decision ids. An incomplete GitHub listing reserves
nothing. **Not in this stage:** the isolated-worktree apply, manifest-limited staging, publication of the
plan, `remedy-feedback.yml` and `/remedy reopen` (the live stage).

**Live stage (2026-10-16).** `config/remedy.yml` `publication_mode` (`report_only` by default,
excluded from the config fingerprint so flipping it re-asks nothing) switches the publisher to execute
its plan. In `live` mode (`scripts/remedy_p3_publish.py`, `citypods/remedy_publication.py`):
- **One isolated worktree per PR**, created from `origin/main` before the unit's change is applied.
  Only the files the mutation reports (the manifest, all `config/feeds/*.yml`) are staged and the
  staged set must equal it; there is no `git clean`, and a failure removes only that unit's worktree
  and branch. `verify_remedy_mutations` runs on that exact tree and the manifest hash is recomputed
  after it, before the commit that is pushed. One failing unit does not stop the others.
- **Identity.** The body carries `<!-- citypods:remedy:v1:<decision_id> -->` markers and a
  per-decision binding (city, source, policy, label, evidence, config and policy hashes, recordings),
  the evidence hash, the models and reasoning each vote used, the changed files with their hash, and
  the limits of the check. A repeat dispatch updates or leaves a marked open artifact; a closed one is
  not republished unless reopened.
- **Feedback** (`remedy-feedback.yml`, `citypods/remedy_feedback.py`): `pull_request_target` and
  `issues` (`closed`, `reopened`) check out `main` only, hold no model keys, and accept an event only
  from a human with write access on an artifact the repository's own Actions identity opened, whose
  bindings recompute to the decision ids and whose branch is the one the decisions derive. A merge
  records `covered`/`assigned` with the PR as approval; closing unmerged records
  `refinement_needed`/`rejected` (the PR is the artifact a refinement issue answers); reopening records
  `in_review`; closing a directional issue nobody settled records `blocked`/`watch`. A daily
  reconciliation recovers missed events with the same delivery ids, so the two never double-record.
- **Refinement.** A rejected decision gets exactly one marked refinement issue that links the closed
  PR and preserves the decision requested; if issue capacity is full the ledger already holds
  `refinement_needed` with the PR link until it can open. The same evidence never regenerates a PR.
- **`/remedy reopen <decision_id>`** (maintainer write access, on a marked PR or issue): records a P2
  `proposed` event carrying the decision's own hashes; the next pass reopens or updates the artifact
  and a reopening counts against the weekly budget. It cannot grant an alias auto-merge or change
  policy.
- All mutation jobs share the concurrency group `remedy-taxonomy-writes` with `cancel-in-progress:
  false` and `queue: max` (a group otherwise keeps one pending run and replaces it with the next, which
  would drop a waiting merge or reopen). The group is on the jobs, not the workflow, so an unrelated PR
  event never enters it. The remedy ledger lease still serializes the writes themselves.
- **Failure isolation and fail-closed.** Any failure of one unit, reopening or issue is recorded under
  `failures` and the pass continues. An unreadable evidence collection time in `live` mode publishes
  nothing rather than falling back to the legacy single-PR path. A branch left on the remote by a run
  that crashed between its push and creating the PR is replaced (`--force-with-lease` against the commit
  just observed), never a permanent block. An open PR keeps its branch; the report lists it under
  `unchanged` and flags `evidence_changed` so a person can close it to regenerate.
- Reconciliation reads GitHub's GraphQL, where Actions is `github-actions` with `__typename: Bot`; the
  login is normalized to the REST `github-actions[bot]` form before an artifact is recognized.

**Gate:** P2/P3 are implemented; all external writes first run in report-only mode for inspection.
**Permitted files:** new `citypods/remedy_queue.py`, `scripts/remedy_unexpected_bodies.py`,
`scripts/remedy_commands.py`, new `scripts/remedy_feedback.py`, `scripts/audit_feeds.py`,
`.github/workflows/{remedy-unexpected-bodies.yml,remedy-commands.yml,audit.yml}`,
new `.github/workflows/remedy-feedback.yml`, `config/remedy.yml`, existing command/remedy tests,
new `tests/test_remedy_queue.py`, new `tests/test_remedy_feedback.py` and lifecycle docs.

New `plan_publications(decisions, ledger, github_snapshot, *, limits, now) -> PublicationPlan`.
A unit is a policy/identity decision, not one raw label or file. Pack at most five units with the
same city/family and compatible approval outcome. A unit can touch selectors, exclusions, migrations
and evals together. A new body/family never shares an automatic-alias PR. Schedule oldest eligible
per city round-robin; skipped/held work stays visible in the ledger.

Enforce maximum eight newly opened PRs in the preceding rolling seven UTC days and eight open
remedy PRs, plus twelve active directional issues. Reopening a PR counts as an admission against
the publication budget. Updating an existing artifact consumes no new slot. Count marked artifacts
across bot/human actors, reconcile GitHub before reservation and reserve slots under the CAS lease.
PR closure/merge frees an open slot but does not reset the seven-day count. Queue excess work with
reason and age; never emit an oversized “overflow PR” or discard findings to meet the limit.

Stable body marker: `<!-- citypods:remedy:v1:<decision_id> -->` plus the participating decision IDs.
Derive branch `fix/remedy-<city>-<family>-<group_hash>` from the unit IDs, not the whole evidence
artifact. Validate components locally. Reuse a marked open PR/issue for the same decisions. A
rejected PR's issue preserves the evidence and exact decision requested; if issue capacity is full,
record `refinement_needed` and the rejected PR link until the issue can be opened.

Refactor `_open_pull_request` to accept a publication unit and a locally verified change manifest.
Checkout an isolated branch/worktree **before** applying its unit; no aggregate working-tree edits
followed by a branch switch. Limit staging and rollback to files in that manifest. Remove broad
`git clean` of `config/feeds`; a failure cannot delete another unit's files. Re-run
`verify_remedy_mutations` on the exact PR tree and refresh hashes before push. Whole-repo Ruff and
full offline tests remain required, with feed snapshots only when feed output intentionally changes.

`remedy-feedback.yml` uses `pull_request_target` types `[closed, reopened]` and `issues` types
`[closed, reopened]` only for feedback: checkout main, never PR head, run no fork-controlled code,
provide no LLM/provider keys, give read contents/PR plus issue write only. Validate exact repo,
recognized ledger artifact, actor permissions for policy-changing commands and delivery ID.
Periodic reconciliation in the trusted remedy run recovers missed events. `remedy_feedback.py`
converts verified events into P2 events and uses stable-marker lookup before creating refinement
issues. All mutation workflows use global concurrency `remedy-taxonomy-writes`, cancel false,
and the P2 lease. Permissions/authentication patterns remain pinned and main-only.

`/remedy` remains supported. New `/remedy reopen <decision_id>` is maintainer-write-only, validates
an existing marked decision and records explicit reopening; it cannot grant alias auto-merge or
change policy. Existing trigger issue markers remain valid. Use body files for GitHub prose and
escape provider evidence; never execute text from bodies/labels/LLM responses as shell code.

Each PR includes evidence links, official identity, approved policy, alternatives/uncertainty,
unique recording impact and source limitations, negative replay, actual models/effort, independent
review, dated evaluation hashes, UID/URL/retention impact and unit IDs. Put unresolved findings in
separate issues, not hidden inside accepted PR descriptions. Operational audit summaries retain
queued/held totals and links; they are not the directional issue queue.

P4 tests: five-unit boundary; independent outcomes separated; round-robin fairness; rolling budget
and open caps; capacity races/crash after PR creation; dedupe; merged/rejected/reopened transitions;
missed webhook reconciliation; malicious fork payload cannot execute; no credential leakage;
rejection cannot regenerate; apply/rollback touches only the manifest. Exit: repeat dispatch is
idempotent and rejected changes become refinement work rather than next week's identical PR.

### P5 — narrow alias qualification and onboarding acceptance

**Gate:** P0 city baseline approved, P1–P4 working in manual mode, and maintainer-approved alias
qualification entries exist. No automatic activation merely because a phase PR merges.
**Permitted files:** new `citypods/remedy_qualification.py`, `config/remedy.yml`,
`scripts/remedy_unexpected_bodies.py`, `.github/workflows/remedy-unexpected-bodies.yml`,
`.github/ADD_CITY.md`, `evals/remedy/`, new `tests/test_remedy_qualification.py` and lifecycle docs.

Each qualified-type entry contains `type_id`, approved city/source/policy scopes, allowed
transformations, positive/negative/holdout case IDs, independent truth refs, replay artifact hash,
route/effort/prompt/schema/catalog hashes, pilot PRs and dispositions, approval PR/ref and
`enabled`. Initial `enabled` is false. Case coverage must include every allowed transformation and
its dangerous neighboring interpretation; no fixed case count or agreement percentage substitutes
for that review. Do not treat a human approving one alias as enabling its whole type.

New `qualify_alias(change, qualification, evidence, review) -> QualificationResult` requires all
entry hashes/scopes to match, deterministic replay, no unknown newly affected recording, independent
family agreement and clean exact-head CI. New body/family/source, official metadata, exclusion,
retention/UID changes or expansion outside approved ownership fail automatic qualification.
Cases outside the enabled type become ordinary manual PRs; a new transformation needs an amended
qualification PR. No widening a reviewed regex based only on model confidence.

Automation uses `gh pr merge --merge --match-head-commit <verified-head>` only after revalidation,
using existing repository authentication and preserving the merge-commit convention. Do not arm
GitHub auto-merge to run later without the required evidence/base recheck. Do not merge a stale
head or bypass protection. Failure to obtain exact-head checks keeps the PR manual. Before merge,
recheck config/evidence/qualification and that the PR is still open; changed base or policy requires
replay and fresh review. Independent validation is cached only by all evidence/config/model/prompt
hashes. Critical error appends a suspension event which overrides `enabled`; report immediately,
retain evidence and create a reviewed rollback PR. A recovered R2 cache cannot remove a suspension.

P5 wires city onboarding to P2's shared template loader/instantiator, ownership resolver and
coverage replay before the same P1 evaluation lane. It must not maintain a second prompt-only
taxonomy or copy city aliases into global defaults. Test identical maintenance/onboarding outcomes
and transfer to unseen cities using frozen official evidence, including wrong-body/topic negatives.

Qualification identifies the stable template/version, city/source owner and permitted
transformation scope. Each change still validates its base/prospective instance hashes and replay.
A new reviewed alias within that enabled scope does not itself require requalifying the entire
template; changes to ownership rules, template version or permitted transformations do. Applying
a subscription default to a new city never transfers another city's automatic-merge authorization.

`onboarding_exceptions` entries: city/source, available date range and views, missing periods/caps,
retrieval attempts with refs, approved issue/PR and maintainer, approval date, visible limitation
text and recheck condition. No blanket “old data unavailable” exception. `.github/ADD_CITY.md`
requires frontier-assisted full available-history census, approved families and rerun of the same
P1 lane, all available legitimate recordings assigned, exclusion negatives, identity preservation,
and explicit exception for incomplete history. The onboarding PR's feed description includes the
approved coverage limitation; exceptions never alter official episode titles or suppress fresh
unknown recordings. New archive availability invalidates completeness and queues targeted review.

P5 tests: per-type enable approval; all hash/scope/negative gates; zero automatic new bodies or
ambiguous ownership; suspensions survive reconstruction; stale PR head/base; protection/CI failures;
partial archive without exception rejected; reviewed exception exposes limits; new historical data
reopens coverage; already established cities do not requeue unchanged old titles.
Exit: one reviewed enabled type demonstrates safe automatic handling and visible manual fallback;
report manual decisions per established city rather than claiming statistical safety or zero work.

### Phase delivery and rollback contract

Each phase PR updates review/11, CHANGELOG and ARCHITECTURE where runtime contracts change.
Record that schemas/prompt recipes do not bump audio/stage versions; existing archived artifacts
are retained. Do not freeze this entire document when P1 ships: stamp the P1 subsection with its PR
and keep remaining phase contracts living until implemented.

Promote P2–P5 to L3 only after recording predecessor evidence and their exact scoped issue here.
If implementation would need a file/API not named by its contract, follow AGENTS.md's stop-and-ask
rule; do not silently add code paths. JEV's later adapter has its own dependency gate above.

Rollback toggles mode to `shadow` (no external publication), disables affected qualified types,
and retains all events/exclusions/suspensions for investigation. Route qualification withdrawal
prevents new calls but never deletes artifacts. Reverting a selector is a reviewed feed-config PR
with historical impact and migration/UID checks. Never use archive deletion as rollback.

## Addison public-input display and verified routing — L3 bounded implementation

Frozen bounded implementation contract: **Implemented in PR #2010**, human-merged
2026-10-04. Remaining evidence/admission gates elsewhere in this document are unchanged.

Tracking: #2009. Maintainer approved the public-input/briefing migration, combined BZA/Appeals
presentation, and retention of historical Citizen Advisory material in Public Input. This slice
implements verified assignments only; remaining dated cases stay in the historical census.

Files and exact changes:

- `config/feeds/addison-tx-town-meetings.yml`: retain slug, URL/source and Citizen Advisory
  GUID56029. Display `Addison: Public Input` and explain historical community-input recordings.
  Remove GUID304981 (verified educational seminar) and five explicit non-meeting events:
  56020 school dedication, 56021/56022 park dedications, 56024 Earth Day, 56025 business award.
  Existing unknown56026/56027/56028 and applicant/staff presentations stay explicitly under
  investigation; their current publication is not proof of final classification.
- `config/feeds/addison-tx-public-briefings.yml`: new existing-schema feed on the same Addison
  Swagit list URL, slug `addison-tx-public-briefings`, display `Addison: Public Briefings`,
  `meeting_family: public_briefings`, exact selector `Homelessness Education Seminar`.
  No generic seminar/education substring. No claim of full-city briefing coverage.
- `config/feeds/addison-tx-board-of-zoning-adjustment.yml`: display
  `Addison: Zoning Adjustment & Appeals`; description names both formal functions.
  Preserve slug, URL/source, selectors and lifecycle.
- `tests/fixtures/addison-public-input-retained.json`: minimal retained metadata/hosted audio
  fixture for affected records, preserving original UIDs/titles/dates/video/audio URLs.
- `tests/test_publication_selection.py`: actual config regression proves Citizen Advisory
  remains selected with original UID/enclosure and metadata; seminar moves exactly once;
  five events leave both family subscriptions; normalized exact seminar positives/negatives;
  BZA source/URL and both formal functions remain; fixture records unchanged after RSS selection.
- Update `review/11`, ROADMAP, CHANGELOG and ARCHITECTURE to describe prepared, unmerged work.

Do not modify provider, audit resolver, storage, stage/runtime, publication-selection machinery,
record schema or audio timeline. No new dependencies, stage versions, invalidation or audio backfill.
Full archived state is read-only for replay; raw meeting pages remain built from stored records
under the existing writer contract. Preserve URLs for existing subscribers. New briefing feed
adds a subscription, not a record or audio object. Existing render fingerprints consume feed
metadata/selectors; offline tests and full CI/preview must verify the change before human merge.

Acceptance: meaningful targeted tests, complete retained822-record ownership comparison, whole
Ruff/format and offline suite. Explicitly report unresolved old records; no coverage-complete claim and no legacy remedy. The
classification settlement hold was lifted by the maintainer on 2026-10-04; follow the current
repository-wide review timing and request rules for code changes.
The separately proposed P2 sweep contracts remain gated; #2009 does not claim to implement them.

## Verified Addison joint subscriptions — L3 bounded slice (#2011)

Frozen bounded implementation contract: **Implemented in PR #2012**, human-merged
2026-10-04. Remaining evidence/admission gates elsewhere in this document are unchanged.

The global joint rule is approved. Recording-bound official packets are cited in the Addison
body-policy audit. Use existing `source.body_includes` in the P&Z feed for GUID295839,
310072 and276855, with their complete retained body labels. Add only295839 and310072 to
CPAC: October17,2023 convened Council/P&Z, not CPAC. Preserve all Council selectors in this
slice. Do not infer participation from agenda topics or generic joint substrings.

Files: the two existing P&Z/CPAC feed YAML files; a minimal retained-record fixture
`tests/fixtures/addison-joint-retained.json`; actual-config RSS, negative participant and
canonical-search ownership tests in `tests/test_publication_selection.py`; this specification,
the body-policy audit, review/11, ROADMAP, CHANGELOG and ARCHITECTURE status descriptions.
No runtime/provider/storage/stage/record-schema changes or dependencies. Preserve existing
feed URLs, records, UIDs, dates, audio objects and current Council canonical search owner.
Replay all822 retained records: the only selector changes are three added P&Z memberships
and two added CPAC memberships. Whole Ruff/format and offline tests plus current CI must pass.
Prepared PRs remain unmerged and all CodeRabbit requests remain held. Other historical cases
remain active; this bounded correction is not complete-city coverage.

## Historical unresolved sweep — L3 approved bounded read-only contract (#2013)

Frozen bounded implementation contract: **Implemented in PR #2014**, human-merged
2026-10-04. Remaining evidence/admission gates elsewhere in this document are unchanged.

Maintainer explicitly approved this proposal in chat on2026-10-04. Tracking issue #2013.
Existing unexpected-body
remedy collection and dispatch remain unchanged. Do not expose expanded historical coverage to
legacy automation or infer that the unresolved register grants P2a model-policy approval.

Proposed files: `review/evidence/unresolved-recording-cases.json` (committed case register),
`scripts/sweep_unresolved_recordings.py` (offline-only CLI),
`tests/test_unresolved_recordings_sweep.py` (offline regressions), and lifecycle/evidence docs.
Use existing config loading, source-key and body-matching helpers; no dependencies or network.
Read cached source episodes.json and the register; write only an explicitly requested local report.
No credentials, workflow dispatch, GitHub mutation, audio work or canonical state writes.

Register envelope: `schema_version: 1`, `cases: [...]`. Each case has string `case_id`,
`city`, `source_key`, `uid`, `provider_guid`, `question`, `missing_evidence`, `next_action`,
`last_researched` (ISO date); string-list `current_feed_assignments`; `status` is `open` or
`resolved`; `disposition` is null for open cases or a string citing evidence/human approval.
Unique case_id and source_key/uid pairs; reject malformed fields, absent IDs or resolved entries
without disposition. A registered UID missing from cache is an explicit evidence failure, never
silently dropped. The snapshot of assignments is for change detection; report actual assignments.

CLI contract: required `--state-root`, `--config-root`, `--case-register`, `--output` paths.
Output JSON envelope has schema_version1, `uncovered_recordings`, `open_cases`,
`assignment_changes`, `missing_registered_records`. Each recording row has city/source/UID/GUID,
body/title/date and actual feed slugs. Open cases additionally retain the evidence/next-action
fields and historical/new case distinction is informational, not a suppression gate. Deterministic
sorting by source_key/uid/case_id. Process each cached source once and every archived recording,
including absent-body and currently matched records. Uncovered records cannot be suppressed by
archived labels or age. Known false inclusions remain in open_cases until evidenced disposition.
Resolved cases leave open_cases but remain in the committed register with their closure evidence.
Unchanged reruns produce identical reports; notification/issue lifecycle is a separate gated slice.

Tests: historical uncovered record, matched false-inclusion case, absent body, approved exclusion
resolved with evidence, missing cached UID, changed assignment, duplicate register validation,
multi-feed genuine joint, deterministic rerun and unchanged input bytes. Before implementation,
create the scoped issue, seed every open Addison case, and record the explicit schema approval.

Implementation functions: `load_cases(path)` strictly validates the register;
`build_report(state_root, config_root, case_register)` reads cached source files and returns
the declared report; `main()` parses the four required paths and writes JSON. Date output uses
the existing stored `published` field. Fail visibly for malformed source files, unknown cached
source/config bindings, or a register city/GUID inconsistent with its stored source record.
Configured sources without local cache are outside this offline snapshot; the CLI help must
state this limitation. Registered missing UIDs still appear in missing_registered_records.
Uncovered recordings with an evidenced resolved disposition remain reported with that disposition
so approved exclusions are distinguishable without hiding archived records. Resolve does not
mean silently erase. Do not overwrite the register or source files with the output path.


## Review hold lifted — maintainer direction2026-10-04

The maintainer explicitly lifted the classification review hold and requested CodeRabbit review
for non-documentation PRs while follow-on corrections proceed in parallel. Earlier hold text
records historical context and is superseded. Skip new documentation-only review requests.
Repository-wide full/incremental requests still require65-minute spacing and pending-request
checks. Human verification and merge commits remain required. #2003 is ready on036beb68:
reviewed production configuration is unchanged after two acknowledged/resolved small fixes.
#2010 full review requested19:50:57UTC; #2012/#2014 await their spaced requests.

## Addison Council ownership correction — L3 (#2015)

Frozen bounded implementation contract: **Implemented in PR #2019**, human-merged
2026-10-04. Remaining evidence/admission gates elsewhere in this document are unchanged.

Approved body/event policy applies: exactly47 P&Z work sessions and one ceremony leave Council.
Replace body/body_any with the complete exact alias list below; keep source URL, metadata and
all four body_includes unchanged, including TIRZ until #2017.

```yaml
body_exact:
  - "2025 City Council Strategic Planning Session"
  - "2026 City Council Strategic Planning Session"
  - "Budget Meeting"
  - "Combined Meeting"
  - "Council Strategic Planning Day 1 & 2"
  - "Fiscal Year 2023-2024 Budget Workshop"
  - "Fiscal Year 2024-2025 Budget Workshop"
  - "Joint CPAC, P&Z, and City Council Meeting #1"
  - "Joint CPAC, P&Z, and City Council Meeting #2"
  - "Joint City Council and Planning & Zoning Commission Meeting"
  - "Joint P&Z and City Council Meeting #1"
  - "Regular City Council"
  - "Regular City Council Meeting"
  - "Special Budget Meeting"
  - "Special Council"
  - "Special Emergency Meeting"
  - "Special Meeting"
  - "Special Meeting and Work Session"
  - "Special Meeting-Tax Rate & Budget Public Hearing"
  - "Special Work Session"
  - "Tax & Budget Public Hearing"
  - "Tax Rate Public Hearing"
  - "Work Session"
  - "Work Session - Economic Development Strategic Plan"
  - "Work Session and Regular Meeting"
```

Files: Council feed YAML, tests/fixtures/addison-council-retained.json (549 lightweight actual
records), tests/test_addison_council_ownership.py (actual config549->501 replay, exact negatives,
P&Z preservation, genuine joints/UID/RSS/audio/search/raw-identity invariants), case register,
body-policy audit, review/11, this contract, CHANGELOG, ARCHITECTURE and ROADMAP.
Full822 local replay changes no other feed memberships. Update47 P&Z and ceremony cases with
evidenced prepared dispositions, retaining IDs/research history; do not claim deployment.
Official representative agenda proof for24 aliases and shared GUID/date/video identity proof for
Regular City Council Meeting399077 are recorded in the audit. Keep future year aliases visible
until evidenced, rather than restoring broad fiscal/strategic wildcards.

## Addison Bond family — L3 (#2016)

Frozen bounded implementation contract: **Implemented in PR #2020**, human-merged
2026-10-04. Remaining evidence/admission gates elsewhere in this document are unchanged.

Create config/feeds/addison-tx-bond-committees.yml, city addison-tx, provider swagit,
meeting_family bond; slug addison-tx-bond-committees; same Swagit list URL128, body_exact
list containing only Bond Advisory Committee. Display Addison: Bond Committees, author
City of Addison, TX, empty email; describe the verified2026 Police/Courts program. No broad
Bond substring, lifecycle assertion, publication groups or pending P2 schema.
Four official dated GUIDs359742/361917/362796/371625 bind existing UID/audio records.
Tests/fixtures/addison-bond-retained.json contains their minimal original metadata/audio;
tests/test_addison_bond_family.py loads actual config and verifies four memberships, original
RSS identities/enclosures, near-match/topic negatives, unchanged raw records and corrected
canonical search owner. Existing unmatched search fallback is BZA; it intentionally becomes Bond,
not a claimed preservation of that erroneous attribution. Full822 replay adds only these four
new-feed memberships; existing feed membership sets remain identical. Update the four cases
with evidenced prepared dispositions, retaining records. Lifecycle/audit files as in #2015.

## Addison TIRZ board — L3 (#2017)

Frozen bounded implementation contract: **Implemented in PR #2021**, human-merged
2026-10-04. Remaining evidence/admission gates elsewhere in this document are unchanged.

Create config/feeds/addison-tx-tif.yml, city addison-tx, provider swagit, meeting_family tif,
slug addison-tx-tif, same Swagit list URL128; source.body_exact LIST contains only
TIRZ #1 Board Meeting (not a boolean). Display Addison: TIF Meetings, author City of Addison,
TX, empty email; describe public tax increment financing/reinvestment-zone board recordings.
Use existing remedy_policy aggregate_family tif and member_names list containing TIRZ #1.
Remove only GUID394474 from Council body_includes, preserving all other selectors/metadata.
Tests/fixtures/addison-tirz-retained.json contains its original minimal metadata/audio;
tests/test_addison_tirz_family.py loads actual config and verifies Council->TIF membership and
canonical search correction, original UID/enclosure, exact negatives/topic exclusion and raw
record preservation. Official cached dated board agenda supplies proof; full822 replay moves
only UID757ab2a4aaf9bbca. Update that case with prepared disposition; lifecycle/audit as above.

All three slices prohibit runtime/provider/storage/stage/record-schema/dependency/audio changes.
No versions, invalidation or backfill; derived feed/search projections rebuild using existing
hashes. Targeted tests plus full offline/Ruff/format and current CI required. Separate dependent
PRs and human merge commits. The2016 joint singleton-owner question remains separately gated.


Integration acceptance for #2017: update tests/test_addison_council_ownership.py to expect
500 Council selections after the separately proved board departure,49 departures from the
original549 (48 initial corrections plus TIRZ). Update the earlier sweep regression in
tests/test_unresolved_recordings_sweep.py to preserve its matched-open-case/assignment-change
assertions against the corrected P&Z-only membership, rather than requiring the removed Council
false match. This is regression compatibility, not a new sweep/runtime path.


## Archive-only disposition — L3 approved serialized contract (#2023)

Maintainer approved broader archive-only visibility for all six Addison recordings on
2026-10-04: GUID55864, 56026, 56027, 56028, 56059 and 56060.
This is a human publication disposition, not a claim that every recording's historical
institution or content binding has been proved. Outstanding evidence obligations remain visible.

Maintainer approved this contract in chat on 2026-10-04.

Approved serialized contract: top-level feed `archive_only` list, each entry containing exactly
`uid`, `provider_guid`, `reason`, and `approval_ref` nonempty strings. The declaring feed's
existing source key scopes the disposition; it applies across every feed/search projection of
that source. Duplicate identical declarations are tolerated; conflicting entries fail config
validation. Exact UID plus provider GUID guards against suppressing an unrelated observation.
No title/substring matching, no blanket body suppression, and no record mutation.

Approved implementation plan: new pure `citypods/archive_visibility.py` parser,
source-scoped index and projection helpers; config validation in `citypods/config.py`; filter
public render projections in `citypods/run.py` after capturing raw page records; filter search
records before document/sidecar generation in `citypods/search.py`. Include the disposition
hash in feed/render and search cache inputs so removal and reversal rewrite derived outputs.
Keep raw meeting pages and direct archived links, stable UIDs/audio, source state and stage versions.
Website feed lists, search documents, speaker projections and recording-based browse/calendar
outputs must omit archive-only recordings; official independent calendar records are not erased.
The read-only unresolved sweep continues reporting these records and their evidence obligations.

Declare the six exact dispositions in Addison Public Input config, retaining existing inclusion
selectors so raw archive pages are preserved. Update case-register next actions to distinguish
human-approved visibility from unresolved identity proof. Do not add the luncheon to Briefings.
Tests: exact identity/source scoping, invalid/duplicate declarations, feed/search/browse absence,
raw-page retention, unchanged bytes/UIDs, and cached-output removal/restoration. Lifecycle docs:
review/11, ARCHITECTURE, CHANGELOG and ROADMAP. No dependency/provider/storage/stage changes,
no audio backfill, no production writes in local verification.

### Addison archive-only deployment evidence — 2026-10-06

Read-only public GET checks against Build & Deploy run 37480863678 (`355bad56`) verified all six
approved UID/GUID pairs across all ten current Addison RSS feeds and both Town Meetings browse
pages: none are listed, while all six direct raw pages return HTTP 200. Citizen Advisory
UID`63b22f80ce9500ff`/GUID`56029` remains in the Town Meetings RSS and both browse pages. The
checks and response hashes are recorded in
[`review/evidence/addison-archive-live-2026-10-06.json`](evidence/addison-archive-live-2026-10-06.json).
The public `/search/` and `/data/search/manifest.json` routes returned HTTP 404; this is recorded as
unavailable live search verification, not as evidence about indexed content. The six publication
dispositions are therefore resolved, with their content/institution uncertainty retained in the
case register. The Citizen Advisory classification case was also closed by the maintainer's
2026-10-07 decision; its exact historical binding remains unknown. No title, UID, source namespace,
raw page, or audio was changed.


## September 2016 Addison joint subscription — L3 bounded correction

The maintainer approved proceeding with the remaining verified joint correction, with canonical
page-link choice separate from redesigned participant discovery (#2018). The earlier proposed
P&Z-owner preservation is superseded by use of the existing Council canonical page choice;
this avoids adding an ownership schema for one UID. Both direct archive pages remain retained.

Files: `config/feeds/addison-tx-city-council.yml` adds only exact body_includes GUID55634,
body `Joint Council & Planning & Zoning Commission Meeting`; new minimal retained fixture
`tests/fixtures/addison-2016-joint.json` and `tests/test_addison_2016_joint.py` exercise actual
config and RSS/search ownership. Existing P&Z rules and runtime modules are unchanged.
Preserve UID383dfdfed6140400, September19 stored date, hosted audio and raw records.
Official cached joint agenda proof is in the Addison audit. Full822 replay must add exactly one
Council membership and change no other feed memberships; negative unrelated-GUID near label.
Case register updates preparation evidence only; keep open until deployed verification.
Lifecycle docs: review/11, audit, CHANGELOG, ARCHITECTURE and ROADMAP. No runtime/schema,
dependency/provider/stage/storage changes, audio invalidation or backfill. Human merge required.

### Arlington Community and Neighborhood Development — L3

Maintainer-authorized catalog-wide committee ownership correction. Four retained 2020 records
have exact body `Community and Neighborhood Development Committee`; dated official agendas
convene that committee alone. Current official Council-member assignments and the2026 city
Neighborhood Matching Grant guide distinguish this continuing committee from full Council.

Create config/feeds/arlington-tx-community-and-neighborhood-development-committee.yml with
slug matching filename, city arlington-tx, provider granicus, existing view2 feed_url, source
body_exact list containing only that exact committee label. Title Arlington: Community and
Neighborhood Development Committee, author City of Arlington, TX, empty email, description
Meetings of Arlington's Community and Neighborhood Development Committee. Do not infer
lifecycle retirement from old cached dates; omit lifecycle and optional meeting_family.
Remove only that label from arlington-tx-council.yml body_any. Preserve arlington-tx All Meetings
and every other selector. No global exclusions or changes to genuine Council joint recordings.

New tests/fixtures/arlington-cnd-retained.json captures four original minimal records/audio;
tests/test_arlington_cnd_ownership.py loads actual config and verifies four Council departures,
four dedicated-feed additions, unchanged All Meetings selection, original RSS UID/enclosure,
near-label/topic negatives and unchanged raw records. Full retained-source replay must show
only those exact membership changes. Update four register cases with prepared proof, not closure.
Lifecycle review11/CHANGELOG/ARCHITECTURE/ROADMAP/audit. Issue before code, whole offline/Ruff,
current CI/CodeRabbit then human merge. No runtime/provider/schema/dependency/stage changes.
Environmental Task Force remains a separate lifecycle/evidence decision, not silently bundled.

### Archive-only and 2016 joint shipped (2026-10-05)

Implemented in human-merged PR #2024 (archive visibility) and PR #2026 (joint subscription).
Their bounded implementation contracts are frozen. Main `ac0fa4bb` Build & Deploy succeeded.
CodeRabbit substantively reviewed #2024 head `56e2cb8e` with no remaining actionable issues;
#2026 was merged before its substantive review slot, an explicit review gap. No stage/audio
invalidation or backfill. Both joint feeds carry the same original UID/audio; its case is resolved.
The register now has seven open historical evidence cases, rather than eight.

Live verification: six archive UIDs absent from Public Input/Council/P&Z RSS and Public Input
browse. Five direct Public Input archive pages return 200. Luncheon UID35a78fa89c7c5c5c returns
404 at checked Public Input/Council/BZA/Briefings paths; its direct archive route remains unverified.
Public search and its expected manifest return 404; do not claim deployed search verification.
Retain these visibility/access checks and all independent historical content evidence obligations.

### Luncheon direct archive routing — L3

Maintainer authorized the proposed access fixes and catalog expansion on 2026-10-05.
Add only GUID55864/body `Addison Economic Development Luncheon` to existing Public Input
body_includes. Its existing archive-only declaration continues excluding all public discovery.
Modify that feed config and tests/test_archive_visibility.py actual-config regression to prove
raw selector admission and archive exclusion, preserving original UID/audio. No runtime changes.
Update audit/review11/CHANGELOG; full offline suite and whole Ruff/format required. Human merge.

### Dallas Redistricting Council leakage — L3 bounded correction

Authorized catalog-wide body-policy correction. Official January22,2022 Commission notice
binds GUID203320/UIDbd172b762dd20b63 to the existing Redistricting Commission feed.
Remove only that GUID override from config/feeds/dallas-tx-city-council.yml. Preserve existing
Commission selectors, source state, UID/audio and archive pages. New minimal fixture
 tests/fixtures/dallas-redistricting-retained.json and tests/test_dallas_redistricting_ownership.py
load actual configs and verify Commission-only membership, stable RSS enclosure/UID, exact
negative and untouched raw record. Full cached Dallas replay must remove only this Council
membership and change no other feed. No runtime/schema/provider/stage/dependency changes.
Update register prepared evidence without closing until deployed proof; review11/CHANGELOG/audit.
Create tracking issue before implementation, whole offline/Ruff/format, CodeRabbit and human merge.

### Denton Health & Building Standards ownership — L3 bounded contract

Authorized catalog correction: four dated official agendas independently convene the commission,
not Council. Council Work Session Room is their venue. Institutional continuity is corroborated
by official 2026 commission agendas and current municipal appointments.

Create config/feeds/denton-tx-health-building-standards-commission.yml, slug matching filename,
city denton-tx, provider swagit, source.list_url https://dentontx.new.swagit.com/views/5.
Use source.body_includes only the existing four exact GUID/body pairs 122398,120835,120506,119886
copied from Council. Remove only those four Council overrides. Title Denton: Health & Building
Standards Commission, author City of Denton, TX, empty email and descriptive commission text.
Omit optional meeting_family and lifecycle; no broad substring rule or invented family.
This bounded migration does not claim complete commission recording coverage.

New tests/fixtures/denton-health-standards-retained.json captures four minimal original records
and hosted audio. tests/test_denton_health_standards_ownership.py loads actual config, verifies
four Council departures/four commission additions, near-GUID negatives, RSS UID/audio and
unchanged raw records. Full retained Denton source replay must change only these four memberships;
retain the proved Council/Library joint. Update four case preparation evidence, keep cases open.
Lifecycle files: review11, audit, CHANGELOG, ARCHITECTURE and ROADMAP. Whole offline/Ruff/format,
current CI and CodeRabbit before human merge commits. No runtime/schema/provider/dependency/stage
changes, invalidation, backfill or production writes. Remaining Denton bodies are separate slices.

Denton Health & Building Standards implementation #2044 was human-merged on 2026-10-05
at10:00:30Z, before substantive CodeRabbit review and before PR CI completed. Record this review
gap explicitly; skipped automatic review is not coverage. Full4918 offline tests passed locally.
Deployed membership verification remains pending; four cases stay open.

### Denton Library Board ownership — L3 bounded contract

Two retained official agendas convene Library Board alone: GUID120859 (May13,2021,
UIDb50c6a018b2c08e7) and118306 (Apr8,2021,UIDc10f3f6cd97ac38f). The March3,2014
GUID13509 UIDa1aa4af8f26e120c agenda explicitly convenes Council jointly with Library Board.

Create config/feeds/denton-tx-library-board.yml, slug denton-tx-library-board, city denton-tx,
provider swagit, existing source.list_url https://dentontx.new.swagit.com/views/5.
Use body_includes the three exact existing Council GUID/body pairs copied unchanged. Remove only
120859 and118306 from Council; preserve13509. Title Denton: Library Board, author City of Denton,
TX, empty email, descriptive Library Board text; omit lifecycle and optional meeting_family.
No broad substring selection. This slice does not claim complete Library recording coverage.
New tests/fixtures/denton-library-retained.json captures three original metadata/audio records;
tests/test_denton_library_ownership.py verifies actual config, two Council departures/three Library
additions, joint remaining Council, RSS UID/audio, exact-GUID negatives and unchanged records.
Full retained Denton replay must change only these memberships. Update three register preparation
next actions without closure. Lifecycle review11/CHANGELOG/ARCHITECTURE/ROADMAP/audit. Whole offline,
Ruff/format/currentCI. CodeRabbit only actual code changes per maintainer override2026-10-05;
configuration YAML/documentation alone needs no bot review. Human merge commits only.
No runtime/schema/provider/stage/dependency changes, invalidation, backfill or production writes.

Library integration acceptance: tests/test_feed_body_routing.py must remove GUID13509 from
the obsolete single-owner list and explicitly require exactly Council and Library holders.
This updates the earlier #1231 regression to the approved proven-participant joint policy;
retain all unrelated single-owner pins. No runtime change.

### Denton TIRZ boards ownership — L3 bounded contract

Eight retained dated agendas explicitly convene their named TIRZ board, not Council. Zone1
has six records (122084,116732,112601,87440,78064,73923); Zone2 has two (111521,14400).
The official Development Districts page and Legistar departments corroborate distinct boards.
Council Work Session Room is a venue, not joint participation proof.

Create config/feeds/denton-tx-tirz-1-board.yml and denton-tx-tirz-2-board.yml, slugs matching
filenames, city denton-tx, provider swagit, existing source.list_url
https://dentontx.new.swagit.com/views/5. Each source.body_includes copies only its listed exact
GUID/body pairs unchanged from Council; remove only these eight Council overrides. Titles
Denton: TIRZ No. 1 Board and Denton: TIRZ No. 2 Board, author City of Denton, TX, empty email,
descriptions naming each board. Omit optional meeting_family/lifecycle, no broad selectors.
Do not infer complete board coverage or retirement from these old cached records.

New tests/fixtures/denton-tirz-retained.json captures eight original metadata/audio records.
tests/test_denton_tirz_ownership.py loads actual config, verifies eight Council departures,
six Zone1/two Zone2 additions, no wrong-zone matches, exact-GUID negatives, original RSS
UID/audio and raw records. Retain the Council/Library joint. Full2176-source replay changes only
these memberships. Update eight register next actions, do not close until deployed verification.
Lifecycle review11/CHANGELOG/ARCHITECTURE/ROADMAP/audit. Whole offline/Ruff/format/currentCI;
CodeRabbit actual code only under maintainer override. Human merge commits only. No runtime,
schema/provider/stage/dependency changes, invalidation, backfill or production writes.

### Denton 2019 Special Citizens Bond committee ownership — L3 (#2051)

The retained official June 6, 2019 agenda explicitly convenes the Special Citizens Bond
Advisory Committee, not Council, for public safety facilities in the proposed 2019 Bond
Program. Exact source binding: GUID `28865`, UID `04689e3929ac104e`, body
`Special Citizens Bond Advisory Committee on 2019-06-06 6:00 PM`, existing Denton Swagit
source `https://dentontx.new.swagit.com/views/5`. This proves the named proceeding; it does
not establish standing status, formal dissolution or other years' committee identities.

Create `config/feeds/denton-tx-special-citizens-bond-advisory-committee.yml`, city `denton-tx`,
provider `swagit`, existing list URL, exact single GUID/body `body_includes` only. Title
`Denton: Special Citizens Bond Advisory Committee`, author `City of Denton, TX`, empty email,
description `Verified recording of the Denton Special Citizens Bond Advisory Committee.`
Omit optional family/lifecycle metadata. Remove only GUID `28865` and its body from
`config/feeds/denton-tx-city-council.yml`; preserve every other selector.

Add original minimal record fixture `tests/fixtures/denton-bond-retained.json` and
`tests/test_denton_bond_ownership.py`: actual config selects the original committee record,
Council rejects it, unchanged body with different GUID rejects it, RSS preserves UID/audio,
and input record remains unchanged. Replay all 2,176 retained source records against the
parent baseline: exactly one Council departure and one new committee addition, no other
feed changes. Run the full offline suite and whole-repository Ruff lint/format checks.

Update this contract and review/11 with implementation state, plus CHANGELOG, ARCHITECTURE,
ROADMAP and the committed ownership audit. Set only this register case's next action to
prepared verification; keep it open until deployed RSS identity/ownership proof. Freeze/stamp
only after the change reaches main. Do not modify runtime, schemas, providers, dependencies,
stages, records, audio or publication groups; no invalidation/backfill or production writes.

Bond #2051 implementation is prepared on `feat/2051-denton-bond-feed` under this contract.
Full 2,176-record replay changes Council 756→755 and the new committee feed 0→1 only;
targeted identity/negative checks and whole-repository Ruff pass. The case remains open for
post-main deployment verification; the contract is not frozen before the parent stack merges.

### Denton bounded stack implemented — frozen stamp, 2026-10-05

The Health Commission, Library, TIRZ boards and 2019 Bond committee contracts above are
implemented and frozen in PRs #2044/#2043, #2047/#2046, #2050/#2049 and #2053/#2052.
The parent stack reached main `8a4adbeb` on October 5. Earlier #2044 and #2053 child merges
into documentation branches did not themselves ship to main. Original raw records, UIDs,
audio and source namespaces remain unchanged. No stage invalidation or backfill occurred.

Library substantive review covered `cb4fa348`; one exact-GUID negative-test finding was
fixed in test-only `1690b8e0`, replied and resolved, with green CI. Health #2044 merged before
substantive review. TIRZ's only full request at 12:30:52 UTC was aborted on head change;
#2050 merged before retry. Bond #2053 merged before a full request. These are explicit
review gaps, not completed reviews. Do not request reviews on the closed PRs. Full offline
suites and CI passed for the implementation heads; deployed ownership verification remains
outstanding. Keep the sixteen Denton cases open until actual deployed proof is recorded.

### Arlington Environmental Task Force — bounded L3 (#2055)

Six retained 2020 agendas explicitly convene the Environmental Task Force with its own call
and adjournment. Council references describe reports/recommendations, not a joint call.
Full cached source `ecc3710ac47f` census finds exactly six ETF records. The 2022 official
ARP report describes recommendations completed in 2020 and Municipal Policy Committee
implementation in 2021; do not infer standing status or formal dissolution from that report.

Create `config/feeds/arlington-tx-environmental-task-force.yml`, city `arlington-tx`, provider
`granicus`, existing feed URL from Council, exact GUID/body `body_includes` for the six UIDs
below only. Provider GUIDs are the original full MediaPlayer URLs, never just clip integers.
Title `Arlington: Environmental Task Force`, author `City of Arlington, TX`, empty email,
description `Verified 2020 Arlington Environmental Task Force proceedings.` Omit optional
family/lifecycle metadata. Remove only Council's `Environmental Task Force` body_any term.
Keep the deliberately unfiltered `arlington-tx` All Meetings feed unchanged.

| UID | clip_id in original `https://arlingtontx.granicus.com/MediaPlayer.php?view_id=2&clip_id=` | Exact body |
|---|---|---|
| 1792515fdd51c6c2 | 3443 | Environmental Task Force |
| 25e9b8510b0a7a66 | 3494 | Environmental Task Force Meeting |
| 2d5dc451df2da130 | 3454 | Environmental Task Force Meeting |
| 35b22fcc8d07967b | 3424 | Environmental Task Force Meeting |
| 9b66e98b682a47f2 | 3508 | Environmental Task Force Meeting |
| a683af6e13e2bd7a | 3479 | Environmental Task Force Meeting |

Add original minimal six-record `tests/fixtures/arlington-etf-retained.json` and actual-config
`tests/test_arlington_etf_ownership.py`: ETF selected, Council rejected, All Meetings retained,
RSS original UID/audio, unchanged raw input, and same body with different GUID rejected.
Replay all 1,515 source records: exactly six Council departures/six ETF additions, all other
feeds including aggregate unchanged. Whole offline/Ruff/format checks required. Update
review/11, CHANGELOG, ARCHITECTURE, ROADMAP, audit and six register next actions in the
implementation; keep cases open until deployed proof. Freeze/stamp only after main merge.
Do not modify runtime, schemas, providers, dependencies, stages, records, audio, publication
groups or other selectors. No invalidation/backfill or production writes.

Arlington ETF #2055 implementation is prepared on its feature branch: six exact GUID/body
inclusions and only the Council ETF term removed. Full1,515 replay changes Council962→956,
ETF0→6 and no other feed, including All Meetings. Deployment verification remains pending.

Arlington ETF bounded contract is implemented/frozen in PR #2058 (human merged October 5,
2026 at 15:31:42 UTC). Conflict resolution `8c312d45` preserved both roadmap entries and
main's unrelated Worker changes; ETF selectors/test remained unchanged, ten targeted checks
and whole Ruff/format passed, current CI green. Full offline implementation suite passed4,927.
Full review request5997651345 at15:30:50 UTC received reply5997653803 "Pull request is closed."
No substantive review completed before merge; record the explicit gap, never request closed
reviews. ETF six deployment cases remain open until live proof. No invalidation/backfill.

### Dallas November 2023 Community Bond Task Force — bounded L3 (#2062)

Official city CBTF archive and November4,2023 minutes establish a task-force proceeding,
chaired by Arun Agarwal with separate call/adjournment. Stored original recording280220
chapter explicitly identifies that same date. Subcommittee chairs attend, but no separate
subcommittee or Council convening is proved. Minutes header gives8:24AM; body says8:24PM,
an official inconsistency which must remain visible rather than silently corrected.

Create `config/feeds/dallas-tx-2024-community-bond-task-force.yml`, city Dallas, Swagit
provider and exact existing Council list_url. Use only body_includes GUID `280220`, exact
body `2024 Bond CBTF and Subcommittee Chairs Meeting`, no broad body/family/lifecycle.
Title `Dallas: 2024 Community Bond Task Force`, author `City of Dallas, TX`, empty email,
description `Verified Dallas 2024 Community Bond Task Force proceedings.` Remove only that
GUID/body pair from Council. Preserve UID `baa19208405acb22`, source `76869ed1994f`, audio
and raw record. Add minimal original `tests/fixtures/dallas-cbtf-retained.json` and
`tests/test_dallas_cbtf_ownership.py` actual-config selection, Council negative, original RSS
UID/audio, same body wrong GUID negative and unchanged raw input. Existing body_includes
selects by exact provider GUID; its body is metadata, not a second predicate. Do not change
that runtime contract.
Full5,353-record replay: one Council departure, one task-force addition, every other feed
unchanged. Whole offline/Ruff/format required. Update review/11, CHANGELOG, ARCHITECTURE,
ROADMAP, audit and register next action in implementation; keep case open until deployed
proof; freeze/stamp after main merge. Do not modify runtime, schema, provider, dependencies,
stages, records, audio, publication groups or other selectors. No invalidation/backfill.

Census also finds17 other CBTF/subcommittee/town-hall rows; their proof and coverage require
separate reconciliation. The exact single-record correction does not claim full family coverage.

Dallas #2062 implementation prepared under the exact contract. Replay changes only
Council358→357 and CBTF0→1; original raw records unchanged. Offline4,929 pass14 deselected,
wholeRuff463/format pass. Current CI/review and deployed proof remain pending.

### Dallas four additional CBTF recordings — bounded L3 (#2065)

Dated city summaries establish CBTF convening/adjournment on June20, August15 and August22,
2023. Original provider chapters identify those same dates; official city archive also links
August22 GUID269916 directly. Add only these exact GUID/body inclusions to existing
`config/feeds/dallas-tx-2024-community-bond-task-force.yml`:

| UID | GUID | Exact original body |
|---|---|---|
| 96c10f4f75706719 | 246971 | 2024 Capital Bond CBTF Meeting |
| aaf0ad8eaabe68a5 | 269278 | 2024 Capital Bond CBTF Meeting |
| 130a7ebded8e0e1d | 269608 | 2024 Bond Task Force Community Bond Task Force Meeting |
| cba1e023051d7c94 | 269916 | 2024 Capital Bond CBTF Meeting |

All four currently match no feed. Preserve both August15 original UIDs and all audio/raw
source identities; no duplicate-winner claim or publication-group change. Add original minimal
`tests/fixtures/dallas-cbtf-additional-retained.json` and extend
`tests/test_dallas_cbtf_ownership.py` with parametrized actual-config original RSS/audio,
no Council holder, same-body/different-GUID negative and raw-preservation checks for each.
Existing exact GUID runtime semantics stay unchanged. Full5,353 replay must add only these
four UIDs to CBTF1→5, every other feed unchanged. Wholeoffline/Ruff/format required.
Update review/11, CHANGELOG, ARCHITECTURE, ROADMAP, audit and four register next actions.
Keep cases open until deployed verification; freeze/stamp after main merge. Do not modify
other selectors, runtime, schemas, dependencies, providers, stages, records, audio or
publication groups. No invalidation/backfill. May25 GUID233037 excluded pending stronger
exact binding (current provider403 and no original chapter); other candidates remain open.

Dallas #2065 implementation prepared. Exact four additions/no other full-source ownership
changes; all4,933 offline tests pass14 deselected. WholeRuff463/format clean. Cases stay open
until deployment; current CI and substantive code review still pending.

### Dallas reusable Economic Development ownership rules — design follow-up

The maintainer requested reusable institution selectors where possible. Existing source
`body_exact` supports complete normalized labels without a new runtime/schema. The cached
Dallas census has 138 Economic Development substring matches across 12 labels; one is the
separately convened 2024 bond subcommittee (GUID269610). Replace the standing feed's broad
substring with the other 11 complete labels only after recording the full alias contract and
positive/negative replay. Preserve its unrelated GUID overrides and all genuine joint aliases.
A dedicated bond-subcommittee feed should use its stable complete institutional label rather
than require a new GUID exception for each recurrence. Original August15 chapters and official
minutes establish this body independently of the standing Council committee.

This entry is L2: no selector changes yet. Before L3, name the alias list, feed/fixture/test
files, exact source replay deltas, and issue. Do not modify runtime body matching, providers,
schemas, dependencies, stages, records, media, publication groups or existing joint policy.

### Dallas Economic Development reusable rules — L3 (#2068)

Supersedes the L2 follow-up above. In `config/feeds/dallas-tx-economic-development.yml`,
replace only `source.body` with `source.body_exact` containing these 11 complete labels:

- Economic Development
- Economic Development and Housing Committee
- Special Economic Development
- Special Economic Development and Housing Committee
- Special Called Joint Meeting of the Council Ad Hoc Committee on Professional Sports Recruitment and Retention and Council Economic Development Committee
- Combined Economic Development & Housing
- Special Called Joint Meeting of Economic Development and Workforce, Education, and Equity
- Joint Meeting of Transportation and Economic Development Meeting
- Special Call Meeting Economic Development & Housing Committee Housing Policy Stakeholder Forum
- Economic Development & Committee on Finance
- Economic Development Committee

Preserve both existing GUID inclusions 205545/203327. Add
`config/feeds/dallas-tx-2024-bond-economic-development-subcommittee.yml`, same city/provider/
list_url as the standing feed, with only `source.body_exact` label
`2024 Bond Task Force Economic Development`. Title it Dallas 2024 Bond Economic Development,
Housing and Homeless Solutions Subcommittee. This is a reusable complete-label rule, not a
GUID pin. Official August15 minutes and original chapters bind GUID269610,
UID8969ad6d91ed23d2 to its independently convened proceeding.

Add minimal original fixture `tests/fixtures/dallas-bond-economic-retained.json` and
`tests/test_dallas_bond_economic_ownership.py`: actual-config standing rejection/new-feed
acceptance; original RSS UID/audio and unchanged raw record; same-label/new-GUID acceptance;
other bond subcommittee and unrelated Economic Development label negatives; all 11 standing
aliases including joint labels retained. Replay all5,353 retained Dallas rows: standing feed
loses only UID8969ad6d91ed23d2, new feed gains only it, every other holder unchanged. Run whole
Ruff/format/offline. Update review/11, CHANGELOG, ARCHITECTURE, ROADMAP, audit and case next action.
Case remains open until deployed ownership/audio proof. Do not modify runtime, other selectors,
schemas, dependencies, providers, stages, records, media, publication groups or joint policy.
No invalidation/backfill; no inference of other bond subcommittees' recording proof.

#2068 prepared: 193 targeted tests pass; final full offline4,935 pass14 deselected.
WholeRuff464/format/diff clean. Initial serializer indentation failure corrected without
runtime changes. Full replay preserves all other holders; deployed closure remains pending.

### Dallas reusable Streets bond ownership rules — Implemented in PR #2082 (frozen)

Approved scope: resolve remaining registered recordings with reusable institution rules under
maintainer direction. Independent city bond archive and May25/June13 minutes establish Streets
and Transportation as a separately convened 2024 Bond Task Force subcommittee, chaired by Linda
Koop. Original chapter dates corroborate May11/June13/June28/July17/Aug15 records. Institution
routing does not assert that every recording's date/content evidence is complete.

Files: replace `body`/`body_any` in
`config/feeds/dallas-tx-transportation-and-infrastructure.yml` with existing `body_exact`:
all current matched full labels except the four 2024 bond labels and the three exception-only
labels Mill Creek Tunnel Project, Transportation for Hire, Transportation-for-Hire Work Group.
Preserve existing four `body_includes` entries unchanged. Add
`config/feeds/dallas-tx-2024-bond-streets-and-transportation-subcommittee.yml`, same Dallas/source
Swagit113, exact four labels from the committed census. No new selector/schema/runtime path.
Existing named DART/legislative/Economic/Government Performance joints remain standing aliases.

Add original eleven-record fixture `tests/fixtures/dallas-bond-streets-retained.json` and test
`tests/test_dallas_bond_streets_ownership.py`: all originals move standing→bond; each same-label
new GUID auto-admits bond; all standing aliases retain standing and reject bond; four GUID
exceptions preserve existing identities and reject same-body different-GUID exception rows;
other bond bodies/unrelated labels reject; RSS preserves each UID/audio and raw records unchanged.
Use standard indented YAML lists compatible with existing feed editor.

Full cached-source/all-feed replay acceptance: 5353 rows, standing140→129, bond0→11, no other
holder additions/removals. Whole Ruff/format and full offline suite required. No provider/stage,
audio/source UID, storage, dependency, workflow, publication-selection, search or record changes.
No invalidation/backfill. Update audit/register prepared actions, ARCHITECTURE/CHANGELOG/ROADMAP
and review11. Close no case solely from selectors; keep independent missing-date/content evidence
and deployment verification explicit. Freeze/stamp this section only after human merge.

### Remaining37 maintainer decisions — October5 follow-up

Maintainer explicitly requests resolution of all37 registered open recordings. Independent
proof work may proceed in parallel; closures still require verified evidence and deployed proof.
Fort Worth3814 disposition approved: keep whole consecutive CCPD/Council bundle in both proven
participant feeds, explicitly label it as consecutive proceedings. Preserve both source-view
UIDs and whole audio; no duplicate winner or audio split. Design the bounded presentation-only
label contract before changing code; no broader bundled-recording inference.

Search scheduling choice approved: run search in a separate bounded job, keep the existing
search time ceiling. Prepare exact workflow/CLI/artifact ownership and failure/preservation
contract before implementation. This is authorization for the chosen approach, not permission
to introduce an unspecified schema, raise a budget or touch credentials interactively.

### Dallas2017 bond taskforce versus town halls — Implemented in PR #2085 (frozen)

Exact agenda binding supersedes misleading shared provider label: May27 GUID202453
UID5e9fdb6ff9e8ef68 provider agenda SHA256eb8a902b8b7b6d309acd807bb940cea1d4fa08ad041ec4ee609107223125dc8f
matches official revised formal2017CitizensBondTaskForce notice. May22/May25 GUID202451/202452
UIDfc9ad3b1d9def142/eb1c279cc51a9b04 agendas share official two-public-town-hall notice,
SHA256bf95a89fcf271b90183ab2b94f8c686a9dac614485326cb938e0d0beeefe0be7.
Both notices visually verified; Council Chambers is venue, not Council participation.

Add `config/feeds/dallas-tx-2017-citizens-bond-task-force.yml` with exact GUID202453 inclusion,
and `config/feeds/dallas-tx-2017-bond-public-town-halls.yml` with exact GUID202451/202452.
Both reuse city/provider/source Swagit113 and expected original body
`Citizens Bond Task Force: Town Hall Meeting`; runtime GUID semantics remain unchanged.
Three source records are all cached occurrences of this shared label. Distinct subscriptions
are necessary because the same label denotes two different proceeding types; do not generalize
by label here. No Council/source/other-feed edits or raw title replacement.

Add `tests/fixtures/dallas-2017-bond-retained.json` original three records and
`tests/test_dallas_2017_bond_ownership.py`: actual configs exact owners, same-body different-GUID
negative, each other owner's GUID negative, RSS original UID/audio, raw identity unchanged.
Full5353-record/all-feed replay: formal0→1, publictownhalls0→2, all other feeds unchanged.
Whole Ruff/format/offline required; no stage invalidation/backfill. Update ARCHITECTURE,
CHANGELOG, ROADMAP, review11 and audit/register prepared actions. No case closure before live
RSS original audio/ownership proof. Freeze/stamp only after human merge.

### Separate bounded static search job — L3, #2084

Maintainer approved separate job at unchanged20minute ceiling. Correction: current deployment
already uses render phase, whose search creates a fresh deadline; the older exhausted shared
stop diagnosis is not current deploy behavior. Separate scheduling is still the chosen approach.

Named files: `citypods/cli.py`, `citypods/run.py`, `citypods/search.py`,
`.github/workflows/deploy.yml`, `tests/test_static_search_job.py`, `tests/test_workflows.py`,
existing search/run/CLI tests,
ARCHITECTURE/CHANGELOG/ROADMAP/review11 and this section. No dependency/provider/stage/state
record/storage-write/Worker changes or public search schema changes.

CLI: add render-only `build --skip-search` and `--search-context-output` path; add
`search-index --state-dir --output-dir --site-config --config-dir --base-url --context-path`.
Coordinator `search.build_search_site` directly reuses `build_search_index`, existing config
loaders, `make_storage` sidecar read operations and templates. Never call build/providers,
statesync or record persistence from this command. Fresh monotonic deadline uses configured
search_index_budget_minutes, rejects values above20 or nonpositive values. No live model calls.

Render exports same-run `feed_info`/base URL into internal artifact context JSON at the explicit
context-output path, never a source record. Skip-search preserves prior complete files and
advertises search only if a complete manifest/page already exists. Default local build behavior
is unchanged. Search staging copies prior generated site/index before any shard mutation;
mutate staged shards and output-local search cache only. On deferral/error discard staging,
preserve every prior public index/shard/asset/navigation byte. On complete success publish staged
search dirs/assets/page and regenerate root navigation from artifact feed_info; update root
meta search counts without changing unrelated metadata. Remove stale source shards only on success.

Workflow split: render→search→deploy. Render retains cache/statesync/read credentials and feed
validation, runs `build --phase render --no-refresh --skip-search` plus explicit context path,
then uploads same-run generated docs, context, and only `.citypods-state/sources/*/episodes.json`
as search record inputs. Search downloads that exact artifact (no fresh snapshot), installs
same pinned production deps, runs new command under20minute indexing deadline with bounded job
setup allowance, uploads complete site. Deploy consumes only that site's same-run artifact,
retains existing Pages concurrency/deployment environment/retries. Keep current60minute job
ceiling; job-level least privilege: render contents:read plus pages:read for existing metadata lookup, search contents:read,
deployment pages:write/id-token:write only.
Existing storage read secrets scoped to search command; no new names/backend or interactive auth.
Preserve paths on deferral and allow deployment of retained complete site with explicit summary.

Tests: CLI wiring/invalid phase/budget, fresh independent deadline, no record/provider/storage
writes, stopped second-source build preserves all old public bytes, complete success drops stale
source output and updates navigation/meta, disabled-search behavior, same-run artifact flow,
six Addison archive UIDs absent from complete shards while raw pages remain. Whole offline suite,
whole Ruff/format and current-head workflow/code review required before human merge. Freeze/stamp
only after merge; direct live search verification still required for archive-case closure.

#2083 prepared:185 targeted and4957 offline tests pass15 deselected, whole Ruff/format468
clean. Full5353 replay changes only formal0→1/publictownhalls0→2. Deployment gates remain.

### Consecutive Fort Worth3814 publication note — L3 (#2107)

**Disposition and field schema approved 2026-10-06.** Both whole recordings remain in Council and
CCPD subscriptions and identify consecutive proceedings. The approved note is “Consecutive CCPD
Board and City Council proceedings; whole recording retained.” Official title/description, source
chapters, stable UID, and audio remain unchanged.

**Bounded implementation:** add `publication_notes` to only the Council and CCPD feed configs,
with strict entries containing `uid`, `provider_guid`, `note`, and `approval_ref`. The two exact
bindings are UID `0c66fc968402fabd` to the provider GUID ending `view_id=10&clip_id=3814`, and UID
`99152cb9e61fa091` to the GUID ending `view_id=11&clip_id=3814`. The exact provider GUIDs are
`https://fortworthgov.granicus.com/MediaPlayer.php?view_id=10&clip_id=3814` and
`https://fortworthgov.granicus.com/MediaPlayer.php?view_id=11&clip_id=3814`. Their shared source key is
`6540eef2dc2e`; all 28 configured Fort Worth feeds currently resolve to that one source archive.
Keep these notes in the two participant feed configs so they do not appear in unrelated feeds.

- `citypods/models.py`: add parsed `City.publication_notes` config and ephemeral
  `Episode.publication_note` presentation fields. The episode field must stay out of persisted
  records.
- `citypods/config.py`: strictly parse the optional list; require exactly the four named nonempty
  string keys, reject unknown keys and duplicate UIDs, and preserve the exact strings.
- `citypods/run.py`: after source-key records are loaded and feed bodies are selected, validate
  each present UID against its exact provider GUID before writing any feed/page output. Project
  notes onto shallow episode copies for the two participant feeds; on a mismatch return an error
  without replacing existing output. Missing UIDs remain unannotated until present.
- `citypods/feeds.py` and `templates/meeting.html.j2`: HTML-escape the note and display it as a
  separate publication note in RSS rich show notes and raw meeting pages. Do not overwrite the
  provider description or title.
- `citypods/records.py`: include the ephemeral note in `feed_content_hash` and `meeting_page_hash`
  so note edits re-render only; do not add it to `audio_spec_hash` or `episode_to_record`.
- Tests in `tests/test_config.py`, `tests/test_feeds.py`, `tests/test_site.py`,
  `tests/test_run.py`, and `tests/test_records.py`: cover exact positive bindings, wrong UID/GUID,
  unrelated feeds, HTML escaping, RSS/page output, immutable records/audio, note-sensitive render
  hashes, and preserving prior output on identity mismatch.

No new modules, dependencies, provider behavior, search schema, stage versions, audio splitting,
publication winner, other source/holder change, or backfill. Run targeted tests, the whole offline
suite, whole `ruff check .`, and `ruff format --check .`; CodeRabbit review is required for the
resulting code PR. Human merge only.

Implemented in [PR #2111](https://github.com/BashfulBits/city-meeting-podcasts/pull/2111), merged
2026-10-07 (merge commit `a23efe3e1c82ca42cd6270d24e05c3b7fb701251`); deployed successfully in
Build & Deploy run [#37643318454](https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/37643318454).
This contract is frozen. On 2026-10-07, the maintainer accepted the two permanent Council pages
plus the bounded RSS window as sufficient P0 publication evidence; both #3814 register cases are
closed.

#2084 test compatibility: existing tests/test_workflows.py pins the superseded single
build-deploy job. Update only deploy workflow job/artifact/order assertions for approved
render→search→deploy, retaining credential scope, pinned action, permissions, refresh/render
and deploy gates. This is required validation of the already approved three-job contract.

#2084 Pages metadata compatibility: pinned configure-pages45bfe uses getPages for existing
site metadata; enablement defaults false. Render retains this action with explicit
enablement:false and pages:read only, preserving dynamic base_url for custom-domain and
github.io sites. Provisioning is disabled; Pages/id-token write remains deployment-only.

October5 lifecycle checkpoint: #2082 human merged23:54:21UTC (20a9d83b), #2085 human
merged23:54:57UTC. These bounded contracts are frozen. Streets full-review request
6005806253 at23:51:28 received base/head-changed rejection at23:51:36; no substantive
coverage confirmed before merge.2017 was merged before review request. Explicit review gaps;
green CI/skipped automatic status is not review coverage. No requests on closed PRs.
Both implementations shipped; case closures still await actual deployed RSS/audio proof.

#2084 shipped in PR #2087: separate bounded render, search and deployment jobs with a persisted
same-run source-record artifact and last-complete search publication. The checkpoint extension
merged without substantive review of the final material change; this explicit review gap is frozen
below. Build & Deploy #37480863678 succeeded on `355bad56`; the public `/search/` and manifest routes
return 404, so live search coverage remains unverified. The six approved Addison archive-only
dispositions were closed only after separate live RSS, browse and direct-raw-page checks recorded
above; offline tests alone did not close them.

### #2084 review checkpoint extension — L3, maintainer authorized 2026-10-06

CodeRabbit4190579663 correctly identifies that render saves its cache before search completes;
next-run fallback therefore cannot promise the latest completed index. Proposed extension in
already named search/workflow/test files: `.citypods-search-checkpoint/` local working directory,
containing resumable completed shards/cache and a separately committed last-complete search
publication. Restore/save with the existing pinned cache action in the search job. Deferred
partial work may advance only the working checkpoint; public output uses only last-complete
manifest/shards/assets/navigation. Never replace complete public output with a partial manifest.
Cross-run success→deferral and multiple-budget completion tests are required. Existing20minute
indexing ceiling, same-run source-record artifact, read-only storage and credentials stay fixed.
The maintainer explicitly directed addressing #2087 after the checkpoint finding and proposal.
Implement in citypods/search.py build_search_site, .github/workflows/deploy.yml search job,
and tests/test_static_search_job.py; update ARCHITECTURE/CHANGELOG/review11 and this contract.
Use checkpoint working/ and complete/ directories plus working cache; persist completed-source
progress on deferral while overlaying only complete search publication on the current rendered site.
Preserve fresh non-search pages. A malformed or missing checkpoint must never delete prior output.
No provider/schema/storage writes, dependencies, stage changes or deadline increases.
Run full offline and whole Ruff/format; material fix requires current-head substantive review.


#2086 implemented in PR #2088, human merged2026-10-06T01:24:40Z. Contract frozen.
Human merge preceded a substantive CodeRabbit request; explicit review gap, no closed-PR review.
Live EMSISD RSS and retained Council page verify the original UID/audio; current bounded Council
RSS omits this historical item. Closure proof is recorded in the committed audit.

### Dallas additional CBTF proceedings — L3, #2092

Verified official archive/source bindings are recorded in the October6 additional CBTF
evidence sections of the committed audit. Existing CBTF institution policy applies.

Files: config/feeds/dallas-tx-2024-community-bond-task-force.yml; new original-record fixture
tests/fixtures/dallas-cbtf-three-retained.json; tests/test_dallas_cbtf_ownership.py; this doc,
review11, CHANGELOG, ROADMAP and the evidence audit/register for preparation/deployed status.
No ARCHITECTURE change: existing body_exact/body_includes semantics are reused.

Preserve five existing GUID inclusions. Add exact body_includes259822 (2024 Capital Bond
CBTF Meeting) and272005 (2024 Capital Bond Program CBTF Meeting). Add one reusable body_exact
alias: 2024 Capital Bond CBTF and Subcommittee Chairs Meeting, proven277589. Future GUIDs
with that exact complete institutional alias are eligible. This earlier contract left shared
labels233037/272574 excluded; later approval for272574 is specified in the Dallas September26
Town Hall contract above. No Council selector changes.

Retain original UID deb5a67aa9b6e8d1/259822, eeca5aeceda62d39/272005 and
6a3caf1854bdd2cd/277589 with original audio/title/date/chapter metadata. New fixture is an
unaltered copy of these three retained records. Tests use actual config, prove own RSS/audio
identity and Council absence, unchanged records, same-body/wrong-GUID rejection for two
pins, future-GUID acceptance only for exact chairs alias, and near-label negatives.

Full5,353-record replay must change only CBTF5→8, exactly these three UIDs; every prior
holder remains identical. Whole offline suite, whole Ruff/format and diff check required.
Actual test-code review follows repository-wide65minute gate; human merge commits only.
Keep all three cases open/prepared until live own subscription/audio verification.

Do not modify providers, models, schema, runtime modules, storage, stages, workers, workflow,
other city/feed configs, source records or audio; no winner, split, backfill or invalidation.
Freeze/stamp this bounded contract and mark shipped after human merge, not preparation.

#2092 preparation: selectors/tests implemented; cases remain open until deployed own RSS/audio
verification. Full retained-source replay changes only CBTF5→8, the three contracted UIDs.

### Dallas bond Critical Facilities institution — L3, #2094

Evidence: official archive dated August15 Meeting#5 plus original269611 chapter binds the
recording. Correctly linked August22 minutes independently prove the named subcommittee,
chair and own convening/adjournment. These are separate evidence roles: do not claim
August22 minutes describe August15. Missing August15 minutes remain a documented limit.
The committed audit retains the official URLs and excludes the mislinked May11 summary.

Files: new config/feeds/dallas-tx-2024-bond-critical-facilities-subcommittee.yml; new
tests/fixtures/dallas-bond-critical-retained.json; new tests/test_dallas_bond_critical_ownership.py;
CHANGELOG, ROADMAP, review11, this doc, evidence audit/register. No architecture change.

Reuse Dallas views113 provider source and body_exact only:
2024 Bond Task Force Critical Facilities Subcommittee Meeting. New dedicated podcast named
Dallas: 2024 Bond Critical Facilities Subcommittee, existing Dallas author/email conventions.
Exact institutional label automatically accepts future GUIDs; no Critical Facilities topic
substring, alternate unproven alias or GUID replacement. Copy retained977d943cea00258e/269611
unaltered into fixture. No prior holder changes, no other feed config modifications.

Actual-config tests: own feed eligibility, absent Council/standing committees, exact original
RSS UID/audio, unchanged fixture, future GUID with same complete label admitted, near-label
public-town-hall/standing-Council/topic-only candidates excluded. Replay all5,353 rows/1,095
labels: new feed0→1 only977d943cea00258e; all previous feed holder sets byte-identical.
If replay differs, stop and investigate before widening scope. Run targeted routing/sweep,
whole offline suite, whole Ruff/format and diff checks. Actual code review requires the
repository-wide65minute request gate and current CI. Human merge commits only.

Record prepared selector assignment while keeping case open until live own RSS/original
audio proof. After human merge freeze/stamp this contract and ship lifecycle docs.
Do not modify runtime/model/provider/schema/storage/stage/worker/workflow/other selectors,
records/titles/dates/chapters/audio; no splits/winner/backfill/invalidation or quota work.

Prepared #2094: exact institutional feed implemented; deployment verification remains required.

### Dallas CBTF meetings in the Bond Program aggregate — L3, #2152

The maintainer approved the October3,2023 Dallas Community Bond Task Force recording for the
Dallas Bond Program feed only. Public comment during an ordinary committee meeting does not by
itself make that recording a Public Info meeting. The exact Swagit source page is the City archive
record for GUID273327; its automated transcript is unedited and is supporting context only. The
September29 City Manager memo says the Task Force planned to meet in early October, but does not
name October3. Retain both limitations in the evidence record.

Change only `config/feeds/dallas-tx-bond-program-meetings.yml`: remove the broad `CBTF` entry
from that feed's `body_exclude` so the P0 policy replay agrees with its existing `*Bond*` /
`*CBTF*` selectors. A live RSS check on October7 found the October3 recording already in Bond
Program, with its original UID/audio, and absent from Public Info and the dedicated Task Force RSS.
The runtime body filter does not apply `body_exclude`, so this correction is expected to change no
published RSS items. In the P0 policy replay, ten retained Dallas CBTF or Subcommittee Chairs
recordings become Bond Program assignments; the eight verified Task Force records keep their
existing dedicated-feed subscriptions. This includes the October3 recording and the September19
and September26 recordings with the same normalized `2024 Capital Bond Program CBTF Meeting`
label. The September26 recording keeps its already approved Public Info placement from #2116;
this contract adds no Public Info placements.

Expected added Bond Program subscriptions from the complete 5,353-record Dallas source are only:

| UID | City provider GUID | Existing feed holders before this change |
|---|---:|---|
| `6135249a278494c5` | 273327 | none |
| `6a3caf1854bdd2cd` | 277589 | Dallas 2024 Community Bond Task Force |
| `84fcc0492e66906d` | 233037 | Dallas 2024 Community Bond Task Force |
| `96c10f4f75706719` | 246971 | Dallas 2024 Community Bond Task Force |
| `aa65e2aafc90711b` | 272574 | none; Public Info is separately approved |
| `aaf0ad8eaabe68a5` | 269278 | Dallas 2024 Community Bond Task Force |
| `baa19208405acb22` | 280220 | Dallas 2024 Community Bond Task Force |
| `cba1e023051d7c94` | 269916 | Dallas 2024 Community Bond Task Force |
| `deb5a67aa9b6e8d1` | 259822 | Dallas 2024 Community Bond Task Force |
| `eeca5aeceda62d39` | 272005 | Dallas 2024 Community Bond Task Force |

Add an unaltered ten-record fixture from the retained Dallas source and focused actual-config
tests. The tests must prove the exact ten Bond Program policy matches, original UID/title/audio
identity, unchanged existing CBTF holders, and no Public Info rule change. Reject near labels and
verify no other Dallas source-record holder changes. Run a full 5,353-record holder replay against
the pre-change policy; only the ten Bond Program policy assignments may be added. Compare with
live RSS and state clearly that no runtime publication change is expected.
Run the full offline suite, whole-repository Ruff and format checks, and `git diff --check`.

Do not change another feed/config, use UID-specific inclusion/exclusion, edit raw records or audio,
or add a Public Info selector. No schema, provider, runtime, stage, storage, worker, workflow,
backfill, winner, or audio-pipeline changes. GUID273327 is already verified in the live Bond Program
RSS only, so its evidence case may close with the recorded user decision and exact UID/audio proof.
Keep GUID272574 open until the merged #2116 deployment verifies its approved Public Info copy.
Update the P0 replay/report and
register with prepared assignments, but do not claim live deployment or close either case here.

### Dallas bond Flood subcommittee reusable ownership — L3, #2089

Official August15,2023 Flood Control/Storm Drainage Subcommittee minutes independently
convene chair Anita Childress6:05pm and adjourn8:15pm. Original provider chapters bind
GUID269612/UID8280ec97799c999e to that date and institution. This is a standalone2024Bond
subcommittee; Council recommendations discussed are not Council participation.

Add `config/feeds/dallas-tx-2024-bond-flood-subcommittee.yml`, same Dallas/Swagit113 source,
existing body_exact only `2024 Bond Task Force Flood`; title Dallas2024Bond Flood Control and
Storm Drainage Subcommittee. No existing selector changes. Add original fixture
`tests/fixtures/dallas-bond-flood-retained.json` and `tests/test_dallas_bond_flood_ownership.py`:
actual-config new feed accepts original, same-label/future-GUID recurrence auto-admits,
standing Council/Flood topic/other bond bodies reject; raw UID/audio/RSS remain unchanged.
Full5353-record/all-feed replay must add only original UID to newfeed0→1, every old holder
unchanged. Full offline and whole Ruff/format required. No provider/runtime/schema/record/audio/
stage/dependency changes, backfill or publication winner. Update ARCHITECTURE/CHANGELOG/
ROADMAP/review11/audit and case prepared action; case stays open until human merge and live
exact UID/audio verification. Freeze/stamp after human merge only.

#2087 bounded equivalent-site clarification: build_search_index must check its deadline only
after completed-source cache hits. Cheap cached-source bookkeeping must not consume restartable
work admission. Moving the existing stop check after cache-hit continue is authorized alongside
a real multiple-budget source-resumption test, with the same deadline and source hash checks.

#2087 checkpoint fix prepared:16 targeted tests and4978 full offline tests pass15 deselected;
whole Ruff/format471 clean. Real three-source/three-budget replay advances one uncached source
each run; cross-run complete→deferred→complete retains last-complete publication and fresh raw
pages. CodeRabbit material-fix review and human merge remain required.

### Frozen implementation stamps — 2026-10-06

Implemented in PR #2091 (#2089 Flood), PR #2093 (#2092 CBTF additions),
PR #2095 (#2094 Critical Facilities), and PR #2087 (#2084 bounded search/checkpoints).
These bounded contracts are frozen as implemented; further changes need a new contract.
#2091/#2093/#2095 retain substantive reviewed selector/test coverage and fixed prose findings.
#2087 original98cb725b had substantive review; material checkpoint fix9b1027cf and
conflict merge b3400313 merged before substantive new-code review. Do not count skipped
automatic checks as review or request reviews on these closed PRs.

### Dallas Sep26,2023 bond Town Hall and CBTF recording — L3, #2115

The original implementation added the same recording to the Dallas Bond Program and Dallas Public
Info feeds under the maintainer's then-current direction. The City’s 2024 Bond Program guide
schedules Town Hall 2 for September 26, 2023, at City Hall to receive public comments. The retained
source record has UID `aa65e2aafc90711b`, GUID `272574`, provider body
`2024 Capital Bond Program CBTF Meeting`, and an original source chapter naming September 26. It
retains one original Swagit source and hosted audio. The provider-generated transcript indicates the
Task Force called the meeting to order and then heard public speakers; it is not official minutes.
The player returned 403 during research, so direct listening and a standalone Town Hall
agenda/minutes were unavailable. The official guide confirms Town Hall 2 and its public-comment
purpose, but public comment alone does not make an event a Public Info session.

The maintainer later superseded the dual-feed direction: GUID 272574 belongs **only** in the Dallas
Bond Program feed. Its existing `*CBTF*` selector already routes the recording. Remove only the
exact GUID 272574 inclusion from Dallas Public Info; do not add or broaden a Public Info title rule.
Keep the September 19 formal CBTF recording outside Public Info. The two records share a provider
body label, so broadening the Public Info selector would incorrectly admit the formal meeting.

### Superseding routing correction — Bond Program only — L3, 2026-10-07

Before changing config or tests, this approved contract records the newer decision. Remove only the
`provider_guid: '272574'` row from `config/feeds/dallas-tx-public-info-meetings.yml`. Keep the Bond
Program selector, all other Public Info selectors, UID, official title/date, source namespace,
source chapter, and audio unchanged. Update the original-fixture test to prove that the recording
remains in Bond Program and is absent from Public Info and the dedicated Task Force feed. A
different GUID with the same provider body must remain absent from Public Info. The September 19
record and every other Dallas feed holder must remain unchanged.

Replay all 5,353 retained Dallas records. The expected result is exactly one removed Public Info
assignment for UID `aa65e2aafc90711b`; its Bond Program assignment remains. No other holder changes.
In the catalog-wide report, the added-placement count decreases by one, the unmatched-record count
stays 411, and the 680 historical title-pattern dispositions do not change. Update the P0 report
and evidence register to describe the Bond-only decision. Keep the case open until the correction
deploys and live RSS verifies the original UID/audio in Bond Program and its absence from Public
Info and the dedicated Task Force feed. No source refresh, title edit, audio change, or backfill.

### #2097 verified May25 CBTF source binding — L3

Before code: add only GUID233037/body from retained record84fcc0492e66906d to existing
config/feeds/dallas-tx-2024-community-bond-task-force.yml body_includes. Original source chapter
May25 plus independent official minutes attachment79 establish exact proceeding binding.
Add original fixture tests/fixtures/dallas-cbtf-may25-retained.json and test in
tests/test_dallas_cbtf_ownership.py: UID/audio/holder preservation, samebody wrongGUID
negative, other unverified shared labels remain excluded. Full5353-record ownership replay must add
only this UID to CBTF; all other holders unchanged. Full offline suite, whole Ruff/format.
Update register prepared assignment/next action, keep open until deployed. Update review11,
ROADMAP and CHANGELOG. Do not modify source records, runtime code, schemas, provider,
stages, dependencies, search, storage, audio, titles/dates or any other feed.

#2097 compatibility clarification before test edits: the existing
`test_three_verified_cbtf_proceedings_preserve_identity` negative for233037 is superseded
by the newly verified May25 admission. Replace that obsolete negative with the same body
and a different unverified GUID. The272574 negative is superseded by the separately approved
September26 dual-feed contract above; no broad label admission is introduced by that exception.

#2097 prepared: full5353-record/1095-label replay changes only CBTF8→9 with original
UID84fcc0492e66906d; every other holder set unchanged.20 targeted and4984 full offline
tests pass15 deselected; whole Ruff/format472 clean. Original UID/audio preserved.
Deployment still required; two independently proven deployed Streets cases close.

### Fort Worth Gas Drilling Task Force joint participant — L3, #2098

User direction to process all remaining gaps authorizes this independently proven participant
coverage under the existing same-UID joint policy. Native official players for clip125/view9
and clip127/view12 independently show the opening slate: Fort Worth City Council, 2008 Gas
Drilling Task Force, Joint Environmental Workshop, Tuesday October14,2008, Will Rogers
Memorial Center, Amon G. Carter Exhibit Hall Stagecoach Room. Independent October7 Council
minutes announce that same joint event and place. Both retain distinct UIDs and publication
dates; neither identical media references nor equal lengths authorize a duplicate winner.

Files: new config/feeds/fort-worth-tx-gas-drilling-task-force.yml, original retained fixture
tests/fixtures/fortworth-gas-joint-retained.json, tests/test_fortworth_gas_joint_ownership.py,
review11, this doc, CHANGELOG, ROADMAP, existing evidence audit/register. No ARCHITECTURE change.
Reuse the five current Fort Worth Granicus source views and body_exact TWO complete aliases:
City Council - Gas Drilling Task Force Workshop City Council - Gas Drilling Task Force Workshop;
City Council - Gas Drilling Task Force Workshop of October 14 City Council - Gas Drilling Task
Force Workshop of October 14. Future GUIDs with these exact institutional labels auto-admit.
No broad Gas, Workshop or topic substring; unproved153/155/180 do not establish Task Force
participation. New title Fort Worth: Gas Drilling Task Force; current author/email conventions.

Keep Council and all existing selectors unchanged. Preserve original UID27254aa1a700569f/125
and0d5832dbd08e6dd8/127, official stored title/date, source namespaces and hosted audio. Copy
these complete records unchanged into fixture. Actual-config tests prove both participant
holders, RSS original UID/audio, future-GUID alias positives, near-label negatives including
153/155/180, unchanged source records and matching source feed URLs. Replay all5,896 retained
source rows: add only these two UIDs to new feed; every prior holder unchanged. Whole offline
suite and whole Ruff/format/diff required. Cases stay open/prepared until deployment evidence.

Durably record6334 discrepancy: both current live views show Budget Work Session, Aug11,2026,
2:12:32; two retained combined-label UIDs show10740s and no audio/sources. Existing budget-only
UIDs have decoded7952.06s/audio. Packet combines three proceedings but does not establish
original combined recording content. Both6334 cases stay open; no refresh/split/winner/holder
change. Evidence screenshot paths and exact observed content are retained in the audit.

Maintainer disposition (2026-10-06): preserve both UIDs' current feed assignments and keep their
cases open until exact UID-to-segment evidence is found. The agenda PDF alone does not establish
which recording UID belongs to which proceeding; do not split audio or change a winner/holder.

Do not modify runtime/provider/schema/storage/stages/workers/workflows/dependencies, source
records, raw titles/dates/chapters/audio or other configs. No merge, split, winner, invalidation,
backfill or production write. Actual test-code review obeys repository-wide65minute spacing.
Freeze/stamp only after human merge; deployment proof is separately required for closure.


Prepared #2098: exact participant feed and retained-record regression test implemented;
125/127 cases remain open until deployed own RSS and original audio verified.

#2098 validation:20 targeted tests,4985 offline tests pass15 deselected; whole Ruff/format473
clean. Full5896-record/979-label replay adds only the two original UIDs to the new feed0→2;
every previous holder is unchanged. No case closure before deployed proof.

## Frozen implementation checkpoint — 2026-10-06

Implemented in PR #2099 through parent #2096 and PR #2100. Their bounded contracts
are frozen. #2100 was human-merged before substantive CodeRabbit coverage; this remains
an explicit review gap. Seven verified deployed RSS/audio assignments are documented in
[deployment proof](deployed-ownership-proof-2026-10-06.md); search and content obligations remain.

### Four Dallas Streets dual-feed placements — L3 (issue #2170, 2026-10-08)

The maintainer explicitly authorized restoring these four recordings to both their existing
bond Streets subcommittee feed and the standing Transportation and Infrastructure feed.
Add only GUID269407/270975/277827/277830 with their exact retained body labels to the latter
feed's existing `source.body_includes`. These are approved historical exceptions, not evidence
that future bond meetings automatically belong to the standing Council committee.

Allowed files: that feed YAML, the four existing register entries, this doc, review/11,
CHANGELOG, `tests/test_dallas_bond_streets_ownership.py`, and a dated evidence report.
Update that test’s exclusive standing-feed assertions for the four approved GUIDs only;
retain future-GUID negative coverage and assert both RSS feeds keep original audio. Keep cases open and separate missing independent
official-recording proof from publication authorization. Preserve all raw records, UIDs,
audio, existing feed selectors and dedicated bond placements. No runtime, workflow, schema,
source refresh, duplicate selection, or production writes.

Acceptance: replay all 5,353 frozen Dallas records; exactly four standing-feed additions and
no other placement changes. Existing Dallas ownership tests, whole Ruff lint/format and the
offline suite must pass. After human merge/deployment, verify both live feeds retain the four
original audio URLs. Publication preparation does not close the evidence cases.

Prepared implementation replay: all 5,353 frozen Dallas entries produce exactly the four
authorized standing Transportation additions and no removals or other changes. The existing
broader Bond Program placements also remain. See
[`dallas-streets-dual-feed-replay-2026-10-08.json`](evidence/dallas-streets-dual-feed-replay-2026-10-08.json).
All four independent evidence cases remain open; current assignments are prepared config
assignments, not a claim of live deployment.

Validation: 12 targeted tests and 5,120 offline tests passed (15 deselected); whole
Ruff lint and format checks passed across 480 files. No pipeline version or audio backfill.

### Search investigation — historical proposal (2026-10-08)

The latest successful published artifact still has zero advertised search shards and no
manifest/page. A synthetic replay reproduces lost progress when interrupted inside one source;
three bounded runs restart the same records. The actual production blocking source is not
identified. Recommended next contract: private per-record progress, policy/hash invalidation,
atomic public publication and explicit deferred diagnostics, retaining the20minute limit.
The later L3 contract below was approved and implemented in #2174. Original evidence:
[`search-index-investigation-2026-10-08.md`](evidence/search-index-investigation-2026-10-08.md).

PR #2171 merged as7134a2c3 with passing CI. Its four-record dual-feed contract is frozen as
implemented in #2171; deployment37733085421 is running and live verification remains pending.
CodeRabbit automatic review was skipped; the ownership-test change merged without substantive
CodeRabbit coverage. No review request will be sent to the closed PR. Six evidence cases stay open.

The refreshed frozen-source baseline is recorded in
[`p0-current-catalog-replay-2026-10-08.md`](evidence/p0-current-catalog-replay-2026-10-08.md);
its counts describe main before #2171 and must not be relabeled as including those four additions.

### Within-source search resume — frozen, implemented in PR #2174 (#2173, 2026-10-08)

Maintainer approved the per-record progress fix and clearer reporting. Named files:
citypods/search.py, tests/test_search.py, tests/test_static_search_job.py, ARCHITECTURE.md,
CHANGELOG.md, review/11 and this section. Keep workflow/cadence/config20minute ceiling unchanged.
No provider/dependency/raw-record/audio/storage-write/public search schema changes.

Private `.search-cache.json` adds partial source entries keyed by source with version/hash and
processed UID-to-document mappings (null means processed with no public document). Existing
_shard_hash binds complete record inputs, candidate config, archive and publication selection.
Reuse only matching version/hash partials; discard stale partials. Save mutable partial state
on normal stop through the wrapper's existing working checkpoint persistence. Complete sources
remove their partial entry. Public manifest/page publish only after all sources finish.
Report interrupted source, processed/total records and completed/total sources in returned
summary; distinguish no prior index from retained complete index. Keep complete return string
compatibility. Cheap cache hits/bookkeeping precede stop admission.

Tests: repeated bounded runs finish one large source without reconverting completed records;
changed source/config/archive policies invalidate partials; a stopped build with no prior index
reports that accurately; prior complete bytes survive deferral/errors. Whole Ruff and offline
suite plus current-head substantive review before human merge. No pipeline bump/backfill.

L3 refinement before final checkpoint implementation: store each processed UID's input hash
and document (null for excluded/no document). Partial source hash covers config/base URL/
archive/publication policy with empty record inputs; per-record hashes cover exact durable
record inputs. New/changed recordings therefore reprocess only affected records, while
policy changes invalidate the entire partial source. Drop removed/nonpublic cached UIDs.
This prevents routine additions during the four-hour cadence from discarding all progress.

Implemented working-cache progress uses per-record hashes to preserve unchanged converted
records across routine input additions. Old complete source caches remain usable; no search
output schema/version change or audio/stage invalidation. Targeted search/job tests pass39.
The deploy workflow runs every four hours, on relevant main pushes, or manual dispatch, with
single-deployment concurrency. The20minute ceiling is an approved operational bound, not a
measured optimum. Keep it and cadence unchanged until progress summaries establish actual
completion/deferral rates; do not increase the limit to hide restart behavior.

Validation:39 targeted and5,123 offline tests passed (15 deselected); whole Ruff/format
passed480 files. Runtime review and deployed repeated-run completion remain required.

PR #2174 merged2026-10-08T05:56:44Z as1a727b28 with current CI/CodeQL success.
It merged before a substantive CodeRabbit review; automatic review was skipped. Record this
coverage gap explicitly; do not request review on the closed PR. Its pending deployment
37734949438 was canceled by newer queued main runs; production verification is still pending.
A manual current-main deployment is requested to exercise the fix. No workflow cadence,
time-limit, source record or runtime edits accompany this conflict resolution.

### Search two-hour budget and progress visibility — L3, 2026-10-08

Maintainer explicitly approves increasing search work from 20 to 120 minutes and manually
starting the workflow after validating checkpoint resume. This supersedes the frozen 20-minute
ceiling in earlier contracts; the four-hour schedule and single-deployment concurrency stay.
Change only citypods/search.py budget default/validation, config/site_config.yml search budget,
.github/workflows/deploy.yml search job timeout (150 minutes, including setup and persistence)
and search command output via tee so record/source progress is visible in logs and summary.
Before indexing, report restored completed-source and partial-record counts from the private
working cache. Use explicit Bash so the tee pipeline preserves indexer failure status.
Update tests/test_static_search_job.py ceiling coverage and two-hour deadline acceptance; run
actual resume regressions, full offline suite and whole Ruff/format. Update review/11 and changelog.
No provider, audio, source identity, storage permission, index schema or publication-policy change.
Existing production evidence: run37744496856 restored37741127403 checkpoint and saved
211027488 bytes versus180143080 previously. Size growth supports retained work, but is not a
meeting completion count. Existing actual multi-budget regression must prove completed records
are reused without repeating sidecar reads; subsequent workflow logs expose exact deferred counts.
Manual branch dispatch is authorized; human merges remain required for main/scheduled adoption.

Validation: actual source/record resume regressions pass; 40 targeted search tests and
5,158 full offline tests pass (16 deselected). Whole Ruff and format checks pass. Workflow
startup diagnostics read only completed-source and partial-record counts from the private cache.

### Retain meeting documents after search source completion — L3, 2026-10-08

Maintainer approved retaining reusable meeting documents after completed sources change.
In citypods/search.py build_search_index, preserve the existing private partials entry on both
source completion and unchanged shard cache hits. Existing per-UID hashes reject changed or
removed records, and source-view/archive-policy hashes retain their existing invalidation rules.
No new cache schema: partials now also holds reusable records for completed sources. Old caches
without these records remain valid but require one rebuild when a completed source changes.
Tests in tests/test_search.py must restore JSON between builds, change one completed record,
add/remove records, and prove only changed/new records are converted, with clean-build-equivalent
public output. Existing policy invalidation and staged-publication tests must still pass.
Update ARCHITECTURE.md and CHANGELOG.md; run targeted tests, full offline suite and whole Ruff.
Do not modify workflow cadence/budget, dependencies, public schema, storage, audio, raw records,
publication selection, feed definitions or provider adapters. Human merges remain required.

Validation: 41 targeted search/job tests and 5,285 offline tests pass (16 deselected). Whole
Ruff/format checks pass (487 files); incremental output matches fresh-build output after
completed-source cache restoration, meeting edits, additions and removals.

### Search browser mixed-content correction — L3, 2026-10-09

Live search assets are published but the rendered HTTPS page requests its manifest and vendored
engine over HTTP. Browsers block the manifest fetch and display unavailable notices. Maintainer
reported the broken interface; correct this within the existing search rendering path.
In citypods/site.py render_search_page, emit path-only asset URLs preserving the configured
base-path prefix. In templates/search.html.j2 upgrade only same-host HTTP shard URLs to HTTPS
when the page itself is HTTPS; old completed manifests must work without reindexing. Leave
other-host URLs and HTTP local development unchanged. Test rendering for HTTP configured bases
and subpaths in tests/test_site.py; verify the URL normalization and browser filter initialization.
Run full offline suite and whole Ruff/format. Update architecture/changelog and review/11.
Do not modify site base configuration, search index schema/cache/hash, source records, audio,
workflow schedule/budget, feed definitions or dependencies. Human merges remain required.

Browser verification: corrected local page with a real published Austin source loaded city/body
options, displayed 2/3 transcript coverage and returned three meeting results for disabilities.
JavaScript regression verifies same-host HTTP-to-HTTPS upgrade while leaving other hosts and
HTTP localhost untouched. Live production requires human merge and deployment of this fix.

Validation: 60 targeted tests pass; whole Ruff/format passes (491 files). Full offline suite
reports 5,380 passed, 16 deselected and one unrelated existing failure in
test_prelabeler_sizing_ignores_paused_routes. It expects the NVIDIA Gemma route to be paused,
but current configuration does not pause it; isolated reproduction also fails. Its test and
citypods/tags.py / citypods/compute/llm_policy.py are unchanged from the main base.

P2a implementation issue: [#2211](https://github.com/BashfulBits/city-meeting-podcasts/issues/2211).


### P2a implemented (2026-10-09, PR #2213, issue #2211)

The typed policy foundation is implemented for review in `citypods/remedy_policy.py`: strict
reviewed declarations/templates, explicit city/source proof binding, immutable source-local
indexes and deterministic ownership results. The existing remedy guards share this resolver
and preserve legacy exact identities, intentional joint subscriptions and conservative TIF
recognition. `config/remedy.yml` contains only an empty template list: no template is activated.
Instantiation validates supplied evidence and references without loading gold, models or websites.
Synthetic transfer examples exercise the contract; they are not independent admission truth.
P2b/P2c replay/ledger and persistent excluded/watch dispositions remain future scoped work, as
do model admission and P3–P5. Implemented in PR #2213, merged 2026-10-09 at main 5005879b.
CodeRabbit reviewed 785a9aa0 and its one valid inclusion-parser finding was fixed in a9ccd673,
replied to and resolved. The final parser fix merged without a new substantive review; record
that limited review gap. Final head CI/preview/dependencies/CodeQL passed; 5,450 offline tests
and 233 targeted tests passed. No templates activated.

Prepared-slice validation: 328 targeted policy/config/evaluation/guard tests and 5,438 full
offline tests passed (16 deselected); whole-repository Ruff/format checked 493 files.

P2a preview correction: dependency-free declaration validation remains in citypods/config.py;
strict template/evidence models reuse it from citypods/remedy_policy.py. Base render installations
do not require the optional Pydantic evaluation dependency. A subprocess regression blocks
Pydantic imports while loading every production feed. No dependency or workflow changes.

P2a review hardening: supplied source recording inclusions use the existing shared parser,
including for hand-built City inputs. Malformed mappings, missing values and duplicate GUIDs
remain unresolved rather than raising an unexpected exception. Valid inclusion proof and
legacy configuration behavior are unchanged; no feed or source-record changes.


### P2b complete coverage report — frozen, implemented in PR #2215

Predecessor: P2a shipped in #2213. This deterministic slice is report-only; no live models,
feed edits, accepted-disposition ledger, automatic notifications or provider/storage changes.
Daily unexpected-body alert behavior stays unchanged until the persistent disposition slice.
This sequencing avoids reopening P0's deliberately unassigned history before decisions can be
recognized durably. The maintainer approved this report schema and staged activation in chat
on 2026-10-09 with “I approve.” Implementation remains report-only as specified below.

Permitted files: citypods/remedy_policy.py, citypods/audit.py, scripts/audit_feeds.py,
tests/test_remedy_policy.py, tests/test_audit.py, existing audit CLI tests and lifecycle docs.
Do not modify audit_remedy.py's evidence-v1 writer, remedy apply scripts/workflows, storage,
provider adapters, feed definitions or models in this slice. Evidence-v2/apply freshness gates
remain a subsequent separately specified integration, not silently fed to the legacy reader.

- material_evidence_hash(evidence) hashes canonical input-only evidence. Unordered references,
  observations and owner lists sort deterministically; official strings remain unchanged.
  Timestamps of report generation are outside the material hash. New recordings may change
  coverage without representing new official identity evidence. No implicit evaluation/gold IO.
- replay_coverage(recordings, feeds, policies) returns immutable CoverageReplay rows and totals.
  Supplied observations carry source_key, existing uid (nullable), provider_guid, verbatim
  body/title/date and observation_refs. Preserve source namespaces. Prefer a persisted UID;
  otherwise match only exact adapter GUID within the same source. Never strip URL/view/GUID
  components or invent UIDs. Distinct unproven observations remain separate and identified
  as uniqueness unknown. Contradictory observations for the same UID retain both references
  and an explicit identity-conflict diagnostic rather than last-write-wins.
- Each CoverageRow exposes source_key, uid, provider_guid, observed label/metadata, references,
  configured_owner_slugs, verified_owner_slugs, holding_owner_slugs, policy IDs/statuses,
  evidence refs and diagnostics. Status is selected, unknown or ambiguous; configured selector
  matches never become official proof. Separate selected counts from verified-policy counts.
  Differences between configured and verified owners are visible diagnostics, not automatic
  false-inclusion judgments: legitimate city aggregates and multi-feed subscriptions are retained.
- collect_body_coverage(episodes, records, *, related_cities, policies=None, dispositions=None)
  in audit.py converts both fetched and persisted observations into the same replay input.
  Records-only history is included; an archived label is never treated as resolved. This
  slice accepts no dispositions other than None: do not design or guess the ledger schema.
  Retain empty/malformed labels with diagnostics rather than silently dropping recordings.
- scripts/audit_feeds.py adds opt-in --body-coverage-report PATH. It writes a deterministic
  source-scoped JSON report with schema_version=1, observed_at UTC, config_hash, policy_hash,
  catalog_hash, material_hash, sources and totals. Each source records source_key, city,
  completeness=unknown, observations, rows and diagnostics. Completeness is never inferred
  from a successful fetch or counts. Record unavailable sources as incomplete diagnostics.
  Hashes cover exact source selectors/policy declarations and supplied recording metadata;
  generation time is excluded. No sidecar/audio reads or extra provider fetches. Use the
  existing audit fetch context; include no-finding sources, not just unexpected rows.
  Report-only mode must avoid timeline/state mutation and GitHub writes.
  Add optional body_coverage_evidence collector and body_coverage_only=False arguments to
  audit_all/audit_city in audit.py; collect all source observations once and short-circuit
  other diagnostic/mutation work in coverage-only mode. CLI --body-coverage-report chooses
  that report-only mode, skips GitHub reconciliation and state push, and does not combine
  with mutation flags. Existing defaults remain unchanged. Retired/unavailable sources
  retain cached observations and explicit non-fetched diagnostics; no forced provider poll.

Tests: persisted-only unmatched labels; broad-selector matched rows still reported; intentional
joints/aggregates retained; source isolation; exact UID/GUID merge vs unproven duplicate views;
contradictory observations; unknown labels; repeatable hashes; report-only no state/GitHub writes;
missing sources/completeness unknown; all existing unexpected-body findings unchanged.
Run full offline suite and whole Ruff/format. Ship one bounded code PR; keep ledger/exclusion
persistence and alert activation gated. No production model or automatic feed change.

P2b implementation issue: [#2216](https://github.com/BashfulBits/city-meeting-podcasts/issues/2216).

### P2b implementation shipped in PR #2215 (issue #2216)

The opt-in `scripts/audit_feeds.py --body-coverage-report PATH` command reports every supplied
source observation, including retained history absent from the current provider response. It
keeps configured matches distinct from verified policy ownership and preserves conflicting
metadata as separate rows with identity diagnostics. Exact source-local GUIDs can attach fresh
observations to retained UIDs; different view/GUID strings are never collapsed by inference.

This command skips canonical state synchronization, timeline/audio diagnostics, state saves,
GitHub reconciliation and notifications. Its only write is the requested JSON report. Active
sources use one existing provider fetch per source; retired sources keep cached observations
without polling. Every source's completeness remains unknown. Daily alert behavior is unchanged.
Persistent accepted/excluded/watch dispositions and alert activation remain subsequent slices.

Prepared validation: 5,463 offline tests passed (16 deselected), followed by the direct joint/
aggregate report regression in the final 248-test targeted policy/audit/CLI run. Whole-repository
Ruff and format pass for 493 files; diff whitespace checks pass. No feeds, records or stage
versions changed. No live provider/model calls were made for verification.

P2b review round on ad4ea48a identified two valid minor findings: provider errors could expose
signed URL query values, and GUID-only observations shared by multiple retained UIDs lacked
an uncertainty diagnostic. The fix redacts both report and ordinary unreachable output and
keeps each unproven shared-GUID observation separate with uniqueness-unknown. Retained UIDs
remain distinct. Regression coverage includes both output paths and two fresh observations.

### P2b parent-branch integration (2026-10-09)

The maintainer merged #2217 into parent design PR #2215 at 23:08:52 UTC (5e1e3f2e).
This is not yet a main-branch shipment. Final code 63a24d3e passed 5,466 offline tests
(16 deselected), 250 targeted tests and whole Ruff/format. Both substantive review findings
were fixed, replied to and resolved. The dependency job's Docker Hub timeout passed on retry.
The final code fixes merged into the parent before fresh substantive review; this limited review
gap remains explicit. No review is requested on closed #2217. Parent #2215 now carries code
and requires its own current-head checks before human merge to main.

P2b report provenance clarification (CodeRabbit review of 56d1fc49): existing source diagnostics
must identify the resolved local state directory and explicitly warn that unsynchronized local
state may omit retained history. The CLI also prints this limitation, including for an empty
report. This uses the existing diagnostics field; no schema, synchronization or hash-input
change. The shared resolve_state_dir helper identifies the same directory audit_all reads.

### P2b main shipment and historical closeout (2026-10-10 UTC)

Human merge #2215 at 01:10:34 UTC (80defe32) shipped the report-only slice. Its final head
b8c09630 passed all CI; substantive review covered 56d1fc49, including the prior two fixes.
The final local-snapshot disclosure fix was tested and its thread resolved; it merged before
a further substantive review. That narrow review gap is retained rather than counted as reviewed.
The six historical cases are now resolved by explicit maintainer-approved dispositions, not new
exact-binding proof. All 169 cases are resolved; no raw records or feed assignments changed.
Daily alert activation, persisted excluded/watch feedback and evidence-v2 integration remain
next P2 work. P3–P5 and model admission remain gated.

P2b retained-snapshot post-merge verification is complete; see
[evidence and corrected projection counts](evidence/p2b-postmerge-verification-2026-10-10.md).

### P2c1 decision event and fold foundation — frozen, implemented in PR #2231

Predecessors: P2a #2213 and report-only P2b #2215 are shipped. Historical case closeout and
post-merge verification shipped in #2227. This is the next deterministic slice, not live model
admission or alert activation. It prepares memory for approved assignment/exclusion/watch
outcomes without yet writing it to storage or using it to suppress reports.

Permitted implementation files: new `citypods/remedy_ledger.py`, new
`tests/test_remedy_ledger.py`, lifecycle docs. Existing `remedy_policy.canonical_hash`,
`bodies.body_key` and config approval-reference validation may be imported unchanged.
Do not modify audit/report schemas, feed config, providers, records, storage/routing, workflows,
leases, models, dependencies, evaluation gold or GitHub mutation scripts. No remote IO.

#### Approved schema

Strict immutable `RemedyEvent`, schema_version integer 1, rejects unknown keys and coercions:

- `city`, `source_key`, `policy_id`, `normalized_label`: explicit nonempty source-local identity.
  `normalized_label` uses existing `body_key`; retain verbatim official text in the evidence,
  not as rewritten official metadata. Decision ID is SHA256 of these four canonical fields.
- `decision_id`, `event_id`: full lowercase SHA256 strings. Validate supplied IDs against
  recomputation; helper constructors compute IDs without inventing recording identity.
- `parent_event_ids`: sorted unique event-ID tuple; no self-parent. Empty only for an initial
  event. Parent hashes must refer to this same decision/source.
- `occurred_at`: aware UTC ISO timestamp of the original action/delivery, not retry time.
- `actor_kind`: maintainer, automation or system; `actor_id`: nonempty trusted caller identity.
  These are provenance assertions, not authentication: trusted entrypoint validation follows
  in later slices. The pure fold must never treat a claimed actor string as authorization.
- `state`: the existing seven states covered, excluded, proposed, in_review,
  refinement_needed, blocked, resolved. No new watch state.
- `evidence_hash`, `config_hash`, `policy_hash`: full SHA256 strings, supplied by callers.
  New recording coverage does not itself authorize changing a terminal policy decision.
- `recording_refs`: tuple of strict `{source_key, uid, provider_guid}` entries. UID and GUID
  may be null individually, but at least one must be present. Source must match this event;
  no stripping GUIDs, deducing cross-view identity or generating UIDs.
- `provenance`: tuple of strict `{role, route_id, model_family, reasoning_level, report_hash}`
  entries for actual reviewed calls; all strings nonempty, report_hash SHA256. Empty is allowed
  for deterministic/human-only decisions. No prompts, raw model responses or credentials.
- `rationale`: nonempty bounded text (at most 4,000 characters), treated as untrusted data.
- `artifact_ref`, `approval_ref`, `external_delivery_id`: nullable nonempty strings; artifact
  and approval references use existing approval-reference shape checks. No URL fetching.
- `disposition`: null or one of assigned, excluded, watch, not_pursued, rejected.
  Disposition is separate from workflow state. A watch decision uses blocked state with watch
  disposition and a rationale describing missing evidence/recheck conditions. It never silently
  becomes excluded or gains feed eligibility after an arbitrary count threshold.

Assigned/covered requires an approval reference, matching nonempty recording refs and supplied
replay hash evidence; this validates declarations, not that replay or approval actually happened.
Excluded/not_pursued terminal outcomes require approval_ref and rationale. Watch requires
rationale and approval_ref, but is not proof of ownership. Rejected proposals use
refinement_needed and rejected disposition; they remain visible. Later entrypoints authenticate
approval and enforce fresh evidence before suppression or publication.

#### Pure functions and conservative reconstruction

`decision_id(city, source_key, policy_id, label)` hashes explicit identity using body_key.
`event_id(payload)` hashes canonical event content excluding its event_id; sort unordered parent,
recording and provenance tuples. Include original occurred_at and delivery ID. No report clock.
`parse_event(payload)` returns the validated immutable event; raises contextual ValueError.
`fold_events(events)` returns immutable DecisionState with decision/source identity, current
state/disposition or blocked, retained events/tip IDs and diagnostics. Empty input returns an
empty blocked result. Multiple decision identities in one input raise ValueError.

Fold causal parent links, not timestamp ordering or last-write-wins. Shuffled inputs produce the
same result. Identical event IDs deduplicate. Repeated same source/actor/external_delivery_id
fold once when semantic content (all fields except event_id and occurred_at) agrees; alias all
retry IDs for parent resolution. Conflicting payloads under the same delivery ID block. Do not
use processing timestamps to pick a winner. Missing parents, cycles, multiple independent roots,
multiple unreconciled tips or contradictory identities block with explicit diagnostics.
An explicit reconciliation event can name all conflicting parents, but this pure slice does not
validate actor permissions or authorize its production use. Preserve all input events.

A covered/excluded/resolved terminal decision may acquire new recording evidence while retaining
its disposition. The pure fold does not infer reopening from counts, dates, changed model route,
clock age or evidence_hash alone. A later explicit trusted reopen action must be represented and
validated in the entrypoint slice before it can drive automated work. Do not invent reopening
semantics here or hide a rejected proposal by dropping its events.

#### Acceptance tests and subsequent PRs

Test strict schema/IDs, source isolation, null refs, optional deterministic provenance, unknown
fields, malformed approvals, retained raw strings, stable unordered hashes, causal replay,
missing parents/cycles/conflicting tips, repeated delivery with timestamp differences,
conflicting delivery content and preserved rejection/watch outcomes. Fake fixtures only;
no production decision import, P0 migration, storage, credentials or network.
Run targeted tests, whole Ruff/format and full offline suite. Document prepared implementation;
freeze only this slice after human merge. Next P2c2 specifies immutable event storage and trusted
lease/coordinator integration. P2c3 wires current evidence/dispositions to audit/remedy in
report-only mode, including v1 refresh and suppression/reopening rules. Alert activation follows
verified report behavior. Neither later slice is implicitly enabled by P2c1.

Maintainer approved this schema in chat on 2026-10-10. A keep-watching decision remains
pending for the same city/source/body case. Newly related recordings join that disposition;
an unchanged weekly scan must not request another decision. Only new related meetings or
material related evidence warrants renewed review. Similarity must use reviewed identity rules,
not a model guess, cross-source UID inference or count threshold. P2c3 implements that behavior;
P2c1 records the pending outcome without suppressing any current scan.

P2c1 implementation prepared under #2230: new pure ledger module and fake-fixture tests.
No storage or scan entrypoint is connected. `evidence_hash` is the supplied replay evidence hash
for assigned/covered declarations; validation cannot prove that the cited replay actually ran.
The approved keep-watching semantics are a later integration acceptance requirement, not an
assertion that weekly suppression is active in this foundation PR.

P2c1 verification: 5,618 offline tests passed (16 deselected), then 38 final ledger checks
passed including additional cycle/terminal tests; whole Ruff/format passed across 497 files.
No current production behavior changed. Human merge remains required.

CodeRabbit review on #2231 identified one valid approval-gate gap: excluded state with null
disposition still requires approval_ref. The fix validates both state and disposition, preserves
nullable resolved outcomes, and adds a regression test. No scan or feed entrypoint is enabled.

Review-fix verification: 39 targeted tests and 5,623 offline tests passed (16 deselected);
whole Ruff/format passed across 497 files.


P2c1 shipped in PR #2231, merged 2026-10-09 at 9:08 PM America/Chicago
(2026-10-10T02:08:58Z), merge b0fcf4b0. The current-head tests, dependencies, preview and
CodeQL passed. CodeRabbit reviewed 99f5951d; its one valid minor approval-gate finding was fixed
in b9ca5053, regression tested, replied and resolved; the bot confirmed the fix. The push's skipped
automatic review is not a separate substantive review. This stamp freezes P2c1 only. P2c remains
incomplete: persistence, trusted mutations and scan integration are not active.

### P2c2a source-scoped event persistence — frozen, implemented in PR #2237

The event path remains the previously approved B2 path:
`state/remedy/events/<source_key>/<decision_id>/<event_id>.json`. Do not request approval of
that path again. P2c1 validates content but has no IO. Implement storage helpers before any
production caller is connected. This proposal does not activate a workflow or import historical
human decisions automatically.

Permitted files: `citypods/remedy_ledger.py`, `tests/test_remedy_ledger.py`, lifecycle docs.
Use existing storage `get_file`, `put_file`, `list_objects` and the existing maintenance lease
`assert_held`; no storage adapter, routing, coordinator, audit, provider, workflow or feed changes.
The event namespace already routes to B2 by default. The approved coordinator remains separate
future work on R2; do not move authoritative events to R2 to gain conditional writes.

Approved interfaces:

- `event_key(event) -> str`: validates the complete event and constructs only the approved path.
  Source must be a safe single segment matching `[a-z0-9][a-z0-9_-]*`; decision/event IDs already
  require lowercase SHA256. Reject traversal rather than sanitize or rewrite identity.
- `append_event(storage, event, *, lease) -> str`: require a live existing remedy maintenance
  lease for every append attempt, verify its exact `maintenance-leases/remedy.json` key and
  `assert_held()` immediately before a write. Validate the event before any backend call.
  Read an existing object first; if its validated canonical content equals the supplied event,
  return the existing key without another upload. Invalid/different existing content fails closed
  and is never overwritten. If absent, write canonical JSON from a temporary file via put_file,
  then read back and validate the identical event. Temporary files are cleaned on all paths.
  Return the storage key, not a public URL. No delete operation. No-CAS or missing/lost lease
  prevents writes; read-only reconstruction remains possible. Backend read/write errors propagate
  as failures, never as absence or a successful append. Retry after a successful-but-unacknowledged
  upload reads the existing content and succeeds idempotently.
- `load_decisions(storage, *, source_keys) -> dict[str, DecisionState]`: enumerate only each
  explicitly named safe source prefix, never a global state/media scan. Validate every listed
  path against its event source and recomputed IDs. Reject malformed or outside-prefix objects,
  duplicate JSON object keys, invalid events and reads that disappear/fail. Group by decision ID
  and use the shipped fold_events; conflicting histories retain its blocked diagnostics.
  Empty requested sources yield an empty dict. Return deterministically ordered decision IDs.
  No writes, derived cache, remote official-evidence fetch or use of processing timestamps.

Persistence helpers do not authenticate an event's actor/approval fields. A held lease serializes
mutations; it does not prove a human approved the disposition. Production trusted-entrypoint
validation, coordinator recovery and GitHub reconciliation require a subsequent separate L3
contract before these helpers are called by a scheduled workflow. No current scan suppression.

B2 does not enforce conditional PUT in this repository. Immutability therefore relies on validated
content-addressed event keys plus the shared maintenance lease, existing-object verification and
post-write verification; never claim server-side B2 CAS. Identical content-addressed concurrent
retries are harmless. Do not reuse a mutable key or overwrite corrupt objects to recover them.

Tests with fake storage and fake leases only: exact source path; traversal; invalid event before IO;
append/read-back; repeated append without second upload; corrupt/mismatched existing content;
wrong, missing, lost and no-CAS lease; read/write/read-back failure; reconstruction of unordered
objects and repeated deliveries; source isolation; outside-prefix/path-content mismatch; duplicate
JSON keys; absent requested source; conflicts remain blocked; temporary-file cleanup. Whole
Ruff/format and full offline suite before push. No production storage writes in validation.

Maintainer approved permanent decision history, lease-required writes and strict reconstruction
failure behavior in chat on 2026-10-09 America/Chicago. The source path, pending-watch behavior
and overall P2 sequence remain approved. This L3 contract is committed before implementation.

P2c2a implementation prepared under #2236. The no-CAS check applies to the lease coordination
backend; B2 event storage intentionally remains non-CAS. Event reads are source-scoped and all
backend failures propagate. Validation uses fake storage/leases only; production remains disconnected.

P2c2a verification: 65 targeted ledger/persistence checks and 5,649 offline tests passed
(16 deselected); whole Ruff/format passed across 497 files. Human merge and code review remain
required; no deployment or production storage verification is claimed.


P2c2a shipped in PR #2237, merged 2026-10-10T04:23:55Z, merge 2763036e.
Final head b92f7d55 passed tests, dependencies, preview and CodeQL. Its ledger code/tests were
unchanged from tested 9a008308 (5,649 offline tests, 16 deselected; 65 targeted checks).
The maintainer merged before a substantive CodeRabbit review; the rate-limited/skipped status
is not review coverage. This explicit review gap does not imply a production storage check.
Only persistence is frozen here: trusted actions, recovery coordination and scan integration
remain disconnected. The temporary #2237 review monitor is paused; no closed-PR review request.

### P2c2b trusted decision actions and recovery — L3 approved

The maintainer authorized this next step after #2237. A lease alone cannot authenticate a
maintainer, and an approval URL alone cannot show what that person approved. Bind an explicit
structured action to a freshly retrieved GitHub comment and its author's current repository
permission. Recommend a dedicated decision command, separate from the legacy `/remedy` command,
which dispatches model-based remediation and must not be reused for recording decisions.
The maintainer approved the dedicated structured comment command in chat on 2026-10-10.

Recommended action payload: schema_version 1, action (`decide` or `reopen`), decision_id,
expected_parent_event_ids, city, source_key, policy_id, normalized_label, state, disposition,
evidence_hash, config_hash, policy_hash, recording_refs and rationale. These use existing ledger
validation; no second disposition vocabulary. `reopen` records an explicit maintainer action,
never a changed count or a clock. The trusted caller supplies original comment ID/time/author,
repository identity and permission from fresh API responses, not fields claimed by the payload.
The approval reference and external delivery ID derive from that exact comment. Reject actor,
repository, approval-content or parent mismatches. Reject stale/missing evidence before writes.

Proposed implementation files: new `citypods/remedy_actions.py` and
`tests/test_remedy_actions.py`, existing `citypods/remedy_ledger.py` and its tests only for recovery
integration, lifecycle docs. Reuse `github_permissions.require_repository_write` unchanged.
Do not modify existing commands, feeds, records, providers, models, dependencies or workflows.
The first PR exposes callable validation/recovery interfaces; scheduled activation is separate.

Proposed interfaces:

- `validate_decision_action(action, *, comment, permission, repository, evidence, decisions)`:
  validate the exact source/city and current parent tips; authenticate the author against a trusted
  fresh GitHub snapshot; verify the structured comment agrees with the proposed action; construct
  the existing immutable event using the original delivery timestamp. No network or writes here.
- `record_decision_action(storage, action, *, lease, trusted_snapshot, evidence)`:
  reread the relevant source's immutable events under the existing lease, validate against current
  history and fresh evidence, then append using the shipped helper. A retry of an already stored
  comment returns that event without manufacturing a new parent or processing timestamp.
  A conflicting retry or another intervening decision blocks and asks for an explicit refresh.

This step records decisions only; it does not create issues or PRs. Therefore recovery can use
immutable events and stable comment delivery IDs without introducing publication reservations.
The R2 coordinator and intent/completion recovery for future GitHub publication remain separate
P2/P4 work; do not claim that recording a decision implements all publication crash recovery.

Tests: write-permission allowed/denied; wrong author/repository/comment content; unknown fields;
wrong source/city; stale evidence/config; current versus stale parents; explicit reopen;
duplicate delivery after lost upload acknowledgment; damaged history; lost lease; concurrent
intervening decision. Fake API snapshots/storage only, full offline and whole Ruff/format.
No production writes or inferred import of historical chat decisions.

### P2c3a remembered-decision comparison report — L3 callable/report contract

The maintainer authorized report-only comparison before changes to recurring alerts. Recommend
an explicit local decision snapshot input alongside the existing body-coverage report. No live
storage reads or approval import during audit; snapshots must preserve complete event history,
and trust validation remains the previous step's responsibility. An empty snapshot is valid and
must be reported as no saved decisions, not as complete historical coverage.

Proposed files: `citypods/remedy_ledger.py`, `citypods/audit.py`, `scripts/audit_feeds.py`,
`tests/test_remedy_ledger.py`, existing audit/script tests and lifecycle docs. Do not change
`collect_unexpected_bodies`, issue reconciliation, workflows, feeds, records, models or providers.
Keep coverage schema v2 intact; add a separate decision-comparison report, not hidden alert
suppression. The existing `dispositions` argument is not enabled until its exact shape is L3.

Recommended report outcomes:

| Outcome | Meaning and recommendation |
|---|---|
| unchanged | Same pending case and evidence; recommend no repeated question. |
| related_recordings_added | New proven related UID/source pairs; retain the pending case and show additions. |
| material_change | Changed official identity proof, approved policy or identity contradiction; request review. |
| history_blocked | Missing/conflicting parents or unusable history; show the failure, suppress nothing. |
| no_saved_decision | Retain ordinary unmatched review; do not invent an outcome. |

Use exact city/source/policy binding and reviewed body identity rules to group additions.
Never use a shared topic, count threshold, fuzzy title or cross-provider GUID rewrite.
Retain the union of known recording references in the report without writing a new event.
Report clock, model route, ordering and additional dates alone are not material evidence.
A new meeting under an approved assignment/exclusion rule changes coverage without reopening
that rule; an explicit contradiction stays visible even if a broad selector matches it.
Changed config requires a fresh replay, not an assumed contradiction or silent suppression.

Proposed interface `compare_decisions(coverage, decisions, *, evidence, config_hash, policy_hash)`
returns deterministic source-local rows with decision ID, previous outcome, observed/new recording
refs, material-change reasons and recommended review status. It must reject incomplete identity
inputs rather than silently attach a meeting to a neighboring case. Exact report schema and
input option will be committed at L3 after the trusted-action transport decision.

Tests: unchanged watch stays pending/quiet recommendation; new related meeting joins same case;
wrong-city/source/topic does not join; contradictory identity requires review; changed date/count
alone does not reopen; assigned/excluded rule receives new recordings without policy reopening;
conflicting history remains visible; deterministic ordering; no network/storage/GitHub/state writes.
Verify reports on frozen local fixtures before proposing any scheduled alert activation.


#### P2c2b exact callable contract, approved transport

Command body is exactly `/body-disposition`, one LF or CRLF newline, and one JSON object
containing only the action fields listed above. Reject duplicate JSON keys. No free-form prose
parsing. The target issue must have been opened by `github-actions[bot]`; parse the checked choices
only inside its generated section.
The action field `expected_parent_event_ids` becomes the existing event parent list. Provenance is
empty for a human-only action. `reopen` must use proposed state and null disposition; a terminal
history can only leave its existing outcome through this explicit action. Diagnostic/conflicting
histories cannot be changed by this first command; explicit multi-parent reconciliation is later.

Trusted snapshot contains repository (`owner/name`), comment (id, html_url, created_at, user.login,
body), permission (the existing GitHub response including user.login), and checked_at. It must
come from fresh authenticated GitHub API calls in a trusted caller; submitted JSON is never a
trusted snapshot. This callable slice validates bindings, not HTTP authenticity. The caller
supplies an aware UTC `now`; permission check age must be between zero and five minutes.
Comment URL must exactly match `https://github.com/<repository>/issues/<number>#issuecomment-<id>`.
Derive actor_id from comment user, approval/artifact references from its URL, delivery ID from
repository/comment ID, and occurred_at from original created_at. Permission user must equal author.

Evidence input has exactly city, source_key, policy_id, normalized_label, evidence_hash,
config_hash, policy_hash, recording_refs, observed_at. It is a fresh deterministic replay context,
not command-supplied evidence. All identity/hash/ref fields must equal the action. Evidence age is
zero to 24 hours. Use now only for freshness, never event identity. A future timestamp fails closed.
The recorder requires the existing lease before source load and immediately before append.
Repeated delivery validates the original event against content and succeeds even though current
parent tips have advanced; conflicting content or a damaged history fails closed. Never fabricate
new events for retries. Return the existing/new event storage key. No production runner is connected.
Additional permitted parameter: `now` on both proposed interfaces. Snapshot is unpacked only into
the validator after current source history has been loaded. No additional files or dependencies.

Validator also takes explicit `checked_at` from the trusted snapshot for the five-minute permission
freshness check. This is a caller context parameter, never a submitted command field.


#### P2c3a exact first report contract

Implement the comparison helper first, with an explicit local input bundle and offline CLI mode.
The first report consumes reviewed case projections, not raw fuzzy titles. Each coverage case
has exactly city, source_key, policy_id, normalized_label and recording_refs. Label is existing
body_key normalization; references use the event schema's exact source/UID/GUID validation.
Cases with the same four-part identity combine references deterministically. A caller must use
reviewed policy identity when projecting aliases; this report does not invent an alias resolver.
Automatic projection from the full coverage report is a later bounded integration, not a claim
that this first report has already reviewed all production cases.

For each saved decision, `evidence` maps its decision ID to exactly `baseline` and `current`
packets. Each packet has exactly `identity`, `official_identity`, `contradictions`, `recording_refs`.
Identity is exactly city, source_key, policy_id, normalized_label. Official identity and
contradictions are JSON lists of retained evidence; no model result is authoritative.
The baseline material_evidence_hash must equal the saved tip's evidence_hash. Its identity and
refs must equal that tip. Current packet identity/refs must equal the observed case. Comparing
only official_identity and contradictions separates substantive evidence changes from increased
recording counts or meeting dates. Missing baseline/current evidence emits history_blocked with
reasons, never a quiet recommendation. Unknown decisions remain ordinary review candidates.

`compare_decisions(coverage, decisions, *, evidence, config_hash, policy_hash)` returns a sorted
list of rows with exactly decision_id, city, source_key, policy_id, normalized_label, disposition,
outcome, recording_refs, new_recording_refs, reasons and review_required. Stored histories are
refolded before use. Output includes saved decisions absent from current observations as
`observations_missing`; this retains uncertainty rather than interpreting absence as exclusion.
Changed config/policy hashes emit `replay_required` unless material identity evidence also changed;
neither is silently classified as a contradiction. `related_recordings_added` recommends renewed
review for watch/open cases, but does not reopen assigned/excluded/not_pursued/rejected decisions
without changed official evidence. Current and retained refs are both preserved. No writes/events.

Add `--decision-comparison-input PATH` and `--decision-comparison-report PATH` to
`scripts/audit_feeds.py`; both required together and incompatible with other audit/action modes.
Read a strict local JSON bundle with schema_version 1, coverage, events, evidence, config_hash,
policy_hash. Reject duplicate JSON keys; events contain full history, not a claimed folded state.
Run this mode before config loading, provider fetch, state synchronization, storage construction
or GitHub reconciliation. Only write the requested JSON report (schema_version 1, rows);
no report timestamp affects comparison. Do not overwrite the input path with the output.

Permitted code for this first report: citypods/remedy_ledger.py, scripts/audit_feeds.py,
tests/test_remedy_ledger.py, tests/test_audit_feeds.py and lifecycle docs. No audit.py change is
needed for this explicit local mode. Existing body-coverage/dispositions interfaces remain intact.
The transport decision is approved; these routine report details implement the authorized
report-only step and are committed before code. Production activation remains gated.

P2c2b prepared verification: 87 ledger/action tests and 5,702 offline tests passed
(16 deselected), with whole Ruff/format across 511 files. No production caller was enabled.


P2c3a synthetic end-to-end report verification (local fixtures, no historical decision import):

| Fixture | Report outcome | Ask again? |
|---|---|---|
| Unchanged pending watch | unchanged | No; retain pending case. |
| New related recording under watch | related_recordings_added | Yes; review the same case. |
| New related recording under exclusion | related_recordings_added | No; retain approved outcome. |
| New official identity contradiction | material_change | Yes; show conflicting evidence. |

Local usage from the repository root:
`PYTHONPATH=. python scripts/audit_feeds.py --decision-comparison-input INPUT.json
--decision-comparison-report REPORT.json` (the command is one line).
Input is the explicit reviewed-case/history bundle specified above, not a raw coverage file.
Automatic coverage projection, trusted API retrieval and weekly alert activation remain subsequent
bounded work. Do not describe these local fixture results as deployed scan suppression.

P2c3a prepared verification: 209 targeted ledger/action/audit-script checks and 5,724 offline
tests passed (16 deselected); whole Ruff/format passed across 511 files. Four synthetic local
case reports matched the recommendations above. No production reads/writes or alert activation.

P2c2b trusted decision actions and retry recovery shipped in
[PR #2244](https://github.com/BashfulBits/city-meeting-podcasts/pull/2244), merged 2026-10-10.
P2c3a remembered-decision comparison shipped in
[PR #2245](https://github.com/BashfulBits/city-meeting-podcasts/pull/2245), merged 2026-10-10.
These callable and local report slices do not connect a scheduled caller, automatic coverage
projection, recurring alert, or publication coordinator. Those P2 integrations remain gated.

CLI review of #2244 completed 2026-10-10 against main with three low-severity findings.
The stale-parent error-order suggestion is not adopted: the early terminal-parent guard also
checks retry semantics before returning an existing delivery; moving it solely to current tips
would weaken that check. Stale parents still fail closed and no accepted outcome changes.
Two report-contract clarifications are recorded here; runtime behavior remains unchanged:

- Baseline evidence_hash is `material_evidence_hash` of the entire exact four-field baseline
  packet (`identity`, `official_identity`, `contradictions`, `recording_refs`), not a packet field
  containing a hash. That existing helper recursively sorts dictionary keys and collection
  elements, omits observed_at clock keys, then uses canonical_hash. Official evidence content and
  recording identifiers are retained; source-local identity and refs must match the saved tip.
- For a saved decision absent from coverage, emit observations_missing without requiring a
  current evidence packet. A damaged/conflicting history still takes precedence as history_blocked.
  Baseline/current packet validation is required for observed cases. #2245 already implements
  this ordering; this clarification removes ambiguity rather than changing its behavior.

Raw CLI output retained locally as /private/tmp/2244-cli-review.ndjson. Code/tests are unchanged
from the previously verified implementation; conflict carry preserves merged main features.

CLI review of #2245 completed 2026-10-10 against its parent implementation, now merged into main.
One valid trivial finding: direct report writes could leave truncated JSON after interruption.
The local report now writes a temporary sibling and atomically replaces the destination after
closing it, cleaning up on failure. A simulated publication failure verifies that the prior report
remains intact and no temporary file remains. No comparison semantics or production activation
changed. Raw review output is retained locally at /private/tmp/2245-cli-review.ndjson.

CLI finding fix verification: 209 targeted checks and 5,747 offline tests passed (16 deselected);
whole Ruff/format passed across 511 files. The prior complete report survives simulated failure.

### P1 admission completion: reasoning capability prerequisite — L3, approved 2026-10-10

The maintainer authorized finishing P1 before the P2 connection, and explicitly approved a scoped
capability update for existing Gemini 3.7/3.8 Flash and DeepSeek V4.1 Flash free direct routes.
Local live execution uses /usr/local/bin/citypods-env and the existing shared CAS quota accounting.
No additional evaluation workflow is authorized or needed. Holdout truth and final admission are
reviewed separately; successful transport alone never qualifies a model.

Permitted prerequisite files: config/provider_limits.yml, scripts/compile_llm_limits.py,
its existing tests, compiled citypods/compute/llm_routes.json and
workers/llm-dispatch-v2/src/dispatch_limits.json, and lifecycle docs. Extend the compiler's closed
reasoning-level set with high/max only. Add high controls for Gemini 3.7/3.8 direct accounts from
Google's documented OpenAI reasoning_effort mapping. Preserve defaults, aliases, route selection,
quotas, existing off/low controls and production lane settings. No lane currently sets a reasoning
level; absent explicit requests continue sending no newly added controls. NVIDIA max mapping must
be supported by that endpoint's documentation and a bounded probe; absent proof leaves it held.

Sources: https://ai.google.dev/gemini-api/docs/openai and
https://ai.google.dev/gemini-api/docs/thinking; NVIDIA endpoint documentation:
https://docs.api.nvidia.com/nim/reference/nvidia-deepseek-v4_1-flash-1.
Tests prove compiler acceptance/rejection, compiled consistency and unchanged default behavior.
Whole Ruff/format and full offline checks precede the prerequisite PR. No production admission,
feed, audio, storage routing, Worker runtime or stage-version change is included.

P1 completion preparation under #2247: compiler supports explicit high/max levels; Gemini
3.7/3.8 direct-account high controls use reasoning_effort=high. Production lane reasoning maps
are empty, so default payloads and route selection remain unchanged. NVIDIA max remains held:
model-level support alone does not prove this endpoint's max-effort mapping.

The maintainer independently approved the ten Denton holdout answers in chat on 2026-10-10.
Frozen manifest, official agenda excerpts and reviewed truth are under
`evals/remedy/holdout/p1-denton-2026-10-10/`; the shipped validator confirms split disjointness.
These four recordings test standalone named-body identity and distinct TIRZ boards, including
wrong Council-room and wrong-zone proposals. They prove no general automatic-merge safety.

Verification: 284 targeted checks, 5,754 offline tests (16 deselected), whole Ruff/format 511 files.
A dry-run planned 54 candidate regression jobs without holds. A bounded local live probe attempted
one case for each Gemini configuration but both failed with NoSuchBucket before any provider call
(provider_attempts=0). Read-only checks confirm B2 primary access and R2 coordination 404; inventory
permission is denied. No quota bypass, new bucket, credential mutation or production admission.
Local evidence is retained as /private/tmp/p1-capability-{dryrun,live}.json. Correct shared quota
storage configuration is required before live evaluation; this prerequisite is not P1 completion.

P1 transport correction approved in chat on 2026-10-10: preserve the existing native Gemini
generateContent transport. Replace the rejected OpenAI-only reasoning_effort field with native
generationConfig.thinkingConfig.thinkingLevel=HIGH in the four Gemini capability entries.
No runtime transport, default lane, quota or production admission changes. Verify compiled
catalogs and bounded provider acceptance before accuracy evaluation.

Native mapping correction verification: 73 compiler checks and 5,754 offline tests passed
(16 deselected), whole Ruff/format clean. Read access to citypods-spike succeeds; use a local
R2_BUCKET override rather than the retired wrapper bucket. Two 30-second native probes timed
out. Maintainer approved a local-only two-request diagnostic with 120-second requests and
150-second case deadlines; production/evaluator defaults remain unchanged. Quota unification
is tracked separately in #2249. No admission or feed change.

CodeRabbit CLI review of #2248 at 84ec7e13 completed: one valid minor rationale issue.
The two wrong-zone holdout explanations now identify the official numbered TIRZ board and
explain that Zone 1/2 are distinct. Approved truth/owner, case inputs and runtime are unchanged.
Raw review retained locally at /private/tmp/2248-cli-review.ndjson.

GitHub full review of #2248 at 52af2c90 completed with one valid minor documentation finding.
The parent holdout README now identifies the independently approved Denton ten-case dataset
and its explicit manifest/gold paths; nested datasets are never discovered implicitly.
Reviewed compiler/runtime, model-visible inputs and approved truth remain unchanged.

P1 capability prerequisite implemented in PR #2248, merged 2026-10-10 at 855cc593.
Freeze the prerequisite contract above; this does not stamp the entire P1 evaluation complete.
Final full review covered 52af2c90; prose-only parent README fix 8d795359 passed all CI.
No model admission or production activation. The first primary Denton batch completed five
correct answers with zero classification errors and one timeout; seven cases per configuration
were unattempted. All four primary/secondary Gemini routes have a 20-request daily cap with
America/Los_Angeles resets. Broader and independent-family qualification remains pending.

### P1 resumable local evaluation — L3, approved 2026-10-10 (#2251)

Permitted files: scripts/eval_remedy.py, tests/test_remedy_evaluation.py,
evals/remedy/README.md and lifecycle docs (review/11, review/51, CHANGELOG, ARCHITECTURE).
Add explicit --request-timeout (1–120 seconds, default 30) and --case-deadline
(1–150 seconds, default 60, at least request timeout); no production limits change.
Add --resume-results explicit immutable result files, validated against manifest/prompt/schema/
catalog/mode and configuration identities. Carry completed rows without another model call;
failed/unattempted cases may retry in a new run and retain prior raw files. Reject conflicting
completed observations and unknown pairs. Resume never reads gold or qualifies a model.
Add --checkpoint-dir: after each attempted job atomically publish a separate immutable JSON
observation with its frozen context and run provenance. An interruption preserves completed
observations; final --out remains exclusive/immutable. A resumed run explicitly names files.
Before each real job read the shared CAS quota ledger. For each allowed physical route use its
reset timezone/current-day reservations; admit only if at least two daily requests remain
(the existing per-job maximum provider attempts). Unavailable/invalid quota state fails closed.
No-RPD routes still rely on shared scheduler limits. Test-injected backends may supply a quota
preflight callback; actual live execution always uses the shared ledger. Shared scheduler
reservations remain authoritative under races. Defer insufficient capacity without provider calls.
Tests: interruption persistence, resume skipping, context/conflict rejection, reset handling,
retry headroom, failed quota read, timeout bounds, unchanged defaults and no gold in prompts.
Run targeted/full offline and whole Ruff/format before push. Do not change model/catalog/
production storage routing, scheduler, feeds, workflows, dependencies or other runtime modules.

#2251 prepared validation: 134 targeted tests and 5,765 offline tests passed (16 deselected);
whole Ruff/format clean across 511 files. CLI dry-run accepts the two prior immutable Denton
result files with four physical candidate configurations. Completed pairs are skipped; the
failed primary observation remains retryable. Secondary batch completed six correct answers
with zero errors/timeouts, three per route. These partial runs do not complete admission.

### P1 independent Kimi reviewer capability — L3, approved 2026-10-10 (#2253)

The maintainer approved evaluating the existing free NVIDIA Kimi K3 physical route as an
independent reviewer. AA Intelligence Index v4.3.2 reports max reasoning at 44, exceeding the
Gemini 3.7/3.8 high baselines of 39/41. NVIDIA’s endpoint documents reasoning_effort=max.
Sources: https://artificialanalysis.ai/models/kimi-k3/ and
https://docs.api.nvidia.com/nim/reference/moonshotai-kimi-k3-infer.

Permitted capability files: config/provider_limits.yml, existing compiler tests, compiled
citypods/compute/llm_routes.json and workers/llm-dispatch-v2/src/dispatch_limits.json. Add only
nvidia_kimi_k3_free reasoning_controls.max.reasoning_effort=max. Existing production request
parameters, lane defaults, routes, quotas, storage, credentials and admissions remain unchanged.
Tests prove compiled Python/Worker parity and absent default request parameters. Run targeted
and full offline tests plus whole Ruff/format before push. Update review/11, CHANGELOG and
ARCHITECTURE. No pipeline version change or backfill.

Use candidate-only local configurations, shared CAS quota accounting, explicit 120-second
request timeout /150-second case deadline and immutable resumable observations. Begin with
one bounded regression probe; expand only after valid physical identity/effort output. Then run
claim-support regression and approved Denton holdout, including blind owner selection. Never
send gold to models. Keep results separate by route/catalog/mode hashes; no automatic admission.
Record failures and unattempted cases honestly; final qualification requires reviewed evidence.

#2253 capability validation: 74 compiler tests and 5,766 offline tests passed
(16 deselected); whole Ruff/format clean across 511 files. CodeRabbit CLI review of
a5a13ccc completed with zero findings. One quota-accounted NVIDIA probe returned the
requested Kimi K3 model/max controls and a valid correct wrong-district rejection.
This verifies transport/control provenance, not admission. Broader local evaluations are
running; no production admission or alias automation is enabled.

### P1 comparison diagnostics and evidence retention — L3, approved 2026-10-10

The maintainer requested diagnosis of Gemini failures, investigation of DeepSeek response
problems, GLM testing, and durable artifacts under evals/remedy/. Permit an evaluation-only
diagnostic script and results bundle there, plus lifecycle documentation. Reuse the exact
configured free routes, existing evidence prompts and shared CAS quota accounting. Do not
change production routing, quotas, provider catalogs, storage routing or model admission.

Capture sanitized provider errors and stream timing separately from scored model answers.
Use at most two new Gemini diagnostics (120-second request, 150-second case), one DeepSeek
stream diagnostic (600-second request, 650-second case, 16,384 output tokens), and GLM's
ten evidence-check plus ten blind-owner cases (120/150 seconds, 4,096 output tokens).
GLM uses its existing default reasoning; absent verified maximum controls it is a baseline
comparison, not an admission candidate. No implicit paid or alternate-route fallback.

Preserve prior configurations, observations, raw answers, scores and original provenance.
Exclude credentials and credential-bearing logs; checksum retained files and document the
unretained DeepSeek runs whose exclusive output paths collided. Gold is read only for
scoring after requests. A valid default call does not prove maximum reasoning.

P1 comparison diagnostics prepared under #2255: the retained Gemini provider response
confirms Google's per-project/model 20-request daily exhaustion, misclassified in direct
logs as a gateway limit. The production classifier/retry fix remains a separate follow-up.
DeepSeek streamed zero chunks before 600-second timeout; NVIDIA versus the configured
gateway is not yet isolated. GLM 5.3 Flash default answered 20/20 Denton cases correctly
in content-only scoring; shortened returned identity and unverified maximum effort remain
held. Kimi completed 20/20 Denton and 18 correct/8 abstained/1 unknown historical cases.
Artifacts, catalogs, original failed attempts and reproducible scores are retained under
evals/remedy/results/p1-comparison-2026-10-10/. No model is activated or admitted here.
Verification: 61 targeted and 5,766 offline tests passed, 16 deselected; whole Ruff/format clean.

### Patient alternative comparison — L3, maintainer approved 2026-10-10

Evaluate NVIDIA full GLM 5.3 (`z-ai/glm-5.3`) and the existing free BeatAPI
GPT 6 Astra, GPT 6.1 Sol and DeepSeek V4.1 Flash physical routes. Permit an
evaluation-only script under evals/remedy/results/p1-patient-2026-10-10/ and
immutable results there. Register GLM only in this evaluator process, using the
existing NVIDIA adapter with a distinct physical quota identity; no persisted catalog
or production lane changes. Its conservative evaluation bounds are 1 RPM, one
in-flight request, 32,768 input and 16,384 output tokens, not published provider limits.

Use the ten approved Denton cases in claim-support and blind-owner modes per model,
600-second provider timeout, 650-second case deadline, 16,384 output-token budget,
one provider attempt per case and immediate result checkpoints. Do not send gold.
Default reasoning is a baseline until exact maximum controls are verified. Preserve
returned model identity and separate content-only scores from admission gates.

Use shared CAS quota accounting and the Worker operator pause/drain/reservation API
around each provider evaluation call so production usage includes the attempted call
on existing routes. Full GLM has no production route to reserve: retain its distinct
CAS ledger usage and provider-exclusive pause, without inventing a Worker route.
BeatAPI calls are serialized, wait at least 65 seconds between attempts and defer
while its production work is in flight. NVIDIA calls likewise wait for provider drain.
Pauses expire after at most one hour and are resumed on exit. No activation, paid
fallback, quota increase, feed edits or gold changes. Save sanitized errors, request
timing, provenance and every failed attempt. Any drain timeout defers the case.

The four Gemini routes are temporarily paused through the explicitly approved
2026-10-11T07:15Z recheck time; renewal and a separate bounded quality recheck are
scheduled in this chat. This does not permanently disable production Gemini routes.

Patient comparison transport clarification, before BeatAPI provider requests:
use LiteLLM's OpenAI-compatible adapter locally for BeatAPI's documented chat
endpoint while preserving exact free upstream model and physical quota identity.
The generated `beatapi/` direct-model prefix is rejected by local LiteLLM before
HTTP; retain that failed setup observation and do not change production catalogs.

Patient comparison retry extension — approved by maintainer 2026-10-10:
add an explicit --retry-from results argument to the evaluation-only script.
Validate model/route and frozen manifest identity before requests; select only
previously deferred mode/case pairs, never repeat completed cases. Retain originals
and save retry observations separately. Existing request/output, pause/drain and
quota bounds stay unchanged. Combine latest retry rows with originals only in an
explicit derived scoring file; never rewrite original observations.

Gemini evaluation resumption — maintainer approved 2026-10-11:
resume missing Denton claim-support and blind-owner pairs on all four approved
Gemini routes after the successful reset diagnostic. Preserve original observations.
Use the existing eval_remedy runner, 120-second request/150-second case limits and
shared quota headroom checks. Generate evaluation-only subset manifests and candidate
configs under evals/remedy/results/gemini-resume-2026-10-11/, explicitly recording
current catalog/subset hashes. Map original configuration IDs through retained
admission files; skip completed pairs without rewriting their provenance. No admission,
production defaults or quotas change. Daily exhaustion remains pending, not incorrect.

### Varied backlog evaluation preparation — 2026-10-11

Maintainer authorized a larger challenge set from the actual unexpected-body backlog.
`evals/remedy/holdout/backlog-varied-2026-10-11/` retains 60 unique source/recording pairs
across Addison, Arlington, Dallas, Denton and Fort Worth. It separates review-only past
assignments/dispositions from model-input drafts, records input hashes and includes joints,
bond programs, numbered districts, planning/Council boundaries, archive media and sparse bodies.
This is preparation, not approved gold or admission evidence. Exact official excerpts and current
feed inventory must be bound before freezing. Split overlap must be removed for admission;
already-tested recordings belong only in a named regression/challenge slice. No live calls or
production route changes are authorized by generating this preparation artifact.

Varied-set preparation checkpoint: 20 retained official agendas retrieved with document/text
hashes; 60 review packets and separate answer-free input drafts saved. Twenty agenda-backed
feed sets are drafted, not new approved gold; forty cases require additional evidence/adjudication.
Three prior Denton recording overlaps are explicitly separated; source/family isolation remains
pending. Multi-feed correctness needs membership checks rather than a fabricated exclusive owner.
A lighter-proposer experiment is proposed in the set's `smaller-model-options.md`; it does not
lower the existing Gemini 3.7 High admission floor or authorize new live model calls.

### Flash-Lite Denton baseline — scoped approval, 2026-10-11

Maintainer approved including existing Gemini 3.5 Flash-Lite in the same ten grounded Denton
cases, claim-support and blind-owner modes (20 logical jobs). This is an evaluation-only exception
to the smaller-model admission floor, not production qualification. Use the existing direct-route
backend, one allowed physical primary route, default reasoning explicitly labelled, 120-second
requests, 150-second case deadlines, 4,096 output tokens and one provider attempt per job.
Read Worker route usage and shared local CAS quota accounting before each call; defer on missing
capacity evidence or fewer than 100 remaining daily calls, never increase limits. Use existing
route pause/drain/reserve helpers around each call and resume on exit; do not pause the entire
Google provider or other models. Save immutable observations, capacity and score artifacts under
`evals/remedy/results/flash-lite-denton-2026-10-11/`. No defaults, catalogs or feeds change.

Flash-Lite baseline completed: 20/20 Denton answers correct, zero wrong/abstained/failed;
provider median1.106s versus end-to-end median5.601s. This tests four underlying meetings with
provider-default reasoning, not maximum-effort or broad admission. Immutable raw/checkpoint,
explicit-gold content reports, route capacity snapshots and request hashes are retained under
`evals/remedy/results/flash-lite-denton-2026-10-11/`. Current main pins Gemini3.1 Flash-Lite as
primary tagger; 3.5 is available for other lanes. No production defaults/floor changed.

Entire varied backlog packet v2 prepared under `evals/remedy/holdout/backlog-varied-2026-10-11/`:
all60 source/UID/GUID pairs found with source media;38 retained agendas plus official supplemental
minutes/native headings/provider pages collected.121 linked document responses and65 supplements
retained; two current Addison pages returned403 and remain documented gaps. Raw evidence is sealed
with per-entry hashes. Proposed answers cover40 assignments,15 existing archive/event/untracked
policies and5 insufficient-owner-proof programs. The maintainer approved the113 membership/proof
answers for this evaluation; they are frozen separately under `approved-v2/`. Body identity and
publication aggregation stay distinct; known-policy controls do not inflate independent identity
accuracy. No new production dispositions, evaluator schemas or route limits changed in preparation.

### Varied-packet evaluation — maintainer approved 2026-10-11

Maintainer approved the60-recording packet's113 membership/proof answers and staged testing of
nine previously compared candidates. Freeze input-only evidence separately from approved gold;
retain historical challenge/regression status, overlap/event groups and separate identity,
publication-compliance and insufficient-proof metrics. Existing exclusion/aggregation approvals
must be supplied as policy, never silently treated as independently inferred identity.
Allowed changes: evaluation-only scaffolding and frozen/results files under `evals/remedy/`,
and lifecycle docs. Reuse direct backend, structured answer schema, local CAS quota accounting
and Worker pause/drain/reserve helpers. No evaluator core schema, production code/config, feeds,
route catalogs, budgets, provider limits or model activation change.
Run a balanced pilot of eight questions, then resume the same frozen113-case comparison without
repeating completed route/case pairs. Each route/case permits one provider attempt per invocation;
failed observations are retained and never silently replaced. Use the prior approved evaluation
controls: Gemini120s request/150s case/4096 output; patient alternatives600s/650s/16384 output.
Gemini3.7/3.8 High and Kimi Max retain verified controls; other candidates are labelled default
reasoning, not maximum-effort qualification. Scoped Flash-Lite evaluation remains below-floor
and diagnostic-only. Separate quota project/account uncertainty and production headroom remain
visible. BeatAPI's shared JEV capacity and NVIDIA's production lanes retain priority. Insufficient
capacity defers work across resets; no paid capacity, duplicate requests or unbounded retry loop.

The first eight-slot pilot invocation was unbalanced: six Flash-Lite observations completed, one
Gemini 3.7 attempt failed at its route attempt limit, one Gemini 3.8 attempt received provider 503,
and the remaining candidates deferred before a provider request because their shared accounts were
busy. This exposed that a global pilot cap could favor the first candidate in the list. The
evaluation-only runner now requires a single candidate per pilot invocation, so each model gets the
same eight examples without changing any original result. A later Kimi K3 pilot completed all eight:
7 correct, 1 incorrect, with the miss being the approved Dallas Transportation and Infrastructure
membership. Raw results and offline scoring are under
`evals/remedy/results/varied-2026-10-11/`. These eight cases are from the regression challenge,
not blind-owner admission truth; no model has a qualifying P1 report from them. Remaining pilot and
full-comparison candidates are pending shared provider capacity. Production admissions remain empty.

### P1 backfill recovery — L3, approved 2026-10-12 (#2409)

The maintainer approved all four repairs in the retained backfill diagnosis: retry scheduling,
reset-aware resumption, controlled route cooldowns, and original provider-error reporting.
This supplements the varied-packet contract above without changing its frozen questions or limits.

Permitted implementation files: the existing varied `run.py` and `score.py`, new `recovery.py`
and `backfill.py` under `evals/remedy/results/varied-2026-10-11/`, its retained diagnosis/README,
`tests/test_remedy_backfill.py`, and lifecycle docs. The new controller reads immutable history,
protects successful logical model/case pairs across Gemini keys, and offers failed cases without
waiting for global success. At most three attempts per physical candidate/case per UTC day and
three new retries per physical pair per controller run; all provider batch caps remain at 16.
Existing historical attempts remain charged evidence. Retry exhaustion is reported explicitly.
Write an immutable attempt intent before each new provider call. An interrupted call without a
final observation remains an unknown outcome and is held across both keys, rather than blindly
repeated. Final observations reference their intent; unmatched intents count toward call budgets.

Preserve original sanitized provider status/message, Retry-After and failure category before
the backend can replace them with its attempt-budget error. Server errors, timeouts and rate
limits schedule evaluation-only cooldowns of at least 30 minutes, respecting longer Retry-After.
Invalid answers remain a distinct bounded retry category. Daily quota exhaustion waits until
the later applicable local/Worker reset; retain the current 20% production reserve and fail
closed on malformed quota/status evidence. Provider-busy or failed status reads wait five
minutes, without changing production priority or pause scopes.

The controller persists atomic status with per-candidate answered, failed, uncalled, exhausted,
next eligible time and stop reason. It resumes from observation history and status after resets,
holds the existing provider-call lock inside each runner invocation, and uses its own controller
lock. It runs for an explicit bounded horizon (default 24 hours), with at most 256 new provider
attempts per run; incomplete stopping is never called completion. Poll no longer than 60 seconds
at a time for cancellation, while sleeping until actual eligibility. Astra is excluded; preserved
historical observations remain available. The comparison target is eight logical models ×113=904.

Offline tests must prove failed-case recovery before global success, cooldown/reset boundaries,
bounded retry exhaustion, immutable success deduplication across keys, diagnostic redaction,
original 429 preservation, status persistence, lock exclusion, and accurate incomplete stopping.
Run targeted and full offline tests and whole Ruff/format before pushing a dependent PR on #2277.
Do not edit production runtime/config, provider quotas, catalogs, credentials, feeds or audio;
do not restart or interrupt the active legacy runner as part of this implementation PR.
Update CHANGELOG, ARCHITECTURE and review/11 in the implementation change.

#2409 prepared validation after carrying current main: 79 targeted tests and 6,043 offline tests
passed (16 deselected). Whole Ruff/format passed across 539 files. A credentials-free plan over
the actual saved history
reports 614/904 answered and makes all 83 unanswered non-Gemini failures eligible for bounded
recovery. Live quotas are checked only on execution. No provider calls were launched during
implementation; the legacy controller and original observations remain untouched.

Implementation PR: [#2410](https://github.com/BashfulBits/city-meeting-podcasts/pull/2410),
dependent on #2277. The parent's single changelog conflict was resolved in an isolated checkout
and carried forward. Current main's fragment convention applies: the lifecycle entry is in
`changelog.d/2410-backfill-recovery.md`, compiled into CHANGELOG after human merge.

### P1 six-model completion — L3, approved 2026-10-13

The maintainer discontinued Gemini 3.7/3.8 testing because of their low daily quotas.
Keep all historical Gemini and Astra observations, but exclude them from active calls and scores.
The active comparison is Flash-Lite, Kimi, full GLM, GLM Flash, Sol and DeepSeek: six models
with 113 questions each (678 answers). This supersedes the eight-model target above.

Before changing code, stop the idle controller without interrupting a provider request.
Change only the existing varied recovery.py/backfill.py/run.py/score.py and README,
tests/test_remedy_backfill.py, and lifecycle docs. Separate active candidates from the
historical candidate allowlist; block discontinued candidates before loading credentials.
Prove historical observations remain readable, excluded calls/counts, and six-model stopping.
The scorer must retain hashed provenance when results live outside the executing checkout.

Resume a new bounded controller run to finish only unanswered active cases. Preserve the
three-attempt per-case UTC-day cap, production headroom, model settings and existing timeouts.
When those daily attempts are exhausted, wait until midnight UTC before any new request.
Save separate scored reports using explicit approved truth; never replace raw observations.
No production routes, feeds, quotas, workflow changes or model activations are authorized here.
Run targeted tests and whole Ruff/format before resuming, and the full offline suite before push.

Six-model follow-on validation: 87 targeted tests passed; full offline suite passed
6,074 tests (16 deselected); whole Ruff/format passed across 539 files. The separate
`six-model-scored-2026-10-13-v1.json` report records 673/678 answers. Full GLM has two
unanswered and Sol three; the bounded replacement is waiting for 2026-10-14 00:00 UTC
(October 13, 7 PM Chicago). This is incomplete comparison evidence, not P1 admission.

### P1 immediate five-pair retry allowance — L3, approved 2026-10-13

The maintainer explicitly approved up to five additional retries per remaining question/model
to start immediately. This overrides the daily three-attempt evaluator cap only for the two
remaining full GLM and three Sol pairs. Total additional provider attempts cannot exceed 25.

Implement an explicit immutable local retry-allowance JSON in the existing varied result
directory, binding the frozen manifest hash, exact five pairs, authorization timestamp and
reference, and five attempts per pair. The existing recovery.py validates this scoped allowance;
candidate_plan counts final observations and unmatched intents since its timestamp, holds
unknown outcomes, stops at five per pair, and bypasses the daily cap only for listed pairs.
Existing controller/run.py accept the explicit allowance path and persist/verify its hash
across restarts. Completed pairs remain skipped; normal policies apply to unlisted pairs.
Keep provider cooldowns, capacity checks, production headroom, per-call limits and timeouts.
A new bounded controller run has max_calls=25; no daily reset refills this one-off allowance.
Modify only the existing varied recovery.py/backfill.py/run.py/README, tests/test_remedy_backfill.py
and lifecycle docs. Verify scope isolation, five-attempt exhaustion across daily resets,
restart hash fencing and unknown-intent holds; targeted/whole Ruff before starting, full
offline tests before push. Preserve all raw history. No production policy changes.

The immediate allowance passed 95 targeted tests, 6,083 offline tests (16 deselected),
whole Ruff/format (539 files), and changelog fragment validation. The scoped controller
submitted its first full GLM attempt at 2026-10-13 13:33 UTC; no completed answer was
available at that handoff. The existing hourly monitor follows actual saved results.

### P1 full independent body-selection comparison — L3, approved 2026-10-13

Maintainer directed the full 60-recording correct prompt for all six remaining models:
which body or bodies does this recording belong to, with no suggested assignment.
Use Flash-Lite, Kimi, full GLM, GLM Flash, Sol and DeepSeek; exclude Astra and Gemini 3.7/3.8.
Reuse frozen official evidence, taxonomy and approved policies from approved-v2. Collapse
113 membership questions into 60 unique source/UID pairs. Mask semantic case IDs, claims,
review answers and proposal metadata. No model sees another model's answer or the gold.

Create evaluation-only body_selection.py, freeze.py, run.py, score.py and README under
evals/remedy/results/body-selection-2026-10-13/, frozen inputs and separately stored gold
under evals/remedy/holdout/body-selection-2026-10-13/, tests/test_remedy_body_selection.py
and lifecycle docs. No production schemas, routes, quotas, feeds or workflows change.
Local answer schema contains opaque case_id, cited body/program names, selected feed slugs,
publication action, evidence-reference IDs, missing evidence and rationale. Validate IDs
and taxonomy locally. Score exact feed sets for 40 assignments, separate policy compliance
for 15 approved archival/event/untracked outcomes, and unsupported-owner guardrails for
five insufficient-proof programs. Do not count those five as known non-government content
or invent exclusive owners for joints. Body-name free text remains available for human
review; feed-choice and policy/insufficient-proof metrics are separately identified.

Reuse existing varied provider routes, shared CAS quota/headroom, operator pause/drain/reserve
helpers, timeout/output settings and immutable pre-call intents. Share the older evaluation's
provider-call lock via an explicit CLI path; lock contention defers rather than duplicating
calls. Independent controller lock/status prevents duplicate body-selection runners.
Patient requests remain600s/650s/16384 output; Flash-Lite120s/150s/4096. Free routes only.
Every completed answer stops retries regardless of its correctness; runtime never reads gold.

Interpret approval as one initial attempt plus up to three retries per round, at most three
rounds: maximum12 submitted attempts per model/recording,4320 across360 pairs. Retain
failed attempts, round/attempt metadata and hashes. No SDK retries. Apply30-minute provider
failure cooldowns and actual quota/reset waits; retain production headroom. Existing
three-attempt daily evaluation cap is replaced only inside this specifically approved
experiment by the12-attempt total cap, never applied to production or older experiments.
A bounded seven-day horizon uses the existing controller hard ceiling; interrupted runs
resume the same original deadline and allowance, never reset attempt counts. Missing final
observations hold that pair as unknown instead of automatically repeating a lost response.

Tests prove answer-free inputs,60 unique pairs, joint/feed-set scoring, unknown-proof holds,
round boundaries/caps across resets, no gold in live execution, immutable deduplication,
identity/provenance binding and shared-lock exclusion. Targeted/whole Ruff before calls;
full offline tests before push. Retain input/result hashes and separate immutable reports.
These reused historical records remain a regression challenge, not newly independent
holdout admission evidence; production model approval remains separate.

Body-selection input clarification before code: taxonomy includes feed names/descriptions,
not existing body/GUID selector predicates that disclose historical assignments. Strip
maintained-policy evidence and maintained_local_disposition; supply only general approved
identity/publication rules. Retain verbatim official recording metadata and source identifiers.
Gold keeps reviewed body descriptions for later review; do not score free-text institutional
names by fabricated string aliases. Report exact feed-choice correctness, policy controls
and insufficient-owner-proof guardrails separately.

Body-selection verification: 115 targeted checks and 6,103 offline tests passed
(16 deselected), whole Ruff/format544 files. A preparation-only metadata correction
now hashes the complete gold document rather than its inner cases; original hash
metadata is retained, model inputs/answers unchanged, and116 targeted checks pass.
The corrected controller started2026-10-13 14:47UTC. First valid Flash-Lite, GLM Flash
and Sol answers independently named the Addison Bond Advisory Committee and cited
official evidence. Live results remain incremental; this is not completion or admission.

Maintainer reporting direction2026-10-13: show the full six-model113-question
suggested-assignment comparison after full GLM completes. Keep those results separate
from the new60-recording body-selection scores. Defer production model recommendations
and selection until the new experiment is scored. Body-selection work is retained in
draft PR#2413, dependent on#2411, while immutable results accumulate.


### P1 concurrent evaluation call priority — L3, approved 2026-10-13

Maintainer approved 50–75% of contested provider-call slots for the older113-question
comparison until it finishes. Choose two older submissions per one new submission
(66.7%) when both runs are eligible. An older cooldown, quota wait, exhausted/held
allowance or completed/stopped controller releases unused slots to body selection.
This corrects shared-lock starvation; it changes neither provider capacity nor retry limits.

Implement evaluation-only call_priority.py beside the body-selection runner, its explicit
--priority-results-dir option, tests/test_remedy_body_selection.py and lifecycle docs.
Persist the exact older results directory, manifest hash, controller run ID, initial
submitted-attempt counter and new contested attempt IDs. Count older submitted attempts,
including failed calls, rather than successful answers. Never reset allocation on resume;
a changed controller identity/counter or stale/unreadable status holds new calls for review.
The older runner_busy state represents eligible work despite its five-minute lock retry.
Yield before acquiring the shared provider lock and recheck under that lock. Deferred
preflight checks consume no slot. Borrowed calls during older capacity/cooldown waits
consume no reserved new slot; publish allocation/wait reasons in controller-status.json.
Use the existing controller lock to detect an older controller that has stopped.

Move the new runner to the tested checkout only after taking its shared call lock,
which proves its current provider call has finished. Interrupt the idle new controller,
then resume with the same result directory, deadline, intents and bounded retry counters.
Do not interrupt the older controller or any submitted request. No production files,
model routes, prompts, gold answers, credentials, quota limits or admission rules change.
Tests cover two-to-one contention, retries counting as slots, borrowing during cooldown,
completion/stopping, stale or changed state, resume and zero-call preflight deferrals.
Run targeted tests, full offline suite, whole Ruff/format and fragment validation before push.


Priority verification: 6,118 full offline tests passed (16 deselected); the final
38 body-selection targeted tests also pass, including controller-level allocation,
resume and preflight-deferral checks. Whole Ruff/format545 files and changelog
fragment validation pass. The former new controller stopped only after its call
lock was acquired, with its response retained; the older GLM run submitted again
at2026-10-13 15:58:29UTC. Resume body selection from the tested priority checkout
using its existing results directory and original seven-day deadline. Draft#2413
retains this fix and the input-size/timeout diagnosis; no model admission follows.


Groq feasibility audit2026-10-13: all60 unchanged body-selection packets were measured
with full schema and upstream tokenizer/chat templates, without provider calls.
GPT-OSS high inputs span1,631–6,645 local tokens against configured7,125 input ceiling.
Qwen3.8 native xhigh (Groq high mapping) spans1,694–6,995 against7,000; June13 Dallas
Streets has only5 tokens of local margin, May25 only222. Hosted formatting may differ;
no acceptance claim or new model scope follows. Audit and all60 per-record measurements
are retained as groq-input-size-audit-2026-10-13-v1.json/.md in the body-selection
results directory. Keep recorded hard ceilings and shared capacity unchanged.
Current Groq documentation lists Qwen output maximum16,384, versus65,536 in our catalog;
this is a follow-up catalog discrepancy, not a reason to alter production in this audit.


### P1 Groq body-selection candidates — L3, approved 2026-10-13

Maintainer approved adding the configured Groq GPT-OSS120B and Qwen3.8-27B routes to
all60 unchanged independent body-selection questions. Add two logical candidates;
comparison target becomes480 answers, while the older claim-checking target stays678.
Keep the original manifest/prompt/schema/gold, six-model observations and seven-day
start/deadline. Each added pair uses the same three rounds/four submitted attempts,
maximum12; total experiment allowance grows4320→5760 only for120 additional pairs.
Record this exact model-set expansion in persisted controller status on resume.

Scope: evaluation-only groq_routes.py plus body_selection.py, run.py, README.md,
score.py if needed, tests/test_remedy_body_selection.py and lifecycle docs under the
existing body-selection contract. Do not edit production route catalogs, quotas,
model activation, prompts, evidence, scoring answers, feeds, workers or workflows.
Register local copies of the existing physical routes, explicit high reasoning
(reasoning_effort=high on Groq; native Qwen xhigh), reasoning_format=parsed for JSON
mode and maximum16384 output tokens. Retain600s request/650s case bounds and exact
physical route allowlists/no fallback. Pin documented reasoning controls in local
config/provenance, without claiming admission or maximum effort from default calls.
GPT-OSS's missing daily-token field uses a conservative evaluation-only180000 cap
below the public200000 limit; never raise an existing route limit. Retain Qwen's
configured180000/day and all current input/rate ceilings. Shared quota/CAS accounting
and provider pause/drain/reserve remain mandatory. Keep20% daily-token headroom too,
using the higher known local/Worker usage; absent or invalid usage holds requests.

For these two new candidates, try the largest two packets first, then the proven
Fort Worth Council/school-board joint, then an insufficient-owner-proof recording.
Continue the full unchanged set after submitted failures using existing cooldowns
and caps; never retry a valid but wrong answer. Persist actual provider prompt/output
usage, diagnostics, requested/observed model and exact controls. Provider input-cap
or reasoning-control rejection remains failed evidence; do not trim the frozen packet
or silently lower effort. The shared older comparison still gets two-thirds of
contested slots. A safe restart takes the shared lock after any active call completes,
then resumes the same results and deadline; never interrupt an in-flight request.

Tests prove eight-model totals/migration without reset, unchanged old contexts,
exact Groq route/effort parameters, complete schema-aware input checks, largest-first
new-model order, daily-token headroom, no fallback and one provider call per attempt.
Run targeted and full offline tests, whole Ruff/format and fragment checks before
push and resumption. Raw live artifacts stay under evals/remedy/; no credentials print.
Sources: https://console.groq.com/docs/api-reference and
https://console.groq.com/docs/rate-limits (checked2026-10-13).


Groq pre-code capacity clarification: existing pause-status reports request counts but
not daily tokens. Read the existing authenticated v2/stats?detail=1&limit=100 endpoint
via DispatchPauseClient's request helper for bounded route rows and tpd_used; do not
create a Worker endpoint. Require complete route inventory and fresh numeric usage.
Combine the observed Worker token debt and local CAS token debt conservatively;
missing/partial evidence defers Groq calls. Local copies preserve all original limits
and may only add the documented missing GPT-OSS daily-token cap for this evaluation.


Groq expansion verification:147 targeted tests and6,135 full offline tests passed
(16 deselected), whole Ruff/format546 files and changelog fragments clean. An offline
expanded plan preserved69 prior answers and added two zero-call candidates for480
pairs. Request tests verify high/parsed controls, schema shaping and one submitted
attempt; capacity tests hold missing/stale/partial token evidence. Worker snapshot
before resumption reported Qwen about179000/180000 daily tokens, so it must wait for
production headroom. No first Groq answer is claimed by these offline checks.
The previous controller stopped between requests; resume the original results and
persisted priority from the tested Groq checkout, without extending the deadline.


### P1 patient evaluation timeout extension — L3, approved 2026-10-13

Maintainer directed increasing the existing600-second evaluation request bound to900 seconds
so slow valid answers can finish. This supersedes only the patient bounds in both active
experiments above: proposed-assignment comparison and independent body selection. Retain
Flash-Lite120-second requests/150-second cases. Patient case deadlines become950 seconds;
provider pause leases become1000 seconds and controller end-of-horizon guards1050 seconds.
The longer wait can recover slow responses but cannot bypass an upstream gateway timeout.

Scope: existing evaluation-only varied run.py/backfill.py and body-selection run.py, their
READMEs, tests/test_remedy_backfill.py, tests/test_remedy_body_selection.py and lifecycle docs.
Do not change production limits, retry allowances, quotas, output caps, route selection,
input/gold, original deadlines or saved observations. Record the new actual timeout in
immutable future intents/results and existing request provenance. Preserve all prior600s rows.

Verify actual provider timeout, surrounding case deadline and pause duration with mocked
requests; retain one provider attempt/no SDK retry. Run targeted and full offline tests,
whole Ruff/format and fragment validation before push. Prepare in an isolated checkout.
Take the shared provider-call lock only after current calls finish, stop both idle controllers
and resume their saved state from tested code without --new-run. Preserve older run identity,
priority allocation, original horizons and all per-question retry counters. Update the hourly
monitor's execution paths after resumption; never interrupt a submitted request.


Timeout extension verification:149 targeted checks and6,137 full offline tests passed
(16 deselected), whole Ruff/format546 files and fragment validation clean. Mocked
requests verify900-second patient timeouts,950-second case deadlines,1000-second
covering provider leases and retained Flash-Lite bounds. Offline resumption plans
preserve the existing184/480 body-selection and676/678 claim-checking answers.
The shared coordination service subsequently reported its Durable Objects free-tier
rows-read budget exhausted; both controllers correctly hold unverifiable capacity.
This timeout change does not bypass that operational limit.


### P1 temporary coordination bypass — L3, approved 2026-10-13

The maintainer explicitly approved the proposed temporary evaluation mode after the
shared Worker exhausted its Durable Objects rows-read allowance. The proposal disclosed
loss of visibility into production jobs' usage. Bypass only unavailable Worker status,
pause/drain/resume and external reservation calls for the existing approved evaluations.
Keep local CAS quota reservation/settlement and20% local headroom, exact free route
allowlists, one shared provider-call lock, original retry caps, cooldowns, output caps,
900s/950s patient bounds,120s/150s Flash-Lite bounds and original controller horizons.

Add explicit default-off --coordination-bypass to the existing varied run.py/backfill.py
and body-selection run.py. Scope also includes their READMEs, groq_routes.py, existing
tests/test_remedy_backfill.py, tests/test_remedy_body_selection.py and lifecycle docs.
No production implementation, catalog, workflow, feed or quota changes. Require the
existing scoped allowance for older bypass calls; reject --new-run with bypass so it
cannot mint fresh attempts. Record bypass/approval and local-only usage visibility in
status, immutable intents/results and settings hashes; never claim production drained.
Old Worker-error waits can be reevaluated on the explicit mode change, but actual provider
cooldowns, successful answers and unknown-intent holds remain untouched.

For GPT-OSS, retain local daily-token quota checks and20% reserve without querying the
Worker. Mark Worker usage unknown, never zero. Qwen stays held in bypass because its
last observed token allowance was almost exhausted; bypass does not authorize Qwen calls
until capacity can be verified. Provider429s terminate the attempt and trigger existing
cooldowns; do not interpret infrastructure capacity errors as submitted model failures.

Test default normal gates, explicit bypass making zero Worker requests, local quota and
token holds, Qwen exclusion, forwarding to older subprocesses, immutable mode provenance
and one-call/no-retry controls. Run targeted and full offline suites, whole Ruff/format
and fragment checks before push/resumption. Switch only while holding the shared call
lock after all intents have final observations; retain run identities, deadlines, saved
answers, allocation and retry counters. End the opt-in with these bounded evaluations;
future runs retain normal coordination checks by default.


Coordination bypass verification:164 targeted checks and6,152 offline tests passed
(16 deselected), whole Ruff/format546 files and changelog fragment validation clean.
Tests cover zero Worker calls in bypass, local request/token reserves, Qwen holds,
mode-bound immutable intents, rate-limit preservation and older subprocess forwarding.
A read-only local CAS preflight showed all seven permitted models eligible; GPT-OSS
had about113287 local token debt against180000, with36000 tokens reserved. Worker
usage remains unknown. Qwen was held before any provider request or Worker read.
No live completion or quota visibility is inferred from these preparation checks.


### P1 Qwen bypass hold removal — L3, approved2026-10-13

Maintainer explicitly directed removing Qwen's unconditional bypass hold so evaluation
can use the next available token window without another manual unblock. This supersedes
only the Qwen exception in the temporary coordination bypass contract. Qwen now follows
GPT-OSS's evaluation-local request/token checks with180000 daily-token cap and20% reserve.
Worker usage remains unknown during bypass; do not claim a fresh provider reset from
midnight alone. Preserve high/parsed controls, native xhigh mapping, all frozen packets,
existing provider cooldowns,12 attempts per pair and original seven-day horizon.

Scope: existing body-selection run.py/README, tests/test_remedy_body_selection.py and
lifecycle docs. Remove both unconditional Qwen guards. On controller resumption, clear
only the obsolete wait whose reason begins Qwen held: bypass; do not clear actual quota
waits, cooldowns or unknown outcomes. Record qwen_bypass_enabled and its approval reference
in status. All calls still pass local CAS reservation/settlement and exact physical route
allowlists. No production routes/quotas, output/input caps, Worker or other experiment edits.

Test Qwen bypass follows local token admission without any Worker request and fails before
submission when tokens are unavailable. Verify actual high/parsed controls and immutable
intents in the common provider-mock test. Run targeted/full offline, whole Ruff/format and
fragment validation before push/resuming. Stop only the active body-selection controller
under the shared call lock, preserving all responses, counters, deadline and allocation.
The older comparison remains stopped; do not restart it.


Qwen release verification:167 targeted checks and6,155 offline tests passed
(16 deselected), whole Ruff/format546 files and fragment validation clean. Tests
verify high/parsed Qwen calls in bypass, real token admission and clearing only the
obsolete hold on resume without resetting the horizon or allowance. Read-only local
preflight permits Qwen; no local Qwen token debt was recorded. Worker production
usage remains unknown, so that preflight is not a claim of unused provider capacity.


Qwen release scheduling clarification before code: the maintainer's request targets
the beginning of the upcoming capacity window. On the first release migration only,
set the existing round-robin next_candidate_index to Qwen, so it gets the first
eligible slot after the active request finishes. Then continue the normal cycle.
Do not repeat this priority on later resumptions, alter shared allocation, bypass
actual token waits or change any attempt allowance. Test initial and repeat resumes.


First-slot verification:169 targeted checks and6,157 offline tests passed
(16 deselected), whole Ruff/format546 files and fragment checks clean. Release
resumption prioritizes Qwen once; repeat resumption preserves normal queue position.
All attempt counters, original horizon and real token waits remain unchanged.


### P1 Groq provider-authoritative stop — L3, approved2026-10-13

Maintainer explicitly directed Qwen and GPT-OSS to continue now until provider429,
then pause them. This supersedes local daily-token reserve/ceiling gates only for
these two models in the current body-selection experiment. Keep free physical
routes, input/output limits, high/parsed controls, RPM/RPD/TPM/concurrency checks,
shared CAS request accounting, one call lock, original horizon and12 attempts/pair.
No production route/catalog/Worker/workflow changes. No calls after all60 answers.

Scope: existing body-selection run.py/groq_routes.py/README,
tests/test_remedy_body_selection.py and lifecycle docs. Add default-off
--groq-until-429, requiring --coordination-bypass and the existing saved run.
Bind a persisted groq_until_429 session to its fixed authorized_at/approval_ref;
resumption never resets this timestamp or a429 pause. Historical429s before that
authorization do not pause this new window. Record mode and provider-authoritative
daily-limit policy in status, immutable intents/results and request-settings hashes.

Evaluation-local Groq route copies set quota.tpd=None only in this explicit mode,
so the shared backend's daily token bucket does not block or erase prior token debt.
Preserve all other route limits. Actual prompt/completion/reasoning usage remains in
immutable results; normal shared request/minute reservations and settlement remain.
Daily token counters do not include new calls while this exception is active, so
they must not be represented as complete provider usage or reconciled implicitly.
Keep earlier counters intact; no production budget edits or paid fallback.

Skip only these models' evaluation-local daily token headroom/reserve tests while
the mode is active. Reevaluate their old daily-quota waits on first activation;
retain real provider cooldowns and unknown outcomes. Give eligible Groq requests
priority while this window remains active, alternating via the existing round-robin
position; other models can use slots when Groq waits or finishes/pauses.

From immutable observations since authorized_at, detect an actual submitted
provider_error.http_status429 and persist that model's paused_after_429 state,
attempt ID and reason. It is a durable evaluation pause, not a production route
pause. Never automatically resume it after cooldown or daily reset. If all remaining
work is complete/held/paused, stop explicitly with an incomplete provider-pause reason.
No unrecognized errors count as429; preserve normal retry handling for other failures.

Tests verify exact two-route scope, preserved non-daily limits, daily gates bypassed,
actual usage/provenance retained, old429 exclusion, first new429 pause across resets
and restarts, and priority without duplicated calls. Targeted/full offline, whole
Ruff/format and fragments before push and safe controller handoff between requests.
The older comparison remains stopped. Future calls after a429 require new direction.


Groq until429 implementation verification2026-10-13:180 targeted and6168 offline
tests passed (16deselected); whole Ruff/format546 files and fragment/diff checks clean.
Coverage includes preserved minute/request settlement and pre-existing daily debt,
exact two-route scope, immutable actual usage, old429 exclusion and durable new429
pauses across restarts. Controller handoff occurs under the shared call lock only
after every intent has a final result. No production route/catalog changes.


### P1 feed-context diagnostic — L3, approved2026-10-14

Maintainer requests a separate one-recording, two-prompt, seven-model diagnostic
(14provider submissions maximum) for Dallas recording270975, August22,2023.
Compare explicit city/source-bound feed-membership policy against representative
existing titles grouped by feed. Keep original evidence, question, response schema,
route reasoning controls and frozen expected three-feed answer unchanged. Gold is
loaded only after calls. This tests publication-context clarity, not independent
institution discovery or production qualification. The existing60-recording study
and Groq429 pauses are unchanged; the new request authorizes at most two diagnostic
submissions per model, including GPT-OSS, without rearming its original study pause.

Scope: new evals/remedy/holdout/feed-context-2026-10-14/{prepare.py,packet.json,README.md}
and evals/remedy/results/feed-context-2026-10-14/{run.py,score.py}; optional-message
parameters on existing body-selection run.py/shaped_messages/attempt; corresponding
tests/test_remedy_feed_context.py and lifecycle docs. Preserve all existing callers
and prompt hashes by default. Reuse current exact seven model routes, schema,
shared call lock, CAS request/minute accounting and approved coordination bypass.
Normal Groq daily-token gates/reserve apply to this separate job; wait for available
capacity rather than modifying budgets or the paused original experiment.
Each pair submits once, no SDK/provider retries, immutable pre-call intent/results,
unknown interrupted outcomes held. Persist a bounded seven-day diagnostic horizon.

Fetch public same-city feed RSS read-only through validate_source_url; retain URL,
retrieval timestamp and hash. Examples are real episode titles, not selector regexes.
Exclude the target UID/providerGUID/title-date entry from examples. Select bounded
representatives by a declared rule independent of model answers; never silently
replace missing histories with invented examples. Retain full title inventory for
size comparison, but send bounded examples. Hash both prompt variants and measure
schema-shaped input tokens/characters per route, showing estimator vs provider usage.
Report exact three-feed match, institutional identity retained for manual review,
missing/extra feeds, completion and latency separately. A local failure is not a
provider call. Whole Ruff/format and targeted/full offline before live submissions.
No production catalog/feed/record/audio/Worker/workflow changes or activation.

Pair-audit retention: use the same seven models excluding Qwen; first intersect
completed recordings across all seven, then score all21 unordered pairs on that
identical set. Agreement means exact unordered feed set plus publication action;
truepositive=agreement matching frozen gold, falsepositive=shared wrong recommendation,
negative=different recommendations. Report counts and percentages plus common-set
category coverage and partial-completion bias; never treat omitted answers as correct.
Retain script, input hashes and per-case results under evals/remedy/. At final scoring
repeat this audit, preserving interim artifacts and separately flagging suspected
publication-policy inconsistencies rather than editing approved truth implicitly.


Feed-context packet preparation: all70 public Dallas RSS feeds returned200. After
excluding the target,5,372 RSS memberships and173 distinct-pattern samples are retained.
Three samples for every feed estimate9,915 schema-shaped tokens, above GPT-OSS7,125.
Use two patterns for six feeds selected by title-token Jaccard similarity, without
gold consultation:6,414 estimated tokens vs6,030 baseline and6,175 explicit policy.
All current RSS titles would cost81,020; that alternative is not sent. Public RSS
caps mean inventories are not a complete historical catalog. Examples can inherit
existing misassignments. This constrained retrieval design is chosen before calls;
it preserves full official evidence and the frozen expected answer for the diagnostic.


Feed-context implementation verification2026-10-14:215 targeted checks (including
structured-shaping regressions) and6189 full offline tests passed,16deselected.
Whole Ruff/format551 files, fragments and diff checks clean. Fixture isolation now
restores all evaluation-local route dictionaries after tests. Fourteen mocked
provider jobs verified exact variant messages, one submission, no SDK retry and
pre-call intent provenance. Original default prompt/shape and Groq pauses remain.


Feed-context diagnostic completed2026-10-14:14submitted,13answers,one fullGLM
title-variant900s timeout, no retries. Explicit policy7/7 exact three-feed matches;
retrieved titles5correct,one Flash-Lite omission of both broader feeds,one unanswered.
Results/intents and immutable scored report are retained under
evals/remedy/results/feed-context-2026-10-14; RESULTS.md includes actual prompt usage
and provider latency. Actual successful inputs5881–6221 tokens with policy and
6236–6517 with titles. This is rule-following vs inference from examples on one known
error, not general accuracy or independent qualification. Existing identity/serving
family admission holds remain. Original60-recording study and its Groq pauses unchanged.
Keep final common-question21-pair audit pending remaining main-study results.


### P1 three-route prompt comparison — L3, approved2026-10-14

Maintainer switches focus to prompt variants for Sol, GLM Flash and DeepSeek V4.1
Flash, with their three pairs. New independent experiment reuses the preserved60
recordings, frozen evidence/schema/gold from PR2413; no committee-specific mapping,
examples, suggested assignments or evaluation answers in live inputs. Three recipes:
original, evidence-first body/grouping separation, self-check inclusions/omissions.
540logical jobs; initial plus at most three retries each (2160submitted maximum),
no SDK retry,30-minute failure cooldown, seven-day saved horizon, valid answers stop
retries regardless correctness; unmatched intents held. Do not restart old lost-state
controllers or rearm old Groq pauses. Shift the existing hourly monitor to this study.

Scope:new evals/remedy/results/prompt-comparison-2026-10-14/{variants.py,run.py,score.py,
README.md}, tests/test_remedy_prompt_comparison.py and lifecycle docs. Reuse existing
body-selection optional request_messages/experiment_mode adapter. Same exact routes,
controls,900s/950s timeout,16384output cap. Normal shared CAS quotas and production
coordination first; existing approved coordination bypass may be selected explicitly
if Worker is unavailable. Never erase accounting or claim production usage is zero.
No catalogs, production routes/quotas, feeds/records/audio, Worker or workflow changes.

Use managed durable prompt-comparison worktree; immutable intents/results, hashed
case/prompt/model-set binding, restart status. Git-commit and push newly completed
batches at bounded checkpoints to preserve them remotely. Never commit runtime locks
or credentials, activate models or merge. Artifact PR2413 stays draft.

Offline scorer alone reads gold. Per prompt, all three pairs use the same common
completed-question intersection with coverage shown. Report correct/shared-wrong
agreements and disagreements. Separately report feed-level FP(extra selected feeds),
FN(missing expected feeds), over-/under-inclusion and action-only mismatches. The
five unknown-owner cases lack established feed truth: score unresolved/unsupported
assignment guardrail separately, not invented institutional negatives. Keep40assignment,
15policy and5guardrail categories separate, with latency/tokens and provenance.
This reused optimization set is not independent qualification; identity holds and
suspected truth/policy mismatches stay explicit; approved truth never changes silently.
Test540jobs/payload isolation, bounded retries/unknown holds, restart binding, pair
and feed metrics, checkpoint preservation; whole Ruff/format and full offline beforecalls.


Prompt-comparison checkpoint detail:push each immutable observation to the dedicated
chore/2413-prompt-observations branch without a PR. This protects completed calls
without triggering repeated PR CI; carry final observations into artifactPR2413 in
a batch. Source is verified before calls; data-only pushes skip optional hooks.


Prompt-comparison verification2026-10-14:125 targeted checks and6201 full offline
tests passed,16deselected. Whole Ruff/format555 files, fragments and diff checks clean.
Read-only Worker and shared-quota preflight confirms all three routes available;
launch uses normal coordination. No provider requests occurred during preflight.
