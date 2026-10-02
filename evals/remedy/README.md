# Unexpected-body remedy evaluation lane

Use `scripts/eval_remedy.py` to freeze input-only evidence, run explicitly bounded direct model
comparisons, report results against separate truth, and rescore immutable results after corrected
gold. Schema v2 rejects unknown fields and disjoint case IDs. The migrated 27 seeds retain their
original claims; they are regression inputs, **not an independent holdout or admission evidence**.
Adapted examples without recording grounding remain regression-only. `null` truth means unknown.

Examples from the repository root:

```bash
python scripts/eval_remedy.py run --manifest evals/remedy/manifest.json \
  --admission config/remedy.yml --role proposer --dry-run --out /tmp/remedy-plan.json
python scripts/eval_remedy.py freeze --evidence /tmp/frozen-inputs.json \
  --policy-root config --provenance 'reviewed source snapshot' --out /tmp/remedy-set
python scripts/eval_remedy.py report --manifest evals/remedy/manifest.json \
  --gold evals/remedy/gold.json --results /tmp/remedy-plan.json --out /tmp/remedy-report.json
python scripts/eval_remedy.py rescore --manifest evals/remedy/manifest.json \
  --gold /tmp/corrected-gold.json --results /tmp/remedy-result.json --out /tmp/rescored.json
```

`freeze` accepts `{"cases": [...]}` containing full schema-v2 cases, retains the supplied recording
identity and source spans, and attaches approved feed policies. It does not fetch or invent source
evidence. `--force` preserves superseded manifests and gold under content hashes. Dates known only
to day precision remain dates; exact observation times are UTC ISO strings. Case/gold revisions
require a correction reason and superseded revision. Freeze does not manufacture truth.

`run` reads only the manifest and admission config: gold never enters a prompt. A live run requires
both `--live` and positive `--max-cases`; quota/deadline exhaustion records remaining work as
unattempted. Dry-run performs no network/storage calls and reports zero model observations. Raw
results and derived reports refuse overwrite; rescore writes a separate report. Multiple candidate
configurations are compared on identical case inputs and scored separately. Results record actual
physical route/upstream/effort/control hash, raw replies, usage, latency, errors and version hashes.
Unknown or mismatched returned model identity is a failure, never a correct decision.

`config/remedy.yml` starts in shadow mode with empty admissions and qualified alias types: a
visible policy hold. Candidate entries can run only evaluations; admitted entries require reviewed,
version-matched results and an admission reference. Both must pass physical free/direct identity,
dated comparable AA baseline, and effective-effort checks. The inspected catalog does not currently
verify high/max controls for the proposed reasoning models; a separately reviewed capability update
is required before live admission. No upstream incapability is inferred from absent catalog evidence.
Live runs use the existing CAS-capable scheduler storage and update shared quota bookkeeping;
missing CAS configuration is a visible policy hold before provider calls. No production feed or
audio artifact writes, route swaps, provider-limit changes or Worker changes occur in this lane.

Holdout cases must be independently adjudicated, source/family/recording-disjoint from the tuned
seeds; call `validate_holdout` before reviewing admission. Only real grounded, verified,
approved-policy holdout truth can contribute to qualification. Report accepted precision,
critical errors, abstentions, unknown truth, failed calls and unattempted work separately. Blind-owner scoring is not implemented yet: it requires clarification of hidden owner truth and
a separate experiment that hides the proposed claim/target. The current owner-selection counters
remain zero (not evaluated); they are not evidence of correctness. Candidate input version hashes
currently remain optional pending specification clarification; supplied mismatches fail closed.
These outstanding contracts keep P1 incomplete. The seed alone qualifies no automatic alias type.
Remedy remains outside continuous leagues; compare frozen inputs when models retire or candidates,
policy, prompt, effort, schema or routing change. Never send `gold.json` to a model.
