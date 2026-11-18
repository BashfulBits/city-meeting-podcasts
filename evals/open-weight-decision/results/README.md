# Phase 3 results (review/60), 2026-11-18

Answer files (`*-answers.json`, `*-main.json`, `*-chapters-single.json`) hold each model's probability
of "yes" per question, its token count, per-request seconds and status. `cloud-costs.json` prices the cloud executions at list rates (not billing).

Reproduce the scores (needs no cloud access and no keys):

    evals/open-weight-decision/results/score_all.sh dev        # also: holdout, all (both pooled)
    python scripts/open_weight_eval_table.py evals/open-weight-decision/results/2026-11-18-summary-holdout.json

The scored summaries (`2026-11-18-summary-{dev,holdout,all}.json`) are not committed: `score_all.sh`
writes them in about ten seconds and they come out byte-identical every time (fixed bootstrap seed).
The tables are in review/60 section 8.

How the answers were made:

- `perplexity`, `gemini`: `python -m scripts.open_weight_eval run-api perplexity|gemini --split S --out F`
  under `citypods-env` (production's `:free` OpenRouter route and the Gemini decision endpoint).
- `karpo-*`, `glide-9b`: `workers/decision-batch/open_weight_eval_worker.py` on the `r60-eval-l4` and
  `r60-eval-rtx` Cloud Run jobs (image from `open_weight_eval.Dockerfile`; only `manifest.json` is
  baked in, never a label).
- `liquid-*`, `minicpm*`, `arsenelupinstral`: the authors' own runners from review/evidence on an M1
  Max, fed by `export-local` and converted by `import-local`.

`gold.json` is read only by the scoring step. `chapters+single` entries replace a model's grouped
chapter answers with the one-question-per-request run.

Notes on reading the files:

- Each answer of a grouped chapter request repeats that request's `tokens` and `seconds`; the summary
  counts a request once (`tokens_per_tag`) and the cost tables use the worker's per-lane telemetry,
  never a sum over answers. GLiDE asks one forward pass per question, so its request token count is
  the sum over the questions (for 12 tags, about 12 times the chapter).
- The `token_budget` in the telemetry is the padded micro-batch size, not a cap on a request.
- Local runs (`import-local`) spread a run's elapsed time evenly over its answers and carry no token
  counts; the full time is in `run_seconds`. API runs time each request.
- ArseneLupinstral ran on dev tags and moments only (it failed the promotion gate), so it has no chapter
  answers and no holdout file and appears only in the dev summary.
- Production's panel is not used for scoring: the reference for agreement is Perplexity itself.

## Adding a model later (a few dollars, not the $20 of this run)

1. Run only the new model: `run-api` for an API route, or `open_weight_eval_worker.py MODEL --split dev`
   then `--split holdout` for a GPU model (add the model to `CHECKPOINTS` in the worker).
2. Save its answer files here as `<date>-<split>-<label>-<split>-main.json` (and `-chapters-single`),
   or `<date>-<label>-<split>-answers.json` for an API or local run, and add one `add` line to
   `score_all.sh`.
3. Run `score_all.sh dev`, then `holdout` (once, after the model is fixed), then `all`. Every earlier
   model is scored again from the files above, paired with the same bootstrap draws, so no old model is
   run again. Use `open_weight_eval_cost.py` for the cloud cost of the new executions.

What the files above cannot replace, and so is kept: the Perplexity and Gemini answers (a route can
change or disappear), `cloud-costs.json` (execution history is not kept forever), and every GPU
model's answers (about $8 of runs).

## Cloud jobs and images (not committed as files; recreate with these settings)

Two Cloud Run GPU jobs, one per GPU class, each with no retries, a runtime service account that can
read the model bucket and write only under `attempts/r60/eval/` of the batch bucket, the environment
variables `CITYPODS_BATCH_BUCKET=<batch bucket>` and `TRITON_CACHE_DIR=/tmp/triton-cache`, a read-only
GCS FUSE mount of the model bucket at `/models` (`implicit-dirs,enable-buffered-read`), and a private
VPC subnet in the job's region. The model bucket must be in the same region as the job. No project,
bucket, account or registry name is stored in this repository; set them in your environment.

| Job | GPU | vCPU / memory | Timeout |
|---|---|---|---|
| `r60-eval-l4` | L4 | 8 / 32 GiB | 1800 s |
| `r60-eval-rtx` | RTX PRO 6000 | 20 / 80 GiB | 2400 s |

Images: `workers/decision-batch/open_weight_eval.Dockerfile` (Karpo and GLiDE, on the Phase 1
streaming image) and `open_weight_eval_rt.Dockerfile` (adds elephant-1, on the
Phase 1 runtime image). Each takes the base image as a required build argument
(`--build-arg BASE_IMAGE=<registry path of the Phase 1 image, pinned by digest>`); build from a
directory holding `citypods/`, `scripts/`, `manifest.json` and the worker. Execute a job with
`gcloud run jobs execute JOB --wait --args=MODEL,--precision,fp8,--split,dev,--label,NAME` (the label
must differ from the model name). For costs, set `R60_GCP_PROJECT`, `R60_L4_REGION` and
`R60_RTX_REGION` and run `python -m scripts.open_weight_eval_cost`.

Copernicus-31B was run once (dev and holdout; its answer files are kept for comparison and are still
scored by `score_all.sh`) and then dropped by the maintainer; its runtime is no longer wired into the
worker, and its cloud weights were deleted.
