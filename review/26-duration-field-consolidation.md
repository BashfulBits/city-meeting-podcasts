# H21 duration field consolidation + normalization

**Status:** L3 design. GitHub issue: [GH#868](https://github.com/BashfulBits/city-meeting-podcasts/issues/868).

## Problem

The current pipeline stores and derives meeting duration through several names and clocks:

- `Episode.duration` / record `duration`: provider/source duration.
- `Episode.audio_duration_served` / record `audio.duration_served`: intended served hosted duration.
- `SourceMedia.duration` + `duration_basis`: per-source planning duration.
- `Timeline.segments[].served_end` / `edl_duration(timeline)`: planned served EDL/cue duration.
- `WorkItem.duration_hours`: derived ordering/cache field in `state/work.json`.
- report and telemetry fields such as `duration_hours`, `declared_duration_seconds`,
  `probed_duration_seconds`, `audio_seconds`, and `served_seconds`.

This split caused live external-worker telemetry to record `0.0h` for episodes whose timeline already
encoded a real served length. It also made the earlier "transcription will heal empty duration fields"
assumption false for external pull workers, and weak even for local transcribe lanes: the local ASR path
can probe hosted audio, but a transcribe-lane push does not own the `audio` block, so that mutation is not
a durable catalog invariant.

The desired durable model is two persisted episode-level scalar fields:

| Field | Meaning | Authoritative owner |
|---|---|---|
| `source_duration_seconds` | Best-known pre-edit/source program duration, usually provider-declared or source-probed. | provider/timeline/audio normalization path |
| `served_duration_seconds` | Duration of the actual served enclosure. Write it only from a canonical read of the hosted artifact (probe or reused artifact carrying that measured value); otherwise leave it missing. | audio-owned materialization and pre-dispatch normalization |

`edl_duration(timeline)` remains derived from `timeline`, not persisted as a third scalar. That preserves
H18's ability to detect `served_duration_seconds` vs EDL/render mismatches instead of reconciling them
away.

## Ownership model

`Episode` is currently a mutable dataclass/DTO, not an aggregate root that owns duration invariants. Stages
mutate it directly, `records.py` serializes/deserializes it, and durable ownership is enforced by
record-block merge rules:

- Provider/fetch code refreshes top-level provider fields.
- Audio lane owns the `audio` and `media_availability` blocks.
- Transcribe/align lanes own `transcript` and `provider_transcript`.
- `integrity` is audit-owned and preserved across ordinary lane pushes.
- Planning fields (`sources`, `timeline`, `chapters`, `chapters_basis`) have special stale-planning
  preservation rules.

Do not try to solve H21 by adding mutating methods to `Episode` alone. A class method cannot override
lane ownership. The safer path is canonical accessor/update helpers plus one normalization path that runs
under the correct ownership before work is dispatched.

## Canonical model

Add a small duration module or record helper surface with both object and dict entrypoints:

- `episode_source_duration_seconds(ep) -> float | None`
- `episode_served_duration_seconds(ep) -> float | None`
- `episode_duration_hours(ep) -> tuple[float, Literal["served", "source", "unknown"]]`
- `record_source_duration_seconds(rec) -> float | None`
- `record_served_duration_seconds(rec) -> float | None`
- `record_duration_hours(rec) -> tuple[float, Literal["served", "source", "unknown"]]`
- `set_source_duration_seconds(ep_or_rec, value, *, basis/evidence)`
- `set_served_duration_seconds(ep_or_rec, value, *, basis/evidence)`

Backward-compatible reads must accept legacy `duration` and `audio.duration_served` until migration is
complete. New writes must use `source_duration_seconds` and `served_duration_seconds`.

The helper precedence is:

1. `served_duration_seconds > 0`
2. legacy `audio.duration_served > 0`
3. `source_duration_seconds > 0`
4. legacy `duration > 0`
5. unknown

For explicit source-only consumers, use only source fields. For feed audio duration, ASR admission,
work ordering, and worker telemetry, use served-first helpers.

## Data-store cost guardrails

Duration access must not become a hidden metered-read multiplier:

- Ordinary planning, work manifest construction, external-worker claim loops, reporting, feed rendering,
  and telemetry must read duration from already-loaded `episodes.json` records or the already-loaded
  `state/work.json` manifest/sidecar.
- Do not list R2 prefixes or fetch per-episode R2 coordination objects to answer duration questions.
  R2 remains for CAS coordination and existing telemetry/lease ledgers, not record-level duration lookup.
- Records stay on B2 today, and reading per-source `episodes.json` from B2 is acceptable because it is
  already the canonical planning input and B2 reads are not the metered bottleneck.
- The optional re-probe path may fetch hosted audio, but it must be explicit, bounded, and visible in
  logs/summary output. It must never run implicitly inside a hot external-worker claim loop.
- The pre-dispatch planner/reconcile normalization step may probe hosted audio when
  `served_duration_seconds` is missing for otherwise claimable work. Every such probe must emit a warning
  and count a metric, because a missing served duration after audio materialization means the audio lane
  failed to persist the canonical value.

## Healing path

The durable healing path is an audio-owned/pre-dispatch normalization step:

1. Rebuild or load per-source canonical records.
2. For each episode with hosted audio and missing `served_duration_seconds`, attempt a cheap hosted-audio
   duration probe when the run mode allows probing.
3. If probing succeeds, write `served_duration_seconds` through the audio-owned/normalization path and
   preserve unrelated lane-owned blocks.
4. If probing is not allowed or fails, leave `served_duration_seconds` missing rather than inferring it
   from `timeline` or source metadata.
5. Emit a warning line/metric for every probe, probe failure, or still-missing episode because the
   expected steady state is that audio materialization already populated the field.

Add a manual GitHub Action for the one-time catalog cleanup:

- `workflow_dispatch` only.
- Inputs: `source` optional, `uid` optional, `dry_run` default true, `max_items` default bounded,
  `probe_existing` default true.
- It restores canonical B2 records, probes hosted audio for missing or legacy-only served durations, writes
  normalized records on apply, and uploads a JSONL/summary artifact listing changed, probed, failed, and
  skipped rows.
- It must not touch R2 coordination prefixes except the existing restore/push machinery already required
  by state sync.

## PR phasing

### PR1: Canonical helpers + compatibility reads

Files:

- `citypods/models.py`
- `citypods/records.py`
- new `citypods/durations.py` or a focused helper section in `records.py`
- tests under `tests/`

Changes:

- Add canonical helper functions for object and record dict access.
- Add `Episode.source_duration_seconds` and `Episode.served_duration_seconds` while retaining legacy
  fields for compatibility.
- Teach `record_to_episode` to read new fields first and legacy fields second.
- Teach `episode_to_record` to write new fields and temporarily mirror legacy fields only if required by
  unchanged consumers in later PRs.
- Unit-test precedence, zero/negative handling, legacy read compatibility, and no mutation on read.

Acceptance:

- No behavior change in feed snapshots.
- All existing records remain readable.
- New helper tests cover both `Episode` and raw record dicts.

### PR2: Migrate consumers off raw duration fields

Files:

- `citypods/stages.py`
- `citypods/ops/workqueue.py`
- `citypods/compute/external_worker.py`
- `citypods/compute/worker_telemetry.py`
- `citypods/feeds.py`
- `citypods/media.py`
- `citypods/site.py`
- `citypods/bench.py`
- `citypods/availability_digest.py`
- `scripts/compute/report_workers.py`

Changes:

- Replace direct `ep.duration`, `ep.audio_duration_served`, and record `audio.duration_served` reads in
  routing/telemetry/feed/report logic with canonical helpers.
- Keep `SourceMedia.duration` and `edl_duration(timeline)` only inside timeline/planning/integrity code.
- Stop treating `WorkItem.duration_hours` as an independent truth; it is a manifest-local cached helper
  derived from canonical fields.
- Ensure external workers log duration from the same helper as workqueue/reporting.

Acceptance:

- External-worker metadata and reports no longer show `0.0h` when records contain a source, served, or
  timeline fallback duration.
- Work ordering and ASR local-duration admission use identical served-first semantics.
- Tests assert that `WorkItem.duration_hours` is regenerated from records and round-trips only as a cache.

### PR3: Pre-dispatch normalization + warning telemetry

Files:

- `citypods/run.py`
- `citypods/statesync.py`
- `citypods/compute/dispatch.py`
- `citypods/ops/workqueue.py`
- `citypods/media.py`
- tests under `tests/`

Changes:

- Add the pre-dispatch normalization step before ASR/external work manifest generation.
- Let it probe hosted audio only when `served_duration_seconds` is missing and probing is enabled for that
  run context.
- Emit structured warnings/metrics: `duration_normalized_from_probe`, `duration_probe_failed`, and
  `duration_missing_after_normalization`.
- Persist normalization under an audio-owned or explicitly normalization-owned path so transcribe lanes do
  not try to write protected audio fields.
- Keep the steady-state path record-local and manifest-local.

Acceptance:

- A record with hosted audio and missing served duration is healed before dispatch and warns.
- A record with timeline but no hosted/probed value stays missing and warns.
- A transcribe-lane push cannot regress a normalized served duration.
- Tests prove no R2 list/read path is added for duration lookup.

### PR4: Manual normalization action

Files:

- `.github/workflows/duration-normalize.yml`
- new `scripts/normalize_durations.py`
- tests for script dry-run behavior

Changes:

- Add a manual workflow to re-probe existing hosted audio and normalize duration fields.
- Default to dry-run and bounded `max_items`.
- Emit a JSONL artifact and summary table.
- Support `source` and `uid` filters for targeted repair.

Acceptance:

- Dry-run performs no writes.
- Apply mode writes only changed records.
- Summary distinguishes probed, unchanged, skipped, and failed.
- Workflow has minimal necessary permissions and no broad R2 listing behavior.

### PR5: Remove legacy write paths and enforce the contract

Files:

- `citypods/models.py`
- `citypods/records.py`
- repo-wide direct-read sweep
- tests

Changes:

- Stop writing legacy scalar names once all consumers use helpers.
- Keep legacy reads indefinitely or until a later catalog migration removes the need.
- Add tests or lint-style checks that catch new direct reads in hot consumers.
- Update `ARCHITECTURE.md` and `CHANGELOG.md` when implementation ships.

Acceptance:

- New records contain `source_duration_seconds` and `served_duration_seconds`.
- No production consumer reads legacy fields directly.
- Feed snapshots and ASR planning tests remain stable except for intentional duration-label fixes.

## Risks

- **Semantic drift:** `served_duration_seconds` must mean actual hosted duration when known, not planned
  EDL duration. Unknown is preferable to an inferred but incorrect scalar.
- **Lane ownership:** transcribe/external worker paths must not become hidden writers of audio-owned
  fields. Normalization must run before dispatch or under explicit ownership.
- **Cost drift:** probing missing durations can accidentally become a hot-path download pattern. The
  design limits probing to pre-dispatch normalization and the manual action, with warning metrics.
- **Migration churn:** compatibility reads are required because historical records carry `duration` and
  `audio.duration_served`.

## Done criteria

- There are exactly two persisted episode-level scalar duration fields in new writes:
  `source_duration_seconds` and `served_duration_seconds`.
- Timeline/EDL planned duration remains derived from `timeline`.
- External-worker telemetry, worker reports, work ordering, ASR admission, feeds, and site rendering all
  read duration through canonical helpers.
- Missing served duration is healed before dispatch when possible and always warned when normalization
  must probe or leave a value missing.
- No duration lookup path introduces broad R2 reads or lists.
