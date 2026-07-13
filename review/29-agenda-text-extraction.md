# review/29 — Agenda Text Extraction

**Maturity: L3 (dev-ready) · breakout of [`review/11`](11-technical-design-roadmap.md) §5.1 ·
ROADMAP R3, inserted 2026-07-14, narrowed 2026-07-16 · issues not yet cut**

> **Matured to L3, 2026-07-12** — the maintainer's own stated bar for starting this item ("once R11
> supplies agenda URLs for almost every meeting in the existing feed index") is a *production-execution*
> bar, not a *design-readiness* one: R11's Part A migration (33 Granicus feeds) is designed but not yet
> executed, so that bar isn't literally met in the live catalog yet. Matured the design now anyway,
> matching this session's established pattern of maturing docs ahead of execution (R2/R10 were fully
> designed before R11 shipped either) — implementation should still wait on R11's real link coverage.

---

## §1. Problem

R4 (search, `review/13`) and R5 (tags, `review/14`) both want real agenda-document content as an input,
but no code anywhere in this repo extracts text from an agenda document — only a *link* exists in some
cases (`ep.links["agenda"]`), and two of the four current providers (Swagit, CivicPlus) have no agenda
link at all without R11's auxiliary attachment. Both designs currently fall back to chapter/agenda-item
titles (`episode_served_chapters`) as a weaker proxy — real, but short and not always descriptive.

**Scope, unchanged from the L1 sketch:** given a URL R11 already discovered, extract plain text. No URL
discovery (that's R11's job), no LLM synthesis (that's Phase F's "what's being proposed" brief, post-1.0).
This item is text extraction only.

---

## §2. Scope boundary — three explicit non-goals

Narrower than "parse whatever's linked," deliberately:

1. **Agenda, not Packet.** R11's discovery work (`review/15` Appendix P, PrimeGov specifically) found
   that "Agenda" and "Packet" are frequently *separate documents at the same endpoint pattern with
   different IDs* — the Packet bundles every attachment/exhibit and can run to hundreds of pages or
   multi-GB, while the Agenda itself is the concise item list. **This item extracts from
   `ep.links["agenda"]`/`ep.links["agenda_portal"]` only, never a separately-linked packet/exhibit
   bundle**, even where R11 also discovers one. Keeps extraction bounded and relevant to search/tag
   input, not an unbounded document-warehouse problem.
2. **No OCR.** A scanned-image PDF with no text layer yields empty/near-empty extracted text. Treated as
   an extraction failure (§9), not a trigger for adding an OCR dependency. Revisit only if telemetry
   shows this is a large fraction of real agendas, matching this project's general "don't build for a
   problem you haven't measured yet" instinct (H14d's chunking-disabled-by-default precedent).
3. **No LLM synthesis at this stage.** Output is the extracted plain text itself, consumed as-is by R4/R5.
   No summarization, no structuring, no "what's being proposed" framing — that's explicitly Phase F.

---

## §3. Inputs — the two link keys R11 already establishes

- `ep.links["agenda"]` — PDF (or a PDF-redirect target), per `review/15` §B.1's "New link keys" and §E's
  acceptance criteria (`review/15:734-735,1118`).
- `ep.links["agenda_portal"]` — the structured HTML/portal page (Legistar AgendaViewer, OneMeeting/
  PrimeGov `Portal/Meeting`, CivicEngage Agenda Center, etc.), same citations.

Both are optional and independent — a given episode may have one, both, or neither, depending on which
provider/auxiliary-source combination R11 found for that city. Extraction attempts whichever is present;
prefers `agenda_portal` when both exist (structured HTML is generally cleaner to extract than a PDF's
layout-flattened text — no page-break artifacts, no column-order ambiguity), falling back to `agenda` PDF
extraction otherwise.

---

## §4. New dependencies

**No PDF-parsing or HTML-parsing library exists in this codebase today** (confirmed: no `pypdf`,
`pdfplumber`, `pdfminer`, `beautifulsoup4`, or `lxml` reference anywhere in `pyproject.toml` or
`citypods/`). Two new dependencies, added per `review/22`'s exact contract (§ "Adding or changing a
dependency"):

- **`pypdf`** (pure-Python, MIT, actively maintained) for PDF text extraction. Chosen over `pdfplumber`
  (built on `pdfminer.six` + `Pillow`, better layout/table fidelity but a heavier dependency footprint)
  because agenda PDFs are simple text documents, not tables/forms needing layout-aware extraction — matches
  this project's general "start with the lighter option, escalate only if real samples show it's
  insufficient" posture. If `pypdf`'s extraction proves too lossy against real agenda PDFs once this ships,
  revisit toward `pdfplumber` as a scoped upgrade, not a redesign.
- **`beautifulsoup4`** (stdlib `html.parser` backend — no `lxml` native-code dependency) for portal-page
  text extraction. Government agenda-portal pages carry substantial markup noise (nav, footer, scripts);
  hand-rolled regex/stdlib-only stripping is exactly the kind of "guessing instead of verifying" fragility
  this whole session has learned to avoid, so a real parser is worth the one added dependency.

**Classification (`review/22` table):** both are **output-affecting** — their extraction behavior directly
becomes `agenda_text`, a content-addressed pipeline artifact under `AGENDA_TEXT_PIPELINE_VERSION` (§5).
Add both to the Python-libraries row's output-affecting list alongside `faster-whisper`/`ctranslate2`/
`stable-ts`/`Pillow`, and to `review/22`'s dependency table. A version bump that changes extraction
behavior must bump `AGENDA_TEXT_PIPELINE_VERSION`, per `AGENTS.md`'s pipeline-version-bump contract —
same discipline as an ffmpeg/Whisper-model bump today, just for this new pipeline.

---

## §5. Data model deltas (exact)

1. **New `Episode` fields** (`citypods/models.py`, alongside `transcript_words_url: str | None = None` at
   `:94`):
   - `agenda_text_url: str | None = None` — public CDN URL for the extracted plain-text sidecar. `None`
     means "not yet extracted, or extraction failed" — R4/R5 already treat `agenda_text: null` as a valid,
     expected state (`review/13:579`, `review/14:178-179`), so no new null-handling is needed downstream.
   - `agenda_text_attempts: int = 0` / `agenda_text_last_attempt: str | None = None` — mirrors the
     existing `transcript_timeout_attempts`/`transcript_timeout_last_attempt` pair exactly (same naming
     shape), feeding the backoff helper below rather than inventing a new failure-tracking convention.

2. **New backoff helper** (`citypods/records.py`, alongside `transcript_timeout_backoff_until` at `:143`):
   `agenda_text_backoff_until(ep: Episode) -> datetime | None`, reusing the existing shared
   `_capped_exponential_backoff(base, cap, attempts)` helper (`:85-101`) with new constants
   `AGENDA_TEXT_BACKOFF_BASE = timedelta(days=1)`, `AGENDA_TEXT_BACKOFF_MAX = timedelta(days=14)`. A
   14-day cap (vs. ASR timeout backoff's shorter cadence) reflects this being a low-stakes, low-frequency,
   non-blocking operation — a persistently-broken agenda PDF isn't worth checking more often, but should
   still be re-tried occasionally in case a city re-uploads a cleaner document or R11 discovers a better
   URL later.

3. **Artifact storage** — a sidecar text object, not an inline record field, mirroring the existing
   transcript pattern (`transcript_words_url` is a URL reference; the words themselves live in object
   storage, never inline in `episodes.json`). New key function in `citypods/stages.py`, alongside
   `_transcript_object_key` (`:1268-1269`):
   ```python
   def _agenda_text_object_key(src_key: str, uid: str, recipe: str) -> str:
       return f"agenda/{src_key}/{uid}-agenda-{recipe}.txt"
   ```
   New spec-hash function, mirroring `_provider_transcript_spec_hash`'s raw-byte-identity pattern
   (`:1261-1265`):
   ```python
   def _agenda_text_spec_hash(url: str, content: bytes) -> str:
       blob = AGENDA_TEXT_PIPELINE_VERSION.encode() + b"|" + url.encode() + b"|" + content
       return hashlib.sha1(blob).hexdigest()[:12]
   ```
   Keying on the source `url` **and** raw fetched `content` (not just content) means a URL that starts
   resolving to a different document (an amended agenda republished at the same link) re-extracts rather
   than serving stale cached text — the same "content-addressed, not just URL-addressed" discipline the
   provider-transcript registry already uses.

4. **`AGENDA_TEXT_PIPELINE_VERSION = "1"`** (`citypods/stages.py`, alongside `TRANSCRIPT_PIPELINE_VERSION`
   at `:1231`) — one version constant for this new pipeline, per the existing per-pipeline-version
   convention (`AUDIO_PIPELINE_VERSION`, `TRANSCRIPT_PIPELINE_VERSION`). A bump re-derives only
   `agenda_text`, never transcript/audio/tags.

5. **`ARTIFACT_BLOCKS`** (`citypods/records.py:1021-1023`) gains `"agenda_text"`. **Not** added to any
   entry in `_LANE_OWNED_BLOCKS` (`:1031-1036`) — no H6b lane owns it (§7), so
   `protected_blocks_for_lane()` correctly includes it in every scoped lane's preserve-set automatically
   (it computes `ARTIFACT_BLOCKS - owned`; an unowned block is preserved by construction). Getting this
   addition right matters concretely: without it, a scoped `transcribe`/`align`/`diarize` lane's
   whole-record push would silently regress a freshly-extracted `agenda_text` back to its prior state —
   exactly the failure class `ARTIFACT_BLOCKS` exists to prevent (per its own docstring precedent for
   `media_availability`).

---

## §6. Module / file plan (exact)

- **`citypods/agenda_text.py`** — new. Two extraction functions:
  - `extract_agenda_pdf(content: bytes) -> str` — `pypdf.PdfReader`, concatenate `page.extract_text()`
    across pages, normalize whitespace.
  - `extract_agenda_html(content: bytes) -> str` — `BeautifulSoup(content, "html.parser")`, strip
    `<script>`/`<style>`/`<nav>`/`<footer>` before calling `.get_text()`, collapse whitespace. No
    provider-specific selectors in a first cut (matches this item's "extraction only, keep it bounded"
    scope) — a generic strip-and-extract, not per-platform scraping logic; Appendix P's per-platform page
    structures are R11's concern (finding the URL), not this item's (reading whatever HTML is there).
  - `extract_agenda_text(agenda_url: str | None, agenda_portal_url: str | None, session) -> str | None` —
    orchestrates: prefer `agenda_portal_url` if present, else `agenda_url`; fetch via the given SSRF-gated
    session; dispatch to the PDF or HTML extractor by content-type; truncate to a bounded ceiling (propose
    50,000 characters — generous for a concise agenda, a hard backstop against an unexpectedly large
    document slipping through despite §2's Agenda-not-Packet scoping); return `None` on any fetch/parse
    failure or near-empty result (fewer than ~200 characters, a cheap proxy for "this was a scanned image
    or an error page, not real agenda text").
- **`citypods/stages.py`** — new `AgendaTextStage`, structurally mirroring `TagsStage`'s planned shape
  (`review/14`): for each episode, if `agenda_text_backoff_until(ep)` is set and in the future, skip; else
  if the current `ep.links` agenda URLs' computed `recipe_hash` matches what's already stored at
  `ep.agenda_text_url`, skip (cheap hash comparison, no network call — the same reuse-check discipline
  every content-addressed stage already uses, and the specific cost concern `review/14` flagged for
  `TagsStage`'s own per-run skip-check); else call `extract_agenda_text(...)` through
  `citypods.http.make_session()` (`citypods/http.py:264-337`, the SSRF gate every other fetch path in this
  codebase already goes through — agenda URLs come from R11's discovery/auxiliary-attachment mechanism,
  externally-sourced by definition, so this is not optional), write the sidecar object on success, update
  `agenda_text_url` + reset `agenda_text_attempts`; on failure, increment `agenda_text_attempts`, stamp
  `agenda_text_last_attempt`, leave `agenda_text_url` as whatever it was before (never clear a
  previously-successful extraction just because a later run's fetch failed transiently).
- **`citypods/run.py`** — register `AgendaTextStage` in the feed-only stage list, positioned **after**
  `AudioStage` (per `AGENTS.md:71`'s "feed-only stages run after" rule — this stage never touches audio
  bytes) and **after** R11's `attach_auxiliary_agenda_links` insertion point
  (`SourcePipeline.fetch_merge`, post-`assign_uids`, per `review/15` §B.1) so `ep.links["agenda"]`/
  `["agenda_portal"]` are populated before extraction runs. Must also run **before** R5's future
  `TagsStage` once that item ships, since tags optionally consumes `agenda_text` (`review/14:177-178`).
- **`citypods/compute/base.py`** — **no change.** This is explicitly not an LLM verb (§2 item 3) and
  doesn't touch `InferenceJob`/`Backend` at all — a plain fetch-and-parse Stage, same shape as
  `ChapterStage`/`TimelineStage`, not R2/R12's LLM-adjacent machinery.

---

## §7. Pipeline placement — no dedicated H6b lane

`AgendaTextStage` is **not** added to `LANE_STAGES` (`citypods/stages.py:3092-3097`) — it runs in the
default/full enrich pass on every scheduled run, the same as `chapters`/`timeline`/`remap`, none of which
have dedicated lanes either. Only the GPU/external-dispatch-heavy stages (`audio`, `transcribe`, `align`,
`diarize`) get lane isolation; a lightweight network-fetch-and-parse stage doesn't need it. This does mean
it runs against every episode on every normal scheduled run — the skip-if-unchanged check in §6 is what
keeps that cheap (a hash comparison, not a re-fetch) once an episode's agenda text is already extracted
and its source links haven't changed.

---

## §8. Failure handling & backoff

Extraction can fail for real, expected reasons: a dead/moved agenda URL, a scanned-image PDF with no text
layer, a malformed/corrupted document, a transient network error. All failures are treated identically at
the Stage level — increment `agenda_text_attempts`, apply `agenda_text_backoff_until`'s escalating delay
(§5), leave `agenda_text_url` unset (or unchanged, if a prior successful extraction exists and this is a
*re*-extraction attempt after a source link changed). **No distinction between "permanent" and
"transient" failure classes** — unlike H19's ASR-timeout handling (which is expensive GPU time, worth
finer-grained treatment), a failed agenda-text fetch costs one HTTP request; the 14-day backoff cap is
already generous enough that over-engineering failure-class detection isn't worth it for this item's
stakes. A city's agenda platform migrating (Appendix P's IQM2/NovusAGENDA EOL tripwires) or R11
discovering a better URL both naturally resolve a stuck extraction on their own next run, since the link
itself changes and the recipe_hash comparison in §6 detects that as "needs re-extraction," independent of
backoff state.

---

## §9. Migration / backfill

No dedicated backfill workflow. `AgendaTextStage` runs as part of the normal enrich phase and extracts
agenda text for every episode with a populated agenda link on the following scheduled runs — the same
gradual pattern H12's version-aware re-transcribe and R5's planned `TagsStage` both already use, not a
special-cased bulk job. Coverage is naturally bounded by R11's own rollout: an episode with no
`ep.links["agenda"]`/`["agenda_portal"]` yet (R11 hasn't found a source for that city, or hasn't been
executed there yet) simply has no extraction attempted — `agenda_text_url` stays `None`, R4/R5 both
already treat that as an expected, valid, additive-not-blocking state.

`feed_content_hash` — confirm whether `agenda_text` needs to participate (does a change to extracted
agenda text change rendered feed output at all, given R4/R5 read it from the search-index/tag pipeline
rather than the rendered episode itself)? **Open question, not resolved by this pass** — likely *no*
(agenda text isn't rendered into the podcast feed XML the way `tags` will be per `review/14`'s data-model
delta #4), but should be confirmed against `feed_content_hash`'s actual field list
(`citypods/records.py`) before implementation, not assumed either way.

---

## §10. Tests

`tests/test_agenda_text.py`:

- `extract_agenda_pdf`/`extract_agenda_html` are deterministic and offline (fixture PDF/HTML bytes, no
  network) — a fixture PDF with known text extracts that text; a fixture HTML page with nav/footer/script
  noise extracts only the meaningful body text.
- A near-empty extraction result (simulating a scanned-image PDF) returns `None`, not a near-empty string
  — proves the "treat near-empty as failure" heuristic from §6 actually fires.
- `extract_agenda_text` prefers `agenda_portal_url` over `agenda_url` when both are present.
- `AgendaTextStage` skip-if-unchanged: a second run against an episode whose `agenda_text_url` is already
  set and whose source links are unchanged makes **zero** network calls (mocked session asserts
  `call_count == 0`) — the specific cost concern `review/14` flagged for `TagsStage`'s analogous check,
  tested explicitly here too.
- Backoff: a failed extraction sets `agenda_text_attempts`/`agenda_text_last_attempt`; a second attempt
  within the backoff window is skipped (no network call); an attempt after the window elapses retries.
- `ARTIFACT_BLOCKS`/`protected_blocks_for_lane`: an `agenda_text` block written by the default pass
  survives a subsequent scoped `transcribe`-lane whole-record push untouched — mirrors the existing
  owned-block-preservation tests for `audio`/`transcript`/`media_availability`, the concrete correctness
  case §5 item 5 exists to prevent.
- `AGENDA_TEXT_PIPELINE_VERSION` bump re-derives `agenda_text` only, leaving `transcript`/`audio`/`tags`
  blocks untouched on the same episode — mirrors the existing per-pipeline-version isolation tests.
- Truncation: a fixture agenda document exceeding the 50,000-character ceiling is truncated, not rejected
  outright (partial real content still beats no content for search/tag purposes).

---

## §11. Risks

- **PDF/HTML parsing is provider-format-dependent** — agenda documents vary in layout/quality across the
  many platforms Appendix P censuses; `pypdf`'s plain-text extraction can produce garbled output on
  complex multi-column layouts. Accepted for a first cut (matches this item's stated minimalism); a
  garbled-but-nonempty extraction still passes the near-empty heuristic and gets stored — worth a future
  quality signal (e.g. a crude "does this look like real prose" heuristic) if telemetry shows it's a
  real problem, not designed preemptively here.
- **New fetch+parse failure surface** — every episode with an agenda link now makes an additional network
  call per scheduled run (until extracted, then it's a cheap skip). Bounded by the same SSRF gate and
  `make_session()` retry/timeout behavior every other fetch path already relies on; no new failure *class*,
  just a new call site.
- **Packet/exhibit-bundle scope creep is the most likely way this item quietly grows** — a future
  temptation to "also index the packet since we have the link" reintroduces exactly the unbounded-size
  problem §2 deliberately scoped out. Worth flagging explicitly so a future pass doesn't casually widen
  scope without re-deriving the size/relevance tradeoff.
- **`feed_content_hash` participation is unresolved** (§9) — a real open question, not a guess either way.

---

## §12. Acceptance criteria

An episode with a populated `ep.links["agenda"]` or `["agenda_portal"]` gains a non-null `agenda_text_url`
pointing at real extracted text within a few scheduled runs of R11 discovering that link (subject to
extraction succeeding at all). An episode with neither link is unaffected — `agenda_text_url` stays
`None`, no error, no regression to existing chapter-title-proxy behavior in R4/R5. A scoped `transcribe`/
`align`/`diarize` lane run never regresses an already-extracted `agenda_text` block. A repeat run against
an unchanged episode makes no new network calls. `AGENDA_TEXT_PIPELINE_VERSION` bumps re-derive only
`agenda_text`.

---

## §13. Sequencing

Depends on R11 supplying real agenda-link coverage — **design-complete now, execution should wait** on
R11 Part A's migration actually shipping (currently designed, not yet executed — `review/15` §E). Once
built, feeds R4 (search, additive `agenda_text` field already schema-reserved) and R5 (tags, optional
`agenda_text` input to the LLM-assist path, additive to the `agenda_item_titles` rules-engine input
already in place) — both already designed to consume `agenda_text` as `null`-tolerant and purely additive,
so this item can ship independently of either's own timeline without blocking or being blocked by them.

---

## Proposed GitHub issues (not filed — batch review pending)

1. `citypods/agenda_text.py` — `extract_agenda_pdf`/`extract_agenda_html`/`extract_agenda_text`, plus
   `pypdf`/`beautifulsoup4` added per `review/22`'s dependency contract (output-affecting classification).
2. `Episode` fields (`agenda_text_url`, `agenda_text_attempts`, `agenda_text_last_attempt`) +
   `agenda_text_backoff_until` helper (`citypods/models.py`, `citypods/records.py`).
3. `AGENDA_TEXT_PIPELINE_VERSION` + `_agenda_text_object_key`/`_agenda_text_spec_hash` + `AgendaTextStage`
   (`citypods/stages.py`), registered in `citypods/run.py` after `AudioStage` and after R11's
   auxiliary-link attachment point.
4. `ARTIFACT_BLOCKS` gains `"agenda_text"` (`citypods/records.py`) — no `_LANE_OWNED_BLOCKS` entry needed.
5. Resolve the `feed_content_hash` participation question (§9) before/during implementation.
