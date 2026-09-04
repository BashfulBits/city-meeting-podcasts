# review/45 — System Architecture Evolution & Refactoring Plan

**Maturity: L2 program design · authored 2026-09-04 · reviewed and hardened 2026-09-04**

Owner: maintainers & agents. Scope: comprehensive system architecture evolution across
throughput, reliability, observability, LLM job admission/refinement, and structural
codebase refactoring.

> **Implementation status.** This is an umbrella decision record, not one independently
> implementable feature. It is deliberately **L2**: its workstreams have different risk,
> ownership, and rollout requirements. No item below is authorized as an L3 implementation merely
> because this document exists. Before coding, each selected workstream must have its own L3
> issue/design slice with the exact data contract, affected call sites, migration/backfill and
> rollback story, observability, and acceptance tests. That prevents a broad audit from silently
> overriding the already-committed designs in review/18, review/22, review/26, review/34, and
> review/44.

---

## §1. Executive Context, Operational Baseline & Motivation

This document establishes the canonical forward architectural and refactoring blueprint for the
`city-meeting-podcasts` repository. Over successive feature phases (Phase H, Phase R, R10 LLM
dispatch v2, R5/R6/R7 enrichment), the codebase expanded to ~65,000 lines of Python and TypeScript.
While individual stages operate effectively, the architecture accumulated critical structural
coupling points, performance bottlenecks, and operational debt:

1. **State Store Concurrency Hazard (TOCTOU)**: Backblaze B2 lacks conditional updates (`If-Match`
   or compare-and-swap). Concurrent GitHub Actions lane workflows (`audio`, `asr`, `tag`,
   `chapter-locator`, `moments`) download, mutate, and re-upload the same monolithic
   `state/sources/<source>/episodes.json` file. Despite `records.merge_preserving_foreign()`,
   concurrent writes risk silent data loss.
2. **Serial & Unfiltered Checkpoint I/O (GH #1458)**: `push_records_merged` processes owned sources
   serially ($N=1$) and re-encodes/re-uploads untouched sources at every checkpoint, causing
   severe wall-clock inflation and runner timeouts in `tag.yml` and other long-running workflows.
3. **Workflow Overrun Reporting Blindspots (GH #1459)**: Long-running workflow jobs lacking
   step-level timeouts get terminated by GitHub's job-level timeout, rendering grey (cancelled)
   and skipping vital trailing reporting steps (e.g., diarization projection, sweep summaries).
4. **Dual-Stack LLM Dispatch Debt**: The system maintains legacy v1 (`workers/llm-dispatch-proxy`,
   R2-backed single-job ingress) alongside v2 (`workers/llm-dispatch-v2`, Durable Object SQLite
   batching), imposing dual-transport logic and complex branching in `citypods/compute/llm.py`.
   PR #1457 established `llm_lanes` as the single source of truth for purpose routing and write
   reservations. The repository still contains v1 producer configuration, recovery/reconciliation
   tooling, worker deployment/test paths, and a dual-output limits compiler; retirement requires
   evidence, not an assumption that its backlog is empty.
5. **Durable Object Quota Exhaustion**: The Cloudflare Workers free plan enforces a 5M daily
   Durable Object row-read ceiling. On 2026-08-27, unindexed bookkeeping scans exhausted this quota,
   halting inference. Pre-breach alerting and structured telemetry are essential.
6. **Core Module Monoliths**: Four files contain ~18,600 lines of code: `stages.py` (7,667 LOC),
   `run.py` (4,208 LOC), `compute/llm.py` (3,040 LOC), and `media.py` (3,691 LOC). This creates
   severe cognitive load, complex merge conflicts, and elevated regression risks.

---

## §2. Candidate State-Store Partitioning: Required Design Gates & Cutover Runbook

### Rationale for Option B (Clean Cutover)

The current `episodes.json` merge protocol in `citypods.records` / `citypods.statesync` is a
documented, conservative mitigation for overlapping lane writes; it is not evidence that four
coarse sidecars are automatically safe. In particular, `enrichment.json` would still have several
writers (`tag`, `moments`, chapter-agenda, chapter-locator, and diarization-related projections),
and catalog refresh, planning fields, and audio work do not have identical ownership. A partition
only eliminates a lost update when **exactly one concurrent writer owns each mutable object**.

The prospective L3 design must therefore define a versioned, per-source manifest plus an exact
object ownership table. A viable shape is a generation namespace such as
`state/generations/<generation>/sources/<source>/` with a small, last-written active-generation
pointer. Each sidecar envelope must carry `schema_version`, `source_key`, `generation`, an
episode mapping, and a digest. The design must assign every existing record field, including
`links`, `stage_completion`, planning fields, append-only calendar rows, integrity results,
private review/evaluation state, and all artifact references. It must either give each such object
a unique lane owner (for example distinct `tags.json`, `moments.json`, and chapter sidecars), or
retain the current merge/lease discipline for the shared object. “enrichment” is not a
sufficient
ownership boundary by itself.

The reader must assemble an in-memory `Episode` only after it verifies that every required
sidecar belongs to the same active generation. A missing, corrupt, duplicate, or mixed-generation
sidecar must fail closed for writes and report the source; it must never synthesize an empty block
that overwrites durable data. Keep source identity, stable episode UID, append-only merge behavior,
split `audio_spec_hash` / `feed_content_hash`, and the stage-before-audio ordering invariant
unchanged. The migration must not turn historical retention, tombstones, or B2 version lifecycle
into a mass deletion of `episodes.json` objects.

There is no permanent dual-read production path in this option, but an operational rollback is
still mandatory. The migration publishes a complete immutable generation, verifies it by fresh
download, and flips the active pointer only as its final write while all writers are quiesced.
The prior generation remains readable and protected from lifecycle deletion through the agreed
rollback window. Rollback is a quiesced pointer flip, followed by a fresh-read canary; it is not a
best-effort reversal of individual sidecars.

### Preconditions and operational runbook

The script and the partition-aware reader/writer must be merged first but remain inactive behind
an explicit state-generation configuration. A cutover operator must record the exact main SHA,
generation ID, object count/digests, and approver in the run summary. Verify that repository-level
Actions administration is available before beginning; do not discover that after cancelling work.

1. Run the migration in read-only mode against a fresh state pull. It must report every source,
   sidecar count, retained UID count, canonical artifact-key set, and semantic round-trip diff.
   Any mismatch is a stop condition.
2. Announce the maintenance window, disable all writers, and cancel **all pages** of queued and
   in-progress runs. Re-list until two consecutive polls are empty. Also account for external
   workers and manually dispatched maintenance workflows; cancelling Actions alone does not fence
   a process that already has B2 credentials.
3. Take an immutable legacy snapshot/generation manifest and verify it from a separate clean
   checkout. Do not rely on an Actions cache as a backup.
4. Publish the candidate generation without changing the pointer; list and fresh-read every
   object, validate schemas/digests, and reassemble every source into the same logical records.
5. Flip the pointer, then run a read-only render canary and one writer canary for a source that
   exercises audio, transcript, links, and enrichment ownership. Verify the written generation
   and the legacy generation independently.
6. Re-enable scheduled writers only after the canary has completed. Keep the old generation and
   a rollback command for the documented retention window; publish the final migration evidence.

The following commands are illustrative operator aids, not a complete or self-authorizing
procedure. The real L3 slice must supply pagination-safe commands and a `--dry-run` default.

To prevent in-flight runner jobs from writing legacy state during the migration, follow this
exact suspension procedure:

```bash
# ==============================================================================
# STEP 1: Cancel all in-flight and queued workflow runs (repeat with pagination as needed)
# ==============================================================================
gh run list --status in_progress --json databaseId -q '.[].databaseId' \
  | xargs -I {} gh run cancel {}
gh run list --status queued --json databaseId -q '.[].databaseId' \
  | xargs -I {} gh run cancel {}

# ==============================================================================
# STEP 2: Suspend GitHub Actions repository-wide
# Freezes all scheduled crons, push triggers, and repository dispatch events.
# ==============================================================================
gh api -X PUT /repos/BashfulBits/city-meeting-podcasts/actions/permissions \
  -F enabled=false

# ==============================================================================
# STEP 3: Verify quiet state (0 active runs)
# ==============================================================================
gh run list --status in_progress
gh run list --status queued
# Confirm both return empty output before proceeding.

# ==============================================================================
# STEP 4: Publish and verify a candidate generation; this does NOT flip the active pointer.
# ==============================================================================
python -m scripts.migrate_state_partitioning --generation "<timestamp-or-uuid>" --execute --verify

# ==============================================================================
# STEP 5: Activate only after the evidence review described above.
# ==============================================================================
python -m scripts.migrate_state_partitioning --generation "<timestamp-or-uuid>" --activate

# ==============================================================================
# STEP 6: Re-enable GitHub Actions repository-wide
# ==============================================================================
gh api -X PUT /repos/BashfulBits/city-meeting-podcasts/actions/permissions \
  -F enabled=true

# ==============================================================================
# STEP 7: Dispatch read and write canaries, then inspect generation/digest evidence
# ==============================================================================
gh workflow run tag.yml -f city=arlington-tx
```

---

## §3. Candidate v1 LLM Dispatch Retirement: Evidence Gates

v1 retirement is a migration of data, producer configuration, generated route metadata, and a
deployed Worker—not a source deletion. The L3 issue must name the surviving v2 equivalents for
every v1 capability before removing the proxy.

1. **Prove quiescence.** Inventory every `LLM_DISPATCH_URL` / token consumer, queue-only path,
   scheduled/manual workflow, R2 prefix, recovery importer, requeue script, test, deployment
   workflow, and limits-compiler output. Disable *new v1 production* first with a configuration
   validation that fails closed; retain a read-only diagnostic while the drain runs.
2. **Drain and preserve.** Run the existing recovery/reconciliation tools in dry-run and apply
   modes, with immutable counts and sampled schema-valid result verification. Keep the R2 source
   records and the read-only recovery path through an explicitly approved retention window. A zero
   queue count at one instant is insufficient: prove no producer has written after the cutover and
   no owned terminal result remains unimported.
3. **Prove v2 parity.** Contract-test ingress, idempotency, polling, schema correction, retry and
   terminal reconciliation for every active purpose/route. Verify dashboard/sweep observability
   and error semantics from a clean workflow environment containing only v2 credentials.
4. **Remove in a coherent series.** Move shared route/credential logic out of the v1 directory
   before deleting it; update `scripts/compile_llm_limits.py`, `scripts/pre-push.sh`, CI, deploy
   workflows, recovery/requeue/report scripts, Python configuration and tests together. Remove
   old GitHub secrets only after the code/config scan and a v2-only canary pass.
5. **Decommission last.** `git rm` does not remove the deployed Worker or R2 data. After the
   retention window and an explicit maintainer approval, disable/delete the deployed Worker using
   Cloudflare's supported operation, record the immutable export/retention disposition, and prove
   that an attempted v1 request cannot be accepted. This is a destructive operational action and
   is outside an ordinary code PR.

---

## §4. The 5 Core Architectural Pillars & 18 Candidate Initiatives

The entries below are candidate workstreams, not a promise to implement all 18. Each one needs a
measured trigger and a narrow L3 breakout before entering the active queue. Existing accepted
designs take precedence where they overlap.

### Pillar 1: Throughput & Work Distribution

#### Initiative 1: Shard-Scoped State Synchronization (Sparse Fetching)
- **Problem**: Matrix shard runners download all ~3,500 state files across all 85 catalog feeds,
  even when processing only a single city or source shard.
- **Design gate**: Extend `citypods.statesync.pull_state()` rather than inventing a second state
  loader. The matrix planner must first emit an explicit manifest-derived source-to-object map;
  `pull_state(only_paths=...)` then restores that closed set plus every required global control
  file. Never infer ownership from a prefix listing at worker time, and never permit a sparse
  worker to write a file it did not restore/own. Compare cold and warm-cache transfer bytes,
  object count, and startup time before claiming a percentage improvement.
- **Impact**: Cuts runner startup state-sync latency by 75–85% (~45–90s saved per matrix job).

#### Initiative 2: In-Process Media Probing Cache & FFmpeg Subprocess Pooling
- **Problem**: A single meeting audio file is independently probed up to four times via `ffprobe`
  during a pipeline execution across `AudioStage`, `SilenceTrimmerStage`, and `ASRStage`.
- **Design gate**: Implement a process-local, bounded cache in `citypods.media` keyed by canonical
  local path plus file identity (`mtime_ns`, size, and, where available, content digest). Cache
  only successful immutable probe results and invalidate on replace/delete. Do not persist
  transient probe data into episode records: persisted duration/evidence already has the
  audio-owned contract in review/26, and a probe-cache entry is neither canonical metadata nor a
  pipeline-versioned artifact.
- **Impact**: Eliminates 300–800ms of process-forking overhead per episode; accelerates local
  and CI builds by 2–4 minutes.

#### Initiative 3: Granular S3/B2 Range-Header Audio Slicing
- **Problem**: Verification stages download entire multi-gigabyte audio files to inspect audio
  continuity, headers, or silence baselines.
- **Design gate**: First classify which verifier decisions are valid from a head range, tail range,
  or a seekable local artifact. Container metadata and a leading sample cannot establish duration,
  continuity, or trailing silence for every codec. Add capability detection (206 + valid
  `Content-Range`), a strict full-fetch fallback, byte caps, redirect/SSRF validation per request,
  and fixtures for ignored/malformed ranges and HLS/MP4 edge cases before changing correctness
  paths.
- **Impact**: Reduces network transfer for verification stages by up to 95%.

#### Initiative 4: Parallelized & Dirty-Skipping State Push (GH #1458)
- **Problem**: `push_records_merged` serially iterates through owned sources and unconditionally
  re-reads, re-merges, re-encodes, and re-uploads untouched records at every checkpoint.
- **Design gate**: `push_state()` is already parallelized; the remaining hot path is the serial
  fetch/merge/put in `citypods.statesync.push_records_merged()`. Select dirty *source record
  files* before any remote read, preserve the existing fail-safe “unreadable remote means no
  write” rule, and parallelize only distinct source keys. Do not share a mutable local record map,
  clear a dirty entry before its PUT and manifest update both succeed, or make the worker count a
  hard-coded 16: make it storage-connection-pool aware, configurable, and measured. Tests need
  interleaved same-source/cross-source writes, one failed PUT, deterministic logging, and a
  retry after a cancelled checkpoint.
- **Impact**: Drops checkpoint latency from 15–30s down to <2s; resolves timeout cancellation in
  `tag.yml` and long-running enrichment workflows.

---

### Pillar 2: Reliability, Idempotency & State Consistency

#### Initiative 5: Partitioned Lane State Store (`catalog.json`, `audio.json`, etc.)
- **Problem**: B2 lacks CAS. Multi-lane CI workflows running concurrently clobber each other's
  mutations in monolithic `episodes.json`.
- **Design**: Enforce isolated single-writer sidecar files partitioned by pipeline domain:
  `catalog.json`, `audio.json`, `transcripts.json`, and `enrichment.json`.
- **Impact condition**: Eliminates cross-lane clobbers only for objects with a verified exclusive
  writer. Shared objects retain their merge/lease protocol until split further.

#### Initiative 6: Step-Level Timeouts & Budget Stop Alignment (GH #1459)
- **Problem**: Overrunning jobs hit GitHub's job-level timeout, cancelling the runner and skipping
  crucial trailing reporting steps.
- **Design gate**: Inventory every long-running *work* step first, including `tag.yml`,
  `moments.yml` and the chapter lanes, rather than applying a generic percentage. Anchor the
  application
  deadline at job start (so restore/install time consumes runway), reserve an explicit durable
  checkpoint/report tail, and make timeout/defer/interrupt outcomes distinguishable. A shell
  timeout alone is not graceful: it must leave enough time for the process's stop predicate and
  existing SIGTERM checkpoint path. Test timeout values, deadline propagation, a deferred exit
  with trailing summary, and a real failure that remains red.
- **Impact**: Prevents grey cancelled runs; guarantees trailing reporting steps execute and fail
  visibly as red on true timeouts.

#### Initiative 7: Hardened SSRF Source URL Validator (DNS Pinning & CIDR Checks)
- **Problem**: Time-of-check to time-of-use (TOCTOU) DNS rebinding vulnerability in
  `validate_source_url`: domain is validated, but subsequent `requests.get()` re-resolves DNS.
- **Design gate**: This is a security-sensitive research spike, not an implementation instruction.
  A naïve IP URL plus `Host` header breaks HTTPS SNI/certificate verification, proxy behavior,
  redirects, connection reuse, and the ffmpeg paths that do not use `requests`. Produce a threat
  model and a transport design that pins the connection while retaining hostname SNI and
  certificate validation, or document a trusted proxy boundary for transports that cannot do so.
  Audit every outbound path (`citypods.http`, provider adapters, document/media fetches, urllib,
  and subprocesses). Test IPv4/IPv6, every DNS answer, rebinding between validation and connect,
  redirects, proxy environment variables, and a safe failure. Do not claim complete mitigation
  before that design is independently reviewed.

#### Initiative 8: Formal `BaseStage` Protocol Contract
- **Problem**: Stages across `citypods/stages.py` have divergent signatures, inconsistent return
  types, and ad-hoc wall-clock stop budget handling.
- **Design**: Establish a runtime-checkable `typing.Protocol` in `citypods/pipeline/contract.py`:
  `execute(ctx: StageContext) -> StageResult` and `should_run(ctx: StageContext) -> bool`.
- **Impact**: Eliminates duck-typing bugs and standardizes wall-clock stop budget enforcement.

---

### Pillar 3: Observability & Operational Telemetry

#### Initiative 9: Structured Event Telemetry & Durable Object Quota Alarms
- **Problem**: Unstructured `print()` logging prevents programmatic log ingestion. Cloudflare
  Durable Object 5M row-read exhaustion hit without pre-breach warning.
- **Design gate**: Preserve the existing stable, human-readable workflow summaries while introducing
  a small event schema (`event`, UTC timestamp, run/workflow id, lane, source count, duration,
  outcome, bounded error class). Never log prompts, transcript text, tokens, URLs with credentials,
  or raw provider responses. Define the authoritative quota source, polling cadence, reset
  timezone, uncertainty behavior, idempotency key/deduplication, and failure path for the alert
  before choosing Discord or issue delivery. The alert must itself be bounded and cannot add an
  unindexed DO scan.
- **Impact**: Real-time visibility into quota consumption and immediate alert on runaway queries.

#### Initiative 10: OpenTelemetry Tracing for Multi-Stage Runner Spans
- **Problem**: Profiling stage duration across distributed runners requires manual log timestamp
  correlation.
- **Design gate**: Decide exporter, sampling, retention/cost budget, secret configuration, and a
  no-export default before adding a dependency. Span attributes must use source hashes/counts, not
  transcript or prompt content. Local tests should assert no network/exporter activity by default.
- **Impact**: Delivers instant visual waterfall traces of feed processing latency.

---

### Pillar 4: LLM Job Admission, Routing & Refinement

#### Initiative 11: Sunset v1 Proxy & Consolidate on v2 with `llm_lanes`
- **Problem**: Dual-stack transport maintenance and bifurcated code paths.
- **Design**: Decommission `workers/llm-dispatch-proxy/`, standardize on `workers/llm-dispatch-v2/`,
  and drive all admission from the unified `llm_lanes` configuration introduced in PR #1457.
- **Impact**: Cuts ~800 lines of legacy code and unifies provider routing logic.

#### Initiative 12: Unified Structured Output via LiteLLM Native JSON Schema
- **Problem**: Instructor 1.15.4's lack of native Gemini JSON Schema support forced a hand-rolled
  shim (`_run_native_structured_direct()`) in `compute/llm.py`.
- **Design gate**: A dependency upgrade does not prove uniform provider semantics. Build a route ×
  model × schema capability matrix from pinned versions and live contract probes, then retain the
  current local Pydantic validation and schema-retry behavior as the correctness authority. Only
  remove a shim when every enabled route has equivalent request/response, refusal, retry, and
  usage-accounting behavior. Follow review/22: pin/recompile constraints and state whether a
  prompt/response recipe change triggers gradual invalidation or leaves historical artifacts.

#### Initiative 13: Model-Specific Prefix Prompt Caching
- **Problem**: Dynamic variables placed early in prompt templates bust provider prefix caches.
- **Design gate**: Treat caching as route-specific and evidence-driven; do not pad prompts to
  assumed provider boundaries or promise a universal saving. Preserve message roles and semantic
  ordering, measure cache-hit/creation tokens and latency per route, and make the prompt/recipe
  fingerprint explicit. Any output-affecting prompt change needs golden/tournament comparison and
  the same backfill statement required for a pipeline-version change.

#### Initiative 14: Dynamic Budget Allocation & Priority Queuing on `llm_lanes`
- **Problem**: High-volume backfills saturate worker admission, starving daily active feeds.
- **Design gate**: v2 already persists indexed priority `0/1` and orders claims by it. Define the
  producer mapping from a verifiable publication deadline to the existing values, authorization
  against priority inflation, an aging/starvation rule for backfills, per-lane admission caps, and
  metrics for queue age by priority. “Zero delay” is not a valid promise when a route/provider
  is unavailable.

#### Initiative 15: Calibrated Confidence Estimation & Deterministic Fallbacks
- **Problem**: Occasional LLM hallucination during low-audio-quality segments produces invalid
  chapter timestamps or inaccurate agenda links.
- **Design gate**: Reuse the existing evidence, candidate, and human-review contracts rather than
  trusting a model-reported or ad-hoc heuristic score. Define feature-specific precision/recall
  thresholds, deterministic validator inputs, what “no result” means, and an audit record for a
  rejected candidate. A regex fallback may be less correct than withholding a chapter/link, so it
  must be evaluated per feature and must never replace official title, date, URL, or transcript
  text. This belongs in the relevant review/35 or generated-chapter follow-up, not a generic
  cross-feature stage.

#### Initiative 16: Autonomous Evaluator Tournament Engine
- **Problem**: Prompt revisions and model upgrades currently rely on subjective manual checks.
- **Status**: The reusable tournament and merge-gated champion-routing work is already designed and
  implemented locally in review/34. This program must integrate with that contract rather than
  create a parallel `scripts/eval_prompt_tournament.py`; any gap must be filed as a narrow review/34
  follow-up with a fixed corpus version, judge version, score thresholds, cost ceiling, and a
  no-auto-production-route rule.

---

### Pillar 5: Architectural Consistency & De-Monolithization

#### Initiative 17: Modularize 4 Monolith Modules (<1,000 LOC per File)
- **Problem**: `stages.py` (7.6k LOC), `run.py` (4.2k LOC), `compute/llm.py` (3.0k LOC), and
  `media.py` (3.0k LOC) are oversized monoliths that impede safe refactoring.
- **Design gate**: Do not create `citypods/stages/`, `citypods/media/`, or `citypods/compute/llm/`
  alongside same-named `.py` facades: Python import resolution would shadow the facade and can
  silently break imports. First extract into distinct implementation namespaces (for example
  `citypods/_stage_impl/`, `_media_impl/`, and `compute/_llm_impl/`) while the existing modules
  explicitly re-export the public API. Split one module per behavior-preserving PR, preserve import
  and CLI contracts, and use characterization tests plus import smoke tests. A file-size target is
  a review heuristic, not an acceptance criterion; no behavior change, circular imports, or
  unreviewable four-module move is acceptable.

#### Initiative 18: Duration-field consolidation — superseded
- **Disposition**: Do not schedule this work. H21 is already shipped in review/26: source and served
  duration are intentionally distinct clocks, with canonical `source_duration_seconds` and
  `served_duration_seconds` helpers, audio-owned normalization, compatibility reads, and a manual
  repair action. Collapsing them into one `duration_seconds` field would regress timeline-integrity
  checks and violate that accepted ownership model. Future duration changes belong in review/26.

---

## §5. Promotion Order and L3 Exit Criteria

The former fixed “11 PR / four sprint” schedule mixed discoveries, migrations, already-shipped
work, and independent refactors into unsafe bundles. The ordering below is a dependency order,
not a calendar commitment; each row becomes a separate issue/PR series only after it passes its
L3 gate.

1. **GH#1458 checkpoint profiling and dirty-source push.** Capture a representative trace and
   identify exactly which `push_records_merged` calls are unchanged. Complete only with no
   lost-update regression, a cancelled-PUT retry, and measured cold/warm improvement.
2. **GH#1459 graceful timeout envelope.** Inventory long work steps and reserve a post-work
   persistence/report tail. Complete only when each affected job defers before the hard timeout
   and failure/cancellation classifications remain distinct.
3. **v1 retirement discovery/drain.** Finish §3's consumer, R2, and deployed-Worker inventory.
   A maintainer must accept the drain report and v2-only canary before any production deletion.
4. **State partitioning design/migration rehearsal.** Complete §2's field/owner matrix,
   generation protocol, offline round trip, and rollback drill. A separate maintainer-approved
   maintenance window—not a normal PR—activates a verified generation.
5. **Sparse state pull.** Require a manifest-derived source/object map. Sparse and full reads
   must assemble identical logical records, and unowned paths cannot be pushed.
6. **Observability/DO quota alert.** Select an authoritative quota signal and alert destination
   with cost/privacy review. Bounded telemetry must pass rows-read scaling tests, and a synthetic
   threshold must emit one deduplicated alert.
7. **Provider/schema and prompt-cache probes.** Require a pinned route capability matrix and
   baseline measurements. Route-specific contracts/evaluations justify every removal or recipe
   fingerprint decision.
8. **One behavior-preserving module extraction.** Identify a leaf seam and characterization
   boundary. Existing imports/CLI/output remain compatible, with no circular import or unrelated
   movement.
9. **Security transport proposal.** The threat model covers every outbound transport, including
   subprocesses. An independent security review accepts a safe transport/fallback before code.

Work that review/26 and review/34 already own stays there. Range slicing, OpenTelemetry, confidence
fallbacks, and a stage protocol remain backlog candidates until their measurement or product trigger
is recorded. None may be bundled merely to fill a sprint.

---

## §6. Testing, Verification & Golden Snapshot Plan

1. **Static Analysis & Linters**:
   - Every PR must pass `ruff check .` and `ruff format --check .` under the
     strict 100-character limit.
   - Enforce type annotations via `mypy` on all newly extracted subpackages.
2. **Offline Test Suite Execution**:
   - Maintain 100% pass rate across the offline test suite (`pytest -q`, 3,730+ tests).
3. **Workflow Timeout Assertions**:
   - `tests/test_workflows.py` asserts step-level timeouts for all long-running workflows to
     prevent regressions (GH #1459).
4. **State migration and concurrent-write tests**:
   - Exercise every field/owner mapping with multi-lane interleavings, a partial upload, corrupt or
     missing sidecar, pointer-flip interruption, rollback, and a fresh-checkout reconstruction.
   - Compare UID set, artifact references, completion state, planning/timeline values, and
     append-only calendar rows—not merely JSON object counts. The migration is idempotent in
     dry-run and publish modes, and does not tombstone legacy objects before approval.
5. **v1 retirement tests**:
   - Run recovery/reconciliation fixtures through the drain report and verify a clean v2-only
     environment has no v1 fallback. Keep a test proving the retired ingress is rejected only
     after the approved operational deletion.
6. **Golden Snapshot Integrity**:
   - First run ordinary `pytest` to detect unintended output changes. Use `SNAPSHOT_UPDATE=1
     pytest` only for an intentional rendered-output change, then review the generated diff and
     state the pipeline-version/backfill disposition.
7. **Durable Object Mutation & Invariant Guards**:
   - Run `test/rows-read.test.js` in `workers/llm-dispatch-v2/` to ensure no database statement
     scans an unbounded or traffic-growable table.
8. **Live/security contract tests**:
   - Gate only the selected route/security changes behind opt-in live tests with no production
     mutation. Record provider, dependency, and baseline versions with the results; mocked tests
     alone cannot establish provider schema or DNS-pinning behavior.

---

## §7. Documentation & Architectural Contract Updates

Per the repository lifecycle contract in `CONTRIBUTING.md` and `review/11`:
- **`review/11-technical-design-roadmap.md`**: Catalog this as an L2 umbrella and point each
  selected L3 issue at its own breakout. Mark superseded overlap with review/26 and review/34.
- **`ROADMAP.md`**: Describe review/45 as a gated architectural program, not an adopted sprint
  schedule or a completed state-store cutover.
- **`ARCHITECTURE.md`**: Describe only the as-built state store and dispatch system. Update it after
  a partitioning or retirement change actually ships; do not document a target design as current.
- **`CHANGELOG.md`**: Record the reviewed planning decision accurately. Add implementation entries
  only as individual PRs merge, including every pipeline-version/backfill disposition.
