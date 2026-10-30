# review/57 — LLM dispatch row efficiency (packed scheduler state)

**Maturity: implemented, inactive until an operator cutover · 2026-10-30.** Maintainer-directed R10
operational follow-up to the shipped v2 coordinator ([review/44](44-bounded-bundled-llm-dispatch.md))
and its read-budget protections ([review/47](47-do-row-read-budget.md)). It changes no provider limit,
route policy, public protocol or Phase R ordering. The code ships dormant: every ledger stays in its
legacy table until the guarded `/v2/packed-state` cutover below is run during a drained global pause.

## Goal and result

Fewer billed rows written per cron tick, measured as an average and as a daily total, without
changing selection, admission, quota, failure or recovery behaviour. The maintainer prioritized
average and total writes over a hard per-cron cap; a 30–40 write average was the target, and the
2,000-read figure was a comparison, not a limit.

Real-coordinator replay of a private production-shaped capture (4,027 jobs, 87 routes, 15
providers), three 80-tick seeds, 15 active bundles, 90 in-flight calls, completions delayed up to
720 s, every ninth bundle abandoned and later reaped, a mixed result stream covering every failure
class, and a coordinator restart at tick 40. Costs include completions and 429 authorizations,
attributed to the tick that claimed the work; ingress and hourly maintenance are excluded.

| Seed | Variant | Mean / max writes | Mean / max reads | Total writes |
|---:|---|---:|---:|---:|
| 1 | legacy | 71.5 / 158 | 921 / 1,643 | 5,719 |
| 1 | packed | **31.3 / 62** | 1,081 / 1,919 | 2,503 |
| 2 | legacy | 69.8 / 156 | 856 / 1,227 | 5,581 |
| 2 | packed | **30.6 / 66** | 957 / 1,572 | 2,445 |
| 3 | legacy | 69.3 / 164 | 921 / 1,260 | 5,543 |
| 3 | packed | **30.9 / 68** | 1,121 / 1,854 | 2,471 |

Writes fall 56%; reads rise 12–22% (compact-group seeks and one scheduler read per transaction).
About one tick in five exceeds 40 writes (maximum 68); the average, not the maximum, is the
guarantee. Every run selected the same jobs and routes tick for tick and finished with identical
audited job, route, estimate, usage, failure and provider state (SHA-256 of normalized rows).
A fourth run rolled back to legacy authority at tick 40 and matched the legacy control through
tick 80. Each run seeded four synthetic failure cells (the capture has none) so cutover and
rollback migrate failure counters too. Abandoned bundles were genuinely reaped (38 jobs requeued
from nine expired bundles in seed 1). At the end of that run the packed sections measured routes
68 KB, estimates 92 KB, leases 0.5 KB, providers 2 KB (caps 512, 512, 128 KB and 128 KB). See the [evidence record](evidence/2026-10-30-llm-dispatch-packed-state-replays.md).

The idle queue now writes one scheduler row per tick (recovery statistics ride the accounting write;
about 1,440 rows a day instead of about 144), a cost the maintainer accepted.

## Design

One rule: **exactly one representation of each ledger is authoritative in every committed
transaction**, selected by durable scheduler flags. Nothing is dual-written.

| State | Legacy | Packed |
|---|---|---|
| Queued model index | `job_models` (one row per job and model) | `compact_queue` + `compact_groups` (one row per job; seeks per model set) |
| Provider ledger | `providers` | `scheduler.packed_providers_json` |
| Route ledger | `routes` | `scheduler.packed_routes_json` |
| Calibration estimates | `estimates` | `scheduler.packed_estimates_json` |
| Active leases | `jobs WHERE state='leased'` scans | `scheduler.packed_leases_json` (job id → bundle, route, expiry) |
| Attempt usage, failure counters | `attempt_usage`, `route_failures` | `packed_telemetry` (8 shards, hashed by route id) |

Scheduler-row sections (routes, estimates, leases, providers, bundles) load once per transaction,
serialize only when changed, and ride the single accounting `UPDATE` every transaction already
issues, so a claim that touches six routes still writes one ledger row. Telemetry shards are the
only extra rows a completion can write, at most one per route hash it touched. Code:
`src/packed_state.js` (store, folds, caps), `src/packed_migration.js` (cutover and rollback),
`src/compact_queue.js`, and the accessor methods in `src/coordinator.js` (`_routeRow`,
`_updateRoute`, `_estimateRow`, `_leaseSet`, `_recordRouteFailure`, …), each with a legacy twin.
Route updates apply the same operations in JS (`applyRouteSet`) that `legacyRouteUpdate` emits in
SQL; a test checks them against SQLite, as it does the failure and usage folds.

**Size policy (2 MB row limit).** Measured sizes: routes 70 KB, estimates 86 KB, providers 2 KB,
usage 229 KB across all shards. Caps: routes 512 KB, estimates 512 KB, leases 128 KB (1,024 entries;
in-flight limits keep it near 100), whole scheduler row 1.5 MB, each telemetry shard 256 KB. Routes
and leases fail closed past their cap (both are bounded by the catalog and in-flight limits). Past
its cap a shard drops its oldest days, failure cells before usage cells and today's last, and
estimates drop their stalest rows; both are diagnostics or calibration that fall back to priors, so
a completion is never aborted for size. Evictions log `packed_telemetry_evicted`.
`/v2/stats` → `authority.packed_chars` reports each section's size for the canary.

**Failure counters** are not a separate packed design. They are cells in the telemetry shards, with
the legacy upsert semantics (count, last status, nullable details, maximum) and one write per
route-hash shard per batch. An earlier paged design with spill, singleton and manifest tables was
removed: it needed a drain, a read cap and its own integrity story to protect a diagnostic counter.

**Recovery.** Immediate recovery stays the policy. Each sweep records checks, useful and empty
checks, requeues and expired bundles in `scheduler.recovery_stats_json`.
`RECOVERY_INTERVAL_MINUTES` (0–1440, default 0 = every tick) delays only expired-lease and
orphan-bundle cleanup; claims continue and expired leases stay reserved until the next check.

## Cutover and rollback

Both are single synchronous transactions (`_stagePackedCutover`, `_stagePackedRollback`) reachable
only through `POST /v2/packed-state` with `{"action": "cutover"|"rollback", "confirm":
"packed-state-<action>", "limits": {…bounded integers…}}`. The coordinator refuses unless:

- a **global** pause exists, was created at least 15 minutes ago, and has at least one minute left
  (rechecked before and after the rebuild, so an expiring pause cannot expose a partial move);
- the transaction is otherwise untouched, the schema and absence of triggers match expectations,
  and every source table fits its row limit;
- the planned writes, plus a reserve for existing leases and bundles and the caller's
  `reserveOtherWrites`, fit under today's enqueue stop.

Any throw rolls everything back (tested with failpoints at each stage). After the transaction the
old representation's rows are gone: cutover deletes the migrated legacy rows so a stale table can
never be mistaken for authority, and rollback rebuilds them from the packed rows after checking
that the lease registry still matches the leased jobs. Rollback also drops the compact queue and
telemetry tables (DROP bills no rows), so the move is repeatable.

Measured on the capture (4,015 queued jobs, 12 leases): cutover 16,173 reads / 4,516 writes
(plan 4,695); rollback after 40 ticks 19,558 reads / 22,488 writes (plan 22,505). Plans are
conservative upper bounds, so a refusal for insufficient write headroom means the move would not fit.

### Runbook

Merging deploys the Worker, and a deploy can restart the object, so the pause and drain come first.

1. Check account-wide DO rows written today. Cutover costs about 4,500 writes and a rollback about
   22,500 at 4,000 queued jobs; keep room for the rollback for the whole window.
2. `POST /v2/dispatch:pause` `{"scope":"global","seconds":3600}` and record the deadline. Preserve
   unrelated operator pauses. The pause lasts at most 3,600 s. **Re-issuing it resets its creation
   time**, which restarts the 15-minute age the cutover requires, so plan one pause for the whole
   window: drain (15+ min), merge and deploy, then cut over with at least a minute to spare.
3. Wait at least 15 minutes plus margin (cron executors are bounded by the 15-minute scheduled wall
   time). `GET /v2/dispatch:pause-status` must show `in_flight` 0 and `/v2/stats` no unexpected
   active bundles. Expired leases are carried into the registry, not lost.
4. Merge the PR and wait for the Worker deploy to finish. The new code is inert: `GET
   /v2/packed-state` must report all three flags `false` and the pause must still be active.
5. `POST /v2/packed-state` `{"action":"cutover","confirm":"packed-state-cutover","limits":{…}}`.
   `409` means nothing changed; read `detail`, fix and retry while the pause lasts.
6. Verify: all three flags `true`; `/v2/stats` `authority.packed_chars` populated;
   `queued_without_model_index` 0; queued count unchanged.
7. `POST /v2/dispatch:resume` for the pause you created. Watch the first claims, `recovery`
   statistics, `packed_telemetry_evicted` logs and account-wide rows for the next hour.
8. **Rollback**: pause and drain again, `action: "rollback"`, confirm all flags `false`. Never run a
   Worker version without this code while any flag is `true`: it would read the emptied legacy tables.

## Limits and not-done

- Operators must not edit packed sections or lease rows in Data Studio; legacy-table edits have no
  effect once authority moves.
- Partial indexes that would stop leased-state index churn (roughly a further 10 writes per tick) and
  bundle-oriented job state were evaluated and not built: the first costs a 4,000-write index build
  and the second needs a full lifecycle redesign. The 30–40 average target is met without them.
- Replay is a single-threaded simulation with simulated provider results. It excludes ingress,
  hourly maintenance, concurrent RPC interleavings and real gateway/B2 calls, and it cannot prove
  Cloudflare's billed totals; compare the first production day's rows against these figures.
- Compact-group cleanup is capped at 64 memberships per hourly pass; watch group cardinality.
- Reproduction: [bench/packed-replay](../workers/llm-dispatch-v2/bench/packed-replay/README.md);
  `cd workers/llm-dispatch-v2 && npm test` (606 tests, including the differential
  `test/packed-state.test.js`).
