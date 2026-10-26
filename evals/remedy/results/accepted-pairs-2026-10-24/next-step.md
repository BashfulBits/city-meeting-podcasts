> APPROVED October 24, 2026: maintainer explicitly approved both bounded adjudication runs
> below. See adjudication-approval.json. The new judge mapping is now authorized for local
> measurement; production role admission remains separate. Controller launched primary-only.

# Accepted pairings: next evaluation and adoption slice

Maintainer acceptance is recorded in maintainer-acceptance.json. It supersedes the task-quality
rejection of full GLM for the Flash + full GLM pairing and the earlier preference for the
Gemini + Flash initial pair. Both accepted pairs are eligible task-level designs; neither is a
blanket approval of other pairings, gold edits, provider identities or runtime activation.

## Concrete next step: evaluate newly bound disagreement packets

- Flash + DeepSeek: seven disagreements. Proposed independent judges: JEV + Gemini 3.8 Flash.
  DeepSeek cannot judge its own proposal. Gemini is a new adjudicator-role measurement, not an
  already-qualified adjudicator. This needs maintainer approval of the changed role mapping.
- Flash + full GLM: five disagreements and two terminal contract holds. Retain DeepSeek + JEV
  for the five actual disagreements. The two invalid full-GLM outputs remain manual contract
  holds; do not retry or repair them. Neither full GLM nor Flash judges its own proposal.

The two frozen packets contain actual source answers, not old Sol/Gemini proposals. Source
model identities and hashes are retained separately from blinded A/B proposal inputs. The
existing deterministic retriever expands the official evidence/feed examples. Gold and this
acceptance record never enter model inputs. Original evidence packets are preserved separately.

Proposed bounded live measurement: 14 judgments for the seven Flash/DeepSeek packets and ten
for the five Flash/full-GLM packets, 24 completed judgments total. No rerun of initial answers.
Use JEV's existing bounded choice protocol, the existing independent final-answer instruction
for chat judges, primary BeatAPI only, default reasoning, 16,384-token chat output cap, no
explicit temperature, 720-second response deadline, zero hidden retries, single-flight and
65-second provider spacing. Transient-only retries follow the prior immutable-attempt/backoff
approval; access failures and completed invalid outputs stop/hold. Never use the banned key.
Gemini's independent adjudicator role needs its own measured outcomes before runtime admission.

DO pause/drain/reserve only exact registered routes; use the already-approved primary provider
pause for an unregistered Gemini probe. Preserve foreign pauses, save receipts and restore owned
leases in finally. No R2 bucket. Identity aliases remain explicit holds, never silently accepted.

Report separately: original exact-gold outcomes; accepted initial-pair exceptions; newly measured
adjudication results; manual/contract holds; owner-proof and serving-identity status. An initial
agreement accepted here cannot be converted into general gold permission for unrelated models.
New disagreements must be evaluated as supplied; prior judgments on different A/B proposals do
not count. JEV neither/insufficient/disagreement and conflicting judge answers go to manual review.
Do not describe a reused optimization cohort as independent production qualification.

## Following slice: concrete integration design before activation

Current citypods/remedy_p3_runner.py pins Sol + Flash proposers and DeepSeek + JEV adjudicators;
p3_enabled also invokes validate_admission without the required project evaluation. Thus pair
acceptance alone cannot enable the existing caller. Before implementation, approve a narrow
review/51 contract covering these existing files and their corresponding tests:

- citypods/remedy_p3_runner.py: explicit selected accepted-pair roles and independent judge mapping;
  no inference of roles from model names; same role map used in request, admission and response
  identity checking. Remove the unavailable Sol dependency only through the approved role change.
- citypods/remedy_evaluation.py: trusted, version-bound evaluation loading supplied to the existing
  validator; bind route, upstream model, prompt/policy/schema/catalog and effective parameters.
  Scope JEV's alternative evidence to its bounded remedy choice role; do not invent AA scores or
  remove project-evidence validation. Missing/stale/mismatched evidence remains fail-closed.
- tests/test_remedy_p3_runner.py and tests/test_remedy_evaluation.py: stale/missing/changed hashes,
  wrong role, self-adjudication, unapproved route/profile, identity mismatch and manual holds.

Design must explicitly reconcile provider/route identities, the full-GLM 16k/32k mixed profiles,
existing storage/CAS ownership versus Worker DO coordination and exact physical-route support.
Do not generalize acceptance of pooled provider results into admission of either route. The
reviewed design must identify exact config/schema/storage changes before implementation; none
are authorized by this document. Then perform offline validation and propose one monitored manual
P3 report-only cycle. Production stays shadow/empty admissions until separate activation approval.
P4/P5, deployment, merge and broader routing are unchanged.
