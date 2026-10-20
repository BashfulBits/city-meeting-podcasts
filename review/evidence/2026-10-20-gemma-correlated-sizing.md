# Gemma complete-request sizing and correlated feedback

Owning design: [review/56](../56-gemma-aistudio-acceptance-plan.md), correlated sizing L3 slice.
Maintainer-directed implementation, 2026-10-20 Central. This is not a deployed-policy measurement
or a claim that the 95% Google execution goal has been achieved.

## Requested signal and quota interpretation

The maintainer explicitly selected prior Google refusal followed by NVIDIA **input plus output
above 16,000**, not input alone, as the downward-sizing signal. Preserve both counts separately.
Treat this as an empirical budget-overflow correlation, not proof Google enforces a 16k combined
quota. The captured Google message/QuotaFailure names
`GenerateContentInputTokensPerModelPerMinute-FreeTier`; an absent requested amount leaves the
individual refusal ambiguous. [Google's rate-limit documentation][google] labels TPM as input.

[google]: https://ai.google.dev/gemini-api/docs/rate-limits

## Bounded historical search

Read-only B2 result search: at most 10,000 metadata objects from the results prefix, then bounded
additional UUID-prefix slices (`4`, `8`, `c`, at most 3,000 metadata each). Only objects modified
on/after October 16 were read, up to 1,000 plus 402 response bodies. Deduplicating result keys
found **687 distinct Gemma-4 responses**; 227 had a NVIDIA-compatible response shape and model
name. Response shape alone does not conclusively establish provider identity.

Three responses exceeded 16k combined usage. Bounded poll verified their actual route:

| Job prefix | Route | Full input | Output | Combined |
| --- | --- | ---: | ---: | ---: |
| `3c378878` | SambaNova Gemma 31B | 10,864 | 6,890 | 17,754 |
| `8791ea76` | Google Gemma 26B primary | 8,689 | 8,933 | 17,622 |
| `89ccf046` | Google Gemma 26B primary | 7,843 | 31,366 | 39,209 |

The Google 26B successes contradict a universal 16k combined-token limit, but do not establish
31B's exact behavior. None of these is a NVIDIA >16k pair. Six additional route-verified NVIDIA
31B completions in saved cohort polls had a maximum combined usage of **14,732** (13,110 input,
1,622 output). One observed packet received two Google TPM refusals then succeeded on NVIDIA
with 12,556 input plus 1,315 output = 13,871. These are useful counterexamples to treating every
Google TPM refusal as correlated combined-budget overflow.

**No verified historical NVIDIA >16k combined pair was found in this bounded search.** This is
not an exhaustive inventory. No target is seeded from a guessed provider, total-only field,
job-ID-only match, or missing generation identity. Older Google diagnostics also lack a request
digest, preventing a conclusive unchanged-payload join when supersede is possible. Private raw
responses, manifests, and bounded poll outputs remain outside Git in `/private/tmp/`.

## Implementation and guardrails

Chat judge packing now estimates its complete serialized messages and contract using enqueue's
raw chars/4 units. Boundary tests verify exact agreement and deterministic splits. It preserves
whole items: indivisible Gemma items above the packing target are isolated and submitted for
normal same-model larger-route admission. Non-Gemma blocking behavior is unchanged. Evidence is
never truncated and a stricter Google packing target does not silently discard a large item.

Future correlated learning occurs inside lease-validated settlement, using the current job
request digest and its generation-local Google quota/size exclusions. Supersede clears exclusions;
schema-correction jobs start a new generation. Only single-model Gemma jobs qualify. NVIDIA
success must provide full input and output usage; total/cached-only usage cannot substitute.

A separate bounded 128-packet state per model/prompt family tracks resolved Google successes and
NVIDIA recoveries. When a newly observed combined-budget overflow comprises at least 5% of the
relevant resolved window after 20 relevant resolutions and 20 fresh resolutions since the last
cut, lower the producer bound using the p95 combined/input-estimate ratio with 5% headroom.
A cut is at least 5% where the 4,000 raw-unit safety floor permits, and at most 10% of the
previous bound (the current normal packing preflight ceiling for the first cut). The floor is a guard, not an
empirically fitted starting target. Never grow it
automatically. Old backlog above the bound cannot repeatedly shrink it. Keep this state separate
from pooled tokenizer calibration and physical admission/refusal classification. Unresolved quota
failures are not classified as size failures or silently counted as successes.

The offline correlation CLI requires an identity-verified response manifest with request digest,
route, model, chronology, and separate input/output usage. It rejects missing generation identity
and conflicting response duplicates. Normal Google diagnostic exports plus such manifests can
replay the correlated signal without accessing production or printing response text.

A resolved-window rate is not first-attempt acceptance, eligible-cohort coverage, or validated
completion. Continue the existing version-specific passive reports and producer-validation join.
No synthetic inference, production import, queue rewrite, deadline/quota/concurrency change,
pipeline-version bump, artifact invalidation, or deployment was performed.

## Row cost

The local workerd rows-written harness, 20 one-job bundles of Google Gemma, measured enqueue
124, claim 119, completion 139, consumption retirement 21 billed rows: **403 lifecycle rows,
20.15 per job** (409 including retention pruning). Feedback adds one point read and one row
update per qualifying completion; a cold correlated-overflow cut also reads the bounded Google
route preflight inputs to anchor its percentage reduction to the normal published ceiling; first insertion of its TEXT-primary-key row also writes the
unique-key index. Single-model positive/paired outcomes alone qualify. The conservative existing
23-row per-lease reservation remains above this success-path cost, with bundle/enqueue overhead
reserved separately. No new schema, job index, or queue scan is introduced.

## CLI review dispositions

The first tracked-file review was clean. The full committed-diff review identified a valid
changelog format issue (fixed) and a valid risk of output-heavy cold-start collapse. The latter
was fixed and expanded: 20 relevant samples, 20 fresh samples between cuts, a maximum 10% cut,
a 4,000 raw-unit safety floor, and whole-item Gemma overflow preservation. Actual output is not
clipped to a reservation, preserving the maintainer's explicit combined-usage rule. No automatic
growth/expiry rebound is intentional. Regression tests cover all safeguards and singleton overflow.

The safeguarded CLI pass found two minor issues: non-string allowed-model entries now use the
same filtering as quota recovery, and singleton overflow is capped by the existing structural
route catalog. Tests cover malformed policy entries and an item beyond every route's context.
A further cold-baseline regression verifies a small output-heavy offender cannot cut a much larger
normal ceiling by more than 10%; `_packingRouteLimit` is shared with the actual packing preflight.
