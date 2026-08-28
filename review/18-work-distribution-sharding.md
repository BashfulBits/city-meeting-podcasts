# review/18 — Work distribution & sharding for distributed ASR workers

**Maturity: L3 implemented Phase-H breakout (H17/H19; H6b sharding × H14 external workers ×
[`review/17`](17-state-store-backend-evaluation.md) R2/CAS) · last updated 2026-07-10**

> How transcription work is partitioned across workers. Today: a single deterministic source-atomic
> **plan** computed in GitHub Actions and fanned out to a fixed matrix of identical shards. Tomorrow:
> heterogeneous workers — Modal, Beam, a Mac mini, AWS — that live **outside** GitHub Actions (H14b/c)
> and cannot be handed a `--shard K/N` index. This doc designs the path between the two so the
> external-worker contract is fixed **before** H14b/H14c are built, avoiding a rewrite of the GPU-worker
> side later. It builds on [`review/17`](17-state-store-backend-evaluation.md) (the R2 + CAS coordination
> substrate this needs) and the scaling envelope in [`review/16`](16-scaling-review-plan.md). Canonical
> phase placement lives in [`review/11`](11-technical-design-roadmap.md); an item enters the active
> roadmap only when its trigger fires and the doc-update contract promotes it.

## Status & maturity

| Sub-item | Maturity | Disposition |
|---|---|---|
| **Stage 1** — per-`(source,uid)` transcribe planning + per-episode owned-block merge | **Implemented** | **Shipped** ([GH#390](https://github.com/BashfulBits/city-meeting-podcasts/issues/390) PR2): `merge_preserving_foreign(owned_uids=)`, `pending_transcribe_items`, `ShardPlan.unit`/v2, `episodes_for_shard`. The one load-bearing change (the merge); the rest composed around it. |
| **Stage 2** — pull-based work ledger (CAS leases on R2); workers claim episodes | **Implemented (contract + both worker classes live)** | **GH#390 PR4 + H19 follow-through.** `citypods/ops/work_leases.py`: per-item CAS lease objects + `claim`/`renew`/`release`/`reap` (the frozen §4.2 contract), reaped by `compute reconcile`; `work-leases/` routed to R2; cost-disciplined (§4.6). External Modal/Beam workers first proved the contract live. H19 then moved the in-Actions transcribe path onto the same ledger: `asr.yml` now runs identical internal pull workers, and `compute reconcile` rebuilds the manifest from canonical records before sweeping leases, breaking the old coupling where queue ordering changed only when a Stage-1 enrich shard happened to refresh `state/work.json`. The `run_claim_loop` orchestration is now shared enough for both worker classes, but worker-specific admission/supervision still lives one layer up: external workers keep provider-budget pacing and long-meeting preference; internal workers keep local timeout/backstop supervision, hard local-duration admission, and dynamic "fits before backstop" gating. The earlier `work_lease_reaper_enabled` root-vs-`defaults` bug remains part of this stage's shipped history: it had silently disabled `reap_work_leases()` in production until fixed while closing out [GH#706](https://github.com/BashfulBits/city-meeting-podcasts/issues/706). |
| Audio lane → per-`(source,uid)` | — | **Rejected** — stays source-atomic (per-source provider leases / rate limits make the source the right unit; §2.3). |
| Static plan retained for transcribe long-term | — | **Superseded by Stage 2** once external workers land; kept as the in-Actions interim. |

Stage 1 plus the review/17 CAS/router substrate are now **H17** implementation work. Before H14b/H14c,
Stage 2 must be matured to L3 and implemented far enough that external workers use the pull/claim
contract from their first production version.

---

## §1. Problem & scope

### 1.1 What exists today (H6b + H14a)

Transcription is partitioned by a **deterministic, source-atomic plan**:

- `reconcile` (in `asr.yml`) restores the B2 state snapshot **once**, then `citypods compute plan-shards`
  emits a versioned `ShardPlan` (`citypods/sharding.py`) and uploads snapshot + plan as one immutable
  artifact.
- Each of the 4 matrix shards downloads that same artifact and calls
  `sharding.sources_for_shard(...)` → the subset of `source_key`s it owns.
- The unit of ownership is the **distinct `source_key`** (`records.shard_assignment`): a source goes to
  exactly one shard, weighted by `estimate_transcribe_shard_work` (recording-duration proxy). Within
  that, heaviest-source-first onto the lightest shard.
- On push, a shard writes back **only its owned sources** (`statesync.push_records_merged`) and the
  cross-lane lost update is closed by **foreign-block preservation**
  (`records.merge_preserving_foreign` + `protected_blocks_for_lane`): a transcribe shard takes `local`'s
  `transcript` block but preserves `remote`'s `audio` block, so a late ASR run can't erase freshly hosted
  audio (review/12 §H6).

This is correct and race-free **because of one structural invariant**:

> **Invariant (current).** For any `episodes.json` file, **exactly one writer** touches it in a given run
> window. Source-atomic assignment enforces it by construction; the block-level merge only relaxes it
> along the *block* axis (audio vs transcript), never the *uid* axis.

### 1.2 Why source-atomic breaks down

Two pressures, one near, one structural:

1. **Skew (near-term, review/16 scale).** A single Granicus source can hold **2,192 pending episodes**
   while its 3 siblings hold a handful. Source-atomic ownership pins that whole source to one shard; the
   other three idle. Transcription is the expensive lane — that is the throughput we most want to spread.
   (The audio lane does not have this problem the same way — see §2.3.)

2. **Heterogeneous external workers (structural, H14b/H14c).** A static plan assumes **homogeneous,
   enumerable** shards: equal-weight partition, fixed count `N`, each addressable by index `K`. External
   GPU workers violate every assumption:
   - **Unpredictable throughput** — a Modal A10G and a free Beam slot clear backlog at different rates;
     an equal partition starves the fast one and overloads the slow one.
   - **Not enumerable at plan time** — the `reconcile` job cannot know how many external workers will be
     alive, or hand them a `K/N`. They come and go.
   - **Partial failure is normal** — with many workers, one dying must let another pick up its dropped
     work *within the same cycle*, not "skipped until next run."

   A statically planned partition cannot adapt to any of these. The natural model is **competitive claim
   from a shared ledger** — which the H5 manifest already anticipated ("Deferred to H6b/H9: competitive
   lease acquisition + per-item persistence") and H14a already half-built (dispatch leases, `seed_leases`/
   `overlay_leases`, `compute reconcile` reaping expired leases).

### 1.3 Scope

- **In scope:** how transcribe/align/diarize work is partitioned and how the owning writer commits its
  result without clobbering siblings, for both in-Actions matrix shards and external workers.
- **Out of scope:** the storage substrate itself (R2 router + `put_cas` — that is
  [`review/17`](17-state-store-backend-evaluation.md)); the inference backend interface (H13); media/blob
  layout (content-addressed, unchanged); the audio lane (stays source-atomic, §2.3).
- **Hard constraint:** the design must hold when a writer is **not** a GitHub Actions job — no `K/N`, no
  matrix, reachable only via S3 creds to B2/R2 (review/17 §5 external-worker access).

---

## §2. The unifying model: ownership is a token

Both the static plan and the pull ledger answer the **same** question: *who holds the right to write
`uid X`'s transcript block this cycle?* They differ only in **how the token is issued**:

| | Static plan (Stage 1) | Pull ledger (Stage 2) |
|---|---|---|
| Token issued by | `reconcile` computes a partition up front | a worker **claims** it via CAS at runtime |
| Token shape | plan entry `{(source,uid) → shard}` | a held **lease** `{uid → owner, expiry}` |
| Membership | fixed `N` homogeneous shards | open set of heterogeneous workers |
| Adapts to worker speed | no | yes (fast workers claim more) |
| External-worker capable | no (needs `K/N`) | **yes** |
| Reproducible / debuggable | **yes** (every shard agrees) | no (race-ordered) |
| Failure recovery | next run | within-cycle (lease expiry → re-claim) |

The crucial consequence:

> **The write path is identical in both.** Whoever holds the token for `uid X` writes the `transcript`
> block for `X` **only**, and preserves `remote` for every other uid. In Stage 1 the token is "uid X is
> in my plan slice"; in Stage 2 it is "I hold uid X's lease." So the **per-episode owned-block merge
> (§3.2) is the single mechanism both stages stand on** — building it now is not throwaway work, it is
> the exact commit path the external workers need. This is the answer to "nail it down before building
> external workers so we don't rework the GPU side."

### 2.1 Why the current merge is unsafe the moment a uid axis appears

`merge_preserving_foreign(remote, local, protected)` today takes `local`'s record wholesale for **every
uid in `local`**, only swapping back `protected` (foreign) blocks. That is safe *only* under the §1.1
single-writer invariant. The moment two transcribe workers split one source, each holds the **whole
source** in its `local` (both pulled the full snapshot), so each `local` carries a **snapshot-stale**
`transcript` block for the uids it does *not* own. Last writer wins → it overwrites the sibling's
**fresh** transcript with the snapshot value. This is the exact lost update the reviewer flagged, and it
is identical whether the second writer is a sibling matrix shard or an external GPU worker.

### 2.2 New uids discovered after planning

A static `(source,uid)` plan is a snapshot-time partition. An episode published *after* the snapshot is
in **no** shard's slice. Rule:

> **Unplanned-uid rule.** A uid absent from the plan is **owned by no one this cycle** and is deferred to
> the next planning cycle. This is benign for transcribe: a brand-new episode almost never has hosted
> audio yet (the audio lane is a separate, earlier workflow), so it is not transcribable this run
> regardless. The merge must therefore **never** write an artifact block for an unowned uid (§3.2).

(Under Stage 2 this disappears: `reconcile` rebuilds the discovery index every cycle and any pending uid
is immediately claimable.)

### 2.3 Why the audio lane stays source-atomic

Per-uid splitting helps transcribe because each episode is **independent** GPU work with no per-source
coupling. Audio is different: the audio lane is throttled **per source** — `max_encodes_per_source`, the
Granicus per-source media leases (`provider_distributed_leases`), and the provider circuit breaker all key
on the source/provider. Splitting one source across audio shards would have multiple shards contend for
the same per-source lease and fight the same rate limiter — more coordination, no parallelism win
(the bottleneck is the provider, not the runner). **Audio and the unscheduled `align` fan-out by source;
only the transcript-producing lanes (`transcribe`, future `diarize`) go per-uid.**

---

## §3. Stage 1 — per-`(source,uid)` planning + per-episode merge (dev-ready)

The immediately shippable change. Spreads a hot source across all shards **and** installs the
ownership-aware write path Stage 2 reuses. Audio/align unchanged.

### 3.1 Planning at episode granularity

- `records.estimate_transcribe_shard_work` already iterates a source's records and classifies each as
  `local`/`dispatch`/`blocked`/`inflight`. Add a sibling that **emits the pending items**, not just an
  aggregate: `pending_transcribe_items(state_dir, key, ...) -> list[(uid, weight_seconds)]`, where weight
  is the recording-duration proxy already used (`audio_duration_served or duration`). Only **pending**
  uids enter the plan, so plan size tracks **backlog**, not catalog size (the 2,192-episode source
  contributes 2,192 entries *only while* that many are pending; a caught-up source contributes none).
- `sharding.create_shard_plan` for `lane == "transcribe"`: build composite keys
  `f"{source_key}/{uid}"`, weight per uid, and run the existing
  `records.shard_assignment` (already generic over opaque string keys — heaviest-first onto lightest
  shard). `audio`/`align` keep keying on `source_key` exactly as today.
- `ShardPlan` gains a `unit: "source" | "episode"` field; bump `SHARD_PLAN_VERSION` to `2`
  (`load_shard_plan` rejects v1 — the reconcile job always emits a fresh plan per run, so there is no
  durable v1 artifact to migrate).
- Cross-source alias rule (production follow-up after ASR runs 26/27): if multiple pending
  source-local records have the same stable `uid` and fresh-transcription recipe, retain every
  `(source,uid)` entry so every record has an owner, but assign the group to one shard and charge its
  inference weight once. Fresh-ASR recipe identity includes the stable `author + body + title` prompt,
  language, compute type, and beam size, so aliases coalesce only when every inference-affecting input
  matches. `TranscriptStage` keeps source-scoped durable keys and uses a thread-safe run-local artifact
  cache with per-key in-flight reservations to fan one result out to all aliases even if more than one
  ASR worker permit is configured. This changes neither `ASR_PIPELINE_VERSION` nor automatic
  invalidation: existing current-version transcripts are left as-is; pending items use the complete
  recipe.
- Plan-size sanity: at review/16's ~3,000-source scale the pending set is bounded by what the free-tier
  ASR throughput can clear per cycle (review/12 §H9 gate: 80-feed backlog < 1 month), i.e. thousands of
  16-char uids = tens of KB of JSON in the workflow artifact. Comfortable.

### 3.2 The one load-bearing change — owned-block merge keyed by ownership

`records.merge_preserving_foreign` gains an optional ownership set:

```python
def merge_preserving_foreign(
    remote: dict,
    local: dict,
    protected: frozenset[str],
    *,
    owned_uids: frozenset[str] | None = None,
) -> dict:
    merged = {uid: dict(rec) for uid, rec in remote.items()}
    for uid, local_rec in local.items():
        # uid this run does NOT own: never write our snapshot-stale artifact for it.
        if owned_uids is not None and uid not in owned_uids:
            if uid not in remote:  # newly discovered, unowned (§2.2)
                merged[uid] = {k: v for k, v in local_rec.items() if k not in ARTIFACT_BLOCKS}
            # else: keep remote as-is (already copied above) — a sibling owns/writes it.
            continue
        rec = dict(local_rec)  # owned: today's behavior
        remote_rec = remote.get(uid)
        if remote_rec:
            for block in protected:  # still preserve cross-lane foreign blocks
                if remote_rec.get(block):
                    rec[block] = remote_rec[block]
        merged[uid] = rec
    return merged
```

`owned_uids=None` reproduces today's behavior exactly (a source-atomic shard owns every uid in its
`local`), so **audio/align and the unsharded full enrich are byte-for-byte unchanged**. Only the
transcribe path passes a real set. The rule composes two preservation axes: **across blocks** (audio vs
transcript, existing) and now **across uids** (sibling-owned, new). Stage 2 calls this with
`owned_uids={X}` — the single uid whose lease the worker holds.

### 3.3 Wiring

- `sharding.sources_for_shard` → `episodes_for_shard(plan, lane, shard_index, ...)`: for `unit==episode`,
  return both the owned `(source, uid)` pairs and the set of sources any owned uid touches. For
  `unit==source` it is today's function. Keep the fail-closed validation (plan lane/`num_shards`/source
  coverage) — matrix shards consume one immutable artifact, so an exact-match check on the planned set
  remains valid (drift is handled by the §2.2 rule, not by recompute).
- `run.py` (~`build()` shard block, currently citypods/run.py:1084–1114): keep every source with **any**
  owned uid; thread `owned_uids` into (a) the H5 transcript-pass work queue so the shard transcribes only
  its uids, and (b) the push.
- The transcript pass (`citypods/ops/workqueue.py` two-pass enrich + `stages.LANE_STAGES` gate) gains an
  `owned_uids` filter so an unowned uid is skipped even though its source is loaded for render context.
- `statesync.push_records_merged` threads a per-source `owned_uids` into the merge call
  (citypods/statesync.py:165).
- `asr.yml`: unchanged shape — `reconcile` still emits the plan (now `unit=episode`); shards still
  download it and pass `--shard-plan`. Stage 1 is invisible at the workflow level.

### 3.4 Tests

- `test_sharding`: transcribe plan over a skewed source is **disjoint + exhaustive over pending uids**
  and spreads one big source across all shards; audio plan stays source-atomic; v1 plan rejected.
- `test_records`: **the reviewer's race** — two shards split one source; shard A writes uid1's transcript,
  shard B (owning uid2) must not regress uid1. Assert `merge_preserving_foreign(..., owned_uids={uid2})`
  preserves remote's fresh uid1 transcript. Plus: unowned new uid contributes provider fields but no
  artifact block; `owned_uids=None` is identical to pre-change output.
- `test_statesync`: two same-source scoped pushes interleaved → neither clobbers the other's transcript.

---

## §4. Stage 2 — pull-based work ledger (target architecture)

When external workers land (H14b/H14c), the static plan can no longer issue tokens (§1.2). Replace it,
**for the transcript lanes only**, with competitive claim from a shared ledger on R2/CAS.

### 4.1 Two artifacts: a read-only index + a hot lease ledger

Separating "what work exists" from "who is doing it now" maps cleanly onto review/17's tiering and avoids
a CAS retry-storm on one monolithic object:

1. **Discovery index** — rebuilt by `reconcile` each cycle from the restored snapshot; the H5 manifest
   (`state/work.json`, `citypods/ops/workqueue.py`) **already is this**, ordered by the configured policy
   (`recency:{desc, within_days:30}`). Low-contention, read-mostly; can stay as-is. Workers read it to
   find candidate uids newest-first. It carries **no** mutable claim state.
2. **Lease ledger** — per-item objects `work-leases/<source>/<uid>.json` on **R2 with CAS**, each
   `{uid, source_key, state: queued|leased|done|failed, owner, lease_expiry, attempts, pipeline_version}`.
   Per-item objects give **independent ETags** so concurrent claims of *different* uids never contend —
   the explicit mitigation for review/17 §6 "CAS retry-storms on hot keys." (A single `work.json` under
   CAS would serialize every claim on one ETag.)

This is the H5 "competitive lease acquisition + per-item persistence" deferral, now concrete, and the
generalization of H14a's dispatch leases: today a lease tracks work an in-process `DispatchCoordinator`
*pushed* to an external backend; here the lease **is** the claim primitive for **all** workers.

### 4.2 The claim protocol (one verb set, every worker)

```
1. read discovery index  → candidate uids, policy-ordered (newest-first)
2. for each candidate until budget:
     read  work-leases/<src>/<uid>.json  (+ ETag)
     if state in {queued, (leased and lease_expiry < now)}:
        put_cas(state=leased, owner=me, lease_expiry=now+ttl, attempts+1, if_match=ETag)
            412 → someone else won; next candidate
            200 → claimed
     fetch audio by URL (B2, free egress)  →  run inference (H13 backend)
     write transcript artifact (content-addressed VTT/words.json → B2, unchanged)
     commit record: merge_preserving_foreign(remote, {uid: rec}, protected, owned_uids={uid})   ← §3.2
     put_cas(state=done, if_match=ETag)   # release
3. renew lease (CAS bump expiry) for any long-running inference before it expires
```

`reconcile` reaps leases whose `lease_expiry < now` back to `queued` (a crashed worker's work re-enters
the pool) — `compute reconcile` already does exactly this for dispatch leases. The completion write is
**§3.2 with `owned_uids={uid}`** — the lease *is* the ownership token.

### 4.3 GitHub Actions shards become just another worker

With a ledger, the `asr.yml` matrix becomes `N` **identical** jobs each running the claim loop until
its wall-clock budget — no `--shard K/N`, no plan artifact, no fan-in. They self-balance (a fast job
claims more) and are homogeneous with external workers. The `reconcile` job shrinks to: rebuild the
discovery index + reap expired leases. This **deletes** the Stage-1 plan-emit/download/validate
machinery for transcribe (it remains for the source-atomic audio lane).

That migration is now implemented. The shared orchestration lives in
`citypods.compute.external_worker`, but the clean boundary is **not** "one giant worker path full of
conditionals." The shared layer owns claim/adopt/write/settle mechanics; the worker subclass owns
admission and supervision:

- External workers keep provider-budget pacing, preferred-day freshness policy, and the
  `prefer_min_duration_hours` hook that can still float long meetings to Modal/Beam first.
- Internal workers keep the hard `asr_local_max_duration_hours` guard, shorter-known-item ordering,
  fit-before-backstop admission using the same learned runtime-estimate substrate, and a killable
  local daemon so a timeout, SIGTERM, or queued replacement run can stop the in-flight local claim
  gracefully and hand it back to the queue.

### 4.4 Scheduled-run coalescing is optional, not the ownership model

The pull ledger plus H11c checkpoints absorb the correctness concern behind former GH#340: expensive
work is durable, exclusively claimed, and reclaimable after lease expiry rather than lost when a new
schedule arrives. A later thin cron admission workflow may skip dispatch when a healthy recent run is
already draining the same queue, while allowing manual/code-changing work to supersede routine
schedules and refusing to let stale runs suppress future work. That optimization reduces runner setup
cost; it does not issue ownership tokens and is not required before H14.

### 4.5 Records stay on B2 (review/17 swing case — decided) — and why the merge already suffices

The `episodes.json`→R2-CAS option is **not the chosen path**: review/17's swing case is decided in favor of
**records on B2 → managed search-DB at Phase R** (no B2→R2→DB double migration). The reason it is safe
without CAS is exactly the ownership model below: under Stage 2 each uid's record block has a **single
writer** (its lease-holder), so the §3.2 owned-block foreign-preserving merge commits it race-free on B2 —
the cross-**uid** race CAS would have guarded against cannot occur. The lease **ledger** is control-plane
and lives on R2; only the **record write** stays on B2.

*Historical note (not the chosen path):* had `episodes.json`→R2-CAS (or the §6 "per-stage object files",
`transcript/<source>/<uid>.json`) landed, the §4.2 commit would have become a CAS read-modify-write of just
the owning uid's object, dissolving the cross-block rule entirely. Retained only to document the discarded
end-state.

### 4.6 Keeping R2 Class A low with per-item leases

Per-item granularity and a low Class A bill are in tension **only if contention and listing drive the
writes**. R2 meters **Class A = writes *and* lists** (1M/mo free, $4.50/M after); Class B = reads (10M/mo
free); B2 writes are **free** (review/17 §1.3). So the discipline is: spend a Class A op only on the **one
atomic act that needs CAS — the claim** — and push everything else onto Class B or B2. Four moves, by
impact:

1. **Discover from the B2 index; never `list` the R2 lease prefix.** The H5 manifest (`work.json`,
   rebuilt by `reconcile`) already enumerates pending uids in policy order. Workers read it (one Class B
   GET, cacheable) and **derive** each lease key deterministically (`work-leases/<src>/<uid>.json`). No
   `list_objects` over `work-leases/`. Listing is the sneaky Class A cost; this removes it outright — the
   single biggest win.
2. **Read-before-claim + per-worker scan offset.** A blind `put_cas` that loses *still* costs a Class A
   on the 412. Instead GET the lease first (Class B) and `put_cas` only if it looks claimable, so M racers
   become **1 Class A write + (M−1) cheap Class B reads**. Each worker starts scanning the ordered index
   at a different offset (hash of worker-id / jitter) so N workers target N different items first → most
   claims succeed first try → **~1 Class A per *claimed* item, not per *attempt*.** Eliminating
   failed-claim writes is the dominant steady-state saving.
3. **Drop the per-item "done" write; infer completion.** Keep **records on B2** (transcript-block write =
   free) and the transcript artifact on B2 (free) — those *are* the durable completion signal. Let
   `reconcile` sweep "lease still `leased` but artifact exists → `done`" (it already walks leases to
   reap). The worker writes only the free B2 record; one Class A per item disappears.
4. **Generous TTL so renews are rare.** Set lease TTL above p95 inference time; only long-audio jobs
   renew (§5). Renew becomes an exception, not a routine op.

Net steady state: **≈ 1 Class A op per completed transcript** (the claim), comfortably inside the 1M/mo
free tier at review/16 scale (thousands of transcripts/mo). Couple with review/16 **S2** (dirty-only
writes) so nothing else leaks Class A.

> **Escape hatch if per-item claim Class A still dominates: batch claims.** Claim K items per CAS
> (`work-leases/batch-<n>.json`) for K× fewer Class A. Batch **across the global policy-ordered queue, not
> by source** — per-source batching reintroduces contention on exactly the skewed 2,192-episode source
> Stage 1 set out to spread. Bound batch size (adaptive to recent throughput) so a dead worker strands
> little before the reaper recovers it.

### §4.7 Active-lease index (GH#1018) — implemented

§4.6 keeps *claiming* cheap, but the reaper side (`reap()`, `compute reconcile`'s Stage-2 sweep) still derives
every candidate `(source_key, uid)` from the discovery index and GETs its lease key — cost proportional to the
**entire backlog**, not to how much work is actually claimed. With a sparse active set (few claims outstanding)
this is thousands of pointless Class-B reads per reconcile; live runs measured ~9–11 minutes probing 6,034
candidate keys for zero active leases.

**Design (matches the issue's preferred option): fixed/sharded CAS-managed active-set buckets**, not a second
unbounded list. `ops/work_leases.py` adds:

- `INDEX_BUCKET_COUNT` (64) fixed objects, `work-leases-index/bucket-<n>.json`, each a flat
  `{entry_id: {source_key, uid, lease_expiry}}` map. Bucket assignment (`index_bucket_for`) is a stable sha1
  hash of the lease key — the same hash family as `scan_offset` — so it never drifts and never needs a
  rebalance step.
- `claim()`/`renew()`/`release()`/`abandon()` take an opt-in `update_index: bool = False` (default off, so
  every existing cost-invariant test and caller is unchanged): on, they mirror the claim into its bucket
  (add/refresh on claim/renew, remove on terminal release/abandon) via a CAS read-modify-write with retry
  (`_mutate_index_bucket`, same shape as `compute.budget.mutate_budget`) so a concurrent sibling's entry in the
  same bucket is never dropped, just retried. Both worker classes (`external_worker.py`'s external and internal
  loops) pass `update_index=True` at every claim/renew/release/abandon call site.
- `reap_indexed()` reads exactly `bucket_count` bucket objects (bounded, independent of backlog size), and for
  every entry found **re-reads the real lease object** — the index is never trusted as claim authority (the
  lease object is; acceptance criterion 7) — before applying the identical settle/requeue/leave decision
  `reap()` uses (both now share `_settle_leased`, so the two sweep strategies can't disagree on what "reap this
  lease" means). Settled or drifted (indexed but no longer actually `leased`) entries are removed from the
  index; still-running entries stay. Zero active leases ⇒ `bucket_count` Class-B reads and **zero** candidate
  lease reads; N active leases ⇒ `O(N + bucket_count)`.
- **Crash recovery / integrity sweep.** A crash between a successful claim and its index write would otherwise
  hide that lease from `reap_indexed()` forever. Callers may pass `integrity_candidates` (the same
  policy-ordered list `reap()` already derives) and `integrity_partition` (rotated per run — `reconcile_compute`
  uses `work_leases.integrity_partition_for(now)`, a **minute**-granularity counter; `now.toordinal()` was tried
  first but only advances once a day, so every reconcile on the same UTC day probed the identical slice —
  full-keyspace recovery in ~64 *days*, not the ~64 *runs* this design promises); `reap_indexed()`
  candidate-probes only the slice of `integrity_candidates` that hashes to that one partition, repairing any
  found-but-unindexed active lease back into the index. One bounded partition per run, not a full backlog scan —
  full coverage recurs within `bucket_count` minutes (in practice, `bucket_count` runs, since reconcile fires at
  most a few times a minute).
- **Rollback.** `reconcile_compute(..., use_lease_index=True)` (default once `sweep_work_leases` is on) picks
  `reap_indexed()`; `use_lease_index=False` (`work_lease_index_enabled: false` under `defaults:`) reverts to the
  original `reap()` candidate-probe with no code change — the config-only rollback the issue asked for.

Kept deliberately out of scope: this does **not** introduce a second unbounded structure (no `list_objects` on
the lease or index prefixes, ever) and does not change the frozen `claim`/`renew`/`release`/`reap` §4.2 contract
signatures beyond the additive `update_index` keyword — existing non-indexed callers pay exactly the same cost
as before.

### §4.8 Batched transcript-record commits (GH#1019) — implemented

§4.5 keeps the per-uid **claim** cheap, but each *commit* still pays the full cost the swing-case
decision in §7 accepted: `push_records_merged()` does a whole-source fetch+merge+put of
`sources/<src>/episodes.json` per call, and until now `external_worker.py`'s success path
(`_run_transcribe_item`) called it **once per completed episode**. On the largest inspected source
(~5,480 records, 59 of one run's 93 successes) that is 59 whole-file B2 round-trips to durably
record 59 single-uid deltas — the archive-sized transfer scales with total historical episodes, not
with what actually changed, and gets worse every time an append-only feed grows.

The issue proposed two options: **(A)** per-episode/lane sidecars — turn the canonical source record
into an index/pointer over content-addressed or UID-keyed per-uid objects, composed on read — or
**(B)** same-source commit batching — queue successful deltas locally and commit one merge per source
per bounded interval/end-of-run, preserving leases until the batch lands durably. Option A would
finally deliver the "per-stage object files" end-state §4.5 explicitly deferred (H17/GH#390) — but
that discarded end-state was deferred *on purpose*: it changes the physical record layout, and
GH#1019's own Phase R sequencing note says exactly that layout choice should be co-designed with the
R6/R7 speaker/attendee record shapes so it isn't migrated twice. Option B does not eliminate
archive-sized transfer (a flushed batch still fetches+merges+puts the whole source file), but it makes
the number of times that happens proportional to *batches*, not *episodes*, entirely inside
`external_worker.py` with zero record-layout or schema change — safe to land now, and it does not
foreclose Option A later. **Decision: ship Option B now. §4.9 investigates Option A against R6/R7's
actual record-shape additions and the real Backblaze B2 cost model, and finds it isn't currently
justified on cost grounds — see §4.9 for the worked numbers and what would actually trigger revisiting
it.**

**Design.** `ExternalTranscribeWorker` (base class shared by both the Modal/Beam external workers and
`InternalTranscribeWorker`) gains a per-run, in-memory pending batch:

- `_pending_transcript_records: dict[source_key, dict[uid, record]]` and `_pending_transcript_since:
  float | None` (monotonic timestamp of the oldest unflushed record). `_run_claim_loop` claims and
  processes one item at a time (no concurrency in this loop — the GPU-characterization canary path is
  separate and does not persist records at all), so a plain dict needs no locking.
- The success path in `_run_transcribe_item` (fresh transcription *and* adoption — both durably persist
  the owned block) now calls `_queue_transcript_record` instead of pushing immediately:
  `save_records` (local, ephemeral-worker-filesystem) still runs synchronously as before; the record is
  added to the pending batch instead of pushed remotely.
- The batch flushes — one `push_records_merged()` call covering every queued `(source, uid)`, still
  scoped by `owned_uids` exactly as the single-item call was — on whichever bound is hit first:
  `_TRANSCRIPT_BATCH_MAX_ITEMS` (5) queued records, or `_TRANSCRIPT_BATCH_MAX_SECONDS` since the oldest
  queued record, or unconditionally at end of run (`run()`'s `finally`, best-effort — a failure there is
  logged, not raised, since the artifacts are already durable and a later run's adoption path re-queues
  the same records).
- **Post-ship fix: the age bound must exceed every backend's own per-item floor, not just look small.**
  The first shipped value (120s) was picked off the `_CHECKPOINT_INTERVAL_SECONDS = 180.0` precedent in
  `run.py` without checking it against this loop's own timing model. `config/site_config.yml` sets
  `min_runtime_seconds` to 180–240s for **every** backend (Modal, Beam, GitHub Actions) — every single
  transcription takes at least that long, which is longer than 120s. So by the time a second item
  finished and was queued, the age bound had already tripped, capping real-world batches at ~2
  regardless of `_TRANSCRIPT_BATCH_MAX_ITEMS` — a real reduction from the pre-#1019 baseline, but a
  fraction of the intended 5×. Fixed by raising `_TRANSCRIPT_BATCH_MAX_SECONDS` to 1800s: the age bound
  is meant as a backstop against a sparse/slow source leaving a batch open, not the thing that should
  drive typical batching, so it needs to sit comfortably above per-item duration, not near it. A
  regression test (`test_transcript_batch_survives_realistic_per_item_gaps`) locks in a realistic
  240s-per-item gap across a full 5-item batch so this can't silently regress again.
- **Lease preservation without new machinery.** The issue calls out that leases must be "preserved
  until durable batch commit." Rather than add a second keepalive thread, this relies on the existing
  §4.2 lease lifecycle: `lease_ttl_seconds` defaults to 6–20 *hours* (`ExternalWorkerConfig`), while the
  batch's own bounds cap the wait at 5 items or 1800 seconds — and the loop's per-item renewal thread
  (`_run_with_renewal`) renews the lease every `_renew_interval()` (60–300s) up until the moment the item
  completes and is queued. A queued-but-unflushed item's lease is therefore always minutes-fresh
  relative to an hours-long TTL, comfortably inside the flush window with no extra renew call. Per
  `_run_claim_loop`, success never explicitly releases the lease anyway (§4.6 point 3 — completion is
  inferred from record+artifact presence), so a queued record sits exactly as "leased" as it always did;
  batching changes *when* the record commits, not the lease's existing liveness contract.
- **Measurement, not estimation.** Each flush now logs `sources=`/`records=`/`payload_bytes=`/
  `elapsed_s=` (`[<backend>-worker] transcript-batch-flush ...`) — real production numbers for whatever
  later re-evaluates §4.9's decision, rather than the back-of-envelope figures that decision was worked
  from.
- **Partial-failure retry-idempotency.** `_flush_pending_transcripts` only clears the batch on a fully
  successful push; a failed flush (transient remote-read error, or a fail-safe per-source skip) leaves
  every queued record in place, so the *next* queue call (or the end-of-run flush) retries the same
  records — `merge_preserving_foreign`'s owned-block merge (§3.2) is idempotent, so re-pushing an
  already-pushed record is a no-op, not a corruption risk.
- Unaffected on purpose: the media-decode-quarantine (`_quarantine_media_decode`) and
  timeout-backoff (`_record_timeout_backoff`) paths keep pushing immediately via the original
  `_push_owned_transcript_record` — both are single-item, already-exceptional paths where batching would
  add lease-scoping complexity for no measurable win.

**Acceptance criteria mapping.** A batch of `_TRANSCRIPT_BATCH_MAX_ITEMS` successes on one source now
costs one fetch+merge+put instead of `_TRANSCRIPT_BATCH_MAX_ITEMS`, independent of source length — a
5–10× reduction in the steady state this worker loop drives, growing with run size up to the per-batch
cap; concurrent audio/ASR lanes keep their field ownership (the merge call is unchanged, just batched);
legacy single-record pushes remain byte-identical when a batch happens to contain exactly one uid; nothing
about `episodes.json`'s on-disk shape changed, so there is no migration/rollback to plan (this is the
Option-B slice — Option A is the one that would need a schema/versioned dual-read migration plan, per
the issue's own criteria, and is examined next).

### §4.9 Option A investigated against R6/R7 and real B2 pricing — deferred, not a cost win

GH#1019's own text suggested revisiting Option A (per-episode/lane sidecars) "once R6/R7 stabilize," on
the theory that those items' new record fields would settle the physical layout question before paying
for a migration. Investigated directly rather than deferred on schedule — the finding changes the
recommendation from "revisit later" to "not currently justified, and re-opened by a different trigger
than R6/R7 shipping."

**What R6/R7 actually add (checked against their own L3 docs, not assumed):**

- **R6** ([`review/36`](36-llm-first-cards-summaries-soundbites.md) §2, superseding the deprecated
  `review/30`) adds four **inline** `Episode` fields — `moment_summary_candidates` (one per chapter),
  `moment_pullquote_candidates` (≤10), `moment_decision_candidates` (≤20), `moments_llm_recipe_hash` —
  plus a new `"moments"` `ARTIFACT_BLOCKS` entry. Sized from the schema's own field limits: roughly
  4–7 KB (summary points, chapter-count-scaled) + up to 7.5 KB (pull-quotes) + up to 15 KB (decisions) ≈
  **+5–15 KB/episode typical, +26 KB worst case.**
- **R7** ([`review/31`](31-speaker-diarization-attendee-extraction.md) §A.3/§B.4) keeps its bulky part —
  per-turn speaker segments — in its **own sidecar** (`transcripts/<src>/<uid>-diarize-<spec>.speakers.json`),
  exactly the sidecar pattern GH#1019 Option A wants generalized; only small pointer/provenance fields
  (`speakers_source`, plus the already-existing `speakers_*` pointer fields) and `attendees: list[str] |
  None` land inline — under 1 KB added.
- **Combined: R6 alone will roughly 2–3× today's measured ~5 KB/record average** (measured directly from
  three cached production `episodes.json` files: 2,338–5,672 bytes/record) **to ~10–20 KB.** This does
  make each Option-B flush's payload bigger — but see the cost model below for why that doesn't change
  the recommendation.
- `external_worker.py:107` already reserves `RESERVED_WORK_CLASSES = frozenset({"transcript-diarize"})`
  — R7's diarization is planned to run through this exact same per-episode claim-loop/commit path, so any
  future Option A (or further Option B tuning) needs to cover the `diarize` lane too, not just
  `transcribe`.

**The real constraint: Backblaze B2 pricing, re-verified 2026-07-26 (review/17 §1.3's prior "Class B/C,
2,500/day free then metered" reading was stale — corrected there and here).** B2 Class A/B/C API
transactions are **entirely free, no tier limit**. The actual metered dimension is **egress bytes**: free
up to **3× the account's average monthly storage**, then $0.01/GB.

**Worked from this project's own real numbers** (an archived `run_summary.json` snapshot, not invented):
84 cities; one run materialized 192 episodes at 7,392,822,387 bytes ⇒ **≈38.5 MB/episode** average
encoded audio; 2,225 episodes already materialized (`reused`+`credited`+`encoded`; `backlog` isn't stored
yet) ⇒ **current total B2 storage ≈ 2,225 × 38.5 MB ≈ 86 GB** (audio-dominated; transcripts/records are
KB-scale and don't move this figure). **Free egress ceiling ≈ 3 × 86 GB ≈ 258 GB/month.**

Demand side, **unbatched** (i.e. pre-#1019, one full-source download per commit — the worst case, since
Option B only makes this better): external workers run ~7×/day (Modal daily, Beam daily, GitHub Actions
every 5h) at up to ~32 claims/run ⇒ ≤224 commits/day. Using the issue's own largest known source (Fort
Worth, ~27 MB): 224 × 27 MB ≈ 6 GB/day ≈ **~181 GB/month**, *if every single commit landed on the one
largest known source* — an extreme case a finite per-source backlog can't sustain every day. Using the
actually-measured typical record size (~5 KB) and typical source sizes (75 KB–1.9 MB, not 27 MB), realistic
unbatched volume is closer to **~7 GB/month — comfortably inside the ~258 GB ceiling by ~30–40×**, even
with *zero* batching.

**Why R6/R7's growth doesn't close this margin:** audio is ~38.5 MB/episode vs. records at ~5–20 KB even
post-R6 — a 2,000–8,000× gap. Records will never meaningfully move the total-storage denominator that
sets the free-egress ceiling, so the ceiling grows with the archive at roughly the same rate the download
need does. There is no future crossover point coming from record-size growth alone. And because Option A
doesn't reduce total bytes downloaded (it changes *which* bytes move per commit — one episode's ~5–20 KB
instead of a whole source — not the total moved across all commits for a given amount of real work), it
doesn't change this margin calculation at all.

**Decision: do not build Option A now.** The pathological worst case (all commits landing on the single
largest source, ~181 GB/month) sits inside the ~258 GB ceiling by only ~1.4× — real but modest, and it is
exactly what the §4.8 age-bound fix (and batching generally) widens, not what Option A's per-uid layout
would fix (Option A doesn't reduce total egress bytes, only re-shapes per-commit size). Building the
sidecar migration now would also risk the same "double migration" waste this project already explicitly
avoided once: [review/17](17-state-store-backend-evaluation.md) §3 kept records on B2 instead of moving
to R2-CAS specifically to avoid a B2→R2→DB double migration once the Phase-R managed-search-DB lands;
a B2→per-uid-sidecar→DB path repeats that exact pattern. That DB migration itself is **trigger-gated, not
scheduled** — [ROADMAP.md](../ROADMAP.md) and [review/11](11-technical-design-roadmap.md) §5.5 place it
decisively past 1.0, itself gated on R6/R7/R8/R9 (all still "Not started" as of this writing) plus one of
four specific triggers (federated query need, a public API, a search partition exceeding budget, or the
full custom-query feed builder) that hasn't fired.

**What would actually re-open this, in order of likelihood:**

1. §4.8's `payload_bytes`/`elapsed_s` flush telemetry (now logged on every flush) shows real wall-clock or
   memory pressure from R6/R7-grown records that this back-of-envelope pass missed — a "measure, don't
   estimate" trigger, matching the same discipline H9/H14d already use (cited directly in
   [review/31](31-speaker-diarization-attendee-extraction.md) §0).
2. The R7 `diarize` lane goes live on this same commit path and its own record contribution (or the
   diarize lane's own commit *cadence*, not just size) changes the egress math materially.
3. The Phase-R managed-search-DB migration's trigger fires — at which point Option A is skipped
   entirely and the migration goes straight from whole-file B2 to the DB, the same "no double migration"
   reasoning §3 already used for the R2 swing case.

---

## §5. Tradeoffs (stated honestly)

- **Static plan's virtue is determinism.** Every shard agrees by construction; there are no runtime
  races, and a run is reproducible/debuggable. Stage 2 trades that for adaptivity + external-worker
  support, paying with CAS contention, lease TTL tuning, and reaper complexity. Keep the static plan as
  the **in-Actions interim** until external workers actually exist; do not pay Stage 2's complexity early.
- **Per-uid plan size.** Bounded by *pending* backlog, not catalog (§3.1). If a pathological backlog ever
  bloats the artifact, fall back to per-uid only for the few skewed sources and source-atomic for the
  rest (the planner can mix units per source — a cheap escape hatch).
- **R2 Class A cost.** Per-item lease objects *could* multiply writes+lists (review/17 §4's cost axis),
  but §4.5 keeps steady state at **≈ 1 Class A op per completed transcript** — discover from the B2 index
  (no R2 listing), read-before-claim + scan offset (no failed-claim writes), infer completion from the
  free B2 record write, generous TTL (rare renews) — so per-item granularity does **not** force a high
  Class A bill. Stays inside the 1M/mo free tier well past 1,000 cities (review/17 §4); batch-claim escape
  hatch in §4.5 if it ever doesn't.
- **Lease TTL vs. long audio.** A 4-hour recording can outlive a naive TTL → double-work. Workers must
  **renew** mid-inference (§4.2); the reaper TTL must exceed the renew interval with margin. This is the
  same liveness concern H14a's dispatch leases already handle.

---

## §6. Sequence & how it avoids GPU-worker rework

1. **Ship Stage 1 now** (§3) — independent of external workers; fixes source skew; installs the
   ownership-keyed merge. Gate before any source is split *or* any external worker writes a record.
2. **Land review/17's R2 + `put_cas`** (already do-next there) — the substrate Stage 2 needs.
3. **Define the external-worker contract as the §4.2 claim protocol** — **before** building H14b (Modal) /
   H14c (Beam). The worker is a *puller* (read index → CAS-claim → infer → CAS-complete), **not** a
   passive executor a coordinator pushes to. Building H14b/c against a push-dispatch coordinator and then
   moving to a ledger would invert the worker's contract — precisely the rework to avoid.
4. **Promote the ledger** (§4) when the second worker class (first external GPU worker) goes live:
   complete. `asr.yml` now runs identical internal claim-loop workers and the transcribe static plan is
   retired. Audio stays source-atomic throughout. The final shape follows §4.3's split: shared
   claim/adopt/write orchestration, with worker-specific admission and supervision kept in distinct
   layers rather than collapsed into one mixed path.

> **Decision rule.** External transcription workers (H14b/H14c) MUST be built against the §4.2
> claim-from-ledger contract, even while in-Actions shards still use the Stage-1 static plan. The
> ownership-keyed merge (§3.2) is the shared commit path; nothing in the GPU-worker code changes when the
> in-Actions side flips from plan to ledger.

---

## §7. Roadmap impact & doc-update contract

- **Relationship to existing items.** This is the concrete design for H5's deferred "competitive lease
  acquisition + per-item persistence" and H6b's deferred "per-stage object files," and it resolves
  [`review/17`](17-state-store-backend-evaluation.md) §3's `episodes.json` **swing case** — **decided:
  records stay on B2** (→ managed search-DB at Phase R). Stage 2 is the "external workers read/write records
  directly" branch, but per-uid leasing makes each block single-writer, so that write commits through the
  §3.2 owned-block merge **on B2** without CAS (§4.5); only the lease ledger is R2 control-plane. It is the
  work-distribution half of **H14b/H14c**; H13's backend interface and H14a's lease lifecycle are reused
  unchanged.
- **review/11 catalog:** H17 + [GH#390](https://github.com/BashfulBits/city-meeting-podcasts/issues/390)
  tracked review/17 + this doc's Stage 1/Stage 2 substrate — **shipped, GH#390 closed.** §4.3/§6 step 4
  (in-Actions shards convert to the claim loop) was always this doc's own designed final step but was
  never split into its own tracked item; now that its trigger has fired (H14b/H14c both live), it is
  tracked separately as **H19** ([GH#831](https://github.com/BashfulBits/city-meeting-podcasts/issues/831)),
  not folded back into the closed H17 entry.
- **At Stage-1 ship time** (per [`review/11`](11-technical-design-roadmap.md) §2), update
  update [`ARCHITECTURE.md`](../ARCHITECTURE.md) (transcribe ownership is now per-episode) +
  [`CHANGELOG.md`](../CHANGELOG.md); flip the catalog entry; mature this doc's Stage-1 row to **Shipped**.
- **Before building H14b/H14c:** mature Stage 2 to L3 here (full ledger schema, TTL/renew/reaper params,
  R2 key layout, claim-loop CLI) under GH#390, so the worker contract is frozen first.
- **§4.8/§4.9 (GH#1019, child of GH#1012):** shipped as the L3 breakout the issue asked for — batching
  decision, lease-preservation argument, and acceptance-criteria mapping live in §4.8 itself rather than
  a separate document, matching the H22/§4.7 precedent. §4.9 investigated Option A (per-episode/lane
  sidecars) directly against R6/R7's actual record-shape additions and the real (re-verified) Backblaze
  B2 cost model, and found it isn't currently justified on cost grounds — deferred with three concrete
  re-open triggers (§4.9), none of which is "R6/R7 ship." If Option A is designed later, it belongs in a
  new §4.10 (or a dedicated breakout) rather than reopening §4.8/§4.9, since §4.8's batching layer is
  independent of and compatible with a later sidecar migration.

---

## §8. References

- In-repo: `citypods/sharding.py` (`create_shard_plan`/`sources_for_shard`/`ShardPlan`);
  `citypods/records.py` (`shard_assignment`, `estimate_transcribe_shard_work`,
  `merge_preserving_foreign`, `protected_blocks_for_lane`, `ARTIFACT_BLOCKS`);
  `citypods/statesync.py` (`push_records_merged`); `citypods/ops/workqueue.py` (H5 manifest);
  `citypods/compute/` (H13 backend, H14a `DispatchCoordinator`/`compute reconcile`);
  `citypods/compute/external_worker.py` (§4.8 `_queue_transcript_record`/`_flush_pending_transcripts`,
  GH#1019); `.github/workflows/asr.yml`.
- [`review/17`](17-state-store-backend-evaluation.md) — R2 + `put_cas` substrate; per-artifact
  disposition; `episodes.json` swing case; CAS retry-storm mitigation.
- [`review/16`](16-scaling-review-plan.md) — scaling envelope; S2 access patterns (dirty-only writes).
- [`review/12`](12-hardening-and-efficiency.md) §H5 (manifest/leases), §H6/§H6b (cross-lane merge,
  sharding), §H9 (throughput gate), §H13/§H14 (backends, external adapters).
