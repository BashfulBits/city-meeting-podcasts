# R2 Class B usage audit — 2026-10-18

Status: audit complete; R2B-01 fix prepared locally, not deployed. Other findings remain proposals.
Code checked: `origin/main` at `1338cfd0`, refreshed before finalizing this report.
The earlier workflow-failure fix is already merged in PR #2486; these findings remain afterward.

## Verified usage and projection

Read the authenticated Cloudflare account overview and `citypods-spike` bucket Metrics page
around 20:23 UTC on October 18. Dashboard numbers are rounded and refresh asynchronously.

| Observation | Value | Interpretation |
| --- | --- | --- |
| Billing period | September 19–October 19 | This is not an October 1–31 counter |
| Account Class B, current period | 3.02M | About 30.2% of the 10M included allowance |
| Account Class A, current period | 261.32k | About 26.1% of the 1M included allowance |
| Current billable usage | $0.00 | Dashboard states no billable usage incurred |
| Bucket Class B, last 24 hours | 147.39k | 4.57M at the same daily rate over 31 days |
| Bucket Class B, last 7 days | 936.35k | 133.76k/day; 4.15M over 31 days |
| Bucket storage | About 17 MB | Request count, not storage capacity, is the concern |

**The observed figures do not confirm a projection above 50% of the allowance.** The latest
24-hour pace projects to 45.7%, the last week to 41.5%, and the nearly finished billing cycle
has consumed only 30.2%. A 31-day rate of 161,290 reads/day would reach 50%; 322,581/day would
exhaust the allowance. These are scenarios, not guarantees that traffic will remain constant.
Do not project the September 19 cumulative counter as if it began on October 1.

The bucket chart body showed slightly different totals (146.67k/day and 935.62k/week) from its
headline cards. Use the headline figures above consistently, and billing for financial status.
The small discrepancy does not change the conclusion. No per-object GraphQL breakdown was
queried; attribution below uses verified code paths and Actions logs, not a billing allocation.

Cloudflare's current [pricing](https://developers.cloudflare.com/r2/pricing/) includes 10M
Standard Class B requests/month, then $0.36 per million with billing-unit rounding. GET and
HEAD are Class B; LIST is Class A; DELETE is free. The
[analytics documentation](https://developers.cloudflare.com/r2/platform/metrics-analytics/)
provides `r2OperationsAdaptiveGroups`, including operation, object name, status, and time fields.

## Findings

### R2B-01 — High: the worker scan cap does not bound lease reads

`citypods/compute/external_worker.py:945–970` compares `summary.claimed` with `max_scan`.
A denied claim increments `skipped`, not `claimed`, and continues. Every attempt calls
`work_leases.claim`, whose first step is an R2 GET (`citypods/ops/work_leases.py:343`).
The full loop can therefore read every candidate even with a tiny configured scan cap.
`summary.scanned` is initialized to candidate count, so it is not an actual-probes meter either.

Offline reproduction on current code: 100 candidates, `max_scan=2`, all claims denied produced
**100 lease-read attempts, zero claims, and 100 skips**. No network or bucket writes were used.

The [17:41 ASR run](https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/39150713763)
reported 8,613 candidates/skips in each of three transcription workers, one claim each, and
zero completions. Both alignment workers skipped 59 candidates with no claims. The workers
independently rotate the same candidate list; they do not divide it into exclusive partitions.
A claimed item can also increment `skipped` after admission declines, so skips and claims are
not disjoint metrics. Lease probes remain approximately 25.8k per such transcription cycle.

**Recommendation:** count actual claim attempts separately and enforce a true probe budget.
A cap of 256 per transcription worker is an illustrative starting point, not an approved knob.
Preserve cheap bookkeeping, use a persistent rotating cursor/coverage policy, and preferentially
inspect fresh eligible work so a smaller budget cannot starve work later in a large queue.
Always retain the authoritative R2 read/CAS immediately before a real claim.

At five scheduled runs/day, three workers and today's 8,613-candidate shape imply approximately
3.88M attempted lease reads per 30 days. A 256-attempt bound would make that component 115.2k,
about **3.76M fewer attempted reads/month**. This is a workload-based estimate, not measured
billed savings; retries, other callers, schedule delays and changing backlog alter the result.

### R2B-02 — High: terminal leases remain in the discovery candidate set

`_base_candidates` rebuilds from records and filters queued/feed-visible work
(`external_worker.py:1190–1223`), while `claim` rejects same-version terminal `done`/`failed`
leases (`work_leases.py:343–353`). Rebuilding records does not make those leases claimable.
Workers repeatedly pay to rediscover the same terminal rejection across runs.

The [Worker report](https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/39150128668)
found 12,750 pending ASR entries: 7,673 failed leases, 1,151 done leases, 3,343 absent, and 583
queued. That is 8,824 terminal lease states still represented among pending ASR work.
This is broader than the workers' 8,613 eligible subset; do not assume identical membership.
The report did not enumerate recipe-version matches: a terminal lease for an older version can
be reopened legitimately, so these totals alone do not prove every terminal entry is suppressed.

**Recommendation:** retain a derived B2 eligibility/suppression snapshot keyed by source, UID
and pipeline version, with bounded expiry and periodic repair. Share it across workers rather
than probing terminal history in every shard. Verify done artifacts/record adoption and
classify failures separately. A new recipe must invalidate suppression; operator requeue and
recovery must remain visible. Do not automatically reopen all failures or cache lease authority.
Savings overlap R2B-01 and must not be added to its estimate.

### R2B-03 — Medium: a daily diagnostic performs a full lease census

`scripts/compute/report_workers.py:300–308` GETs one lease for every pending ASR entry.
The observed report's 12,750 entries imply **382,500 attempted reads per 30 daily reports**.
The report also labels every nonempty owner as in-flight, even for terminal leases; its
8,824 owner counts match the terminal count despite zero budget reservations in flight.

**Recommendation:** default to the 64-bucket active index plus local record/backoff counts.
Report active state/expiry accurately; label terminal totals as sampled/cached unless a full
census ran. Make an exact full-history census explicit/manual or infrequent, preserving the
backoff watchdog without requiring a lease read per archived candidate. Use a bounded rolling
sample if terminal failure coverage is required daily. This saving is separate from worker scans.

### R2B-04 — Medium: provider slot waiters repeatedly scan every slot

`citypods/provider_leases.py:248–279` scans all occupied slots and sleeps a fixed poll interval.
`config/site_config.yml` currently has four Granicus slots and a two-second poll: a continuously
blocked waiter can attempt roughly **7,200 reads/hour**, before network latency. Many local
waiters multiply this. This is a code-derived exposure, not measured current production traffic.

**Recommendation:** one prober per process/domain, a local waiter queue, and capped exponential
backoff with jitter while all slots are occupied. Reset promptly after success and retain stop
responsiveness. Increasing a blocked-wait interval from 2s toward 20s can reduce sustained poll
traffic about 90%, at the cost of slower acquisition; measure backlog throughput before tuning.
Keep slot count, TTL/renewal ownership and cross-process CAS safety unchanged.

### R2B-05 — Low: avoidable extra reads in LLM and telemetry paths

`citypods/compute/llm_scheduler.py:625,669–672` reads the same budget key again when no route is
selected, only to compare serialized bytes. Preserve the original raw body from the first read
and compare with it; a changed ledger's conditional write must still fail/retry on its old ETag.
That removes one GET per no-route attempt without relaxing quota enforcement.

`external_worker.py:607` fetches the budget for a runtime coefficient. Claim telemetry recomputes
the estimate, including two identical `_estimate_gpu_seconds` calls (`:1954–1957`). Reuse the
already-computed estimate and short-lived read-only coefficient snapshot for prediction/logging.
Do not cache authoritative spend admission, reservation, settlement or ownership checks.
These are secondary savings; no production volume estimate is available.

### R2B-06 — Medium: existing counters cannot enforce a real request budget

`citypods/storage/routing.py:228–264` counts high-level router calls, not adapter/SDK attempts;
retries beneath the router can multiply HTTP reads (`storage/s3.py:153–173`). Its `delete` method
also increments Class A even though Cloudflare documents deletion as free. No production caller
of `RoutingStorage.telemetry()` was found; current logs do not attribute R2 reads by prefix.

**Recommendation:** emit per-prefix GET/HEAD/PUT/LIST/free-delete and retry counts once per run,
including attempted scans, denied claims and useful completions. Count actual HTTP attempts if
using the metric for charge estimation; retain Cloudflare billing as the financial authority.
Query daily analytics by operation/object prefix and compare with emitted process counts.
A derived B2 usage snapshot avoids making the monitor itself a new hot R2 counter.

## Recommended sequence and guardrails

1. Fix actual-probe accounting and the ineffective scan bound, with no-claim regression coverage.
2. Add terminal-aware, recipe-versioned candidate suppression and fair cursor/resumption.
3. Replace the default daily full lease census with bounded diagnostics.
4. Add attribution and review provider-waiter polling; remove the small duplicate reads.
5. Set account budget notifications and a projected 50% warning, with staged degradation of
   optional diagnostics/backfill before productive work. Alerts are not spending caps. Keep a
   reserve for claim safety, lease renewal and budget bookkeeping if an application budget is added.

All steps above are proposals. No schedules, concurrency settings, quotas, routes, lifecycle rules,
buckets, stored leases or budgets were changed. A runtime change should use its own reviewed fix.
Avoid a blanket R2 shutdown, routing CAS state to non-CAS B2, caching authority, or deleting leases
as a cost workaround. Keep archival state append-only and artifact UIDs/keys stable.

The existing prefix router already keeps audio, transcripts, records and broad state listings on
B2. The v2 LLM dispatch worker uses SQLite Durable Objects and B2, with no R2 binding declared in
its Wrangler configuration. Moving media off R2 or slowing its minute cron would target the wrong
component. The indexed reaper is already bounded at 64 buckets plus active leases and a rotating
integrity slice; preserve that recovery mechanism rather than disabling it indiscriminately.

This audit extends [review/17](17-state-store-backend-evaluation.md)'s R2 coordination boundary
and [review/18](18-work-distribution-sharding.md)'s cost discipline: formerly cheap per-item reads
are now multiplied by backlog and worker count. It does not replace their CAS contracts.
[review/44](44-bounded-bundled-llm-dispatch.md) and
[review/47](47-do-row-read-budget.md) cover a separate DO/B2 dispatch system and SQLite row meter;
DO row reads must not be combined with R2 Class B operations.


## R2B-01 implementation follow-up — prepared, 2026-10-18

The maintainer authorized investigation/fix of the dominant scan issue. Two causes are addressed:
`max_scan` counted acquired claims rather than attempts, and the internal worker's unlimited
claim count made its default scan allowance effectively unlimited. The prepared change bounds
attempts, defaults internal workers to 256, and honors CLI/environment/backend-policy overrides.
The helper-level limit also protects internal workers instantiated outside the CLI entrypoint.

Adjacent matrix slots now scan consecutive windows, advancing by lane worker count and Actions
run number. For a stable 8,613-entry queue, three workers consuming 256 probes each cover the
queue in at most 12 consecutive runs. This is not a wall-clock guarantee under queue churn,
stopped/skipped jobs, or early productive completion. External workers keep owner-based rotation.
No new durable cursor or R2 coordination object is introduced. `scanned` now counts attempted
claims; an exhausted window logs the full candidate count separately. Claim ownership still uses
fresh R2 GET/CAS. Internal shutdown now flushes partial transcript batches before closing
inference; cheap final flushing/settlement remains outside the scan admission gate.

The earlier 3.76M/month reduction remains an estimate pending deployment and live metrics.
This follow-up does not implement terminal suppression, report census changes, provider backoff,
or budget telemetry. No production settings or schedules were changed during preparation.


## CodeRabbit CLI review — 2026-10-18

CodeRabbit CLI 0.9.0 reviewed the committed branch against `origin/main` with `--agent`.
Both findings were verified against current code:

- **Minor: invalid scan configuration can silently disable work. Fixed and expanded.** Negative
  scan settings now fail validation in the shared worker configuration, covering direct callers,
  internal CLI/environment overrides, and external workers. Malformed environment integers report
  the setting name. Startup tests assert invalid settings fail before constructing a worker.
  The suggestion to reject zero is not adopted: zero is an explicit, documented no-probe setting,
  covered by tests; it must not be confused with silently accepting a negative typo.
- **Trivial: coverage depends on queue/configuration stability. Clarified.** The ordering comment
  and architecture text now explicitly require a stable queue and worker configuration. Runtime
  ordering is unchanged; overlap/churn limitations remain documented rather than hidden.

No second CLI review was needed for these bounded validation/comment changes. The raw structured
CLI output was retained locally for the maintainer's review. Focused regression tests passed.
