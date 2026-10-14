# Independent body-selection comparison

The maintainer requested the correct ownership prompt on all 60 recordings for six models
on October 13, 2026. Each model sees official evidence, recording metadata, feed names
and general approved policy. It sees no suggested feed, prior model answer, retained
record-specific disposition, selector predicate, gold or semantic question identifier.

The frozen input packet and separately stored answers are under
`evals/remedy/holdout/body-selection-2026-10-13/`. The 113 prior claim questions collapse
to 60 unique source/UID pairs. All original packet/results remain unchanged.

The prompt asks for cited body/program names and all appropriate existing feeds, supporting
joints, real unconfigured bodies, archived formats and insufficient evidence. Exact feed
selection on 40 assignments, policy compliance on 15 controls, and five unsupported-owner
guardrails are separate metrics. The five unknown owners are not non-government truth.
Free-text body names remain available for human evidence review, not fabricated alias scoring.
This is a reused historical challenge, not a newly disjoint production-admission holdout.

Models: Flash-Lite, Kimi, full NVIDIA GLM, GLM Flash, Sol and DeepSeek. Gemini 3.7/3.8 and
Astra are excluded. Current production routes, quotas, models and feed rules are unchanged.

One initial attempt plus three retries per round, at most three rounds, means at most 12
submitted attempts per model/recording and 4,320 across the 360 pairs. A valid answer, even
if wrong, stops all retries for that pair. Gold is never loaded by the live runner. Failures
receive at least 30 minutes of provider cooldown; actual quota and production capacity waits
remain enforced. Missing final observations hold the pair as an unknown outcome. No SDK retry.

The controller has a seven-day bounded deadline, preserved on restart. It shares the older
evaluation's call lock, so either evaluator defers while the other has a request in flight.
Each call uses existing shared CAS quota accounting and provider pause/drain/reserve controls.
Default is an offline plan; execute only through the approved credential wrapper:

```sh
/usr/local/bin/citypods-env env R2_BUCKET=citypods-spike PYTHONPATH=. \
  python evals/remedy/results/body-selection-2026-10-13/run.py --execute \
  --results-dir /private/tmp/p1-body-selection-evaluation/evals/remedy/results/body-selection-2026-10-13 \
  --shared-lock /private/tmp/p1-kimi-reviewer/evals/remedy/results/varied-2026-10-11/run.lock
```

Do not start a duplicate. Status and immutable intents/attempts show actual progress and
round positions. Execution uses patient900s/950s/16384-output settings; Flash-Lite uses
120s/150s/4096. No admission, activation, paid fallback or production quota change.

Score separately after calls, preserving all input hashes and every attempt:

```sh
PYTHONPATH=. python evals/remedy/results/body-selection-2026-10-13/score.py \
  --manifest evals/remedy/holdout/body-selection-2026-10-13/manifest.json \
  --gold evals/remedy/holdout/body-selection-2026-10-13/gold.json \
  --results-dir evals/remedy/results/body-selection-2026-10-13 \
  --out /tmp/body-selection-score-unique.json
```

Use a new score filename; existing observations/reports are never overwritten.

`hashes-v2.json` binds the complete gold document; the initial metadata helper hashed
only its inner cases. Original `hashes.json` is retained as superseded metadata. Frozen
model inputs, expected answers and live requests did not change in this correction.


Concurrent allocation: --priority-results-dir points to the older comparison using
this same shared call lock. While both controllers have eligible work, the older run
receives two submissions per new submission (66.7%). Failed submissions count;
preflight deferrals do not. The new run borrows unused slots during older cooldowns,
capacity waits or held outcomes. Allocation persists in controller-status.json;
changed/stale older state holds new calls. After completion or controller exit, new
work uses all slots. No response, gold answer, provider quota or retry limit changes.


The maintainer-approved Groq expansion adds GPT-OSS120B high and Qwen3.8-27B high
(native xhigh), for eight models and480 answers. The original60 inputs, gold,
seven-day deadline and prior six-model answers are unchanged. Resume migrates only
the approved six-to-eight model set; maximum attempts become5760 for480 pairs,
still12 per pair. Older claim-checking totals remain678, with two-thirds priority.

Evaluation-local copies of existing physical Groq routes pin documented high
controls and parsed reasoning for JSON output,16384 output cap and approved900s
request/950s case bounds. No production catalog/selection changes. Largest packets
are attempted first, followed by a joint meeting and an insufficient-proof case.
Keep complete evidence even if a provider rejects the borderline Qwen packet.
Both local and Worker daily-token usage count conservatively, with20% production
headroom. Qwen was near its existing180000-token budget at expansion and must wait.
Request controls and provider usage are retained with each immutable observation.


## Temporary coordination outage mode approved October13

Explicit --coordination-bypass resumes this existing bounded evaluation using local
CAS quota accounting. It skips unavailable Worker capacity/pause/drain/reservation
requests; it does not skip local quotas, retry limits, cooldowns or the shared call
lock. Results and intents record bypass and do not claim production was drained.
Production jobs' usage cannot be seen in this mode, so provider failures still stop
and cool down that route. Normal coordination checks remain the default.

The older controller requires its existing retry allowance and refuses --new-run
with bypass. Both controllers preserve their original horizons and prior answers.
Qwen remains held during bypass until daily token capacity can be verified. GPT-OSS
keeps its local180000/day cap and20% reserve; Worker token usage is recorded unknown.
Use the same shared lock and saved result directories when switching modes. Stop
only between submitted requests, then resume from tested code without changing caps.


Qwen hold superseded2026-10-13: the maintainer removed the unconditional outage hold.
Qwen can now submit under --coordination-bypass using the same local daily-token cap,
20% reserve and CAS accounting as GPT-OSS. No manual unblock is needed at a later
capacity window. Midnight alone is not proof of a provider token reset. Worker usage
remains unknown; preserve all caps, retries, controls and original experiment horizon.
Only the obsolete explicit-hold wait is cleared on resume; real quota/cooldown waits stay.

On the first Qwen release resume, Qwen gets the first eligible round-robin slot after
the current call finishes. Later resumes preserve the normal queue position. Token
waits still take precedence; the release does not reserve a provider capacity window.


## Provider-authoritative Groq stop approved October13

For this existing experiment only, `--groq-until-429 --coordination-bypass` removes
the local daily-token ceiling and reserve for GPT-OSS and Qwen. It preserves request
and per-minute limits, one-call locking, frozen inputs, retry allowances and deadline.
Eligible Groq requests take priority; other models use slots while Groq waits or stops.
Each route stops at its first new submitted provider HTTP429, or after all60 answers.
The saved authorization timestamp excludes earlier errors. Pauses persist across
cooldowns, date changes and resumes; this command never changes production pauses.

Actual provider token usage remains in immutable results. The direct backend stops
adding daily-token debt when the evaluation route has no daily ceiling, so the old
CAS daily counter is incomplete in this mode. Request/minute accounting remains;
Worker production usage remains unknown. No claim of unused production capacity.
