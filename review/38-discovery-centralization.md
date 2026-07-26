# GH#1023 Follow-on Discovery Centralization

**Maturity: L2 designed breakout · follow-on to the immediate no-refresh deploy fix · last updated
2026-07-25**

This document defines the work in GH#1023 that is intentionally outside the immediate deploy slice.
The immediate slice adds `--no-refresh` to the production render command, proves that path makes no
provider episode-list requests, and exposes canonical-state age. This breakout defines the later
topology change: one discovery producer owns provider polling and artifact/render consumers operate
from durable canonical state.

The design follows GH#1014's shipped conditional-refresh contract. It does not replace the existing
`SourcePipeline`, append-only records, `source_refresh.json`, stage-completion fingerprints, or the
durable work/lease substrate.

## 1. Problem and boundary

Today provider discovery is embedded in general build orchestration. Render, audio, and other lanes
can therefore rediscover the same source even when their work can be planned from persisted records.
This causes provider availability and deploy availability to share a failure domain, and makes the
meaning of a refresh depend on which workflow happened to run first.

The follow-on target is:

```text
scheduled/manual refresh
          │
          ▼
 conditional provider polling
          │
          ▼
 canonical records + generation + dirty plan
       ┌──┴──────────────┐
       ▼                 ▼
 audio/ASR workers       render deploy
 records-only planning   --no-refresh
 media acquisition       provider-free
 remains allowed
```

The phrase “records-only” means that consumers do not fetch provider episode lists or rebuild
discovery input. Audio may still fetch media required to materialize an already-planned audio
artifact; media transfer is not discovery.

### In scope

- A separately scheduled and manually dispatchable discovery/refresh producer.
- A generation-stamped source snapshot manifest over the existing per-source records.
- Durable dirty-UID/work planning consumed by audio and later artifact lanes.
- Per-source freshness, partial-refresh, and provider-error semantics.
- Compare-and-swap/revision protection against stale writers.
- Operational visibility and forced-refresh/manual-repair paths.

### Out of scope

- A new hosted queue or scheduler service.
- Copying the complete record store into generation-specific duplicate objects.
- Moving records to SQL or introducing the future Interaction seam.
- Eliminating provider media downloads from audio.
- Any content pipeline-version bump or automatic artifact backfill.
- Adaptive polling beyond the bounded cadence already designed in `review/16`.

## 2. Architecture decisions

### 2.1 Use a dedicated refresh producer

Candidate topologies were considered:

| Option | Benefit | Cost | Decision |
|---|---|---|---|
| Deploy preflight refresh | Fresh immediately before render | Deploy latency and provider outages remain coupled to publishing | Reject |
| Dedicated scheduled/manual producer | Clear ownership; isolates deploy; reuses Actions and object storage | Requires durable generation and freshness semantics | **Choose** |
| Event-driven queue | Lower change latency at larger scale | New scheduler/queue, replay, dedupe, and operations contract | Defer |

The producer should use its own workflow concurrency group so scheduled and manual refreshes cannot
run concurrently. A manual `refresh-now` mode may narrow to selected sources or cities without
changing the canonical publication protocol.

The producer is the only workflow allowed to perform provider episode-list discovery. Its existing
conditional validator/content-digest logic remains the refresh algorithm.

### 2.2 Use a generation envelope, not duplicate snapshots

The canonical records remain the existing per-source record objects. A generation is a compact
manifest identifying the exact source-record revision/digest that consumers should use:

```json
{
  "version": 1,
  "generation": 42,
  "producer_run_id": "...",
  "created_at": "...",
  "sources": {
    "arlington-council": {
      "record_key": "records/arlington-council.json",
      "record_digest": "sha256:...",
      "source_revision": 18,
      "last_success": "...",
      "last_attempt": "...",
      "next_poll_at": "...",
      "dirty_uids": {
        "stable-uid": "metadata_changed"
      },
      "last_error": null
    }
  },
  "complete": true,
  "base_generation": 41
}
```

Do not copy all records under `generation-42/`. That would create a second canonical representation,
increase storage traffic, and complicate append-only merges. The existing state manifest/CAS support
is the foundation for publishing this envelope atomically.

A generation may retain the prior source revision when that source's refresh fails. The manifest
must make this visible rather than deleting or replacing the last valid records. This permits a
deploy to publish a valid last-known catalog while reporting source freshness accurately.

### 2.3 Keep the work plan level-triggered and durable

The producer should update the existing durable work manifest and stage input fingerprints rather
than introducing one-shot queue events:

```text
canonical record + input fingerprint
              │
              ▼
       durable work manifest
              │
       ┌──────┴──────┐
       ▼             ▼
    audio          ASR/other lanes
```

An item remains actionable until its stage completion marker matches the current relevant input.
This is retry-safe, rebuildable from records, compatible with existing leases, and resilient to a
worker crashing after claiming but before persisting its result.

Dirty UIDs in the generation are planning hints and observability, not an ephemeral queue. A
reconciliation command must be able to rebuild actionable work from canonical records if a producer
run is interrupted after record persistence but before work-manifest persistence.

### 2.4 Separate discovery from media acquisition

Audio consumers must not fetch provider episode lists or recompute source archives. They may fetch a
media URL while materializing a planned audio artifact:

```text
provider list ───────────────► refresh producer only
                                   │
                                   ▼
                           canonical media metadata
                                   │
                                   ▼
                              audio worker
                                   │
                                   └── media request allowed
```

This preserves the existing media/proxy/rate-limit behavior while removing redundant discovery.
The implementation and logs must use “provider episode-list requests” when asserting the deploy
zero-provider-call invariant; “no provider calls” would incorrectly describe audio.

### 2.5 Track freshness per source

Freshness is not a single catalog timestamp. Each source needs:

- `last_success`
- `last_attempt`
- `next_poll_at`
- validator/content digest state
- source revision
- last error and error class
- whether the current record revision is inherited from an earlier generation

The render summary should report both aggregate health and the oldest/most stale sources.

Recommended policy:

| State | Meaning | Default render behavior |
|---|---|---|
| Fresh | Within configured source freshness window | Publish normally |
| Stale | Last valid state exists but soft SLA is exceeded | Publish with visible warning |
| Unavailable | No valid canonical state exists | Apply existing empty-feed/validation policy |
| Invalid | Manifest missing or incompatible | Fail closed |

The soft warning should be derived from the configured poll interval, initially around two missed
expected intervals. A hard operational alert should fire after a configured maximum age, initially
around three intervals. Staleness alone should not fail the production deploy by default: the
provider-outage requirement is to keep publishing a valid last-known catalog. A future strict
environment may opt into `fail_on_stale`, but that is not the production default.

Forced refresh/manual repair must support a source, city, or full catalog and must record the actor,
requested scope, reason, and resulting generation or failure.

### 2.6 Protect against stale writers

There are two races:

```text
Producer A reads generation 41 ─┐
                                ├─ B publishes 42
Producer B reads generation 41 ─┘
                                └─ A must not overwrite 42
```

Use the existing workflow concurrency group plus manifest compare-and-swap. A producer that loses
CAS must reload the newer manifest and merge only source results still based on the expected source
revision. Otherwise it discards the stale result and replans.

Consumers can also outlive the generation they planned from:

```text
audio plans from generation 42
refresh publishes generation 43 for the same UID
audio completes using generation 42
```

Every work item must carry the source revision/input fingerprint it was planned against. Artifact
consumers update only their owned blocks through the existing lane-preserving merge. A stale result
may be retained only when its input fingerprint still matches; it must not overwrite newer official
metadata or invalidate a newer artifact pointer.

The producer must never write artifact-lane state, and artifact consumers must never write provider
discovery metadata.

## 3. Failure and consistency semantics

### Successful refresh

1. Load the current generation and source refresh metadata.
2. Refresh only due sources using GH#1014 conditional detection.
3. Merge observations into append-only records.
4. Calculate normalized input fingerprints and dirty UIDs.
5. Persist changed source records.
6. Rebuild/update the durable work manifest.
7. Publish the new generation envelope with CAS.
8. Emit a summary containing generation, source counts, dirty counts, freshness, and errors.

### Partial refresh

If some sources fail, retain their prior valid source revision in the new generation and mark them
stale/error. A partial generation is publishable when all manifest entries point to valid records.
It is not publishable when a source has neither a new result nor a prior valid revision.

### Producer interruption

The producer may persist records before publishing the generation. A later run reconciles the
records, refresh metadata, and work manifest. The generation manifest remains the consumer boundary;
consumers must not infer an unpublished generation from arbitrary local files.

### Consumer interruption

Existing stage completion markers, leases, and owned-block merges provide retry behavior. A consumer
reconciler can compare the work item's planned fingerprint with current records and requeue only the
affected stage.

## 4. Proposed implementation slices

This breakout is intentionally separate from the immediate deploy fix.

### Slice A — immediate GH#1023 work

- Add `--no-refresh` to `.github/workflows/deploy.yml`.
- Add a workflow regression test asserting the production render command contains it.
- Add a provider-call spy test for the render/no-refresh path.
- Add canonical-state age/source-refresh age to the existing build summary.

### Slice B — generation contract

- Add a versioned generation data model and serializer.
- Add storage/CAS publication and load helpers.
- Add tests for complete, partial, invalid, and stale generations.
- Add source revision/input-fingerprint checks.

### Slice C — dedicated producer

- Add a dedicated refresh workflow with scheduled and manual modes.
- Move discovery ownership to the producer while retaining `SourcePipeline` as the implementation
  boundary for provider refresh and record merge.
- Persist the generation and durable work plan only after source persistence succeeds.
- Add run summary and operator-facing freshness reporting.

### Slice D — consumer cutover

- Make audio planning consume the generation/work manifest without episode-list discovery.
- Make ASR and future artifact lanes use the same records-only contract.
- Preserve media acquisition where required.
- Add stale-generation and concurrent-writer integration tests.
- Remove now-unreachable consumer-side discovery paths only after production telemetry confirms the
  producer is authoritative.

## 5. Test and acceptance plan

### Unit tests

- Generation serialization and schema validation.
- CAS publication rejecting an older generation.
- Source revision merge behavior after a concurrent refresh.
- Partial refresh retaining a prior valid source revision.
- Fresh/stale/unavailable/invalid classification.
- Work-plan rebuild after producer interruption.
- Consumer completion rejected or requeued when its input fingerprint is stale.

### Workflow tests

- Refresh workflow has its own concurrency group.
- Deploy invokes render with `--no-refresh` and has no refresh step.
- Audio/ASR workflows do not invoke provider episode-list discovery.
- Manual refresh scope and forced-refresh inputs are passed safely and do not enter shell syntax.

### Acceptance criteria

- Exactly one scheduled producer owns provider episode-list polling.
- Production deploy performs zero provider episode-list requests.
- Rendering a generation twice produces identical feed/page output.
- Provider outage leaves a valid prior generation deployable with visible freshness warnings.
- A source with no valid prior state follows an explicit, tested empty/validation policy.
- Stale producers cannot replace a newer generation or official metadata.
- Audio consumes durable records/work planning and does not rediscover episode lists.
- Durable work can be rebuilt after any producer or consumer interruption.
- No pipeline-version bump or automatic catalog-wide artifact backfill occurs.

## 6. Operational and migration notes

The first producer rollout should run in observation/shadow mode or with a narrowly scoped catalog
before consumer cutover. Compare:

- provider episode-list requests by workflow;
- refresh latency and source freshness;
- dirty UID counts;
- work-manifest deltas;
- provider failures and partial-generation frequency;
- audio/render output parity.

The producer must not delete historical records or archived meetings. Existing append-only record
semantics and split audio/feed hashes remain unchanged. Existing provider SSRF validation and
provider transport/proxy gates apply to all refresh and media requests.

The design deliberately leaves room for a later event-driven scheduler: the generation manifest and
level-triggered work plan are the stable interfaces. A future scheduler can replace the cron trigger
without requiring a new record model or consumer contract.
