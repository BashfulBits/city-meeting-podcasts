# review/17 — State-store backend evaluation & R2 migration design

**Maturity: L3 · breakout of [`review/11`](11-technical-design-roadmap.md)
cross-cutting infra · last updated 2026-06-19**

> **Reprioritized 2026-07-12 (maintainer decision):** the "records → managed SQL @ Phase R" line below
> (row 4) moves decisively **past 1.0** and merges with [review/25](25-future-features-and-architecture.md)
> §3.1's "Interaction seam" proposal into one initiative, designed together when the trigger fires rather
> than scoping the DB now and a Worker/alerts/API tier later. This doesn't change any conclusion in this
> doc — §1.4's "SQL-for-search is a trigger, not a default" and the B2-vs-R2-vs-DB analysis in §3 are
> exactly why the migration is safe to defer: static pages + partitioned client search (review/13)
> already carry Phase R without it. See [`review/11`](11-technical-design-roadmap.md) §5.5 for the
> canonical catalog entry.

> Forward-looking evaluation of where the project's **persistent state** should live as the catalog
> scales ([`review/16`](16-scaling-review-plan.md)) and as transcription/diarization moves to runners
> **outside GitHub Actions** (H13/H14). It does **not** propose moving large media. The canonical phase
> placement and maturity live in [`review/11`](11-technical-design-roadmap.md) §4/§5.5; an item enters the
> active roadmap only when its trigger fires and the doc-update contract promotes it.

## Status & maturity

| Sub-item | Maturity | Disposition |
|---|---|---|
| R2-CAS spike (boto3 conditional writes against R2) | **L3 · Shipped** | Run 2026-06-19; all 4 tests PASS; native params (boto3 1.43); see §7 |
| Coordination control-plane → R2 (CAS) | L2 → L3 | **H17 do-next** — spike unblocked; one consolidated implementation issue with review/18 |
| `episodes.json` records → R2 vs hold-for-DB | **Decided** | **Stay on B2** (per-uid lease ⇒ single-writer; no CAS need) → managed search-DB @ Phase R; skip R2 to avoid a double migration; see §3 |
| Records → managed SQL (D1/Turso) | L1 (post-1.0) | Trigger-gated; merged with the Interaction-seam proposal (review/25 §3.1) into one deferred initiative, now sketched inline in [review/11](11-technical-design-roadmap.md#55-cross-cutting--ongoing) §5.5; supersedes the "no hosted DB" note for this scope |
| Coordination → dedicated KV/DO (fallback) | L1 | Deferred; trigger in §8 |
| Immutable blobs + append-only logs | — | **Stay on B2** (settled) |

---

## §1. Problem & scope

### 1.1 The three tiers, then the real axis

Persistent state in B2 splits into three tiers with very different needs:

1. **Large immutable blobs** — content-addressed audio (`audio/<provider>/<source_key>/<uid>-<spec>.m4a`,
   ~2–20 MB) and transcripts (`transcripts/.../<uid>-<spec>.vtt` + `.words.json`). Served publicly via a
   Cloudflare-fronted URL; egress is free (Bandwidth Alliance). Never mutated.
2. **Durable records** — `state/sources/<source_key>/episodes.json` (~1–10 KB/source), the bucket-as-truth
   record set, merged with foreign-block preservation (`records.merge_preserving_foreign`,
   `statesync.push_records_merged`).
3. **Hot coordination / control-plane** — `provider-leases/**` (distributed host leases),
   `state/work.json` (H5 manifest leases/backoff), `state/compute_budget.json` (H14a free-tier ledger),
   `state/asr_runtime_log.json` (telemetry). (The PR358 storage-coordinated, tenant-scoped circuit breaker
   that also lived here was deleted by GH#353; only the lease pool survives.)

But the tier label is not what decides the right home. The deciding axis is **per artifact**:

> **Decision rule.** (1) *Immutable / content-addressed?* → object-storage blob → **stay B2** (free
> egress; workers GET by URL; no CAS). (2) *Needs atomic CAS or concurrent multi-writer access — including
> future external GPU workers?* → **R2 now** (CAS); → **SQL at Phase R** when queryability / row-level
> writes also matter. (3) *Append-only / low-contention / telemetry?* → **stay B2** (B2's free unlimited
> writes are ideal).

### 1.2 Root cause: B2 has no compare-and-swap

Every coordination primitive we keep in B2 is *emulated* — `provider_leases.py` uses deterministic,
timestamp-ordered objects guarded by a **one-slot FIFO lease**, precisely because **B2's S3 API offers no
conditional writes** (`If-Match` / `If-None-Match`). (The PR358 circuit breaker shared this emulation
before GH#353 removed it.)
That emulation is the efficiency and reliability cost, and it gets worse as shards multiply and as
non-Actions workers join (H13/H14), each of which must participate in the same emulated protocol.

### 1.3 Corrected facts that reshaped this evaluation (verified Jun 2026; B2 pricing re-verified 2026-07-26)

A naive reading is "move state off B2 to get speed/consistency." The facts say otherwise:

- **B2 Class A, B, and C transactions (uploads, downloads, lists) are all FREE with no tier limit**
  (corrected 2026-07-26 against Backblaze's live pricing page — an earlier "Class B/C, 2,500/day free
  then metered" reading here was stale/wrong; only Class D outbound-webhook calls carry that
  2,500/day-then-metered shape, and this project doesn't use those). **The actual metered dimension is
  egress bytes**: downloads are free up to **3× the account's average monthly storage**, then
  **$0.01/GB**; storage itself is $0.005/GB/month past a 10GB free allowance. B2 is **already strongly
  consistent** (read-after-write). [B2-pricing] [B2-consistency]
- **R2 meters Class A = writes *and* lists** (1M/mo free, then **$4.50/M**); Class B 10M/mo free then
  $0.36/M; storage $0.015/GB; egress $0. [R2-pricing]
- **R2 supports conditional writes / CAS** (Workers `onlyIf` etagMatches/etagDoesNotMatch + S3
  `If-Match`/`If-None-Match`) on top of strong consistency. [R2-conditional] [R2-consistency]

**Therefore R2's *only* storage-level gain over B2 is CAS** — not consistency (B2 has it) — and R2 in
fact *introduces* write metering that B2 did not impose. This is why the strong case is the
**coordination control-plane** (which genuinely needs CAS), while immutable blobs and append-only logs
have no reason to leave B2, and low-frequency records are cost-trivial either way.

### 1.4 What a SQL store actually buys over static JSON (and what it does not)

Because the public site is **static** (GitHub Pages + Cloudflare), it is worth being precise about where a
database helps. This determines how the Phase-R records-→-SQL item is scoped.

- **Per-meeting pages → static HTML is strictly better.** Pages render cheaply from records under the
  existing `feed_content_hash` skip/prune logic ([`review/13`](13-per-meeting-pages-and-search.md) Part A);
  static HTML is fast, cacheable, crawlable, and $0 to serve. **A database adds nothing to serving them.**
- **Transcript search → static, partitioned client-side search is the right default and scales far.**
  [`review/13`](13-per-meeting-pages-and-search.md) Part B already commits to a record-built, per-city/
  source, lazy-loaded index ("no server until proven insufficient"). Pagefind runs full-text search over a
  **10,000-page site in <300 kB of total payload** (~100 kB typical), scaling to tens/hundreds of
  thousands of pages. [Pagefind] Functionally: *static* = the browser downloads a few index chunks and
  searches **locally** (zero backend, bounded only by how large an index you can ship); *SQL* = the
  browser calls a **Worker→D1/Turso** endpoint that searches the **whole corpus server-side** (unbounded,
  ranked, faceted, but adds a network hop and requires a Worker, since Pages is static).
- **So SQL-for-search is a *trigger*, not a default.** Reach for it only when (a) you want **federated,
  multi-faceted queries across the entire catalog** (all cities × date × topic tag × speaker × vote in one
  ranked query) that exceed a comfortable static index; (b) you want a **public query API**; (c) a city/
  source partition exceeds the client index budget ([`review/16`](16-scaling-review-plan.md): 1 MB target,
  2 MB hard warning); or (d) the custom-query feed builder (#12/#13), which [`review/11`](11-technical-design-roadmap.md)
  §5.2 already notes "needs either pre-gen combinations *or* a Cloudflare Worker."
- **The non-search reason SQL is attractive is state integrity, not the frontend.** Row-level writes
  eliminate the monolithic `episodes.json` read-modify-write — the cross-lane clobber the
  foreign-block-preserving merge mitigates, and the "per-stage object files" item
  ([`review/11`](11-technical-design-roadmap.md) §6) targets — and make `/admin/status` and reporting
  **queryable** instead of scan-the-bucket.

**Conclusion:** the Phase-R records-→-SQL move is scoped to **federated query + a query API + state-model
integrity**, all trigger-gated. Static carries pages and search a long way; SQL is a scaling/feature
upgrade, not a launch dependency.

### 1.5 Scope of this review

- **In scope:** the records (tier 2) and coordination (tier 3) state — per-artifact.
- **Out of scope:** tier-1 blobs (stay B2); rendered HTML (`docs/**` → GitHub Pages, untouched);
  off-Actions media migration (separately deferred, [`review/16`](16-scaling-review-plan.md)).
- **Constraints:** default **$0 / free-tier**; the store must be **reachable from non-GitHub runners**;
  prefer **pluggable / no-vendor-lock** (managed services only behind an adapter).

---

## §2. Options evaluation (exhaustive)

Capacity is a non-issue: records + coordination total tens of MB even at thousands of cities, far inside
every free tier (R2 10 GB, Turso/D1 5 GB, DynamoDB 25 GB). The binding constraints are **operation
counts, latency, atomics (CAS), and reachability from arbitrary runners**.

| Option | Speed | Cost (free-tier / at scale) | Reliability / atomics | External-runner reach | Verdict |
|---|---|---|---|---|---|
| **B2, status quo** | fine | **writes free**, lists cheap; ~$0 | strong-consistent but **no CAS** (FIFO emulation) | S3 creds (works) | Keep for blobs/logs; the CAS gap is the problem for coordination |
| **R2 (object storage + CAS)** | fine; strong-consistent | 10 GB / 1M Class A / 10M Class B free; Class A meters writes+lists ($4.50/M) | **strong + real CAS** | S3 creds (works) | **Recommended** for coordination; already a wired adapter (`r2_from_env`) |
| **Managed SQL — Turso/libSQL** | low-latency; SQLite | 5 GB / 500M row-reads / 10M row-writes free | server-side txns / CAS | **direct** libSQL/HTTP | **Phase-R** records (queryable) — kept open |
| **Managed SQL — Cloudflare D1** | low-latency; SQLite | 5 GB / ~5M reads-day / ~100k writes-day free | server-side txns | via Worker / D1 HTTP API (extra shim) | **Phase-R** records alt; same CF account as R2 |
| **Managed Postgres — Neon / Supabase** | full SQL | small free (0.5 GB); **Neon cold-start ~5 min**, **Supabase pauses after 1 wk idle** | strong | network | Heavier; pause/cold-start are reliability flags |
| **KV/DO — Upstash Redis** | ms; native TTL | 256 MB / **500k cmd/mo (~16.7k/day)** | atomic ops | REST (works) | Great for coordination, but free command cap is tight at scale |
| **KV/DO — Cloudflare Durable Objects** | single-thread txnal actor + alarms | free since 2025; 100k req/day, 5 GB | **purpose-built** serialized coordination | needs a Worker+DO | Best pure-coordination fit; most new infra |
| **DynamoDB** | ms; CAS + TTL | 25 GB free **provisioned-only** (on-demand billed) | conditional writes | AWS creds | Solid but AWS-coupled; provisioned tuning |
| **SQLite + obj-storage repl (Litestream/LiteFS)** | fast local | cheap | **single-writer only** (Litestream = DR; LiteFS = 1 primary) | n/a | **Rejected** — clashes with 4 concurrent shards + external writers |
| **Git-as-datastore** | slow (clone/commit/push) | free, versioned | commit contention; CI loops; repo bloat | clone | **Rejected** except tiny snapshots |

Two cross-cutting conclusions: **managed SQLite (D1/Turso) dominates self-hosted SQLite+replication** for
our concurrency model (the managed flavor handles multi-writer server-side; Litestream/LiteFS are
single-writer); and the coordination tier wants **either** R2 CAS **or** a dedicated atomic KV/DO — R2
wins on "no new dependency, already wired, $0," the KV/DO option is held as a triggered fallback (§8).

---

## §3. Per-artifact disposition (recommendation)

| Artifact | Writers / pattern | Read by external GPU workers? | CAS need | **Home** |
|---|---|---|---|---|
| `audio/**.m4a` | audio lane; immutable content-addressed | **yes** (fetch to transcribe) | none | **Stay B2** |
| `transcripts/**.vtt` + `.words.json` | ASR lane / **external workers write results**; immutable | yes (downstream) | none | **Stay B2** |
| rendered HTML `docs/**` | render lane; rebuilt from records | no | none | **GitHub Pages** (not B2) |
| `state/sources/<key>/episodes.json` | audio + ASR lanes; **RMW + foreign-block merge** | **yes (near-term)** | no (per-uid lease ⇒ single writer) | **Stay B2 → managed search-DB @ Phase R** (decided; see below) |
| `state/work.json` (H5 manifest) | reconcile + lanes; merge | **yes** (claim work) | **yes** (lease claims) | **→ R2 now** → SQL at Phase R |
| `state/compute_budget.json` | reconcile + dispatch coordinator | **yes** | **yes** (atomic decrement) | **→ R2 now** (overspend risk) |
| `provider-leases/**` | all shards; high-freq | yes | **yes** (per-slot CAS) | **→ R2 (H17 PR6, done)** — per-slot CAS objects, FIFO dropped for cap-only |
| `state/asr_runtime_log.json` | all ASR shards; merge-union; telemetry | indirectly | no (merge-tolerant) | **Stay B2** |
| `state/run_history.jsonl` + `run_events/` | one writer / run; append | no | no | **Stay B2** (free writes) |

**Settled:** the **coordination + dispatch control plane** (`provider-leases`, `work.json`,
`compute_budget.json`) → **R2** for CAS; **immutable blobs + append-only logs stay on B2**.

**Swing case — `episodes.json` — DECIDED: stay on B2, migrate straight to a managed search-DB at Phase R.**
Records were the one artifact whose backend depended on the H14 external-worker access model. The decision
is **not R2-CAS**, for two reasons:
- **Per-uid lease ownership removes the race CAS was for.** The R2-CAS argument was "uncoordinated RMW of a
  monolithic JSON from many writers." But under [`review/18`](18-work-distribution-sharding.md) **Stage 2**
  exactly **one** worker holds a uid's lease, so that uid's record block has a **single writer**. The
  **shipped** Stage-1 owned-block foreign-preserving merge (`merge_preserving_foreign(owned_uids=)`, #394)
  commits each owned block without clobbering siblings, on B2, race-free — no CAS needed. `review/16` **S2**
  (dirty-only writes / targeted reads) keeps `episodes.json` volume bounded as cities scale (§4).
- **Avoid a double migration.** B2 writes are free and strongly consistent today; the long-term home for
  records is a **managed, search-capable database** (DBaaS) adopted at **Phase R** to scale search. Moving
  records B2 → R2 → DB would migrate the same data twice for no interim benefit, so we **skip R2 for
  records** and go B2 → DB once.

Net: the control-plane (work/budget/leases) moves to R2 for CAS (PR1–PR6); **records stay on B2** and are
the only state that defers straight to the Phase-R database. A near-term design goal is to **reduce
`episodes.json` read/writes** (S2 access patterns + dirty-only commits) so B2 remains comfortable at scale
until that migration.

**GH#1015 implementation note (2026-07-25):** the first S2 state-sync slice now publishes
`state/catalog/manifest.json`, restores by digest, registers dirty paths in central writers, and
requires explicit tombstones for deletion. The manifest uses backend CAS when available and falls
back to the pre-S2 full list/restore path when absent or incompatible; demand planning and
shard-specific hydration remain separate follow-up work.

> **Cross-ref:** [`review/18`](18-work-distribution-sharding.md) Stage 2 — external workers **claim**
> episodes from the lease ledger and write transcript records — is the "external workers read/write records
> directly" case. With per-uid leasing each block is single-writer, so that path commits through the
> Stage-1 owned-block merge **on B2** (not R2-CAS). Stage 2's lease ledger itself is control-plane and
> lives on R2; only the *record write* stays on B2. (§4.5 of review/18 documents the now-unused R2-CAS-on-
> records end-state for completeness; it is not the chosen path.)

---

## §4. Cost model — B2 vs R2 at scale

The asymmetry that matters: **B2 = free unlimited writes** (lists metered cheaply); **R2 = metered Class A
on writes *and* lists** (1M/mo free, $4.50/M after). So the cost question is entirely about **R2 Class A
volume**, driven by (a) coordination ops and (b) record writes/lists — and whether we adopt
[`review/16`](16-scaling-review-plan.md) **S2** access patterns (dirty-only writes, targeted reads, no
bucket-wide `state/` listing).

**Method.** Class A/mo ≈ (writes + lists per shard-run) × shard-runs/mo. Cadence: `audio.yml` 6/day +
`asr.yml` ~5/day, ×4 shards, + reconciles ≈ ~1,500 state-touching shard-runs/mo. Order-of-magnitude
planning estimates (not promises); coordination scales **sub-linearly** with cities because providers are
shared across cities via `source_key` dedup.

| Scale (cities / ~sources) | Coordination on R2 | Records on R2 — naive (re-push all) | Records on R2 — dirty-only (S2) | Total naive vs 1M free | Naive overage |
|---|---|---|---|---|---|
| Today (~10 / ~30) | ~45k | ~30k | ~3k | ~75k (**~7%**) | $0 |
| ~100 / ~300 | ~120k | ~300k | ~25k | ~420k (**under**) | $0 |
| ~500 / ~1,500 | ~300k | ~1.5M | ~110k | ~1.8M (**over**) | **~$3.5/mo** |
| ~1,000 / ~3,000 | ~450k | ~3.0M | ~220k | ~3.4M (**over**) | **~$11/mo** |

**Reading of the model:**
- **Coordination-only on R2 is comfortably free past ~1,000 cities** — the settled migration is cheap.
- **Records on R2 *naive* cross the free tier around ~300–500 cities**, but the **overage is small**
  (<$15/mo even at 1,000 cities); **dirty-only writes (S2) keep records on R2 free past ~1,000 cities.**
- **Records on B2 are $0 regardless** (free writes) — reinforcing the swing-case framing: records-on-B2
  needs no cost mitigation; records-on-R2 should ship **with** S2, or accept a few $/mo.

**Takeaway:** $0 is preservable. The only place cost discipline is required is **records on R2 at scale**,
and the mitigation (S2) is already on the roadmap. Coordination — the part we move now — is cheap.

---

## §5. Architecture & implementation

Grounded in the existing seams (`citypods/storage/`):

- **Two-backend router.** Today `make_storage()` (`storage/__init__.py`) returns a **single** backend
  chosen by `AUDIO_STORAGE_BACKEND` / `defaults.audio_storage_backend`. Introduce a `RoutingStorage` that
  implements the `StorageBackend` Protocol (`storage/base.py`) and dispatches **by key prefix** to two
  `S3CompatibleStorage` instances — `audio/**`, `transcripts/**`, append-only logs (and, per §3, records)
  → **B2**; the coordination control-plane → **R2**. Callers (`statesync.py`, `media.py`, `stages.py`,
  `provider_leases.py`) keep using one storage object unchanged. R2 is already
  constructible via `r2_from_env()`.
- **CAS extension.** Add `put_cas()` to `S3CompatibleStorage` using **native boto3 params** (confirmed by
  the §7 spike on boto3 1.43 — no header injection needed). Expose it as an **optional** Protocol
  capability (feature-detected via `hasattr`, like `get_file`/`list_objects` today). Pass the ETag as
  returned by boto3 (includes surrounding quotes). Raise `CASConflict` on 412.

  ```python
  def put_cas(self, key: str, data: bytes, content_type: str, *,
              if_none_match: str | None = None,
              if_match: str | None = None) -> tuple[str, str]:
      kwargs = dict(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)
      if if_none_match:
          kwargs["IfNoneMatch"] = if_none_match
      if if_match:
          kwargs["IfMatch"] = if_match          # pass ETag as returned (includes quotes)
      try:
          r = self._client.put_object(**kwargs)
          return self.public_url(key), r["ETag"]
      except botocore.exceptions.ClientError as exc:
          if exc.response["Error"]["Code"] == "PreconditionFailed":
              raise CASConflict(key) from exc
          raise
  ```
- **Coordination redesign.** Re-implement `provider_leases.py` and the H5 work/budget lease writes on R2
  CAS, retiring the candidate-list FIFO emulation: lease acquire = conditional create; renew/release =
  CAS update; budget decrement = CAS read-modify-write with retry. The CAS retry-storm is avoided not by
  a serialization guard but by **independent ETags** — work-leases are per `(source,uid)`, and provider
  leases are per *slot* (`provider-leases/<domain>/slot-<i>.json`), so concurrent claims of different
  items/slots never contend. **Shipped (H17 PR6):** `provider_leases.py` now uses per-slot CAS objects;
  a worker reads a slot (Class-B) and claims a free one with `if_none_match="*"` or an expired one with
  `if_match=<etag>`, never listing. FIFO arrival order is dropped (the contract is the concurrency cap,
  not fairness) and the soft cap can briefly admit N+1 on a reap-vs-release race — both fine for a rate
  limiter. The pool degrades to in-process-only on a non-CAS backend.
- **External-worker access (H13/H14).** Document the access path per artifact: blobs and coordination via
  **S3 creds** to B2/R2; Phase-R records via **Turso (direct libSQL/HTTP)** or **D1 (Worker / D1 HTTP
  API)**. This is the concrete payoff for "reliability as we expand to non-Actions runners."
- **S2 coupling.** Land the record/coordination R2 writes **with** targeted reads + dirty-only uploads +
  a manifest instead of bucket-wide `state/` listings ([`review/16`](16-scaling-review-plan.md) S2) so R2
  Class A stays inside the free tier (§4).

---

## §6. Risks & mitigations

- **Two backends + two credential sets** on every runner (incl. external) — threaded via `RoutingStorage`;
  document secrets (`B2_*` and `CLOUDFLARE_ACCOUNT_ID`/`R2_*`).
- **boto3 ↔ R2 conditional PUT ergonomics** — ~~the implementation risk~~ **resolved by §7 spike**
  (2026-06-19): native `IfNoneMatch`/`IfMatch` params work on boto3 1.43 against R2. Fallbacks (Worker
  shim, lease-on-R2) are not needed.
- **Tier-3 is the safety-critical throttle path** — sequence the **lease-pool** migration **after** the
  Granicus reliability work (PR358, merged) and the GH#353 circuit removal; prove the router + CAS helper
  on lower-stakes `work.json`/budget first.
- **R2 introduces a metered Class A budget B2 didn't** — couple records-on-R2 with S2 (§4).
- **CAS retry-storms on hot keys** — backoff/jitter; retain serialization on the few hottest keys.
- **D1 reachability vs Turso** — D1 needs a Worker/HTTP shim from external runners; Turso is directly
  reachable. Keep the Phase-R choice **open** and design records as an **adapter swap**, not a rewrite.

---

## §7. R2-CAS spike — acceptance criteria & run instructions

The spike lives in [`scripts/spike_r2_cas.py`](../scripts/spike_r2_cas.py) with a GHA workflow at
[`.github/workflows/spike-r2-cas.yml`](../.github/workflows/spike-r2-cas.yml).

### Acceptance criteria

Four tests that must all PASS before `cas_mechanism` is promoted to L3:

| # | Test | Expected |
|---|---|---|
| 1 | `create_if_absent_success` | `put_object(IfNoneMatch="*")` on absent key → **200** + ETag |
| 2 | `create_if_absent_conflict` | same call on existing key → **412 PreconditionFailed** |
| 3 | `cas_update_success` | `put_object(IfMatch=<current_etag>)` → **200** + new ETag |
| 4 | `cas_update_stale` | `put_object(IfMatch=<stale_etag>)` → **412 PreconditionFailed** |

Additionally captured:

- **Mechanism:** whether boto3 accepts `IfNoneMatch`/`IfMatch` natively (boto3 ≥ 1.35) or requires a
  `botocore` `before-send` header-injection hook (boto3 1.34). The script auto-detects and reports.
- **Helper signature:** the recommended `put_cas()` implementation for `S3CompatibleStorage` (printed in
  `cas_mechanism` section of the JSON report, one variant per mechanism).
- **Latency:** p50/p95 for conditional PUT / unconditional GET / HEAD.

**Decision output (gates L3):** all four tests PASS ⇒ §5 CAS extension proceeds using the reported
mechanism. Any FAIL ⇒ adopt the Worker-shim or lease-on-R2 fallback (§6). The spike is **independent of
PR358** and can run in parallel.

### Running the spike

**Prerequisites:**

```
pip install -e ".[storage]"           # boto3 included in the storage extra
```

Required env vars (same set as `r2_from_env()`):

| Var | Value |
|---|---|
| `CLOUDFLARE_ACCOUNT_ID` | Cloudflare account ID |
| `R2_ACCESS_KEY_ID` | R2 token key ID |
| `R2_SECRET_ACCESS_KEY` | R2 token secret |
| `R2_BUCKET` | Target bucket (use a scratch bucket if available; spike prefixes all objects under `spike-r2-cas/<run-id>/` and deletes them on exit) |

**Local run (CAS tests only, no latency):**

```bash
export CLOUDFLARE_ACCOUNT_ID=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... R2_BUCKET=...
python scripts/spike_r2_cas.py --no-latency
```

**GHA run (CAS tests + GHA-runner latency — recommended for the L3 gate):**

1. Ensure the four secrets are set in **repo Settings → Secrets and variables → Actions**.
2. Go to **Actions → R2-CAS spike → Run workflow**.
3. Leave `latency_iterations` at `20`; leave `skip_latency` unchecked.
4. After the run completes, download the **`r2-cas-spike-results`** artifact (a JSON file).

**Local run (full, with latency):**

```bash
python scripts/spike_r2_cas.py --latency-iterations 20 --output r2-cas-spike-results.json
```

### Results (run 2026-06-19, GHA `ubuntu-latest`)

| Field | Value |
|---|---|
| boto3 / botocore version | 1.43.34 / 1.43.34 |
| `cas_mechanism` | **native** (no header injection needed) |
| `create_if_absent_success` | PASS — 200, 176 ms |
| `create_if_absent_conflict` | PASS — 412, 69 ms |
| `cas_update_success` | PASS — 200, 160 ms |
| `cas_update_stale` | PASS — 412, 46 ms |
| conditional PUT p50 / p95 (GHA) | **170 ms / 211 ms** |
| GET p50 / p95 (GHA) | **86 ms / 118 ms** |
| HEAD p50 / p95 (GHA) | **41 ms / 51 ms** |
| `overall_pass` | **true** |

**Decision: proceed with §5 native-param CAS extension.** No fallback needed.

---

## §8. Phased sequence & triggers

1. **Land PR358 / resolve the Granicus reliability work** — ✅ merged.
2. **R2-CAS spike** (§7) — ✅ complete (2026-06-19); native params confirmed; this doc promoted to L3.
3. **Execute H17's consolidated implementation issue** — `RoutingStorage` + `put_cas()` (§5); fold in
   [`review/16`](16-scaling-review-plan.md) **S2** access-pattern work to keep R2 Class A free.
4. **Migrate the coordination control-plane → R2** — start with `work.json`/`compute_budget.json` to prove
   the router + CAS helper; move `provider-leases` **after PR358**.
5. **`episodes.json`** per §3 — **decided: stay on B2** (per-uid lease ownership makes each block
   single-writer, so the shipped owned-block merge is race-free without CAS) and **migrate straight to a
   managed search-DB at Phase R** — skipping R2 for records to avoid a B2→R2→DB double migration.

**Deferred fallback (L1, no L3): coordination → dedicated KV/DO** (Upstash / Durable Objects / DynamoDB).
**Trigger:** R2 Class A attributable to coordination approaches the free tier, **or** CAS-mismatch retry
rate under throttle storms exceeds a set threshold (instrument during step 4). Only then design the KV/DO
adapter.

### §8.1 Validating live before production cutover

Before flipping `defaults.audio_storage_backend` to `routing` (B2 primary + R2 coordination), confirm
the plumbing works against the **live** services without risking production data:

1. Set the repo Action secrets for **both** backends (`CLOUDFLARE_ACCOUNT_ID`, `R2_*`, `B2_*`).
2. Run **Actions → "Validate R2/CAS control plane"** (`.github/workflows/validate-control-plane.yml`;
   also runs weekly as a health check). It exercises `RoutingStorage` routing, R2 CAS
   (`put_cas`/`get_bytes`: create-if-absent / conditional update / stale rejection), and the
   work-lease ledger (`claim`/contend/`renew`/`release`/`reap`) via
   [`scripts/validate_control_plane.py`](../scripts/validate_control_plane.py).
3. Confirm the job is green and every check in the `control-plane-validation` artifact is
   `"pass": true`. **Safe by construction:** all writes live under a scratch
   `work-leases/__validate__/<run-id>/…` namespace and are deleted on exit; the real budget/lease
   keys are never touched.
4. Only then flip the backend and watch the next scheduled audio/asr run.

---

## §9. Roadmap impact & doc-update contract

- **Reconciles the "hosted DB/API: out of scope (now)" stance** in
  [`review/11`](11-technical-design-roadmap.md) (§4 Deferred backlog, §6, §8). **R2 is object storage / the
  S3 API — it is *not* a hosted DB/API**, so the coordination-and-records → R2 move is fully consistent
  with the current "bucket-as-truth" philosophy and does **not** supersede that stance. Only the
  **records-→-SQL** item is a hosted DB; it was promoted from Deferred to Phase R, then **reprioritized
  2026-07-12 to past-1.0 (L0)**, merged with the Interaction-seam proposal, scoped to federated query + a
  query API + state integrity + the dynamic edge tier, trigger-gated. Off-Actions media migration is
  unaffected (runners still reach R2 via S3).
- **Unblocks / aligns with:** H13/H14 external workers (clean multi-worker state access);
  [`review/16`](16-scaling-review-plan.md) S2 (shared work); [`review/13`](13-per-meeting-pages-and-search.md)
  (records-→-SQL is the federated-search escalation path); the "per-stage object files" deferred item
  (SQL row-level writes are the eventual form of that fix).
- **At ship time** (per [`review/11`](11-technical-design-roadmap.md) §2): update
  [`ARCHITECTURE.md`](../ARCHITECTURE.md) (state now spans B2 + R2 behind a router) and
  [`CHANGELOG.md`](../CHANGELOG.md); flip the `review/11` catalog entry; **freeze + stamp this doc**
  ("Implemented in PR #N").

---

## §10. References (verified Jun 2026; B2 pricing re-verified 2026-07-26)

- [B2-pricing] Backblaze B2 transaction pricing — Class A/B/C entirely free, no tier limit; egress
  free up to 3× average monthly storage, then $0.01/GB; storage $0.005/GB/month past 10GB free:
  <https://www.backblaze.com/cloud-storage/transaction-pricing>
- [B2-consistency] B2 strong read-after-write consistency: <https://news.ycombinator.com/item?id=23072419>
- [R2-pricing] Cloudflare R2 pricing & free tier (10 GB / 1M Class A / 10M Class B; $4.50/M Class A;
  $0 egress): <https://developers.cloudflare.com/r2/pricing>
- [R2-conditional] R2 conditional writes (`onlyIf` / `If-Match` / `If-None-Match`):
  <https://developers.cloudflare.com/r2/api/workers/workers-api-reference/>
- [R2-consistency] R2 strong global consistency: <https://developers.cloudflare.com/r2/reference/consistency>
- [Pagefind] Static low-bandwidth search at scale (<300 kB over 10k pages):
  <https://cloudcannon.com/blog/introducing-pagefind/>
- Free-tier references — Turso: <https://turso.tech/pricing> · Cloudflare D1:
  <https://developers.cloudflare.com/d1/platform/pricing/> · Durable Objects (free tier, SQLite):
  <https://developers.cloudflare.com/durable-objects/platform/pricing/> · Upstash Redis:
  <https://upstash.com/pricing/redis> · DynamoDB (provisioned-only free): <https://dynobase.dev/dynamodb-free-tier/>
  · Neon: <https://neon.tech/pricing> · Supabase: <https://supabase.com/pricing>
- Litestream (single-node DR) / LiteFS (single-primary): <https://litestream.io/how-it-works/> ·
  <https://fly.io/docs/litefs/faq/>
