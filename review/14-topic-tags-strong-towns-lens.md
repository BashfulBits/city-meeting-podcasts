# review/14 — Topic Tags & Strong Towns Lens (Phase R)

**Maturity: L3 · matured 2026-07-14, grounded against current `main` · breakout of
[`review/11`](11-technical-design-roadmap.md) Phase R (#4) · ROADMAP R5 · issues not yet cut, batch
review pending**

> Topic tags turn the catalog from "meetings you can listen to" into "meetings you can *track by issue*."
> They are the join key for topic feeds (#12/#13), watchlists + alerts (Phase F), search filters (#6),
> and the "national highlights" reel (Phase E). This adopts Codex R8 largely as-is.

## Principles

1. **Transparent first.** Start with explainable keyword/rule tags from agenda titles + transcripts.
   Every tag carries its **evidence** (matched term + location). No black-box classification at the
   start.
2. **Human-correctable.** Tags are editable/lockable via config/state; a human override always wins and
   is never overwritten by a later automated pass.
3. **AI is additive and untrusted** (SECURITY.md). LLM classification is layered on *after* transcripts
   are stable, and only ever **adds** tags with a **confidence + explanation**; it never edits official
   data (titles, dates, votes, links, transcript text) and never silently removes a human/rule tag.

## The taxonomy (Strong Towns lens)

Seed taxonomy (versioned, in a checked-in file so changes are reviewable):

```
zoning-reform · parking-mandates · minimum-lot-size-setbacks · housing-supply ·
accessory-dwelling-units · annexation-outward-expansion · road-widening ·
street-safety-vision-zero · walk-bike-transit-access · infrastructure-maintenance-liability ·
debt-bonds-tif-subsidies · downtown-incremental-development · small-business-permitting ·
stormwater-utility-maintenance · budget-structural-balance
```

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

## Data model deltas (exact)

1. **`Episode.tags: list[dict] = field(default_factory=list)`** — new field, `citypods/models.py`,
   inserted alongside the other enrichment-artifact fields (`summary`, `chapters`, ~`models.py:75`; the
   comment there — *"Enrichment artifacts populated by later stages"* — should be updated to name tags).
   Shape unchanged from the L2 sketch:
   ```jsonc
   "tags": [
     {
       "id": "parking-mandates",
       "source": "rule" | "llm" | "human",      // provenance
       "confidence": 0.0–1.0,                    // 1.0 for human; rule = fixed; llm = model score
       "evidence": [{"where": "agenda|transcript", "span": "…matched text…", "t": 1234}],
       "locked": false                            // human-locked tags are immune to re-tagging
     }
   ]
   ```
2. **Serialization** — `episode_to_record` (`citypods/records.py:756-826`): add `"tags": ep.tags or
   None,` beside `"summary": ep.summary,` (`:766`), following the existing omit-when-empty convention
   used for `provider_transcript`/`integrity` nearby. `record_to_episode` (`records.py:926-1019`): add
   `tags=rec.get("tags") or [],` beside `summary=rec.get("summary") or "",` (`:988`).
3. **Cross-lane write isolation** — `citypods/records.py:1013-1019`'s own module comment is a literal
   how-to written for exactly this extension (it was left for "the next lane," which is this one): add
   `"tags"` to `ARTIFACT_BLOCKS` (`:1021-1023`) and `"tag": frozenset({"tags"})` to `_LANE_OWNED_BLOCKS`
   (`:1031-1037`), mirroring the reserved `"diarize": frozenset({"speakers"})` entry. This is what lets a
   `TagsStage` write land through the shipped Stage-1 owned-block merge (H17) without a sibling
   audio/transcribe lane push clobbering it — the same mechanism every other lane already uses, not new
   infra.
4. **`feed_content_hash`** (`citypods/records.py:328-355`) — append `e.tags` to the per-episode payload
   list (`:337-352`, currently `[uid, title, published, description, summary, transcript_*, links,
   episode_served_chapters(e), chapters_basis, durations, hosted_audio_url, video_url, media_kind]`). The
   function's own docstring (`:329-334`) already names the exact consequence this causes and frames it as
   expected, not a regression: *"adding a field here changes every feed's hash once, so the first deploy
   after this lands re-renders the whole catalog... like a template-fingerprint bump."* Directly reusable
   for this doc's own migration note below.
5. **`tags_spec_hash`** = `taxonomy_version` + tagger version + input fingerprint
   (`agenda_item_titles` + transcript text fingerprint), so a taxonomy/tagger change re-tags only
   affected records — unchanged from the L2 sketch, now precisely wired to the corrected input above.
6. **Human overrides — model on `MediaAvailability.operator_override`, not `City.body_exclude`.**
   Exploration found two candidate precedents and they're not equivalent: `body_exclude`
   (`citypods/models.py:216`) is a static per-feed YAML value with no computed counterpart to merge
   against — a poor fit, since tag overrides must coexist with automated output, not replace it wholesale.
   `MediaAvailability.operator_override`/`effective_state()`/`with_operator_override()`
   (`citypods/availability.py:119-124,377-403`) is the right shape: an immutable per-episode override
   field sitting alongside the auto-detected value, with `effective_state()` = `override or detected`,
   preserving the underlying computed value rather than erasing it. **Design `locked`/override the same
   way**: a per-tag `locked: bool` (already in the L2 sketch's shape) plus a per-episode `tags_override`
   block (`{add: [...], remove: [...]}`) applied as an immutable merge step after the automated pass,
   never mutating the rule/LLM-produced tags in place — exactly `with_operator_override`'s
   `dataclasses.replace(...)` pattern, adapted to a list-of-tags merge instead of a single-state field.

## Module / stage plan (exact)

- `citypods/tags.py` — new. Taxonomy loader (`load_taxonomy(path) -> Taxonomy`, modeled on
  `load_site_config`) + a pure rules engine `tag_episode(agenda_item_titles: str, transcript_text: str,
  taxonomy: Taxonomy) -> list[Tag]` (deterministic, offline-testable, no I/O — matches the pattern of
  every other pure transform in this codebase, e.g. `timeline.py`).
- `citypods/stages.py` — new `TagsStage`, implementing the existing `EnrichmentStage` Protocol
  (`stages.py:438-446`: `name: str`, `version: str`, `process(self, provider, city, episodes, ctx) ->
  StageStats`). `name = "tags"`, `version = "1"`. **Ordering**: inserted after `TranscriptStage`/
  `ProviderTranscriptDiarizeStage`, alongside `LinksStage` in the feed-only stage cluster (`default_stages()`/
  `enrich_stages()`, `stages.py:3033-3085`) — exact position relative to `LinksStage` doesn't matter,
  confirmed neither stage affects the other's inputs. **Skip logic**: modeled on `LinksStage`'s
  value-diff check (`stages.py:1220-1223`), not a version-hash comparison — recompute tags, compare
  against the stored value, only write+bump `feed_content_hash`-relevant state if they actually differ.
  Emits **agenda-only tags immediately** when no transcript exists yet (`agenda_item_titles` alone is
  enough for a first pass), re-tags with `transcript_text` added once `TranscriptStage` has run — this
  is the L2 sketch's existing "picked up a later run" behavior, now precisely: the stage always runs, but
  its *output* differs based on what inputs are available that run.
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
- **Human overrides**: per-episode `tags_override` block (§ Data model deltas #6) — where it's *stored*
  is an open call for the implementer: either alongside the tag list in the record itself (simplest,
  travels with the episode) or in `state/` config similar to other operator actions
  (`with_operator_override`'s callers). Recommend the record-inline approach for consistency with
  `MediaAvailability`'s own precedent, which stores its override on the same object it overrides.

## Implementation paths

1. **Rules-only (ship first, ~$0).** Keyword/phrase rules over agenda titles + transcript, with guard
   terms. Transparent, cheap, good recall on agenda titles; moderate precision on transcript prose.
2. **+ LLM-assist (additive, cost-gated).** After rules, an LLM pass proposes additional tags with
   confidence + a one-line explanation, cached and bounded to the <$20/mo near-term budget; output is
   untrusted/additive and clearly labeled. Improves recall on prose where rules miss. **Not greenfield —
   the `tag` task verb is already reserved** in the H13 compute-backend interface's `Task` `Literal`
   (`citypods/compute/base.py:28-35`, shipped, pre-1.0-locked), alongside `summarize`/`soundbite-select`,
   specifically so "the R2 LLM-API adapter... slots in with no interface change" (module docstring,
   `base.py:13-16`, updated 2026-07-14 when R2 was inserted). **The adapter itself is built at R2**
   (dedicated infra item, ahead of this item); R5 is the first *feature* caller of the `tag` verb against
   that already-working adapter, per `review/11`. **Inputs, updated 2026-07-14 — this path gets a richer
   input than the rules engine, not the same one:** the rules engine (path 1, above) uses
   `agenda_item_titles` (chapter titles) because that's all that exists without new infra. Once **R3**
   (agenda text extraction, inserted 2026-07-14, ahead of this item) ships, the LLM path additionally
   takes the **real extracted agenda-document text** — richer than chapter titles, and exactly the kind
   of input an LLM pass is positioned to make good use of where a keyword rules engine could not. This is
   an `InferenceJob(task="tag", inputs={agenda_item_titles, agenda_text, transcript_text,
   taxonomy_version}, recipe_hash)` call through `Backend.run_inference` — `agenda_text` is optional
   (`None` until R3 ships or for providers where extraction fails) and purely additive to
   `agenda_item_titles`, never a replacement, so the LLM path degrades gracefully to the same input the
   rules engine already has if real extraction isn't available for a given episode. `recipe_hash` must
   fold in `prompt_hash` + `model_id` per review/11's LLM-verb convention, so a prompt or model change
   re-derives cleanly. No
   adapter implements `tag` yet today, but **R2 builds the first one** (dedicated infra item, ahead of
   this item) — by the time this path is built, it's a new call site on an already-working adapter, not
   the adapter's own construction.
3. **Embedding/zero-shot classifier.** A local embedding model scores each taxonomy entry per meeting —
   no API cost, more infra. Consider only if (2)'s API cost or (1)'s precision proves limiting.

**Lean: (1) now; (2) as a later additive layer once transcripts are stable.** (Matches the review/01
rescope: "transparent keyword/rule tags first; LLM classification only after transcripts are stable,
with confidence/explanation fields.")

## Surfaces that consume tags

- **Search filters** (#6, review/13) — facet results by tag.
- **Meeting pages** (review/13) — show tag chips with evidence on hover.
- **Topic / region roll-up feeds** (#12/#13, Phase E) — "all `parking-mandates` items in TX."
- **Watchlists + alerts** (Phase F) — match upcoming agenda items against watched tags.
- **National highlights** (Phase E) — select clips by topic.

## Migration / backfill

`feed_content_hash` gains `tags` (§ Data model deltas #4) — per that function's own docstring, this
re-renders the whole catalog once on the first deploy after this lands, the same expected one-time cost
a template-fingerprint bump already causes. No dedicated backfill workflow: `TagsStage` runs as part of
the normal enrich phase and tags every retained episode over the following scheduled runs (agenda-only
first pass for episodes without a transcript yet, full pass once transcripts exist), the same gradual
pattern H12's version-aware re-transcribe already established — not a special-cased bulk job.

## Tests

`tests/test_tags.py`:
- Rules engine (`tag_episode`) is deterministic and offline (fixture inputs, no network, no I/O).
- A fixture with `agenda_item_titles` containing "variance for reduced parking ratio" tags
  `parking-mandates` with evidence — built from `episode_served_chapters`-shaped fixture data, not a
  hand-written string, so the test exercises the corrected input contract, not the old ambiguous one.
- Guard terms suppress a known false positive.
- A fixture with only `agenda_item_titles` (no transcript) still produces agenda-only tags; the same
  fixture with transcript text added produces a superset, not a replacement.
- Human-locked tags survive a re-tag with a bumped `taxonomy_version` (via the `tags_override`
  merge-after-automated-pass logic, § Data model deltas #6) — assert the override merge never mutates
  the automated tag list in place.
- LLM path is mocked (no network in CI) and only **adds** tags with confidence/explanation; asserts a
  `recipe_hash` change (prompt or model) is detectable and re-derives.
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
- **The `tags_override` storage location (record-inline vs. `state/`) is a real open call**, not fully
  settled by this pass — recommended record-inline for `MediaAvailability` consistency, but confirm
  against how operator actions are actually surfaced/audited elsewhere (review/13's `/admin/status`
  plans) before committing, since that surface may have its own storage expectations.
- **`TagsStage` in the `audio_stages` bucket of the H5 global queue is confirmed safe, not confirmed
  cheap** — running an agenda-only tag pass on every episode on every run (even ones that already have
  final tags) adds a real, if small, per-episode cost; the value-diff skip check (§ Module/stage plan)
  needs to be genuinely cheap (a hash/equality check, not a re-run of the rules engine) or this compounds
  at scale the same way any un-cached per-run stage would.

## Sequencing & dependencies

Depends on transcripts (shipped) and benefits from per-meeting pages (review/13, R1) as a display
surface. **Also depends on R2 (LLM backend) and R3 (agenda text extraction) for path 2 specifically** —
both inserted 2026-07-14, ahead of this item, precisely so this item's LLM-assist path has a working
adapter and real agenda text to consume rather than building either under its own time pressure. Path 1
(rules-only) depends on neither and can ship as soon as R1 lands. Precedes topic feeds (#12/#13, Phase E)
and watchlists/alerts (Phase F), which are its main consumers, and R6's "what changed" cards
(review/11 §5.1: "Depends on tags (#4) for topic chips"). R5 precedes R6–R9 in the outer ROADMAP
sequence. Build rules-only (path 1) within Phase R; defer the LLM layer until the near-term LLM budget
and transcript stability are confirmed — the LLM path is otherwise ready to build (R2's adapter + R3's
extraction both already exist by the time R5 is built), it's purely cost-gated, not blocked on missing
infra.

## Acceptance

Meetings carry transparent, evidence-backed topic tags from agenda-item titles/transcripts; a maintainer
can add/remove/lock a tag and the lock is honored on re-tag without mutating the automated output;
episodes without a transcript yet still get agenda-only tags rather than waiting; the optional LLM layer
only augments (never overwrites) and is cost-bounded; tags drive at least one downstream surface (search
facet, once R4's `tags: []` reserved field is populated) without requiring changes to R4's own code.

## Proposed GitHub issues (not filed — batch review pending)

1. `citypods/tags.py` — taxonomy loader + pure `tag_episode` rules engine.
2. `config/taxonomy.yml` — seed taxonomy file, versioned.
3. `TagsStage` + `LANE_STAGES`/`ARTIFACT_BLOCKS`/`_LANE_OWNED_BLOCKS` registration, `Episode.tags` field
   + serialization, `feed_content_hash` extension.
4. `tags_override` human-correction merge logic, modeled on `MediaAvailability.with_operator_override`.
5. LLM-assist path: first real adapter for the reserved `tag` compute verb.
