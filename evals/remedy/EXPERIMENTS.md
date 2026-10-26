# Reproduce the accepted-pair experiment or evaluate another model

This is the maintained entry point for the October 2026 body-selection and adjudication work.
Use the same frozen 60 cases, original prompt and original gold when comparing another model.
Quality acceptance is separate from serving identity, runtime-contract qualification and admission.
Nothing here enables production or authorizes new provider calls.

## What belongs in Git

Keep the reusable generation, transport, judging and scoring scripts; the frozen cohort and gold;
the two actual blinded, expanded adjudication packets; maintainer acceptance and scope decisions;
and compact observed model outputs that cannot be reproduced without a new inference run.

`results/accepted-pairs-2026-10-24/evidence.json` contains 298 selected initial answers, 24 judge
responses, two completed invalid full-GLM response texts, and compact attempt/profile/identity/
usage/latency metadata. Original source hashes identify the local raw receipts. Normalized JSON
has different bytes and must never be presented as the original transport receipt or attestation.
The final accepted-pair score and written conclusions are retained as the small decision record.

Do not commit generated per-case requests, intermediate score snapshots, runtime replay output,
HTML/PDF downloads, raw SSE frames, polling/pause/reservation receipts, controller PIDs, locks,
logs, or generated admission preflight sentinels. These remain locally available for this run.
Narrow `.gitignore` rules keep them out of future commits. No local archive files were deleted.

## Offline rescore from a clean checkout

Install the normal development/LLM dependencies from CONTRIBUTING.md. From the repository root:

```bash
PYTHONPATH="$PWD" python evals/remedy/accepted_pairs.py score > /tmp/remedy-pairs.json
```

This validates cohort/gold hashes, validates every selected answer against its frozen case,
recomputes each pair on its identical answered-case intersection, shows total 60-case coverage,
and recomputes the two adjudication resolution tables. It reads no credentials or network.
Expected coverage is Sol/Flash/DeepSeek/Gemini 60 each and full GLM 58. Flash/DeepSeek adjudication
has four correct resolutions and three manual disagreements; Flash/full-GLM has five correct
resolutions. No new wrong resolutions are accepted. The final evaluated cascades remain 55/2/3
and 55/3/2 (original-gold correct / pair-specific accepted initial exceptions / manual or contract
holds). Acceptance never changes gold or excuses a new wrong judgment.

## Prepare the identical cohort for another chat model

```bash
PYTHONPATH="$PWD" python evals/remedy/accepted_pairs.py prepare \
  --model exact-upstream-id --output-cap 16384 --output-dir /tmp/new-model-requests
```

The output directory must not exist. This generates 60 original-prompt requests, the exact
BodySelectionAnswer schema and a request hash manifest. It reads no gold and makes no calls.
Settings are default reasoning, no temperature override, no client retries, and a 720-second
response deadline. A 32,768-token option is available for a separately approved profile; report
mixed profiles explicitly. The old full-GLM requests used 16k first and 32k for approved retries.
Do not compare modified prompts or repaired answers as if they were the original-prompt run.

The retained runners show the execution and coordination protocol:

- `results/gemini-replacement-2026-10-24/run.py`: chat transport, immutable intents/results,
  returned-identity/schema checks, optional streaming, owned provider pause/drain/restore,
  single-flight pacing and transient-only retry/backoff.
- `results/glm-full-2026-10-24/run.py` and `nvidia/run.py`: provider/profile/partition wrappers.
  The original run split even/odd indices across BeatAPI/NVIDIA; do not change the cohort.
- `results/accepted-pairs-2026-10-24/prepare.py` and `expand.py`: deterministic disagreement
  selection, blinded A/B mapping and bounded additional evidence retrieval.
- `results/accepted-pairs-2026-10-24/run.py`: approved chat/JEV judge job construction; without
  `--execute` it validates the 24 frozen jobs without inference.
- `results/gemini-replacement-2026-10-24/adjudicate.py` and the Worker qualification scripts:
  chat payload shaping and JEV's native systemone wire. Do not send chat controls to systemone.

For a new live run, create a fresh output directory and a small wrapper around the retained chat
runner, binding OUT, MODEL, BASE_ROUTE_ID and CANDIDATE_ROUTE_ID to the exact approved physical
route, PROVIDER_OVERRIDE to its provider, and API_BASE_OVERRIDE only when reviewed. Generate that
run's request manifest and explicit approval files in the fresh directory. Those dated scripts
retain historical defaults; never invoke their `--execute` mode against an old output directory
or treat old approval files as permission for a new model, account, endpoint or profile.

Before execution, review the exact route/provider, context and output limits, returned identity,
profile, call count and coordination. Primary accounts only: the secondary BeatAPI account is
banned and must never be used or retried. Never create accounts. Credentials come only through
the suppressed citypods-env wrapper. Use owned provider pauses, preserve foreign pauses, exact
reservations for registered routes, no false route charging, and 65-second single-flight spacing.
Never duplicate a live controller or unresolved intent. Stop 401/402/403/404; retry only authorized
transient transport/5xx/429 failures after five minutes with immutable attempts. Never retry a
valid wrong answer, completed invalid answer, or 32k truncation to improve semantic quality.

Collect valid new-model responses into a JSON list of `{case_id, answer}` rows. Preserve failed
and invalid attempts separately; they contribute to coverage, never answered-case quality. Add a
new label for each provider/profile rather than replacing a historical model:

```bash
PYTHONPATH="$PWD" python evals/remedy/accepted_pairs.py score \
  --additional-model new-model-profile=/tmp/new-model-answers.json > /tmp/expanded-pairs.json
```

## Rebuild disposable inputs for the historical scripts

The compact scorer above needs no raw archive. If inspecting the old packet preparation or
runtime adapter replay, materialize normalized scorer inputs into a NEW tree:

```bash
PYTHONPATH="$PWD" python evals/remedy/accepted_pairs.py materialize \
  --output-dir /tmp/remedy-reconstructed
```

This creates an `evals/remedy/results/` subtree with selected normalized answers, an index with
new hashes, and normalized judge outputs. Copy that subtree into a separate clean checkout for
experimentation, never over the retained local archive. From that checkout run:

```bash
PYTHONPATH="$PWD" python evals/remedy/results/accepted-pairs-2026-10-24/prepare.py
PYTHONPATH="$PWD" python evals/remedy/results/accepted-pairs-2026-10-24/score.py
PYTHONPATH="$PWD" python \
  evals/remedy/results/accepted-pairs-2026-10-24/adoption/route-replay-2026-10-25/replay.py
```

Generated provenance hashes identify the reconstructed JSON, not the original raw receipts.
The expanded frozen packets are retained so the 24 judge inputs remain auditable despite later
website changes. Re-fetching pages is a new evidence snapshot and requires a newly reviewed
packet hash. The replay should still show 322 projections: 195 bounded candidates needing source
policy checks and 127 manual holds. These are representation counts, not runtime qualification.

The `compact` subcommand is an archival extraction tool for the existing complete local run tree.
It requires the original local index/results, refuses to overwrite its destination, and is not
needed to rescore a clean checkout. Historical reports may reference untracked raw files and
intermediate snapshots; those references describe the original run, not clean-checkout inputs.

## Remaining gates

GLM's exact returned-model alias was accepted by the maintainer on October26 for report-only
and production P3 identity checks; see `adoption/glm-alias-acceptance.json` under the accepted-pairs
results. This is not provider/weight attestation or production activation. The runtime contract needs
qualification or explicit bounded transfer acceptance, followed by ordinary admission eligibility
checks. The candidate bundle remains held. Merging candidate route registration deploys the
Worker through the existing workflow; it does not grant admissions or start monitored inference.

## Validation of this retention change

A clean tracked-file export reproduced all ten original pair tables, all 60 original request
payloads and all 24 judge payloads exactly. Reconstructed scoring retained the 4-correct/3-manual
and 5-correct judge results; reconstructed runtime replay retained all 322 projections. A distinct
additional-model label imported successfully without replacing historical results. All 2,042
untracked local archive files were verified still present with identical hashes. No inference,
credential access, deployment or admission change occurred.

Whole-repository Ruff/format checks passed. The unchanged runtime implementation's preceding
full validation passed 6,902 Python tests (17 deselected) and 572 Worker tests. To repeat normal
repository validation, use `ruff check .`, `ruff format --check .` and `pytest -q`; run `npm test`
in `workers/llm-dispatch-v2`. The offline commands above specifically verify evidence retention
and reconstruction without relying on ignored local files.

## Runtime-contract challenge preparation

The next unapproved twelve-case source/finding challenge is described in
[qualification/runtime-v1/README.md](qualification/runtime-v1/README.md). Its curated draft
expectations and reusable builder are tracked; generated review/input/preflight files stay local.
This separately tests runtime-generated prompts/schema rather than assuming the earlier answer
projection transferred accuracy. It is not an independent production holdout or live approval.
