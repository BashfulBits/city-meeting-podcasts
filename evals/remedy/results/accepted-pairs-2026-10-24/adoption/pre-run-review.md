# Pre-run admission review — October 25, 2026

The implementation and offline checks are ready for PR review. The monitored run is held.
`admission-review-bundle.json` pins four candidate routes to the current role plan, runtime
prompt, schema, policy, catalog and request profile, and pins the supporting evidence hashes.
Candidates are deliberately unqualified: this is a review bundle, not an executable admission.
Production remains shadow/report_only with no admissions.

The GLM identity review proposes retaining a hold on the exact returned-ID mismatch until the
maintainer accepts a narrow identity disposition or a provider documents the alias. Public naming
similarity alone does not establish identical serving weights. No alias whitelist was relaxed.

The contract review accepts the offline replay as evidence of representation only. Its 195 bounded
candidates still need source-policy validation; its 127 semantic holds remain manual. It does not
qualify the different runtime prompt or transfer the original task accuracy. The original gold,
pair-specific exceptions, two full-GLM contract failures and all raw judgments remain separate.

Before inference, review separately qualified runtime evidence or explicitly approve a bounded
contract transfer; then construct the exact hash-bound admissions and a real one-finding plan.
The existing preflight sentinel must never be submitted. A merge alone cannot satisfy those gates.
The bundle records each current admission rejection, including missing comparable AA evidence
for chat roles and verified-truth qualification. Only bounded JEV choice evidence has an AA
alternative; no synthetic scores or holdout eligibility counts are authored.

Merging this PR into main automatically triggers `.github/workflows/llm-dispatch-v2-worker-deploy.yml`
because it changes provider_limits.yml and the generated Worker catalog. Check that deployment
succeeds and the exact Gemini route is present before authorizing the monitored run. Deployment
registers a candidate route; it does not add remedy admissions or publish a remedy.

The isolated report-only runner keeps immutable local intent, handle, result and observation
artifacts. Ordinary daily operation still requires its existing CAS ledger. Persistent operation
without R2 requires a separately designed fenced DO ledger and is outside this PR.

Validation is recorded in `route-replay-2026-10-25/validation.json`: 6,747 offline tests passed,
17 deselected; 131 targeted tests passed; whole-repository Ruff and format checks passed. This
review preparation made no provider calls, read no credentials and performed no deployment.

Retention update: generated validation/replay JSON and raw run receipts are local-only.
See `evals/remedy/EXPERIMENTS.md` for clean-checkout scoring and reconstruction commands.

October26 supersession: the maintainer accepted the exact GLM alias for report-only and
production identity checks; see glm-alias-acceptance.json. Earlier alias holds in this document
are historical. Runtime contract qualification and admissions remain held.
