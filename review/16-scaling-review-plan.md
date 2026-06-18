# Efficiency and Scaling Review Plan: 10 to 500 Cities

## Executive conclusion

The project's architecture is fundamentally compatible with 500 cities, but the current workflow
topology and state-transfer model would become inefficient well before then.

The limiting factors are unlikely to be transcript or diarization compute under the assumption that
sufficient GPU capacity is added. The likely constraints are:

1. Every scheduled lane refreshes every provider source, even when that lane can operate entirely
   from durable records.
2. Every runner pulls the complete bucket-backed state snapshot, although shards only need a subset.
3. State synchronization repeatedly lists broad object prefixes, turning small logical reads into
   catalog-wide storage operations.
4. A fixed four-shard schedule runs continuously, whether four runners are useful or not.
5. Audio source caching lasts only for one runner invocation, so retries and later scheduled runs
   download provider media again.
6. Provider refresh, render, audio planning, and transcript dispatch are not yet driven by a durable
   incremental work/event model.
7. Static transcript search can become a client-bandwidth and build-output problem unless indexing
   is incremental and aggressively sharded.
8. Monitoring presently emphasizes backlog and outcomes more than per-request efficiency, so network
   amplification could grow unnoticed.

The appropriate target is not to make the existing four workflows bigger. It is:

> Provider polling creates durable change events; durable work manifests schedule only required work;
> runners receive compact source-specific state bundles; immutable media and derived artifacts are
> reused across runs; render and search rebuild only affected partitions.

With that design, the planning estimates are:

- **500 cities:** comfortably supportable.
- **1,000 to 2,000 cities:** plausible on standard public-repository GitHub-hosted runners, assuming
  moderate meeting frequency, GPU offload, incremental state, and provider-aware scheduling.
- **2,000 to 4,000 cities:** technically possible but increasingly operationally awkward because of
  provider traffic, long-tail media failures, static-index size, runner-IP reputation, and workflow
  orchestration, rather than raw CPU minutes.
- **Migration planning should begin around 1,000 cities**, even if Actions can still keep up.
- **A dedicated scheduler/media worker becomes the sensible default around 1,500 to 2,500 cities**,
  or sooner if provider throttling and GitHub egress-IP behavior dominate.

These are planning ranges, not promises. The first phase of the review must replace "cities" with
measured variables: sources, feeds, meetings per day, source-media hours, provider requests, changed
partitions, and runner-seconds per work class.

---

## 1. Baseline observations

### 1.1 Catalog size is already larger than the city count suggests

At the time this plan was written, the repository contained:

- **10 city configuration files**
- **85 feed configuration files**

This matters because the scaling unit varies by subsystem:

| Subsystem | Correct scaling unit |
|---|---|
| Provider polling | Unique source |
| Record persistence | Unique `source_key` |
| Audio and transcript work | Episode |
| RSS and city-page rendering | Feed |
| Per-meeting pages | Episode |
| Search | Transcript bytes/tokens and index partitions |
| Provider rate limits | Provider domain or tenant |
| Storage cost | Hosted media hours |
| GitHub workflow overhead | Jobs and runner starts |

A 500-city catalog may mean several thousand feeds, but considerably fewer unique provider fetches if
each city's board feeds share a source. The code already deduplicates per-board processing through
`source_key`, which is a strong foundation.

### 1.2 Existing scaling primitives to preserve

The review should preserve, not replace:

- Append-only records.
- Stable episode UIDs.
- Split audio and feed-content hashes.
- Content-addressed audio and transcripts.
- Separate render, audio, and ASR lanes.
- Source-atomic sharding.
- Global backlog prioritization.
- Foreign-block-preserving record merges.
- Wall-clock stopping and graceful resumption.
- Provider concurrency limits and distributed provider leases.
- Circuit breakers and materialization backoff.
- Pluggable compute backends.

The project does not require a wholesale rewrite for 500 cities. It needs an incremental scheduling
and state-I/O redesign.

---

## 2. Review goals and measurable success criteria

### 2.1 Network efficiency

For each work class, determine:

- How many HTTP and storage requests occur per:
  - scheduled run;
  - source;
  - new episode;
  - unchanged source;
  - failed episode;
  - completed artifact.
- How many bytes are:
  - downloaded from providers;
  - downloaded from object storage;
  - uploaded as state;
  - uploaded as media;
  - transferred to browsers for search.
- Which requests are:
  - required;
  - duplicated across lanes;
  - duplicated across shards;
  - repeated because cache metadata is missing;
  - repeated because cached bytes are unavailable.

#### Target

At steady state, an unchanged source should cost approximately:

- zero media downloads;
- zero chapter/transcript-page fetches;
- one conditional provider catalog request at its due interval;
- zero record uploads;
- zero search-partition rebuilds;
- zero city/feed rerenders.

### 2.2 Runner efficiency

Measure:

- Useful work seconds versus:
  - checkout;
  - Python installation;
  - apt installation;
  - model preparation;
  - full-state restoration;
  - waiting on leases;
  - polling empty backlogs;
  - uploading unchanged state.
- Per-job useful-work ratio.
- Empty or nearly empty shard frequency.
- Queue delay.
- Average and p95 job duration.
- Runner-hours per new meeting.

#### Targets

- At least **80% of long-running audio worker time** should be useful media work.
- At least **90% of lightweight refresh/render job time** should be useful application work rather
  than environment setup.
- Do not start an audio/transcript runner when no eligible work exists.
- Empty shard rate below **5%**.
- A new meeting should become visible in site metadata within one refresh interval even if media
  processing is delayed.

### 2.3 Provider safety

Measure per tenant/domain:

- Requests and media reads per minute.
- Concurrent requests.
- HTTP status distribution.
- Retry count and retry delay.
- Circuit-breaker activations.
- Truncated downloads.
- Median and p95 throughput.
- Attempts until success.
- GitHub runner versus local/external-worker success rate.

#### Targets

- Zero published truncated artifacts.
- Provider throttling below an agreed threshold, initially perhaps **less than 0.5% of attempts**.
- Automatic reduction of concurrency when throttling rises.
- No monitoring workflow should compete with production media fetches.

### 2.4 Incrementality

Measure:

- Percentage of sources unchanged.
- Percentage of source records downloaded despite being irrelevant to a shard.
- Percentage of state files uploaded unchanged.
- Number of render outputs rewritten per actual source change.
- Search partitions rebuilt per transcript change.

#### Targets

- A shard downloads only its assigned state plus small global manifests.
- A run uploads only changed state objects.
- One new transcript rebuilds one source/city search partition, not the catalog.
- A metadata-only provider change does not invalidate audio.

### 2.5 Reliability and fallback

Measure:

- Provider outage survival.
- Storage outage survival.
- Partial shard failure recovery time.
- Lease recovery.
- Staleness of last-known-good pages.
- Number of failed workflows that result in user-visible regressions.

#### Targets

- Provider failure leaves the last-known-good catalog online.
- Object-store read failures prevent unsafe writes rather than overwriting newer state.
- A killed worker loses at most its in-progress temporary transfer, not completed immutable work.
- Backlog automatically resumes without manual state repair.
- Each source exposes last successful refresh and data-freshness status.

### 2.6 Client performance

Measure separately for:

- Directory index.
- City page.
- Meeting page.
- Transcript fetch.
- Search bootstrap.
- Search query.
- Search partition transfer.
- Mobile memory usage.

#### Initial budgets

- Directory HTML compressed: **less than 150 KB at 500 cities**.
- Initial search JavaScript plus manifest: **less than 200 KB compressed**, excluding lazily loaded
  indexes.
- No transcript index downloaded before search interaction.
- City-scoped search initial partition: ideally **less than 1 MB compressed**; hard warning at 2 MB.
- Search interaction p95 after required partitions load: **less than 200 ms** on a midrange mobile
  device.
- Meeting transcript lazy-load must not block the page/player shell.
- Search-worker memory target: **less than 100 MB** for an ordinary city-scoped search.

---

## 3. Phase 0: Instrument before optimizing

This should be the first implementation phase. Optimizing without request-level telemetry risks moving
costs rather than removing them.

### 3.1 Add a run telemetry schema

Create a durable, append-friendly event format, partitioned by date/run/source rather than one shared
JSONL file.

Each run record should include:

```text
run_id
workflow
lane
shard
started_at / finished_at
sources_considered
sources_due
sources_polled
sources_changed
sources_failed
episodes_considered
work_items_started/completed/deferred/failed
runner_seconds
setup_seconds
useful_work_seconds
provider_requests_by_domain/method/status
provider_bytes_downloaded
storage_requests_by_operation
storage_bytes_downloaded/uploaded
cache_hits/misses
source_media_cache_hits/misses
lease_wait_seconds
rate_limit_wait_seconds
render_outputs_written/skipped
search_partitions_written/skipped
```

Each source record should include:

```text
source_key
provider
tenant/domain
last_attempt
last_success
change_token
content_digest
poll_interval
consecutive_unchanged
consecutive_failures
request_count
bytes_downloaded
new_episode_count
backlog by work class
```

### 3.2 Instrument all outbound paths

Instrumentation boundaries:

- `GuardedHTTPAdapter.send`
- ffprobe and ffmpeg remote fetch wrappers
- storage `list_objects`, `get_file`, `put_file`, `exists`, and `delete`
- provider `fetch_episodes`
- provider chapter/transcript fetches
- source-cache fetches
- transcript retrieval used by search generation
- GitHub Actions API polling

### 3.3 Add baseline benchmark scenarios

Create deterministic benchmark fixtures for:

1. 10 cities/current feed ratio.
2. 100 cities.
3. 500 cities.
4. 500 cities with no changes.
5. 500 cities with 1% of sources changed.
6. 500 cities with one new two-hour meeting per city.
7. One provider tenant returning 403/429.
8. Object-storage latency at 50, 200, and 500 ms.
9. Search indexes with 10,000, 50,000, and 250,000 meetings.
10. A cold cache versus a warm cache.

Synthetic provider and storage backends should count requests and bytes exactly.

### 3.4 Baseline report

The review should produce a machine-readable report and a Markdown decision document containing:

- Current request graph.
- Current bytes graph.
- Current runner-time graph.
- Estimated 500-city amplification.
- Top ten waste sources by cost.
- Top ten risks by reliability impact.

#### Exit gate

Do not implement adaptive optimization until the baseline can reliably say:

> An unchanged scheduled cycle caused X provider calls, Y storage operations, Z downloaded bytes,
> and N runner-minutes.

---

## 4. Phase 1: Stop unnecessary provider contact

This is probably the highest-value change.

### 4.1 Separate provider refresh from artifact processing

The current audio global queue prepares all unique sources before processing its backlog. Transcript
lanes have a provider-failure fallback to records, but still attempt the provider first.

#### Proposed topology

##### Refresh workflow

Responsibilities:

- Poll sources due for refresh.
- Parse provider lists.
- Assign UIDs.
- Merge append-only records.
- Discover new or changed episode metadata.
- Add/update durable work-manifest entries.
- Persist only changed source records.
- Emit dirty render/search partitions.

##### Audio workflow

Responsibilities:

- Read the audio work manifest.
- Load only assigned records.
- Resolve media when work begins.
- Download/encode/upload.
- Update the owned audio block.
- Never poll provider episode lists.

##### Transcript/diarization workflow

Responsibilities:

- Read only persisted records and hosted audio.
- Dispatch/reconcile inference.
- Never poll provider episode lists.

##### Render workflow

Responsibilities:

- Read persisted records.
- Optionally consume a refresh result produced immediately before it.
- Never independently refetch all providers.

This turns provider polling into one controlled operation rather than one operation per lane.

### 4.2 Implement real conditional refresh

Implement:

```text
refresh_source(source):
    load refresh metadata
    if not due:
        return persisted archive

    conditional GET if supported
    if 304:
        record unchanged
        advance next_poll
        return persisted archive

    otherwise GET and parse
    hash normalized provider response
    if digest unchanged:
        record unchanged
        return persisted archive

    merge and persist changes
```

Prefer conditional GET over HEAD-then-GET. A separate HEAD can double requests, and many providers do
not provide useful validators.

For providers without validators:

- Fetch the list once.
- Hash the normalized raw response or normalized episode identity/metadata.
- Persist the digest.
- Do not invoke downstream work when unchanged.

### 4.3 Adaptive poll intervals

Polling every source every four hours is unlikely to be necessary at 500 cities.

Track each source's recent change cadence and calculate `next_poll_at`.

Suggested bounds:

| Source behavior | Poll interval |
|---|---:|
| Recently changed or active meeting window | 1 to 2 hours |
| Typical active source | 4 to 6 hours |
| Unchanged for 7 days | 12 hours |
| Unchanged for 30 days | 24 hours |
| Repeated provider failures | Exponential backoff, capped at 24 hours |
| Manual priority source | Configured minimum interval |

Avoid missing newly published meetings by maintaining:

- A configured maximum staleness SLA.
- A likely-meeting-day heuristic if useful.
- Random jitter of perhaps plus or minus 10%.
- Manual `refresh-now`.
- Immediate refresh on configuration changes.

### 4.4 Deduplicate shared provider URLs globally

At 500 cities, different feed/source configs may refer to:

- The same RSS URL.
- Different body filters over the same list endpoint.
- Multiple Granicus views under one tenant.
- Reused watch pages or provider metadata endpoints.

Build a refresh plan keyed by canonical URL/request signature so identical requests are executed once
per run and shared.

### 4.5 Cache stable chapter and provider metadata durably

Chapter fetches are often per episode and immutable after publication. Persist:

- Chapter-fetch recipe/version.
- Provider endpoint fingerprint.
- Result digest.
- Last successful fetch.
- "No chapters" as a valid cached result.
- Retry classification.

Do not refetch chapters merely because:

- A runner cache was evicted.
- Audio needs re-encoding.
- Search was rebuilt.
- A transcript model version changed.

#### Phase 1 acceptance criteria

At 500 synthetic cities with no source changes:

- Only due sources are contacted.
- Audio/ASR/diarization perform zero provider-list calls.
- No chapter pages are fetched.
- No record state is uploaded.
- Provider request count is proportional to sources due, not workflows times shards times sources.

---

## 5. Phase 2: Replace full-state synchronization with manifests and targeted reads

The current bucket-backed state is correct and durable, but its transfer algorithm will become
expensive. Each worker should first download one small manifest, then retrieve only the state it needs.

### 5.1 Create a versioned state manifest

Add a compact root manifest:

```json
{
  "version": 1,
  "generation": 4812,
  "updated_at": "...",
  "sources": {
    "<source_key>": {
      "record_key": "state/sources/.../episodes.json",
      "etag": "...",
      "size": 12345,
      "updated_at": "...",
      "work_counts": {
        "audio": 3,
        "transcribe": 1,
        "diarize": 0
      }
    }
  }
}
```

A worker first downloads the manifest, then retrieves only:

- Global work metadata it needs.
- Its assigned source records.
- Relevant telemetry samples.
- Relevant leases.

### 5.2 Use object metadata or ETags

Extend the storage abstraction so reads can be conditional:

- `stat(key) -> etag, size, last_modified`
- `get_file_if_changed(key, local_path, etag)`
- `put_if_generation_matches`, where supported
- At minimum, compare manifest digest before download/upload.

Retain the existing foreign-block merge safety. The optimization should reduce transfers without
weakening fail-safe merging.

### 5.3 Split global and source-local state cleanly

Suggested layout:

```text
state/catalog/manifest.json
state/sources/<source_key>/episodes.json
state/sources/<source_key>/refresh.json
state/sources/<source_key>/work.json
state/sources/<source_key>/metrics/<date>.json
state/work/ready/<class>/<priority>/<id>.json
state/work/leased/<class>/<id>.json
state/work/completed/<date>/<id>.json
state/runs/<date>/<run_id>.json
state/search/<source_key>.json
```

The precise key design should avoid requiring one `list_objects("state/")` for ordinary work.

### 5.4 Upload only changed files

Introduce a dirty-state tracker:

```text
state_write(path) -> mark dirty
push_dirty_state() -> upload only dirty paths
```

Use content hashes as a fallback guard so writing logically identical JSON does not upload it again.

### 5.5 Compact records when needed

Do not split source record files prematurely, but establish triggers:

- Warn at 10 MB.
- Consider partitioning at 25 to 50 MB.
- Partition by year or stable UID prefix while preserving a source-level index.

The same applies to run events and leases: avoid ever-growing shared files.

### 5.6 Cache state on runners only as an accelerator

Keep `actions/cache`, but use:

- Stable dependency-cache keys.
- Generation-specific compact state keys.
- Separate caches for Python wheels, ffmpeg binaries, model files, and source-media spill cache where
  appropriate.
- Avoid caching all of `docs/` if incremental render artifacts can be restored more directly or
  cheaply.

#### Phase 2 acceptance criteria

For one audio shard owning 5% of a 500-city catalog:

- It downloads approximately 5% of source state, not 100%.
- No bucket-wide state listing occurs in the hot path.
- It uploads only source records it changed.
- A cache miss does not materially change request complexity.
- Existing cross-lane lost-update tests continue to pass.

---

## 6. Phase 3: Durable cross-run source-media reuse

The existing `SourceCache` avoids downloading an episode twice within one runner process. At 500
cities, the next efficiency step is avoiding repeat downloads across failed encodes, workflow
interruptions, recipe changes, diarization/transcript tasks, and clip generation.

### 6.1 Distinguish three caches

#### Metadata cache

Small and durable:

- Catalog responses.
- Watch-page parse results.
- Chapter results.
- Media-resolution metadata.

#### Immutable source-media cache

Potentially large:

- Provider source audio or a lossless/stream-copy audio extraction.
- Keyed by source URL identity plus validator/content digest.
- Used for audio rendering, ASR, diarization, clips, and reprocessing.

#### Derived-artifact cache

Already substantially implemented through content-addressed audio/transcript keys.

### 6.2 Source-cache design

Suggested object key:

```text
source-cache/<provider>/<source_key>/<episode_uid>/<source_media_digest>.mka
```

Metadata:

```json
{
  "provider_url": "...",
  "resolved_at": "...",
  "validator": "...",
  "size": 0,
  "duration": 0,
  "codec": "...",
  "source_digest": "...",
  "last_used": "..."
}
```

Rules:

- Cache only after truncation and duration validation.
- Never treat an expiring URL as identity.
- For tokenized HLS, derive identity from stable episode/provider metadata plus validated captured
  bytes.
- On retry, use cached bytes if recipe and source identity permit.
- Audio, ASR, diarization, and clipping consume the same cached source object.
- Pin entries while unfinished work references them.
- Retain recent/high-reuse entries.
- Evict by storage budget and last use.
- Never evict final podcast artifacts merely because the disposable cache is being cleaned.

### 6.3 Cost decision gate

Source-media caching trades provider bandwidth and reliability for object-storage capacity.

The review must calculate:

```text
monthly cache storage cost
vs.
repeat provider bytes avoided
vs.
runner time avoided
vs.
failure reduction
```

For two-hour source media, a stream-copy source cache can be substantially larger than the 96 kbps
podcast output. Therefore:

- First cache only failed/incomplete-work episodes.
- Then cache providers with high throttling or expensive resolution.
- Do not automatically cache every source indefinitely.
- Consider short retention such as 7 to 30 days after dependent work completes.

### 6.4 Range and resume support

For large provider downloads:

- Prefer ffmpeg-compatible reconnection flags where safe.
- Persist completed source-cache objects atomically.
- Do not publish partial objects.
- Evaluate resumable transfer where direct HTTP range requests are supported.
- Record `Accept-Ranges`, content length, and validator.
- Restart with backoff where provider semantics make resume unsafe.

#### Phase 3 acceptance criteria

- A failed encode retry does not redownload source media when a validated cache object exists.
- Audio and transcript/diarization do not independently fetch the same provider media.
- Cache hit/miss and bytes-saved metrics are visible.
- Eviction cannot invalidate final hosted artifacts or records.

---

## 7. Phase 4: Demand-driven GitHub Actions topology

The current schedule starts four audio shards every four hours, four ASR shards every five hours plus
reconciliation, a render deployment every four hours, a daily audit, and weekly endpoint contracts.
This is simple and robust at current scale but can spend runner starts and setup time on empty or
lightly loaded work.

### 7.1 Add a lightweight planner job

A scheduled planner should:

1. Read the catalog manifest.
2. Refresh due providers or trigger a separate refresh matrix.
3. Calculate ready work by class/provider/resource estimate.
4. Determine required shard count.
5. Emit a matrix through job outputs.
6. Skip heavy jobs if there is no work.

Example:

```json
{
  "audio": [
    {"partition": "a", "estimated_minutes": 170},
    {"partition": "b", "estimated_minutes": 150}
  ],
  "transcribe_dispatch": [],
  "render_partitions": ["dallas", "denton"]
}
```

### 7.2 Dynamic shard count

Do not fix the catalog permanently at four shards.

Calculate:

```text
desired_shards =
  ceil(estimated_work_seconds / target_job_seconds)
```

Then clamp by:

- Provider concurrency limits.
- GitHub plan concurrency.
- Storage throughput.
- Memory constraints.
- Maximum useful source parallelism.

Start with a target job duration of perhaps 60 to 180 minutes, rather than 350 minutes, once work is
distributed incrementally.

### 7.3 Provider-aware partitioning

The planner should include:

- Estimated media seconds.
- Historical processing ratio.
- Provider domain/tenant.
- Expected memory.
- Retry risk.
- Work priority.
- Source affinity.

Avoid assigning simultaneous shards that all target the same provider tenant even if source keys
differ.

### 7.4 Shared setup optimization

Repeatedly running `apt-get update` and installing ffmpeg on every heavy runner is avoidable overhead.
Review options in this order:

1. Confirm whether the runner image already provides a suitable ffmpeg.
2. Pin/download a known static ffmpeg build and cache it.
3. Create a small setup action that restores a cached binary.
4. Use a container job only if startup and pull costs are lower.
5. Avoid larger/custom-image runners solely for setup optimization because larger runners are billed
   even for public repositories.

### 7.5 Workflow trigger design

Recommended steady-state topology:

```text
refresh-and-plan.yml: every 2 to 4 hours
  - refresh due sources
  - persist changed records/work
  - render dirty pages/feeds
  - dispatch audio worker matrix if needed
  - dispatch inference jobs if needed
  - update operational summary

reconcile.yml: every 30 to 60 minutes, very lightweight
  - settle external jobs
  - expire leases
  - enqueue follow-up work

audit.yml: daily, sampled/tiered
contracts.yml: weekly, provider representatives only
full-consistency.yml: weekly or monthly
```

### 7.6 GitHub concurrency and network controls

Changes to evaluate:

- Keep separate concurrency groups for render, audio, and reconciliation.
- Replace one global audio lock with provider-safe partition locks only if state ownership permits.
- Add workflow `timeout-minutes` explicitly.
- Add `max-parallel` dynamically or conservatively.
- Use `cancel-in-progress` only for cheap refresh/render work, never for expensive non-checkpointed
  media work.
- Stagger workflows to prevent contracts and media jobs from hitting the same providers
  simultaneously.
- Pass provider distributed-lease configuration into contract checks.
- Add randomized schedule jitter inside the planner.
- Set least-privilege permissions on every workflow.
- Pin third-party actions to commit SHAs after a security review.
- Add dependency and artifact retention limits.
- Avoid relying on stable GitHub-hosted runner IPs.
- If a provider requires allowlisting or treats shared GitHub IPs poorly, move that provider's media
  reads to a fixed-egress proxy, a self-hosted runner, or the external-worker layer.

#### Phase 4 acceptance criteria

- Empty audio backlog starts zero heavy runners.
- Shard count follows measured work.
- No contract probe overlaps production media access to the same throttled provider.
- Planner estimates are within plus or minus 25% of actual runner time for p50 jobs.
- Provider throttling does not rise when scale tests increase shard count.

---

## 8. Phase 5: Automatic iterative resource optimization

The code should become more efficient automatically through bounded control loops, not unconstrained
self-tuning.

### 8.1 Adaptive provider polling

Inputs:

- Time since last change.
- Historical publication cadence.
- Consecutive unchanged polls.
- Consecutive failures.
- Provider rate-limit responses.
- Staleness SLA.

Output:

- `next_poll_at`.

Hard bounds prevent a bad model from polling too aggressively or too rarely.

### 8.2 Adaptive provider concurrency

Maintain per-domain concurrency profiles:

```text
current_slots
success_rate
403/429_rate
p50/p95 throughput
lease_wait
truncation_rate
```

Use additive increase after a sustained healthy window and multiplicative decrease on
throttling/truncation. Configure minimum and maximum values, recover slowly, persist profiles by
provider domain, and require enough samples before increasing.

Example:

```text
healthy for 100 transfers -> slots + 1
403/429 or truncation spike -> max(1, floor(slots / 2))
```

### 8.3 Adaptive shard sizing

Use recent work-class durations:

```text
estimated_seconds =
  media_duration * historical_ratio(provider, recipe, path)
  + fixed_overhead
```

Bin-pack work so each shard targets a chosen duration and memory envelope. Recompute after every run
using exponentially weighted moving averages, with conservative percentiles for admission decisions.

### 8.4 Adaptive cache retention

Track per cached source:

- Size.
- Download cost.
- Number of reuses.
- Provider failure rate.
- Remaining dependent work.
- Age.

Calculate a value score:

```text
expected future bytes avoided
+ expected retry time avoided
+ reliability premium
- monthly storage cost
```

Evict the lowest-value unpinned entries until under budget.

### 8.5 Adaptive search partitioning

Start with city/source partitions.

Split when:

- Compressed index exceeds threshold.
- Browser memory exceeds threshold.
- Query latency exceeds threshold.

Merge tiny partitions where HTTP overhead exceeds payload value.

Possible hierarchy:

```text
global metadata manifest
state/city metadata shards
city/year transcript shards
optional common-term fragments
```

Partition decisions must remain deterministic and versioned.

### 8.6 Automatic anomaly response

Examples:

- Provider request rate doubles with no catalog growth: emit a high-severity efficiency alert.
- Cache hit rate drops suddenly: flag a cache-key/version regression.
- Bytes downloaded per new episode rise: investigate repeated source transfer.
- State download bytes grow faster than the catalog: trigger a state-partition warning.
- Provider throughput drops while 403s rise: reduce slots automatically.
- Search initial payload exceeds budget: fail the build or disable global transcript loading until
  repartitioned.
- No useful work occurs in several heavy jobs: lower minimum shard count or cancel the schedule.

### 8.7 Guardrails

Automatic tuning must never:

- Violate configured provider ceilings.
- Increase cost beyond configured monthly budgets.
- Alter artifact recipes.
- Change retention of final/official records.
- Delete source-cache objects still referenced by pending work.
- Delay refresh beyond the catalog-freshness SLA.
- Change pipeline versions.

---

## 9. Phase 6: Monitoring, alerting, and fallback design

### 9.1 Operational dashboard additions

#### Catalog freshness

- Sources due/overdue.
- Last successful refresh.
- Oldest source.
- Publication-to-discovery latency.

#### Network

- Provider requests/day.
- Storage operations/day.
- Provider GB downloaded.
- State GB downloaded/uploaded.
- Source-cache savings.
- HTTP status and retry distribution.

#### Efficiency

- Runner-hours/day.
- Useful-work ratio.
- Empty job count.
- Jobs per completed episode.
- Setup time versus useful time.
- State bytes per source.
- Search bytes per indexed transcript hour.

#### Backlog

- Ready, leased, deferred, and permanently failed by work class.
- Estimated drain time.
- Oldest pending item.
- Inflow versus completion rate.
- GPU budget and queue.

#### Provider health

- 403/429 rate.
- Circuit state.
- Current adaptive concurrency.
- p95 fetch duration.
- Truncation count.
- Representative contract status.

### 9.2 Alert levels

#### Warning

- Refresh overdue by twice the SLA.
- Cache hit rate below target.
- Search shard exceeds warning size.
- Planner estimate error over 50%.
- One provider exceeds 1% retries.

#### Error

- Source stale for 24 to 48 hours.
- Backlog age increases for several cycles.
- Provider circuit repeatedly opens.
- State merge push is skipped repeatedly.
- Published feed unexpectedly loses episodes.
- Search index references missing meeting pages.

#### Critical

- Append-only invariant violation.
- Widespread truncated media.
- Storage write corruption.
- Last-known-good site cannot render.
- Multiple sources lose hosted artifact references.

### 9.3 Alert delivery

Prefer:

1. GitHub job summaries for routine metrics.
2. Deduplicated GitHub issues for actionable failures.
3. A static status page for public/operator visibility.
4. Optional webhook/email only for critical failures.

Avoid opening an issue for expected free-tier GPU exhaustion or normal temporary provider backoff.

### 9.4 Fallback modes

#### Provider unavailable

- Render the persisted archive.
- Mark the source stale.
- Increase backoff.
- Continue media/transcript work from persisted records and cached source media.
- Do not erase missing provider entries.

#### Object storage unavailable

- Render from restored local cache if sufficiently fresh.
- Do not push state.
- Do not claim success for new hosted artifacts.
- Retry later.
- Fail safe on merge reads.

#### Source media unavailable

- Reuse valid source cache.
- Otherwise use exponential backoff.
- Preserve the existing hosted artifact.
- Escalate after a configured number of failures.

#### GitHub Actions unavailable or delayed

- Static site and podcast media remain online.
- External inference results remain durable.
- The next planner run reconciles leases and resumes.
- No user-facing rollback occurs.

#### Search build failure

- Publish pages and feeds without the new search index.
- Keep the last-known-good search index.
- Show index freshness.
- Never block core feed publication on search.

---

## 10. Client-side and website scaling review

### 10.1 Directory index

The current index renders cities in chunks and filters an in-page JSON dataset. That should remain
usable at 500 cities because city/feed metadata is small. However, the page also includes a complete
no-JavaScript duplicate of the city list, so the HTML contains both JSON data and full fallback markup.

Review actions:

- Measure compressed HTML at 500, 1,000, and 5,000 cities.
- Consider server-rendering the first page only.
- Put the full directory dataset in a cacheable versioned JSON file.
- Lazy-load the full dataset after first paint.
- Keep a useful but limited no-JavaScript directory or paginated alphabetical pages.
- Add state/provider filters without loading transcript indexes.
- Use a Web Worker only if filtering becomes visibly slow.

### 10.2 Per-meeting pages

The planned hybrid is sound:

- Static shell and SEO excerpt.
- Full transcript fetched lazily.
- Synced playback in client JavaScript.

Additional scaling requirements:

- Transcript URLs must be content-addressed and cacheable for a long period.
- Use VTT for playback and compact segment JSON for UI if needed.
- Do not fetch word-level JSON until a feature requires it.
- Virtualize very long transcript DOMs.
- Parse transcript data in a Web Worker for multi-hour meetings.
- Use `IntersectionObserver` to render nearby transcript segments only.
- Limit `timeupdate` work and binary-search cue start times.
- Avoid one DOM element per word.
- Preserve a no-JavaScript excerpt and official links.
- Cache transcript responses in the browser's HTTP cache.
- Avoid adding a service worker until there is a clear offline requirement.

### 10.3 Static transcript search

The index must be generated from records/transcript artifacts, not by relying on a crawl of meeting
HTML when full transcripts are stored outside the page.

#### Required scaling spike

Generate realistic transcript corpora and compare:

- Pagefind custom records/Node API.
- A custom compact inverted index.
- MiniSearch per city.
- SQLite WASM as a benchmark, not a presumed choice.
- Browser-native substring search on compressed segment shards for city-scoped searches.

Measure:

- Build time.
- Generated size.
- Compression ratio.
- Initial runtime size.
- Query latency.
- Mobile memory.
- Timestamp/snippet quality.
- Incremental rebuild behavior.
- Cache reuse after deployment.

#### Recommended search architecture

##### Tier 1: directory and metadata search

- Tiny global index.
- City, body, title, date, and tags.
- Loaded on search-page entry.

##### Tier 2: transcript search

- Lazy.
- Partitioned by city, then year if necessary.
- Query only selected cities by default.
- "Search all cities" progressively loads candidate partitions or uses Pagefind fragments.

##### Tier 3: optional advanced filtering

- Topic/tag facets from compact metadata.
- Do not duplicate full transcripts for each facet.

#### Index generation rules

- Build directly from stored transcript segments.
- Store segment-level timestamp and meeting UID.
- Avoid word-level postings unless required.
- Normalize text once at artifact-generation or index time.
- Build incrementally per source.
- Content-address each partition.
- Emit a small manifest mapping city/year to partition URL, digest, byte size, coverage, and index
  version.
- Keep the old manifest active until all new partitions are uploaded.
- Garbage-collect unreferenced partitions after a safety window.

#### Search coverage

- Always expose title/agenda metadata search.
- Display transcript coverage clearly.
- Include available transcripts even below a configured completeness threshold.
- Label incomplete coverage rather than silently omitting it.
- Allow maintainers to set a minimum threshold for making transcript search prominent.

### 10.4 Browser-side monitoring

If privacy-preserving aggregate performance sampling is added, measure:

- Search-manifest load time.
- Partition load time.
- Query latency.
- Result count.
- JavaScript errors.
- Approximate device class.

Do not collect query text by default; civic-search queries can be sensitive.

---

## 11. Storage and CDN review

### 11.1 B2/R2 request efficiency

Review:

- List operations per run.
- HEAD operations per artifact.
- Small-object count.
- State-object size distribution.
- CDN hit ratio.
- Cache-control headers.
- Upload checksum behavior.
- Lifecycle rules.
- Multipart thresholds.

### 11.2 Artifact existence

Use:

- A per-source artifact inventory in the catalog manifest.
- Prefix listing only when reconciling.
- Direct object lookup only for exceptional verification.
- Content-addressed keys as the first-level existence guarantee.

### 11.3 CDN headers

Immutable objects should receive:

```http
Cache-Control: public, max-age=31536000, immutable
```

Mutable manifests should receive:

```http
Cache-Control: no-cache
ETag: ...
```

For pages and feeds:

- Use short or revalidation-based caching.
- Avoid excessive RSS staleness.
- Consider `stale-if-error`.

### 11.4 Lifecycle and backup

- Keep final audio/transcript artifacts according to project retention policy.
- Apply short lifecycle rules to temporary uploads and unreferenced source cache.
- Maintain delayed orphan collection.
- Export or replicate critical state periodically.
- Test restore from bucket-only state.
- Test restore from a manifest plus source records without Actions cache.

---

## 12. GitHub Actions cost, limits, and social-good programs

### 12.1 Current cost reality

As of June 17, 2026, GitHub documents standard GitHub-hosted runners as free for public repositories,
while larger runners are billed. GitHub-hosted jobs have a six-hour execution limit, and plan-dependent
concurrency limits should be verified before relying on a precise ceiling.

Therefore, minimizing GitHub runner cost means:

1. Keep the repository public and use standard runners.
2. Minimize waste and operational load even if direct minute charges are zero.

"Free" should not be interpreted as an invitation to use Actions as an unbounded general-purpose batch
platform.

Current references:

- [Choosing the runner for a job](https://docs.github.com/actions/using-jobs/choosing-the-runner-for-a-job)
- [GitHub Actions billing and usage](https://docs.github.com/en/actions/concepts/billing-and-usage)
- [GitHub Actions limits](https://docs.github.com/en/actions/reference/limits)
- [Actions runner pricing](https://docs.github.com/en/billing/reference/actions-runner-pricing)

These links and terms must be rechecked when this plan is executed.

### 12.2 Do not use larger runners as the scaling strategy

Larger runners:

- Are billed for public repositories.
- Do not solve provider throttling.
- Do not eliminate state-transfer amplification.
- Can make per-job failure more expensive.
- May improve individual encode throughput, but scaling out demand-driven standard runners is likely
  cheaper.

Use a larger runner only if measurements show a single non-divisible task requires more RAM/CPU and the
cost is preferable to moving that task elsewhere.

### 12.3 Social-good and nonprofit opportunities

GitHub advertises nonprofit programs with premium developer tools, cloud credits, and partner
offerings. Eligibility and actual Actions benefits require application and confirmation; do not assume
that a nonprofit program grants additional public-repository Actions capacity.

Relevant page:

- [GitHub for Nonprofits](https://github.com/solutions/industry/nonprofits)

Recommended action:

1. Determine whether the project can operate through or partner with a qualifying nonprofit.
2. Apply for relevant nonprofit/social-impact programs.
3. Ask GitHub Social Impact or Support specifically about:
   - Actions concurrency accommodations;
   - credits for larger runners;
   - Azure/cloud credits;
   - security tooling;
   - sponsorship or digital-public-good programs.
4. Document the project as open-source civic infrastructure, public-interest government
   transparency, accessible civic information, and a potential digital public good.

Do not make scaling contingent on receiving a program benefit. Treat credits as acceleration, not
architecture.

### 12.4 Other funding paths

Research these again when execution begins because program terms change:

- GitHub Sponsors.
- Fiscal sponsorship through a civic-tech nonprofit.
- Cloudflare Project Galileo eligibility.
- AWS, Azure, and GCP nonprofit or open-source credits.
- Civic-tech and local-news grants.
- Partnerships with universities, libraries, public-interest technology groups, or municipal-league
  organizations.

---

## 13. Capacity model for 500 cities

### 13.1 Model by sources and meeting inflow, not city count

Define:

```text
C = cities
F = feeds
S = unique provider sources
M = new meetings/day
H = new source-media hours/day
A = average audio processing ratio
P = provider polling requests/day
R = render-dirty partitions/day
T = transcript text generated/day
```

For 500 cities, plausible scenarios are:

| Scenario | Meetings/city/week | New meetings/day |
|---|---:|---:|
| Low | 2 | 143 |
| Moderate | 5 | 357 |
| High | 10 | 714 |
| Very high | 20 | 1,429 |

At two hours per meeting:

| Scenario | New media hours/day |
|---|---:|
| Low | 286 |
| Moderate | 714 |
| High | 1,428 |
| Very high | 2,858 |

The review must derive separate throughput ratios by recipe:

- Direct AAC copy.
- AAC transcode.
- Loudness normalization.
- Silence trim.
- Concatenation.
- Source resolution/download.
- Source-cache hit.

### 13.2 Standard-runner capacity

Four audio shards running 204 useful minutes every four hours provide:

```text
4 shards * 6 cycles/day * 204 minutes
= 4,896 runner-minutes/day
= 81.6 runner-hours/day
```

After optimizing setup, state transfer, scheduling, and source reuse, suppose an average new meeting
requires:

| Audio path | Runner time/meeting |
|---|---:|
| Cheap copy/cache hit | 2 to 5 min |
| Normal encode | 8 to 15 min |
| Heavy loudnorm/trim | 15 to 30 min |
| Difficult provider/failure | 30+ min |

At a blended 12 minutes per meeting, 81.6 runner-hours/day supports roughly:

```text
4,896 / 12 = 408 meetings/day
```

At 8 minutes, capacity is approximately 612/day. At 20 minutes, capacity is approximately 245/day.

This suggests the existing four-shard envelope could support:

- 500 cities under low-to-moderate inflow, after optimization.
- It could struggle with an average of ten or more new meetings per city per week if most require
  heavy re-encoding.
- Dynamic use of more standard shards can raise throughput, but provider caps may bind before GitHub
  concurrency.

### 13.3 What GPU offload changes

With transcription and diarization off the runner:

- ASR jobs become small dispatch/reconcile jobs.
- Hosted runners stop spending hours on model inference.
- State and provider refresh become relatively more visible.
- Audio processing becomes the primary runner workload.
- Search-index generation becomes the next potentially large CPU/data task, but it is partitionable
  and incremental.

### 13.4 Storage estimate

A two-hour 96 kbps mono AAC episode is approximately:

```text
96,000 bits/s / 8 * 7,200 s = 86.4 MB
```

At 357 new meetings/day:

```text
approximately 30.8 GB/day
approximately 11.2 TB/year
```

At the projection's B2 rate of $0.006/GB-month, one retained year at that inflow eventually represents
roughly $67/month before considering free allowance and accumulation patterns.

At 500 cities, storage retention may become a more material cash cost than public standard Actions
minutes. Search and transcript JSON are comparatively small; source-media cache retention must be
carefully bounded.

---

## 14. When to move beyond GitHub-hosted runners

### 14.1 Migration should be based on triggers, not a city number

Start implementing an alternative worker path when any two of these remain true for four weeks:

1. Audio backlog p95 age exceeds 24 hours.
2. More than 25% of scheduled heavy jobs run near the six-hour cap.
3. More than 10 to 12 standard runners are continuously required.
4. Provider throttling persists even under adaptive caps.
5. Providers block or degrade GitHub-hosted runner IPs.
6. Workflow coordination becomes more complex than the media pipeline.
7. Source-media cache locality would save substantial transfer but is impossible across ephemeral
   runners.
8. Operator intervention is needed weekly.
9. Actions API/concurrency queues materially delay work.
10. Terms or policy concerns arise around continuous batch processing.

### 14.2 Estimated practical threshold

#### Comfortable Actions range: 500 to 1,000 cities

Conditions:

- Moderate meeting inflow.
- Incremental provider refresh.
- Targeted state sync.
- Dynamic shards.
- Cross-run source cache for high-value cases.
- Search partitioning.
- Provider-safe concurrency.

#### Transitional range: 1,000 to 2,000 cities

Actions can likely still process the work, but a persistent worker begins to offer:

- Stable egress identity.
- Better local source cache.
- No repeated environment setup.
- Simpler queue semantics.
- Longer task windows.
- Better provider affinity.
- More predictable throughput.

#### Beyond approximately 2,000 cities

A dedicated scheduler plus persistent media workers generally makes more sense, even if Actions
technically keeps up.

GitHub Actions should remain for:

- CI.
- Deployment.
- Scheduled orchestration.
- Audits.
- Recovery operations.
- Lightweight reconciliation.

Heavy media fetching/encoding should move to:

- A small autoscaled VM fleet.
- A persistent self-hosted runner pool.
- Batch/container workers.
- A serverless job platform suitable for long media tasks.

#### Best estimate

> Plan for 500 cities entirely on optimized standard GitHub-hosted runners. Begin migration
> engineering at approximately 1,000 cities. Expect the economically and operationally sensible
> crossover around 1,500 to 2,500 cities.

A favorable catalog may reach 3,000 to 4,000 cities, but that should be viewed as a stress ceiling,
not a recommended operating target.

---

## 15. Proposed execution sequence

### Review PR 1: Instrumentation and benchmark harness

Deliver:

- Network/storage metrics.
- Synthetic 10/100/500-city benchmark.
- Run-efficiency report.
- No behavioral changes.

### Review PR 2: Provider-refresh inventory

Deliver:

- Exact call graph per lane.
- Request-count tests.
- Documentation of redundant provider calls.
- Proposed refresh-state schema.

### Review PR 3: Refresh-only orchestration

Deliver:

- Persisted source-refresh metadata.
- Conditional fetch/content digests.
- Audio/ASR lanes running from records only.
- Adaptive polling behind conservative bounds.

### Review PR 4: State manifest and targeted pull

Deliver:

- Root catalog manifest.
- Shard-specific state fetch.
- Dirty-only upload.
- Migration and rollback path.

### Review PR 5: Demand-driven planner

Deliver:

- Dynamic matrix.
- Zero-job behavior for empty backlogs.
- Provider-aware bin packing.
- Setup/useful-time metrics.

### Review PR 6: Cross-run media-cache experiment

Deliver:

- One provider or failure class only.
- Retention and cost model.
- Cache-hit/savings dashboard.
- Automatic eviction.

### Review PR 7: Adaptive controls

Deliver:

- Adaptive poll intervals.
- Additive-increase/multiplicative-decrease provider concurrency.
- Adaptive shard duration.
- Safety ceilings.
- Canary deployment.

### Review PR 8: Client/search scaling spike

Deliver:

- Generated-corpus benchmark.
- Pagefind/MiniSearch/custom comparison.
- Mobile memory and transfer measurements.
- Chosen partitioning design.
- Explicit launch budgets.

### Review PR 9: Incremental meeting pages and search

Deliver:

- Dirty-partition rendering.
- Lazy transcript loading.
- Incremental, content-addressed search partitions.
- Last-known-good index fallback.

### Review PR 10: 500-city load rehearsal

Deliver:

- End-to-end synthetic test.
- 7-to-14-day shadow scheduling simulation.
- Capacity report.
- Provider-safety report.
- Go/no-go checklist.
- Updated migration threshold.

---

## 16. Review artifacts to commit during execution

Suggested artifacts:

```text
review/NN-efficiency-and-500-city-scaling.md
review/NN-search-scale-spike.md
benchmarks/scaling/...
citypods/metrics.py
citypods/ops/planner.py
citypods/ops/refresh.py
tests/test_scaling.py
tests/test_network_budget.py
```

Documentation updates should include:

- `review/11-technical-design-roadmap.md`
- A new L2/L3 breakout.
- `ROADMAP.md` if this is promoted into the active phase.
- `ARCHITECTURE.md` as each architectural change ships.
- `CHANGELOG.md` for implementation PRs.
- Frozen breakout stamps when completed.

This plan does not itself promote deferred off-Actions media work into the active phase. If execution
would reorder the canonical roadmap or promote deferred work, follow the repository's explicit
deviation process: surface the deviation, explain its trade-offs, obtain maintainer confirmation, and
record the rationale in the relevant canonical document.

---

## 17. Questions the executed review must answer

1. How many unique provider sources exist per city and per feed?
2. How many provider-list requests does one complete schedule cycle make today?
3. Which lanes call providers despite having sufficient persisted state?
4. How many bucket objects and bytes does each shard read?
5. What percentage of state downloads are irrelevant to that shard?
6. How many unchanged files are uploaded?
7. What is the source-cache hit rate within a run?
8. How often is provider media downloaded again on a later run?
9. What is audio runner time per source-media hour by recipe/provider?
10. How many jobs perform no useful artifact work?
11. Which provider, tenant, or CDN limits aggregate concurrency?
12. At what concurrency does each provider's success rate degrade?
13. What is the average daily meeting inflow per city, source, and feed?
14. What is the catalog's p95 meeting duration?
15. How large will one year of retained audio be at 500 cities?
16. How large are transcript search indexes per transcript hour?
17. What is the mobile-memory cost of city-wide and global search?
18. At what point does a persistent cache save more than it costs?
19. At what point does stable egress matter more than free runner capacity?
20. What measured threshold should automatically recommend off-Actions media workers?

---

## 18. Final review deliverables

Execution of this plan should end with:

1. A measured current-state network and runner-cost baseline.
2. A reproducible 500-city simulation.
3. Per-provider concurrency envelopes.
4. A source-refresh and state-manifest technical design.
5. An incremental search-size and browser-performance report.
6. A prioritized implementation backlog with estimated impact and risk.
7. A 500-city readiness checklist.
8. A measured GitHub Actions ceiling and an off-Actions migration trigger.
9. Updated architecture, roadmap, design, and changelog documentation as implementation ships.
10. A decision on whether the next scaling investment should be provider polling, state transfer,
    source-media caching, search partitioning, or external media workers.
