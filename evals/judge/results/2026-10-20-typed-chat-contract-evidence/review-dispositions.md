# CodeRabbit CLI dispositions

Three completed CLI reviews cover the committed typed-contract change and its successive
quote-policy and Worker-parity diffs. Raw NDJSON is retained beside this file. Each review
completed; saving CodeRabbit's own review metadata reported EPERM.

| Finding | Disposition and evidence |
|---|---|
| Missing macOS `security` executable aborts the run | Fixed: catch FileNotFoundError and report missing credentials. Offline regression test covers it. |
| Integration fake replies exercise only legacy parsing | Fixed: emit kind in answer_job, exercising v3 through planning, collection, quotes and adjudication. Legacy replies retain dedicated compatibility coverage. |
| Shared-model roles overwritten; aliases duplicate route calls | Fixed: retain role sets, deduplicate route/role pairs and use role-qualified output directories. Offline test covers both issues. |
| Changelog item lacks bold title | Fixed to the repository fragment format. |
| Rerunning a route overwrites first-200 evidence | Fixed at the CLI boundary: refuse nonempty output directories in both plan and live mode. Test proves existing failed-validator HTTP-200 evidence remains unchanged and no network call occurs. The separate task scheduler resumes from preserved results. |
| Documented transport retry absent from baseline harness | Clarified the documentation: the baseline harness is intentionally single-pass; the authorized transport retry is implemented by the separate task-scoped scheduler retained as transport-retry.py.txt. Four scheduler tests cover first-200 termination despite validator failure, retry after transport failure, existing-200 preservation and fairness across routes sharing credentials. No claim of automatic retry in the baseline harness remains. |

The final code change after the third review is the small nonempty-directory refusal above.
Whole-repository lint/format checks and a full offline suite are run after that change.

Approved Groq cap follow-up: a fourth CLI review completed with zero findings across the
five changed config/catalog/documentation files. Raw output is `approved-groq-cap-coderabbit.ndjson`.
The actual v2 `outputTokensForRoute` returns 16,384; all 74 compiler tests pass. Historical
65,536-token request/parity artifacts remain unchanged and describe their original attempts.
