# Admission integration implemented locally

Expanded scope approval: scope-approval.json. This change is unmerged and production remains
shadow/report_only with empty admissions. No provider requests, credentials, remote state,
Worker deployment or live publication were used to implement or test this slice.

Implemented:

- Immutable legacy and selected role plans. Flash/DeepSeek are the selected initial pair;
  Gemini/JEV are independent adjudicators. Requests, vote ordering, reports and new observations
  consume the same plan. New observations have a plan hash; legacy v1 payloads and IDs remain.
- Stage-bound roles and generic chat adjudication. A/B selection follows the selected plan even
  when incoming vote order differs. Legacy DeepSeek adjudication fields cannot be used for the
  new plan. Wrong upstream, source/evidence/policy and plan bindings fail closed.
- Bounded reviewed project-artifact loading. The reviewed config pins relative evals/remedy paths
  and SHA256. Sources are independently checked; traversal, symlink escape, malformed/duplicate
  evidence and runtime bindings are rejected. No report can qualify itself merely by setting a
  boolean. Policy hashes exclude trust-anchor hashes to avoid circular artifact dependencies.
- JEV may use reviewed native systemone choice evidence instead of AA only in the narrow
  manual/report-only remedy scope. Other admission callers preserve their old route/AA gates.
- Explicit --report-only-plan and --plan-sha256 mode with local immutable observation artifacts.
  It checks exact admissions before constructing a backend or storage. One frozen source/finding
  is allowed. Ordinary daily mode still requires CAS; no fake CAS or R2 bucket is introduced.
- Run artifacts are keyed by plan hash, so pending handles can be reconciled across invocations.
  Intent files precede submissions, handle/result receipts are immutable, and an unacknowledged
  intent cannot be resubmitted. Queue-only Worker inference still owns quota/pacing/claims.

Validation: targeted integration/legacy tests, whole Ruff/format and full offline suite. See
validation.json for final counts. report-only-preflight-result.json confirms policy_hold with no
provider/backend/production-store access. Its input is an explicit non-live preflight sentinel,
not an invented meeting or a qualification example; it must never be sent to inference.

Remaining runtime gates:

1. Exact free Gemini physical route was registered locally on October25; see
   route-replay-2026-10-25/REPORT.md. Worker deployment remains a separate approval.
2. OrcaRouter GLM canonical returned alias needs an explicit serving-identity disposition.
3. The accepted cohort evaluates BodySelectionAnswer, whereas runtime still uses BodyDecision
   via P3ChatResponse. The October25 offline adapter replay records bounded candidate/manual
   projections; it does not qualify the runtime prompt. Do not relabel schema hashes or fabricate a
   passing admission envelope. A reviewed mapping/replay or bounded runtime-profile qualification
   is still needed. New runtime admissions require runtime_contract_reviewed and
   serving_identity_reviewed in their reviewed evidence, in addition to exact hash bindings.
4. Persistent shared daily ledger operation without R2 still needs its own DO fenced append
   contract. The isolated report-only path deliberately does not write that ledger.

The monitored live Worker cycle has therefore NOT been submitted. This completes the safe
integration foundation and report-only preflight, not production adoption. Accepted task scores
and pair-specific exceptions remain unchanged in the original evals artifacts.

Retention update: generated validation/replay JSON and raw run receipts are local-only.
See `evals/remedy/EXPERIMENTS.md` for clean-checkout scoring and reconstruction commands.
