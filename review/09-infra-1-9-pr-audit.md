# INFRA-1 … INFRA-9 — senior code audit of the timeline-foundation PRs

**Audience:** a junior dev who will read the diffs tonight.
**Scope:** the nine open PRs (#159–#167) that implement the *Timeline & content-transform
foundation* designed in [`review/08`](08-timeline-and-content-transforms.md) and filed as GitHub
issues #142–#150 (epic #141). All nine are **green** (the full offline suite is **550 passing,
4 deselected**, of which **246 tests are new** across these PRs).

This document walks each PR the way I'd walk it in a review session: *what it does*, *the code that
matters*, *what's done well* (so you can copy the pattern), and *what I'd change and why*. A
consolidated, priority-ordered punch list is at the end, followed by a links table.

> **How I reviewed this.** I read the integrated tip (`infra/9-verification` contains the whole
> stack), then attributed each change to its PR via the incremental branch-to-branch diff. Where I
> had a concrete suspicion about runtime behavior (ffmpeg filter validity, concat format
> negotiation) I ran the real binary rather than trusting the fake-ffmpeg tests — and twice that
> changed my conclusion. **Verify, don't assert** is the main meta-lesson here.

---

## 0. Read this first — two things about the *shape* of these PRs

### 0a. They are a stack, but every PR targets `main`

The branches are stacked exactly along the design's dependency DAG
(`1 → 2 → (3,4,5) → 6 → (7,8,9)`): `infra/2` contains all of `infra/1`'s commits, `infra/3` contains
1+2, … `infra/9` contains the whole chain. Each PR adds exactly two commits (one `feat:`, one
`fix: ruff`).

But **all nine set `base = main`**. So GitHub's "Files changed" tab shows the *cumulative* diff, not
the increment:

| PR | INFRA | GitHub diff vs `main` | *Actual* increment (vs previous branch) |
|----|-------|----------------------|------------------------------------------|
| #159 | 1 | +631 / 2 files | +631 / 2 |
| #160 | 2 | +1496 / 7 | **+865 / 5** |
| #161 | 3 | +2403 / 12 | **+908 / 7** |
| #162 | 4 | +2837 / 14 | **+434 / 3** |
| #163 | 5 | +3229 / 15 | **+404 / 3** |
| #164 | 6 | +3567 / 22 | **+338 / 7** |
| #165 | 7 | +4288 / 24 | **+721 / 2** |
| #166 | 8 | +4878 / 28 | **+598 / 10** |
| #167 | 9 | +5382 / 30 | **+504 / 2** |

**Why this matters:** if you open PR #167 you'll be staring at the entire 5,382-line stack, not
INFRA-9's 504 lines. It also forces a strict 1→9 merge order and makes "approve INFRA-6 on its own"
impossible on GitHub.

**Recommendation.** Either (a) retarget each PR's base to its predecessor (`gh pr edit 160
--base infra/1-timeline-core`, etc.) so GitHub shows the true per-PR diff and they can be reviewed
and merged as a proper stack; or (b) accept the linear order and merge 1→9, rebasing the tail each
time. I've given you **compare links for the clean increments** in the final table so you can review
each PR's real delta tonight regardless.

### 0b. The backward-compat keystone holds — and it's tested

The whole design rests on one promise: *shipping this is a no-op for existing audio — no re-encode
storm.* That promise is delivered by the **identity timeline** serializing to an empty digest, which
makes `audio_spec_hash` byte-identical to the old v1 format for un-manipulated episodes. INFRA-2
proves it with a test that re-derives the exact v1 formula and asserts equality
([`tests/test_schema_v2.py`](../tests/test_schema_v2.py)):

```python
def _v1_hash(self, ep, max_kbps=96):
    """Reproduce the exact v1 formula to assert against."""
    spec = {"v": AUDIO_PIPELINE_VERSION, "source": ep.video_url,
            "max_kbps": max_kbps, "chapters": ep.chapters}
    blob = json.dumps(spec, separators=(",", ":"), sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]

def test_plain_episode_matches_v1(self):
    assert audio_spec_hash(_ep(), max_kbps=96) == self._v1_hash(_ep())

def test_episode_with_one_source_still_matches_v1(self):
    ep = _ep(); ep.sources = [_src()]
    assert audio_spec_hash(ep, max_kbps=96) == self._v1_hash(ep)
```

This is the single most important assertion in the whole stack, and it's done right. 👍

---

## INFRA-1 — Timeline/EDL core (`citypods/timeline.py`) · PR #159 · issue #142

**Purpose.** The pure, dependency-free model that every later PR builds on: `SourceMedia`,
`Segment`, `Timeline`, the two time-maps, `remap`, `identity_timeline`, and `timeline_digest`.

**What's good (study this file).**
- It's genuinely pure (`projection.py`-style) — no I/O, trivially testable, 36 tests.
- The module docstring states the *time-basis convention* and the *identity-timeline keystone*
  up front. New contributors learn the mental model before the code.
- The empty-string digest sentinel is the load-bearing trick, and it's isolated in one place:

```python
def timeline_digest(tl: Timeline) -> str:
    if _is_identity(tl):
        return ""                                   # ← keeps audio_spec_hash v1-identical
    blob = json.dumps(dataclasses.asdict(tl), separators=(",", ":"), sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]
```

**Improvements I'd make.**

1. **Boundary inversion is asymmetric across the two maps (low, document it).** Both maps use a
   half-open `[start, end)` interval *except the last segment*, which is closed. But "last" is
   computed differently: `served_to_source` uses the last segment *globally*, while
   `source_to_served` uses the last segment *for that source_id*. At a concat boundary the two maps
   therefore disagree by one instant:

   ```python
   # concat: s0 → served[0,100], s1 → served[100,150]
   served_to_source(tl, 100)        # → ("s1", 0.0)   (seg0 is half-open, 100 ∉ [0,100))
   source_to_served(tl, "s0", 100)  # → 100.0         (s0's only seg is "last" for s0 → closed)
   ```

   They're supposed to be inverses; here they round-trip to different segments at the seam. The
   blast radius is sub-frame (a chapter starting *exactly* on a concat boundary), so it's not urgent
   — but when #122 (concat) lands this can put a chapter one segment off. I'd either pick a single
   global convention or add a docstring note + a golden test pinning the chosen behavior.

2. **`_is_identity` compares floats with `==` (low).** Fine today because `identity_timeline`
   constructs `0.0`/`D` exactly. But a future "trim that happened to remove nothing" planner could
   emit a segment that is *semantically* identity yet fails `served_start == source_start` by a
   float ULP, producing a spurious non-empty digest → a needless re-encode. Consider a tolerance, or
   document that planners must emit exact identity when they no-op.

3. **`remap` leaves `end=None` on a partially-cut item (low).** Correct and documented ("caller may
   clamp"), but every consumer now has to remember to clamp. The encoder's `_ffmetadata` already
   handles `end=None`; just make sure transcript/permalink consumers do too. A `remap(...,
   clamp_to=served_duration)` convenience would centralize it.

*Diff: PR #159 · clean increment `main...infra/1-timeline-core`.*

---

## INFRA-2 — Record schema v2 + spec-hash + `rebuild-audio` CLI · PR #160 · issue #143

**Purpose.** Extend `EpisodeRecord`/`Episode` with the v2 blocks (`sources[]`, `timeline`,
`chapters_basis`, `transcript{}`, `audio.duration_served`, `audio_rebuild`, `audio_encode_time`),
generalize `audio_spec_hash` with the identity guarantee, and ship the surgical-rebuild CLI.

**What's good.**
- The dual-format hash is the cleanest possible way to honor the keystone — old shape when nothing
  is manipulated, new shape only when it must change ([`citypods/records.py`](../citypods/records.py)):

```python
if not tl_digest and not rebuild and not loudness and len(ep.sources) <= 1:
    spec = {"v": AUDIO_PIPELINE_VERSION, "source": ep.video_url,
            "max_kbps": max_kbps, "chapters": ep.chapters}          # v1-identical
else:
    spec = {"v": ..., "max_kbps": ..., "timeline": tl_digest, "loudness": loudness,
            "chapters": ep.chapters, "sources": source_refs, "rebuild": rebuild}
```

- Lazy v1→v2 upgrade is handled in `record_to_episode` / `merge_persisted` by defaulting absent
  blocks to identity/empty — no migration script, consistent with the existing
  `migrate_legacy_manifests` philosophy.
- The CLI's three-blast-radii model (drop-object / nonce / version bump) is faithfully implemented,
  with `--drop-object` and `--reason` mutually exclusive and date bounds parsed as inclusive,
  open-ended, UTC.

**Improvements I'd make.**

4. **`rebuild-audio --reason X` with *no selector* stamps the entire catalog (medium — foot-gun).**
   The loop applies the nonce to every record that passes the (possibly empty) filters. So
   `citypods rebuild-audio --reason oops` — forgetting `--uid`/`--source`/`--encoded-*` — silently
   queues a **full-catalog re-encode**, which is exactly the "blunt instrument" the nonce is
   supposed to be the scalpel *against*. Require at least one selector, or an explicit `--all`, before
   stamping:

   ```python
   has_selector = bool(target_uids or target_source or target_body or after or before)
   if not drop and not has_selector and not args.all:
       print("error: refusing to stamp every episode; pass a selector or --all"); return 1
   ```

5. **The identity hash keys off `ep.video_url`, but the source registry's `ref` is excluded
   (low, latent).** Once `TimelineStage` starts populating `sources` for identity episodes,
   `len(ep.sources) <= 1` keeps you on the v1 branch (good — no churn), but it means the *stable
   `ref`* a `SourceMedia` carries is never part of the identity spec; `video_url` is. For Granicus
   `video_url` *is* the stable archive URL so this is fine today, but document that identity-equivalence
   intentionally pins on `video_url`, not `ref`, so nobody "fixes" it later and triggers a storm.

6. **`feed_content_hash` gained several fields → a one-time global re-render on first deploy
   (benign, just call it out).** Adding `transcript_*`, `chapters_basis`, `audio_duration_served`
   to the payload changes every feed's hash once. That's a *render* (cheap phase), not a re-encode,
   and is the intended "enrichment re-renders" behavior — but flag it in the PR description so it
   isn't mistaken for a regression when every city rebuilds on the first run.

*Diff: PR #160 · clean increment `infra/1-timeline-core...infra/2-schema-v2`.*

---

## INFRA-3 — Timeline-aware encoder (`citypods/media.py`) · PR #161 · issue #144

**Purpose.** Generalize `FfmpegRunner.extract_audio` from "copy/encode one URL" to "render a
`Timeline`": identity stays byte-for-byte the old path; non-identity builds an ffmpeg
`filter_complex` of `atrim`→`concat` (+ optional `loudnorm`, + insert splicing).

**What's good.**
- `build_filter_complex` is a **pure function returning the graph string**, so the whole
  filtergraph is unit-tested with no media (29 tests). This is the right seam.
- The identity path is physically separate (`_render_identity`) and unchanged, so the keystone holds
  at the encoder level too.
- Stall safety carried over correctly: `-rw_timeout`, subprocess `timeout=`, `-protocol_whitelist`.

**A note on two things I suspected and then disproved (the teaching bit).**
I thought the insert path's `acopy` filter was invalid (I expected `anull`) and that `concat`
across mismatched sample rates would fail without an explicit `aresample`. I ran ffmpeg:

```
$ ffmpeg -filters | grep -E '\bacopy\b'
 ... acopy   A->A   Copy the input audio unchanged to the output.      # ← acopy IS real
# concat of 44.1k-stereo + 22k-mono with no aresample:  exit=0          # ← auto-negotiated
```

Both "bugs" were wrong. **Lesson: a fake-ffmpeg string assertion can't tell you whether ffmpeg
*accepts* the graph — verify against the binary.** That said:

**Improvements I'd make.**

7. **Compute `audio_duration_served` from the timeline, not from `ep.duration` (medium — this is
   the one I'd insist on).** Today the encoder records:

   ```python
   ep.audio_duration_served = float(ep.duration) if ep.duration is not None else None
   ```

   For identity that's correct (served == source). But the moment a trim planner (#111) produces a
   non-identity timeline, `ep.duration` is still the *source* duration unless the planner remembers
   to overwrite it — and INFRA-9's `check_timeline_integrity` then **falsely flags**
   `timeline-duration-mismatch` (segment-sum ≠ recorded served duration). Derive it from the EDL so
   the invariant is true by construction and the verification check is meaningful:

   ```python
   if ep.timeline is not None and timeline_digest(ep.timeline) != "":
       ep.audio_duration_served = sum(s.served_end - s.served_start for s in ep.timeline.segments)
   else:
       ep.audio_duration_served = float(ep.duration) if ep.duration is not None else None
   ```

   This also lets the feed switch to served duration cheaply (see INFRA-2 / #11 below) and removes a
   subtle coupling the first feature PR would otherwise have to remember.

8. **Defensive `aformat`/`aresample` before `concat` (low, nice-to-have).** Modern ffmpeg
   auto-negotiates, so it's not a bug — but the design doc already commits to "pin the ffmpeg major
   version in CI for `loudnorm`/`silencedetect` determinism." Inserting an explicit
   `aresample=<rate>,aformat=channel_layouts=mono` per branch before `concat` makes the *output*
   deterministic across ffmpeg versions too, complementing that decision. Cheap insurance for #25/#122.

*Diff: PR #161 · clean increment `infra/2-schema-v2...infra/3-timeline-encoder`.*

---

## INFRA-4 — TimelineStage + TimelinePlanner protocol (`citypods/stages.py`) · PR #162 · issue #145

**Purpose.** A new enrichment stage, ordered **before** `audio`, that composes registered planners
into the persisted EDL. With no planners (today), it's a no-op and `ep.timeline` stays `None`
(= identity).

**What's good.** The planner-composition contract (each planner gets the accumulated timeline, may
return a new one or `None` to pass through) is a clean plug-in seam, and the ordering invariant
`chapters → timeline → remap → audio` is encoded in `default_stages()`/`enrich_stages()` and
covered by an ordering test.

**Improvements I'd make.**

9. **The scaffold doesn't bake in "detect once, persist, never recompute" — and it should, *now*
   (medium).** `review/08` §4/§7 is explicit: "Silence detection runs once in the enrich phase,
   lands on the record … we never re-run detection unless its version changes." But the stage
   re-invokes every planner on every enrich pass:

   ```python
   for ep in _materialize_set(episodes, city.max_episodes):
       if ep.timeline is not None and not self.planners:   # only short-circuits when NO planners
           stats.reused += 1; continue
       current = ep.timeline
       for planner in self.planners:
           result = planner.plan(provider, city, ep, ctx, current)   # ← re-runs every run
           ...
   ```

   Silence detection is *an ffmpeg pass per episode* (the design says so). As written, the first real
   planner (#111) will re-run that pass every run and **isn't gated by `ctx.stop()`** — both of which
   the stack's own "stop convention" (the big docstring at the top of `stages.py`) says expensive,
   restartable work must respect. This is the moment to establish the pattern, because the scaffold
   is the example #111 will copy. I'd add: (a) skip when `ep.timeline` is present *and* was produced
   by the current planner versions (store a `timeline.version`/planner-version stamp), and (b) a
   `ctx.stop()` check immediately before invoking an expensive planner.

*Diff: PR #162 · clean increment `infra/3-timeline-encoder...infra/4-timeline-stage`.*

---

## INFRA-5 — Artifact remap + served/source basis (`citypods/stages.py`) · PR #163 · issue #146

**Purpose.** `RemapStage` converts source-time chapters to served-time via `remap`, sets
`chapters_basis = "served"`, and is a no-op for identity timelines.
`is_timed_transcript()` detects VTT/SRT so untimed transcripts are never mis-aligned.

**What's good.** `_needs_chapter_remap` is a tidy guard (skips identity, skips already-served, skips
empty) and is idempotent across runs. The stage sits correctly *before* `audio` so embedded M4A
markers are in the listener's clock.

**Improvements I'd make.**

10. **Remap is one-way and isn't re-triggered when the EDL changes (medium, latent).** The stage
    does `ep.chapters = remap(ep.timeline, ep.chapters, …)` — it **overwrites** the source-time
    chapters in place and flips `chapters_basis` to `"served"`. After that:
    - `ChaptersStage` skips (`if ep.chapters: reused` — "chapters don't change once set"), and
    - `RemapStage` skips (`chapters_basis == "served"`).

    So if a planner later changes the timeline (e.g. silence algorithm v2 trims differently), the
    stored chapters are **stale and already in the old served clock**, and the original source-time
    values were discarded. `review/08` §10.2 chose "derive source-time on demand from
    `sources`+`timeline`," but the implementation throws the source values away, so you can't
    re-derive correctly after the EDL moves. Fix when planners land: stamp the timeline version the
    chapters were remapped against, and re-remap (from a re-fetch or from retained source chapters)
    when it differs. Inert today (no planners), but worth a `# TODO(when planners land)` so it isn't
    forgotten.

*Diff: PR #163 · clean increment `infra/4-timeline-stage...infra/5-artifact-remap`.*

---

## INFRA-6 — Provider deep-link capability (`citypods/providers/*`) · PR #164 · issue #147

**Purpose.** Add `capabilities: frozenset[str]` and `video_deeplink(ref, t)` to the provider
Protocol; implement for Granicus (`&starttime=`), Swagit (`/play/{id}/{t}`); declare empty caps +
`None` for CivicClerk/CivicPlus. Sampled liveness lives in `contracts.py`.

**What's good.**
- Honest capability declaration — providers that *can't* produce a time-anchored URL say so
  (`frozenset()`, return `None`) instead of faking one, and callers gate on membership.
- Each impl has a safety guard against being handed the wrong `ref` shape:

```python
# Granicus
if "MediaPlayer.php" not in ref:
    return None
return f"{ref}&starttime={int(t_seconds)}"

# Swagit — parses /videos/{id} → /play/{id}/{t}, returns None if the id isn't numeric
```

- I checked the wiring concern (does anything actually pass a `MediaPlayer.php` ref?): Granicus's
  `_episode_links` sets `links["canonical_video"] = link` (the RSS `<link>`, a MediaPlayer page), and
  `contracts.py` uses `canonical_video or video_url` as the ref — so the guard passes in practice. ✔

**Improvements I'd make.**

11. **The liveness check can't actually validate the time anchor (low — set expectations).**
    `contracts.py` does `sess.head(url, allow_redirects=True)` and passes on `status < 400`. For
    Granicus the time is a `&starttime=` query param and for Swagit a path segment; both return 200
    for the base page regardless of whether the seek is honored. So this is a *page-liveness* probe,
    not a *deep-link-correctness* probe. That's a reasonable monitor, but name it honestly in the
    detail string (it currently says `"deeplink"`), and consider a `GET` with a small range for
    Swagit's path form where a wrong id would 404.

12. **Minor redundancy in `contracts.py`:** `newest = max(episodes, …)` is computed at line 49 and
    again inside the deeplink branch (line 81). Harmless, just delete the second.

*Diff: PR #164 · clean increment `infra/5-artifact-remap...infra/6-provider-deeplink`.*

---

## INFRA-7 — Clip service (`citypods/clips.py`) · PR #165 · issue #148

**Purpose.** `extract_clip(ep, a, b, kind)` forward-maps a served range through the EDL into one or
more source cuts, encodes via the INFRA-3 graph, and uploads a content-addressed clip object. Powers
soundbites (#15) and future video compilation. **Not yet wired into any stage** — module + 28 tests
only.

**What's good.** `_clip_timeline` correctly rebases the sub-range to start at served 0 and emits one
`source_cut` per overlapping segment, so a clip spanning a concat boundary becomes multiple cuts —
exactly the multi-source case the design calls for. Content-addressing by
`(uid, range, timeline_version, kind)` gives reuse + cache-bust for free.

**Improvements I'd make.**

13. **`kind="video"` is wired but can't produce video (medium — it's a trap).** The video path
    builds a `.mp4` name and `video/mp4` content-type, then calls `ffmpeg.extract_audio(...)` —
    which hard-codes `-vn` (drops video). So `extract_clip(kind="video")` uploads an mp4 container
    with no video stream. It's documented as "future," but it *looks* functional and a caller could
    wire it up and ship silent black clips. I'd either `raise NotImplementedError` for `kind="video"`
    until INFRA-3 grows a real video render path, or add a `# pragma: not wired` and a test that
    asserts the guard.

14. **Clip key uses raw `float` string interpolation (low).** `f"{uid}|{served_start}|{served_end}
    |..."` makes `600` and `600.0` (and `600.0000001`) distinct keys, so a soundbite re-requested
    with a slightly different float misses the cache and re-encodes. Normalize to integer
    milliseconds before hashing.

15. **Single-resolver for multi-source clips (low, acknowledged — fixed in PR #182).** `resolve_media_url(ep)` was called
    once and mapped to *every* `source_id`, so a true concat clip would point all cuts at one URL.
    Fixed in PR #182 (`citypods/clips.py`): multi-source episodes now use `ep.sources[*].ref` directly,
    so each source in a clip spanning a concat boundary fetches from the correct file. #15 can now
    ship for concat feeds.

*Diff: PR #165 · clean increment `infra/6-provider-deeplink...infra/7-clip-service`.*

---

## INFRA-8 — Transcript artifact storage (`citypods/stages.py`, `feeds.py`) · PR #166 · issue #149

**Purpose.** `TranscriptStage` (reuse-first provider transcript slot + stubbed ASR slot) stores
transcripts as content-addressed objects (`transcripts/<src>/<uid>-<spec>.<fmt>`), records the
`transcript{}` block, and `feeds.py` emits `<podcast:transcript>` **only when synced**. This stage
**is** in `default_stages()` and `enrich_stages()` — i.e. it runs in production.

**What's good.** The synced/basis logic is careful: identity timeline + timed content →
`served`/`synced=True`; non-identity timed → kept `source:s0`/`synced=False` until a real VTT remap
lands (so nothing is ever mis-aligned); untimed → notes-only. The feed gate matches:

```jinja
{% if item.transcript_url %}
<podcast:transcript url="{{ item.transcript_url }}" type="{{ item.transcript_mime }}"/>
```

**Improvements I'd make.**

16. **`TranscriptStage` gates the *reuse* path behind `ctx.stop()` — violating the stack's own stop
    convention (medium).** The big docstring at the top of `stages.py` rule #1 says: *"Never gate
    cheap/idempotent bookkeeping (reuse checks, attaching an already-known URL) … that must always
    finish so the run leaves consistent, deployable state."* `AudioStage`/`materialize_audio` follow
    that (reuse + credit run before the `stop()` gate). But `TranscriptStage` does the opposite:

    ```python
    for ep in _materialize_set(...):
        if ctx.stop is not None and ctx.stop():     # ← gates EVERYTHING, including reuse
            stats.skipped += 1; continue
        if ep.transcript_key and _present(ep.transcript_key):   # reuse should run unconditionally
            ep.transcript_hosted_url = ctx.storage.public_url(ep.transcript_key)
            stats.reused += 1; continue
    ```

    Consequence: once the wall-clock window closes, a yielded run won't re-attach hosted URLs for
    transcripts that are *already stored*, and counts them as skipped — so the rendered feed can drop
    `<podcast:transcript>` for episodes whose transcript is done. Move the `stop()` check to *after*
    the reuse short-circuit, mirroring `materialize_audio`.

17. **Transcript spec hash keys on a possibly-tokenized URL (low).** `_transcript_spec_hash(source_url)`
    hashes the raw provider URL, whereas `audio_spec_hash` deliberately *excludes* expiring URLs and
    keys on a stable `ref`. Mostly masked because the `transcript_key` reuse short-circuit fires
    before re-fetch — but on a cold cache or post-GC re-fetch, a rotated token yields a different key
    for identical content (orphan churn). Prefer hashing the stable provider ref (or the fetched
    bytes) like the audio path does.

18. **MIME map duplicated (low).** `_TRANSCRIPT_MIME` in `stages.py` and an inline
    `{"vtt": ..., "srt": ...}` in `feeds.py`. Import the one in `stages.py` (or move it to a shared
    spot) so they can't drift.

*Diff: PR #166 · clean increment `infra/7-clip-service...infra/8-transcript-storage`.*

---

## INFRA-9 — Timeline/clip verification + contracts (`citypods/audit.py`) · PR #167 · issue #150

**Purpose.** `check_timeline_integrity` validates non-identity EDLs offline (ordering/overlap,
coverage-start, duration match, source-span bounds, served-chapter range), wired into `audit_city`;
deep-link liveness is sampled in `contracts.py` (INFRA-6).

**What's good.** Identity episodes are skipped (`timeline_digest == ""`), a `_FRAME_TOLERANCE = 0.1`
is applied consistently, errors are precise (`uid`, segment index, the two numbers and their delta),
and severity is right (structural = ERROR, chapter-out-of-range = WARN). 25 tests including synthetic
bad EDLs.

**Improvements I'd make.**

19. **It checks overlap but not internal *gaps*, and never asserts the end (medium).** The loop
    flags `served_start < prev_end` (overlap) but not `served_start > prev_end` (a hole in the served
    clock — which should be impossible, since served time is contiguous). Full coverage is only
    inferred via the sum-vs-duration check, which can be fooled (a gap plus an equal overrun sums
    correctly). Add the symmetric contiguity assertion and an explicit end check:

    ```python
    if s.served_start > prev_end + _FRAME_TOLERANCE:
        findings.append(Finding(slug, "timeline-gap", ERROR,
            f"{uid}: gap before segment {i}: {prev_end:.3f}s → {s.served_start:.3f}s"))
    ...
    if served_dur is not None and abs(segs[-1].served_end - served_dur) > _FRAME_TOLERANCE:
        findings.append(Finding(slug, "timeline-short-coverage", ERROR, ...))
    ```

20. **The duration check is only as good as `audio_duration_served` (see #7).** Right now that field
    is set from `ep.duration`, so the check is sound for identity and *will start mis-firing for
    trims unless #7 is done first.* These two PRs are coupled: fixing the encoder to derive served
    duration from the EDL is what makes INFRA-9's headline check actually correct. Worth a comment in
    INFRA-9 pointing at that dependency.

*Diff: PR #167 · clean increment `infra/8-transcript-storage...infra/9-verification`.*

---

## Cross-cutting finding (highest priority) — orphan GC will delete live transcripts

This spans INFRA-2/7/8 and is the one I'd block on before anyone runs the GC in anger.

`referenced_audio_keys()` (INFRA-2) collects **only** `audio.key`:

```python
for rec in (data.get("episodes") or {}).values():
    key = (rec.get("audio") or {}).get("key")     # ← audio only; transcript.key / clip keys ignored
    if key: keys.add(key)
```

`scripts/gc_audio.py` defaults to **`--prefix ""`** (every object) and deletes anything not in that
set and not under `state/`, older than 7 days:

```python
for key, last_modified in storage.list_objects(args.prefix):   # prefix defaults to ""
    if key in referenced or key.startswith(f"{STATE_PREFIX}/"):
        continue
    ... storage.delete(key)
```

INFRA-8 uploads real objects to `transcripts/<src>/…` and **is wired into production**
(`default_stages`/`enrich_stages`). So the first time the maintainer runs the documented
`python scripts/gc_audio.py --apply` (its whole purpose is to reclaim orphaned *audio* after a spec
bump), it will also delete **every hosted transcript** older than the age floor — and later, every
soundbite clip (`clips/…`, INFRA-7) once #15 ships. The status page even advertises the command, so
this *will* be run.

It's latent only because the GC is manual + dry-run-default; functionally it's a data-loss bug.
**Fix:** make the live-set include all managed object kinds and/or scope the GC.

```python
def referenced_keys(state_dir: Path) -> set[str]:
    keys: set[str] = set()
    for path in Path(state_dir).glob("sources/*/episodes.json"):
        for rec in (json.loads(path.read_text()).get("episodes") or {}).values():
            for k in ((rec.get("audio") or {}).get("key"),
                      (rec.get("transcript") or {}).get("key")):
                if k: keys.add(k)
    return keys
```

Plus: have the GC default to the audio prefix (or iterate a known set of managed prefixes and only
reap within each), and treat `clips/` as derivable/ephemeral with its own policy. Add a regression
test: "a stored transcript key is in the referenced set / survives a GC sweep."

---

## Priority-ordered punch list

**Block before relying on the GC**
- **A. Orphan GC deletes transcripts (and later clips).** Extend `referenced_audio_keys` → all
  managed keys + scope the GC prefix. *(cross-cutting; INFRA-2/7/8)*

**Should-fix before the first feature PR (#111) builds on this foundation**
- **B. Encoder should derive `audio_duration_served` from the EDL** (#7) — unblocks correct feed
  duration and makes INFRA-9's duration check valid (#20).
- **C. `TimelineStage` must skip already-planned episodes by version + gate expensive planners on
  `ctx.stop()`** (#9) — establish the convention in the scaffold, not in #111.
- **D. `TranscriptStage` must not gate its reuse path on `stop()`** (#16).
- **E. `rebuild-audio` must refuse a no-selector stamp** (#4).

**Good hygiene (do with the above)**
- F. Re-remap chapters when the EDL version changes; don't discard source-time chapters (#10).
- G. INFRA-9: add gap + end-coverage checks (#19).
- H. `extract_clip(kind="video")` should `raise NotImplementedError` until a real video path exists
  (#13); normalize clip keys to ms (#14).
- I. Transcript spec-hash off a stable ref, not a tokenized URL (#17); de-dup the MIME map (#18).

**Low / documentation**
- J. Document the map boundary convention + add a concat-seam test (#1); float-`==` identity note (#2).
- K. Honest naming for the deep-link *page*-liveness probe (#11); drop the duplicate `max()` (#12).
- L. Note the intentional `video_url`-not-`ref` identity pin (#5) and the one-time re-render (#6).

**Process**
- M. Retarget each PR's base to its predecessor (or merge strictly 1→9) so GitHub shows true
  per-PR diffs (§0a).

None of A–M is a correctness bug *for the foundation as it stands today* (identity episodes only) —
they are the seams that will crack the first time a real planner produces a non-identity timeline.
That's the right time to fix them: now, while the scaffold is the worked example the feature PRs
will copy.

---

## Overall assessment

This is a strong, disciplined stack. The decomposition mirrors the design DAG one-to-one, every PR
is small and independently testable, the docstrings teach the model, and the backward-compat
keystone is real and proven. The recurring theme in my notes isn't "this is wrong" — it's "this is a
no-op scaffold today, and the *next* PR will inherit its conventions, so encode the right behavior
(persist-once, stop-gating, served-duration-from-EDL, GC-awareness) here rather than discovering it
under #111." Fix item **A** before touching the GC; fold **B–E** into the foundation; the rest are
cheap follow-ups.

---

## Links — review each PR's *clean* increment

Repo: `BashfulBits/city-meeting-podcasts`. PR links show the cumulative stack (§0a); the **compare**
links show only that PR's real delta.

| INFRA | Issue | PR (cumulative) | Clean increment (compare) |
|------|-------|-----------------|---------------------------|
| 1 | [#142](https://github.com/BashfulBits/city-meeting-podcasts/issues/142) | [#159](https://github.com/BashfulBits/city-meeting-podcasts/pull/159) | [`main...infra/1-timeline-core`](https://github.com/BashfulBits/city-meeting-podcasts/compare/main...infra/1-timeline-core) |
| 2 | [#143](https://github.com/BashfulBits/city-meeting-podcasts/issues/143) | [#160](https://github.com/BashfulBits/city-meeting-podcasts/pull/160) | [`infra/1-timeline-core...infra/2-schema-v2`](https://github.com/BashfulBits/city-meeting-podcasts/compare/infra/1-timeline-core...infra/2-schema-v2) |
| 3 | [#144](https://github.com/BashfulBits/city-meeting-podcasts/issues/144) | [#161](https://github.com/BashfulBits/city-meeting-podcasts/pull/161) | [`infra/2-schema-v2...infra/3-timeline-encoder`](https://github.com/BashfulBits/city-meeting-podcasts/compare/infra/2-schema-v2...infra/3-timeline-encoder) |
| 4 | [#145](https://github.com/BashfulBits/city-meeting-podcasts/issues/145) | [#162](https://github.com/BashfulBits/city-meeting-podcasts/pull/162) | [`infra/3-timeline-encoder...infra/4-timeline-stage`](https://github.com/BashfulBits/city-meeting-podcasts/compare/infra/3-timeline-encoder...infra/4-timeline-stage) |
| 5 | [#146](https://github.com/BashfulBits/city-meeting-podcasts/issues/146) | [#163](https://github.com/BashfulBits/city-meeting-podcasts/pull/163) | [`infra/4-timeline-stage...infra/5-artifact-remap`](https://github.com/BashfulBits/city-meeting-podcasts/compare/infra/4-timeline-stage...infra/5-artifact-remap) |
| 6 | [#147](https://github.com/BashfulBits/city-meeting-podcasts/issues/147) | [#164](https://github.com/BashfulBits/city-meeting-podcasts/pull/164) | [`infra/5-artifact-remap...infra/6-provider-deeplink`](https://github.com/BashfulBits/city-meeting-podcasts/compare/infra/5-artifact-remap...infra/6-provider-deeplink) |
| 7 | [#148](https://github.com/BashfulBits/city-meeting-podcasts/issues/148) | [#165](https://github.com/BashfulBits/city-meeting-podcasts/pull/165) | [`infra/6-provider-deeplink...infra/7-clip-service`](https://github.com/BashfulBits/city-meeting-podcasts/compare/infra/6-provider-deeplink...infra/7-clip-service) |
| 8 | [#149](https://github.com/BashfulBits/city-meeting-podcasts/issues/149) | [#166](https://github.com/BashfulBits/city-meeting-podcasts/pull/166) | [`infra/7-clip-service...infra/8-transcript-storage`](https://github.com/BashfulBits/city-meeting-podcasts/compare/infra/7-clip-service...infra/8-transcript-storage) |
| 9 | [#150](https://github.com/BashfulBits/city-meeting-podcasts/issues/150) | [#167](https://github.com/BashfulBits/city-meeting-podcasts/pull/167) | [`infra/8-transcript-storage...infra/9-verification`](https://github.com/BashfulBits/city-meeting-podcasts/compare/infra/8-transcript-storage...infra/9-verification) |

*Design source: [`review/08-timeline-and-content-transforms.md`](08-timeline-and-content-transforms.md) · epic [#141](https://github.com/BashfulBits/city-meeting-podcasts/issues/141).*
d