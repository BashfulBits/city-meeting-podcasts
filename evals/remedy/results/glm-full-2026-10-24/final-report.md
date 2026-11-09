# Full GLM cohort measurement completed — October 24, 2026

All 60 cases have terminal outcomes: 58 valid answers and two completed NVIDIA contract
failures. No unresolved provider-call intents remain; both controllers exited. Valid answers,
including wrong answers, were never rerun. Transport retries retain immutable attempts.

Full GLM scored 51 correct and 7 wrong among 58 valid answers. Assignment accuracy was 36/39;
publication-policy controls passed 14/14; insufficient-owner-proof controls passed only 1/5.
The two excluded completed outputs are model contract failures, requiring manual handling:
Citizen Advisory invented feed slug `unconfigured_body`; the candidate-forum response changed
its case ID. Neither was repaired or retried. These are failures of usable output, not availability.

## Same-case initial-pair comparison

All rows use exactly the same 58 valid full-GLM cases and original-prompt partner answers.
Agreement means exact publication action and feed set; owner identity and deterministic evidence
checks remain separate gates. These are retrospective task results, not production admissions.

| Pair | Correct agreements | Shared wrong agreements | Disagreements |
|---|---:|---:|---:|
| Full GLM + DeepSeek | 50 | 4 | 4 |
| Flash + DeepSeek | 50 | 1 | 7 |
| Full GLM + Gemini | 51 | 5 | 2 |
| Flash + Gemini | 52 | 3 | 3 |

Full GLM reduces disagreements by accepting additional shared errors. It does not improve the
criterion the maintainer requested: fewer accepted incorrect answers. Retain Flash over full
GLM for the proposed roles; do not promote full GLM. Of these measured available initial pairs,
Flash + DeepSeek has the fewest shared wrong agreements on this intersection. A changed cascade
would need fresh disagreement packets and role/independence approval; old Gemini/Flash
adjudications cannot validate different A/B proposals. No additional cascade calls were made.
The existing Gemini/Flash cascade's four accepted errors also remain unresolved design evidence.

## Provider observations and profile limits

BeatAPI completed 30/30 valid cases in 47 attempts; NVIDIA completed 28/30 valid cases in 40
attempts, plus the two contract failures. BeatAPI had 17 availability failures; NVIDIA had nine
availability failures, one historical empty 16k truncation and two contract failures. Earlier
non-streaming BeatAPI calls produced eight 524 failures; changing the hostname alone did not
resolve them. Streaming ultimately produced all 30 valid BeatAPI answers, with nine additional
transport failures retained. NVIDIA had two 504s and other stream/timeout failures. One old
84-second stream RuntimeError has no proven cause; it is not relabeled as a deadline failure.

Observed valid throughput was approximately 4.92 cases/hour for each provider. Median response
across all returned attempts was 244 seconds for BeatAPI and 301 seconds for NVIDIA. These are
not hard capacity estimates or a matched latency benchmark: disjoint input halves, restarts,
transport/profile changes, maintenance drain waits and 65-second single-flight spacing affect
both figures. No clear capacity winner is established by this experiment.

All 58 selected valid answers echoed the exact requested provider model ID. The scorer's raw
identity-mismatch-attempt count includes failed attempts without returned identity and must not
be read as 16 wrong-model answers. Exact ID echoes do not independently attest serving weights.
BeatAPI `glm-5.3-free` and NVIDIA `z-ai/glm-5.3` retain separate route provenance.

Selected profiles: 11 answers at 16,384 output tokens, 47 at the approved 32,768 cap. Both kept
720-second hard deadlines, default reasoning, no temperature override, original messages/schema
and streaming after the safe transport switch. The historical truncation was subsequently
resolved at 32k; it is not an outstanding budget hold. Mixed-profile results are explicitly
preserved in score-final.json with provider, result path and intent hash per selected answer.

## Adoption disposition

Measurement is complete; review/51 adoption is not. Keep production shadow/empty admissions,
with no merge, deployment or role activation. The trusted evaluation-loading, JEV alternative
evidence and serving-identity gates in admission-preflight.md and adoption-next-slice.md remain.
Use stricter independent evidence/owner checks rather than accepting a pair's agreement as proof.
All investigation-owned pauses are released after completion; foreign pauses are preserved.

Machine-readable evidence: score-final.json, capacity-final.json, immutable intents/results,
provider-specific raw responses/SSE records and completion restore receipts. Availability
failures never reduce calibration quality, while wrong and invalid model outputs remain visible.
