# Timeline/audio integrity repair plan

**Status:** L3 design, PR1 merged 2026-06-27; PR2-PR5 implemented in follow-up branch.

## Follow-up: single-file silence root cause + duration-model hardening (GH#702)

Post-PR666 audit comparison proved the remaining `rendered-duration-mismatch` survivors are a single
class: `SilencePlanner` plans single-file silence EDLs against the source **container** duration
(`_parse_ffmpeg_duration`), while the renderer renders the source **audio stream**. Where container >
stream (HLS manifest overstatement, or a direct MP4 whose video outlasts its audio), the EDL's tail
span over-claims and the rendered file is short by ≈(container − stream). `SwagitConcatPlanner` already
plans on the stream-sample clock (`duration_basis="stream-sample"`); the silence planner was never given
the same treatment. PR636's `timeline-replan` is idempotent for these rows (same container duration →
identical digest) so it cannot fix them.

The fix series below is a **separate, later series** from the `PR1 — sample-clock probe … PR6 —
auto-repair enablement` sequence documented in "## PR sequence" further down (that earlier series built
the diagnostic/repair infrastructure and is already implemented). To avoid ambiguity these are labelled
**GH#702 PR1…PR6**. The served=probe inversion was split into its own PR after GH#702 PR2 because the
`timeline-duration-mismatch` / `timeline-short-coverage` contract checks currently assume
`audio_duration_served == EDL` "by construction" (audit.py §3), so repointing them is an audit-semantics
change that warrants separate review:

- **GH#702 PR1 (#703, merged):** consolidate the EDL/cue clock into one `timeline.edl_duration` primitive
  that `media._served_duration`, `stages._edited_timeline_served_duration`, and `audit._timeline_duration`
  all delegate to. No behavior change; establishes the single derivation site so the three canonical
  duration facts (source / served-hosted / EDL-cue) cannot drift apart.
- **GH#702 PR2 (#704):** `SilencePlanner` measures the source's stream-sample duration
  (`_probe_stream_sample_duration`, mirroring `concat.py`) and uses it for trailing-silence detection and
  the final keep-span, recording `duration_basis="stream-sample"`. Root-cause fix; re-planned episodes get
  a corrected EDL the renderer matches.
  - **PR2 follow-up (decoded-end fallback):** the stream-sample ffprobe returns `None` for precisely the
    over-claiming sources — HLS manifests and fragmented MP4 expose no stream-level
    `duration_ts`/`time_base`/`duration` — so the planner fell straight back to the **container** header
    and re-planned those episodes onto the *same* over-claiming EDL (identical `timeline_digest`, identical
    short rendered file). This was the reason the manual repair cohort did not converge even with
    `timeline-replan` flags set: re-encode (the `audio-rematerialize` flag) fired and changed
    `audio_spec_hash`, but the EDL never changed. Fixed by reusing the decode the `silencedetect` pass
    already performs: `detect_silences` now also returns the decoded audio-stream end (its final `time=`
    stats timestamp), and the planner uses it as a `duration_basis="decoded"` tier between `stream-sample`
    and `container`. A clean post-repair audit now shows the survivors with changed `timeline_digest` and
    `source_duration_bases=["decoded"]` (or `["stream-sample"]` where exposed), not `["container"]`.
  - **PR2 follow-up correction (PTS-gap fix — the decoded-end fallback above did not converge in
    production).** A before/after production audit of the repair cohort showed **0/56 survivors fixed**
    despite genuine re-encodes and, for 27/56, a genuinely re-planned EDL — `stream_delta` was
    statistically unchanged for every row. Root cause: ffmpeg's `time=` progress field is a
    **presentation-timestamp (PTS) clock, not a decoded-PCM-sample-count clock** (the original duration-
    clocks table below always specified `decoded_duration` as "PCM sample count after decode" — the PR2
    follow-up's `time=` parse was a clock-type mismatch against that spec, not a design error). Without
    correction, `time=` carries forward any PTS discontinuity in the source (a stream splice, an
    ad-insertion boundary, a dropped HLS segment) as if the gap were real elapsed audio — so it
    overstates by exactly the gap size, landing on the *same* value as the container `Duration` header
    (also PTS-based). Confirmed for the three largest survivors: `decoded_duration` was bit-identical to
    the prior `container_duration` (one of the three, Fort Worth, is `media_kind="direct"` — not HLS —
    ruling out HLS-segment-loss as the mechanism). The render path
    (`_build_streaming_single_source_filter`, `media.py`) is intended to operate on the same contiguous
    sample-index clock, so any PTS gap must be compacted before comparing source spans. **Fix:**
    `detect_silences` now prepends the identical `asetpts=N/SR/TB` reset ahead of `silencedetect` in its
    own filter chain, so `time=` (and `silencedetect`'s own reported silence boundaries) are measured on
    the same gap-compacted clock the render will actually produce. This is a per-frame timestamp rewrite
    at the native sample rate — no resampling, no second decode pass, a no-op on a source with no
    discontinuity — reproduced directly: a constructed 10s two-segment file with a deliberate 2s forward
    PTS jump reports `time=12.0x` unfixed (matching its container header) and `time=10.06s` fixed,
    against a measured render output of `10.069s` for the same file. See
    `citypods/silence.py::_parse_ffmpeg_decoded_end`'s docstring for the full mechanism.
  - **PR2 follow-up correction (renderer pre-select PTS fix — the decode-pass PTS fix only partially
    converged).** The next run 5 → run 6 production audit of the same repair cohort showed the repair
    lanes were active but still wrong: 9/63 selected UIDs fixed, 54/63 remained
    `rendered-duration-mismatch`, `audio_key` changed for 61, and `timeline_digest` changed for 62. All
    old-cohort survivors were now `source_duration_bases=["decoded"]`, proving the planner moved off the
    container clock. Root cause: `_build_streaming_single_source_filter` had `asetpts=N/SR/TB` only after
    `aselect`; the final output was left-packed, but the selector still compared compacted EDL
    boundaries to raw source PTS. With a 2s source PTS gap, a synthetic 10s EDL rendered as ~8.056s. The
    streaming filter now rewrites source PTS to the contiguous decoded-sample clock before boundary
    framing / `aselect`, and keeps the post-select `asetpts` that packs retained samples onto served
    time. The synthetic PTS-gap regression now renders the same 10s EDL as 10.0s.
- **GH#702 PR3 (#705):** make the probed hosted-stream duration authoritative for `audio_duration_served`
  — `_backfill_served_duration` is now fill-when-missing and `_refresh_served_duration_from_audio` is
  probe-first for every timeline, so the measured hosted-file duration is no longer overwritten with the
  EDL sum. RSS `<itunes:duration>` for audio feeds advertises the served clock (`enclosure_duration`). The
  cheap stored-field `timeline-duration-mismatch` / `timeline-short-coverage` checks defer to the precise
  live `rendered-duration-mismatch` probe when one is supplied (no double-filing). Lands after the planner
  fix so corrected durations — not the short broken ones — are what gets published.
- **GH#702 PR4 (#707):** decouple `_build_streaming_single_source_filter` from `PODCAST_SPEECH_PROFILE`
  (attempt it for every single-source timeline; append loudnorm to its output on the legacy path) and
  guard the generic graph: a single-source many-cut timeline reaching `build_filter_complex` now raises
  `StreamingFilterBypassedError`. `build_filter_complex` is retained only for multi-source concat
  assembly/fallback and inserts.
- **GH#702 PR5 (#708):** audit threshold de-noise — a distinct `_RENDERED_DURATION_TOLERANCE` (0.5s)
  for the rendered/container duration classification, separate from the 0.1s `_FRAME_TOLERANCE` used by
  structural checks, so the AAC-priming/sample-rounding band stops producing sub-finding
  `rendered-duration-mismatch` artifact noise. Plus this remediation runbook.
- **GH#702 PR6 (gated, build-but-do-not-merge):** `silence:3` catalog version bump for the permanent
  guarantee.

### GH#702 remediation runbook (operator steps)

The code fixes above correct EDLs and durations *going forward*. Remediating the already-broken
single-file cohort (~26 unique uids over 1s: Denton ×20, Arlington ×3, Addison, Fort Worth; plus the
0.5–1s band) is an operator action, because it re-encodes hosted audio and regenerates transcripts in
production and drains over multiple runs under the stop budget:

1. **Confirm the replan flags are still set.** The before-PR666 manual cohort stamped `timeline-replan`
   on these episodes; a clean post-repair audit clears them, so a still-broken episode should still
   carry the flag. If not, re-dispatch the feed-health workflow with `timeline_repair`,
   `timeline_repair_min_delta` (use `0.5` to also catch the small band, or `1.0` for findings only) and
   a `timeline_repair_cohort` label to re-stamp them.
2. **Let the lanes drain.** With PR2 merged, `TimelineStage` re-plans flagged episodes on the
   stream-sample clock → new EDL digest → `AudioStage` re-encodes → ASR regenerates. This is bounded by
   the existing wall-clock stop budget.
3. **Verify with the artifact.** Compare the before/after `audit-timeline-integrity` artifacts with
   `scripts/compare_timeline_diagnostics.py --cohort <label>`. Gate: selected rows return with
   `stream_delta` within tolerance, `fixed` rises, and no `worsened` / unexpected `missing-after`.
   Diagnostic tell: a survivor with `audio_key_changed` but **unchanged** `timeline_digest` (and
   `source_duration_bases=["container"]`) means the audio lane re-encoded against an EDL the timeline
   lane never actually re-planned — the EDL must change (`timeline_digest_changed`, basis `decoded`/
   `stream-sample`) for the rendered file to match. If digests stay put, the re-plan is not engaging
   (lanes not drained, flags not set, or — pre-fix — the planner falling back to the container clock).
   Note that **post-fix** the planner still lands on `["container"]` when the decoded-end parse itself
   fails (no parseable `time=` in the silencedetect stats), so a lone post-fix container-basis survivor
   is not necessarily a stale pre-fix cohort — verify decoded-end parsing for that source before
   treating it as one.
4. **Stragglers handled separately (not via timeline-replan):**
   - **Dallas** (`dallas-tx-city-council`): `audio_key` never changed and `audio_duration_served` is
     null while the hosted stream looks like the untrimmed source — the audio lane never
     re-materialized it. Investigate the materialize backoff/queue for that uid; this is an audio-lane
     issue, not a planner/renderer one.
   - **Pflugerville `missing-audio-key`** (`duration-probe-inconclusive`): the audio object is absent
     (never materialized or GC'd). Confirm whether the episode should re-materialize or is withheld.

## Problem

Feed-health timeline findings currently compare the persisted EDL duration against
`audio_duration_served`. That field has historically mixed two meanings:

- the semantic served duration derived from `timeline.segments`;
- the hosted container duration reported by ffprobe after materialization or ASR probing.

For edited timelines, those clocks can diverge. Small container-only drift is not enough to prove
chapters or VTT cues are wrong, but decoded audio drift from the EDL is a real cue-integrity problem.
Multi-source concat can also accumulate per-segment duration errors if `SourceMedia.duration` was
planned from container metadata rather than the same sample clock used by rendering.

## Duration clocks

Every integrity check must distinguish:

| Clock | Meaning | Use |
|---|---|---|
| `timeline_duration` | Sum of served EDL segment spans | Canonical cue/chapter clock |
| `stream_sample_duration` | First audio stream endpoint from `duration_ts * time_base` | Cheap sample-clock endpoint probe |
| `container_duration` | `format.duration` | Diagnostic only |
| `decoded_duration` | PCM sample count after decode | Expensive fallback / proof when stream timing is absent or suspicious |

The daily audit should start with `stream_sample_duration` rather than a full decode. For normal M4A
and Matroska outputs ffprobe can expose the stream endpoint directly from stream timestamps. A
"last frame only" probe is not generally sufficient: raw codec frame counts can include encoder
padding, while container edit-list semantics can shorten the playable stream. If stream timing is
missing or contradictory, fall back to bounded PCM decode for the small suspicious set only.

## Finding classes

| Finding | Condition | Severity | Repair |
|---|---|---|---|
| `container-duration-drift` | `container_duration` differs from EDL, but stream/decoded duration matches | warn/debug | none |
| `rendered-duration-mismatch` | stream/decoded audio duration differs from EDL | error | re-plan timeline, then re-materialize/re-transcribe |
| `timeline-source-duration-mismatch` | concat segment decoded durations differ from persisted `SourceMedia.duration` enough to explain drift | error | re-plan timeline, then re-materialize/re-transcribe |
| `timeline-identity-misclassified` | timeline digest is empty for a source span that is not full-source identity | error | fix GH#495 identity detection, then re-plan/re-materialize/re-transcribe |
| `duration-probe-inconclusive` | stream timing absent and bounded decode unavailable/fails | warn | no automatic repair |

## Persisted repair state

Add a small episode-record block once PR3 lands:

```json
"integrity": {
  "timeline_audio": {
    "status": "rendered-duration-mismatch",
    "checked_at": "2026-06-27T00:00:00Z",
    "timeline_duration": 8010.788,
    "stream_sample_duration": 8014.859,
    "container_duration": 8014.859,
    "repair": ["audio-rematerialize", "timeline-replan", "transcript-regenerate"]
  }
}
```

This block is owned by the audit/repair reconciler. It must be preserved by audio/transcript lanes in
the same way foreign artifact blocks are preserved today.

## PR sequence

### PR1 — sample-clock probe + design documentation

Add a reusable ffprobe helper that reports both `format.duration` and first-audio-stream
`duration_ts * time_base`. Do not change audit behavior yet. Document this series in `review/11`,
`ROADMAP`, and this breakout.

Backfill story: none. This is helper-only and does not change records or artifacts.

### PR2 — read-only audit diagnostics

Extend `check_timeline_integrity` to include cheap duration diagnostics for hosted edited timelines:
`timeline_duration`, `stream_sample_duration`, `container_duration`, and clock deltas. Keep existing
issue keys until classification is proven. Add a manual/debug flag for segment-level probes.

Implemented: `scripts/audit_feeds.py --timeline-diagnostics <path>` writes JSONL diagnostics and the
scheduled feed-health workflow uploads the artifact as `timeline-audio-integrity`. The probe downloads
only hosted edited-timeline objects and uses the stream sample-clock helper before classifying. The
artifact now records `probe_error` for inconclusive rows (`missing-audio-key`, storage/download
failures, `ffprobe-error`, `no-duration-metadata`) so PR6 can distinguish missing evidence from true
duration ambiguity. Scheduled feed-health keeps those inconclusive rows in the artifact but does not
file per-slug issues for them, and the workflow installs `ffmpeg`/`ffprobe` before probing. The
workflow can also be manually dispatched from `main` with `timeline_repair`, a
`timeline_repair_min_delta`, and a required `timeline_repair_cohort` label to persist only a named
cohort of over-threshold repair flags while still uploading the full diagnostic artifact.
`scripts/compare_timeline_diagnostics.py` compares the selected before cohort against a later artifact
and reports fixed, still-mismatched, missing-after, worsened, audio-key-changed,
timeline-digest-changed, and timeline-version-changed counts for the PR6 gate. The artifact also carries planner-input telemetry
per row: `source_mode` (`single-file` / `multi-part`), `source_count`, `source_media[]` with
per-source durations and duration bases, `source_duration_total`, `segment_source_span_total`, and
the persisted `timeline_version` / `timeline_digest`. Silence-planned single-file episodes now
persist that source duration and basis too, so post-repair analysis can tell whether a mismatch came
from a measured file duration or a provider fallback without reconstructing planner state by hand.

Backfill story: bounded manual cohort repair only. The scheduled audit remains non-mutating; diagnostics
and the before/after comparison artifact are used to gate PR6.

### PR3 — persisted integrity/repair flags

Persist `integrity.timeline_audio` for confirmed mismatches. Teach issue reconciliation to show repair
state and avoid closing while a repair flag remains unresolved. Add status/admin counts for repair
queues.

Implemented: records round-trip an audit-owned `integrity` block, scoped lane pushes preserve it, and
`/admin/status` exposes `backlog.timeline_audio_repair` counts by status/action. The audit can stamp
confirmed repair blocks with `--persist-timeline-integrity`, but the scheduled workflow does not enable
that flag yet.

Backfill story: only manually persisted confirmed mismatches get a metadata block. No global artifact
invalidation.

### PR4 — planner duration basis + GH#495 identity fix

Make source-duration-aware identity detection so true full-source identity remains digest-empty, while
tail-only trims and other real edits get a non-empty digest. For concat planning, store segment duration
basis (`decoded:<rate>` where available) and prefer stream/decoded sample-clock durations over container
duration.

Implemented: episode-level digest callers pass `ep.sources`, so a span can collapse to identity only
when it covers the known full source duration. The render path now receives that same source registry,
so tail-only trims cannot fall back to the legacy identity/copy branch while hash-producing call sites
treat them as real edits. `SwagitConcatPlanner` now asks ffprobe for stream `duration_ts * time_base`
first and stores `duration_basis="stream-sample"` on segment sources.

Backfill story: targeted. Existing artifacts are not globally invalidated; only records with repair
actions or later planner-version changes are reworked.

### PR5 — repair consumers

Teach stages to consume repair flags:

- `timeline-replan`: `TimelineStage` ignores the matching planner signature for that episode.
- `audio-rematerialize`: stamp a deterministic `audio_rebuild` nonce so `audio_spec_hash` changes.
- `transcript-regenerate`: add a targeted transcript rebuild input so ASR/provider-align artifacts can
  invalidate without bumping `ASR_PIPELINE_VERSION`.

Clear repair flags only after the post-repair audit sees stream/decoded duration match the EDL.

Implemented: `TimelineStage` ignores a matching signature only for episodes without
`timeline-replan`; `AudioStage` stamps a deterministic `audio_rebuild` nonce for
`audio-rematerialize`; ASR and provider-align recipes include a persistent repair token for
`transcript-regenerate`. Resolved repair tokens remain in recipe history so artifacts do not snap back
to pre-repair keys after the active repair queue clears.

Backfill story: bounded by the existing timeline/audio/ASR lanes and backlog policy. No global
pipeline-version bump.

### PR6 — auto-repair enablement

Enable automatic flagging for confirmed decoded/stream mismatches after PR2-PR5 have run in diagnostic
mode and the manual over-threshold cohort has repaired cleanly. Gate on the uploaded
`timeline-audio-integrity` artifact and `scripts/compare_timeline_diagnostics.py` showing that selected
cohort rows return with stream deltas within tolerance and no unexpected missing-after/worsened pattern,
then turn on `--persist-timeline-integrity` for the scheduled audit with an emergency config switch to
disable automatic repair stamping.

Backfill story: only confirmed affected records enter the repair queues; all work drains gradually under
existing stop budgets.

## Follow-up: header-only (range-read) duration probe, with continuous full-download reconciliation

The diagnostics probe (`_probe_timeline_audio` in `citypods/audit.py`, feeding
`_classify_timeline_audio_duration`) used to download every non-identity-timeline episode's whole
hosted `.m4a` just to read `format.duration`/`stream.duration_ts`/`time_base` — fields that live
entirely in the MP4 `moov` box. Every hosted episode is written `-movflags +faststart` (moov
before mdat), so a small range read of just `ftyp`+`moov` (`StorageBackend.get_range`,
`media._probe_audio_duration_header`) yields bit-identical values to a full download; `mdat` was
never read for these fields either way. This cut the diagnostics-enabled audit's data transfer by
roughly two orders of magnitude (moov is typically well under 1% of a multi-hour episode's file
size).

Given this project's history of subtle duration-measurement bugs in this exact area (the GH#702
decoded-vs-container/PTS-gap saga above), the header-only probe is not trusted silently: any
episode it flags as non-"ok" is automatically re-measured with a full-download probe
(`probe_audio_full` in `check_timeline_integrity`). The full read is authoritative for that
episode's actual finding/repair decision, and a new `timeline-audio-probe-divergence` finding
(ERROR) fires if the two methods disagree beyond float noise — a live, ongoing check of the
moov-only assumption against exactly the "problem" files this project periodically hits, not just
a one-time validation. `tests/test_media.py` also pins the assumption directly against real
ffmpeg/ffprobe output (short clip and a multi-round-trip long clip).

## Acceptance

- The audit distinguishes container-only drift from decoded/stream-clock drift.
- Multi-source concat diagnostics can identify whether segment-duration accumulation explains a mismatch.
- GH#495 tail-only trims no longer collapse to identity.
- Repair is targeted: no silent full-catalog ASR or audio invalidation.
- Feed-health issues close only after a successful post-repair audit, not merely after a field is stamped.
