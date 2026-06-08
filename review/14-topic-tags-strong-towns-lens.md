# review/14 — Topic Tags & Strong Towns Lens (Phase R)

**Maturity: L2→L3 · breakout of [`review/11`](11-technical-design-roadmap.md) Phase R (#4) · last
updated 2026-06-08**

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
(`taxonomy_version`) so a change can trigger a controlled re-tag (like a stage-version bump).

## Data model

Add `tags` to the episode record (additive, schema-bump per the timeline-model precedent):

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

`tags_spec_hash` = `taxonomy_version` + tagger version + input fingerprint (agenda+transcript), so a
taxonomy/tagger change re-tags only affected records. `feed_content_hash` includes `tags` so re-render
follows correctly (it already includes summary/links; extend it).

## Module / stage plan

- New `citypods/tags.py`: the taxonomy loader + a pure rules engine
  `tag_episode(agenda_text, transcript_text, taxonomy) -> list[Tag]` (deterministic, offline-testable).
- New **`TagsStage`** in `citypods/stages.py`, **feed-only**, ordered **after** `TranscriptStage`
  (it reads agenda + transcript; it does not affect audio bytes, so it must come after `AudioStage`).
  It skips episodes without a transcript yet (picked up a later run, like other backfill), but can emit
  **agenda-only** tags immediately when no transcript exists.
- Human overrides live in config/state: a per-feed `tags_override` (add/remove/lock) merged after the
  automated pass; locked tags are preserved across re-tags.
- Taxonomy file: `config/taxonomy.yml` (or `citypods/assets/taxonomy.yml`), `taxonomy_version` in it.

## Implementation paths

1. **Rules-only (ship first, ~$0).** Keyword/phrase rules over agenda titles + transcript, with guard
   terms. Transparent, cheap, good recall on agenda titles; moderate precision on transcript prose.
2. **+ LLM-assist (additive, cost-gated).** After rules, an LLM pass proposes additional tags with
   confidence + a one-line explanation, cached and bounded to the <$20/mo near-term budget; output is
   untrusted/additive and clearly labeled. Improves recall on prose where rules miss.
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

## Tests

`tests/test_tags.py`: rules engine is deterministic and offline; a fixture agenda with "variance for
reduced parking ratio" tags `parking-mandates` with evidence; guard terms suppress a known false
positive; human-locked tags survive a re-tag with a bumped `taxonomy_version`; LLM path is mocked
(no network in CI) and only **adds** tags with confidence/explanation; `feed_content_hash` changes when
tags change.

## Acceptance

Meetings carry transparent, evidence-backed topic tags from agendas/transcripts; a maintainer can
add/remove/lock a tag and the lock is honored on re-tag; the optional LLM layer only augments (never
overwrites) and is cost-bounded; tags drive at least one downstream surface (search facet or topic feed).

## Sequencing & dependencies

Depends on transcripts (shipped) and benefits from per-meeting pages (review/13) as a display surface.
Precedes topic feeds (#12/#13, Phase E) and watchlists/alerts (Phase F), which are its main consumers.
Build rules-only (path 1) within Phase R; defer the LLM layer until the near-term LLM budget and
transcript stability are confirmed.
