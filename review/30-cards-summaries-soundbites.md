# review/30 — Per-Agenda-Item Cards, Auto-Summaries, and Soundbites

**Maturity: L3 (dev-ready) · breakout of [`review/11`](11-technical-design-roadmap.md) §5.1 ·
ROADMAP R6 (bundles #3/GH#155 cards, #2 auto-summaries, #15/GH#156 soundbites) · issues not yet cut**

> **Matured to L3, 2026-07-12.** Verified against ROADMAP.md's live Pri table before starting (table
> order is R1, R10, R11, R2, R12, R3, R4, R5, R6, R7...; R4/R5 are already L3, so R6 — not R7 — is the
> next item still needing design work). Also found and fixed a stale cross-reference: review/11's
> diarization catalog row still said "ROADMAP R5," left over from before the R2/R3 insertion shifted
> numbering; corrected to R7 to match the live table.

---

## §0. Sequencing reality check — R2/R3/R5 foundations are now available locally

**Checked directly rather than assumed:** R2's LiteLLM adapter and R3's bounded document sidecars are
shipped, and R5's local implementation now supplies the shared chapter IDs, bounded transcript windows,
and topic tags. **Updated 2026-07-17:** R5 originally also built a reusable per-candidate confidence-
calibration policy, but that was removed in favor of [`review/27`](27-llm-backend-and-provider-routing.md)
§6's tournament/champion-routing design (`review/14`'s 2026-07-17 section) — provider/model quality
assurance for **every** LLM-assisted path in this item (Part A approach 2, Part B approach 2, Part C
approach 3) is now review/27's scope, not a per-feature evaluator each of these paths would otherwise
need to wire up separately. Each path's own dispatch result is schema-validated and evidence-grounded,
then exposed directly, the same pattern R5 ships (`review/14`); review/27 §6.4 defines the interface for
whichever of these verbs a future tournament run compares. The non-LLM paths remain independently
shippable and are still the recommended first user-visible layer.

**One shared piece of new infra, used by two of the three Parts:** `_parse_words_payload`
(`citypods/transcript_quality.py:924-941`) already parses the word-JSON sidecar
(`{"segments": [{"words": [{"w", "s", "e"}, ...]}]}`) into a flat word list, but it's private to that
module. Promote it to a shared location — new `citypods/transcript_words.py` — alongside a new
`words_in_range(words: list[dict], start: float, end: float) -> list[dict]` slicing helper, which neither
this codebase nor `transcript_quality.py` has today. Both Part A (excerpts) and Part C (heuristic
selection) need "the words between these two timestamps"; building it once, shared, avoids two
near-duplicate slicing implementations.

---

## Part A — Per-agenda-item "what changed" cards (#3/GH#155)

### A.1 Problem, and a real scoping correction

*Problem:* a freeform meeting summary is risky and low-trust; residents want "what did they decide on
item 7?" The L1 sketch's extractive approach (1) proposed joining chapter boundaries with "the transcript
span + **official minutes/vote metadata**." **Checked: no vote-tally, vote-result, or minutes-parsing
code exists anywhere in this codebase** (`vote_tally`/`VoteResult`/`minutes_url`/`official_minutes` —
none found). This isn't a gap to fill as part of this item; R7's own sketch already correctly scopes
"platform-metadata tallies" as Phase F, post-1.0, distinct from R7's own minimal attendee-name slice.
**Part A's first cut builds from what's actually available today and by the time this ships**: chapters
(`episode_served_chapters`, shipped), the transcript (shipped), R3's `agenda_text`/`agenda_backup`
(matured to L3 this session, `review/29`), and R5's tags (L3-designed). No vote/action data — a card
shows *what was discussed*, not *what was decided*, until Phase F's real vote-metadata capture exists.

### A.2 Data model (exact)

Mirrors R3's `agenda_backup` shape (`review/29` §5a) — a consolidated per-episode JSON sidecar with
per-item entries, not one artifact per card, for the same reason: bounded object-storage operation count
(the `speakers.json` precedent), while still supporting per-item skip-checking via a per-entry hash.

- **New `Episode` field:** `cards_url: str | None = None`.
- **New object key** (`citypods/stages.py`, alongside `_agenda_backup_object_key`):
  `f"cards/{src_key}/{uid}-cards-{recipe}.json"`.
- **New pipeline version:** `CARDS_PIPELINE_VERSION = "1"` — independent of `AGENDA_TEXT_PIPELINE_VERSION`/
  `AGENDA_BACKUP_PIPELINE_VERSION`; a cards-logic change shouldn't force re-extracting agenda text, and
  vice versa.
- **Schema:**
  ```json
  {
    "version": "<CARDS_PIPELINE_VERSION>",
    "cards": [
      {
        "chapter_index": 6,
        "title": "Item 7 — Zoning Variance, 412 Main St",
        "start": 3120.0,
        "end": 3410.0,
        "excerpt": "plain transcript text for [start, end), truncated to ~2,000 chars — the literal slice, not a synthesized/AI-picked snippet",
        "doc_links": [{"label": "Staff Report", "url": "..."}],
        "tags": [],
        "changed_summary": null
      }
    ]
  }
  ```
  - `excerpt` — **a straight slice of `words_in_range()` output for that chapter's span, not an
    algorithmically "best" snippet.** Deliberately simple: picking the single most representative moment
    in a span without LLM help is itself a hard selection problem (arguably Part C's own job for a
    *meeting-wide* highlight) — approach 1 stays honestly "extractive" by just showing the real text for
    that time range, not synthesizing a summary of it.
  - `doc_links` — populated by filtering R3's `agenda_backup` JSON entries (`review/29` §5a) for an
    explicit `chapter_index` matching this card's, when that structured association exists. Flat
    packet links remain episode-level context rather than being guessed into a card.
  - `tags` — the taxonomy-ordered visible episode projection from R5; chapter tags are retained on the
    matching chapter. This includes R5's directly-visible LLM candidates, not just rule tags (`review/14`'s
    2026-07-17 section) — there is no separate shadow/admitted split to wait on.
  - `changed_summary` — `null` until approach 2 (LLM-assisted, §A.4) runs for this card.

**R5 integration (added 2026-07-16).** Each card should consume the same stable `chapter_id` and
chapter-scoped tags as R5. This gives a card topic context without recomputing taxonomy labels, lets
users filter cards by one topic rather than a union of meeting-level facets, and gives the extractive
excerpt/highlight path a bounded transcript window. Episode-level tags remain the union projection,
so cards and search cannot silently disagree about which taxonomy version produced a tag. When R3
supplies explicit per-item agenda text, cards can show it only for the matching item; flat agenda or
packet text remains meeting-level context.

- **`ARTIFACT_BLOCKS`** gains `"cards"` — same correctness reasoning as `agenda_text`/`agenda_backup`
  (`review/29` §5): without it, a scoped `transcribe`/`align`/`diarize` lane's whole-record push would
  silently regress a freshly-built card set.

### A.3 Approach 1 — extractive, no LLM, ships first

New `citypods/cards.py`: `build_cards(ep: Episode, agenda_backup: dict | None, words: list[dict]) ->
list[dict]` — one card per `episode_served_chapters(ep)` entry, `end` computed as the next chapter's
`start` (or `ep.duration` for the last one, since chapter dicts carry no explicit `end`,
`citypods/models.py:74`), `excerpt` via the new `words_in_range()` helper (§0), `doc_links` via the
`agenda_backup` join described above. A new `CardsStage` (feed-only, no dedicated H6b lane — same
reasoning as `AgendaTextStage`, `review/29` §7) runs **after** `AgendaTextStage` (needs `agenda_backup`
populated) and consumes R5's point tags when available (or an empty tag projection in an older catalog —
additive, not blocking).

### A.4 Approach 2 — LLM-assisted, additive

**Reuses the `"summarize"` verb, not a new one** — `InferenceJob(task="summarize", inputs={"scope":
"item", "chapter_title", "excerpt", "doc_text": joined agenda_backup text for this item if present},
recipe_hash)`, mode-aware via `inputs["scope"]` rather than inventing a fourth reserved verb, matching the
precedent R12 already established for `classify-civic-platforms` (`review/28` §3.2: one verb, mode-aware
prompt, not near-duplicate verbs). Populates `changed_summary` — one sentence, clearly labeled
AI-generated wherever rendered (SECURITY.md's untrusted-LLM-output rule, unconditional). Gated on R2
actually shipping (§0).

---

## Part B — Auto-summaries (#2)

### B.1 Design decision: inline, not sidecar — a deliberate break from R3's pattern, with a stated reason

**Unlike `agenda_text`/`agenda_backup`/`cards` (all sidecars), the summary is small and bounded by
construction** — a 3–5 sentence blurb, not a multi-thousand-character document. Storing it as a separate
object would be following the sidecar convention past the point it earns its keep (an extra fetch for a
few hundred characters). New `Episode` fields instead: `summary: str | None = None`, `summary_source:
Literal["template", "llm"] | None = None` (transparency — which path produced it, needed for the
never-overwrite-official-text rule below), versioned by a new `SUMMARY_PIPELINE_VERSION = "1"` constant
even though there's no sidecar object to re-key — the version still gates *whether* to regenerate, the
same role it plays everywhere else, just without a storage-key side effect. `ARTIFACT_BLOCKS` gains
`"summary"` regardless of the inline-vs-sidecar choice — the block-protection mechanism's own existing
precedent (`media_availability`) is itself inline structured data, not a sidecar, so this isn't a new
pattern.

### B.2 Rendering — never touches the feed's own `<description>`/`<itunes:summary>`

**Explicit constraint, carried over from the L1 sketch's "never overwrites official text":** the summary
renders as an additional, clearly-labeled block on the per-meeting page (`review/13`, R1) and in R4's
search-result snippets — **not** substituted into the podcast feed's existing `<description>`/
`<itunes:summary>` elements, which stay whatever they are today (title/date-derived or provider-supplied).
Mixing an AI/template-derived blurb into the canonical feed description would blur exactly the
official-vs-generated line SECURITY.md's untrusted-output rule exists to keep clear.

### B.3 Two approaches, sequenced after Part A

- **Approach 1 (templated, $0):** a fixed-shape sentence built from `agenda_text`/chapter titles (e.g.
  "This meeting covered N items, including: ..."). No LLM, ships whenever `agenda_text`/chapters exist.
- **Approach 2 (LLM, additive):** `InferenceJob(task="summarize", inputs={"scope": "episode",
  "transcript_text", "agenda_text", "cards": [card titles/timestamps from Part A]}, recipe_hash)` — 3–5
  sentences, sub-cent–few-cents per meeting, governed by R2's existing budget ledger as-is (no new budget
  concept needed). **Built after Part A** so the summary can reference structured card titles/timestamps
  rather than free-associating from raw transcript text, per the L1 sketch's own sequencing.

---

## Part C — Soundbite highlights (#15/GH#156)

### C.1 Two outputs from one selection decision

**Confirmed: `citypods/clips.py`'s `extract_clip`/`ClipArtifact` is fully built and has zero callers
anywhere in this codebase today** — real, tested infra waiting for its first real consumer, exactly as
the L1 sketch said ("infra exists; only selection is new"). Two distinct outputs, both worth building
once a range is selected, since the marginal cost of the second is near-zero:

1. **The `<podcast:soundbite>` RSS tag** — per the Podcasting 2.0 spec, `startTime`/`duration` attributes
   marking a highlight *within the episode's own existing audio enclosure*; no separate file needed. New
   `podcast_soundbite(ep: Episode) -> tuple[float, float, str] | None` in `citypods/feeds.py`, mirroring
   `podcast_transcript`'s exact existing shape (`citypods/feeds.py:81-103`) and consumption pattern
   (computed in the per-item context around `feeds.py:239`, rendered by a new line in `templates/
   feed.xml.j2` alongside the existing `<podcast:transcript>` tag at `:39`).
2. **A standalone extracted clip artifact**, via the already-built `extract_clip(ep, start, end,
   kind="audio", ...)` (`citypods/clips.py:196-...`) — feeds the future Phase E highlights reel. Built
   *eagerly* now (not deferred to Phase E) since calling an already-existing, already-content-addressed
   function costs nothing extra once the range is already decided — matches this session's "build the
   boring infra ahead of its flashier consumer" pattern (R2/R10 built ahead of R5/R6 for the same reason).

### C.2 Data model — inline, same reasoning as Part B's summary

`Episode.soundbite: dict | None` — `{"start": float, "end": float, "label": str, "source": "manual" |
"heuristic" | "llm", "clip_url": str | None, "clip_key": str | None}`. Small, bounded, no sidecar
warranted. `SOUNDBITE_PIPELINE_VERSION = "1"`; `ARTIFACT_BLOCKS` gains `"soundbite"`. `clip_key`/
`clip_url` reuse `extract_clip`'s existing content-addressing (`clip_object_key`,
`citypods/clips.py:76-92`) as-is — no new addressing scheme.

### C.3 Selection — three approaches, only one needs anything not already shipped

1. **Manual/config** — an explicit `soundbite: {start, end, label}` override in city or per-episode
   config. Cheapest, deterministic, always available; the escape hatch when heuristic/LLM selection picks
   badly for a specific meeting.
2. **Heuristic, ships first, zero new dependencies:** the **longest chapter/agenda-item span**
   (`episode_served_chapters`, already shipped) as a proxy for "the meatiest topic" — no diarization, no
   new detection logic. **A "longest public-comment turn" variant, closer to the L1 sketch's original
   wording, is explicitly *not* buildable yet**: no structured public-comment detection exists (grepped:
   no `public_comment` reference anywhere), and a genuinely reliable "longest single speaker turn" signal
   needs per-speaker segmentation, which is **R7's diarization work, not yet shipped**. Keyword-matching
   chapter titles for "public comment"/"citizen comment" is a plausible cheap refinement worth trying once
   real chapter-title data is reviewed, but the longest-chapter heuristic is the honest zero-dependency
   default for a first cut.
3. **LLM-picked, additive:** `InferenceJob(task="soundbite-select", inputs={"transcript_text",
   "chapters"}, recipe_hash)` → `{start, end, label}`. Gated on R2 shipping (§0).

**Lean: (1) config escape hatch + (2) longest-chapter heuristic ship together** (both zero-dependency),
**(3) additive once R2 exists** — mirrors Parts A/B's own "cheap-first, LLM-additive" shape.

---

## Cross-cutting notes

- **Untrusted-output labeling applies uniformly**: Part A's `changed_summary`, Part B's LLM summary, and
  Part C's LLM-picked label must all render with a clear AI-generated label wherever shown, per
  SECURITY.md — restated here because this is the first item this session where all three LLM-assisted
  sub-paths land in the same place (rendered UI), not just internal search/tag data.
- **`feed_content_hash` participation**: `cards`/`summary` don't touch the RSS feed itself (§B.2), so
  likely no participation, same reasoning as `agenda_text` (`review/29` §9) — confirm against the actual
  field list before implementation, not assumed. **`soundbite` is the one exception** — the
  `<podcast:soundbite>` tag *is* rendered feed content, so `feed_content_hash` must depend on it (a
  changed/newly-selected soundbite should trigger a re-render), unlike every other artifact this item
  introduces.
- **Sequencing**: Part A → Part B (B cites A's cards). Part C is independent of A/B and can ship in any
  order relative to them. All three Parts' non-LLM paths depend only on already-shipped infra (chapters,
  `extract_clip`, R3's `agenda_text`/`agenda_backup` once that ships) — **none of R6 needs to wait on R2
  or R5 actually shipping code**, only their LLM-assisted halves do.

---

## Tests

`tests/test_cards.py` / `tests/test_summaries.py` / `tests/test_soundbites.py`:

- `words_in_range`: a fixture word list returns exactly the words whose `[start, end)` overlaps the
  queried range, correctly handling a word that spans the boundary.
- `build_cards`: one card per chapter, `end` correctly falls back to `ep.duration` for the last chapter;
  `doc_links` correctly filters `agenda_backup` entries by matching `chapter_index`, and is empty (not
  erroring) when `agenda_backup` is `None`.
- `podcast_soundbite`/feed rendering: a fixture episode with `ep.soundbite` set renders the
  `<podcast:soundbite>` tag with correct `startTime`/`duration`; a fixture with `soundbite: None` renders
  no tag at all, mirroring `podcast_transcript`'s existing None-handling test shape.
- Longest-chapter heuristic selects the correct chapter on a fixture episode with chapters of varying
  length.
- `ARTIFACT_BLOCKS`/`protected_blocks_for_lane`: `cards`, `summary`, and `soundbite` each survive a
  scoped `transcribe`-lane whole-record push untouched — same test shape as `agenda_text`/`agenda_backup`.
- Summary never populates/overwrites `podcast_description` or any existing feed `<description>` field —
  an explicit regression test for §B.2's constraint, since "just wire it into the description" is the
  natural (wrong) shortcut a future edit could take.
- LLM paths (Part A approach 2, Part B approach 2, Part C approach 3) are mocked in CI (no network),
  asserting only-additive behavior (a card's `changed_summary`/episode `summary`/soundbite `label` is
  populated without altering the extractive fields already present).

---

## Proposed GitHub issues (not filed — batch review pending)

1. `citypods/transcript_words.py` — promote `_parse_words_payload` out of `transcript_quality.py`, add
   `words_in_range()` (§0). Shared by issues 2 and 5.
2. `citypods/cards.py` (`build_cards`) + `Episode.cards_url` + `CARDS_PIPELINE_VERSION` +
   `_cards_object_key` + `CardsStage` + `ARTIFACT_BLOCKS["cards"]` (Part A, approach 1).
3. Part A approach 2: `InferenceJob(task="summarize", inputs={"scope": "item", ...})` call site in
   `CardsStage`, gated on R2 shipping.
4. `Episode.summary`/`summary_source` + `SUMMARY_PIPELINE_VERSION` + `ARTIFACT_BLOCKS["summary"]` +
   templated approach-1 summary generation (Part B).
5. Part B approach 2: `InferenceJob(task="summarize", inputs={"scope": "episode", ...})` call site, gated
   on R2 shipping and sequenced after issue 2 (cards).
6. `podcast_soundbite()` (`citypods/feeds.py`) + `templates/feed.xml.j2` tag + `Episode.soundbite` +
   `SOUNDBITE_PIPELINE_VERSION` + `ARTIFACT_BLOCKS["soundbite"]` + longest-chapter heuristic + manual/
   config override path (Part C, approaches 1–2) + the first real call to `extract_clip` in production.
7. Part C approach 3: `InferenceJob(task="soundbite-select", ...)` call site, gated on R2 shipping.
8. `feed_content_hash` — confirm and wire `soundbite` participation (the one artifact in this item that
   actually needs it, §Cross-cutting notes).
