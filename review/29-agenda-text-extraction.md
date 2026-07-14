# review/29 — Agenda Text Extraction

**Maturity: L3 (dev-ready) · breakout of [`review/11`](11-technical-design-roadmap.md) §5.1 ·
ROADMAP R3, inserted 2026-07-14, narrowed 2026-07-16 · issues not yet cut**

> **Matured to L3, 2026-07-12** — the maintainer's own stated bar for starting this item ("once R11
> supplies agenda URLs for almost every meeting in the existing feed index") is a *production-execution*
> bar, not a *design-readiness* one: R11's Part A migration (33 Granicus feeds) is designed but not yet
> executed, so that bar isn't literally met in the live catalog yet. Matured the design now anyway,
> matching this session's established pattern of maturing docs ahead of execution (R2/R10 were fully
> designed before R11 shipped either) — implementation should still wait on R11's real link coverage.
>
> **Corrected and expanded, same day.** The original draft excluded "Packet"/backup material, citing
> "hundreds of pages or multi-GB." **That figure was wrong — not a hedge, an actual error.** Checked
> directly: the only "multi-GB" claim anywhere in this session's research is
> `config/feeds/travis-county-tx.yml:12`, describing **source video files**, not agenda packets. No
> packet-size data was ever actually gathered; the earlier §2 stated an unverified assumption as if it
> were a researched fact, which is exactly the failure mode this whole design effort exists to catch.
> Corrected, and the maintainer's follow-up question ("is it feasible, and can text-only extraction stay
> bounded even if the source packet is large?") turned out to have a real, well-grounded yes: `pypdf`
> (already the chosen PDF library) exposes `extract_uris()` for internal PDF links directly, and
> `CivicClerk`'s provider code already carries an unused `"Agenda Packet"` link
> (`citypods/providers/civicclerk.py:64`). Backup/packet material is now in scope (§3a, §5a, §6a) —
> **stored as a genuinely separate artifact from agenda-only text**, per the maintainer's explicit
> requirement, so a consumer can ask for "just the agenda," "just one item's backup," or "just a link,"
> independently.

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

## §2. Scope boundary — two explicit non-goals, one reversed decision

1. **No OCR.** A scanned-image PDF with no text layer yields empty/near-empty extracted text. Treated as
   an extraction failure (§9), not a trigger for adding an OCR dependency. Revisit only if telemetry
   shows this is a large fraction of real agendas, matching this project's general "don't build for a
   problem you haven't measured yet" instinct (H14d's chunking-disabled-by-default precedent). Applies
   equally to backup/packet material (§3a) — an image-only exhibit (a site plan, a photo) extracts to
   nothing and is dropped, which is also the main reason backup material stays bounded in practice even
   though it's no longer excluded by policy (see the corrected reasoning below).
2. **No LLM synthesis at this stage.** Output is the extracted plain text itself, consumed as-is by R4/R5.
   No summarization, no structuring, no "what's being proposed" framing — that's explicitly Phase F.
3. ~~**Agenda, not Packet.**~~ **Reversed 2026-07-12 — this exclusion was based on an unverified size
   claim (see the correction note above) and didn't survive scrutiny.** The real reason text-only
   extraction stays bounded even for a large source packet is non-OCR extraction itself (item 1 above):
   a packet's size usually comes from scanned exhibits/drawings/photos, which contribute ~0 extracted
   characters regardless of the source file's byte size. Backup/packet material is now in scope — see §3a
   (inputs), §5a (storage, kept separate from agenda-only text per the maintainer's explicit requirement),
   §6a (extraction/attribution). What's still true and still bounds this: per-item and aggregate
   truncation ceilings (§5a), and fetch-count multiplication is a real, new cost (§11) that the
   agenda-only design didn't have to think about.

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

### §3a. Backup/packet inputs — three sources, three confidence levels (new, 2026-07-12)

Unlike `agenda`/`agenda_portal` (one flat URL per episode, uniform across providers), backup material is
exposed differently by provider, with genuinely different attribution confidence — worth being explicit
about rather than pretending there's one clean mechanism:

| Source | Mechanism | Attribution | Confidence |
|---|---|---|---|
| **`links["agenda_packet"]`** (new key) | CivicClerk already computes this internally (`_FILE_TYPE_LINKS["Agenda Packet"] -> "agenda_packet"`, `citypods/providers/civicclerk.py:64`) but never wires it to `Episode.links` — a one-line gap, not new discovery work. PrimeGov's separate Packet document ID (`review/15` Appendix P) fits the same shape once R11's Part C ships. | **Whole-meeting only** — neither platform exposes per-item packet segmentation | Medium (real URL, but one undifferentiated document) |
| **Internal PDF links** (found during §3's own agenda-PDF fetch) | `pypdf.extract_uris()` on the already-fetched `agenda` PDF — the exact mechanism the maintainer described ("individual links within the PDF that go to each backup item"). No new fetch; a byproduct of parsing a document R3 is already parsing. | **Best-effort, order-based** (§6a) — not geometrically precise | Lower — a heuristic, stated as such |
| **Legistar `EventItems?Attachments=true`** (`EventItemMatterAttachments`, Legistar's *Web API*, distinct from the Calendar.aspx HTML scraping Part A actually uses) | **Not built by this pass — Phase 2, an R11 follow-on** (§13). Genuinely structured, per-item attachment metadata from a real API; the highest-confidence source once wired, but it's new provider-discovery work belonging to R11's Legistar adapter, not this item's extraction concern. | **Per-item, structured** | Highest (once built) |

**Phase 1 (this pass) ships the first two — zero new R11 discovery work required** (CivicClerk's link
already exists in code; internal PDF links are found during extraction, not discovered upstream at all).
Phase 2 (Legistar's structured API) is proposed as a follow-on R11 item (§13, Proposed issues).

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
behavior must bump `AGENDA_TEXT_PIPELINE_VERSION` (agenda-only text) **and/or**
`AGENDA_BACKUP_PIPELINE_VERSION` (backup text, §5a — a separate constant, since the two extraction paths
can evolve independently), per `AGENTS.md`'s pipeline-version-bump contract — same discipline as an
ffmpeg/Whisper-model bump today, just for these two new pipelines.

**No third dependency needed for backup extraction.** `pypdf.PdfReader`'s `extract_uris()` method reads a
page's `/Annots` → `/A` → `/URI` link annotations directly — the exact library already chosen for §4's
PDF text extraction, no new package.

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

5. **`ARTIFACT_BLOCKS`** (`citypods/records.py:1021-1023`) gains `"agenda_text"` **and `"agenda_backup"`**
   (§5a — two separate blocks, matching the two separate artifacts). Neither is added to any entry in
   `_LANE_OWNED_BLOCKS` (`:1031-1036`) — no H6b lane owns either (§7), so `protected_blocks_for_lane()`
   correctly includes both in every scoped lane's preserve-set automatically (it computes
   `ARTIFACT_BLOCKS - owned`; an unowned block is preserved by construction). Getting this addition right
   matters concretely: without it, a scoped `transcribe`/`align`/`diarize` lane's whole-record push would
   silently regress a freshly-extracted `agenda_text`/`agenda_backup` back to its prior state — exactly
   the failure class `ARTIFACT_BLOCKS` exists to prevent (per its own docstring precedent for
   `media_availability`).

### §5a. Backup/packet text — a genuinely separate artifact, per the maintainer's explicit requirement

**Direct answer to "should this go in a sidecar rather than blow up episodes.json": yes, and this isn't
a close call.** Every precedent in this codebase for "potentially large per-episode derived text" already
uses the URL-reference-in-record + sidecar-object-in-storage pattern — `transcript_words_url`,
`speakers.json`, and (§5 above) `agenda_text_url` itself. `episodes.json` is the append-only canonical
record store that `load_records`/`merge_persisted` load and diff on every run; keeping it lean isn't
optional hygiene, it's load-bearing for that mechanism's own performance at catalog scale. Backup text —
potentially several items' worth of staff-report/memo prose per episode — follows the same rule agenda
text already does, just more so.

**The separation the maintainer asked for is a distinct artifact, not a distinct storage *mechanism*** —
both `agenda_text` and `agenda_backup` are sidecars, but two different sidecars with two different
version constants, so "send an LLM just the agenda" and "send an LLM one item's backup" are naturally
independent operations (fetch one URL, not the other), and neither's extraction logic changing forces
re-deriving the other.

**New `Episode` field:** `agenda_backup_url: str | None = None` — public CDN URL for a **consolidated
per-episode JSON sidecar** (not one file per backup document — see rationale below), structured:

```json
{
  "version": "<AGENDA_BACKUP_PIPELINE_VERSION>",
  "items": [
    {
      "chapter_index": 3,
      "chapter_title": "Item 7 — Zoning Variance, 412 Main St",
      "label": "Staff Report",
      "source": "internal-pdf-link",
      "source_url": "https://...",
      "text": "extracted plain text, or null if extraction failed/image-only",
      "truncated": false,
      "spec_hash": "abc123def456"
    }
  ]
}
```

- `chapter_index`/`chapter_title` — best-effort attribution to `episode_served_chapters(ep)` (§6a);
  `null`/`null` for the unattributed whole-packet case (`links["agenda_packet"]`, which has no per-item
  structure to attribute against).
- `source` — provenance (`"agenda_packet"` | `"internal-pdf-link"` | `"legistar-attachment-api"` once
  Phase 2 lands), so a consumer or a future debugging pass can tell how confident to be in the
  attribution, per §3a's table.
- `source_url` — **always present, even when `text` is null** — this is what answers the maintainer's
  "sometimes we just want the link" case: a consumer building HTML show-notes/episode-page backup links
  reads this field and never needs to touch `text` at all, and doesn't pay for extraction to get it.
- `text`/`truncated` — `null`/irrelevant when extraction wasn't attempted or failed (image-only exhibit,
  dead link); otherwise the extracted text, truncated against a **per-item** ceiling (propose 20,000
  characters — smaller than agenda-only's 50,000, since a single backup attachment is usually one
  document, not a whole meeting's item list) plus an **aggregate** per-episode ceiling across all items
  (propose 200,000 characters total, truncating additional items once hit) — both concrete but explicitly
  tunable defaults, not deeply researched numbers.
- `spec_hash` — **per-item** content address (`url` + raw fetched bytes, same construction as
  `_agenda_text_spec_hash`), enabling per-item re-extraction skip-checks even though storage is
  consolidated (§6a) — an item whose source is unchanged is copied forward from the previous JSON without
  a re-fetch, only changed/new items cost a network call.

**Why one consolidated JSON object per episode, not one text file per backup document (the
`_agenda_text_object_key`-per-artifact pattern §5 already uses):** bounded object-storage operation
count matters at catalog scale — a meeting with 15 items and 2–3 backup docs each would mean 30–45
separate small objects under the per-artifact pattern, versus one. Matches the existing `speakers.json`
precedent (one consolidated structured JSON per episode holding multiple entries, not one file per
speaker) rather than inventing a new shape. Per-item skip-checking (via each entry's own `spec_hash`)
recovers the fine-grained change-detection the per-artifact pattern would have given "for free," without
the object-count cost.

**New object key function** (`citypods/stages.py`, alongside `_agenda_text_object_key`):
```python
def _agenda_backup_object_key(src_key: str, uid: str, recipe: str) -> str:
    return f"agenda/{src_key}/{uid}-backup-{recipe}.json"
```
`recipe` here is derived from the sorted list of per-item `spec_hash` values (deterministic — changes
if, and only if, at least one item's source content changed), not a single whole-document hash.

**New pipeline version:** `AGENDA_BACKUP_PIPELINE_VERSION = "1"` — independent of
`AGENDA_TEXT_PIPELINE_VERSION` (§4), so a backup-extraction-only bug fix doesn't force re-deriving
already-correct agenda-only text, and vice versa.

**New backoff fields**, mirroring §5 items 1–2 exactly for the independent backup pipeline:
`agenda_backup_attempts: int = 0` / `agenda_backup_last_attempt: str | None = None` on `Episode`, and an
`agenda_backup_backoff_until(ep)` helper reusing the same `_capped_exponential_backoff` machinery with
its own constants (propose the same `1d`/`14d` base/cap as agenda-only — no evidence yet that backup
extraction fails at a meaningfully different rate).

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
    50,000 characters — generous for a concise agenda, a hard backstop against any unexpectedly large
    document); return `None` on any fetch/parse failure or near-empty result (fewer than ~200 characters,
    a cheap proxy for "this was a scanned image or an error page, not real agenda text").

### §6a. Backup/packet extraction and attribution (new, 2026-07-12)

Three more functions in `citypods/agenda_text.py`:

- `extract_pdf_links(pdf_content: bytes) -> list[tuple[int, str]]` — `pypdf.PdfReader`, calls
  `extract_uris()` per page, returns `(page_index, uri)` pairs in document order. Pure parsing, no
  fetching.
- `attribute_links_to_chapters(links: list[tuple[int, str]], chapters: list[dict], pdf_page_count: int) ->
  list[tuple[str | None, str | None, str]]` — the chapter-attribution heuristic, **order-based, not
  geometric.** `pypdf` gives page index and URI directly; getting a link's exact position on the page
  (needed for true geometric nearest-heading matching) requires correlating `/Rect` bounding boxes against
  per-line text positions — meaningfully more implementation complexity for a precision gain that isn't
  obviously worth it in a first cut. Instead: assume agenda documents list items and their associated
  links in sequential order (true for essentially every real agenda structure — item 1's materials appear
  before item 2's) and distribute the `len(links)` discovered links proportionally across the
  `len(chapters)` known chapters by document-order position (`link_index / len(links) ≈
  chapter_index / len(chapters)`, roughly). **Explicitly a heuristic, stated as such in the output
  (`source: "internal-pdf-link"` in §5a's schema, distinct from higher-confidence sources) — not
  guaranteed precise, good enough for "which item is this probably about" rather than a hard guarantee.**
  If real extracted data shows this heuristic attributing badly, the fallback is graceful: `chapter_index:
  null` (unattributed, still usable as whole-episode backup context) rather than a wrong attribution
  presented as confident.
- `extract_backup_item(url: str, session) -> tuple[str | None, bool]` — fetch + extract text for one
  backup document (reuses `extract_agenda_pdf`/`extract_agenda_html` by content-type, same as the
  agenda-only path), apply the per-item 20,000-character ceiling (§5a), returns `(text, truncated)`.

**`AgendaBackupStage` is not a separate Stage class** — folded into the same `AgendaTextStage` pass
(below) as a second phase per episode, not a second stage. Both write to independent artifact blocks
(`agenda_text` / `agenda_backup`, §5), so the storage-separation the maintainer asked for is a property
of the *data*, not the *code* — one Stage can own multiple artifact blocks (the existing `audio` lane
already owns both `audio` and `media_availability`, precedent for exactly this shape). Splitting into two
Stage classes would mean re-fetching/re-parsing the same agenda PDF twice (once for agenda text, once to
find its internal links) for no benefit.

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
  previously-successful extraction just because a later run's fetch failed transiently). **Second phase,
  same Stage, same episode pass:** if `agenda_backup_backoff_until(ep)` isn't active, gather candidate
  backup URLs (`links.get("agenda_packet")`, plus `extract_pdf_links()`'s output from the agenda-PDF fetch
  already made in phase one — no duplicate fetch), diff each candidate's freshly-computed `spec_hash`
  against the previous `agenda_backup_url` JSON's per-item hashes (§5a), fetch+extract only changed/new
  items via `extract_backup_item(...)` (same `make_session()` SSRF gate), copy unchanged items forward,
  write the consolidated JSON, update `agenda_backup_url`/reset `agenda_backup_attempts` on any successful
  item, or increment `agenda_backup_attempts` only if every candidate failed.
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
it runs against every episode on every normal scheduled run — the skip-if-unchanged checks in §6/§6a are
what keep that cheap: a whole-document hash comparison for agenda-only text, and a per-item hash
comparison for backup text (so one changed backup item doesn't force re-fetching every other item on the
same episode), neither a re-fetch unless something actually changed.

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
agenda text (and backup text, §6a) for every episode with populated links on the following scheduled
runs — the same gradual pattern H12's version-aware re-transcribe and R5's planned `TagsStage` both
already use, not a special-cased bulk job. Coverage is naturally bounded by R11's own rollout: an episode
with no `ep.links["agenda"]`/`["agenda_portal"]`/`["agenda_packet"]` yet simply has no extraction
attempted — both URL fields stay `None`, R4/R5 both already treat that as an expected, valid,
additive-not-blocking state.

`feed_content_hash` participation — **resolved differently for the two artifacts, not one open question
anymore:**
- **`agenda_text`**: likely *no* participation — it's consumed by R4's search index and R5's tag input,
  neither of which renders into the podcast feed XML itself. Should still be confirmed against
  `feed_content_hash`'s actual field list (`citypods/records.py`) before implementation, not assumed.
- **`agenda_backup`**: **a real, different case, per the maintainer's own stated consumption model** —
  backup *links* (not necessarily extracted text) are explicitly intended to render into episode
  show-notes/HTML/podcast-feed-notes views (§5a's `source_url` field exists specifically for this). **R3
  itself doesn't render anything** (that's R1's meeting-page/feed-notes rendering, a future consumer) —
  but whichever future stage *does* render backup links into feed output must include `agenda_backup_url`
  (or a stable hash of its content) in that render's own content-hash dependency, or a changed backup
  link set wouldn't trigger a re-render. Noted here as a forward-looking dependency for that future work,
  not something R3 needs to wire itself.

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

**New, for backup extraction (§6a):**

- `extract_pdf_links` returns `(page_index, uri)` pairs in document order from a fixture PDF with known
  internal links.
- `attribute_links_to_chapters`: a fixture with links evenly distributed across a known chapter list
  attributes each link to the expected `chapter_index`; a fixture with more links than chapters still
  produces a valid (if coarser) distribution rather than an index error.
- `links["agenda_packet"]` present, `agenda`/`agenda_portal` absent: still produces a whole-episode
  (`chapter_index: null`) backup entry — proves packet extraction doesn't require agenda-PDF presence.
- Per-item skip-if-unchanged: a second run where only one of three backup items' source content changed
  re-fetches **only** that item (mocked session asserts `call_count == 1`, not `3` or `0`) — the specific
  behavior §5a/§6a's per-item `spec_hash` design exists to enable, tested explicitly since it's easy to
  accidentally implement as all-or-nothing.
- Per-item and aggregate truncation: a fixture single item exceeding 20,000 characters truncates that
  item; a fixture set of items whose combined text exceeds 200,000 characters truncates the tail items
  (order-preserving — earlier items in document order are kept whole before later ones are dropped/cut).
- `source_url` is always populated even when `text` is `None` (a fixture image-only exhibit) — proves the
  "link-only consumption" case (§5a) doesn't require successful extraction.
- `ARTIFACT_BLOCKS`/`protected_blocks_for_lane`: `agenda_backup` (like `agenda_text`) survives a scoped
  lane's whole-record push untouched — same test shape as the `agenda_text` case, run against the second
  block too, not assumed to follow from the first test passing.
- `AGENDA_BACKUP_PIPELINE_VERSION` bump re-derives `agenda_backup` only, leaving `agenda_text` (and
  everything else) untouched — proves the two pipelines are actually independent, not just documented as
  such.

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
- **Fetch-count multiplication for backup material (new, real, not present in the agenda-only design)** —
  a meeting with many agenda items and multiple backup docs each means many additional network calls the
  first time it's extracted (bounded to changed/new items on every run after, per §6a's per-item
  skip-check, but the *first* pass per episode is genuinely more expensive than agenda-only extraction
  ever was). Not a reason not to build it — this project already does more per-episode network activity
  for transcription — but a real, honest cost this revision adds that the original design didn't have.
- **Chapter-attribution is a heuristic, not a guarantee (§6a)** — the order-based approach can misattribute
  a link to the wrong item, especially for agendas with irregular structure (consent-agenda blocks, items
  added late, non-sequential internal links). Mitigated by defaulting to `chapter_index: null` when
  uncertain rather than a confident-looking wrong answer, and by `source` provenance tagging so a consumer
  can weight `internal-pdf-link` attributions lower than a future `legistar-attachment-api` one. Revisit
  toward geometric (bounding-box) attribution only if real extracted data shows the order heuristic is
  poor, not preemptively.
- **`feed_content_hash` participation resolved differently per artifact** (§9) — `agenda_text` likely
  doesn't participate; `agenda_backup` plausibly does once a future feed-notes renderer consumes its
  links, a forward dependency for that later work, not this item's own open question anymore.

---

## §12. Acceptance criteria

An episode with a populated `ep.links["agenda"]` or `["agenda_portal"]` gains a non-null `agenda_text_url`
pointing at real extracted text within a few scheduled runs of R11 discovering that link (subject to
extraction succeeding at all). An episode with neither link is unaffected — `agenda_text_url` stays
`None`, no error, no regression to existing chapter-title-proxy behavior in R4/R5. A scoped `transcribe`/
`align`/`diarize` lane run never regresses an already-extracted `agenda_text` block. A repeat run against
an unchanged episode makes no new network calls. `AGENDA_TEXT_PIPELINE_VERSION` bumps re-derive only
`agenda_text`.

**New, for backup material:** an episode with `links["agenda_packet"]` and/or internal PDF links
discoverable in its `agenda` document gains a non-null `agenda_backup_url` pointing at a consolidated
JSON whose entries always carry a `source_url` (even when `text` extraction failed). A repeat run
re-fetches only items whose source content actually changed. `AGENDA_BACKUP_PIPELINE_VERSION` bumps
re-derive only `agenda_backup`, never `agenda_text`. An episode with neither `agenda_packet` nor internal
PDF links is unaffected — `agenda_backup_url` stays `None`, same additive-not-blocking posture as
agenda-only text.

---

## §13. Sequencing

Depends on R11 supplying real agenda-link coverage — **design-complete now, execution should wait** on
R11 Part A's migration actually shipping (currently designed, not yet executed — `review/15` §E). Once
built, feeds R4 (search, additive `agenda_text` field already schema-reserved) and R5 (tags, optional
`agenda_text` input to the LLM-assist path, additive to the `agenda_item_titles` rules-engine input
already in place) — both already designed to consume `agenda_text` as `null`-tolerant and purely additive,
so this item can ship independently of either's own timeline without blocking or being blocked by them.

**Minutes and structured meeting data extension:** Agenda extraction also discovers minutes links. Those
links fill a missing effective `links["minutes"]` only when the provider has not supplied one; provider
links remain canonical and win on every later refresh. The minutes URL is then consumed by the separate
`MinutesTextStage`, which writes minutes text independently from agenda text. Per-item vote records and
member rosters are extracted from that text with evidence/source URLs and become inputs to the later
diarization lane; they are not inferred from audio. Full schema and sequencing are in [`review/30`](30-minutes-text-votes-rosters.md).

**Backup material specifically, phased (§3a):** Phase 1 (`links["agenda_packet"]` consumption + internal
PDF link extraction) ships with this item — zero new R11 discovery work required, since CivicClerk's link
already exists in code and internal-link extraction is a byproduct of extraction already happening. Phase
2 (Legistar's structured per-item `EventItemMatterAttachments` API) is a **proposed follow-on to R11**
(`review/15` Part A), not this item — new provider-discovery work belonging to R11's existing
Legistar/Granicus adapter, orthogonal to R3's own extraction concern. Not blocking; Phase 1's lower-
confidence `internal-pdf-link` attribution already provides real (if imperfect) coverage for Legistar
cities in the meantime.

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
5. Resolve the `agenda_text`/`feed_content_hash` participation question (§9) before/during implementation.
6. **New:** wire CivicClerk's already-existing `_FILE_TYPE_LINKS["Agenda Packet"]` output through to
   `Episode.links["agenda_packet"]` (`citypods/providers/civicclerk.py:62-67` + wherever `_published_links`
   is consumed) — a small, near-zero-risk gap-close, not new discovery logic.
7. **New:** `extract_pdf_links`/`attribute_links_to_chapters`/`extract_backup_item` (§6a) +
   `Episode.agenda_backup_url`/`agenda_backup_attempts`/`agenda_backup_last_attempt` +
   `agenda_backup_backoff_until` + `AGENDA_BACKUP_PIPELINE_VERSION` + `_agenda_backup_object_key` +
   `AgendaTextStage`'s second phase (§6).
8. **New:** `ARTIFACT_BLOCKS` gains `"agenda_backup"` (`citypods/records.py`).
9. **New, Phase 2, proposed as an R11 follow-on, not this item:** Legistar Web API
   `EventItems?Attachments=true` per-item attachment discovery (§3a), feeding `source: "legistar-attachment-api"`
   entries into the same `agenda_backup` JSON shape once built.
