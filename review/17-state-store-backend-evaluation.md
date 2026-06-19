# review/17 — State-store backend evaluation & R2 migration design

**Maturity: L2 (→ L3 after the R2-CAS spike) · breakout of [`review/11`](11-technical-design-roadmap.md)
cross-cutting infra · last updated 2026-06-19**

> Forward-looking evaluation of where the project's **persistent state** should live as the catalog
> scales ([`review/16`](16-scaling-review-plan.md)) and as transcription/diarization moves to runners
> **outside GitHub Actions** (H13/H14). It does **not** propose moving large media. The canonical phase
> placement and maturity live in [`review/11`](11-technical-design-roadmap.md) §4/§5.5; an item enters the
> active roadmap only when its trigger fires and the doc-update contract promotes it.

## Status & maturity

| Sub-item | Maturity | Disposition |
|---|---|---|
| R2-CAS spike (boto3 conditional writes against R2) | L2 → L3 | Do-next; gates the L3 design |
| Coordination control-plane → R2 (CAS) | L2 → L3 after spike | **Recommended now** (after PR358 for circuits/leases) |
| `episodes.json` records → R2 vs hold-for-SQL | L2 (swing) | Recommend per access-model; see §3 |
| Records → managed SQL (D1/Turso) | L1 (Phase R) | Trigger-gated; supersedes the "no hosted DB" note for this scope |
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
3. **Hot coordination / control-plane** — `provider-circuits/**` + `provider-circuits-locks/**` (the PR358
   storage-coordinated, tenant-scoped circuit breaker), `provider-leases/**` (distributed host leases),
   `state/work.json` (H5 manifest leases/backoff), `state/compute_budget.json` (H14a free-tier ledger),
   `state/asr_runtime_log.json` (telemetry).

But the tier label is not what decides the right home. The deciding axis is **per artifact**:

> **Decision rule.** (1) *Immutable / content-addressed?* → object-storage blob → **stay B2** (free
> egress; workers GET by URL; no CAS). (2) *Needs atomic CAS or concurrent multi-writer access — including
> future external GPU workers?* → **R2 now** (CAS); → **SQL at Phase R** when queryability / row-level
> writes also matter. (3) *Append-only / low-contention / telemetry?* → **stay B2** (B2's free unlimited
> writes are ideal).

### 1.2 Root cause: B2 has no compare-and-swap

Every coordination primitive we keep in B2 is *emulated* — the PR358 circuit breaker and
`provider_leases.py` both use deterministic, timestamp-ordered objects guarded by a **one-slot FIFO
lease**, precisely because **B2's S3 API offers no conditional writes** (`If-Match` / `If-None-Match`).
That emulation is the efficiency and reliability cost, and it gets worse as shards multiply and as
non-Actions workers join (H13/H14), each of which must participate in the same emulated protocol.

### 1.3 Corrected facts that reshaped this evaluation (verified Jun 2026)

A naive reading is "move state off B2 to get speed/consistency." The facts say otherwise:

- **B2 Class A transactions (uploads/writes) are FREE and unlimited.** Only Class B (downloads,
  2,500/day free) and Class C (lists, 2,500/day free) are metered, cheaply. B2 is **already strongly
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
| `state/sources/<key>/episodes.json` | audio + ASR lanes; **RMW + foreign-block merge** | **yes (near-term)** | **yes** (lost-update race) | **Swing — see below** |
| `state/work.json` (H5 manifest) | reconcile + lanes; merge | **yes** (claim work) | **yes** (lease claims) | **→ R2 now** → SQL at Phase R |
| `state/compute_budget.json` | reconcile + dispatch coordinator | **yes** | **yes** (atomic decrement) | **→ R2 now** (overspend risk) |
| `provider-circuits/**` + `-locks/**` | all audio shards; high-freq under throttle | yes (future media workers) | **yes** (PR358's purpose) | **→ R2 now** (retire FIFO emulation) |
| `provider-leases/**` | all shards; high-freq | yes | **yes** (atomic FIFO) | **→ R2 now** |
| `state/asr_runtime_log.json` | all ASR shards; merge-union; telemetry | indirectly | no (merge-tolerant) | **Stay B2** |
| `state/run_history.jsonl` + `run_events/` | one writer / run; append | no | no | **Stay B2** (free writes) |

**Settled:** the **coordination + dispatch control plane** (`provider-circuits`, `provider-leases`,
`work.json`, `compute_budget.json`) → **R2** for CAS; **immutable blobs + append-only logs stay on B2**.

**Swing case — `episodes.json`.** Records are the one artifact where the right answer depends on the H14
external-worker access model:
- **If external GPU/media workers will read/write records *directly* in the near term**, prefer
  **R2-CAS now** — object-storage RMW of a monolithic JSON from many uncoordinated writers is exactly the
  race we want to avoid, and CAS lets us *simplify* the foreign-block merge. This directly serves the
  "reliability as we add external runners" goal.
- **Otherwise keep records on B2** (writes are free, already strongly consistent, the merge race is
  already mitigated) and **migrate straight to SQL at Phase R** — avoiding a double migration of the same
  data (B2 → R2 → SQL).

Recommended default: dispatch (work/budget) moves to R2 with the rest of the control-plane; **hold
`episodes.json` on B2 until the H14 access model is decided**, then either move to R2-CAS or skip to SQL.

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

## §5. Architecture & implementation (→ L3 after the spike)

Grounded in the existing seams (`citypods/storage/`):

- **Two-backend router.** Today `make_storage()` (`storage/__init__.py`) returns a **single** backend
  chosen by `AUDIO_STORAGE_BACKEND` / `defaults.audio_storage_backend`. Introduce a `RoutingStorage` that
  implements the `StorageBackend` Protocol (`storage/base.py`) and dispatches **by key prefix** to two
  `S3CompatibleStorage` instances — `audio/**`, `transcripts/**`, append-only logs (and, per §3, records)
  → **B2**; the coordination control-plane → **R2**. Callers (`statesync.py`, `media.py`, `stages.py`,
  `provider_circuits.py`, `provider_leases.py`) keep using one storage object unchanged. R2 is already
  constructible via `r2_from_env()`.
- **CAS extension.** Add conditional-write support to `S3CompatibleStorage`. The current `put_file` uses
  boto3's high-level `upload_file`, which cannot carry conditional headers — add a `put_bytes`/`put_cas`
  path over `put_object` with `IfNoneMatch="*"` (create-if-absent) and `IfMatch=<etag>` (CAS update),
  returning the new ETag or signalling a 412 conflict. Expose it as an **optional** Protocol capability
  (feature-detected via `hasattr`, like `get_file`/`list_objects` today). The exact botocore mechanism
  (native param vs. an event-system header injection) is what the §7 spike pins down.
- **Coordination redesign.** Re-implement `provider_circuits.py` + `provider_leases.py` and the H5
  work/budget lease writes on R2 CAS, retiring the one-slot FIFO emulation: lease acquire = conditional
  create; renew/release = CAS update; circuit open/close = CAS update; budget decrement = CAS read-modify-
  write with retry. Keep a serialization guard only on the **hottest keys** to avoid CAS retry-storms
  under throttle bursts (bounded backoff + jitter).
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
- **boto3 ↔ R2 conditional PUT ergonomics** — *the* implementation risk; **gated by the §7 spike**.
  Fallbacks: a thin CF Worker in front of R2 for CAS ops, or keep lease-emulation on R2 (then R2 buys
  consistency-parity, not simplification).
- **Tier-3 is the safety-critical throttle path; PR358 is in flight** — sequence circuits/leases **after**
  PR358 lands; prove the router + CAS helper on lower-stakes `work.json`/budget first.
- **R2 introduces a metered Class A budget B2 didn't** — couple records-on-R2 with S2 (§4).
- **CAS retry-storms on hot keys** — backoff/jitter; retain serialization on the few hottest keys.
- **D1 reachability vs Turso** — D1 needs a Worker/HTTP shim from external runners; Turso is directly
  reachable. Keep the Phase-R choice **open** and design records as an **adapter swap**, not a rewrite.

---

## §7. R2-CAS spike — acceptance criteria

A throwaway script (not shipped) against a scratch R2 bucket via `boto3`:

- **Create-if-absent:** `put_object(..., IfNoneMatch="*")` succeeds when the key is absent and returns
  **412** when it exists.
- **CAS update:** `put_object(..., IfMatch=<etag>)` succeeds with the current ETag and returns **412** on a
  stale ETag.
- **Mechanism:** confirm whether botocore exposes these natively or needs a `before-send` header-injection
  hook; capture a minimal helper signature for the `S3CompatibleStorage` CAS path.
- **Latency:** record p50/p95 for conditional PUT / GET / HEAD from a GitHub Actions runner.
- **Decision output (gates L3):** native CAS works ⇒ §5 uses it directly; awkward ⇒ adopt the Worker-shim
  or lease-on-R2 fallback (§6). The spike is **independent of PR358** and can run in parallel.

---

## §8. Phased sequence & triggers

1. **Land PR358 / resolve the Granicus reliability work** — do not refactor the throttle/coordination path
   mid-stabilization.
2. **R2-CAS spike** (§7) — parallel-OK; gates the L3 design.
3. **Mature this doc to L3** (concrete file/function changes, tests, backfill, acceptance) and cut issues;
   fold in [`review/16`](16-scaling-review-plan.md) **S2** access-pattern work.
4. **Migrate the coordination control-plane → R2** — start with `work.json`/`compute_budget.json` to prove
   the router + CAS helper; move `provider-circuits`/`provider-leases` **after PR358**.
5. **`episodes.json`** per §3 — R2-CAS now *iff* external workers will access records directly near-term,
   else hold for **SQL at Phase R**.

**Deferred fallback (L1, no L3): coordination → dedicated KV/DO** (Upstash / Durable Objects / DynamoDB).
**Trigger:** R2 Class A attributable to coordination approaches the free tier, **or** CAS-mismatch retry
rate under throttle storms exceeds a set threshold (instrument during step 4). Only then design the KV/DO
adapter.

---

## §9. Roadmap impact & doc-update contract

- **Reconciles the "hosted DB/API: out of scope (now)" stance** in
  [`review/11`](11-technical-design-roadmap.md) (§4 Deferred backlog, §6, §8). **R2 is object storage / the
  S3 API — it is *not* a hosted DB/API**, so the coordination-and-records → R2 move is fully consistent
  with the current "bucket-as-truth" philosophy and does **not** supersede that stance. Only the **Phase-R
  records-→-SQL** item is a hosted DB; it is **promoted from Deferred to Phase R (L1)**, scoped to
  federated query + a query API + state integrity, trigger-gated. Off-Actions media migration is
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

## §10. References (verified Jun 2026)

- [B2-pricing] Backblaze B2 transaction pricing — Class A free; Class B/C 2,500/day free:
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
