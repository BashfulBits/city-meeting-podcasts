# review/46 — Preferred + backup model dispatch, and the Nemotron chapter-agenda migration

**Maturity: L3 dev-ready · authored 2026-09-13**

Owner: dispatch Worker maintainers. Scope: `citypods/compute/llm_lanes.py`,
`citypods/compute/llm_policy.py`, `citypods/compute/llm.py`'s v2 enqueue path, and
`workers/llm-dispatch-v2/src/{routes.js,coordinator.js}`. First consumer: the `chapter-agenda`
lane (`config/site_config.yml`).

## Motivation

`chapter-agenda` was pinned to `mistral/mistral-medium-latest`, which became permanently blocked:
primary, secondary, and tertiary Mistral keys all report an identical zero rate limit
(`x-ratelimit-limit-req-minute: 0`) for every flagship model — an account-tier restriction, not a
per-key billing state (confirmed live, 2026-09-12). A replacement model was needed, and a
30-episode benchmark (scored with the project's own span-overlap matcher,
`scripts/research/agenda_chapters/audit_locator_crosswalk.py::_pair_features`/`_chapter_status`)
found `nvidia/nemotron-3-ultra-550b-a55b:free` (at `max_tokens=32768`, after fixing a
reasoning-token-budget-exhaustion bug identical to one already found in DeepSeek) the best
candidate: 29/30 valid JSON (97%), 77.9% recall, **87.4% precision — the best of any model
tested**, at the cost of ~134s latency and an NVIDIA free-tier route with no published rate-limit
table (best-effort capacity, occasional `503`s even after retries). `gemini/gemini-3.1-flash-lite`
and `gemini/gemini-3.5-flash-lite` were the next-most-reliable candidates (100%/97% valid JSON,
much faster, slightly lower precision).

Picking a best-effort-capacity model as *primary* only makes sense with a real fallback: a job that
is genuinely stuck (not just slow) should be able to fall through to a more reliable model rather
than sit unresolved. Nothing in either dispatch path (the Python direct scheduler or the Cloudflare
Worker) had a mechanism for this before this change — the closest existing thing,
`config/provider_limits.yml`'s `model_routing` map (consulted by
`citypods/compute/llm_scheduler.py::select_route`), is a config-driven **quota-exhaustion**
overflow: it only kicks in once a model's own routes are genuinely rate-limited/exhausted in the
ledger. A `503`, a timeout, or a bad-JSON response doesn't look "exhausted" to that ledger — the
route still has capacity on paper — so `model_routing` would never fire for Nemotron's actual
failure modes. `model_routing` is also Python-scheduler-only: the Worker (`workers/llm-dispatch-v2`)
explicitly does not implement it (`coordinator.js`'s own docstring: "v2 does not expand
config/model_routing").

Since the user need — "a preferred model with a backup that kicks in once a job looks stuck" — is
generic and expected to recur across LLM tasks, this is built as reusable lane infrastructure, not
a `chapter-agenda`-specific hack.

## Design

### Config surface

`config/site_config.yml`'s `llm_lanes` entries gain two optional fields, validated in
`citypods/compute/llm_lanes.py::parse_lanes`:

```yaml
chapter-agenda:
  models: ["nvidia/nemotron-3-ultra-550b-a55b:free"]
  backup_models:
    - "gemini/gemini-3.1-flash-lite"
    - "gemini/gemini-3.5-flash-lite"
  backup_after_attempts: 12
```

- `backup_models` and `backup_after_attempts` must be set together or not at all.
- `backup_models` must not overlap `models`, must be non-empty/deduplicated strings (same rules as
  `models`), and is rejected on a `dispatch_shape: per_model` lane (those fan out one job per model
  for *comparison*; a failure-based fallback is a different concept).
- `backup_models` is **not** counted in `LaneConfig.ingress_write_units_per_job` — a job's per-job
  ingress cost is still `3 + len(models)`. Backups are never part of a job's indexed model set at
  enqueue time; they only ever activate on an already-queued job's later lease attempts.
- Enforced by the same ingress lane allowlist as `models`
  (`coordinator.js::_modelsOutsideLane`, `scripts/compile_llm_lanes.py`'s compiled
  `ingress_reservations.json`) — a lane cannot smuggle an unbudgeted/unreviewed model in through
  `backup_models` that ingress never checks.

`citypods/compute/llm_policy.py::LLMRequestPolicy` carries the same two fields, threaded from a
lane into a job at build time (e.g. `citypods/chapter_jobs.py::build_agenda_job`), and serialized
into the v2 Worker's `policy_json` payload (`citypods/compute/llm.py`, the one place that dict is
hand-built) only when both are set — every other lane's wire payload is byte-identical to before
this change.

### Activation: two independent triggers, one counter pair

`workers/llm-dispatch-v2/src/routes.js`:

```js
export function backupModelsActive(job, policy) {
  const backupModels = Array.isArray(policy?.backup_models) ? policy.backup_models : [];
  const threshold = policy?.backup_after_attempts;
  if (backupModels.length === 0 || !Number.isInteger(threshold) || threshold <= 0) return false;
  const attempts = Number.isInteger(job.attempts) ? job.attempts : 0;
  const schemaRetryCount = Number.isInteger(job.schema_retry_count) ? job.schema_retry_count : 0;
  return attempts >= threshold || schemaRetryCount >= 1;
}
```

- **`jobs.attempts`** is the Worker's existing, durable, cross-lease dispatch counter
  (incremented in `attemptStarted` on every real provider call, survives a requeue back to
  `queued` after a retryable/terminal error). Crossing `backup_after_attempts` means the job has
  been dispatched that many times without ever completing successfully.
- **`jobs.schema_retry_count`** is new (this change): incremented on every `schemaRetry` clone (the
  existing mechanism for correcting a response that failed JSON-schema validation *after* a
  successful HTTP response). Reaching 1 means this job already needed at least one such
  correction — a model-output-quality failure, a different kind of problem from capacity/latency,
  worth escalating immediately rather than waiting for the attempts threshold.
- `schemaRetry`'s clone now carries `attempts` **forward** from the source job (previously
  hardcoded to `0`) alongside the incremented `schema_retry_count`, so a chain of schema
  corrections and a run of plain failed attempts both accumulate toward the *same* threshold
  rather than needing two separate escalation paths.

`modelsForJob(job, policy)` unions `backup_models` into the eligible model list once active; both
`routesEligibleFor` (route selection at claim time) and `coordinator.js::_modelsForQueuedJob`
(the `job_models` index used to find a job at all) call it, so a job's backup eligibility is
correctly re-derived on every requeue (every requeue path already re-indexes from a freshly-read
row that carries the current `attempts`).

### Reachability: extending the retry ceiling for a job with backups configured

The Worker's own class-specific retry ceilings exist to bound how long a job with **no fallback**
gets retried (`MAX_5XX_RETRIES=1` → 2 attempts, `MAX_UPSTREAM_CAPACITY_RETRIES=8` → 9 attempts)
before `completeBatch` marks it `failed`. Left as-is, these ceilings make a `backup_after_attempts`
in the 5-20 range **unreachable** for the realistic dominant failure mode here: a raw HTTP 5xx
(e.g. NVIDIA's own "Service temporarily overloaded" 503) classifies as `server_error`
(`classify.js`'s HTTP-5xx rule runs before any 429/400 check), gated by `MAX_5XX_RETRIES=1` — the
job would terminally fail on its second attempt, long before `attempts` could ever reach a
double-digit threshold. Caught in review (CodeRabbit) against the first version of this change,
which picked `backup_after_attempts: 12` specifically to sit *above* the retry budgets instead of
fixing this.

Fix: `completeBatch` reads the job's own `policy_json.backup_models`/`backup_after_attempts` and,
when both are set, raises every one of its class-specific ceilings to
`backup_after_attempts + <that class's own normal budget>` (`_retryCeiling`, `coordinator.js`).
This guarantees the job survives to backup eligibility on `attempts` alone, then gets its ordinary
per-class retry allowance again once a backup model is in play, rather than an untested unbounded
extension — a job with no configured backups is completely unaffected (`_retryCeiling` returns the
base ceiling unchanged).

**Chosen `backup_after_attempts: 12`** for `chapter-agenda`: comfortably within the 5-20 range this
mechanism expects, now that reachability no longer depends on ducking under a fixed retry budget.
Given Nemotron's benchmarked ~97% real-world success rate, a job needing 12 attempts is a genuine
outlier, not the common case.

### Scope: Worker (`queue_only`) dispatch only

Direct-mode Python dispatch (`citypods/compute/llm_scheduler.py::select_route`, used by lanes that
don't set `queue_only=True`) has no equivalent persistent, cross-run attempt counter today — a
direct call's retry loop lives and dies within one process invocation. Extending this mechanism to
direct-mode dispatch would need a new counter (most naturally living in the LLM budget ledger,
`citypods/compute/llm_budget.py`) and is left as a follow-up if a direct-mode lane ever needs it.
Every current dispatching lane that might want this (`chapter-agenda`, `chapter-locator`,
`topic-tags:*`, `r6-moments`) already uses `queue_only=True`.

### Ingress enforcement of `backup_after_attempts`

`_modelsOutsideLane` already stops a job naming a model outside its lane's declared
`models`/`backup_models`, but that alone doesn't stop a job from declaring a *lower*
`backup_after_attempts` than the lane intends — the models named would still be legal, only the
threshold would be wrong. `scripts/compile_llm_lanes.py` also compiles `backup_after_attempts`
into the reservation map, and `coordinator.js::_backupThresholdBelowLaneMinimum` rejects
(`backup_after_attempts_below_lane_minimum`) any job whose own policy undercuts it, at the same
three admission points `_modelsOutsideLane` already guards (new-job enqueue, idempotent supersede,
`schemaRetry`). A job that declares no backup models at all is unaffected.

### Recording which model actually completed a job

`JobHandle.model` is set once at enqueue time from `allowed_models[0]` and never updated
afterward — it always names the *primary* model, even for a job that later completes on a backup
route. `pollBatch` now resolves a completed job's actual route (`lease_route_id`, left untouched by
`completeBatch`'s success path) back to its canonical model via `routes.js::modelForRouteId` (a
cached reverse of `model_routes_map`, since `routes_by_id` entries carry no `model` field of their
own) and returns it; the Python client (`_poll_batch_chunk`/`_resolve_completed`,
`citypods/compute/llm.py`) prefers that returned model over the handle's stale guess when building
the final `JobResult`. Without this, `AgendaCandidatesArtifact.model` (and by extension the
`AgendaChapterCandidatesStage.process()` currency check above) would silently misattribute every
backup completion to the primary model.

## Alternatives considered

- **Extend `model_routing`** to also fire on non-quota failures. Rejected: `model_routing` is a
  config-driven, quota-exhaustion-specific overflow map with ranking semantics tuned for that case
  ("fully exhaust one model's free-then-paid routes before ever trying the next model in the
  list"); conflating it with a per-job failure-count signal would make both harder to reason about,
  and it doesn't exist in the Worker at all today.
- **`schema_retry_count >= 2`** (require the correction *itself* to also fail before escalating), a
  stricter reading of "retried due to a JSON validation error and still failed." Not chosen:
  nothing chains a second real correction today (`scripts/llm_deferred_sweep.py`'s sweep discards
  after one attempt), so `>= 2` would be unreachable without also building that chaining. `>= 1`
  gives the correction attempt itself a chance to use a backup model instead of being forced back
  onto whatever model just produced bad JSON, which is a reasonable independent justification.

## Backfill

`CHAPTER_AGENDA_PIPELINE_VERSION` bumped `"1"` → `"2"` so the back catalog reprocesses under
Nemotron. Per AGENTS.md's rule that a pipeline-version bump states its backfill story: this is
gradual and automatic, not a bulk migration — every episode whose stored
`generated_agenda_candidates.model`/`.pipeline_version` no longer matches production is picked up
by the ordinary `chapter-agenda.yml` cron (every 2 hours), bounded by the lane's own
`max_dispatches_per_run: 1000`/day, draining over however many days the backlog takes. Two
pre-existing gaps in that reuse check were fixed alongside this (see `CHANGELOG.md`'s entry and
`AgendaChapterCandidatesStage.process()`'s `is_current_artifact` check and its pending-job
retirement branch) — without them, the version bump would not have reliably re-queued anything.
