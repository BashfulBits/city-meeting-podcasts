# Gemini 3.5 Flash-Lite Denton comparison

Maintainer approved this evaluation-only smaller-model experiment on 2026-10-11. No production
admission floor, defaults, model controls, route catalog or feed assignments changed.

Same ten approved Denton cases, in claim-support and blind-owner modes: 20 completed answers,
20 correct, zero incorrect, abstained or failed. These are four underlying real meetings, not
20 independent meetings. Scoring used explicit approved manifest/gold after calls; gold never
entered the model request. Content eligibility in the generic report does not qualify this
below-floor baseline for production admission.

Provider median 1.106 seconds, maximum 1.566 seconds. End-to-end median 5.601 seconds,
maximum 20.335 seconds includes quota checks, route coordination and scheduler pacing.
Provider default reasoning, 4,096 output-token cap, 120-second request and 150-second case
bounds. Each job allowed one provider attempt; no automatic retries or route fallback.
Returned identities were gemini-3.5-flash-lite. No maximum-effort claim is made.

Current main at b0dbf348 uses Gemini 3.1 Flash-Lite as the primary topic tagger. Gemini 3.5
Flash-Lite is provisioned and eligible for other work; existing users retain priority.
Initial Worker and local accounting both reported zero daily use on the tested primary route,
with a configured 500/day allowance. Snapshot capacity is not a guarantee of future spare
capacity or four independent Google project quotas. The experiment stopped/deferred if capacity
could not be checked or fewer than 100 remaining calls were visible. Worker reservations and
shared CAS accounting both charged the calls. Only the tested route was briefly paused/drained
around each request; it was resumed on every context exit. No provider-wide pause occurred.

Raw response file and numbered checkpoints are immutable. `content-report-v1.json` and
`summary-v1.json` are separate derivatives. Capacity-before/after and run/context metadata
retain provenance. The next meaningful comparison is the varied backlog set after its
expected decisions and evidence are ready; this perfect narrow score is not sufficient admission.
