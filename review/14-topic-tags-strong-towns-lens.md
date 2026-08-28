# review/14 — Topic Tags & Strong Towns Lens (Phase R)

**Maturity: Implemented locally with unified calibration overlay · shadow dispatch rollout 2026-07-16 · matured 2026-07-14, grounded against current `main` · breakout of
[`review/11`](11-technical-design-roadmap.md) Phase R (#4) · ROADMAP R5 · no PR yet**

> Topic tags turn the catalog from "meetings you can listen to" into "meetings you can *track by issue*."
> They are the join key for topic feeds (#12/#13), watchlists + alerts (Phase F), search filters (#6),
> and the "national highlights" reel (Phase E). This adopts Codex R8 largely as-is.

## Principles

1. **Transparent first.** Start with explainable keyword/rule tags from agenda titles + transcripts.
   Every tag carries its **evidence** (matched term + location). No black-box classification at the
   start.
2. **Derived, reproducible, and future-reviewable.** Episode tags are a deterministic projection of
   episode-scope and chapter-scope annotations. R5 does not add manual overrides; a later
   crowd-sourced workflow will submit audited proposals for moderation rather than editing canonical
   records directly.
3. **AI is additive and untrusted** (SECURITY.md). LLM classification is layered on *after* transcripts
   are stable, and only ever **adds** validated candidates with a **confidence + explanation**; it never
   edits official data (titles, dates, votes, links, transcript text). Deterministic and LLM candidates
   share one persisted ledger; the initial 12-review/90%-precision gate controls LLM admission, while a
   separately calibrated pre-labeler can later suppress a likely-incorrect display projection without
   deleting the underlying candidate, rule phrase, or evidence.

## Implemented rollout and calibration behavior (2026-07-16)

R5 now enables the LLM path in [`config/site_config.yml`](../config/site_config.yml), over Gemini's
direct transport (`tagging.llm_mode: direct` — see the R13-migration section below for why; there is no
working `LLM_DISPATCH_URL` for this route). A completed suggestion is never treated as a public tag
merely because it passed JSON/schema validation. The stage stores each validated suggestion in
`Episode.llm_tag_candidates`, then projects only candidates whose confidence clears the generic evaluator
in [`citypods/llm_evaluation.py`](../citypods/llm_evaluation.py) — **full design now written up in
[`review/35`](35-llm-confidence-calibration-human-review.md)**, extracted from this section 2026-07-17
since the module is feature-independent by design, not R5-specific. What follows here is R5's own
concrete wiring; see review/35 for the matrix/admission algorithm, state shape, human-review workflow,
security hardening, and the module's structural limits.

**R5's concrete instance:** feature `topic-tags`, route `litellm:gemini/gemini-3.1-flash-lite`, initial
fallback `1.0` (so ordinary uncalibrated suggestions remain shadow-only), matrix dimensions feature ×
provider/model route × prompt/schema version × taxonomy version × tag ID × episode/chapter scope, human
review decisions in `state/llm_evaluation.json`, 90% required precision, 12 minimum reviews per row
(lowered from an initial 95%/30, 2026-07-17). The
weekly [`llm-tag-review.yml`](../.github/workflows/llm-tag-review.yml) workflow packages the digest and
actionable child issues (bounded quoted transcript region with derived timestamps, or a bounded
agenda/document quote with an allowlisted official document link); the shared
[`review-issue-resolve.yml`](../.github/workflows/review-issue-resolve.yml) workflow ingests trusted
decisions, refreshes the matrix, and makes newly qualified tags visible on the next normal build without
another LLM call. The current
rollout is chapter-only for LLM candidates: episode-level LLM suggestions are ignored, while
deterministic episode/chapter rules remain unchanged. Deterministic candidates remain public until
their pre-labeler overlay qualifies at 50 reviewed examples and 95% precision for both actionable
decisions; qualified likely-incorrect results suppress display only. The weekly packet defaults to 80
items with per-tag/source/scope stratification and reports distance to both gates.

**This is *not* the same mechanism as the still-unbuilt quality tournament/champion routing
([`review/34`](34-llm-quality-tournament-champion-routing.md), `review/27` §6's old home) — see review/34
§7 and review/35 §8 for how the two compose. This module decides whether one candidate is trustworthy at
its own confidence; the tournament would decide which provider is champion for the `tag` verb at all.
They were designed to coexist, not substitute for each other.**

## Post-implementation review hardening (2026-07-17)

A full review of the R5 branch before it merges found and fixed several correctness/integrity gaps:
`merge_persisted()` now restores the tag fields (it silently dropped them, so every normal run
re-tagged and re-dispatched every episode); `chapter_id()` now resolves a served chapter's source
identity by its stamped true position rather than served-list position, which desynced whenever
`remap()` dropped an earlier chapter; the fallback-confidence admission check now requires the
candidate to strictly *exceed* an unreviewed fallback (not just meet it), so a model reporting
exactly `1.0` confidence can no longer bypass calibration; `_transcript_region()` now traces a quote's
exact contiguous span instead of OR-matching its first/last word, which could produce a bogus,
episode-spanning evidence timestamp; and the weekly review workflow's `/llm-ingest` comment trigger
now requires collaborator-or-above `author_association` and a matching issue title (previously any
public commenter could fabricate a calibration review), its matrix jobs are serialized
(`max-parallel: 1`) to stop concurrent runs from clobbering each other's recorded decisions, and
`render_review_body`/`parse_review` no longer let untrusted candidate text (explanation,
document_locator) forge a checkbox decision or spoof the marker a review is parsed from.

Two related gaps are deliberately left open, both now written up fully in
[`review/35`](35-llm-confidence-calibration-human-review.md) rather than described twice: `review/35` §7's
closing note (`ingest_review_body()` trusting edited-issue JSON verbatim, no durable candidate-ledger
cross-check) and `review/35` §9 (the module's `StageContext`/CLI/workflow wiring is still R5-specific
despite the module itself being feature-independent). Neither is what the R13 migration below addresses
(that's about model selection/scheduling, not calibration-key or `StageContext` generality) — aligning
this module's callers to R13's current interface is separate, ongoing follow-up work.

## Migrated onto the R13 LLM-scheduler adapters (2026-07-17)

R5's LLM dispatch now goes through `citypods/compute/llm_policy.py`/`llm_scheduler.py`/
`llm_budget.py` (review/33, R13) instead of a static single-model call. `llm_tag_suggestions()`
attaches an `LLMRequestPolicy(allowed_models=(<configured model>,), allow_paid=False,
purpose="topic-tags")` to the job whenever `ctx.tag_backend`'s storage is CAS-capable (R2
configured); otherwise it omits the policy entirely and the backend takes its pre-R13 static path
unchanged, so local dev/dry runs keep working without R2 credentials.

Two deliberate choices, both revisitable without a design change if the constraints that motivated
them ever loosen:

- **`allowed_models` stays pinned to exactly the one model `config/site_config.yml`'s
  `tagging.llm_model` configures**, rather than left open to the scheduler's full eligible pool.
  R5's calibration matrix is keyed per exact `provider_model` route, with one fallback entry
  configured for that one route; letting the scheduler roam across models (e.g. falling over to
  paid DeepSeek) would fragment calibration effort across separately-unreviewed routes instead of
  deepening review of the one route R5 was designed around. Pinning still gains real value from
  R13: CAS-safe quota accounting across concurrent shards (no more silent Gemini RPM/RPD overspend)
  and a uniform deferred `JobHandle` (retried on `TagsStage`'s own next scheduled run) instead of a
  raw provider error whenever quota is exhausted.
- **`allow_paid=False`, no `deadline_at`.** Matches R5's existing design intent (LLM tags are
  additive/shadow-until-calibrated, deterministic rule tags always cover the gap, and there's no
  urgency) — the same reasoning city discovery's own R13 migration landed on for the same reasons
  (review/33 §5, LLM-SCHED-9). A `deadline_at` would only matter for Gemini's off-peak-preference
  gate, which never fires for a route with no pricing windows (Gemini has none) — so it would be a
  no-op even set, and omitting it keeps the policy legible.

`llm_tag_suggestions()` now returns a 4-tuple (`tags, chapter_tags, dispatched, resolved_model`);
`TagsStage` uses `resolved_model` (falling back to the precomputed `llm_route` if unset) as
`decorate_llm_candidates()`'s `provider_model`, rather than assuming the backend's statically
configured model always matches what the scheduler actually picked — correct today only because
of the pinning above, but no longer a silent assumption if that pinning is ever loosened later.
`config/site_config.yml`'s `tagging.llm_mode` changed from `dispatch` to `direct`: R13's scheduler
gates transport eligibility on `dispatch_url` presence, not `mode`, and Gemini's route is
`transport="direct"` (review/33 §7 — its free tier doesn't need the Mistral Worker's async
pacing); `dispatch` mode was never actually wired to a working `LLM_DISPATCH_URL` for this route,
which is what crashed the build before the fix earlier in this section.

**A real gap found and fixed in R13 itself, not R5-specific:** `scripts/llm_deferred_sweep.py`
constructed its `LiteLLMBackend` but never registered *any* feature's structured-output contract,
so `reconcile()` on a pending "tag" (or "classify-civic-platforms") record would always fail with
`unknown structured-output contract` — caught per-record, logged, but the record would never
actually complete, only eventually TTL-expire after 38 days. Fixed by exposing `citypods.tags.
ensure_llm_contract()` (renamed from `_ensure_llm_contract`, now a public, idempotent entry point)
and having the sweep call it — plus importing `citypods.discovery.classify` for its existing
import-time registration — before reconciling anything, each guarded by `except ImportError` so
the sweep still runs for whichever features' optional extras happen to be installed.

## The taxonomy (Strong Towns lens)

The initial taxonomy is versioned in [`config/taxonomy.yml`](../config/taxonomy.yml), with compact
source identifiers and the full bibliography below. It contains 37 flat retrieval tags:

```
zoning-reform · parking-mandates · minimum-lot-size-setbacks · housing-supply ·
missing-middle-housing · accessory-dwelling-units · affordable-housing · anti-displacement-equity ·
infill-redevelopment · incremental-development · adaptive-reuse · form-based-codes ·
annexation-outward-expansion · neighborhood-planning · road-widening · street-safety-vision-zero ·
walk-bike-transit-access · traffic-calming-road-diets · transit-oriented-development ·
street-trees-green-infrastructure · third-places-public-life · main-street · placemaking-public-space ·
tactical-urbanism · historic-preservation · public-art-culture · parks-recreation ·
accessibility-universal-design · small-business-permitting · community-wealth-local-ownership ·
home-based-business · infrastructure-maintenance-liability · stormwater-utility-maintenance ·
debt-bonds-tif-subsidies · budget-structural-balance · climate-resilience · neighborhood-engagement
```

The grouping in YAML is display/navigation metadata, not a second hierarchy users must combine.
`incremental-development` is intentionally citywide and remains distinct from `main-street`. At the
current roughly 85-feed catalog, 37 active tags is in the useful range: enough coverage for distinct
single-tag retrieval while still small enough that users should usually begin with one tag. There is
no universal tags-per-catalog formula; the annual review should prefer tags that recur across multiple
bodies/cities, have clear query intent, and do not require unions of near-synonyms.

### Source review and annual taxonomy process

The seed was checked against Strong Towns material on walkability, street trees, third places, and
incremental development; Congress for the New Urbanism on missing-middle housing; Better Block on
tactical/interim design; Southern Urbanism on Main Street, walkable neighborhoods, housing, and
third places; Dallas Urbanists as a city-focused Substack example; and related Lean Urbanism,
Incremental Development Alliance, and Project for Public Spaces material. Canonical URLs and the
tag-to-source mapping live in `config/taxonomy.yml`.

Maintenance is intentionally slow: one scheduled review per year, plus an exceptional correctness
fix only when a rule is demonstrably harmful. The annual review freezes a catalog sample, measures
per-tag coverage/co-occurrence and precision samples, revisits every source including sources behind
eliminated tags, maps eliminated concepts to existing tags when user intent is genuinely equivalent,
and proposes new tags only when they are distinct, independently useful, and likely to recur across
the catalog. It then bumps `taxonomy.version`, records alias/replacement decisions, runs a
deterministic archive diff, and lets the next normal enrich cycle backfill gradually. It is not an
annual live web crawl. Future community edits should be moderated proposals with evidence and a
versioned decision; R5 adds no unreviewed inline override field.

Each taxonomy entry: stable `id`, display `label`, short `description`, and a `rules` block (synonyms,
phrases, and negative/guard terms to reduce false positives). The taxonomy is **versioned**
(`taxonomy_version`) so a change can trigger a controlled re-tag (like a stage-version bump). **Loader:**
`config/taxonomy.yml`, one checked-in file, loaded the same way `load_site_config`
(`citypods/config.py:53-56`, `yaml.safe_load` + a plain dict) already loads `config/site_config.yml` —
not the multi-file `load_entity_configs` (`config/cities/*.yml`) pattern, since this is one versioned
document, not one file per entry.

## Critical correction: "agenda text" means chapter titles, not document text (2026-07-14)

**The original draft's `tag_episode(agenda_text, transcript_text, taxonomy)` signature is ambiguous in
exactly the way review/13's search design already had to resolve and fix for itself.** Confirmed: **no
code anywhere in this repo extracts text from agenda PDFs** — `ep.links["agenda"]` is a URL string,
never fetched or parsed by any stage (`citypods/models.py:70`, `citypods/feeds.py:26`). "Agenda text" for
tagging purposes must mean **`episode_served_chapters(ep)`'s chapter titles**, concatenated —
`citypods/chapters.py:9-34` — the same fix review/13 already made explicit for search
(`review/13-per-meeting-pages-and-search.md:387-390`). `episode_served_chapters` isn't raw `ep.chapters`:
it remaps the canonical source-time chapters through the current `Timeline` into served time whenever
real editing (silence-trim/concat) occurred (`chapters.py:16-34`), so it's the same "what's actually
served" semantics `feed_content_hash` already uses it for (`citypods/records.py:766`). **Rename the
parameter in the actual function** to `agenda_item_titles: str` (built as `"\n".join(ch["title"] for ch
in episode_served_chapters(ep))`) so a future reader isn't misled into expecting document content. The
**rules engine** (path 1, below) stays on `agenda_item_titles` only — it ships without depending on real
extraction existing, and chapter titles are a good-enough keyword-matching signal on their own.

**Updated 2026-07-14 — this is no longer a vague "if/when Phase F ships" caveat.** Real agenda-document
text extraction is now **ROADMAP R3** (a minimal, extraction-only slice pulled forward from Phase F's
"Backup-material (packet) analysis," see `review/11` §5.1), sequenced *before* this item. The **LLM-assist
path** (path 2, below) takes real `agenda_text` from R3 as an additional, optional input alongside
`agenda_item_titles` — additive, not a replacement, and gracefully degrading to chapter-titles-only for
any episode where extraction didn't run or failed. The full richer Phase-F "what's being proposed" brief
stays out of scope for both this item and R3.

**Chapter-level agenda caution (2026-07-16).** R3 currently persists a flat agenda sidecar and a
consolidated backup/link manifest; it does not yet guarantee a normalized agenda-item-to-chapter
mapping. R5 therefore treats transcript timing as the reliable per-chapter signal. When a future R3
provider/parser supplies an explicit `chapter_index` plus item text, R5 consumes that mapping as
additional chapter evidence. Link labels, document order, or whole-packet text are not silently
attributed to a chapter because a wrong association is worse than an episode-level tag.

## Data model deltas (exact)

1. **`Episode.tags: list[dict] = field(default_factory=list)`** — the episode-level facet projection,
   inserted alongside the other enrichment-artifact fields (`summary`, `chapters`, ~`models.py:75`).
   It is rebuilt as the taxonomy-ordered union of episode- and chapter-scope annotations, never
   independently hand-maintained.
   Shape unchanged from the L2 sketch:
   ```jsonc
   "tags": [
     {
       "id": "parking-mandates",
       "source": "rule" | "llm",                // human proposals are future scope
       "confidence": 0.0–1.0,                    // rule = 1.0; llm = model score
       "evidence": [{"where": "agenda|transcript", "span": "…matched text…", "t": 1234}],
     }
   ]
   ```
   LLM candidate evidence uses a stricter reviewable shape: `where`, bounded `quote`, optional derived
   transcript `start`/`end`, optional allowlisted `document_url`/`document_locator`, and `chapter_id`.
   The server verifies the quote against the supplied source and derives transcript timing rather than
   trusting model-supplied offsets.
2. **`Episode.chapter_tags: list[dict]`** — per-chapter annotations keyed by a stable source-time
   `chapter_id`, for example `{chapter_id, tags}`. The ID is derived from source chapter
   index/start/title, never served time, so timeline remapping does not move annotations. Transcript
   windows are the reliable chapter-level evidence. When no chapters exist, the episode-scope
   annotation is the explicit virtual fallback.
3. **Serialization** — `episode_to_record` (`citypods/records.py:756-826`): add `"tags": ep.tags or
   None,` beside `"summary": ep.summary,` (`:766`), following the existing omit-when-empty convention
   used for `provider_transcript`/`integrity` nearby. `record_to_episode` (`records.py:926-1019`): add
   `tags=rec.get("tags") or [],` beside `summary=rec.get("summary") or "",` (`:988`), plus the
   `chapter_tags` annotation block and `tags_spec_hash`.
4. **Shadow candidates** — `Episode.llm_tag_candidates` stores validated LLM suggestions separately
   from visible tags, including the generic evaluator dimensions, confidence, explanation, bounded
   evidence quote, admission basis/status, and input recipe hash. `tags_llm_recipe_hash` records a
   completed empty result as well as a non-empty result, so a policy change reprojects candidates
   without recalling the model.
5. **Cross-lane write isolation** — `citypods/records.py:1013-1019`'s own module comment is a literal
   how-to written for exactly this extension (it was left for "the next lane," which is this one): add
   `"tags"` to `ARTIFACT_BLOCKS` (`:1021-1023`) and `"tag": frozenset({"tags"})` to `_LANE_OWNED_BLOCKS`
   (`:1031-1037`), mirroring the reserved `"diarize": frozenset({"speakers"})` entry. This is what lets a
   `TagsStage` write land through the shipped Stage-1 owned-block merge (H17) without a sibling
   audio/transcribe lane push clobbering it — the same mechanism every other lane already uses, not new
   infra.
6. **`feed_content_hash`** (`citypods/records.py:328-355`) — append `e.tags` to the per-episode payload
   list (`:337-352`, currently `[uid, title, published, description, summary, transcript_*, links,
   episode_served_chapters(e), chapters_basis, durations, hosted_audio_url, video_url, media_kind]`). The
   function's own docstring (`:329-334`) already names the exact consequence this causes and frames it as
   expected, not a regression: *"adding a field here changes every feed's hash once, so the first deploy
   after this lands re-renders the whole catalog... like a template-fingerprint bump."* Directly reusable
   for this doc's own migration note below.
7. **`tags_spec_hash`** = `taxonomy_version` + tagger version + input fingerprint
   (`agenda_item_titles` + transcript text fingerprint), so a taxonomy/tagger change re-tags only
   affected records — unchanged from the L2 sketch, now precisely wired to the corrected input above.
8. **Human overrides are deferred.** R5 does not add `tags_override` or an inline lock field. A future
   crowd-sourced workflow should store proposals separately (identity, add/remove request, evidence,
   moderation state, and decision timestamp), then apply an approved versioned annotation. This keeps
   the projection reproducible and gives moderators an audit trail.

## Module / stage plan (exact)

- `citypods/tags.py` — new. Taxonomy loader (`load_taxonomy(path) -> Taxonomy`, modeled on
  `load_site_config`) + pure rules/projection helpers. Episode-scope rules use chapter titles,
  explicitly mapped agenda-item text when R3 supplies `chapter_index`, and the full transcript.
  Chapter-scope rules use the chapter title plus the transcript window between this chapter and the
  next. Flat agenda text or a whole packet is never guessed into a chapter. A no-chapter episode uses
  one virtual episode-scope annotation.
- `citypods/stages.py` — new `TagsStage`, implementing the existing `EnrichmentStage` Protocol
  (`stages.py:438-446`: `name: str`, `version: str`, `process(self, provider, city, episodes, ctx) ->
  StageStats`). `name = "tags"`, `version = "1"`. **Ordering**: inserted after `TranscriptStage`/
  `ProviderTranscriptDiarizeStage`, alongside `LinksStage` in the feed-only stage cluster (`default_stages()`/
  `enrich_stages()`, `stages.py:3033-3085`) — exact position relative to `LinksStage` doesn't matter,
  confirmed neither stage affects the other's inputs. **Skip logic**: modeled on `LinksStage`'s
  value-diff check (`stages.py:1220-1223`), not a version-hash comparison — recompute tags, compare
  against the stored value, only write+bump `feed_content_hash`-relevant state if they actually differ.
  Emits **agenda-only tags immediately** when no transcript exists yet (`agenda_item_titles` alone is
  enough for a first pass), emits chapter annotations whenever chapters exist, and re-tags with
  transcript windows once `TranscriptStage` has run. `Episode.tags` is recomputed from those
  annotations in taxonomy order, so the union cannot drift.
- **Lane registration**: add `"tag": frozenset({"tags"})` to `LANE_STAGES` (`stages.py:3092-3097`),
  mirroring the reserved `"diarize"` entry. The module comment at `stages.py:3086-3091` already
  anticipates this ("gains an entry there, not in run.py").
- **Phase placement**: `enrich_stages()`/`default_stages()` only, **not** `render_stages()`
  (`citypods/run.py:1264`). `LinksStage` is safe in the cheap `render_stages()` phase because it's a
  trivial dict diff; a rules-engine pass over transcript text is comparably closer to `TranscriptStage`'s
  cost class than `LinksStage`'s, so it belongs with the heavy-phase stages, matching where
  `TranscriptStage` itself already sits (also enrich-only, confirmed not in `render_stages()`).
- **The H5 global-queue partition** (`_run_enrich_global_queue`, `run.py:933-940`) splits stages into an
  `audio_stages` bucket (`s.name != "transcript"`) that runs *before* the decoupled transcript pass, and
  a `transcript_stages` bucket that runs after. A stage named `"tags"` falls into the *first* bucket by
  that condition — **confirmed not a bug, not something to fix**: `TagsStage`'s own no-transcript-yet
  skip/agenda-only behavior (above) is exactly what makes running early in that pass safe — it either
  emits agenda-only tags (first pass, no transcript yet) or reuses whatever transcript already existed
  from a *prior* run (not this run's transcript work, which is fine — the pipeline's existing
  eventual-consistency model already has this shape everywhere, e.g. diarization picking up ASR output
  from a previous run). No special-casing needed in `run.py:933-940`.
- **Human review is calibration scope, not manual tagging**: the weekly workflow reviews individual
  LLM candidates with source evidence and records correctness decisions. Reviewers never edit canonical
  tags directly; the resulting matrix changes automatic admission policy. Future crowd-sourced edits
  remain separately moderated proposals.
- `citypods/llm_evaluation.py` — generic evaluator for exact calibration keys, provider/model fallbacks,
  confidence admission, durable human decisions, sparse-matrix selection, and evidence-rich Markdown
  issue packaging. Full design in [`review/35`](35-llm-confidence-calibration-human-review.md). Future
  `summarize` and `soundbite-select` features *could* use the same module without adding feature-specific
  confidence logic to `Episode` or `TagsStage` — but only once review/35 §9's wiring gap is closed;
  today's `StageContext`/CLI/workflow integration is still R5-specific.

## Implementation paths

1. **Rules-only (ship first, ~$0).** Keyword/phrase rules over agenda titles + transcript, with guard
   terms. Transparent, cheap, good recall on agenda titles; moderate precision on transcript prose.
2. **+ LLM-assist (implemented, dispatch + calibrated visibility).** After rules, an LLM pass proposes
   additional tags with confidence, explanation, and bounded source evidence. The result is untrusted,
   additive, cached by its input recipe, and clearly labeled. **Not greenfield —
   the `tag` task verb is already reserved** in the H13 compute-backend interface's `Task` `Literal`
   (`citypods/compute/base.py:28-35`, shipped, pre-1.0-locked), alongside `summarize`/`soundbite-select`,
   specifically so "the R2 LLM-API adapter... slots in with no interface change" (module docstring,
   `base.py:13-16`, updated 2026-07-14 when R2 was inserted). **The adapter itself is built at R2**
   (dedicated infra item, ahead of this item); R5 is the first *feature* caller of the `tag` verb against
   that already-working adapter, per `review/11`. **Inputs, updated 2026-07-14 — this path gets a richer
   input than the rules engine, not the same one:** the rules engine (path 1, above) uses
   `agenda_item_titles` (chapter titles) because that's all that exists without structured item mapping.
   Once **R3** (agenda text extraction, inserted 2026-07-14, ahead of this item) supplies an explicit
   item mapping, the LLM path additionally
   takes the **real extracted agenda-document text** — richer than chapter titles, and exactly the kind
   of input an LLM pass is positioned to make good use of where a keyword rules engine could not. This is
   an `InferenceJob(task="tag", inputs={agenda_item_titles, agenda_text, transcript_text,
   taxonomy_version, chapters[]}, recipe_hash)` call through `Backend.run_inference` — one bounded
   request may return both episode-scope and `chapter_id`-scoped suggestions. `agenda_text` is optional
   (`None` until R3 ships or for providers where extraction fails) and purely additive to
   `agenda_item_titles`, never a replacement, so the LLM path degrades gracefully to the same input the
   rules engine already has if real extraction isn't available for a given episode. `recipe_hash` must
   fold in the prompt version + model route per review/11's LLM-verb convention, so a prompt or model
   change re-derives cleanly.
   The implementation calls the shipped R2 adapter through the existing `tag` verb. Dispatch handles
   remain deferred safely; the recipe hash is resubmitted idempotently until a terminal result is
   available. Completed results are stored as shadow candidates first, then admitted automatically only
   when the generic calibration matrix or its configured feature/route fallback allows them.
3. **Embedding/zero-shot classifier.** A local embedding model scores each taxonomy entry per meeting —
   no API cost, more infra. Consider only if (2)'s API cost or (1)'s precision proves limiting.

**Lean: (1) plus the calibrated additive layer.** Rules remain the always-visible baseline; dispatch
LLM work can accumulate without making unquantified model output public.

## Surfaces that consume tags

- **Search filters** (#6, review/13) — facet results by tag.
- **Meeting pages** (review/13) — show episode facets plus chapter-level topic labels, each with a
  direct seek target and future transcript-highlight context.
- **Topic / region roll-up feeds** (#12/#13, Phase E) — "all `parking-mandates` items in TX."
- **Watchlists + alerts** (Phase F) — match upcoming agenda items against watched tags, retaining the
  item/chapter scope when the source provides it rather than alerting only at meeting granularity.
- **National highlights** (Phase E) — select clips by topic and prefer the bounded chapter transcript
  window for quote/highlight extraction.

## Migration / backfill

`feed_content_hash` gains `tags` (§ Data model deltas #4) — per that function's own docstring, this
re-renders the whole catalog once on the first deploy after this lands, the same expected one-time cost
a template-fingerprint bump already causes. No dedicated backfill workflow: `TagsStage` runs as part of
the normal enrich phase and tags every retained episode over the following scheduled runs (agenda-only
first pass for episodes without a transcript yet, full pass once transcripts exist), the same gradual
pattern H12's version-aware re-transcribe already established — not a special-cased bulk job.

LLM jobs follow the same gradual schedule and the initial LLM contract is chapter-only: legacy
episode-level LLM rows remain as hidden historical evidence while episodes with usable chapters are
tagged lazily. Results are stored as shadow candidates. **Precisely, since
`tag_recipe_hash()` computes two different hashes for two different gates:** the *dispatch* gate
(`llm_recipe`, used for `ep.tags_llm_recipe_hash`) never includes the admission policy, so a policy
change alone never re-triggers a vendor call — cached candidates are reused as-is. The *cache-skip* gate
(`tags_spec_hash`, `tag_recipe_hash()`'s `admission_policy` parameter) *does* include it, so a policy
change invalidates that hash and every episode's rule-tagging/re-projection step reruns on the next
build (cheap — no vendor call, just re-deriving rule tags and re-merging cached candidates through the
new threshold) rather than being skipped via the usual `ep.tags_spec_hash == projection_hash` check. The
weekly review state is durable and independent of the episode record lane, so human decisions survive
concurrent source-scoped builds.

## Tests

`tests/test_tags.py`:
- Rules engine (`tag_episode`) is deterministic and offline (fixture inputs, no network, no I/O).
- A fixture with `agenda_item_titles` containing "variance for reduced parking ratio" tags
  `parking-mandates` with evidence — built from `episode_served_chapters`-shaped fixture data, not a
  hand-written string, so the test exercises the corrected input contract, not the old ambiguous one.
- Guard terms suppress a known false positive.
- A fixture with only `agenda_item_titles` (no transcript) still produces agenda-only tags; the same
  fixture with transcript text added produces a superset, not a replacement.
- Chapter fixtures assign transcript evidence to the correct chapter window, produce stable
  source-time chapter IDs, and preserve a deterministic taxonomy-ordered episode union.
- A no-chapter fixture produces a virtual episode-scope annotation and a valid episode tag projection;
  it never fabricates a chapter.
- LLM path is mocked (no network in CI) and only **adds** tags with confidence/explanation; asserts a
  `recipe_hash` change (prompt/model/chapter input) is detectable and re-derives, including chapter
  suggestions in one bounded request. Evidence tests require bounded quotes, derived transcript timing,
  and allowlisted agenda-document links.
- `tests/test_llm_evaluation.py` covers the 1.0 fallback, sparse exact matrix qualification, automatic
  admission after human review, and evidence-rich issue parsing/rendering.
- `feed_content_hash` changes when, and only when, `tags` changes (mirrors R1's per-field hash test
  pattern).
- `TagsStage` correctly registers under the `"tag"` lane in `LANE_STAGES` and is excluded from
  `render_stages()`.
- `ARTIFACT_BLOCKS`/`_LANE_OWNED_BLOCKS` round-trip: a `tags` block write survives a concurrent
  sibling-lane merge without being clobbered (mirrors the existing owned-block merge tests for
  `audio`/`transcript`).

## Risks

- **The corrected `agenda_item_titles` input is a materially weaker signal than "real agenda text" would
  be** — chapter titles are short and not every meeting has rich ones. Acceptable for a rules-first
  launch (matches what search (R4) already ships with for the same reason), but don't oversell precision
  expectations against this input until Phase F's real document extraction exists.
- **Agenda association can be wrong if inferred.** R5 uses explicit R3 chapter mappings only; flat
  agenda/packet text remains episode-level context. Transcript timing is the safe fallback.
- **Model confidence is not calibration.** A syntactically valid, grounded suggestion can still be
  semantically wrong. The feature/route fallback starts at 1.0, exact rows require human verification,
  and the weekly digest prioritizes sparse rows and threshold-boundary candidates.
- **`TagsStage` in the `audio_stages` bucket of the H5 global queue is confirmed safe, not confirmed
  cheap** — running an agenda-only tag pass on every episode on every run (even ones that already have
  final tags) adds a real, if small, per-episode cost; the value-diff skip check (§ Module/stage plan)
  needs to be genuinely cheap (a hash/equality check, not a re-run of the rules engine) or this compounds
  at scale the same way any un-cached per-run stage would.

## Sequencing & dependencies

Depends on transcripts (shipped), R2's LLM backend (shipped), and R3's bounded agenda/document sidecars
(shipped). Per-meeting pages and search (review/13, R1/R4) consume the visible projection. The generic
evaluator is intentionally a shared dependency for future R6 summaries and soundbite selection, not a
tag-specific side path. R5 precedes topic feeds/watchlists and R6's "what changed" cards, which use
chapter IDs, evidence windows, and calibrated topic chips.

## Acceptance

Meetings carry transparent, evidence-backed topic tags from agenda-item titles/transcripts; episodes
without a transcript still receive deterministic agenda-only tags; dispatch LLM results are retained as
shadow candidates with bounded evidence; the generic calibration matrix and 1.0 feature/route fallback
control visibility; human review changes admission automatically without manual tag overrides; and tags
drive search, meeting-page, chapter, and future quote/highlight surfaces.

## Implementation worklist (local, no PR yet)

1. `citypods/tags.py` / `config/taxonomy.yml` — taxonomy loader, deterministic rules, structured
   Instructor/Pydantic suggestions, and bounded source evidence.
2. `citypods/llm_evaluation.py` — reusable calibration matrix, fallbacks, admission, state, and review
   issue contract. See [`review/35`](35-llm-confidence-calibration-human-review.md) for the full design.
3. `TagsStage` plus record/lane/search/page integration — visible projection plus shadow candidates.
4. Weekly `llm-tag-review.yml` / ingest workflow — sparse-matrix human calibration.
5. Future moderated proposal storage for community/human tag corrections; deliberately not part of R5.
