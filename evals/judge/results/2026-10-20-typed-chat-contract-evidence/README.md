# Direct judge-contract checks, October 20, 2026

Candidate evaluation; no production rollout or calibration approval. The saved request bodies
are authoritative for exactly what was sent. Request-time hashes and final offline replay hashes
are recorded separately; the sibling quote-policy clarification happened during the live run.
The live processes retained the earlier quote-inclusive `pass`; final-parser evaluations below
apply the maintainer policy: sibling decisions count independently of quote support, while
adjudicators still require supported quotes. The sibling prompt’s last sentence was subsequently
clarified to describe this policy; this is not a claim that every saved call used that later wording.

## Scope and method

Twelve free routes serve the configured sibling/adjudicator model sets. All available routes
were attempted with local direct calls; the secondary BeatAPI route has no local credential.
Each full plan contains 158 items from the unchanged frozen manifest: 30 synthetic claims,
44 T1 tag items, and six four-quote meetings with validate/gate/grade plus both choose orders.
Packets preserve the production answer contract, compiled schema shaping, route output sizing,
and lane reasoning. The production backend parser and quote checker process actual replies.
One diagnostic Qwen run caps output at 16,384 tokens and is labeled separately.

The frozen moment evidence consists of the saved quote/reason, not the full transcript.
No new human labels were added. These are contract and consistency checks, not semantic
accuracy estimates, independent samples, or a replacement for clean paired calibration data.
Provider errors and unattempted packets do not count as parser passes or parser failures.

## Initial single-pass measurements

Transport retries authorized afterward are still pending and are recorded separately.
This table retains the original attempts; it is not an all-routes completion claim.

All denominators below are explicit: parsed items / expected items in HTTP-200 replies;
quote-valid / parsed items; choice capture / expected choices in HTTP-200 replies.

| Route | Role | HTTP-200 packets | Parsed items | Quotes | Choices | Other outcome |
|---|---|---:|---:|---:|---:|---|
| beatapi_deepseek_v4_1_flash_free | adjudicator | 14 | 144/158 | 144/144 | 10/12 | none recorded |
| beatapi_secondary_deepseek_v4_1_flash_free | adjudicator | 0 | 0/0 | 0/0 | 0/0 | missing_key: 14 |
| gemma_4_31b_primary | sibling | 1 | 14/14 | 14/14 | 2/2 | http_503_packets: 1 |
| gemma_4_31b_secondary | sibling | 1 | 14/14 | 14/14 | 2/2 | http_503_packets: 1 |
| groq_qwen_3_8_27b_primary | sibling | 0 | 0/0 | 0/0 | 0/0 | http_400_packets: 1 |
| nvidia_deepseek_v4_1_flash_free | adjudicator | 0 | 0/0 | 0/0 | 0/0 | ReadTimeout: 1 |
| nvidia_gemma_4_31b_it_free | sibling | 6 | 86/86 | 86/86 | 12/12 | ReadTimeout: 1 |
| nvidia_nemotron_3_5_super_vl_120b_a12b_free | sibling | 10 | 131/131 | 131/131 | 12/12 | ReadTimeout: 1 |
| openrouter_google_gemma_4_31b_it_free | sibling | 0 | 0/0 | 0/0 | 0/0 | http_429_packets: 1 |
| openrouter_nvidia_nemotron_3_5_super_vl_free | sibling | 12 | 145/145 | 144/145 | 12/12 | http_502_packets: 2 |
| orcarouter_zai_glm_5_3_flash_free | adjudicator | 0 | 0/0 | 0/0 | 0/0 | http_429_packets: 1 |
| sambanova_gemma_4_31b_it_primary | sibling | 0 | 0/0 | 0/0 | 0/0 | http_401_packets: 1 |
| groq_qwen_3_8_27b_primary (16,384 cap diagnostic) | sibling | 4 | 56/56 | 56/56 | 8/8 | http_429_packets: 1 |

Each route JSON retains requests (without credentials), replies, original result records and
final-parser evaluations, including per-kind counts and parsed values. The SambaNova 401 error
message is redacted because it echoes credential fragments; the status is preserved.

## Findings and gates

- **Required answer capture:** successful typed replies contain actual choice fields; no letter
  is inferred from prose. Exact IDs, kind, required fields, grade bounds and option membership
  are checked. The real Gemma closing-fence reply also has a regression fixture.
- **Sibling quote policy:** quote failures remain measured without vetoing parsed decisions.
  The adjudicator evidence gate remains. Reports and disagreement selection use the same rule.
- **DeepSeek reliability:** one BeatAPI reply exhausted all 16,384 completion tokens on reasoning
  and returned empty content. It failed parsing and remains a failure, with its raw reply retained.
- **Qwen configuration:** the production 65,536-token output request receives HTTP 400; Groq
  reports a 16,384 maximum. The separately capped diagnostic does not validate that production
  configuration. Its four parsed choice-order pairs include one winner flip.
- **Availability:** SambaNova authentication, missing secondary BeatAPI credentials, free-capacity
  limits, NVIDIA timeouts and upstream 502/503 responses prevent an all-routes pass.
- **Calibration:** the earlier non-atomic catalog snapshot has no clean paired sibling/adjudicator
  cohort. Direct fixtures do not repair that provenance gap. No thresholds or task graduation
  are proposed from these results; no production cleanup, dispatch, merge or quota-policy change
  was performed.

**Recommended next action:** review the typed-contract and sibling-policy change with these
explicit route limitations; keep production recovery and calibration blocked until the runtime
configuration/credential failures and clean paired evidence are resolved.

## Code verification

Whole-repository Ruff lint and format checks passed. Full offline pytest: 6,778 passed,
17 deselected after the final evidence-overwrite guard. Three CodeRabbit CLI reviews completed; all six findings have explicit
dispositions in review-dispositions.md. The CLI reported review persistence failure (EPERM);
its raw completed reports are retained with the evidence.

The authorized task-scoped transport scheduler uses the actual v2 Worker output-sizing helper
and the compiled 720-second response budget. It retains the first HTTP 200 for each packet
even if validation fails. Four offline scheduler checks passed. Initial shorter-timeout failures
remain inconclusive until their first-200 retry. The baseline CLI now rejects nonempty output
directories so reruns cannot overwrite existing request/response evidence.

## First-HTTP-200 retry checkpoint

The [retry checkpoint](http200-retries/README.md) includes every completed transport attempt
and the terminal first-200 results, including failures. Seven routes have all 14 packets at
HTTP 200. Three GLM packets echo forbidden request metadata; one secondary AI Studio packet
changes an answer ID. These failures remain failures, and their responses are not retried.
The earlier empty BeatAPI reply also remains terminal. CI is green; PR #2514 stays draft.

The task scheduler now rotates by route, not by per-packet attempt count: a route with many
unattempted packets cannot starve another route sharing its credential. Five offline scheduler
checks passed, including a multi-packet fairness regression. Its graceful restart drains active
HTTP calls before the replacement acquires the single-runner lock.
