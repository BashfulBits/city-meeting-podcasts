# P1 model comparison, 2026-10-10

This is evidence for choosing models, not permission to activate them. No production feeds,
model selection, quotas, storage routing or evaluation answers changed. The independent Denton
truth set was approved before evaluation and is in ../../holdout/p1-denton-2026-10-10/.

## Finished comparisons

| Model / mode | Correct | Wrong | Abstained | Failed | Other |
|---|---:|---:|---:|---:|---|
| Kimi K3 max, historical claims | 18 | 0 | 8 | 0 | 1 unknown truth |
| Kimi K3 max, Denton claims | 10 | 0 | 0 | 0 | None |
| Kimi K3 max, Denton blind committee selection | 10 | 0 | 0 | 0 | None |
| Gemini four routes, Denton claims | 23 | 0 | 0 | 17 | Four copies of 10 cases |
| Gemini four routes, Denton blind selection | 2 | 0 | 0 | 38 | Four copies of 10 cases |
| Gemini four routes, historical claims | 6 | 0 | 0 | 102 | Four copies of 27 cases |
| GLM 5.3 Flash default, Denton claims (content only) | 10 | 0 | 0 | 0 | Identity/effort held |
| GLM 5.3 Flash default, Denton blind selection (content only) | 10 | 0 | 0 | 0 | Identity/effort held |

These are latest-per-pair outcomes, not sums of every retry. Kimi's malformed historical reply
was retried successfully; the original failed observation remains. Abstention is not a correct
assignment. Small, related Denton cases do not prove general safety across cities. The historical
cases are regression fixtures and are not independently held-out admission evidence. Unknown
truth is never guessed for a score. Failed calls are not wrong classifications or successful tests.

## Gemini diagnosis

The bounded diagnostic preserved Google's 429 RESOURCE_EXHAUSTED response. It specifically
reports a per-project/per-model daily request limit of 20 and a reset delay, rather than a wrong
answer or unsupported reasoning control. The local quota ledger had 3–5 apparent daily requests
remaining per physical route before the diagnostic. Its counter therefore is not a complete view
of what Google counts. These results do not establish whether the two keys belong to distinct
projects, or how much of Google's counted usage came from production, earlier diagnostics or
failed requests.

Google documents that quotas are per project, not per key, and reset at midnight Pacific:
https://ai.google.dev/gemini-api/docs/rate-limits.

The diagnostic log classified the response as gateway_limit / cf-aig. The current classifier
checks gateway headers before provider quota details. Direct retry handling uses a short fallback
cooldown for gateway_limit, whereas own_rpd waits for the route's daily reset. This explains a
repeat-rejection path. A production fix should verify provider daily-quota details through the
proxy before changing classification, preserve genuine gateway limits, and test reset behavior.
The evaluator should also stop the affected batch after confirmed provider daily exhaustion.
No runtime fix or quota bypass is included in this evidence bundle.

## DeepSeek limitations

Default-reasoning transport and numeric reasoning_effort=100 probes returned no usable answer.
The two retained long numeric probes allowed 600-second requests, 650-second case deadlines and
16,384 output tokens, with one provider attempt each. Both timed out after about 603 seconds.
A timeout neither verifies nor disproves the numeric control. NVIDIA's model-card benchmark
setting is not the same as an independently verified endpoint contract.

The first long-probe script accidentally reused prior exclusive output filenames. Both calls
ran, then failed saving their results; those answers/errors were not retained. They are listed
as unscorable diagnostics, not omitted successes. The corrected v2 probes preflighted fresh
output paths and retained their failures. There is no basis for declaring DeepSeek qualified.
The stream diagnostic also timed out after 601 seconds, receiving zero stream chunks.
That is not an observed partial answer truncated by the output-token limit. It does not
isolate NVIDIA from the configured AI Gateway path; a bounded direct-endpoint comparison
would be a separate transport-scope decision. No control or model was qualified.
Its v1 script used a 170-second outer deadline for Gemini/GLM (120-second provider requests);
the retained source snapshot records that mistake. The reusable script corrects it to 150
seconds. DeepSeek used the intended 650-second outer deadline. Actual durations are retained.

## GLM baseline and identity

The exact existing route is orcarouter_zai_glm_5_3_flash_free, upstream
z-ai/glm-5.3-flash-free. It is GLM 5.3 Flash, not the full GLM 5.3 model. OrcaRouter's live
model catalog confirms the requested free ID. Chat responses report glm-5.3-flash instead.
All 20 provider replies had valid answer content: 10/10 evidence checks and 10/10 blind
committee choices, median 32.2 seconds and maximum 62.9 seconds. The strict diagnostic
identity gate holds all 20 responses; raw output is retained. Content-only
scores must be distinguished from a passed identity or admission gate.

NVIDIA also publicly offers full z-ai/glm-5.3, but that route is absent from the fetched
repository catalog at a81317f7. It is distinct from the GLM Flash baseline here:
https://docs.api.nvidia.com/nim/reference/z-ai-glm-5-3-infer. Evaluating it would require
a separate scoped route/capability update and verified limits, with NVIDIA contention considered.

GLM receives its existing default reasoning, not an invented high/max setting. OrcaRouter's
reasoning documentation lists generic controls but does not establish maximum effort for this
exact model: https://docs.orcarouter.ai/advanced/reasoning. No route capability was added.

## Capacity and recommendation

The retained Worker snapshot shows 10 Kimi chapter-location calls in the observed UTC day,
including two calls at least 600 seconds long and a maximum of 720 seconds. The catalog caps
Kimi concurrency at one; NVIDIA capacity is also shared with other production models. Kimi is
used for large chapter-location packets and is eligible for highlights. This snapshot does not
measure all-day occupancy or queue pressure and does not include local CAS evaluation calls.

Kimi is the strongest demonstrated reviewer here, provisionally suitable for sparse difficult
reviews. It should not displace critical chapter work merely because it has no daily request cap.
GLM has configured 800 requests/day and concurrency two, but competes with other OrcaRouter work;
its default baseline and identity/control gaps must be evaluated before recommending activation.
DeepSeek NVIDIA is also proposed for review/49 adjudication, so it is not dedicated spare capacity.
BeatAPI alternatives share one account-wide window with JEV; long calls can block that judge.
There is no combined Worker/local quota measurement or production admission in this PR.

## Reproduction and retained provenance

- artifacts/: original candidate configs, immutable checkpoints, raw results and scored reports.
- catalogs/: the distinct Gemini and Kimi compiled catalogs used by the respective runs.
- historical-scripts/: exact temporary drivers as text snapshots; old absolute paths are provenance.
- diagnose.py: bounded new diagnostics; uses the existing CAS scheduler, exact free allowlists,
  no gold access, sanitized provider errors and evaluation-only controls. Its GLM run is a baseline.
- SHA256SUMS.json: hashes of all retained files except the checksum file itself.

Use the repository commit recorded in each original result. Do not relabel a result with a newer
catalog or claim it is comparable solely because the model name matches. For historical drivers,
replace /private/tmp paths with the corresponding retained artifacts; retain config/catalog hashes.
The credential wrapper must be supplied locally; it is not part of this bundle. R2_BUCKET's
citypods-spike override selects the existing shared CAS quota store and writes quota bookkeeping.
Credentials were scanned before retention. Unfiltered provider logs and credential files are excluded.

Example new diagnostic, from the repository root:

```bash
/usr/local/bin/citypods-env env R2_BUCKET=citypods-spike PYTHONPATH=. \
  python evals/remedy/results/p1-comparison-2026-10-10/diagnose.py glm \
  --out /private/tmp/new-glm-baseline.json
```

Never reuse an existing output path. Running the example spends provider quota. Score only after
calls finish, using the explicit approved gold file. Saved raw answers never become approval records.

Verification: 61 targeted evaluation tests and 5,766 offline tests passed (16 deselected);
whole Ruff/format passed. Diagnostic scripts are evaluation-only; their baseline scorer
explicitly preserves identity/maximum-reasoning holds. No production admission was made.
