# Approved varied comparison and recovery

The frozen challenge is 113 questions across 60 recordings. Primary/secondary Gemini keys
were pooled under the same logical model. On October 13 the maintainer discontinued both
Gemini 3.7/3.8 models because of their low daily quotas. The active six-model target is 678.
Historical Gemini and Astra observations remain preserved but excluded from scoring and calls.
This is a regression challenge, not independent production-admission evidence.

`run.py` makes bounded single-candidate batches with existing shared quota accounting,
production headroom and pause/drain/reserve controls. `--retry-failed` skips successful pairs
and admits unanswered failures after their cooldown and attempt limits. Each case permits one
provider call per invocation. No SDK retries are enabled. Every attempted result is immutable.

`backfill.py` replaces the temporary shell loop's unreachable retry stage. It plans each
candidate independently, uses daily reset timestamps, and retains status across restarts.
Default execution is an offline plan; model calls require `--execute`.

From the checkout containing this code, inspect an existing result directory without credentials:

```sh
PYTHONPATH=. python evals/remedy/results/varied-2026-10-11/backfill.py \
  --results-dir /private/tmp/p1-kimi-reviewer/evals/remedy/results/varied-2026-10-11
```

The offline plan derives eligibility from saved observations; it does not query live quota.
Execution performs that quota/capacity check before admitting a call.

After the legacy runner has exited, start a bounded replacement through the approved wrapper:

```sh
/usr/local/bin/citypods-env env R2_BUCKET=citypods-spike PYTHONPATH=. \
  python evals/remedy/results/varied-2026-10-11/backfill.py --execute \
  --results-dir /private/tmp/p1-kimi-reviewer/evals/remedy/results/varied-2026-10-11
```

The default horizon is 24 hours with at most 256 new provider attempts. Restarts preserve
the original deadline and allowance. An explicit `--new-run` begins another bounded run;
`--horizon-hours` accepts up to seven days and `--max-calls` never exceeds 256. A completed
run is recognized from successful logical model/case pairs, not elapsed time or a green exit.
The legacy runner is retired. The eight-model recovery controller was stopped while idle
after the maintainer discontinued Gemini 3.7/3.8. A new six-model run preserves the daily
attempt cap and waits for midnight UTC before retrying the five remaining GLM/Sol cases.

Recovery safeguards:

- At most three calls per physical candidate/case per UTC day and three retries per controller
  run. Completed answers are never requested again, including via the other Gemini key.
- Failed routes cool down for at least 30 minutes; longer Retry-After values take precedence.
  A daily quota wait ends at the later applicable Worker/local reset. Capacity-read failures
  defer work. Provider-busy waits preserve production priority.
- Original sanitized HTTP status, message, retry delay and category survive generic backend
  attempt-limit errors. Invalid answers and transport failures remain separate evidence.
- An immutable intent is written before a new call. If interruption loses the final observation,
  its outcome stays unknown and the case is held across both keys for manual reconciliation.
  Automatically repeating a lost-response call would risk a duplicate request.
- The controller has its own lock; runner invocations use the existing provider-call lock in
  the selected result directory. Concurrent controllers and duplicate in-flight batches refuse.
- `controller-status.json` reports counts, per-route eligibility, next action, call allowance,
  and an explicit completion, horizon, budget, retry-exhaustion or error reason. Old observation
  files are never modified. Original provider settings and production quotas are unchanged.

Score with explicit approved truth after requests:

```sh
PYTHONPATH=. python evals/remedy/results/varied-2026-10-11/score.py \
  --results /private/tmp/p1-kimi-reviewer/evals/remedy/results/varied-2026-10-11/batches \
  --manifest evals/remedy/holdout/backlog-varied-2026-10-11/approved-v2/manifest.json \
  --gold evals/remedy/holdout/backlog-varied-2026-10-11/approved-v2/gold.json \
  --out /tmp/varied-recovery-score.json
```

Use a new output filename for each score; it is created exclusively. Gold never enters the
controller or model messages. The diagnosis and raw batches explain historical failures;
quality, completion, latency and unresolved identity/maximum-effort holds remain separate.

## One-off immediate retries approved October 13

The maintainer approved five more attempts on each of the two remaining full GLM and three
Sol questions, beginning immediately. The immutable `retry-allowance-2026-10-13-v1.json`
binds those exact pairs to this manifest and authorization time. Only these pairs bypass
the daily evaluator cap. At most five calls since that timestamp are allowed per pair,
including unmatched intents; daily resets and new controller runs do not replenish them.
A valid answer immediately suppresses further calls. Normal quotas, headroom, timeouts,
provider cooldowns and unknown-outcome holds continue to apply. Resume with the same file:

```sh
/usr/local/bin/citypods-env env R2_BUCKET=citypods-spike PYTHONPATH=. \
  python evals/remedy/results/varied-2026-10-11/backfill.py --execute --max-calls 25 \
  --retry-allowance \
  /private/tmp/p1-kimi-reviewer/evals/remedy/results/varied-2026-10-11/retry-allowance-2026-10-13-v1.json \
  --results-dir /private/tmp/p1-kimi-reviewer/evals/remedy/results/varied-2026-10-11
```

The initial authorized handoff used `--new-run`; ordinary restarts omit it. The stored
allowance hash must match on restart. Keep the original allowance file unchanged.


## Patient request timeout extension approved October 13

Future non-Gemini requests use900-second provider timeouts,950-second case deadlines
and1000-second covering provider pause leases. Flash-Lite retains120s/150s/4096.
Controllers reserve1050 seconds before their original horizon expires. This changes
only local evaluation waiting time; all retry counts, daily quotas, output caps and
original controller deadlines remain intact. Earlier600-second observations stay
immutable and retain their original settings. A provider gateway may still time out
sooner; extending our local wait cannot bypass an upstream timeout.
