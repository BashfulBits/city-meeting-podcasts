# Evidence: packed-state replays (2026-10-30)

Supports [review/57](../57-llm-dispatch-row-efficiency.md). Local `wrangler dev` 4.131.1 (workerd),
real `LLMSchedulerDO` unmodified except for statement metering in the replay subclass, driven by
[`bench/packed-replay`](../../workers/llm-dispatch-v2/bench/packed-replay/README.md). The capture is
private (job metadata, lease tokens) and is not in the repository; only aggregates appear here.

Setup: 4,027 jobs, 87 routes, 15 providers, 94 estimates, 282 usage cells, no preexisting failure
rows; `MAX_ACTIVE_BUNDLES=15`, `MAX_IN_FLIGHT_LLM_CALLS=90`; two priming ticks in legacy mode, then 80
measured ticks of 60 s; completions delayed 5–720 s; every ninth claimed bundle abandoned (its
leases expire and are reaped); results drawn per job from success 70%, request defect 8%, own_rpm
429 (authorized first) 5%, upstream capacity 5%, route input limit 3%, deferred late 3%, route
unavailable 2%, payment required 2%, output budget exhausted 2%.

| Seed | Variant | Jobs claimed | Mean / max writes | Mean / max reads | Ticks > 40 writes |
|---:|---|---:|---:|---:|---:|
| 1 | legacy | 372 | 71.5 / 158 | 921.0 / 1,643 | 57 |
| 1 | packed | 372 | 31.3 / 62 | 1,081.0 / 1,919 | 18 |
| 2 | legacy | 366 | 69.8 / 156 | 855.5 / 1,227 | 53 |
| 2 | packed | 366 | 30.6 / 66 | 957.3 / 1,572 | 16 |
| 3 | legacy | 366 | 69.3 / 164 | 920.7 / 1,260 | 52 |
| 3 | packed | 366 | 30.9 / 68 | 1,121.2 / 1,854 | 18 |
| 1, rollback at tick 40 | packed→legacy | 372 | 45.0 / 123 | 880.7 / 1,643 | 35 |

Parity: for every pair, the per-tick selection hash (job id and route) was identical; audits every
ten ticks and at the end matched on SHA-256 of normalized jobs, routes (lease tokens mapped to
owning job), estimates, usage cells, failure cells and providers. Seeds 1–3 restarted the object
(`ctx.abort`) at tick 40 on both sides. The final lease-registry count equalled the leased-job count.

Migration (seed-1 shape, 4,015 queued jobs, 12 active leases, 87 route / 94 estimate / 282 usage
rows): cutover 16,144 reads / 4,514 writes against a plan of 4,691; rollback of 3,829 jobs 19,499
reads / 22,486 writes against a plan of 22,503. Failed attempts, ingress and account-wide traffic
are excluded.

Caveats: the replay client retries a whole run when the local proxy drops a connection; one such
transport error was seen on `complete` and none on `claim`. Results are one capture on one day.
