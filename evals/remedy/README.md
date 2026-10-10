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
both `--live` and positive `--max-cases`, a total logical-case cap across configurations.
Plans interleave candidates by case, so the cap yields comparable case prefixes without multiplying
quota. Each case permits at most two client provider invocations, shared across schema correction
and capacity/pacing retries; SDK retries are disabled for these bounded requests. The run therefore
makes at most twice `--max-cases` client calls. Counts do not measure retries internal to a remote
provider or gateway. Actual attempt counts distinguish first-attempt from retried completion in
raw results and reports. Normal production retry behavior is unchanged. A timeout holds remaining cases for that configuration; other candidates may still run
within the total cap. Remaining work is recorded as unattempted. Dry-run performs no network/storage calls and reports zero model observations. Raw
results and derived reports refuse overwrite; rescore writes a separate report. Multiple candidate
configurations are compared on identical case inputs and scored separately. Results record actual
physical route/upstream/effort/control hash, raw replies, usage, latency, errors and version hashes.
Unknown or mismatched returned model identity is a failure, never a correct decision.

`config/remedy.yml` starts in shadow mode with empty admissions and qualified alias types: a
visible policy hold. Candidate entries can run only evaluations; admitted entries require reviewed,
version-matched results and an admission reference for production qualification. Evaluations can
rerun either status without reading prior gold or production qualification. Both must pass physical
free/direct identity,
dated comparable AA baseline, and effective-effort checks. The inspected catalog does not currently
verify high/max controls for the proposed reasoning models; a separately reviewed capability update
is required before live admission. No upstream incapability is inferred from absent catalog evidence.
Live runs use the existing CAS-capable scheduler storage and update shared quota bookkeeping;
missing CAS configuration is a visible policy hold before provider calls. No production feed or
audio artifact writes, route swaps, provider-limit changes or Worker changes occur in this lane.

`run --mode claim_support` (default) checks a proposed claim. `run --mode blind_owner` runs a
separate owner-selection experiment: opaque deterministic IDs replace named case IDs, and claims,
decision types, split groups and proposer/tuning metadata are omitted. Approved policy metadata
uses an explicit allowlist. Both modes preserve raw replies and map IDs locally; results and reports
identify the mode and its distinct prompt hash. Claim-support metrics exclude blind-owner replies.

Optional hidden gold `expected_owner` is an independently reviewed existing feed slug. Missing
owner truth is unknown; null proposed owners abstain; failed and unattempted jobs never count as
correct. Exclusion correctness remains a claim-support decision. All 27 migrated seeds leave owner
truth unset. Do not infer it from claims or policy targets. Candidate evaluation requires matching
frozen manifest, mode-specific prompt, response-schema and catalog hashes even for dry-run. Gold
and qualified-result references remain optional for candidates; production admission requires all
five hashes and reviewed qualification.

Holdout cases must be independently adjudicated and source/family/recording-disjoint from the tuned
seeds. Load, freeze and report enforce this split against the canonical regression manifest. Only
real grounded, verified, approved-policy holdout truth can contribute to qualification. Report
accepted precision, critical errors, abstentions, unknown truth, failed calls and unattempted work
separately. The seed alone qualifies no automatic alias type. Remedy remains outside continuous
leagues; compare frozen inputs when models retire or candidates, policy, prompt, effort, schema or
routing change. Never send `gold.json` to a model.


`policies/p2a-foundation/transfer.json` contains synthetic typed-template/proof regression
examples for source-local instantiation, topic negatives and an unseen-city transfer. These
are tested as policy contracts, not appended to the independent model-admission manifest or
gold. Policy instantiation never reads this directory implicitly.

### Resuming bounded local qualification

Keep each raw result file immutable. Use `--checkpoint-dir /tmp/remedy-observations` to
publish a complete observation immediately after each attempted job; interruptions preserve
already-published observations. Pass those JSON files explicitly through `--resume-results`
in a later run with a new `--out` path. Matching completed case/configuration pairs are carried
forward without another provider call. Failed and unattempted pairs can retry; conflicting
completed observations or changed manifest, prompt, schema, catalog or mode are rejected.
Gold is used only by `report`/`rescore`, never by execution or resume.

The default request timeout remains 30 seconds and the case deadline 60 seconds. For the
approved high-reasoning evaluation use `--request-timeout 120 --case-deadline 150`.
The CLI bounds requests to 120 seconds and case deadlines to 150 seconds. These are local
evaluation controls, not production model or workflow limits.

Each live job checks the shared quota ledger for at least two remaining daily requests on
its allowed physical routes, accounting for the route's reset timezone. This preserves room
for the existing maximum of two provider attempts. Insufficient capacity or a failed quota
read defers the job; the shared CAS scheduler still enforces limits under concurrent usage.
