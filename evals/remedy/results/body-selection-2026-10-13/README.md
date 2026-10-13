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
round positions. Execution uses patient600s/650s/16384-output settings; Flash-Lite uses
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
