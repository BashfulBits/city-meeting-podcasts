# Gemini replacement evaluation — approved assignment change

Maintainer direction October 23, 2026 America/Chicago: replace proposed initial Sol with BeatAPI
Gemini 3.8 Flash Free, test on the frozen packet, gather responses and score proposed assignments.

Target: `gemini-3.8-flash-free`, candidate physical ID `beatapi_gemini_3_8_flash_free`.
Initial partner: existing OrcaRouter GLM Flash. Adjudicators: DeepSeek V4.1 Flash and JEV 1.13.
The approval changes the pinned design; it does not fabricate identity evidence or enable serving.

Evaluate the 60 frozen body-selection cases with the original assignment prompt, including
40 assignment cases, 15 policy controls and five unknown-owner guardrails. Reuse the already
completed GLM original-prompt responses on the same inputs for paired scoring. Preserve Sol
historical scores, but exclude Sol from the current assignment recommendation. Gold is loaded
only by offline scoring. Keep raw responses, hashes, timings and errors; report unavailability
separately from quality. No hidden retries, paid IDs, account substitution or effort substitution.

Use 16,384 output tokens, provider-default reasoning, no temperature override, 720-second hard
response deadline. Candidate structured method starts with prompt-only schema shaping, then
validates actual answers. Preserve exact returned model separately from requested identity.
A successful packet is not proof of model-family identity or an independently held-out result.

Coordination prerequisite: the candidate does not yet exist in the deployed Worker catalogue.
Do not mischarge its requests to Sol or another route, and do not send uncoordinated calls.
The maintainer explicitly approved a temporary provider-scoped BeatAPI pause including JEV.
The approval is preserved in provider-pause-approval.json.
Once approved, drain BeatAPI, preserve foreign pauses, renew owned one-hour leases, space calls
65 seconds after each response, and restore only the owned provider pause in finally. This is
review/48's candidate-probe coordination pattern; no R2 coordination bucket is involved.

Before adoption: resolve route verification and family/GLM identity dispositions, score the new
initial pair and its disagreement subset, and address the documented trusted-evaluation loading
and JEV admission-schema integration blockers. Keep production shadow/empty admissions until
those requirements are actually met. P4 review throughput and P5 auto-merge remain later gates.


The maintainer additionally approved pausing the secondary BeatAPI API key to parallelize the
evaluation. The original controller stopped between calls after two results with no unresolved
intent. The remaining cases are split deterministically: even manifest indices use primary, odd
indices use secondary. There are 60 total cases, not 60 per account. Each account keeps separate
provider pause ownership, single-flight, 65-second spacing, controller lock and response provenance.
Existing results remain immutable; the secondary key does not repeat primary results.

Secondary launch hold: the approved credential source lacks BEATAPI_API_KEY_SECONDARY.
No secondary request was sent. Primary continues its partition; secondary waits for the
maintainer to configure the credential. The acknowledged secondary provider pause remains.


The maintainer explicitly authorized retrying upstream failures until all results are obtained.
Retries preserve original attempts and use retryNNNN intent/result files, five-minute per-case
backoff, per-account single-flight and pacing. Completed answers, including wrong answers, are
never retried. Scoring selects a successful answer without treating failed attempts as quality
negatives. Authentication, paid-access and unavailable-model errors still require disposition.
The secondary export still resolves empty via the approved loader's secret helper, including
an outside-sandbox presence check; no nonempty secondary key was tested or rejected.

Secondary credential access resolved: corrected Keychain service loads nonempty outside the
sandbox; sandbox lookup still returns empty. The authorized secondary controller uses approved
escalated execution of the same credential loader, without displaying credentials.

Secondary access resolved after the maintainer refreshed its stale key and authorized retry.
Authenticated model discovery returned HTTP 200 with the exact free Gemini model enabled.
The secondary odd-index controller is running with approved Keychain access.

Secondary controller stopped on a non-retryable HTTP 403 provider ban: multiple account
registration prohibited. Its owned provider pause was restored on exit. Do not retry the
secondary account or move its assigned cases to another account to evade that restriction.
This is an operational access hold, not a quality failure. Primary retains its own partition.


After the secondary restriction was reported, the maintainer explicitly instructed: "Ok,
continue until finished with the primary key." This supersedes the partition restriction for
this evaluation. Primary-only --all-cases processes remaining frozen cases, preserving all
completed answers and prior failed receipts. The primary timeout retry has now succeeded.
Secondary access is not retried; any primary access denial stops execution. Transient upstream
failures retain authorized backoff and immutable attempts. No new accounts or higher pace.
