# 51 — Unexpected-body remedy: complete coverage, bounded decisions

**Status: P1 tooling shipped; inactive publication selection L3; activations and P2–P5 gated.**
**Revised:** 2026-10-03 after maintainer approval of the remaining evaluation design choices.
This specification does not itself change production routing or enable auto-merge.

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
coverage/quality checks even when only one candidate is available; lack of an independent reviewer
means a policy hold, not relaxed diversity. Shared JEV reliability may be measured by review/49,
but remedy-specific claim accuracy still needs this lane's verified truth.

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

### P0 publication selection — inactive machinery L3; activations L2

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
**Other city activations and automatic future-member admission: L2.**
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

Implementation for #1997 is under validation: parser/index/proof checks, feed/search projection,
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
winner/proof packet; unresolved Foundation exposure and Addison agenda date remain held.

### Foundation historical activation — L3, implementation under validation

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

### P1 — evaluation harness and physical-route admission (L3)

Tracking issue: [#1979](https://github.com/BashfulBits/city-meeting-podcasts/issues/1979).
Implementation: [PR #1987](https://github.com/BashfulBits/city-meeting-podcasts/pull/1987),
ready for review, pending merge. Live comparison/admission remains a separate opt-in activity.
P1 is ready for review, not complete or frozen. The offline harness and strict direct
routing/provenance are implemented, with empty shadow admissions and no live model calls.
The maintainer resolved the three specification clarifications: hidden independently adjudicated
`expected_owner` truth, explicit blind-owner isolation, and mandatory frozen input hashes for
all evaluation candidates are implemented requirements. Current route effort capability metadata is
unverified, so no candidate/admitted route is enabled. Live evaluation requires shared CAS-capable
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
model family, reasoning
level, effective request-parameter fingerprint, AA version/variant/score/source/date, manifest/gold/
prompt/schema/catalog hashes and reviewed result paths. Candidate entries may omit result hashes/paths only when
`for_evaluation=True`; they still require the free/direct, AA, physical identity and effective-effort
checks. Live evaluation records candidate status and cannot publish or qualify changes. Production
requires `admitted`, complete version-matched results and the reviewed admission reference. This
avoids requiring a model comparison before a candidate can participate in that comparison.
Unknown effort or model identity fails
admission. Role names map to `audit-remedy-proposer`, `audit-remedy-reviewer` and
`audit-remedy-adjudicator`; `audit-remedy` remains the protected purpose for quota admission.

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

New immutable B2 events: `state/remedy/events/<decision_id>/<event_id>.json`.
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

### P3 — diverse reasoning review and bounded escalation

**Gate:** P1 reports support at least one admitted proposer and a reviewer from another family;
P2 policy/evidence/ledger contracts are implemented. Missing either route keeps production manual.
**Permitted files:** new `citypods/remedy_review.py`, `citypods/audit_remedy.py`,
`scripts/remedy_unexpected_bodies.py`, `config/remedy.yml`, existing remedy tests,
new `tests/test_remedy_review.py` and lifecycle docs. No Worker or shared judge API invented here.

New `ReviewDecision` contract: decision ID, independently selected owner/action, supported/unknown,
policy ID, evidence refs, conflicts, alternatives, missing evidence and critical-error class.
New `ReviewedDecision`: proposal, blind verdict, comparison, optional adjudicator result, role/route/
effort provenance and disposition `pr_candidate|issue_candidate|policy_hold`.

`review_decisions(evidence, *, admissions, backend, deadline_at) -> list[ReviewedDecision]` uses
existing `classify_unexpected_bodies` for proposer generation with P1 strict routing, then a separate
blind reviewer job using the same input-only evidence and approved policies. The reviewer cannot
see proposer rationale, verdict or selected owner. Compare independently derived decisions locally;
retain both. Agreement on an unsupported identity still fails deterministic validation.
One adjudicator sees both claims and cited evidence after disagreement. It is a third model family
with approved stronger reasoning baseline, not a second account of either model. If unavailable or
still uncertain/disagreeing, make an issue candidate; never repeat semantic escalation.
Existing bounded schema repair is distinct from semantic escalation and must be counted/reported.

Remove the three-recording eligibility rule from prompt and `_rejection_reason`; verified sparse
bodies are legitimate. A dated alias uses a proven rule; unknown sparse bodies abstain. Replace
city/topic token acceptance with P2 ownership and replay checks. Refresh feed context between
accepted family groups; unmerged proposals are labelled pending, never treated as approved policy.

All role calls remain direct/same-run and free, using existing quota/deadline accounting. Exhaustion
is `policy_hold`, not a below-floor fallback or paid call. The report identifies missing capacity,
identity evidence and disagreement separately. Update `REMEDY_VERSION` to reflect prompt/schema
changes only; state explicitly that no audio/stage pipeline version or catalog backfill is changed.

JEV is optional shadow work. Until review/49 exposes a merged documented dispatch judgment API
and an admitted remedy-specific claim evaluation, record `shadow_unavailable` and do not submit
JEV jobs. A direct client copied from the pilot is forbidden. The later JEV adapter needs its own
scoped spec naming that real API and `BEATAPI_API_KEY` dispatch credential integration; it cannot
block or authorize feed mutations in P3. This is an explicit dependency gate, not an open choice
for the implementing agent. No automatic JEV promotion is authorized.

P3 tests: blind input has no proposer data; different actual families, even under logical aliases;
missing evidence despite agreement; incorrect district/city; unavailable stronger route; exactly one
semantic escalation; transport/schema failure; explicit uncertainty; sparse legitimate body;
quota exhaustion; optional shadow absence changes no accepted outcome.
Exit: independent verdicts and exact grounded evidence precede every PR candidate.

### P4 — bounded PRs, issue refinement and recovery

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
