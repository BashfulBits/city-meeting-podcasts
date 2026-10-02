# Unexpected-body remedy evaluation lane

The rerunnable lane specified by [review/51](../../review/51-unexpected-body-remedy-flow.md).
Follow the repository layout used by `evals/chapter-agenda` and `evals/judge`:

| Artifact | Purpose |
|---|---|
| `manifest.json` | Versioned input-only cases: labels, claims, policy/source evidence |
| `gold.json` | Separate verdicts, reasons and adjudication provenance; uncertain truth is null |
| future `holdout/manifest.json`, `holdout/gold.json` | Disjoint sources/families/recordings; not inspected while tuning |
| future `results/<date>-<experiment>-<route>.json` | Raw decisions, actual route/effort, version/hash/commit and errors |
| planned `scripts/eval_remedy.py` | `freeze`, `run`, `report`, `rescore` through existing routing |

The 27 seed cases cover wrong TIF districts, aggregate subscription versus body identity,
word-boundary negatives, cross-city targets, duplicate provider views, dated families, sparse
bodies, approved bond/charter family policy, civic-media exclusions and uncertainty. Exact provider observations, adapted policy examples and
synthetic cases are identified. Exact observations carry recording IDs and official metadata;
adapted examples require additional source grounding before admission. Recommendations not yet
approved have a distinct policy status.

This seed was written after inspecting #1747. Its truth is agent-reviewed and approved-policy-
derived where stated; **it is not an independent holdout, a completed model comparison or evidence
that any route is safe to admit**. The community-aggregate policy was approved in chat on
2026-10-02; its adapted
example still needs recording evidence before admission. `null` means unknown, not false.
Future results must distinguish truth provenance.

The runner is specified by review/51 P1 (L3) and is **not yet present**. Planned commands
are specified in that document; do not describe them as working CLI commands today. Until P1,
review and amend the seed in a normal PR, bump its version when its meaning changes, and retain
source IDs and the reasons for corrected truth. Remedy uses matched frozen comparisons, not continuous route leagues. Narrow alias-type
qualification requires explicit scope approval and positive/negative historical replay; no fixed
299-case or 99% precision guarantee is claimed. Re-run all admitted routes whenever models retire,
new models enter, or policy/prompt/effort/schema/routing changes; do not replace this lane with
one-off experiments. Never send `gold.json` to a model.
