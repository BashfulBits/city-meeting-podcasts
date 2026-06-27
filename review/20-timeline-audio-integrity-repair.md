# Timeline/audio integrity repair plan

**Status:** L3 design, PR1 merged 2026-06-27; PR2-PR5 implemented in follow-up branch.

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
| `rendered-duration-mismatch` | stream/decoded audio duration differs from EDL | error | re-materialize; maybe re-transcribe |
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
    "repair": ["audio-rematerialize", "transcript-regenerate"]
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
duration ambiguity.

Backfill story: none. The scheduled audit remains non-mutating; diagnostics are an artifact used to
gate PR6.

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
when it covers the known full source duration. `SwagitConcatPlanner` now asks ffprobe for stream
`duration_ts * time_base` first and stores `duration_basis="stream-sample"` on segment sources.

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
mode. Gate on the uploaded `timeline-audio-integrity` artifact showing stable classifications, then turn
on `--persist-timeline-integrity` for the scheduled audit with an emergency config switch to disable
automatic repair stamping.

Backfill story: only confirmed affected records enter the repair queues; all work drains gradually under
existing stop budgets.

## Acceptance

- The audit distinguishes container-only drift from decoded/stream-clock drift.
- Multi-source concat diagnostics can identify whether segment-duration accumulation explains a mismatch.
- GH#495 tail-only trims no longer collapse to identity.
- Repair is targeted: no silent full-catalog ASR or audio invalidation.
- Feed-health issues close only after a successful post-repair audit, not merely after a field is stamped.
