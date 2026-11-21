# 61 — Video slide extraction as timed chapter images

Status: **L3 — approved 2026-11-20 (maintainer: design first, slide sub-chapters, no M4A
artwork, on by default for every city).** Tracks
[GH#2707](https://github.com/BashfulBits/city-meeting-podcasts/issues/2707). Supersedes the
deferred backlog item **#26 chapter images** ([review/01](01-feature-brainstorm.md),
[review/11](11-technical-design-roadmap.md) §4 "Deferred backlog" and §6). This document changes
no code; the slices in §9 may land as one PR, one commit per slice.

**Implemented 2026-11-20** in one PR (PR1–PR4 of §9),
[#2754](https://github.com/BashfulBits/city-meeting-podcasts/pull/2754). Where the code differs from the text below, §13 lists the
difference and the reason; the sections themselves are updated to match the code.

## 1. Question

Meetings often show slides, site plans, zoning maps and budget charts. Listeners hear "as you can
see on this slide" with nothing to look at. Can Citypods find the static presentation segments in a
meeting's video, keep one image per slide, and show it in podcast apps at the right moment? The cost
must stay far below re-hosting video, and the audio must not be re-encoded.

## 2. Decisions taken (2026-11-20)

| # | Decision | Why |
|---|---|---|
| D1 | Design doc first; code in later PRs after review | Deferred-backlog promotion (AGENTS.md "When a change deviates from the plan") |
| D2 | Slides appear as **extra chapter entries** with `img` and `"toc": false` | Agenda chapters stay as they are; apps that ignore `toc` still show the image |
| D3 | **No embedded M4A chapter artwork** | Embedding changes audio bytes, `audio_spec_hash` and forces catalog re-encodes. Kept as a later evaluation (§11) |

Further defaults this document proposes. The maintainer may overturn any of them in review:

- **P1** JPEG, not WebP. Podcasting 2.0 apps and Apple Podcasts render JPEG and PNG chapter images
  reliably; WebP support is uneven.
- **P2** Images live on the **primary (B2) audio store**, never R2, because R2 holds only
  ephemeral/derivable objects (ARCHITECTURE "Object storage" invariant).
- **P3** A **dedicated opt-in stage and lane** (`slides`), like R6's `r6_stages()`, not part of
  `enrich_stages()`. Video download is the expensive part and must not lengthen the audio lane.
- **P4** **On by default** (maintainer decision 2026-11-20, replacing the proposed off-by-default
  pilot). Every city runs slides unless its config sets `slides.enabled: false`; the site switch
  `slides.enabled` is the kill switch. The per-run caps in §7 bound cost while the archive drains,
  and §10 becomes a quality review of the first runs instead of a gate before enabling.

## 3. Goals and non-goals

Goals

1. Detect intervals where the video is a static slide for at least `min_duration_seconds`.
2. Capture one representative, compressed image per kept interval.
3. Publish the images in the Podcasting 2.0 chapters JSON, timed in the enclosure's clock
   (served time when the audio is re-hosted with edits).
4. Leave `audio_spec_hash` and audio bytes unchanged; change only `feed_content_hash`.
5. Bound runner disk, bandwidth, storage and slide count per episode.

Non-goals (this design)

- Embedded M4A chapter artwork (D3).
- OCR or LLM-generated slide titles. Slide entries reuse the enclosing agenda chapter title (§6.3).
- Agenda-packet PDF page rendering (the old #26 approach; rejected in the issue).
- Slides on the meeting web page. A small follow-up once the feed path is proven (§11).
- Re-hosting video.

## 4. What exists today (seams this design uses)

| Seam | Where | Relevance |
|---|---|---|
| Chapters JSON sidecar | `citypods/feeds.py:chapters_json`, `chapters_url`; written by `run.py` (`docs/<slug>/chapters/<uid>.json`) | Gains `img`, `endTime`, `toc` entries. `chapters_url` returns `None` today when an episode has no chapters |
| Public chapter view | `citypods/chapters.py:episode_public_chapters` / `episode_served_chapters` | Defines the clock slides must use |
| Timeline mapping | `citypods/timeline.py:source_to_served`, `remap(..., snap_cut_starts=True)`, `timeline_digest` | Source → served mapping for slide starts and ends |
| Source media resolution | `stages.py:_moment_source` (R6): `provider.resolve_media_url(ep, city.source)`, or `ep.sources[i].ref` for concat | Same resolution for slide detection, one pass per source |
| Video via ffmpeg, streamed | `citypods/video_clips.py` (R6), `validate_source_url` SSRF gate | Precedent: ffmpeg reads the remote URL directly; no full download to disk |
| Opt-in stage list | `stages.py:r6_stages()`, wired in `run.py` when `site_config.moments.enabled` | Pattern for `slides_stages()` |
| Lane ownership | `stages.py:LANE_STAGES`, `records.py:_LANE_OWNED_BLOCKS` / `protected_blocks_for_lane` | New owned block `slides` |
| Stage completion envelope | `stage_input_fingerprint`, `stage_is_dirty`, `_mark_stage_complete` (GH#1013) | Dirty-only scheduling; deferred ≠ failed |
| Feed hash | `records.py:feed_content_hash` (already includes `moment_video_clip`) | Gains `ep.slides` |
| Orphan GC | `audio-gc.yml` / storage reclaim (GH#496) | Must learn the `slides/` prefix |
| Pillow, OpenCV | `pyproject.toml` (already dependencies) | Image checks and encoding; no new dependency |

The audio chapter list embedded in the M4A (`media.py:_ffmetadata`, fed from served chapters) must
**not** receive slide entries. Slides live in their own record block and merge only at JSON render
time. That keeps D3 and goal 4.

## 5. Detection and capture

### 5.1 Pass 1: find static intervals (one streamed ffmpeg read per source)

```
ffmpeg -nostdin -hide_banner -skip_frame nokey -i <url> -map 0:v:0 -an -sn -dn \
  -vf "scale=320:-2,format=gray,freezedetect=n=<noise>:d=<min_duration>,metadata=print:file=-" \
  -f null -
```

- `-skip_frame nokey` decodes keyframes only (typically every 2 to 4 s on HLS and Swagit MP4). That
  cuts decode CPU by an order of magnitude. Static detection at a 15 s floor does not need every
  frame. PR2's spike (§9) measures CPU, wall time and bytes on real Granicus HLS and Swagit MP4.
- Downscaled grayscale makes the freeze comparison cheap and less sensitive to encoder noise.
- ffmpeg reads the network stream; nothing but the parsed `lavfi.freezedetect.freeze_start/end`
  lines reaches disk. Bytes still cross the network (a 3 h meeting is roughly 1 to 3 GB of video);
  that is the main cost and why the lane is separate and capped (§8).
- Defaults: `min_duration_seconds: 15`, `noise_db: -50` (ffmpeg `n` in dB). Both configurable.
- A URL that fails `validate_source_url` or has no video stream records `status: "no-video"` and is
  terminal for that source identity (not retried every run).

Parsing is a pure function, `slides.parse_freeze_intervals(text) -> list[Interval]`, so it is unit
tested on captured ffmpeg output without running ffmpeg.

### 5.2 Pass 2: capture one frame per candidate

For each candidate interval `[s, e)` (after the filters in §5.3 that need no pixels), seek and grab
one frame at `s + settle`, where `settle = min(3.0, (e - s) / 3)`. That skips slide-in animations,
fades and an early cursor movement:

```
ffmpeg -nostdin -ss <s+settle> -i <url> -map 0:v:0 -frames:v 1 -f image2pipe -vcodec png -
```

Input seeking is a range or segment fetch, so pass 2 costs a few MB per slide, not a re-read.
Pass 2 runs at most `max_slides_per_episode + slack` times (slack absorbs filter rejections).

### 5.3 False-positive filters (in order; each rejection is counted by reason)

| Filter | Rule (defaults) | Catches |
|---|---|---|
| Edge trim | Drop intervals that start in the first `edge_trim_seconds: 120` of the source **before** the first agenda chapter (single-source episodes), or start in the last `edge_trim_seconds` | Pre-meeting holding slates, "meeting will begin shortly" cards, end slates |
| Flat frame | Luma standard deviation < `min_luma_stddev: 8` (Pillow `ImageStat`) | Black, blue or blank screens |
| Low detail | Edge density < `min_edge_density: 0.004` (share of pixels ≥ 40 after Pillow `FIND_EDGES` on 320 px gray). Real slides measured 0.2 to 0.3 | Empty-room and empty-dais camera shots, which are soft compared with slide text and lines |
| In-episode recurrence | Perceptual hash (64-bit dHash) recurs in ≥ `max_recurrence: 3` disjoint intervals | Slates and logo cards shown between items |
| City denylist | dHash within Hamming 6 of any `slides.exclude_dhashes` entry in the city config | Known per-city slates the other rules miss |
| Consecutive duplicate | dHash within Hamming 6 of the previous kept slide → merge intervals | A slide re-detected after a brief cursor move |

Known limitation: picture-in-picture layouts (slide plus a moving presenter inset) do not freeze,
so they are missed. A per-city `detect_region` crop could fix it later. It is not in this design.

Cross-episode recurrence (the same frame in several meetings of one body, which reliably marks room
shots and slates) is effective but needs new per-body state. It is held back as an optional slice
(§9 PR5), triggered only if the first-runs false-positive rate fails the §10 review.

### 5.4 Image recipe

- Letterbox or pad to **1280×720**, preserving aspect ratio (Pillow `ImageOps.pad`, black bars).
  Apple's square chapter-art guidance is a recommendation; 16:9 slides cropped to square lose
  content, and the major Podcasting 2.0 apps render non-square chapter images.
- JPEG, progressive, quality 82, stepping down by 8 to a floor of 58 until ≤ `max_image_kb: 150`.
  Typical slides land at 50 to 120 KB.
- Strip metadata. Constant recipe string `SLIDES_IMAGE_RECIPE = "jpeg-1280x720-pad-q82-v1"`.

## 6. Timeline, storage and record

### 6.1 Clock

Detection yields **source-time** intervals per `source_id`. Served times come from
`timeline.remap(tl, items, source_id=..., snap_cut_starts=True, clamp_to=served_duration)`.
For identity episodes, served time equals source time. Slides whose served span is shorter than
`min_duration_seconds / 2` after mapping are dropped (mostly cut by silence trims). Slides use the
same clock as `episode_served_chapters`, so agenda chapters and slides always agree.

The block stores **source times only**. Served times are computed at render time
(`slides.episode_served_slides`), so a timeline change needs no stage run at all. A slide whose
start and end both fall in cut spans is dropped. Only a source, recipe or detection-config change
re-runs detection.

### 6.2 Storage key and record block

- Key: `slides/<uid>/<sha256(image bytes)[:24]>.jpg`, content-addressed, written with `put_file`
  only when the key is absent. Public URL from `storage.public_url(key)`.
- New `Episode.slides: dict` (default `{}`), its own artifact block `slides` in `records.py`
  (`ARTIFACT_BLOCKS`, `_LANE_OWNED_BLOCKS["slides"]`, serialize and parse, `merge_persisted`
  carry-over):

```json
{
  "status": "ready" | "none-found" | "no-video" | "failed",
  "recipe": "<SLIDES_PIPELINE_VERSION>:<SLIDES_IMAGE_RECIPE>",
  "detect_fingerprint": "<sha of the detection config>",
  "items": [
    {"source_id": "s0", "source_start": 812.0, "source_end": 905.0,
     "key": "slides/<uid>/<hash>.jpg", "url": "https://…/slides/<uid>/<hash>.jpg",
     "dhash": "f0e1…", "bytes": 84213}
  ],
  "rejected": {"edge_trim": 2, "flat": 1, "low_detail": 4, "recurrence": 3},
  "capped": false,
  "streamed_bytes_estimate": 270000000,
  "bytes_written": 168426,
  "detected_at": "2026-11-20T12:00:00+00:00"
}
```

- `none-found` and `no-video` are terminal complete results (GH#1013 envelope), not failures.
- `failed` carries `attempts` and `reason`. The stage reports an error (so the episode stays
  dirty and retries) for the first two failures; the third is recorded as a quality outcome and
  the episode is left alone until its inputs change.

### 6.3 Chapters JSON merge (feed-only)

`chapters_json` gains slide entries, sorted with agenda chapters by `startTime`:

1. A slide that starts within 2 s of an agenda chapter's start puts its `img` **on that agenda
   chapter** (no extra entry).
2. Otherwise it adds `{"startTime", "endTime", "title": <enclosing agenda chapter title or
   "Presentation">, "img": <url>, "toc": false}`.
3. When a slide ends more than 5 s before the next chapter or slide starts, a reset entry
   `{"startTime": <slide end>, "title": <enclosing title>, "toc": false}` follows (no `img`), so
   apps that keep the last image until the next chapter fall back to episode art.
4. Times are rounded to whole seconds like today's entries; a slide that rounds onto an existing
   entry's second is dropped.
5. `chapters_url` returns a URL when the episode has agenda chapters **or** ready slides.
   Episodes with slides but no agenda chapters get a valid document (slides plus resets).

The `toc` and `endTime` fields are in the Podcasting 2.0 JSON chapters spec 1.2.0, so the document
version string is unchanged.

### 6.4 Hashes and invalidation

- `feed_content_hash` adds `e.slides` (like `moment_video_clip`). First deploy after PR3 re-renders
  once (cheap; see the note in its docstring).
- `audio_spec_hash`, AudioStage inputs and `_ffmetadata` input are untouched. PR3 adds a regression
  test asserting the audio spec hash is identical with and without a slides block.
- `meeting_page_hash` is untouched until the meeting-page follow-up.

## 7. Stage, lane and config

- `SlidesStage` (`name = "slides"`, `version = "1"`) in `citypods/stages.py`; detection, filters and
  image recipe in a new `citypods/slides.py` (pure where possible). `slides_stages()` returns
  `[SlidesStage()]`.
- `run.py` runs `slides_stages()` only in `enrich --lane slides`, and only while
  `site_config.slides.enabled` is not false; `LANE_STAGES["slides"] = frozenset({"slides"})`.
  A city runs the stage unless its own config sets `slides.enabled: false`.
- Input fingerprint (dirty check): video URL, source refs, `SLIDES_RECIPE`
  (`SLIDES_PIPELINE_VERSION` + image recipe) and the city's `slides` override. Per-episode
  detection is gated on `ctx.stop()` and the shared per-run caps (`SlidesRunBudget`).
- Eligible episodes: `media_availability` not withheld and a video present (`video_url` or
  sources). Served times are computed at render, so audio need not be planned first. Order: the
  lane's normal newest-first candidate order.
- Workflow `slides.yml` (PR4): scheduled a few times a day, `concurrency` per lane, bounded by the
  wall-clock stop budget and `max_episodes_per_run`.

Config (`config/site_config.yml` defaults; per-feed override as a `slides:` block in
`config/feeds/*.yml`):

```yaml
slides:
  enabled: true             # site kill switch; a city opts out with `slides.enabled: false`
  min_duration_seconds: 15
  noise_db: -50
  edge_trim_seconds: 120
  min_luma_stddev: 8
  min_edge_density: 0.004
  max_recurrence: 3
  max_slides_per_episode: 50
  max_image_kb: 150
  max_episodes_per_run: 20
  max_video_gb_per_run: 40  # stop starting new detections once streamed bytes pass this
  exclude_dhashes: []       # per-city only
```

Per-feed values stay in `City.extra["slides"]` (the loader's home for unknown feed keys) and are
merged over the site defaults by `slides.effective_config`. `config.py` validates types and
ranges at load time, so a bad block fails config loading rather than a run.

## 8. Guardrails and expected cost (estimates; PR2 measures)

| Item | Bound | Estimate |
|---|---|---|
| Runner disk | Only one PNG and one JPEG per slide at a time | < 10 MB |
| Network per episode | One video read + ≤ 50 seeks | 1 to 3 GB for a 3 h HLS meeting |
| Network per run | `max_video_gb_per_run` | ≤ 40 GB |
| CPU | Keyframe-only decode at 320 px | Target < 0.1× real time; measured in PR2 |
| Slides per episode | `max_slides_per_episode` (keeps the first N by start) and sets `capped` | 5 to 20 typical |
| Storage | ≤ 50 × 150 KB = 7.5 MB worst case per episode | ~1 MB typical; 1,000 episodes ≈ 1 GB on B2 |
| Egress | B2 via the Cloudflare CDN (free egress, as for audio) | Negligible |

The first-runs review (§10) records actual numbers in this table.

**Measured during implementation (2026-11-20, one Swagit MP4, local Mac):** keyframe-only
detection over 1,500 s of a 640×360, 0.83 Mb/s Addison Planning & Zoning meeting took 25 s
(≈ 0.017× real time). It found five static intervals of 35 to 95 s; all five were real staff
report slides and all passed the pixel filters (luma σ 49 to 56, edge density 0.21 to 0.32). A
2.5 h meeting at that bit rate streams about 0.9 GB.

## 9. Delivery slices

| PR | Scope | Acceptance |
|---|---|---|
| PR1 | `citypods/slides.py`: freeze-output parser, interval filters (edge trim, recurrence, duplicates, cap), dHash, image recipe, served remap helper. No pipeline wiring | Unit tests on captured ffmpeg output and synthetic Pillow images; ruff clean |
| PR2 | Spike + `SlidesStage`: ffmpeg passes, storage upload, `Episode.slides` block and record round-trip, lane ownership, config validation, run wiring (on by default, P4). Spike results (CPU, bytes, wall time, false-positive rate on 5 real episodes per provider) appended to §8 and §10 here | Stage tests with a fake ffmpeg and local storage; record merge keeps the block across lanes; dry run writes nothing |
| PR3 | Feed: `chapters_json` merge (§6.3), `chapters_url`, `feed_content_hash` | Golden JSON tests; audio-spec-hash invariance test; feed validates (`citypods validate-build`) |
| PR4 | `slides.yml` workflow, `/admin/status` counters (ready, none-found, rejected by reason, bytes streamed), orphan GC learns `slides/`. Enabled for every city by default (P4) | Workflow dry run; GC test keeps referenced and drops unreferenced `slides/` keys |
| PR5 (optional) | Cross-episode per-body recurrence denylist | Only if the §10 first-runs review fails |

**Backfill story:** a new stage at `version = "1"` with no prior artifacts; nothing is invalidated.
Turning the feature on queues every city's archive newest-first under the per-run caps. Bumping
`SLIDES_PIPELINE_VERSION` or the image recipe later re-detects a city's episodes gradually; such a
PR must say so (AGENTS.md "Pipeline-version bumps state their backfill story").

## 10. First-runs quality review

The feature is on for every city from the first deploy (P4), so there is no pilot gate.

1. After the first scheduled runs, sample 20 episodes with ready slides across at least two
   providers. Hand-label each kept slide as slide or not-slide, and spot-check 5 episodes for
   missed slides.
2. Targets: kept-slide precision ≥ 90%, no slate or black frame in the feed, measured CPU and bytes
   within §8 bounds, and chapters render in at least two apps (for example Podverse and
   AntennaPod; Apple Podcasts is checked but not a target, because its support for JSON chapter
   images is not verified).
3. A city below target is opted out (`slides.enabled: false` in its config) while thresholds are
   tuned or PR5 lands. A widespread problem uses the site kill switch.

## 11. Risks, open questions and follow-ups

- **Decided:** a separate `slides.yml` workflow (§7), approved with this document.
- **Risk:** some providers' HLS keyframe interval is long (≥ 10 s). Then a 15 s floor sees one or
  two keyframes. PR2 measures; fallback is decoding at `fps=1` without `-skip_frame` for that
  provider (configurable `decode: keyframes | fps1`).
- **Risk:** camera shots of a still room pass the low-detail filter. Mitigated by the §10
  review, per-city opt-out and PR5.
- **Risk (unverified):** Apple Podcasts may show chapter images only from embedded M4A chapters,
  not from the JSON sidecar. Accepted under D3; PR4's app check settles it, and M4A artwork is the
  fallback follow-up.
- **Follow-up:** slides on the meeting page (thumbnail strip linked to timestamps).
- **Follow-up:** M4A chapter artwork, which needs an audio-hash and re-encode cost plan.
- **Follow-up:** OCR'd slide titles (would be untrusted text; SECURITY.md rules apply).

## 12. Do not modify (when implementing)

- `media.py:_ffmetadata` and the AudioStage chapter inputs (D3, §6.4).
- `audio_spec_hash` inputs.
- `enrich_stages()`, `render_stages()` and `default_stages()` membership (slides are opt-in, §7).
- The R2 ephemeral prefix list in `storage/routing.py` (slides are not on R2, P2).

## 13. Implementation notes (differences from the original text)

- **Served times at render.** The block stores only source times; `episode_served_slides`
  computes served times on every render with the chapters clock. That removes the stored served
  times, the stored `timeline_digest` and the remap-only stage path from §6.1 and §7.
- **Config location.** The override is a `slides:` block in the feed file (`config/feeds/*.yml`),
  kept in `City.extra`, not a new `City.slides` field or `config/cities/`.
- **Low-detail filter.** Pillow `FIND_EDGES` instead of OpenCV Canny, default `0.004`.
- **SSRF and protocols.** Every source, whatever its scheme, passes `validate_source_url` before ffmpeg runs, and
  ffmpeg gets `-protocol_whitelist crypto,data,http,https,tcp,tls` (the audio lane's list without
  `file`), so HLS is accepted as in the audio lane. A rejected URL is a failure that retries
  twice, not a terminal `no-video`.
- **Status counters.** Outcomes reach the run log through the generic stage counters (ran,
  quality outcome by status, deferrals by `stop` / `run-cap` / `byte-budget`). A dedicated
  `/admin/status` panel is not part of this change.
- **Capture budget.** Pass 2 captures at most `2 × max_slides_per_episode` frames per episode.
- **Episode-level byte reservation.** The stage probes every source of an episode first and reserves
  the episode's whole estimate in one `admit_bytes` call, so a byte-budget deferral happens before
  any detection and the next run never repeats half an episode. A probe with no duration or bit
  rate counts as 4 h at 8 Mbit/s, never zero.
- **Bounded candidate memory.** Each accepted frame is encoded to the final JPEG at once and only
  those bytes are kept until selection, never the decoded native-resolution frame.
- **Merge gap.** Consecutive near-identical frames merge only when at most 30 s apart; a slide
  shown again later stays two slides so the chapters keep the artwork reset in between.
