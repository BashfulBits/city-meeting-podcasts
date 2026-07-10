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
    remote: dict, local: dict, protected: frozenset[str],
    *, owned_uids: frozenset[str] | None = None,
) -> dict:
    merged = {uid: dict(rec) for uid, rec in remote.items()}
    for uid, local_rec in local.items():
        # uid this run does NOT own: never write our snapshot-stale artifact for it.
        if owned_uids is not None and uid not in owned_uids:
            if uid not in remote:                       # newly discovered, unowned (§2.2)
                merged[uid] = {k: v for k, v in local_rec.items() if k not in ARTIFACT_BLOCKS}
            # else: keep remote as-is (already copied above) — a sibling owns/writes it.
            continue
        rec = dict(local_rec)                            # owned: today's behavior
        remote_rec = remote.get(uid)
        if remote_rec:
            for block in protected:                      # still preserve cross-lane foreign blocks
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

---

## §8. References

- In-repo: `citypods/sharding.py` (`create_shard_plan`/`sources_for_shard`/`ShardPlan`);
  `citypods/records.py` (`shard_assignment`, `estimate_transcribe_shard_work`,
  `merge_preserving_foreign`, `protected_blocks_for_lane`, `ARTIFACT_BLOCKS`);
  `citypods/statesync.py` (`push_records_merged`); `citypods/ops/workqueue.py` (H5 manifest);
  `citypods/compute/` (H13 backend, H14a `DispatchCoordinator`/`compute reconcile`);
  `.github/workflows/asr.yml`.
- [`review/17`](17-state-store-backend-evaluation.md) — R2 + `put_cas` substrate; per-artifact
  disposition; `episodes.json` swing case; CAS retry-storm mitigation.
- [`review/16`](16-scaling-review-plan.md) — scaling envelope; S2 access patterns (dirty-only writes).
- [`review/12`](12-hardening-and-efficiency.md) §H5 (manifest/leases), §H6/§H6b (cross-lane merge,
  sharding), §H9 (throughput gate), §H13/§H14 (backends, external adapters).
