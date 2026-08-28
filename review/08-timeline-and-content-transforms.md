# Timeline & content-transform model — design proposal

**Status:** APPROVED (2026-06-03; proposal drafted 2026-06-02). Maintainer decisions on all §10 open
questions are applied; the surgical-re-encode answer (§4) and future-video accommodation (§7) were
added in review. Issues filed — see **§11**. Written before implementing the audio-cleanup band
(#111 silence-trim, #122 multi-segment concat, host-all-audio, with loudness, intro/outro,
soundbites and the transcript band close behind). The goal is to lock **one** extensible model for
"where does a moment in the audio we serve come from" so every later feature plugs in without
re-litigating timestamps.

> **Roadmap-number vs GitHub-issue-number caveat (important):** throughout this doc, bare `#NN` in
> *feature* references means the **roadmap item number** in `review/01`/`ROADMAP.md` (e.g. "#23
> host-all", "#21 loudness", "#25 intro/outro", "#1 ASR", "#11 podcast:transcript", "#3 per-item
> summaries", "#15 soundbites", "#46 permalink pages"). Those are **not** GitHub issue numbers. Only
> #111 (silence), #122 (concat), #110 (ASR) exist as GitHub issues today. §11 lists the actual
> GitHub issue numbers created for this initiative.

Read alongside: `review/02-architecture.md` (stage pipeline, split-hash invalidation, transcript
storage = Change 4), `review/03-resource-model.md` (encode backlog is the constraint, not storage),
`citypods/records.py` (identity + content-addressing), `citypods/stages.py` (stage ordering +
`stop` convention), `citypods/media.py` (the encoder we generalize).

---

## 1. The problem

Today the served enclosure is a 1:1 derivative of a single source media URL: `-c:a copy` or one
mono-AAC re-encode of `Episode.video_url`. Because of that 1:1-ness, every timestamped artifact we
attach (chapters, and soon transcripts) is implicitly in **the same time base as the source video**,
and `chapters_json` / the embedded M4A markers / `<itunes:duration>` all just pass source seconds
straight through. `audio_spec_hash` encodes exactly this assumption: `{version, source_url, max_kbps,
chapters}`.

The audio-cleanup band breaks the 1:1-ness. The moment we **trim** silence (#111), **concatenate**
multiple source segments into one meeting (#122), **insert** an intro/outro stinger (#25), or
**re-host + loudness-normalize** Granicus (#23/#21), the audio a listener hears no longer shares a
clock with the city's original video(s). Then a cascade of features needs to translate between clocks:

- **Chapters / transcript** (#1/#3/#11) arrive in **source** time (provider markers, provider SRT)
  but must be aligned to the **served** enclosure, or apps scrub to the wrong place.
- **Soundbites** (#15) and a future **video-clip compilation** pick a span of the **served** audio
  but must cut the **source** video(s) — and deep-link to where that moment lives on the city site.
- **Newsletter / RSS / per-meeting page** (#18/#46) want "jump to this quote in the city's own
  video," i.e. served-time → source-time → a provider deep-link.
- **Concat** (#122) means a single served second can map to *different source files*.

These are all the **same** translation problem. This doc proposes the single abstraction that
answers it, the data model + pipeline changes it implies, and how it slices into standalone,
independently-testable infrastructure issues that should land **before** (and separately from) the
feature issues that consume them.

---

## 2. Core model: SourceMedia + AudioTimeline (an EDL) + a time-basis convention

Three concepts, one new module (`citypods/timeline.py`, pure + dependency-free, like
`projection.py`):

### 2a. SourceMedia registry (per episode)
The set of original media a meeting is built from. One entry today; N for concat/multi-view.

```python
@dataclass(frozen=True)
class SourceMedia:
    id: str            # stable within the episode, e.g. "s0", "s1" (or seq-derived)
    provider: str      # "granicus" | "swagit" | ...
    ref: str           # how the provider re-resolves playable media (page url / clip id / dfile)
    media_kind: str    # "direct" | "hls"
    duration: float | None     # source-clip seconds (moved off Episode.duration; see §4)
    watch_url: str | None      # human watch page (canonical_video)
    backup_key: str | None = None   # optional: our archived copy of this source media (see §7 "Future: video")
```

`ref` is deliberately the *stable* handle (page URL / clip id), never the tokenized HLS URL —
mirroring why `audio_spec_hash` already excludes the expiring URL. `backup_key` is unused today; it
is the seam for a future "archive the source video as a backup" feature (§7) and stays `None` until
then, so it costs nothing now.

### 2b. AudioTimeline (the Edit Decision List)
The served audio is an **ordered list of segments**. Each segment is either a span lifted from a
source, or a synthetic insert. This is the whole model:

```python
@dataclass(frozen=True)
class Segment:
    served_start: float
    served_end: float
    kind: str                  # "source" | "insert"
    # kind == "source":
    source_id: str | None = None
    source_start: float | None = None
    source_end: float | None = None
    # kind == "insert":
    insert: str | None = None  # "intro" | "outro" | "silence" | "gap"
    asset_id: str | None = None
    asset_version: str | None = None

@dataclass(frozen=True)
class Timeline:
    version: str               # the planner/algorithm version that produced this EDL
    segments: tuple[Segment, ...]
    basis: str = "served"      # served-time is canonical
```

We do **not** time-stretch — every source segment is a constant-offset copy
(`served_t = source_t + (segment.served_start − segment.source_start)`), so the map is
piecewise-linear with slope 1. That keeps the math trivial and lossless and means audio bytes are
always a cut/paste of real source audio (plus inserts).

### 2c. Two maps (the only API features call)

```python
def served_to_source(tl, t_served) -> tuple[str, float] | None
    # forward: a served instant -> (source_id, source_t). None if it lands in an insert.

def source_to_served(tl, source_id, t_source) -> float | None
    # inverse: a source instant -> served instant. None if that source time was cut out.

def remap(tl, items, *, source_id=None) -> list   # convenience over the two above
    # remap chapters / transcript cues from one basis to the other, dropping cut items.
```

### 2d. Time-basis convention (write it down once, enforce everywhere)
Every timestamped artifact declares its basis: `"served"` or `"source:<id>"`. The render layer
emits **served**-time offsets (they must match the enclosure). Source-time artifacts are converted
via `source_to_served` at the remap step. ASR output is born `"served"` (we transcribe the served
file). Provider chapters/SRT are born `"source:<id>"`. The identity timeline (below) makes
served == source so legacy artifacts need no conversion.

### 2e. The identity timeline (backward-compatibility keystone)
An un-manipulated episode (the common case today: direct copy / single re-encode) has exactly one
source and a single identity segment `served=[0,D], source=[0,D]`. For that case:
- `served_to_source`/`source_to_served` are the identity, so existing source-time chapters/transcripts
  render unchanged with zero remap.
- The timeline serializes to a **canonical empty/identity digest** so `audio_spec_hash` is byte-for-byte
  what it is today — *no mass re-encode* when this model ships (see §4 + §7 migration). Only episodes
  that actually get trimmed/concatenated/stamped pay a (deliberate) re-encode.

---

## 3. Every audio-manipulative feature is just an EDL composition

| Feature | Sources | Timeline it produces | Remap needed? | Re-encode? |
|---|---|---|---|---|
| (today) direct copy | 1 | identity, 1 segment | no | n/a |
| **#23 host-all** | 1 | identity (re-host a full Granicus MP4 as M4A) | no | yes (net-new host) |
| **#21 loudness** | 1 | identity (filter only, no time change) | no | yes (filter ≠ copy) |
| **#111 silence-trim** | 1 | source spans with the silent gaps dropped | yes (chapters+transcript) | yes |
| **#122 concat** | N | each source's span laid end-to-end | yes (per-source offsets) | yes (can't `copy` across inputs) |
| **#25 intro/outro** | 1+ | `insert(intro)` + source span(s) + `insert(outro)` | yes (shift by intro len) | yes |
| **#15 soundbite / clip** | — | *consumes* the EDL (forward-map a served range to source(s)) | n/a | derived clip |

So #111, #122, #23, #21, #25 are **planners that emit a Timeline**, then **one generalized encoder**
that renders any Timeline. They stop being five bespoke audio paths and become five small EDL
contributors over shared infrastructure. That is the central payoff of doing this design first.

---

## 4. Data-model changes

### Episode / EpisodeRecord (schema v2)
Add to the record (and the `Episode` dataclass, round-tripped in `records.py`):

```jsonc
{
  "sources": [ {id, provider, ref, media_kind, duration, watch_url} ],
  "timeline": { "version", "basis":"served", "segments":[ ... ] },   // omitted when identity
  "chapters_basis": "served",          // after remap; default "source:s0" pre-remap
  "audio": { ...existing..., "duration_served": float },             // see duration note
  "transcript": { "key", "url", "spec_hash", "format":"vtt", "basis":"served", "synced":bool }
}
```

- **`chapters` become served-time** after the remap step (so embedded M4A markers + the
  `application/json+chapters` sidecar are correct against the enclosure). The provider's original
  source-time markers are recoverable from `sources` + `timeline` if ever needed; we don't store both.
- **Duration semantics shift.** `<itunes:duration>` / enclosure must reflect the **served**
  duration (sum of segment lengths), not the source. Today `Episode.duration` is source duration.
  Move source durations onto `SourceMedia.duration`; let `Episode.duration` mean **served** duration
  (computed from the timeline; identity → unchanged). This is a real correctness fix that trim/concat
  *force* — call it out in migration tests.
- **`transcript` block** mirrors `audio`: content-addressed object key + spec hash + basis + a
  `synced` flag (false for plain untimed transcripts shown as notes only — #111 acceptance).

### Generalized `audio_spec_hash` (the invalidation contract)
Today: `{v, source_url, max_kbps, chapters}`. Generalize to:

```python
{ "v": AUDIO_PIPELINE_VERSION,
  "max_kbps": max_kbps,
  "timeline": timeline_digest(tl),     # canonical hash of the EDL; "" for identity
  "loudness": loudness_profile or "",  # e.g. "" today, "ebuR128:-16LUFS" once #21 ships
  "chapters": served_chapters,         # now served-time
  "sources": [s.ref for s in sources], # replaces the single source_url
}
```

Rules that keep this safe:
- **Identity ⇒ no churn.** `timeline_digest(identity) == ""` and a single-source `sources==[url]`,
  so the serialized spec equals today's for un-manipulated episodes → **no re-encode storm** on rollout.
- Changing the silence algorithm / loudness target / intro asset bumps `version` or the relevant
  field → new spec → new content-addressed key → CDN cache-bust + orphan-GC reclaim, all via the
  machinery that already exists. No new invalidation plumbing.
- The EDL is **persisted, not recomputed each build.** Silence detection runs once in the enrich
  phase, lands on the record, and feeds the hash. Renders are stable and reproducible; we never
  re-run detection unless its version changes. (Determinism, §7.)

### Surgical re-encode without a global storm (answer to "how do we fix one bad file?")

There are **two distinct kinds of "this audio is wrong,"** and they want opposite blast radii. The
model already separates them; the rule is *which knob you turn*:

1. **Corrupted bytes / bad upload, but the recipe was correct.** The stored object is bad (truncated
   upload, B2 corruption, a half-written file from a hard-killed run) but the spec that produced it is
   still right. **Fix: delete the object; change nothing else.** `materialize_audio` already
   re-encodes any episode whose `audio_key` is *absent from storage* (the issue-#116 "record points
   at a missing object → drop the dead pointer and re-host" path). So a corrupt-file fix is just
   "drop the object" — the **same** content-addressed key is regenerated, no version bump, no other
   episode touched. Surface this as an ops command (see below) that deletes the object (and clears the
   record's audio block) for one or more uids.

2. **A bug in the *encode path itself* that produced wrong bytes, fixed in code.** Re-encoding with
   the same spec would faithfully reproduce the bug, so the object must be re-keyed — but you only
   want to re-key the **affected** episodes, not bump `AUDIO_PIPELINE_VERSION` (which re-encodes the
   entire catalog). For this, add a **per-record rebuild nonce** that is mixed into `audio_spec_hash`:

   ```python
   { "v": AUDIO_PIPELINE_VERSION, ..., "rebuild": ep.audio_rebuild or "" }   # absent ⇒ "" ⇒ no change
   ```

   - Default absent/empty ⇒ identity ⇒ existing hashes unchanged (no storm).
   - Set `audio_rebuild` (a short token, e.g. the fixing PR/issue id) on **just the affected records**
     → their spec changes → new content-addressed key → those (and only those) re-encode on the next
     enrich pass; orphan-GC reclaims the old objects on the usual cycle. Subscribers keep their stable
     `uid`/`<guid>`, so no re-subscribe — only the enclosure URL rolls.
   - `AUDIO_PIPELINE_VERSION` stays reserved for genuinely *global* recipe changes (a codec/loudness
     policy you intend to apply to everything). The nonce is the **surgical** instrument; the version
     is the **blunt** one. The two are independent inputs to the same hash.

   Selecting the affected set is a predicate, not a guess: typically a window of encode times (the
   buggy code ran between deploy X and the fix), or "spec_hash == legacy", or a body/source filter,
   or an explicit uid list. The window must be a **date range**, not just a single before/after bound
   — a bug introduced in one deploy and fixed in a later one affects only the episodes encoded
   *between* those two points, and re-encoding files outside that window is wasted work. That is
   exactly the input a small ops CLI takes:

   ```
   # date-range select (inclusive bounds; either bound optional → open-ended on that side):
   citypods rebuild-audio --encoded-after 2026-06-03 --encoded-before 2026-06-10 --reason fix-pr-####
   citypods rebuild-audio --encoded-before 2026-06-10 --reason fix-pr-####   # open-ended start
   citypods rebuild-audio --encoded-after  2026-06-03 --reason fix-pr-####   # open-ended end
   # other selectors:
   citypods rebuild-audio --source <key> [--body <name>] --reason fix-pr-#### # by source/body
   citypods rebuild-audio --uid <uid> [--uid ...] [--drop-object]            # one-off; --drop-object = case (1)
   ```

   (Selecting on encode time requires that each audio record carry the time it was encoded; INFRA-2
   adds it to the `audio{}` block if not already present.)

   The CLI only *stamps the records* (and optionally drops objects); the normal budgeted enrich/encode
   loop does the work, so a large surgical re-encode still spreads over runs under the wall-clock
   `stop()` budget — never a single blocking storm even when you *do* want everything rebuilt.

This is why "rev the `asset_version`" is the wrong default for a one-off corruption: `asset_version`
lives on an **insert** asset (the intro/outro stinger) and bumping it correctly re-encodes *every*
episode that embeds that stinger — that is the *global* semantics you want for "the stinger changed,"
but not for "this one file is corrupt." Corruption → drop the object (case 1) or stamp the nonce on
the affected set (case 2). Three independent levers, three blast radii: **object delete** (one file,
same recipe) ⊂ **`audio_rebuild` nonce** (a chosen set, code-fix) ⊂ **`AUDIO_PIPELINE_VERSION` /
`asset_version`** (everything sharing that recipe/asset).

---

## 5. Pipeline / stage changes

Ordering is the subtle part. Audio is content-addressed by a spec that now includes the timeline
**and** the (served-time) chapters, so everything that shapes the served bytes must precede the
encode. The new order (production splits cheap render vs heavy enrich as today — see
`render_stages`/`enrich_stages`):

```
chapters(fetch, source-time)            # existing; basis = source:s0
  → timeline(plan EDL)                  # NEW: silence/concat/intro planners emit Segments
  → remap(chapters source→served)       # NEW (or folded into timeline): chapters_basis = served
  → audio(render the Timeline)          # GENERALIZED encoder: trim/concat/insert/loudness + markers
  → transcript(reuse provider SRT→remap→served, OR ASR on served audio = born served)   # NEW (#1)
  → summary / links / soundbites        # feed-only / derived (consume served time)
```

Key points:
- **TimelineStage** is the new seam. Each edit feature is a *planner* it composes, not a new stage:
  silence-detector (#111), concat-planner (#122, from `parse_media_segments`), intro/outro inserter
  (#25). Host-all (#23) and loudness (#21) need **no** planner — host-all just flips `_should_host`
  for direct sources (identity timeline), loudness just sets `loudness_profile` (identity timeline,
  filter-only). They ride the generalized encoder.
- **Generalized encoder.** `FfmpegRunner.extract_audio` grows from "one input → copy/encode" to
  "render a Timeline": build the ffmpeg `concat`/`atrim`+`concat` filtergraph from the segments,
  splice insert assets, apply `loudnorm` when a profile is set, embed the (served-time) chapters.
  The fake ffmpeg in tests asserts the planned graph, so this is testable offline with no media.
- **ASR runs post-trim.** When no provider transcript exists, transcribe the **served** file so cues
  are born served-time (no remap, no drift) — exactly #111's "born aligned." When a provider
  transcript exists, parse it (source-time), `remap` it, mark `synced=true`; if it's untimed,
  `synced=false` and it renders as notes only.
- Reuses the existing `stop`/backoff machinery wholesale: the EDL plan is cheap (silence detect is
  one ffmpeg pass — gate it like an encode); the render is the expensive restartable unit already
  gated by `ctx.stop()`. Deferred ≠ failed; broken segments → #120 backoff.

---

## 6. Deep-linking and clip extraction (the "link back to the source video" half)

Two small, independently-useful pieces sit on top of the maps:

### 6a. Provider deep-link capability
Add to the provider Protocol (this is doc 02's "Change 8 — capability declaration" made concrete):

```python
capabilities: frozenset[str]            # e.g. {"agenda","chapters","transcript","video","deeplink"}
def video_deeplink(self, ref: str, t_seconds: float) -> str | None
```

Per provider: Granicus (`&starttime=`/MediaPlayer time param), Swagit (`/videos/{id}?...t=`),
CivicClerk (player bookmark/time), YouTube (`&t=Ns`). Returns `None` when unsupported (fall back to
the plain `watch_url`). This is what turns "served second 412" into a clickable
"watch this moment on the city's video": `served_to_source(tl, 412) → (s_id, t_src) →
deeplink(sources[s_id].ref, t_src)`.

### 6b. Clip service (`citypods/clips.py`)
One function powers soundbites (#15) **and** a future video-clip compilation:

```python
def extract_clip(ep, served_start, served_end, *, kind="audio"|"video") -> ClipArtifact
```

It forward-maps the served range through the EDL into one-or-more **source** ranges (a clip can span
a concat boundary → multiple source cuts), ffmpeg-cuts each source, concatenates, uploads a
content-addressed object keyed by `(uid, served_range, timeline_version, kind)`. Soundbite =
audio clip + a `<podcast:soundbite startTime=… duration=…>` tag (those offsets are **served**/enclosure
time — exactly what the EDL produces, no extra work). Video compilation = the same call with
`kind="video"`. Deep-link attribution per clip comes from 6a.

This cleanly answers the user's two scenarios: (1) "compile a set of video clips derived from the
silence-clipped podcast audio timestamps" = `extract_clip(kind="video")` over served ranges; (2)
"concatenated audio (#122) links to the externally-hosted video clips for the same reason" =
each concat source already carries its `watch_url`/`deeplink`, so per-chapter source attribution is free.

---

## 7. Cross-cutting concerns

**Storage model.** Nothing new in shape — mirror audio. Small structured data (the EDL, source
registry, served-time chapters, transcript pointer) lives **in the EpisodeRecord JSON** (cheap,
already synced to the bucket). Large blobs (transcript VTT/JSON, clip media) are **content-addressed
bucket objects** referenced by key+spec_hash, never committed to `docs/`. Storage stays a non-issue
(doc 03: 1,000 cities full audio ≈ $26/mo; transcripts ≈ 100–150 KB each; EDLs are bytes).

**Determinism / reproducibility.** Silence detection and loudness analysis are the only
non-trivially-deterministic steps. Pin their parameters and an explicit `version` (part of
`Timeline.version` / `loudness_profile`), **persist the resulting EDL**, and never recompute on a
plain render. A reproducible build re-derives the *same* EDL only when a version bumps — at which
point content-addressing + orphan-GC handle the rollover. Pin the ffmpeg major version in CI/Actions
so `loudnorm`/`silencedetect` output doesn't drift under us.

**Verification (new contract checks, extend `contracts.py`/`audit.py`).**
- Timeline integrity: `Σ segment lengths == served audio duration` (± a frame); segments are
  non-overlapping, monotonic, and cover `[0, duration]`.
- Every source segment's `[source_start,source_end]` lies within `SourceMedia.duration`.
- Remapped chapter/transcript offsets all fall in `[0, served_duration]`; none reference a cut span.
- Endpoint contract: provider `video_deeplink` returns a 2xx page (sampled), like the existing
  enclosure-liveness check.

**Error handling.**
- *Missing source segment* (e.g. a Swagit keyless `dfile`, #120): the planner can't build a complete
  EDL → defer the whole meeting via the existing materialization backoff. **Decision needed:** for a
  multi-segment meeting missing a *middle* segment, do we (a) defer the whole meeting [safe default,
  recommended], or (b) serve it with a labeled `insert:"gap"` + a chapter annotation? Recommend (a)
  until we see real prevalence; the model *supports* (b) without change if we later want it. **Decision made:** Agree, we should (a) defer the whole meeting, while filing a ticket in the audit flow consistent with other enclosure issues with existing feed structure.
- *Untimed provider transcript*: detected, `synced=false`, rendered as notes — never mis-aligned.
- *Partial enrich* (run yields mid-backfill): each artifact is independent and content-addressed, so
  a half-done meeting just lacks the not-yet-produced artifact and is picked up next run (status quo).

**Migration / backward-compat.** The identity-timeline equivalence (§2e/§4) is the linchpin: shipping
the model is a **no-op for existing audio** because identity episodes serialize to today's spec hash.
The one true behavior change is **duration semantics** (source→served), which only differs for
manipulated episodes; pin it with a migration test. Schema v2 records carry new optional blocks;
v1 records upgrade lazily on next enrich (like the legacy-manifest carry-over already in
`migrate_legacy_manifests`). All of this happens during beta (banner up) — the documented window for
any one-time churn, consistent with the stable-UID migration note in the architecture memory.

**Future: hosting video (not planned now, but the model already fits both shapes).** We do not host
or store video today. Two future video features were raised; the model accommodates each as an
*additive* block, no rework:
1. **Served/derived video** (chop & host video the way we host the audio podcast — e.g. a video feed,
   or per-meeting clips). This is the *same* `Timeline` rendered to a video artifact: add a parallel
   `video{}` block beside `audio{}` on the record (its own content-addressed key + spec hash, where
   the spec adds video codec/resolution to the audio spec inputs). The generalized encoder (INFRA-3)
   already takes a `Timeline`; it gains a `kind="video"` render path, and `clips.extract_clip`
   (INFRA-7) already has `kind="video"`. Trim/concat/insert/deep-link **all carry over unchanged** —
   the served↔source maps are media-agnostic.
2. **Source backup** (archive the original video to guard against the city deleting it). This is the
   `SourceMedia.backup_key` field (§2a): on archive, copy the source MP4 to a content-addressed bucket
   object and record its key; `resolve_media_url`/clip extraction can then prefer the backup when the
   provider URL dies (a natural extension of the issue-#116 dead-pointer handling). No timeline change
   at all — it is just a second location for an existing source.

Storage is the only real consideration: video is ~10–50× audio per minute, so unlike audio (§ Cost
below) it is **not** negligible — both features must land as explicit projection knobs (doc 03) before
turning on, exactly as host-all-audio (#23) is today. Flagged here only so neither future feature
forces a schema migration; both are `None`/absent until built.

**Cost.** Trim/concat/loudness/intro are ffmpeg (free CPU). Host-all is storage (~$1/mo at current
scale, doc 03). The constraint is the **encode backlog** — and that's *already* governed by the
wall-clock `stop()` budget + #120 backoff; manipulated episodes are just encodes like any other. No
new cost lever. The EDL/transcript JSON adds negligible storage.

**Things this also forces us to get right (worth naming):**
- `<itunes:duration>` and enclosure `length` must come from the **served** artifact, not the source
  feed's advertised duration.
- Chapter `end` handling across trimmed recess gaps (already half-handled in `_ffmetadata`).
- Per-meeting permalink page (#46) is the natural surface to render served-time transcript/chapters
  **with** per-cue source deep-links — i.e. #46 should be designed against this model, not before it.

---

## 8. Impact on the named issues / roadmap items

- **#111 (silence-trim / timeline-transform, P1).** *Becomes the first consumer, not the owner, of
  the model.* Its "cut-map" **is** the Timeline; its chapter/transcript remap **is** `remap()`; its
  "ASR after trim" **is** the enrich ordering in §5. Implementing #111 = a silence-detector planner +
  using the generalized encoder + the remap step. Much smaller once the infra below exists.
- **#122 (multi-segment Swagit concat, P5).** A **concat-planner** that turns `parse_media_segments`
  output into N `SourceMedia` + end-to-end segments; the generalized encoder renders it. Today's
  "defer multi-segment" branch in `swagit.resolve_media_url` is replaced by emitting the EDL. Its
  deferred-audio feed-health finding clears automatically (as the issue already anticipates). Could
  be pulled earlier than P5 cheaply once the concat-capable encoder exists.
- **#23 (host-all-audio, P1.5).** Mostly *unchanged in spirit* — flip `_should_host` to opt-in
  direct re-hosting via a projection knob (doc 03) — but it should land **after** the model so that
  re-hosted Granicus audio carries a (identity) timeline + source registry + deep-link, making its
  chapters/transcript first-class like everyone else's. No remap (identity).
- **#21 (loudness, P1.5).** A `loudness_profile` field on the spec; identity timeline; filter-only
  encode. Trivial once the encoder takes a profile.
- **#25 (intro/outro stinger, P3).** An insert-planner + a versioned brand asset; the only feature
  that adds `insert` segments. Validates that half of the model end-to-end.
- **#1 / #11 / #3 (transcripts, P1/P1.5/P2).** Ride doc 02 Change 4 (transcript artifact storage) +
  this doc's basis convention + remap. Reuse-first provider transcripts are source-time (remap);
  ASR is served-time (born aligned). Per-item summaries (#3) = `remap` chapter spans onto the
  transcript and summarize each span.
- **#15 (soundbites, P2.5).** `clips.extract_clip` + `<podcast:soundbite>` in served time. Free given §6b.
- **#46 (per-meeting permalink pages, P2).** The display surface for served-time transcript/chapters
  + per-cue source deep-links. Design it against §6, not before.
- **#18 (newsletter, P3).** "Jump to this quote in the city video" = served→source→deeplink. Same path.
- Doc 02 **Change 4** (transcript storage), **Change 8** (provider capabilities) are *subsumed/made
  concrete* here (capabilities now include `deeplink`; transcript block defined in §4).

Net effect on the roadmap: the audio-cleanup band (#111/#122/#23/#21/#25) and the transcript band
(#1/#11/#3/#15) **share one foundation**. Build the foundation as its own issues (below), then each
feature is a small planner/consumer PR — exactly the "don't commingle" goal.

---

## 9. Proposed new infrastructure issues (file these standalone, build them first)

Each is independently testable (pure modules + fake-ffmpeg graph assertions + record round-trips),
each closes cleanly, and none ships a user-visible feature on its own — which is the point: keep the
foundation out of the feature PRs for traceability.

> **INFRA-1 — Timeline/EDL core (`citypods/timeline.py`).** Pure dataclasses (`SourceMedia`,
> `Segment`, `Timeline`), `served_to_source` / `source_to_served` / `remap`, identity-timeline
> constructor, `timeline_digest` (canonical, `""` for identity). No pipeline wiring. **Accept:** golden
> tests for forward/inverse/remap incl. cut-span drops, concat boundaries, inserts, identity == today.
> *Deps: none.* *Unblocks all below.*

> **INFRA-2 — EpisodeRecord schema v2 + spec-hash generalization.** Add `sources[]` (incl. optional
> `backup_key`), `timeline{}`, `chapters_basis`, `transcript{}`, `audio.duration_served`, and the
> `audio_rebuild` nonce; generalize `audio_spec_hash` (§4) with the identity-equivalence guarantee;
> bump `SCHEMA_VERSION`; lazy v1→v2 upgrade. Record the **encode time** on the `audio{}` block (if
> not already present) so rebuilds can select by encode window. Ship the surgical-rebuild ops CLI
> `citypods rebuild-audio` (§4): stamp the nonce on a predicate-selected set — a **date range**
> (`--encoded-after`/`--encoded-before`, either bound optional), a source/body filter, or an explicit
> `--uid` list — or `--drop-object` for one-off corruption.
> **Accept:** identity episodes hash byte-identically to v1 (no re-encode storm); round-trip tests;
> duration-semantics migration test; setting `audio_rebuild` re-keys *only* the stamped records;
> a date-range select stamps only records whose encode time falls **within** the range (inclusive,
> open-ended bounds honored); dropping an object re-encodes the *same* key. *Deps: INFRA-1.*

> **INFRA-3 — Timeline-aware encoder.** Generalize `FfmpegRunner.extract_audio` / `materialize_audio`
> to render a `Timeline` (trim via `atrim`+`concat`, multi-input `concat`, insert splice, optional
> `loudnorm`), embedding served-time chapters. **Accept:** fake-ffmpeg asserts the planned filtergraph
> for identity/trim/concat/insert/loudness; identity path equals today's copy/encode args. *Deps: 1,2.*

> **INFRA-4 — TimelineStage + planner interface.** New stage (ordered before `audio`) that composes
> registered planners into the persisted EDL; a `TimelinePlanner` protocol so silence/concat/intro
> features plug in. **Accept:** stage produces+persists an identity EDL when no planner fires (no-op);
> a fake planner composes correctly; ordering test (timeline before audio, remap before audio).
> *Deps: 1,2.*

> **INFRA-5 — Artifact remap + served/source basis convention.** The remap step for chapters and
> timed transcripts, untimed-transcript detection, and the basis field plumbing through
> records/render. **Accept:** source-time chapters remap onto a trimmed EDL correctly; cut chapters
> drop; untimed transcript flagged `synced=false`. *Deps: 1,2.*

> **INFRA-6 — Provider deep-link capability.** `capabilities` frozenset + `video_deeplink(ref, t)` on
> the Protocol; implement for Granicus/Swagit/CivicClerk (YouTube when that provider lands). **Accept:**
> per-provider unit tests of the generated URL; capability gating; endpoint-contract liveness sample.
> *Deps: none (parallelizable). Powers §6a and clip attribution.*

> **INFRA-7 — Clip service (`citypods/clips.py`).** `extract_clip(ep, a, b, kind)` over the EDL
> forward-map; content-addressed clip objects. **Accept:** a served range spanning a concat boundary
> maps to the right multiple source cuts; fake-ffmpeg asserts the cut+concat plan. *Deps: 1,3,6.*
> *(Soundbites #15 and video-clip compilation both consume this; building it standalone keeps #15 thin.)*

> **INFRA-8 — Transcript artifact storage (doc 02 Change 4, restated).** Content-addressed transcript
> objects + `transcript{}` block + `TranscriptStage` scaffold (reuse-first slot + ASR slot) +
> `<podcast:transcript>` emission gated on basis/synced. **Accept:** reuse-first provider transcript
> stored+remapped+referenced; ASR slot stubbed; feed emits the tag only when `synced`/present.
> *Deps: 1,2,5. (#1/#11 build on this.)*

> **INFRA-9 — Timeline/clip verification + contracts.** The §7 integrity checks in
> `contracts.py`/`audit.py`. **Accept:** synthetic bad EDLs (overlap, out-of-range remap, duration
> mismatch) are caught; deep-link liveness sampled. *Deps: 1,2,6.*

**Suggested sequence:** INFRA-1 → 2 → (3, 4, 5 in parallel) → 6 → 7/8/9. Then features land as thin
PRs in roadmap order: #111 (silence planner + remap), #21 (loudness profile), #23 (host-all knob),
#25 (intro planner), #122 (concat planner), then the transcript band (#1/#11/#3) and #15/#46.

---

## 10. Open decisions for the maintainer

1. **Missing-middle concat policy (§7):** defer the whole meeting [recommended] vs. serve with a
   labeled `gap` insert. Model supports both; pick the default. **decision:** Defer, as stated earlier in the body.
2. **Store source-time chapters too, or derive on demand** from `sources`+`timeline`? Recommend
   derive-on-demand (don't duplicate; served-time is canonical). **decision:** Agree.
3. **Duration source of truth:** adopt served-duration for `<itunes:duration>` now (correctness) —
   confirm we accept the one-time value change for any episode we later trim. **decision:** Agree
4. **Doc/issue home:** file INFRA-1..9 as GitHub issues now (traceability) vs. keep them in this doc
   until the foundation work starts. (Roadmap currently files issues just-in-time.) **decision:** File now, with priorities set by phasing plan.
5. **Pin ffmpeg version** in CI/Actions to stabilize `loudnorm`/`silencedetect` — agree? **decision:** Yes.

---

## 11. Issues filed (2026-06-03)

Tracking epic: **#141** — *Epic: Timeline & content-transform foundation*. All issues carry the
`area:timeline` label.

**Foundation (build first; higher priority than the P1 features that consume them).** DAG:
`1 → 2 → (3,4,5 ∥) → 6 → (7,8,9 ∥)`.

| # | Issue | Deps |
|---|---|---|
| INFRA-1 | **#142** Timeline/EDL core (`citypods/timeline.py`) | — |
| INFRA-2 | **#143** Record schema v2 + spec-hash generalization + surgical-rebuild CLI | #142 |
| INFRA-3 | **#144** Timeline-aware encoder | #142, #143 |
| INFRA-4 | **#145** TimelineStage + planner interface | #142, #143 |
| INFRA-5 | **#146** Artifact remap + served/source basis | #142, #143 |
| INFRA-6 | **#147** Provider deep-link capability | — (parallel) |
| INFRA-7 | **#148** Clip service (`citypods/clips.py`) | #142, #144, #147 |
| INFRA-8 | **#149** Transcript artifact storage (doc-02 Change 4) | #142, #143, #146 |
| INFRA-9 | **#150** Timeline/clip verification + contracts | #142, #143, #147 |

**Features (thin planner/consumer PRs), in suggested build sequence (roadmap priority in parens):**

1. **#111** silence-trim (P1) — deps #142,#143,#144,#145,#146
2. **#151** loudness normalization (P1.5) — deps #142,#143,#144
3. **#152** host-all-audio (P1.5) — deps #142,#143,#144
4. **#153** intro/outro stinger (P3) — deps #142,#143,#144,#145
5. **#122** multi-segment concat (P5; pullable earlier) — deps #142,#143,#144,#145 — **Shipped PR #182**
6. **#110** ASR transcripts (P1) — deps #142,#143,#146,#149
7. **#154** `<podcast:transcript>` (P1.5) — deps #149 + #110
8. **#155** per-agenda-item summaries (P2) — deps #110 + #146
9. **#156** soundbites (P2.5) — deps #148
10. **#157** per-meeting permalink pages (P2) — designed against #146,#147,#149

(Existing issues #111/#122/#110 were updated with a comment placing them in the sequence + the
`area:timeline` label rather than re-filed.)
