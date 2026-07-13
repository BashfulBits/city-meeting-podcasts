# review/31 — Speaker Diarization, Minimal Attendee Extraction, and Per-Speaker Pages

**Maturity: L3 (dev-ready) · breakout of [`review/11`](11-technical-design-roadmap.md) §5.1 ·
ROADMAP R7 (#7 diarization + a minimal #14 attendee slice; per-speaker pages adopted from
[`review/25`](25-future-features-and-architecture.md) §2.3 #11) · full pull-forward, gating 1.0 ·
issues not yet cut**

> **Matured to L3, 2026-07-13.** The L1 sketch this builds from was already unusually detailed (real
> CPU-viable model research, a named execution-backend dependency, an explicit naming/confirmation
> policy) — closer to L2 in places. This pass's main job was verifying that research is still current,
> grounding every dependency against the actual shipped codebase rather than the sketch's own claims
> (one turned out stale, one turned out already resolved — §0), and writing the data model, module plan,
> and test/acceptance detail L3 requires.

---

## §0. What's already shipped vs. what's actually new — checked directly, not assumed

The L1 sketch names three sequencing dependencies. Verified each against the live codebase rather than
trusting the sketch's own framing:

| Dependency named in the L1 sketch | Status, verified | Consequence for this item |
|---|---|---|
| "H6b sharded ASR workflow (dedicated runner/lane)" | **Shipped** (`review/11` H6b row: `#273`, `audio.yml`/`asr.yml` split, `LANE_STAGES` already extends to a `diarize` lane) | The blocker the sketch said to wait on is cleared — nothing left to do here before starting |
| "H9 offload evaluation (cost/quality baseline)" | **Deferred/closed, not blocking** — H14d's live telemetry already answered H9's original question (combined free-tier capacity clears the transcription backlog); its design text is explicitly kept "as background for a future re-open if... the diarization rollout materially changes" (`review/11` H9 note) | Not a dependency to wait on. A real, honest watch item: if native diarization's own GPU/CPU cost profile turns out to strain the backend mix, H9's shelved evaluation is the thing to re-open — noted in Risks, not treated as settled forever |
| "Execution backend (§5.5) — same interface as transcription" | **Already true, further than the sketch implies.** `Task = Literal["transcribe", "align", "diarize", ...]` (`citypods/compute/base.py:29-36`) has included `"diarize"` since H13 shipped; H14b/Modal and H14c/Beam (both shipped) already dispatch *any* task generically — neither needs new code to carry a `diarize` job | This item does **not** need to build or extend the execution-backend interface at all — only give it a real adapter to dispatch to (§1) |

**What's actually missing, confirmed by reading the code, not inferring from the sketch:**

- `citypods/compute/local.py`'s `run_inference` handles exactly `"transcribe"`/`"align"` and raises
  `ValueError` for anything else (`:44-63`) — **no `"diarize"` branch exists.** This is the real gap, not
  the execution-backend interface itself.
- `speakers_key`/`speakers_url`/`speakers_spec_hash`/`speakers_format`/`speakers_synced`/
  `speakers_confidence`/`speakers_pipeline_version`/`speakers_error` **already exist on `Episode`**
  (`citypods/records.py:791-799`, `860-872`) — but they were built for **provider-supplied** diarization
  (PT-PR6, `PROVIDER_DIARIZE_PIPELINE_VERSION`, `citypods/stages.py:1233,1292-1303`), a different data
  source (a city's own platform metadata) from what this item builds (running a model over our own
  audio). `ARTIFACT_BLOCKS`/`_LANE_OWNED_BLOCKS` already reserve a `diarize` lane owning the `speakers`
  block (`citypods/records.py:1012-1013,1022,1035`) — **the reservation is real, the Stage that fills it
  is not.**
- No speaker/person identifier field exists anywhere in `citypods/models.py` — confirmed, not present.
  Both the naming-confirmation workflow (§3) and per-speaker pages (§4) need one; it doesn't exist yet.

**Net effect: this item is narrower than "diarization plus its execution backend" — the backend is
already built and already generic over this exact task. The real scope is (1) a native diarization
adapter, wired into the existing-but-inert lane/block plumbing and unified with the existing provider-
diarize schema rather than inventing a parallel one, (2) minimal attendee extraction, (3) an
identify-then-confirm naming workflow, (4) per-speaker pages.**

---

## Part A — Native speaker diarization

### A.1 Model choice — re-verified, not re-derived from scratch

The L1 sketch's own research (wespeaker ECAPA-TDNN, ~100MB, no HF gate, ~2× transcription cost on CPU)
checked out on re-verification (2026-07-13): wespeaker's embeddings run at ~0.67s of compute per second
of audio on a single CPU vs. pyannote.audio's ~2.2s/s — pyannote remains the accuracy/convenience
standard (DER ~11–19%, strong community support) but is materially heavier on CPU, consistent with why
the sketch scoped it out for the Actions-runner-budget path. **One new data point worth recording, not
acting on yet:** a newer library ("Diarize," built on Silero VAD + WeSpeaker ResNet34 embeddings via
ONNX Runtime + GMM/BIC speaker counting + spectral clustering) claims ~7× pyannote's CPU speed at
comparable DER (~10.8% vs. ~11.2% on VoxConverse) — but it's a very recent, single-maintainer project with
no track record here. **Recommendation: keep wespeaker ECAPA-TDNN as the primary choice** (the sketch's
own conclusion, now re-confirmed current); note the newer library as a future swap candidate if
wespeaker's real production throughput proves insufficient, not a reason to delay on an unproven
dependency. `speechbrain` ECAPA-TDNN via `simple-diarizer` stays the documented fallback if wespeaker's
packaging proves difficult.

### A.2 Module plan

- **`citypods/diarize.py`** — new, mirroring `citypods/asr.py`'s existing shape (model load/cache,
  produce an artifact, no I/O beyond what's handed in): `diarize(audio_path: Path, model: str, *,
  compute_type: str, cpu_threads: int) -> DiarizeArtifacts`, where `DiarizeArtifacts` carries speaker-
  labeled turn segments in **source-time** (the same clock ASR/alignment already operate in before
  serve-time remapping).
- **`citypods/compute/local.py`** — add an `elif job.task == "diarize":` branch (`:54-61` is the exact
  pattern to mirror), lazily importing `citypods.diarize` the same way `asr` is lazily imported (`:27-32`,
  "keep ASR extras cost off import" — the same reasoning applies to wespeaker's own dependency weight).
- **Meeting-wide identity reconciliation** (the sketch's own stated hard part): for long meetings,
  diarize in overlapping windows and reconcile speaker identity **across window boundaries via embedding
  similarity**, not by concatenating independently-numbered per-window labels — the sketch is explicit
  that naive concatenation is wrong, and this pass doesn't relax that. Concretely: each window's speaker
  clusters carry a mean embedding vector; a simple nearest-neighbor match (cosine similarity above a
  threshold) against already-seen embeddings from prior windows assigns a stable meeting-local speaker
  index, falling back to a new index when no match clears the threshold. This is deliberately the
  simplest reconciliation method that respects the sketch's own constraint — not a claim that it's
  optimal, just that it's a real, implementable first cut rather than hand-waved.
- **Source-to-served remap:** diarization's output (source-time turn segments) reuses the existing
  `remap(tl, items, source_id=..., clamp_to=...)` utility (`citypods/timeline.py:302-...`) — the exact
  function `episode_served_chapters` already uses for chapters (`citypods/chapters.py:30-34`). No new
  remap logic; speaker turns are just another `{start, end, ...}` item list.

### A.3 Data model — unify with the existing provider-diarize schema, don't parallel it

**The `speakers_*` fields already on `Episode` (§0) are reused as-is for native diarization output** —
both provider-supplied and natively-computed diarization are conceptually the same artifact (speaker-
labeled turns for an episode), and `_LANE_OWNED_BLOCKS`'s existing `"diarize": {"speakers",
"provider_transcript"}` entry already assumes one unified `speakers` block regardless of which method
produced it. Concrete additions:

- **New `Episode` field:** `speakers_source: Literal["provider", "native"] | None = None` — the one
  genuinely missing piece of provenance; everything else (`key`/`url`/`spec_hash`/`format`/`synced`/
  `confidence`/`pipeline_version`/`error`) already exists and is reused unchanged. Explicit rather than
  inferring provenance from which `pipeline_version` string happened to be stamped — matches this
  project's general preference for explicit provenance fields (`agenda_backup`'s per-item `source`,
  `summary_source`, `review/29`/`review/30`) over implicit inference.
- **New pipeline version:** `DIARIZE_PIPELINE_VERSION = "1"` (`citypods/stages.py`, alongside the existing
  `PROVIDER_DIARIZE_PIPELINE_VERSION = "1"` at `:1233`) — independent constant, so a native-diarization
  model/logic change doesn't force re-deriving provider-sourced speaker data and vice versa.
- **New object key** (mirroring `_provider_diarize_object_key`, `:1302-1303`, dropping the `provider-`
  segment): `f"transcripts/{src_key}/{uid}-diarize-{spec}.speakers.json"`.
- **New spec-hash function**, mirroring `_provider_diarize_spec_hash`'s structure (`:1292-1299`) but keyed
  on the *audio* + ASR transcript spec rather than a provider artifact:
  ```python
  def _diarize_spec_hash(ep: Episode) -> str:
      spec = {"v": DIARIZE_PIPELINE_VERSION, "transcript": ep.transcript_spec_hash}
      blob = json.dumps(spec, separators=(",", ":"), sort_keys=True)
      return hashlib.sha1(blob.encode()).hexdigest()[:12]
  ```
- **Precedence when both exist:** a city with genuinely provider-supplied diarization (rare — most
  providers don't expose it) and a native run both populate the same `speakers_*` fields at different
  times; **provider-sourced, when present and synced, wins** (matches this codebase's general "trust
  city-supplied ground truth over our own inference where both exist" posture, e.g. provider-transcript
  vs. ASR) — native diarization only runs when no synced provider-sourced `speakers` artifact already
  exists for that episode, checked before dispatching a `diarize` job at all (cheap, avoids wasted
  compute on a city that already supplies real diarization).
- **`speakers.json` schema** (both sources, unified): `{"version": "...", "speakers": [{"speaker_index":
  int, "turns": [{"start": float, "end": float}], "embedding_confidence": float | null}]}` —
  `speaker_index` is meeting-local only (§A.2's reconciliation output), **not** the cross-meeting stable
  identifier (§3 introduces that separately, only once a human confirms a name).

### A.4 Budget — its own profile, not ASR's borrowed

Per the sketch's own explicit note (carried forward, not new): diarize must **not** reuse ASR's H14d
budget coefficients, admission thresholds, or VRAM-vs-host-RSS guard ratio — it needs its own measured
profile once real telemetry exists, following the exact same "measure from live runs, don't estimate
upfront" discipline H14d itself was built on (`review/11` H14d row). Not designed further here — this is
a statement of what H14d-for-diarize will need to do once diarization is live, not a number to guess now.

---

## Part B — Minimal attendee extraction (Phase F #14, pulled forward)

### B.1 Scope, restated precisely (unchanged from the L1 sketch, now grounded)

**Only the name list of who was present at a meeting, parsed from released minutes.** Explicitly **not**
in scope: vote/roll-call tallies, platform per-member metadata linkage, or the entity model — all stay in
Phase F's fuller attendee/vote item (`review/11` §5.3), confirmed unchanged by this pass. The reason this
minimal slice exists at all: diarization (Part A) produces only anonymous voice clusters ("Speaker 2");
turning that into a real name needs *some* ground-truth name list to match against, and released minutes
are the cheapest real source of one.

### B.2 Input source — a direct, concrete payoff from R3's already-matured work

**Minutes documents are already a distinct, named link type this session's own R3 work established** —
`citypods/providers/civicclerk.py:62-67`'s `_FILE_TYPE_LINKS` maps `"Minutes"` to `"minutes"`, structurally
identical to the `"Agenda Packet"` → `"agenda_packet"` gap R3 found and proposed closing
(`review/15` issue 9). **Same fix, same file, same one-line wiring gap** — `Episode.links["minutes"]`
needs the identical `_published_links`-through-to-`Episode.links` wiring R3 already proposed for
`agenda_packet`. Once wired, attendee extraction reuses R3's own extraction primitives directly:
`extract_agenda_pdf`/`extract_agenda_html` (`citypods/agenda_text.py`, `review/29` §6) run against
`links["minutes"]` exactly as they already run against `links["agenda"]`/`["agenda_portal"]` — no new
fetch/parse machinery, a new *consumer* of R3's existing one.

### B.3 Name extraction — a bounded, extractive pattern match, not NLP

Released minutes conventionally open with a structured roster line ("PRESENT: Mayor X, Council Members
A, B, C... ABSENT: D"). New `citypods/attendees.py`: `extract_attendees(minutes_text: str) ->
list[str] | None` — a small set of pattern rules over the extracted minutes text (case-insensitive
`PRESENT:`/`ATTENDANCE:`/`MEMBERS PRESENT:` line markers, common in the platforms this catalog already
covers — Legistar/CivicClerk minutes both follow this convention), splitting on commas/semicolons/"and",
stripping titles (`Mayor`, `Council Member`, `Councilmember`) into a bare name list. **Deliberately not
NLP/NER** — matches review/25's own explicit note that general entity extraction "was *deleted*" as
premature (§2.3 #12) — this is pattern-matching a conventional document header, not open-ended text
understanding. Returns `None` (not an empty list) when no roster-shaped text is found, so a genuinely
unparseable minutes format degrades to "no ground truth available" rather than a false-empty attendee
list.

### B.4 Data model

- **New `Episode` field:** `attendees: list[str] | None = None` — a bare name list. Small and bounded
  (a council roster is a handful to a few dozen names), so **inline, not a sidecar** — same size-based
  reasoning `review/30` already used for `summary`/`soundbite`.
- **New pipeline version:** `ATTENDEES_PIPELINE_VERSION = "1"`, `ARTIFACT_BLOCKS` gains `"attendees"`.
- Runs as part of the same feed-only Stage tier as `AgendaTextStage` (`review/29` §7) — no dedicated H6b
  lane, a lightweight parse over already-fetched text, not a GPU operation.

---

## Part C — Voice-cluster-to-name matching: identify-then-human-confirm, never auto-named

**Restated from both the L1 sketch and `review/25` #11, because it's a hard integrity constraint, not a
preference:** diarization (Part A) produces anonymous `speaker_index` clusters; attendee extraction
(Part B) produces a name list; **nothing in this design ever auto-assigns a name to a cluster.** A human
reviews and confirms each match. This is consistent with the project's standing "never
editorialize/auto-attribute the factual record" stance (already cited in the L1 sketch), applied here to
speaker identity specifically.

### C.1 The stable identifier — reserved now, populated later

**New, genuinely missing piece (§0):** a `speaker_id: str` — stable across meetings, distinct from the
display name (a councilmember's name can be misspelled/change; the identifier must not). Minted only on
first human confirmation (`speaker_id = sha1(city_slug + confirmed_display_name)[:12]`, or a small
`state/speakers/<city_slug>.json` registry mapping display name → id, whichever proves simpler at
implementation time — not settled further here, since neither choice blocks anything else in this
design). **Reserved in the data model now, per the L1 sketch's own instruction**, even though it stays
unpopulated until a human confirms the first match: `Episode`'s per-turn speaker data (§A.3's
`speakers.json`) carries `speaker_index` (meeting-local, always present) and an optional `speaker_id`
(cross-meeting stable, `null` until confirmed) — adding the field now means the eventual entity-model
formalization of `Person` (§5.5) doesn't require a later migration to backfill it onto records that
already existed.

### C.2 Confirmation workflow — a review surface, not a new automation class

Modeled on `/admin/status`'s existing review-surface precedent (`review/13`'s plans, H16's operator-review
digest pattern) rather than inventing a new UI paradigm: for each episode with unconfirmed
`speaker_index` clusters and a non-null `attendees` list, surface a simple mapping form (cluster → name
dropdown, "unknown"/"skip" always available) either in `/admin/status` or a lightweight dedicated review
page. Confirming a mapping writes `speaker_id` onto that cluster's entry (and, going forward, any
subsequent episode's clusters whose embedding matches closely enough — a soft, human-checked
carry-forward, not automatic — flagged as a real design detail to firm up at implementation time, not
resolved further in this pass since it depends on how well cross-meeting embedding matching actually
performs once real data exists).

---

## Part D — Per-speaker pages (`review/25` §2.3 #11)

**Static, generated-from-records, the same build-time mechanism as R1's meeting pages** — not gated on
the Interaction seam, since (per the L1 sketch's own reasoning) the speaker roster is bounded per city,
like the city/body roster R1 already handles. **Only for confirmed speakers** (a non-null `speaker_id`,
§C.1) — an anonymous "Speaker 2" cluster never gets a page, avoiding exactly the kind of unconfirmed
attribution the identify-then-confirm rule exists to prevent.

- New `templates/speaker.html.j2` + `render_speaker_page(speaker_id, episodes: list[Episode]) -> str`,
  structurally mirroring R1's `render_meeting_page`/`meeting_page_url` pattern (`review/13` Part A).
- Content: every confirmed turn across every episode for that `speaker_id`, aggregated across meetings —
  "everything Councilmember X has said, across meetings" (the L1 sketch's own framing, and `review/25`'s
  cited Digital Democracy precedent). Each entry deep-links to that turn's timestamp on the source
  meeting page (reuses R1's existing per-meeting transcript-seek mechanism, not a new one).
- **Depends on R1** (meeting pages, already L3) for the per-meeting linking target, and on Part C's
  confirmation workflow actually having run for at least one speaker before any page exists — an empty
  roster produces zero pages, not an error.

---

## Sequencing

Part A (diarization) has no remaining hard blocker (§0) and can start immediately. Part B (attendees)
depends on the same one-line `links["minutes"]` wiring gap R3 already flagged (`review/15` issue 9) —
trivial, shared infrastructure, not new design. Part C (confirmation) depends on both A and B producing
real output to match against. Part D (per-speaker pages) depends on C having actually confirmed at least
one speaker, and on R1 (meeting pages, shipped) for its linking target. **Full pull-forward maintained**
(2026-07-12 decision, unchanged): implement before the front-end design cycle (R8), since a real
speaker-attribution UI is an input to that redesign, not a later bolt-on.

---

## Tests

`tests/test_diarize.py` / `tests/test_attendees.py` / `tests/test_speaker_pages.py`:

- `diarize()` on a fixture audio file (or a mocked wespeaker call in CI, matching `asr.py`'s own
  offline-test convention) produces source-time turn segments; meeting-wide reconciliation correctly
  merges the same speaker's clusters across a fixture with an artificial window boundary, not
  double-counting them as two speakers.
- `local.py`'s new `"diarize"` branch round-trips an `InferenceJob` to a `JobResult`, mirroring the
  existing `"transcribe"`/`"align"` branch tests.
- `speakers_source` correctly distinguishes provider vs. native writes; a synced provider artifact blocks
  a native diarize dispatch (§A.3's precedence rule) — asserted via a mocked dispatch check, not just
  documented.
- `ARTIFACT_BLOCKS`/`protected_blocks_for_lane`: `speakers` (already-reserved) and the new `attendees`
  block both survive a scoped lane's whole-record push untouched.
- `extract_attendees`: a fixture minutes document with a conventional `PRESENT:`/`ABSENT:` roster line
  extracts the correct name list, titles stripped; a fixture with no roster-shaped text returns `None`,
  not an empty list.
- Confirmation workflow: confirming a cluster→name mapping writes `speaker_id`; an anonymous
  (unconfirmed) cluster never appears in per-speaker page generation — an explicit regression test, since
  "just render whatever clusters exist" is the natural (wrong) shortcut.
- Per-speaker page: a fixture speaker with turns across two episodes aggregates both, each deep-linking
  to the correct meeting-page timestamp.

---

## Risks

- **Meeting-wide identity reconciliation quality is unverified against real long-meeting audio** — the
  nearest-neighbor embedding match (§A.2) is a real, implementable first cut, not a proven-accurate one;
  a genuinely noisy meeting (crosstalk, poor mic placement for public comment) could produce spurious
  speaker splits. Mitigated by the identify-then-confirm workflow itself (a human sees and can correct
  bad clusters), not by claiming the model is perfect.
- **H9 may need reopening** (§0) — if native diarization's real CPU/GPU cost profile (once measured, per
  H14d's own "measure, don't estimate" discipline) strains the current Modal/Beam free-tier mix, the
  shelved H9 combined-throughput evaluation is the right thing to revisit, not a sign this item's design
  was wrong.
- **Minutes-format heterogeneity** (§B.3) — the `PRESENT:`/`ABSENT:` pattern is conventional but not
  universal across every platform Appendix P censuses; `extract_attendees` returning `None` for an
  unrecognized format is the honest degrade path, same posture R3 already established for extraction
  failures generally.
- **The stable `speaker_id` minting mechanism is deliberately left semi-open** (§C.1) — a real
  implementation-time decision (hash-of-name vs. a small registry file), not resolved further here since
  neither choice blocks the rest of this design.
- **The "Diarize" library's 7× speed claim is unverified in this codebase's own conditions** — recorded
  as a data point (§A.1), not adopted; re-evaluate only if wespeaker's real measured throughput proves
  insufficient once live telemetry exists (matching H9/H14d's own precedent).

---

## Acceptance criteria

An episode with ASR-transcribed audio and no synced provider-sourced diarization gains a populated
`speakers_url`/`speakers_source="native"` after a `diarize` job runs, with meeting-local `speaker_index`
clusters remapped to served time. A city with genuine provider-sourced diarization never triggers a
native run (precedence check, §A.3). An episode with a discoverable `links["minutes"]` document gains a
non-null `attendees` list when the minutes follow a recognizable roster format, `None` otherwise — never
a silently-wrong partial list. No cluster ever renders with a confirmed display name without an explicit
human confirmation action having occurred. A per-speaker page exists only for `speaker_id`s with at least
one confirmed turn, aggregating correctly across every episode that speaker appears in.

---

## Proposed GitHub issues (not filed — batch review pending)

1. `citypods/diarize.py` (wespeaker ECAPA-TDNN adapter) + `local.py`'s new `"diarize"` branch (Part A).
2. `Episode.speakers_source` + `DIARIZE_PIPELINE_VERSION` + `_diarize_object_key`/`_diarize_spec_hash` +
   a new `DiarizeStage` filling the already-reserved `diarize` lane, including the provider-vs-native
   precedence check (Part A).
3. Meeting-wide identity reconciliation (nearest-neighbor embedding match across diarization windows),
   Part A's own sub-component, real enough to test independently of the rest of the Stage.
4. `Episode.links["minutes"]` wiring (same fix as `agenda_packet`, `review/15` issue 9 — batched together
   at implementation time since it's the identical gap in the identical file).
5. `citypods/attendees.py` (`extract_attendees`) + `Episode.attendees` + `ATTENDEES_PIPELINE_VERSION` +
   `ARTIFACT_BLOCKS["attendees"]` (Part B).
6. Confirmation review surface (cluster → name mapping, `/admin/status` or dedicated page) +
   `speaker_id` minting (Part C).
7. `templates/speaker.html.j2` + `render_speaker_page` (Part D), gated on issue 6 having a real confirmed
   speaker to render.
8. H14d-for-diarize: measure native diarization's real CPU/GPU cost profile once issue 1–2 are live,
   feeding its own budget coefficients rather than borrowing ASR's (§A.4) — explicitly deferred to real
   telemetry, not designed further in this pass.
