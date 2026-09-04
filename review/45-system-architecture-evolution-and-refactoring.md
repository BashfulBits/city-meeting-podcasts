# review/45 — System Architecture Evolution & Refactoring Plan

**Maturity: L3 dev-ready per workstream · authored 2026-09-04 · hardened 2026-09-04 · detailed to
L3 2026-09-04**

Owner: maintainers & agents. Scope: comprehensive system architecture evolution across
throughput, reliability, observability, LLM job admission/refinement, and structural
codebase refactoring.

> **Implementation status.** This is an umbrella document covering 19 initiatives plus the
> state-store partitioning (§2) and v1 retirement (§3) programs; it is not one single feature with
> one rollout. Each workstream below is now individually **L3 dev-ready**: exact file/function
> references, verbatim signatures, concrete algorithms, schemas, and test names, grounded directly
> in the current codebase (not paraphrased or assumed) — sufficient for direct implementation
> without a separate design-discovery pass. This detail is **not** a blanket go-ahead to run every
> destructive or irreversible step unattended. A handful of steps are marked explicitly below as
> requiring a human maintainer's sign-off before executing (the state-generation pointer flip in
> §2, the deployed-Worker deletion and secret removal in §3, and the security-transport design in
> Initiative 7) — an implementing agent, human or automated, must stop and get that sign-off at
> exactly those points and may proceed mechanically everywhere else. Existing accepted designs
> still take precedence where they overlap: review/18, review/22, review/26, review/34, and
> review/44 are the owning documents for the areas they cover, and this document's job there is to
> point at them precisely, not restate or override them. Selecting *which* workstream to pick up
> next, and in what order, is still governed by §5's dependency ordering and each item's measured
> trigger — detail-readiness is not itself a scheduling decision.

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
   tooling, worker deployment/test paths, and a triple-output limits compiler; retirement requires
   evidence, not an assumption that its backlog is empty.
5. **Durable Object Quota Exhaustion**: The Cloudflare Workers free plan enforces a 5M daily
   Durable Object row-read ceiling. On 2026-08-27, unindexed bookkeeping scans exhausted this quota,
   halting inference. Pre-breach alerting and structured telemetry are essential.
6. **Core Module Monoliths**: Four files contain ~18,800 lines of code: `stages.py` (7,667 LOC),
   `run.py` (4,356 LOC), `compute/llm.py` (3,118 LOC), and `media.py` (3,691 LOC). This creates
   severe cognitive load, complex merge conflicts, and elevated regression risks.

---

## §2. State-Store Partitioning — L3 Design, Ownership Table & Cutover Runbook

### Rationale for Option B (Clean Cutover)

The current `episodes.json` merge protocol in `citypods.records` / `citypods.statesync` is a
documented, conservative mitigation for overlapping lane writes; it is not evidence that four
coarse sidecars are automatically safe. In particular, `enrichment.json` would still have several
writers (`tag`, `moments`, chapter-agenda, chapter-locator, and diarization-related projections),
and catalog refresh, planning fields, and audio work do not have identical ownership. A partition
only eliminates a lost update when **exactly one concurrent writer owns each mutable object**.

**The object-ownership table this design needs already exists — reuse it, do not invent one.**
`citypods/records.py` already carries the exact per-lane block-ownership table this partitioning
requires: `ARTIFACT_BLOCKS` (the full set of mutable derived-artifact blocks — `audio`, `transcript`,
`provider_transcript`, `speakers`, `media_availability`, `integrity`, `agenda_text`, `agenda_backup`,
`minutes_text`, `minutes_votes`, `minutes_roster`, `tags`, `chapter_tags`,
`llm_tag_candidates`/`tags_llm_call_attempts`/`tags_llm_recipe_hash`/`tags_spec_hash`/
`tags_input_fingerprint`, `generated_agenda_candidates`, `generated_chapters`,
`generated_chapters_spec_hash`, `moments`), `PLANNING_FIELDS` (handled separately — see below), and
`_LANE_OWNED_BLOCKS: dict[str, frozenset[str]]` mapping each lane (`audio`, `transcribe`, `align`,
`diarize`, `speaker-identity`, `tag`, `moments`, `chapter-agenda`, `chapter-locator`, `chapter`) to
the blocks it owns, exposed via `protected_blocks_for_lane(lane)`. A sidecar-per-domain design is a
re-projection of this existing table onto file boundaries — group each lane's owned blocks into the
sidecar it should live in — **provided the two places this table is not already a clean partition
are resolved first, not discovered mid-migration**:

1. **`moments` is dual-owned today.** Both the `moments` lane and the `speaker-identity` lane list
   `_LANE_OWNED_BLOCKS[...] = {"moments"}`. Under the current `episodes.json` merge this works
   because `merge_preserving_foreign` still applies field/key-level tie-breaking inside one shared
   dict; a single-writer sidecar file has no such fallback. Either split `moments.json`'s fields
   into genuinely disjoint sub-blocks (e.g. `moment_video_clip`/`moments_llm_call_attempts` vs. the
   summary/pullquote/decision candidate lists) with a distinct owner each, or keep `moments.json`
   explicitly multi-writer with the existing merge/lease discipline intact — do not assume the
   current single dict key implies a clean split.
2. **`integrity` has no lane owner at all.** It is in `ARTIFACT_BLOCKS` (an unscoped/full run owns
   it) but appears in **no** `_LANE_OWNED_BLOCKS` entry — every scoped lane today treats it as
   foreign and preserves it from remote unconditionally, because it is written only by
   `citypods/integrity.py:95,131`, entirely outside the lane-scoped merge machinery. The
   partitioned design must name an explicit single writer for `integrity.json` (most likely the
   audio lane, since it inspects audio artifacts) or keep it in a multi-writer shared sidecar.

**Fields and objects outside this table — do not force them into the four sidecars:**
- **`PLANNING_FIELDS`** (`sources`, `timeline`, `source_chapters`, `chapters`, `chapters_basis`)
  are governed by a *rank comparison* (`_preserve_remote_planning_if_better`,
  `citypods/records.py:1909-1957`), not lane ownership — a run only overwrites them when its own
  plan/basis ranks strictly higher than the remote's. This cross-lane arbitration must be preserved
  verbatim inside whichever sidecar hosts these fields (most naturally `catalog.json` or
  `audio.json`); it cannot be replaced by a single-writer assumption.
- **Calendar rows are already partitioned.** `state/sources/<source>/calendar.json` is already a
  separate file with its own schema, its own merge (`merge_calendar_records`,
  `citypods/records.py:749`), and its own push path (`push_calendar_records_merged`,
  `citypods/statesync.py:771`) — entirely outside `episodes.json`. This migration does not touch it.
- **Private review/evaluation state is already partitioned and already flat.**
  `r6_moment_evaluation.json` (`citypods/moment_review.py`) and `llm_tournament.json`/
  `llm_tournament_tickets.json` (`citypods/tournament.py:55-56`) are root-level files, not
  per-source, not part of any `Episode` record. Out of scope.
- **Catalog/scrape metadata has no discrete stage class today.** The primary crawl fields (`guid`,
  `title`, `published`, `body`, `video_url`, `media_kind`) are written by the fetch/crawl loop in
  `run.py`'s main flow and by `assign_uids`/`merge_calendar_backfill`/`attach_auxiliary_agenda_links`
  (`citypods/records.py:303-432`), not by a `Stage` subclass with a lane name. `catalog.json`'s
  ownership rule must be stated in terms of "the crawl/discovery pass," not a
  `_LANE_OWNED_BLOCKS["catalog"]` entry — none exists to copy from.

The prospective design must therefore define a versioned, per-source manifest plus this exact
object ownership table (reusing `_LANE_OWNED_BLOCKS` and closing the two gaps above). A viable
shape is a generation namespace such as `state/generations/<generation>/sources/<source>/` with a
small, last-written active-generation pointer. Each sidecar envelope must carry `schema_version`,
`source_key`, `generation`, an episode mapping, and a digest.

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

**Naming collision the L3 design must resolve first.** `citypods.statesync` already writes a
`generation` field on the compact remote manifest (`_validate_manifest`, `ensure_remote_manifest`)
— an unrelated monotonically incrementing counter for that manifest object itself, bumped on every
manifest write and unconnected to any per-source state layout. Reusing the word "generation" for
the partitioning cutover's own version namespace risks a reader (or a log line, or a future grep)
conflating a manifest-generation bump with a state-layout-generation cutover, which are
independent concepts that can each advance without the other. The L3 design must either pick a
distinct term for the partitioning namespace (for example `layout_epoch` or `partition_generation`)
or explicitly document how the two counters coexist and are told apart in tooling and operator
output.

### Compatibility shim — mandatory given the blast radius

`load_records`, `save_records`, `records_path`, `push_records_merged`, `push_state`, and
`pull_state` are called from **~28 call sites across 19 files** (`citypods/{audit,bench,
availability_review,cli,llm_tag_review,moment_review,r5_benchmark,report,run,search,sharding,
statesync,transcript_quality,tournament,tournament_backfill,speaker_review,state}.py`,
`citypods/compute/external_worker.py`, and 10 scripts under `scripts/`). Rewriting every call site
as part of the cutover is out of scope for a safe migration. **The implementation must preserve
every one of these six function signatures exactly** and dispatch internally to the partitioned
sidecars behind the active-generation pointer; callers do not change. A caller that bypasses these
functions and reads `episodes.json` directly is a separate, explicitly-scoped follow-up.

### Concurrency fencing — two layers, not one

The runbook's GitHub Actions suspension below is necessary but not sufficient by itself. A
CAS-backed maintenance-lease primitive already exists — `citypods.ops.maintenance_leases.acquire`,
used today by `scripts/reset_agenda_chapter_state.py` to fence concurrent lane writers during its
own read-modify-push window — and the migration script should acquire it as a second, code-level
guard. It is **only usable against the R2 coordination backend** (requires
`storage.cas_capable`; raises `MaintenanceLeaseUnavailable` otherwise), so it cannot by itself
fence a legacy workflow writing directly to B2 with R2 unaware. Use both: Actions suspension is the
primary fence (covers every writer regardless of backend); the maintenance lease is defense in
depth for any code path that already respects it, and fails faster/louder than polling the Actions
API for zero active runs.

### Rollback window — grounded in the B2 lifecycle rule that already exists, not a new mechanism

`citypods/ops/reclaim.py`'s `build_b2_retention_rules()` already applies the bucket's only B2
lifecycle rule: a bucket-wide (`Prefix: ""`) `NoncurrentVersionExpiration` with a default
`retention_days=30`. This protects noncurrent **object versions**, not prefixes or generations —
there is no per-generation lifecycle override today. Two designs follow from this, and the L3
implementation must pick the first one unless it has a specific reason not to:
- **Write the new generation to genuinely new paths** (e.g. `state/generations/<id>/...`) and
  never overwrite or delete the legacy `episodes.json` paths during the cutover. The old generation
  is then never noncurrent — no special exemption is needed, "keep the old generation for the
  retention window" is satisfied by construction, and the old paths can be deleted explicitly and
  deliberately once the window closes. **This is the lower-risk design and the default choice.**
- If the migration instead overwrites or deletes an old-generation object in place, the existing
  flat 30-day version-retention window is what recovers it. Extending
  `build_b2_retention_rules`/`scripts/apply_bucket_lifecycle.py` with a generation-aware exemption
  is its own separate follow-up if a window other than the existing 30 days is required — do not
  build one as part of this migration unless the new-paths design above is rejected with a stated
  reason.

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
procedure; the implementation must supply pagination-safe `gh run list`/`cancel` commands (GitHub
paginates at 30 results by default).

**CLI flag convention — match house style, do not invent `--execute`/`--dry-run`.** Every existing
migration/reset script in this repo (`scripts/migrate_stale_issue.py`,
`scripts/reset_agenda_chapter_state.py`, `scripts/reset_materialize_backoff.py`,
`scripts/apply_bucket_lifecycle.py`) uses **`--apply` (`action="store_true"`), dry-run
unconditionally by default when the flag is absent**, plus a printed trailer reminding the operator
to pass `--apply`. None uses a literal `--dry-run` flag. The commands below use `--apply` for that
reason; `--activate` is kept as its own separate, more dangerous flag specifically because flipping
the pointer is the one truly irreversible step and must never be combinable with `--apply` in the
same invocation — the implementation should refuse both flags together with a hard `ValueError`.
`scripts/reset_agenda_chapter_state.py` is also the closest existing analog for the script's
overall shape: it plans first with a pure function (no mutation), prints a summary, mutates only
behind `--apply`, and re-applies its intended state between passes because "the preceding scoped
merge may restore the sibling lane from remote state" — the migration script should follow the
same plan/print/gate/re-verify structure.

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
# STEP 4 (dry run, default): print the full migration plan; mutates nothing.
# ==============================================================================
python -m scripts.migrate_state_partitioning --generation "<timestamp-or-uuid>"

# ==============================================================================
# STEP 5: Publish and verify the candidate generation. Does NOT flip the active pointer.
# ==============================================================================
python -m scripts.migrate_state_partitioning --generation "<timestamp-or-uuid>" --apply

# ==============================================================================
# STEP 6: Activate only after the evidence review described above. Never combined with --apply.
# ==============================================================================
python -m scripts.migrate_state_partitioning --generation "<timestamp-or-uuid>" --activate

# ==============================================================================
# STEP 7: Re-enable GitHub Actions repository-wide
# ==============================================================================
gh api -X PUT /repos/BashfulBits/city-meeting-podcasts/actions/permissions \
  -F enabled=true

# ==============================================================================
# STEP 8: Dispatch read and write canaries, then inspect generation/digest evidence
# ==============================================================================
gh workflow run tag.yml -f city=arlington-tx
```

### Test plan — reuse existing fixtures, do not invent new ones

- **Pure ownership/merge logic**: test directly against in-memory dicts, following
  `tests/test_records.py`'s existing pattern for `merge_preserving_foreign` (e.g.
  `test_merge_preserving_foreign_asr_lane_keeps_remote_audio`,
  `..._never_drops_a_block_remote_lacks`, `..._better_remote_plan_keeps_owned_transcript`) — no
  storage fake needed.
- **Storage/cutover mechanics** (manifest publish, pointer flip, rollback): reuse
  `tests/test_statesync.py`'s `LocalStorage` (plain B2-like fake), its `_CASLocal(LocalStorage)`
  subclass (`cas_capable = True`, hand-rolled `get_bytes`/`put_cas` raising
  `citypods.storage.s3.CASConflict` on mismatch), `RoutingStorage(primary=b2, coordination=r2,
  coordination_prefixes=COORDINATION_PREFIXES)` for dual-backend scenarios, the `_seed_remote(...)`
  helper for pre-populating a fake remote source record, and `_FlakyBucket`/`_FlakyKeyError` for
  transient-error-tolerance tests.
- **No existing fixture simulates a true multi-generation namespace** — a harness for
  `state/generations/<gen>/...` is new and must be built, following the same
  `LocalStorage`-subclassing style as `_CASLocal`.
- Exercise every field/owner mapping (including the `moments` and `integrity` gaps closed above)
  with multi-lane interleavings, a partial upload, a corrupt or missing sidecar, a pointer-flip
  interruption, rollback, and a fresh-checkout reconstruction — comparing UID set, artifact
  references, completion state, planning/timeline values, and append-only calendar rows, not
  merely JSON object counts.

---

## §3. v1 LLM Dispatch Retirement — L3 Evidence Gates & Removal Surface

v1 retirement is a migration of data, producer configuration, generated route metadata, and a
deployed Worker—not a source deletion. `workers/llm-dispatch-proxy/` is **~8,536 lines** across
`src/index.js` (3,719), `test/index.test.js` (3,868), `bench/` (569), `README.md` (285), and
config — not the "~800 lines" Initiative 11 previously estimated; correct that figure wherever it
recurs (it also appears as this section's own Impact line).

**This is a gate on [`review/44`](44-bounded-bundled-llm-dispatch.md)'s own "Phase 3 — Exit
coexistence," not a parallel plan.** Phase 3 already specifies the mechanics: monitor the v1
client registry, drain via the existing recovery tool, flip v2's route ledger from its 50%
split-cap back to 1x, then retire v1's cron trigger and Worker. The implementation should promote
and complete that existing plan, updating review/44 in place, rather than re-deriving quiescence/
drain mechanics from scratch.

**Correction: `list_pending_deferred` is not v1-scoped evidence.**
`citypods.compute.llm_deferred.list_pending_deferred(storage) -> list[JobHandle]` returns
`list(load_deferred_snapshot(storage).pending())` — the shared B2 deferred registry, indexed by
**route/model** (`DEFERRED_INDEX_PENDING_PREFIX{model}/{recipe_hash}.json`), not by transport. A
pending `JobHandle` here can belong to either v1 or v2. An empty result proves the *whole* deferred
registry is drained, not that v1 specifically has nothing outstanding — do not cite it alone as
v1-quiescence proof. v1-specific proof requires the R2-side scan in
`scripts/recover_v1_llm_dispatch_results.py` (below), which is the actual bounded, manual bridge
for v1's own provenance gap and is itself a removal candidate once v1 is empty.

**Exact env var names** (verified in `citypods/compute/llm.py:361-367`) —
`LLM_DISPATCH_URL`/`LLM_DISPATCH_AUTH_TOKEN` (v1) and `LLM_DISPATCH_V2_URL`/
`LLM_DISPATCH_V2_AUTH_TOKEN` (also accepted with a `CITYPODS_` prefix) for v2. **`LLM_DISPATCH_PROXY`
does not exist anywhere in the repo** — do not use that name in any implementation.

**Every workflow referencing v1 secrets or v1-only scripts (15, verified by grep — inventory this
exact list, not a re-derived one):** `chapter-locator.yml`, `chapter-agenda.yml`, `tag.yml`,
`moments.yml`, `r5-benchmark.yml`, `tournament-tag-backfill.yml`, `llm-tournament.yml` (still sets
the v1 secrets even though its own automation is v2-routed — a removal touchpoint),
`llm-deferred-sweep.yml`, `reconcile-orphaned-llm-jobs.yml` (invokes
`scripts/reconcile_v1_llm_jobs.py`), `llm-v1-dispatch-recovery-import.yml` (invokes
`scripts/recover_v1_llm_dispatch_results.py`), `llm-dispatch-reindex.yml` (invokes
`scripts/reindex_llm_dispatch_queue.py`), `llm-dispatch-queue-report.yml` (invokes
`scripts/report_pending_dispatch_queue.py`), `reclaim-transcript.yml` (invokes one of the
requeue/reindex scripts), `llm-dispatch-worker-deploy.yml` (the full v1 deploy pipeline — runs
`scripts/compile_llm_limits.py`, diffs `dispatch_limits.json`, `npm test` in
`workers/llm-dispatch-proxy`, deploys via wrangler-action), and `ci.yml`'s `"Test LLM Dispatch v1
Worker"` step (`working-directory: workers/llm-dispatch-proxy`, `run: npm test`).

**`scripts/compile_llm_limits.py` writes three outputs, not two** — v1's
`workers/llm-dispatch-proxy/src/dispatch_limits.json`, v2's
`workers/llm-dispatch-v2/src/dispatch_limits.json` (a second write of the *same* compiled catalog,
kept per the script's own comment "so v2 has no build/deploy dependency on v1's directory
continuing to exist past its Phase 3 retirement"), and `citypods/compute/llm_routes.json` (the
Python-shaped route table). Retiring v1 removes the first output and
`llm-dispatch-worker-deploy.yml`'s drift check on it; the other two writes and their consumers
**must not** be touched.

**Exact removal diff surface in `citypods/compute/llm.py`** — the transport-selection branch
(`_available_transports()`, verbatim):
```python
def _available_transports(self) -> frozenset[str]:
    if self.config.mode == "dispatch":
        transports = {"mistral-dispatch", "llm-dispatch"}
        if self.config.dispatch_v2_url:
            transports.add("llm-dispatch-v2")
        return frozenset(transports)
    transports = {"direct"}
    if self.config.dispatch_url:
        transports.add("mistral-dispatch")
        transports.add("llm-dispatch")
    if self.config.dispatch_v2_url:
        transports.add("llm-dispatch-v2")
    return frozenset(transports)
```
v1-only transport names are literally `"mistral-dispatch"` and `"llm-dispatch"`. Every code path
gated on `self.config.dispatch_url`/`LLM_DISPATCH_URL` is part of the removal surface: config
validation (mode/URL/scheme checks), `_completed_dispatch_result`, the durable-queue-path builder
(`v1/chat/completions` URL construction, three separate call sites), the dispatch base URL, and
the v1 branch of `delete_dispatched_ref`/`retry_malformed_dispatched`/schema-correction (each of
which has a paired v2 branch that must survive unchanged).

1. **Prove quiescence.** Inventory every `LLM_DISPATCH_URL`/`LLM_DISPATCH_AUTH_TOKEN` consumer
   using the exact file list above (Python modules, the 15 workflows, and the v1-specific scripts
   `scripts/{compile_llm_limits,reconcile_v1_llm_jobs,report_pending_dispatch_queue,
   reindex_llm_dispatch_queue,requeue_failed_llm_dispatch,recover_v1_llm_dispatch_results}.py`) —
   including `citypods/tournament.py`, `citypods/audit_remedy.py`, and ad hoc `TagsStage` calls
   outside the agenda/chapter flow, since `reset-agenda-chapter-state.yml` only covers the
   agenda/chapter path and would silently orphan their pending jobs if reused as a general
   migration mechanism. Disable *new v1 production* first with a configuration validation that
   fails closed; retain a read-only diagnostic while the drain runs.
2. **Drain and preserve.** Run `scripts/recover_v1_llm_dispatch_results.py` (dry-run by default;
   `--apply` writes validated completed results to B2's deferred registry) — its algorithm cross-
   references R2's `requests/` prefix against owned `job_ref`/`locator_job_ref` fields in
   `episodes.json`, validates completed results against the exact Pydantic response contract before
   import, and **never deletes an R2 record in either mode** ("the legacy Worker has no verified
   delete endpoint"). A zero queue count at one instant is insufficient: prove no producer has
   written after the cutover and no owned terminal result remains unimported. Do not treat
   `list_pending_deferred()` returning empty as sufficient v1 evidence (see correction above).
3. **Prove v2 parity.** Contract-test ingress, idempotency, polling, schema correction, retry and
   terminal reconciliation for every active purpose/route. Verify dashboard/sweep observability
   and error semantics from a clean workflow environment containing only v2 credentials.
4. **Remove in a coherent series.** Move shared route/credential logic out of the v1 directory
   before deleting it; update `scripts/compile_llm_limits.py` (removing only its v1 output, per the
   triple-output note above), `scripts/pre-push.sh`, `ci.yml`'s v1 test step, the 15 workflows'
   secret blocks, the v1-specific scripts, `citypods/compute/llm.py`'s removal surface above, and
   tests (`tests/test_workflows.py:830-831`, `tests/test_tournament.py:190-191`) together. Remove
   old GitHub secrets only after the code/config scan and a v2-only canary pass.
5. **Decommission last.** `git rm` does not remove the deployed Worker or R2 data. After the
   retention window and an explicit maintainer approval, disable/delete the deployed Worker using
   Cloudflare's supported operation, record the immutable export/retention disposition, and prove
   that an attempted v1 request cannot be accepted. This is a destructive operational action and
   is outside an ordinary code PR.

---

## §4. The 5 Core Architectural Pillars & 19 L3-Detailed Initiatives

Each entry below is individually L3 dev-ready — implementable directly from this document. Being
detailed is not the same as being scheduled: entering the active queue still requires a measured
trigger (the problem it fixes has actually recurred, or the metric it improves has actually been
profiled), and picking up all 18 at once is not the intent — pick one, per §5's dependency order.
Existing accepted designs take precedence where they overlap; several entries below (5, 11, 15, 16,
18) are explicit pointers into those existing designs rather than independent plans.

### Pillar 1: Throughput & Work Distribution

#### Initiative 1: Shard-Scoped State Synchronization (Sparse Fetching) — L3
- **Problem**: Matrix shard runners download all ~3,500 state files across all 85 catalog feeds,
  even when processing only a single city or source shard.
- **Gap confirmed**: `citypods.statesync.pull_state(storage, state_dir, *, only_paths=None, log=None)`
  (`statesync.py:294`) has **no `only_prefixes` parameter** — only exact-match `only_paths`
  (line 335: `wanted = {f"{STATE_PREFIX}/{rel}" for rel in only_paths}`). `push_state` already has
  both `only_prefixes` and `only_paths`; pull is exact-path-only today. The caller already has the
  scoping information it needs and simply isn't using it: `run.py:2558-2574` computes the shard's
  `owned` source-key set, then `run.py:2690` calls `pull_state(storage, state_dir)` — **unscoped,
  every time** — unless the whole pull is skipped by `state_snapshot_restored`.
- **Implementation**:
  1. Add `only_prefixes` to `pull_state`, mirroring `push_state`'s existing implementation exactly
     (same `str.startswith(tuple(prefixes))` matching against the POSIX-relative path, same
     mutual-exclusivity `ValueError` against `only_paths`).
  2. At `run.py:2690`, once `owned` is known (already computed at :2558-2574), pass
     `only_prefixes=[f"sources/{k}/" for k in owned] + <global control files>` instead of calling
     unscoped. The shard planner producing `owned` is `citypods/sharding.py`'s
     `episodes_for_shard(plan, lane=..., shard_index=..., num_shards=..., expected_sources=...)`,
     which itself consumes a `ShardPlan` from `create_shard_plan(...)` (`sharding.py:92`) built on
     `citypods/records.py:241` `shard_assignment(source_keys, num_shards, *, weights=None)`.
  3. **Global control files that must always be restored regardless of shard** (no single existing
     function returns this list — reconstructed from default `rel_path` params and
     `COORDINATION_PREFIXES`, so hardcode it and keep it in one place): `run_summary.json`,
     `run_events/*.json` (append-only), `source_refresh.json`, `asr_runtime_log.json`,
     `transcript_quality_log.json`, `transcript_quality_calibration_trend.json`,
     `transcript_quality_rollups.json`. CAS-managed files (`compute_budget.json`, `llm_budget.json`,
     `asr_worker_telemetry.json`, `transcript_quality_ledger.json`, `catalog/manifest.json`, and the
     non-`state/` lease prefixes) are already excluded from the bulk file set by
     `_is_cas_managed`/`COORDINATION_PREFIXES` — a sparse-fetch design does not need to special-case
     them.
  4. Never infer ownership from a prefix listing at worker time (the manifest/plan is the source of
     truth), and never permit a sparse worker to write a file it did not restore/own.
- **Tests**: extend `tests/test_statesync.py` with a pull-side analog of
  `test_push_state_only_prefixes_scopes_to_owned_sources` (line 96) — no existing pull-side prefix
  test exists today, confirming the gap. Reuse `test_pull_state_downloads_in_parallel`'s
  `_SlowBucket(LocalStorage)` pattern (peak-concurrency assertion) to verify sparse pulls stay
  parallelized, and compare cold/warm transfer bytes, object count, and startup time on a real
  `workflow_dispatch` run before claiming a percentage improvement.
- **Impact**: Cuts runner startup state-sync latency by 75–85% (~45–90s saved per matrix job).

#### Initiative 2: In-Process Media Probing Cache — L3
- **Problem, corrected**: there is no `SilenceTrimmerStage`/`ASRStage` in `citypods/stages.py` —
  the real classes are `TimelineStage` (runs `SilencePlanner` from `citypods/silence.py`) and
  `TranscriptStage`. The concrete duplicate work is: `AudioStage.process` → `materialize_audio`
  probes the just-encoded local file (`media.py:3576`, `_probe_served_duration_secs(dest, ...)`);
  then `TranscriptStage` (`stages.py:6187,6204-6208`) **re-downloads the same already-hosted audio
  into a brand-new `TemporaryDirectory`** via `_download_audio_file(ep.hosted_audio_url, audio_path)`
  and re-probes it — a second full download plus a second `ffprobe` subprocess of what is, barring
  an upload-corruption bug, the identical bytes already probed once. (`SilencePlanner`'s own
  pre-encode download is already deduplicated against `AudioStage`'s read via the shared
  `SourceCache`, per that class's own docstring — that pair is not a gap.)
- **No existing bounded-cache pattern to extend.** The only caching in the codebase is two
  `functools.lru_cache(maxsize=1)` singletons (`citypods/render.py:14`, `citypods/state.py:78`) —
  neither is argument-keyed or bounded beyond size 1. A probe cache must be designed from scratch:
  keyed by canonical local path + file identity (`mtime_ns`, size, and content digest where
  available), caching only successful immutable results, invalidated on replace/delete, and
  thread-safe (the existing `AudioArtifactCache` in `media.py:2943-2996`, built on
  `threading.Condition`, is the closest structural precedent to copy for thread-safety shape, even
  though it solves a different problem — run-local coalescing of duplicate work, not probe caching).
- **The highest-value fix is the re-download, not just the re-probe.** Caching only the `ffprobe`
  result while still re-downloading the file saves the subprocess cost but not the network cost —
  since `TranscriptStage` already receives `ep.hosted_audio_url` and the audio was *just* produced
  by `AudioStage` in the same run, the cache key should let `TranscriptStage` skip the download
  entirely when it can prove the local artifact from `AudioStage`'s own encode step is still valid
  (same run, same `audio_spec_hash`), falling back to download+probe otherwise. Do not persist
  transient probe data into episode records: persisted duration/evidence already has the
  audio-owned contract in review/26, and a probe-cache entry is neither canonical metadata nor a
  pipeline-versioned artifact.
- **Tests**: follow `tests/test_media.py`'s existing per-scenario naming
  (`test_probe_duration_reads_local_file`, `test_probe_served_duration_prefers_stream_clock_over_container`);
  model a cache-hit test on the existing `test_credit_path_does_not_download_hosted_audio_for_duration`
  (line 540), which already asserts a *non*-download optimization for one code path.
- **Impact**: Eliminates a full second download plus 300–800ms of process-forking overhead per
  episode; accelerates local and CI builds by 2–4 minutes.

#### Initiative 3: Granular Range-Header Audio Slicing — L3
- **Problem, corrected**: no standalone "continuity" verification stage exists (the only
  "continuity" hits in the codebase are `silence.py`'s in-filter PTS-discontinuity correction and
  an unrelated minutes-continuity docstring in `SpeakerIdentityStage`). The real whole-file
  downloads are `SilencePlanner.plan()`'s pre-encode source fetch (`silence.py:503+`, via
  `SourceCache.get_or_fetch`) and `TranscriptStage`'s duration-only re-download (Initiative 2,
  above).
- **Most of what this initiative wants for our own hosted storage already exists — extend it, do
  not rebuild it.** `StorageBackend.get_range(key, start, end) -> bytes | None`
  (`citypods/storage/base.py:59-69`, real `Range:` HTTP headers in `storage/s3.py:389-411`, also
  implemented in `storage/local.py`/`storage/routing.py`) is already consumed by
  `probe_hosted_audio_duration_seconds()` (`media.py:2681-2703`) via `_probe_audio_duration_header`
  → `_fetch_mp4_header`, which isolates just the MP4 `ftyp`/`moov` boxes (bounded by
  `_MP4_INITIAL_RANGE_BYTES`/`_MP4_MAX_MOOV_BYTES`) without downloading `mdat`. This already solves
  the duration-probe case for **already-hosted** audio.
- **The real gap is provider-source fetches**, which never go through `StorageBackend.get_range` —
  they use `requests`/`make_session()` against the provider's URL, before our own hosting exists.
  `citypods/http.py:420-464` `preflight_media_size()` already does exactly this shape of request
  (`Range: bytes=0-0` GET through `make_session()`), and `make_session()` mounts
  `GuardedHTTPAdapter` on both schemes, which calls `validate_source_url(request.url, resolve=True)`
  on every request *and every redirect hop* (`http.py:301-302`). Any new provider-source range
  fetch must route through `make_session()`/`GuardedHTTPAdapter` the same way — never a bespoke
  unguarded session — so SSRF/redirect validation applies automatically per request.
- **Implementation**: (1) classify which verifier decisions are valid from a head range, a tail
  range, or a seekable local artifact — container metadata and a leading sample cannot establish
  duration, continuity, or trailing silence for every codec; (2) add capability detection (a real
  `206` + valid `Content-Range`, since `get_range`'s own docstring notes a backend may return
  fewer bytes than requested without erroring) with a strict full-fetch fallback, byte caps
  matching `security.py`'s existing `MAX_RESPONSE_BYTES = 64 * 1024 * 1024`, and fixtures for
  ignored/malformed ranges and HLS/MP4 edge cases before changing any correctness path.
- **Impact**: Reduces network transfer for verification stages by up to 95%.

#### Initiative 4: Parallelized & Dirty-Skipping State Push — L3 (issue: [GH#1458](https://github.com/BashfulBits/city-meeting-podcasts/issues/1458))
- **Problem**: `push_records_merged` serially iterates through owned sources and unconditionally
  re-reads, re-merges, re-encodes, and re-uploads untouched records at every checkpoint.
- **Evidence already on file**: the issue's own instrumented `tag` lane run measured 224.9s and
  555.1s per checkpoint pushing 42 sources (5.4s and 13.2s per source), all serial round-trips
  against `_STATE_SYNC_MAX_WORKERS` sitting idle at concurrency 1; this is the direct cause of
  `tag.yml` hitting GitHub's 180-minute job timeout on three consecutive scheduled runs. It
  directly affects every lane wired to `mid_run_checkpoint` in `run.py` — `tag`, `diarize`,
  `speaker-identity` — and therefore `tag.yml`, `r7-diarization.yml`, and
  `tournament-tag-backfill.yml`; scoped `audio.yml`/`asr.yml` shards pay a smaller, single-call
  version of the same cost at end-of-run.
- **Implementation**: `push_state()` is already parallelized (`statesync.py:509-516`,
  `ThreadPoolExecutor(max_workers=min(_STATE_SYNC_MAX_WORKERS, len(changed)))` where
  `_STATE_SYNC_MAX_WORKERS = 16` is defined at `statesync.py:55` — copy this exact template into
  `push_records_merged`, do not reinvent it); the remaining hot path is the serial fetch/merge/put
  loop in `push_records_merged` (`statesync.py:640-768`). Select dirty *source record files* before
  any remote read (the existing `DIRTY_JOURNAL_NAME` journal or a pre-merge digest comparison both
  work), preserve the existing fail-safe "unreadable remote means no write" rule, and parallelize
  only distinct source keys. Do not share a mutable local record map, clear a dirty entry before
  its PUT and manifest update both succeed, or assume `_STATE_SYNC_MAX_WORKERS` is automatically
  the right bound here: make it storage-connection-pool aware, configurable, and measured —
  reusing the existing constant is the right default unless a measurement says otherwise.
  **Before touching the hot path**, resolve or explicitly account for `push_records_merged`'s two
  live `DIAGNOSTIC` blocks tied to the still-open agenda-extraction storage-recall investigation:
  block A (`_diag_new_artifact_keys`, the local-pre-merge and merged-pre-push checkpoints,
  `statesync.py:706-742`) and block B (the post-push readback, `statesync.py:749-766`, which
  re-fetches the just-written record and adds a *fourth* round-trip per source whenever a new
  `*_artifact_key` is present). A naive parallelization would silently change their behavior or
  interleaving. Tests need interleaved same-source/cross-source writes, one failed PUT,
  deterministic logging, and a retry after a cancelled checkpoint — extend the existing
  `tests/test_statesync.py` suite directly (`test_push_records_merged_preserves_concurrent_audio`,
  `test_push_records_merged_owned_uids_no_sibling_shard_clobber`,
  `test_push_records_merged_skips_unreadable_remote_rather_than_clobber`, and
  `test_push_records_merged_can_requeue_on_unreadable_remote`, which already asserts
  `raise_on_transient=True` behavior via `pytest.raises(TransientStateSyncError, ...)`) rather than
  writing a parallel suite; a before/after timing comparison on one real `workflow_dispatch` run of
  `tag.yml` (the checkpoint's own `persist=..s push=..s` log line) is the issue's own suggested
  verification.
- **Impact**: Drops checkpoint latency from 15–30s down to <2s; resolves timeout cancellation in
  `tag.yml` and long-running enrichment workflows.

---

### Pillar 2: Reliability, Idempotency & State Consistency

#### Initiative 5: Partitioned Lane State Store — see §2 (`catalog.json`, `audio.json`, etc.)
- **Problem**: B2 lacks CAS. Multi-lane CI workflows running concurrently clobber each other's
  mutations in monolithic `episodes.json`.
- **This initiative *is* §2** — the object ownership table, generation namespace, compatibility
  shim, concurrency fencing, rollback grounding, and runbook are all specified there in full. Do
  not draft a second, independent design here; §2 is the L3 spec for this row.
- **Impact condition**: Eliminates cross-lane clobbers only for objects with a verified exclusive
  writer (§2's `moments`/`integrity` gaps must be closed first). Shared objects retain their
  merge/lease protocol until split further.

#### Initiative 6: Step-Level Timeouts & Budget Stop Alignment — L3 (issue: [GH#1459](https://github.com/BashfulBits/city-meeting-podcasts/issues/1459))
- **Problem**: Overrunning jobs hit GitHub's job-level timeout, cancelling the runner and skipping
  crucial trailing reporting steps.
- **Exact current state, verified per workflow (do not re-derive — implement directly from this
  table):**

  | Workflow :: job | Job timeout | Step timeout already set? |
  |---|---|---|
  | `tag.yml :: tag` | 180m | **Yes — 165m**, step "Produce bounded LLM topic-tag candidates" |
  | `moments.yml :: moments` | 180m | **Yes — 165m**, step "Produce bounded R6 moment candidates and judge assessments" |
  | `chapter-agenda.yml :: extract` | 240m | **Yes — 225m**, step "Extract agenda candidates" |
  | `chapter-locator.yml :: locate` | 45m | **Yes — 38m**, step "Locate agenda candidates in complete transcripts" |
  | `llm-tournament.yml :: tournament` | 30m | **Yes — 22m**, step "Run bounded tag samples" |
  | `r7-diarization.yml :: diarize` | 330m | No — a cancel skips the unconditional trailing "Project speaker identities and queue review candidates" step; diarization work finishes but is never projected or queued for review |
  | `audio.yml :: audio` | 360m | No — its `if: always()`-guarded "Collect H16 run event"/"Upload H16 shard evidence" steps are only partially protected, inside the runner's cancellation grace period before force-termination |
  | `llm-deferred-sweep.yml :: sweep` | 360m | No — loses the `llm_deferred_sweep_end` summary, the only place `submit_failed` currently surfaces |
  | `r5-benchmark.yml :: benchmark` | 180m | No |
  | `tournament-tag-backfill.yml :: backfill` | 180m | No |
  | `asr.yml :: asr` | **not set at all** (silently inherits GitHub's 360m default) | No — needs a job-level timeout added *first*, before a step timeout can be layered on |
  | `asr.yml :: reconcile` | **not set at all** | No |

  The five rows already marked "Yes" share a nearly word-for-word rationale comment (verbatim,
  from `tag.yml`):
  ```yaml
      - name: Produce bounded LLM topic-tag candidates
        # Fail loudly instead of being cancelled. A JOB-level timeout cancels the run, which
        # GitHub reports as a grey "cancelled" -- indistinguishable at a glance from someone
        # cancelling it by hand, and it skips every later step so nothing is persisted or
        # reported. This lane hit exactly that on eight consecutive scheduled runs
        # (2026-08-26..09-02). A STEP-level timeout below the job's fails the step, so the run
        # goes red and the post-steps still execute. Record-first source preparation is what
        # should keep the lane well inside this; reaching it means something regressed.
        timeout-minutes: 165
  ```
  Reproduce this exact comment (adjusted per-job) and a step ratio of ~88–93% of the job timeout
  for each of the six remaining rows; give `asr.yml`'s two jobs an explicit job-level timeout as
  its own decision first (there is no existing value to derive a ratio from).
- **Cooperation contract with the Python-side stop mechanism** (`citypods/run.py`): the SIGTERM
  handler is `install_signal_handlers()` (`run.py:4161`, called only from the two CLI entry points
  in `citypods/cli.py`), which registers `_signal_stop_handler` (`run.py:4147`) writing to the
  process-wide `_INTERRUPT = threading.Event()` latch (`run.py:4134`) via `request_stop()`
  (`run.py:4137`); `interrupt_requested()` (`run.py:4142`) reads it. `class StopSignal` (`run.py:4173`)
  is the actual per-run budget object — `__call__(self) -> bool` (`run.py:4206`) checks, in order,
  the interrupt latch, then a wall-clock `deadline` (a `time.monotonic()` value), then a polled
  `superseded()` callback. **A step-level `timeout-minutes` must sit above this `StopSignal`
  deadline, not merely above the job timeout** — if the step timeout fires before the run's own
  deadline, Actions' SIGTERM→hard-kill sequence races the graceful in-process stop instead of
  giving it room to finish and persist. The `interrupted` flag in run summaries (`run.py:4069`,
  tagged `GH#377`) is what already distinguishes a deliberate stop from a step-timeout kill.
- **Tests**: extend `tests/test_workflows.py` directly — it already has the exact idiom needed
  (`_job(workflow_file, job_name)` helper, line 27, returning `(wf, job)`; existing tests locate a
  step via `next(step for step in job["steps"] if step.get("name") == "...")` and assert
  `job["timeout-minutes"] == N`). **Confirmed gap**: none of the file's existing
  `timeout-minutes` assertions check a *step-level* value anywhere, including for the five
  workflows that already have one — add `assert step["timeout-minutes"] == N` for every workflow
  this initiative touches, both the five already-set ones (to lock the invariant in) and the six
  new ones.
- **Impact**: Prevents grey cancelled runs; guarantees trailing reporting steps execute and fail
  visibly as red on true timeouts.

#### Initiative 7: Hardened SSRF Source URL Validator (DNS Pinning & CIDR Checks) — L3 research spike
- **Problem, exact current behavior**: `validate_source_url` (`citypods/security.py:178-217`,
  verbatim):
  ```python
  def validate_source_url(
      url: str, *, allowed_hosts: Iterable[str] | None = None,
      resolve: bool = True, resolver=_default_resolver,
  ) -> None:
      if not _SCHEME_RE.match(url or ""):
          raise SecurityError(f"source URL has no scheme: {url!r}")
      scheme = urlsplit(url).scheme.lower()
      if scheme not in ALLOWED_SCHEMES:  # {"https"}
          raise SecurityError(f"scheme {scheme!r} not allowed (https only): {url!r}")
      host = _hostname(url)
      if not host:
          raise SecurityError(f"source URL has no host: {url!r}")
      if allowed_hosts is not None and not _host_allowed(host, allowed_hosts):
          raise SecurityError(f"host {host!r} not in the allowlist for this source: {url!r}")
      if resolve:
          for ip_str in resolver(host):
              ip = ipaddress.ip_address(ip_str)
              if _is_blocked_ip(ip):  # private/loopback/link-local/reserved/CGNAT 100.64.0.0/10
                  raise SecurityError(f"host {host!r} resolves to non-public address {ip_str} — refusing: {url!r}")
  ```
  It checks scheme, an optional host allowlist, and — only when `resolve=True` — that every
  resolved IP is public. **It does not pin the validated IP into the actual connection.** Called
  from **~26 sites across 18 modules** (`citypods/http.py`'s `GuardedHTTPAdapter.send` — the
  fetch-time gate, run on every request *and every redirect hop*, per its own class docstring —
  plus `concat.py`, `granicus_chunked.py`, `granicus_proxy.py`, `transcript_quality.py`,
  `provider_request.py`, `swagit_proxy.py`, `config.py`, `video_clips.py`, `providers/{swagit,
  granicus,civicplus,civicengage,onemeeting}.py`, `stages.py` (3 sites), and
  `compute/external_worker.py`).
- **Confirmed TOCTOU mechanism**: `citypods/http.py`'s `make_session()` (line 357) builds a
  `requests.Session` with `GuardedHTTPAdapter` mounted on both `https://`/`http://`; the adapter's
  `send()` (line 301) calls `validate_source_url(request.url, resolve=True)`, resolving the
  hostname once via `_default_resolver`→`socket.getaddrinfo`, **then discards that result** —
  `super().send()` triggers a second, independent resolution deep inside `urllib3`'s connection
  pool, with no code path carrying the validated IP forward. There is no `getaddrinfo`/
  `create_connection` override, `source_address` pinning, or custom transport beyond
  `GuardedHTTPAdapter` anywhere in `http.py` today — no hook point for DNS-pinned dialing exists;
  one must be added (most likely subclassing `urllib3.connection.HTTPSConnection` or overriding
  the adapter's `get_connection`/`init_poolmanager` to inject a resolved `socket_options`, or
  resolving once and rewriting the connection-pool key).
- **A second, distinct bypass surface the original doc undersold**: `citypods/media.py`'s
  `_preflight_remote_source(url, max_media_bytes)` (line 1065) is called before
  `CommandFfmpeg._render_identity`/`_render_filter` invoke `ffmpeg`/`ffprobe` **directly on a
  remote URL** (when no local source-cache copy exists yet — dry-run, a failed cache fetch, or a
  concat fallback). It only does a size check via `preflight_media_size` (which does route through
  the guarded session and gets `validate_source_url` applied once); it does **not** re-validate
  immediately before the subprocess launches, and ffmpeg's own libavformat networking performs its
  own independent DNS resolution and TCP connect, **entirely outside `citypods.http`**. SSRF
  pinning at the `requests` layer does not cover this path at all — the L3 design must treat it as
  a second, separately-solved surface (e.g. a trusted egress proxy boundary for the ffmpeg case,
  since pinning inside libavformat itself is not realistically achievable), not an afterthought of
  "the ffmpeg paths that do not use `requests`."
- **This remains a research spike, not an implementation instruction.** A naïve IP URL plus `Host`
  header breaks HTTPS SNI/certificate verification, proxy behavior, redirects, and connection
  reuse. Produce a threat model and a transport design that pins the connection while retaining
  hostname SNI and certificate validation for the `requests` path, and a stated boundary (proxy or
  accepted-risk) for the ffmpeg path. Test IPv4/IPv6, every DNS answer, rebinding between
  validation and connect, redirects, proxy environment variables, and a safe failure. Do not claim
  complete mitigation before that design is independently reviewed.

#### Initiative 8: `EnrichmentStage` Protocol Hardening — L3 (premise corrected)
- **The original problem statement is factually wrong for the current code — do not act on it as
  written.** All 19 stage classes in `citypods/stages.py` already implement the *identical*
  signature:
  ```python
  def process(
      self, provider, city: City, episodes: list[Episode], ctx: StageContext
  ) -> StageStats:
  ```
  (verified at all 19 class definitions). A `Protocol` already exists —
  `citypods/stages.py:570-578`:
  ```python
  class EnrichmentStage(Protocol):
      name: str
      version: str
      def process(
          self, provider, city: City, episodes: list[Episode], ctx: StageContext
      ) -> StageStats:
          """Enrich the ``_materialize_set`` of ``episodes`` in place, within budget."""
          ...
  ```
  and there is exactly **one** call site, `stage.process(provider, city, dirty, ctx)`
  (`citypods/stages.py:7650`, inside `run_stages()`), already typed against
  `stages: list[EnrichmentStage]`. `citypods/pipeline/` and `contract.py` **do not exist anywhere
  in the repo** — the doc's original "relocate to `citypods/pipeline/contract.py`" is a proposal
  for a wholly new module, not a move.
- **Real, narrow remaining work**: (1) make `EnrichmentStage` `@runtime_checkable` (it currently
  is not — no such decorator is imported/used anywhere in the file) so `isinstance(stage,
  EnrichmentStage)` becomes usable at registration/test time; consider relocating it into a new
  `citypods/pipeline/contract.py` for discoverability, re-exporting from `stages.py` for backward
  compatibility. (2) Normalize the `ctx.stop` null-check idiom, which genuinely is ad hoc: 31
  occurrences split between `if ctx.stop and ctx.stop():` (e.g. lines 955, 1211, 1297, 1364) and
  `if ctx.stop is not None and ctx.stop():` (e.g. lines 1688, 1998, 2143, 2555) — functionally
  identical, stylistically inconsistent. Pick one and apply it uniformly; this is the actual
  substance behind "ad-hoc wall-clock stop budget handling," not a structural defect.
- **Do not** rename `process`→`execute`, invent a `should_run` method that doesn't exist, or touch
  all 19 classes' primary entrypoint — there is no behavioral divergence to fix there, and doing so
  would be a pure-cost, zero-benefit rewrite of the one uniform call site.
- **Impact**: Makes the existing stage contract mechanically verifiable at registration/test time
  and removes one real stylistic inconsistency; corrects a previously-inaccurate problem statement.

---

### Pillar 3: Observability & Operational Telemetry

#### Initiative 9: Structured Event Telemetry & Durable Object Quota Alarms — L3
- **Problem**: `citypods/run.py` and `citypods/stages.py` narrate exclusively through unstructured
  `print(..., flush=True)` (104 call sites in `run.py`, 60 in `stages.py`; zero use of stdlib
  `logging` anywhere in `citypods/`), blocking programmatic ingestion. Separately, the Cloudflare
  Workers Free plan's 5M-daily Durable Object row-read ceiling was exhausted on 2026-08-27
  (review/44's "Durable Objects rows-read overage retrospective") with no pre-breach warning.
- **Structured events — implementation**:
  1. Create `citypods/events.py` (**not** `citypods/telemetry.py` — `citypods/compute/worker_telemetry.py`
     already exists and covers an unrelated concern, external ASR worker resource metrics; reusing
     "telemetry" naming would collide). Define
     `emit_event(event: str, *, outcome: str, lane: str | None = None, source_count: int | None = None,
     duration_s: float | None = None, error_class: str | None = None,
     log: Callable[[str], None] | None = None, **fields) -> None`. It composes one JSON object
     (`event`, `ts` as UTC ISO-8601, `run_id` read from the `GITHUB_RUN_ID` env var when present,
     plus the named/`**fields` values), serializes with `json.dumps(..., sort_keys=True)`, and
     prints one line prefixed `EVENT ` (grep/`jq`-extractable from existing workflow logs without a
     second sink). `log` defaults to the same `print(msg, flush=True)` fallback already used
     elsewhere (e.g. `run.py:969`'s `emit = log or (lambda msg: print(msg, flush=True))`).
  2. **Attach at the existing `log`-parameter seam, not at every print site.** `run.py` and
     `citypods.statesync` already thread an optional `log: Callable[[str], None] | None` through
     most multi-step functions. Do not rewrite all ~164 print call sites. Add one `emit_event(...)`
     call at the start and end of each already-narrated phase boundary; the L3 issue must enumerate
     every site by a fresh grep (line numbers shift as nearby code lands — re-grep, don't trust a
     stale citation), but these five are confirmed real as of this writing and keep their existing
     human-readable print line unchanged alongside the new structured one: `run.py:2168`/`2176`
     (audio pass start/done), `run.py:2190`/`2200` (transcript pass start/done),
     `run.py:1998-2016`'s `_flush_tag_batch()` (tag LLM batch flush — port its existing
     `jobs=`/`pending=`/`completed=`/`errors=` values verbatim into `emit_event`'s fields; this
     function now runs at both `_checkpoint_if_due()` and end-of-pass, so instrument it once inside
     the shared helper rather than at each call site), `run.py:2705-2707` (state restore summary),
     and `stages.py:2660-2668` (`_log_asr_external_required` — already structured-ish; port its
     fields).
  3. **Redaction enforced by construction.** `emit_event`'s `**fields` accepts only
     `int | float | str | bool | None`; passing anything else (a list, dict, or object) must raise
     `TypeError` at the call site rather than silently stringify — the mechanical guard against a
     future call site smuggling transcript text, a prompt, or a raw provider response into
     structured output. `tests/test_events.py::test_emit_event_rejects_non_scalar_field` asserts
     this; `test_emit_event_shape` asserts `event`/`ts`/`outcome` are always present and `ts`
     parses as UTC ISO-8601; `test_emit_event_never_contains_known_secret_patterns` is a
     belt-and-suspenders regex smoke test against `sk-`, `Bearer `, and `://.*:.*@` credential-in-URL
     patterns — not a substitute for the discipline of never passing that data in.
- **DO quota alarm — implementation**: no mechanism exists today to read Cloudflare's real, billed
  Durable Object rows-read count from inside the Worker (`workers/llm-dispatch-v2/wrangler.jsonc`
  binds only `durable_objects` and Workers Logs `observability`; there is no Analytics Engine
  binding and no API-token secret for Cloudflare's GraphQL Analytics API). The L3 issue must pick
  exactly one design and state which in the PR description — they are not equivalent:
  - **(a) External cron against the real meter.** A scheduled step queries Cloudflare's GraphQL
    Analytics API (`durableObjectsInvocationsAdaptiveGroups`/storage-metrics) with a scoped
    read-only API token stored as a repo secret, at a cadence no tighter than the metric's own
    empirically-verified refresh lag. Only this path alarms on the actual billed number.
  - **(b) In-process proxy alarm.** Reuse the DO's existing bounded `stats(now, limit=20)` RPC
    (`coordinator.js:1090-1189`, already the precedent for a cheap indexed diagnostic query) to
    expose row counts of the tables the 2026-08-27 incident implicated (`bundles`, `attempts`) and
    alarm on those growing past a threshold. Cheaper, zero external dependency — but
    `test/rows-read.test.js`'s own documentation states its estimator is "a scale-sensitivity
    probe, not a billing meter: exact for the unbounded-scan class, conservative elsewhere," so a
    proxy alarm can miss quota pressure that isn't row-count-shaped. State this limitation
    explicitly in the L3 doc rather than implying parity with (a).
  Whichever is chosen, the alarm's own read must itself be indexed/bounded and pass
  `test/rows-read.test.js`'s existing invariants (it must not become a second unbounded scan — the
  exact failure mode of the original incident). Delivery reuses `workers/city-request-intake`'s
  Discord pattern verbatim: `notifyDiscord`/`updateDiscord` (`index.js:147-196`) and its D1-backed
  idempotency key (`sha256(f"{kind}|{issue_url}|{target_url}")`, claimed via a conditional
  `UPDATE ... WHERE column IS NULL OR column NOT LIKE 'pending:%'`, `index.js:211-218`); a new
  quota alarm needs one D1 table (or a reused binding, if in the same Worker) keyed by
  `(alert_kind, threshold_bucket, day)` so a sustained breach sends one message per day per bucket.
  Tests mirror `test/rows-read.test.js`'s own methodology: seed at two scales, assert the alarm's
  query has no `SCAN` on a growable table via `EXPLAIN QUERY PLAN`, and add one deliberate
  mutation (drop the relevant index) proving the test actually catches the regression it claims to.
- **Impact**: Real-time visibility into quota consumption and one deduplicated alert on runaway
  queries, without reproducing the 2026-08-27 incident inside its own alarm.

#### Initiative 10: OpenTelemetry Tracing for Multi-Stage Runner Spans — L3
- **Problem**: Profiling stage duration across distributed runners requires manual log timestamp
  correlation.
- **Design/implementation**:
  1. Genuinely greenfield with one version constraint to check first: no `citypods/telemetry.py`
     exists and no code under `citypods/`/`scripts/` imports `opentelemetry`. However,
     `constraints/asr.txt` already vendors a full transitive OTel stack
     (`opentelemetry-api==1.44.0`, `opentelemetry-sdk==1.44.0`, `opentelemetry-exporter-otlp*==1.44.0`,
     `opentelemetry-semantic-conventions==0.65b0`), pulled in solely by `pyannote-audio` and
     otherwise unused. Before adding a direct dependency, run `scripts/compile_constraints.sh`
     locally with the new `pyproject.toml` floor and confirm the resolver lands on one compatible
     version across the `dev`/`prod`/`asr` profiles rather than a split-version conflict with the
     existing transitive pin.
  2. Follow review/22's exact 5-step dependency-addition contract
     (`review/22-dependency-and-reproducibility-policy.md` §"Adding or changing a dependency"): an
     abstract `>=` floor in `pyproject.toml` only, recompile `constraints/*.txt` via
     `scripts/compile_constraints.sh` and commit the diff, classify the change (tracing never
     touches produced audio/transcript bytes, so it is **hygiene** — Renovate's grouped auto-merge
     lane, not the Dashboard-approval lane), do not re-list it in `scripts/compute/modal_app.py`/
     `beam_app.py` (external workers inherit the shared constraints), then open the PR — CI's
     `deps` job (`.github/workflows/ci.yml:114-127`, `python scripts/check_dependency_policy.py`)
     enforces constraints-drift, pinned-Actions, and external-worker-dependency guards.
  3. No-export-default: instrumentation is inert unless an exporter endpoint is explicitly
     configured (e.g. `OTEL_EXPORTER_OTLP_ENDPOINT` unset → a no-op tracer provider). A new
     `tests/test_telemetry_tracing.py` asserts that running a representative stage with no exporter
     env var set makes zero network calls (patch the OTLP exporter's transport and assert it is
     never constructed).
  4. Span attributes are restricted to source hashes/counts (`source_key`, `episode_uid`, byte
     counts, durations) — never transcript text or prompt content, matching Initiative 9's
     non-scalar-rejection discipline.
  5. Scope the first span boundary to one pillar only (e.g. `AudioStage`/`ASRStage`, or one LLM
     dispatch call) rather than instrumenting every stage in the first PR, so exporter/sampling/cost
     decisions get real production evidence before wider rollout.
- **Impact**: Waterfall traces of feed-processing latency once explicitly enabled; zero
  behavior/cost change when it is not.

---

### Pillar 4: LLM Job Admission, Routing & Refinement

#### Initiative 11: Sunset v1 Proxy & Consolidate on v2 with `llm_lanes` — see §3
- **Problem**: Dual-stack transport maintenance and bifurcated code paths.
- **This initiative *is* §3** — the evidence gates, the exact 15-workflow/6-script inventory, the
  `citypods/compute/llm.py` removal surface, and the triple-output limits-compiler correction are
  all specified there. Do not draft a second plan here.
- **Impact**: Removes `workers/llm-dispatch-proxy/` (**~8,536 lines** including its own tests and
  benchmarks — not the previously-stated ~800) and unifies provider routing logic under
  `llm_lanes` (`config/site_config.yml`, `citypods/compute/llm_lanes.py`, PR #1457, now merged).

#### Initiative 12: Unified Structured Output via LiteLLM Native JSON Schema — L3
- **Problem**: Instructor 1.15.4's lack of native Gemini JSON Schema support forced a hand-rolled
  shim, `_run_native_structured_direct()` (`citypods/compute/llm.py:945-984+`), triggered only when
  `route.structured_output_direct_handler == "native"` (`_run_structured_direct`, `:884-904`) — a
  field traced through `config/provider_limits.yml`'s per-profile `direct_handler` key, compiled by
  `scripts/compile_llm_limits.py:205-225,813`, into `LLMRequestPolicy.structured_output_direct_handler`
  (`citypods/compute/llm_policy.py:183,303-304`, default `"instructor"`). The shim's own docstring
  documents exactly which providers need it today: Gemini (native `responseJsonSchema`) and
  DeepSeek (JSON-object mode) — and a real usage-accounting subtlety: on a retry-then-succeed
  outcome, `output["usage"]` sums *both* attempts' usage, "because a first attempt that fails
  validation still reached Gemini and spent real tokens/quota."
- **Comparison path**: the Instructor+Pydantic route (`_run_structured_direct`, `:884-943`) uses
  `instructor.from_litellm(completion_fn, mode=...).create_with_completion(response_model=model,
  messages=..., max_retries=1, ...)`, catching `InstructorRetryException` →
  `LLMStructuredOutputError`. Pinned versions (identical across `constraints/dev.txt` and
  `constraints/prod.txt`): `instructor==1.15.4`, `litellm==1.95.0.dev1`.
- **Implementation**: a dependency upgrade does not by itself prove uniform provider semantics.
  Build a route × model × schema capability matrix from these exact pinned versions and live
  contract probes (extend `citypods/llm_compat_probe.py`, already the tool that confirmed Gemini's
  missing Instructor compatibility-table entry), then retain the current local Pydantic validation
  and schema-retry behavior as the correctness authority. Only remove a shim when every enabled
  route has equivalent request/response, refusal, retry, and usage-accounting behavior (including
  the dual-attempt sum above — a native path must not silently under-report usage on retry). Follow
  review/22: pin/recompile constraints and state whether a prompt/response recipe change triggers
  gradual invalidation or leaves historical artifacts.

#### Initiative 13: Model-Specific Prefix Prompt Caching — L3
- **Problem**: Dynamic variables placed early in prompt templates bust provider prefix caches.
  There is no `citypods/compute/prompts/` directory — prompt-building lives in
  `citypods/chapter_titles.py`/`citypods/chapter_jobs.py`.
- **The fixed-prefix/variable-suffix shape already exists — anchor caching there, don't invent a
  new structure.** `build_agenda_item_extraction_request()` (`citypods/chapter_titles.py:321-401`)
  already builds a `system` message as a large fixed instruction string with only a
  `prompt_variant`-keyed suffix appended (`:393`), and a `user` message that is pure
  `json.dumps(material, ...)` (`:396-399`) — the *only* variable content. This is a natural cache
  anchor: system message stable per `prompt_variant`, user message varies per call.
  `build_production_agenda_item_extraction_request()` (`:412-428`) is the pinned-model wrapper to
  extend first.
- **Zero existing cache telemetry — build from scratch, not "extend."** No call site captures
  `cache_creation`/`cache_read`/`cached_tokens`/`prompt_tokens_details` anywhere in
  `citypods/compute/llm.py` or elsewhere (confirmed by repo-wide grep). Add this telemetry via
  Initiative 9's `emit_event` (an `event="llm_cache"` with `route`, `cache_hit_tokens`,
  `cache_creation_tokens`, `latency_ms` fields) before making any prompt-restructuring change, so
  the "measure per route" requirement below has something to measure against.
- **Implementation**: treat caching as route-specific and evidence-driven; do not pad prompts to
  assumed provider boundaries or promise a universal saving. Preserve message roles and semantic
  ordering, measure cache-hit/creation tokens and latency per route using the new telemetry above,
  and make the prompt/recipe fingerprint explicit. Any output-affecting prompt change needs
  golden/tournament comparison and the same backfill statement required for a pipeline-version
  change.

#### Initiative 14: Dynamic Budget Allocation & Priority Queuing on `llm_lanes` — L3 (premise corrected)
- **Problem**: High-volume backfills saturate worker admission, starving daily active feeds.
- **Correction: `llm_lanes` does not carry priority today — this is new wiring, not an
  extension.** `LaneConfig` (`citypods/compute/llm_lanes.py:56-79`) has no `priority` field at all
  (`purpose`, `models`, `max_dispatches_per_run`, `reserved_write_units`, `daily_write_units`,
  `dispatch_shape` only); `config/site_config.yml`'s `llm_lanes` block (e.g. `chapter-agenda`,
  `chapter-locator`, `topic-tags:tagger`, `tournament:tag` entries) has no `priority` key in any
  entry. Priority is set today as a **per-call** `LLMRequestPolicy.priority: Literal[0, 1] = 1`
  field (`citypods/compute/llm_policy.py:48-55`, "settable only at submission... there is
  deliberately no API to edit priority on an already-queued job"), read at dispatch build time
  (`citypods/compute/llm.py:1923`: `priority = policy.priority if policy else 1`).
- **Corrected ordering SQL** (`workers/llm-dispatch-v2/src/coordinator.js`'s `claimDispatchWindow`,
  query at lines 1966-1974): `ORDER BY job_models.priority ASC, job_models.created_at ASC,
  job_models.job_id ASC` (filtered by `WHERE job_models.model = ? AND jobs.state = 'queued'`) — no
  `state` column in the `ORDER BY` itself.
- **Implementation**: add a `priority` field to `LaneConfig` and `parse_lanes()`'s validation
  (`llm_lanes.py:137-222`), extend `scripts/compile_llm_lanes.py`'s Worker-side compilation and the
  YAML schema, and wire the per-lane value into `LLMRequestPolicy.priority` at dispatch-build time
  (replacing the current per-call default). Define the producer mapping from a verifiable
  publication deadline to `{0, 1}`, authorization against priority inflation, an aging/starvation
  rule for backfills, per-lane admission caps, and metrics for queue age by priority. "Zero delay"
  is not a valid promise when a route/provider is unavailable.

#### Initiative 15: Calibrated Confidence Estimation & Deterministic Fallbacks — see review/35
- **Problem**: Occasional LLM hallucination during low-audio-quality segments produces invalid
  chapter timestamps or inaccurate agenda links.
- **Implementation**: reuse the existing contract by name, not generically —
  `candidate_matrix_key()` (dimensions: `feature`, `provider_model`, `prompt_version`,
  `taxonomy_version`, `label`, `scope`, `source_kind`, `assessment_kind`, `evaluator_model`),
  `resolve_threshold()` (falls back to `EvaluationConfig.fallback_for(feature, provider_model)`,
  default `1.0` — deliberately unreachable), `apply_admission()` (asymmetric: `confidence >
  threshold` for a `"fallback"` basis, `confidence >= threshold` for a qualified one), and
  `refresh_matrix()` (recomputes from `REVIEW_DECISIONS = ("correct", "incorrect", "ambiguous")`,
  picking the lowest threshold meeting `minimum_reviews` and `required_precision`) — all in
  `citypods/llm_evaluation.py`, per review/35. Define feature-specific precision/recall thresholds,
  deterministic validator inputs, what "no result" means, and an audit record for a rejected
  candidate. A regex fallback may be less correct than withholding a chapter/link, so it must be
  evaluated per feature and must never replace official title, date, URL, or transcript text. This
  belongs in the relevant review/35 or generated-chapter follow-up, not a generic cross-feature
  stage.

#### Initiative 16: Autonomous Evaluator Tournament Engine — see review/34 §14
- **Problem**: Prompt revisions and model upgrades currently rely on subjective manual checks.
- **Status, corrected**: review/34's header ("champion-ticket automation remains follow-up") is
  stale relative to its own **§14 "Implemented ticket and merge gate (2026-08-28)"**, which
  documents that champion-ticket automation for the `tag` verb is already implemented: a weekly
  rolling ticket via the shared review-issue adapter, actionable at "tie-adjusted win rate strictly
  greater than 60%," landing as a scoped config-only PR rather than an auto-merge. Cite review/34
  §14, not its header, and flag/fix the header's own staleness as part of this work.
- **Integration points**: `citypods/tournament.py`'s `champion_stats()`, `render_champion_ticket()`,
  and `package_ticket()` (the last resolves the current champion via
  `lane_for("topic-tags:tagger").primary_model` — confirming the champion is already read from the
  `llm_lanes` registry, not a separate config key) are the real entry points a broader-verb rollout
  must call into. `.github/workflows/llm-tournament.yml` still sets the v1
  `LLM_DISPATCH_URL`/`LLM_DISPATCH_AUTH_TOKEN` secrets — a §3/Initiative-11 removal touchpoint to
  include when that work lands.
- **Before writing anything new**: confirm whether `scripts/eval_prompt_tournament.py` exists as a
  stray duplicate (not verified in this pass) — the doc's "must not create a parallel" instruction
  needs that check resolved first, either way.
- This program must integrate with the review/34 contract rather than create a parallel engine;
  any gap must be filed as a narrow review/34 follow-up with a fixed corpus version, judge version,
  score thresholds, cost ceiling, and a no-auto-production-route rule.

#### Initiative 19: Admission-First & Registry-First Tag Dispatch ([GH#1463](https://github.com/BashfulBits/city-meeting-podcasts/issues/1463)) — L3
- **Problem, confirmed against the real code**: `LiteLLMBackend.enqueue_batch()`
  (`citypods/compute/llm.py:1806-2124`) unconditionally writes every job's B2 payload —
  `storage.put_cas(payload_key, canonical_payload_str.encode("utf-8"), "application/json")`
  (`:1911-1918`) — **before** it ever POSTs the batch to the Worker's
  `v2/jobs:enqueue-batch` (`:1952-1959`). Admission/capacity rejection (`purpose_not_registered`,
  `model_not_in_lane`, per-purpose `daily_write_units` exhaustion, global
  `MAX_INGRESS_WRITE_UNITS_PER_UTC_DAY`) happens entirely inside the DO's
  `enqueueBatch` (`workers/llm-dispatch-v2/src/coordinator.js:650+`), *after* that write has
  already happened — a rejected job has already cost a real B2 write for nothing. This part of the
  issue's problem statement is accurate as filed.
- **Item 3 (only merge/persist changed source records) is [GH#1458](https://github.com/BashfulBits/city-meeting-podcasts/issues/1458),
  i.e. Initiative 4 above — already fully specified there.** Do not re-derive or duplicate it here;
  this initiative should be sequenced to land *after* Initiative 4 (it builds on the same dirty-push
  mechanism) and must not touch `push_records_merged` independently.
- **Item 2 (registry sufficiency) needs its premise checked, not assumed — the registry is already
  more sufficient than the issue implies, and one relevant piece landed independently while this
  document was being written.** PR #1462 ("flush topic tag jobs at checkpoints", merged after this
  section was first drafted) already moved the tag batch's submission into a shared
  `_flush_tag_batch()` (`run.py:1998-2016`), called from `_checkpoint_if_due()` *before*
  `_persist_all()`/`mid_run_checkpoint()` (`run.py:2024-2027`, with an explicit comment: "Never
  push provisional batch-pending handles to durable state. Submit them first so the checkpoint
  records a real Worker handle... for retry"). That closes exactly one gap this initiative would
  otherwise have had to open: it guarantees every checkpoint's local persist reflects the batch's
  post-submission state (durable handle or recorded submission error), never a stale
  pre-submission placeholder. It does **not** by itself prove crash safety *before* a checkpoint —
  `write_deferred(storage, job.recipe_hash, handle)` (`citypods/compute/llm.py:2082`) already runs
  **synchronously and independently of episode-record persistence**, immediately on Worker
  acceptance inside `enqueue_batch` itself, so a crash between acceptance and the next checkpoint
  (whether or not that checkpoint has run `_flush_tag_batch()` yet) does not lose the durable B2
  registry entry for that job. What
  is genuinely unproven is **recipe-hash determinism across a crash and restart**: on the next run,
  `TagsStage` must re-derive the *exact same* `recipe_hash` for the same candidate from the
  episode's pre-tag state so that `look_up_deferred()` (called at the top of `enqueue_batch`,
  `:1832`) finds the prior handle/result and skips re-submission. This has never been asserted as
  an explicit test. Before reducing checkpoint frequency or scope on the strength of "the registry
  already covers it," add a kill-and-resume integration test that: runs `TagsStage`'s real
  recursive per-episode job-splitting path (not a synthetic single-job case, since that's what
  `citypods/tags.py`'s `llm_tag_suggestions` actually does — recursively splitting one episode into
  a variable number of jobs, per review/44's Phase 4 note), submits a batch, kills the process
  before any `_persist_all()`/push, restarts, and asserts (a) zero duplicate `enqueue_batch` calls
  for already-accepted recipe hashes and (b) every already-completed result is still reachable and
  gets applied. Only once that property is proven does it become safe to widen Initiative 4's
  dirty-skip to cover in-flight tag checkpoints more aggressively.
- **Item 1 (atomic, expiring admission reservation before payload staging)**: this extends
  review/44's existing ingress-reservation design (`llm_lanes`'s `reserved_write_units`/
  `daily_write_units`, compiled into `ingress_reservations.json`, enforced in
  `coordinator.js`'s `enqueueBatch`), not a replacement for it. A two-phase protocol is needed: a
  cheap pre-check/reserve call the client makes *before* `put_cas`-ing any payload, and a matching
  consume-on-enqueue step. "Expiring" means a client that reserves and then crashes before staging
  must not permanently strand that capacity — size the TTL against `_CHECKPOINT_INTERVAL_SECONDS`
  (180s) plus this initiative's own new admission round-trip, not an arbitrary value. **Whatever
  storage backs the reservation must stay rows-read-bounded exactly like the existing
  `scheduler`/`ingress_purpose` tables** (`scheduler` is a single row; `ingress_purpose` is keyed
  by `(utc_day, purpose)`, so at most a few dozen rows ever exist at once) — a reservation table
  keyed by job id or unbounded by day would reintroduce the *exact* failure shape of the
  2026-08-27 DO rows-read incident review/44 already paid for. Prefer extending the existing
  per-purpose/per-day row over adding a new per-job one if the design can make that work.
- **Item 4 (extended per-purpose stats)**: reuse the existing bounded diagnostic-RPC precedent,
  `stats(now, limit=20)` (`coordinator.js:1090-1189+`, already exposed at the authenticated
  `GET /v2/stats`, `index.js:308`) — do not invent a second reporting path. `ingress_purpose`
  (per-purpose daily `jobs_ingested`/`write_units`, already a bounded table) is trivial to add to
  `stats()`'s response. **Rejections are not persisted anywhere today** — `enqueueBatch` returns a
  `reason` synchronously to the caller and the client only emits a local structured event
  (`_emit_v2_dispatch_event`); there is nothing for `/v2/stats` to read back. Add a bounded,
  per-day-per-purpose-per-reason rejection counter (same `(utc_day, purpose)` shape as
  `ingress_purpose`, not a per-rejection row) and extend `test/rows-read.test.js` with the same
  seed-at-two-scales/`EXPLAIN QUERY PLAN`/mutation-test methodology for the new table before this
  ships.
- **Impact**: eliminates wasted B2 payload writes for jobs the Worker cannot admit; converts
  "the registry probably already handles crash recovery" into a proven, tested property that
  Initiative 4's checkpoint-skipping can safely rely on; gives operators real per-purpose
  admission/rejection visibility via the existing stats surface.

---

### Pillar 5: Architectural Consistency & De-Monolithization

#### Initiative 17: Modularize 4 Monolith Modules (<1,000 LOC per File) — L3
- **Problem**: `stages.py` (7,667 LOC), `run.py` (4,356 LOC), `compute/llm.py` (3,118 LOC), and
  `media.py` (3,691 LOC) are oversized monoliths that impede safe refactoring.
- **Namespace hazard**: do not create `citypods/stages/`, `citypods/media/`, or
  `citypods/compute/llm/` alongside same-named `.py` facades — Python import resolution would
  shadow the facade and can silently break imports. First extract into distinct implementation
  namespaces (for example `citypods/_stage_impl/`, `_media_impl/`, and `compute/_llm_impl/`) while
  the existing modules explicitly re-export the public API.
- **First-seam candidates, identified by actual coupling (fewest private-helper cross-references),
  not guessed**: in `stages.py`, `LinksStage` (lines 3114-3160, 49 lines, exactly **1** distinct
  private-helper call — the shared `_materialize_set`, used by every stage and not
  `LinksStage`-specific coupling; no I/O, fully self-contained business logic). In `media.py`,
  `AudioArtifact`/`AudioArtifactCache` (lines 2943-2996, ~53 lines combined, **zero** calls into
  any module-private helper — depends only on stdlib `threading` and its own dataclass):
  ```python
  class AudioArtifact:
      """Successful audio result shared by duplicate stable-meeting source views."""
      key: str; spec: str; url: str; duration: float | None; size: int | None; encoded_at: str | None

  class AudioArtifactCache:
      """Thread-safe run-local coalescing for identical stable-uid + audio-recipe work."""
      def __init__(self) -> None:
          self._condition = threading.Condition()
          self._canonical_sources: dict[tuple[str, str], str] = {}
          self._values: dict[tuple[str, str, str], AudioArtifact] = {}
          self._inflight: set[tuple[str, str, str]] = set()
      # register / canonical_source / claim / complete / abort
  ```
  Do **not** start with `TranscriptStage` (1,730 lines, 49 distinct private-helper references —
  the most deeply entangled class in `stages.py`) or `CommandFfmpeg` (509 lines, 24 distinct
  private-helper references — the most entangled in `media.py`); both are far riskier first moves.
- **Export-shim scope**: only `compute/llm.py` has an existing `__all__` (13 names, e.g.
  `LiteLLMBackend`, `dispatch_job_batch`) to preserve — `stages.py`, `run.py`, and `media.py` have
  none, so their re-export list must be enumerated from scratch by grep, not copied from an
  existing manifest. Import fan-in (rough count, `citypods/`+`tests/` combined) is highest for
  `citypods.compute.llm` (22 files) and lowest for `citypods.run` (5 files) — `run.py` is
  plausibly the safest of the four to restructure first with the fewest re-export shims needed,
  despite being the largest file overall.
- **Tests**: `tests/test_scripts_import.py` already has the exact pattern to model a new
  `citypods`-wide import-smoke-test on (`pkgutil.iter_modules(...)` +
  `@pytest.mark.parametrize` + `importlib.import_module`) — it currently covers only `scripts/`,
  not `citypods/`; extending it is new work, not an extension of existing coverage. Split one
  module per behavior-preserving PR, preserve import and CLI contracts, and use characterization
  tests plus the new import-smoke test. A file-size target is a review heuristic, not an
  acceptance criterion; no behavior change, circular imports, or unreviewable four-module move is
  acceptable.

#### Initiative 18: Duration-field consolidation — superseded
- **Disposition**: Do not schedule this work. H21 is already shipped in review/26: source and served
  duration are intentionally distinct clocks, with canonical `source_duration_seconds` and
  `served_duration_seconds` helpers, audio-owned normalization, compatibility reads, and a manual
  repair action. Collapsing them into one `duration_seconds` field would regress timeline-integrity
  checks and violate that accepted ownership model. Future duration changes belong in review/26.

---

## §5. Implementation Sequencing and Acceptance Criteria

The former fixed "11 PR / four sprint" schedule mixed discoveries, migrations, already-shipped
work, and independent refactors into unsafe bundles. The ordering below is a dependency order, not
a calendar commitment: every row is already L3 dev-ready per §4/§2/§3, so nothing here is waiting
on further design — pick a row, open it as its own issue/PR series, and use its stated criterion
as the definition of done, not as a precondition to start.

1. **[GH#1458](https://github.com/BashfulBits/city-meeting-podcasts/issues/1458) checkpoint
   profiling and dirty-source push.** Root cause and a representative trace are already recorded
   on the issue (Initiative 4) — this row is comparatively lower research risk than the rows
   below and can move to L3 fastest, provided the two live `DIAGNOSTIC` blocks are resolved or
   accounted for first. Complete only with no lost-update regression, a cancelled-PUT retry, and
   measured cold/warm improvement.
2. **[GH#1459](https://github.com/BashfulBits/city-meeting-podcasts/issues/1459) graceful timeout
   envelope.** The affected-jobs table and suggested step values are already recorded on the issue
   (Initiative 6), including the separate `asr.yml` gap (no job timeout at all). Also comparatively
   ready for L3. Complete only when each affected job defers before the hard timeout and
   failure/cancellation classifications remain distinct.
3. **v1 retirement discovery/drain.** Execute §3's consumer, R2, and deployed-Worker inventory
   against the exact 15-workflow/6-script list already named there. A maintainer must accept the
   drain report and v2-only canary before any production deletion (§3 step 5).
4. **State partitioning migration rehearsal.** Execute §2's ownership-table closure (the `moments`
   and `integrity` gaps), generation protocol, offline round trip, and rollback drill exactly as
   specified. A separate maintainer-approved maintenance window — not a normal PR — activates a
   verified generation (the pointer flip is the one step in this row requiring that sign-off).
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
10. **[GH#1463](https://github.com/BashfulBits/city-meeting-podcasts/issues/1463) admission-first
    and registry-first tag dispatch (Initiative 19).** Sequenced strictly after row 1
    (GH#1458/Initiative 4) — it builds on the same dirty-push mechanism and must not fork it.
    Complete only when the kill-and-resume test proves zero duplicate `enqueue_batch` calls and no
    lost completed results through `TagsStage`'s real recursive splitting path, the admission
    reservation's storage stays rows-read-bounded (same invariant class as
    `test/rows-read.test.js`), and the new rejection counter is bounded per `(utc_day, purpose)`
    rather than per-rejection.

Work that review/26 and review/34 already own stays there. Range slicing, OpenTelemetry, confidence
fallbacks, and a stage protocol remain backlog candidates until their measurement or product trigger
is recorded. None may be bundled merely to fill a sprint.

---

## §6. Testing, Verification & Golden Snapshot Plan

1. **Static Analysis & Linters**:
   - Every PR must pass `ruff check .` and `ruff format --check .` under the
     strict 100-character limit (the current `select = ["E", "F", "I", "UP", "B"]`; no annotation
     or type-checking rules are enabled today).
   - **No type checker is part of this repository's toolchain** (no `mypy`/`pyright` config, CI
     step, or `constraints/*.txt` entry). Do not assume Initiative 17's module extraction can lean
     on one: adding a type checker is itself a new-tooling decision gated by review/22's
     dependency policy (pinned constraints, CI wiring, `check_dependency_policy.py` coverage), and
     it would need its own evaluation of how much of the pre-existing code it touches by import
     already type-checks cleanly. Track it as its own candidate if wanted, not a blanket
     requirement on every extraction PR.
2. **Offline Test Suite Execution**:
   - Maintain 100% pass rate across the offline test suite (`pytest -q`, 3,780+ tests).
3. **Workflow Timeout Assertions**:
   - `tests/test_workflows.py` already asserts job timeouts for several long-running workflows; it
     must gain step-level timeout assertions for every workflow Initiative 6 touches, to prevent
     regressions ([GH#1459](https://github.com/BashfulBits/city-meeting-podcasts/issues/1459)).
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
- **`review/11-technical-design-roadmap.md`**: Catalog this as an **L3 dev-ready per-workstream**
  umbrella (updated in this same change) and point each selected initiative at its exact
  file/function references here rather than re-deriving them. Mark superseded overlap with
  review/26 and review/34.
- **`ROADMAP.md`**: Describe review/45 as an architectural program whose workstreams are each
  individually implementable, sequenced by §5 — not an adopted sprint schedule and not a completed
  state-store cutover (nothing here has shipped merely because this document is L3).
- **`ARCHITECTURE.md`**: Describe only the as-built state store and dispatch system. Update it after
  a partitioning or retirement change actually ships; do not document a target design as current.
- **`CHANGELOG.md`**: Record the reviewed planning decision accurately, including the L3 detailing
  pass. Add implementation entries only as individual PRs merge, including every
  pipeline-version/backfill disposition.
