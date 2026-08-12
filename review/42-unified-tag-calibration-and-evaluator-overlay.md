# review/42 — Unified Tag Calibration and Evaluator Overlay

**Maturity: Implemented and corrected in the current change · R5 chapter-only tagging, unified
candidate review, independent pre-labeler overlay, bounded benchmark, and reusable evaluator engine ·
authored 2026-08-11**

## Decision

R5 uses one persisted tag-candidate ledger and one weekly human-review workflow for deterministic
rule matches and LLM tagger candidates. The existing tagger admission gate remains the first public
gate: an exact chapter/tag/model row qualifies at 90% precision with at least 12 human-reviewed
examples. The independent pre-labeler does not delay that first admission. After its own exact row
reaches 50 human-reviewed examples and 95% precision, it becomes a display overlay for that source,
tag, scope, and route: likely-correct candidates remain visible, likely-incorrect candidates are
suppressed without deletion, and uncertain candidates remain visible while entering continued human
sampling.

The persisted field `Episode.llm_tag_candidates` remains the compatibility ledger even though entries
may now have `source_kind: rule` or `source_kind: llm`. No second rule-audit or pre-labeler ledger is
introduced. Existing LLM-only entries and evaluation state remain readable.

LLM tagging is chapter-only for this rollout. Deterministic rules retain their existing episode and
chapter behavior. Existing candidates and rule evidence are re-projected when policy changes; a
chapter-boundary, taxonomy, content, or recipe change creates the normal gradual work for the
affected material. The chapter-only migration therefore does not force a blanket catalog recall,
but old episode-level LLM rows are retained as hidden historical evidence while usable chapters are
tagged lazily.

## Existing implementation review and extension points

The implementation already supplies the main primitives:

- `TagsStage` runs rules, dispatches the optional LLM tagger, and projects visible tags.
- `citypods/tags.py` validates taxonomy IDs, evidence quotes, transcript timing, chapter IDs, and
  allowlisted document links. The matcher uses manually authored literal include/exclude phrases from
  `config/taxonomy.yml`; it does not require the full tag label.
- `Episode.llm_tag_candidates` separates persisted candidates from visible `tags` and `chapter_tags`.
- `citypods/llm_evaluation.py` owns matrix keys, thresholds, review identities, refresh, admission,
  weekly selection, issue rendering, and safe issue parsing.
- `citypods/llm_tag_review.py` and the two GitHub workflows package and ingest native sub-issues while
  serializing state writers.
- `citypods/tournament.py` supplies a reusable structured pairwise judge contract, blind provenance,
  order swapping, independent-route validation, stable comparison identities, and durable result
  envelopes. R5 is one chapter-tag caller; R6 can supply a different task and freeform payload.

The correction pass also closes the operational edges: legacy episode-level LLM rows remain in the
ledger as hidden historical evidence, persisted display is re-projected rather than sticky, prompt
versions are part of evaluator identity, pre-labeler audit identities are stable across retries,
evaluator calls are greedily context-budgeted into batches, empty/deferred/failed calls retain
compact provenance, deterministic include/exclude hits are observable, and render-only jobs never
initialize an LLM backend or need LLM secrets.

## Unified candidate and display model

Every ledger entry identifies:

- `source_kind`: `rule` or `llm`;
- `id`, `scope`, and `chapter_id` when applicable;
- bounded evidence and source location;
- tagger model/provenance for LLM entries;
- matched phrase and rule version for rule entries;
- tagger admission (`not_applicable`, `shadow`, or `admitted`);
- pre-labeler route, prompt version, decision, confidence, and evidence;
- derived `display` state.

The raw candidate and raw pre-labeler decision are retained. Human corrections are recorded as
separate review decisions/overrides and never overwrite model output.

LLM display is initially controlled by the tagger's 12/90% matrix. Once the pre-labeler row is
qualified, `likely_correct` displays, `needs_human_review` retains the prior display state and is
sampled, and `likely_incorrect` is suppressed. Failed, malformed, or deferred evaluator work never
suppresses a candidate.

Rule entries display before their pre-labeler overlay qualifies. After qualification, the same
likely-correct/uncertain/likely-incorrect overlay applies. Suppression changes only the display
projection; the rule match, phrase, and evidence remain persisted. Taxonomy files are never edited
automatically by model output. The pre-labeler's calibrated confidence threshold is reported for
diagnostics, but the overlay is explicitly decision-only; confidence does not become a hidden second
gate. A human override is stored as a separate review row.

## Chapter context and model limits

LLM tagger context prioritizes taxonomy definitions/exclusions, chapter title, mapped agenda
evidence, chapter transcript segments, bounded surrounding context, and explicitly mapped backup
evidence. It should retain the richest safe context and record token estimates, output budget,
context ceiling, truncation policy/version, truncation status, and final input digest.

The pre-labeler may use a smaller payload: proposed tag definition, chapter title, tagger explanation
and evidence, bounded transcript surroundings, mapped agenda evidence, and only explicitly linked
backup excerpts. Whole backup packets are not sent to either model. Its requests are greedily split
against the configured route context/TPM ceiling; an individually oversized subject is deferred with
`payload-too-large` and never silently truncated. Every batch records its token estimate, route limit,
batch index, truncation policy, and input digest. Payload-too-large remains distinct from ordinary
quota deferral.

Every LLM suggestion must be chapter-scoped and have a valid `chapter_id`. Missing chapters skip
LLM tagging; deterministic tags remain available.

## Evaluation and weekly review

The existing evaluation state gains an assessment dimension distinguishing tagger admission from
pre-labeler overlay. Matrix identity includes source kind, assessment kind, candidate/evaluator
routes, prompt versions, taxonomy version, tag ID, and scope. Old rows default to the existing tagger
assessment.

The reusable discrete evaluator returns:

```json
{
  "decision": "likely_correct | needs_human_review | likely_incorrect",
  "confidence": 0.0,
  "reason": "...",
  "evidence_supported": true
}
```

It reviews both rule matches and LLM candidates. Pre-labeler rows qualify at 50 reviewed examples,
95% likely-correct precision, 95% likely-incorrect precision, and no more than 5% likely-correct
false positives, with at least five human-reviewed examples supporting each actionable branch. The
human remains the ground truth; pre-labeler outputs never count as human reviews.

The weekly batch defaults to 80, configurable to 100, with stratified allocation and a hard cap of
eight reviews per source/assessment/tag/scope stratum (configurable):

- 50% unqualified/near-qualified tagger rows;
- 25% unqualified/near-qualified pre-labeler rows;
- 15% rare or low-volume tags;
- 10% post-admission monitoring and deterministic audits.

The parent report shows, per tag, reviewed counts, precision, threshold, reviews remaining, precision
gap, candidate volume, display status, pre-labeler progress, overlay decisions, deterministic audit
counts, observed authored include matches, observed exclude hits, disagreement phrases, and
suppression counts. Historical rows are excluded from active selection and generic visibility.

## Reusable pairwise evaluator

The pairwise engine is task-agnostic while preserving blind candidate provenance, order-swapped
comparisons, independent judge routes, durable results, singleton model allowlists, bounded deadlines,
quota accounting, and human-approved routing changes. The R5 route is explicitly chapter-only: each
comparison uses the same chapter and taxonomy with Model A's chapter tags versus Model B's chapter
tags. The judge route is outside the R5 contestant set, and the call rejects accidental overlap.

For R5, the pairwise question is which model's tag set is better supported by the same chapter and
taxonomy. Pairwise results select a candidate model route but do not replace per-tag 12/90% admission.
For R6, the same engine compares freeform outputs such as two summaries for the same agenda item.

## Bounded benchmark execution

The benchmark is a separate shadow artifact, not another admission database. The manual
`.github/workflows/r5-benchmark.yml` workflow invokes `citypods.r5_benchmark` and persists
`r5_tag_benchmark.json` beside the durable state. It freezes 200–300 real chapters using three
strata (multi-rule/difficult, deterministic rule match, and no-rule match), then runs the candidate
taggers and the independent pre-labeler over the same chapter payloads. The default taggers are
Gemini 3.1 Flash Lite, Gemma 4 26B, and GLM 4.7 Flash; Gemma 4 31B is kept independent as the
pre-labeler. Mistral Small 2603 can be added explicitly.

The workflow never writes episode tags or `llm_evaluation.json`. Its artifact includes structured
validity, latency, token/truncation call metadata, provider errors/pending/quota/payload status,
per-tag precision/recall after human labeling, evidence fidelity, pre-labeler actionable
precision/abstention by source kind, deterministic agreement, and model disagreement. The optional
pairwise sample requires a judge route outside the candidate set and retains both presentation
orders.

After the run, `package` emits bounded Markdown ground-truth packets plus a labels template. A
maintainer reviews the packets, then `ingest --review-body-file ...` or `ingest --labels-file ...`
records labels into the benchmark artifact and recomputes metrics. A run resumes only when its model,
tagger/evaluator prompt versions, chapter pipeline, and frozen sample all match; prompt changes start
a new execution run while retaining the old run for audit. `execution_complete`,
`human_review_complete`, `benchmark_complete`, and `route_selection_eligible` are explicit state
fields. Pending/error calls or incomplete human labels can be packaged for continuation but can never
be treated as a completed benchmark or select a production route. After reviewing the report, a
maintainer must run the explicit `approve --actor ...` operation; only the matching approved sample
digest/run can set `route_selection_eligible`. The benchmark itself cannot alter routing.

## Model policy

Gemini Live is excluded from the structured batch path because the documented Live model is a
persistent real-time multimodal/WebSocket model and does not support structured outputs. Gemini 3
Flash Preview is excluded from high-volume R5 tagging because of its account limit. The initial
benchmark covers Gemini 3.1 Flash Lite, Gemma 4 31B, Gemma 4 26B, GLM 4.7 Flash, and optionally
Mistral Small 2603. The highest-quality OpenRouter capacity is reserved for R6 and future public
verbs unless R5 benchmark evidence justifies otherwise.

No route is promoted solely from a static `free` flag. Observed RPM, TPM, RPD, latency, failures,
structured-output validity, and reset behavior are recorded. A 200–300 chapter shadow benchmark
measures structured validity, precision/recall, evaluator precision, abstention, evidence fidelity,
truncation, token use, latency, quota behavior, and disagreement with deterministic rules.

## Rollout, compatibility, and backfill

1. Persist unified rule/LLM candidates and keep existing display behavior.
2. Enforce chapter-only LLM outputs and add token-aware provenance.
3. Increase and stratify the weekly review packet.
4. Freeze at least 200 real chapters and run the shadow benchmark workflow. Package its human
   packets, review every sample, ingest all labels, and confirm execution and human-review
   completion before considering any route recommendation.
5. Run the pre-labeler in shadow mode and benchmark candidate routes.
6. Admit tagger rows at 12/90%.
7. Enable qualified per-source pre-labeler overlays at 50/95%.
8. Continue sampling uncertain and automatically accepted/rejected results.

Existing records load through the legacy `llm_tag_candidates` shape. The TagsStage version bump
backfills the unified ledger and re-projects stored candidates; existing evaluation state remains
valid. Policy-only changes re-project stored candidates without recalling the vendor. Chapter,
taxonomy, content, or model/recipe changes trigger ordinary gradual work for affected records; the
chapter-only migration lazily creates chapter calls for old episode-level rows rather than forcing a
blanket catalog recall. Superseded and legacy episode-level LLM rows are retained as hidden
historical ledger entries rather than deleted, so evidence and review identity survive a chapter-only
or recipe transition.

The production secret boundary is explicit: `.github/workflows/tag.yml` supplies primary/secondary
Gemini keys plus dispatch URL/auth secrets for the configured tag lane. Build & Deploy's `Render
feeds` step supplies storage/proxy secrets only; `run.py` skips LLM backend construction when
`phase == render`, so render-only publication does not require an LLM dispatch URL or report a
misleading rules-only tagging fallback.

The implementation updates `review/11`, `review/14`, `review/34`, `review/35`, `ROADMAP.md`,
`CHANGELOG.md`, and `ARCHITECTURE.md` as required by the repository lifecycle contract.
