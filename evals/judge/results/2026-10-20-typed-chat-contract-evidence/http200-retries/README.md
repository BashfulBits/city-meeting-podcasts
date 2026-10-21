# First-HTTP-200 retry checkpoint

As of 2026-10-21T13:57:59.285996+00:00. Transport retries remain active.

Every completed transport attempt is retained in its route JSON. Initial attempts remain in the
parent evidence bundle. The first HTTP 200 is selected mechanically for each packet; failed
validation is terminal. Export assertions reject duplicate terminal replies or any later attempt
after an HTTP 200. No validator or prompt change is made to turn these failures into passes.

Quote support is measured only on parsed rows. Sibling quote failures remain non-gating;
adjudicator evidence requirements remain in force. HTTP errors and missing credentials are
pending outcomes, never validator passes or validator failures. This is a frozen-fixture contract
check, not a catalog-quality estimate, new gold labeling, or calibration evidence.

| Route | First 200 / planned packets | Contract pass / first 200 | Parsed / expected items | Quote-valid / parsed | Choices / expected |
|---|---:|---:|---:|---:|---:|
| beatapi_deepseek_v4_1_flash_free | 14/14 | 13/14 | 144/158 | 144/144 | 10/12 |
| beatapi_secondary_deepseek_v4_1_flash_free | 0/14 | 0/0 | 0/0 | 0/0 | 0/0 |
| gemma_4_31b_primary | 14/14 | 14/14 | 158/158 | 151/158 | 12/12 |
| gemma_4_31b_secondary | 14/14 | 13/14 | 157/158 | 154/157 | 12/12 |
| groq_qwen_3_8_27b_primary | 14/14 | 12/14 | 135/158 | 107/135 | 10/12 |
| nvidia_deepseek_v4_1_flash_free | 0/14 | 0/0 | 0/0 | 0/0 | 0/0 |
| nvidia_gemma_4_31b_it_free | 14/14 | 14/14 | 158/158 | 158/158 | 12/12 |
| nvidia_nemotron_3_5_super_vl_120b_a12b_free | 14/14 | 14/14 | 158/158 | 157/158 | 12/12 |
| openrouter_google_gemma_4_31b_it_free | 14/14 | 12/14 | 158/158 | 157/158 | 12/12 |
| openrouter_nvidia_nemotron_3_5_super_vl_free | 14/14 | 14/14 | 158/158 | 157/158 | 12/12 |
| orcarouter_zai_glm_5_3_flash_free | 14/14 | 11/14 | 137/158 | 137/137 | 12/12 |
| sambanova_gemma_4_31b_it_primary | 0/14 | 0/0 | 0/0 | 0/0 | 0/0 |

## Observed first-200 failures

- BeatAPI DeepSeek packet 4: its initial HTTP-200 response exhausted 16,384 tokens on reasoning
  and returned empty content. It remains the terminal failure and has never been retried.
- OrcaRouter GLM packets 8, 9 and 10: each echoes `answer_with` on all seven answers. The current
  `extra="forbid"` typed schema rejects these additional fields. The verdicts are present; this
  is a contract failure, not evidence of wrong verdicts or quote-quality failure.
- Secondary AI Studio Gemma packet 10: `eval-i18:supported:T1` became
  `eval-i18:supported:supported:T1`. The JSON schema accepts the reply, but exact-ID coverage
  fails and the backend parses six of seven items. No fuzzy ID repair is applied.

- Groq Qwen packet 5 adds forbidden `level_note` fields to three grade answers; the strict
  schema rejects the packet. Packet 10 duplicates `eval-i18:supported:T1` and omits
  `eval-i17:supported:T1`; duplicate-ID validation rejects it. Both first-200 replies are final.

- OpenRouter Gemma packet 7 omits `kind` on all 13 answers. Compatibility parsing recovers
  13 decisions, but the required v3 schema fails. Packet 9 similarly omits kind on seven
  answers; compatibility parses seven. Both first HTTP 200 replies remain terminal.

## Gates and one recommended next action each

| Gate | Evidence | Recommended next action |
|---|---|---|
| HTTP response sampling: pending | See remaining packet counts; auth, missing key and NVIDIA 504 blockers remain | Continue the authorized transport-only retries with backoff |
| Parser contract: failures observed | The nine terminal packet failures above are retained | Review a bounded contract/prompt remedy before qualifying the affected model results; preserve this baseline |
| CI: runner setup failure | Latest test job failed before pytest because apt could not locate libvpx9; local suite 6,778 passed | Inspect the next genuine evidence push checks; keep PR draft |
| Calibration: insufficient evidence | Last targeted 13-source catalog check has zero clean paired subject-questions | Keep thresholds pending until a verified paired cohort exists |

The maintainer approved the Groq cap correction; commit ed3f492d regenerates both catalogs.
All 14 Groq first-200 replies used the actual v2 helper limit of 16,384. Historical attempts
remain preserved. No production-state recovery is included in this checkpoint.
