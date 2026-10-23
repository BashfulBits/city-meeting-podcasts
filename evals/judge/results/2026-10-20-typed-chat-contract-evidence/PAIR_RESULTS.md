# Judge-pair comparison against frozen labels

Auto-accept requires both distinct logical models to answer supported=true in a valid complete v3 packet.
Rejections, disagreements, missing/invalid replies go to human review. Quote quality is non-gating.

53 labeled items: 30 synthetic, 10 planted wrong-tag controls, 13 real adjudicated items.
Two uncertain labels are excluded. The real labels are Claude-adjudicated with maintainer corrections,
not uniformly human-scored. Moment gate/grade/choose have no gold labels in this set.

This is selection-set evidence, not held-out validation or a catalog population estimate.
Wilson intervals below are descriptive; selection, item dependence and multiple comparisons limit inference.

| Route pair (different logical models) | False accepts / 35 negatives | True accepts / 18 positives | Auto-accepted / 53 | Human review |
|---|---:|---:|---:|---:|
| beatapi_deepseek_v4_1_flash_free + openrouter_google_gemma_4_31b_it_free | 0/35 | 16/18 | 16/53 | 37 |
| nvidia_deepseek_v4_1_flash_free + openrouter_google_gemma_4_31b_it_free | 0/35 | 16/18 | 16/53 | 37 |
| nvidia_nemotron_3_5_super_vl_120b_a12b_free + openrouter_google_gemma_4_31b_it_free | 0/35 | 16/18 | 16/53 | 37 |
| openrouter_google_gemma_4_31b_it_free + openrouter_nvidia_nemotron_3_5_super_vl_free | 0/35 | 16/18 | 16/53 | 37 |
| beatapi_secondary_deepseek_v4_1_flash_free + openrouter_google_gemma_4_31b_it_free | 0/35 | 14/18 | 14/53 | 39 |
| openrouter_google_gemma_4_31b_it_free + orcarouter_zai_glm_5_3_flash_free | 0/35 | 11/18 | 11/53 | 42 |
| groq_qwen_3_8_27b_primary + openrouter_google_gemma_4_31b_it_free | 0/35 | 10/18 | 10/53 | 43 |
| beatapi_deepseek_v4_1_flash_free + openrouter_nvidia_nemotron_3_5_super_vl_free | 1/35 | 18/18 | 19/53 | 34 |
| nvidia_deepseek_v4_1_flash_free + openrouter_nvidia_nemotron_3_5_super_vl_free | 1/35 | 18/18 | 19/53 | 34 |
| beatapi_deepseek_v4_1_flash_free + gemma_4_31b_primary | 1/35 | 17/18 | 18/53 | 35 |
| beatapi_deepseek_v4_1_flash_free + nvidia_gemma_4_31b_it_free | 1/35 | 17/18 | 18/53 | 35 |
| beatapi_deepseek_v4_1_flash_free + nvidia_nemotron_3_5_super_vl_120b_a12b_free | 1/35 | 17/18 | 18/53 | 35 |
| beatapi_deepseek_v4_1_flash_free + sambanova_gemma_4_31b_it_primary | 1/35 | 17/18 | 18/53 | 35 |
| gemma_4_31b_primary + nvidia_nemotron_3_5_super_vl_120b_a12b_free | 1/35 | 17/18 | 18/53 | 35 |
| gemma_4_31b_primary + openrouter_nvidia_nemotron_3_5_super_vl_free | 1/35 | 17/18 | 18/53 | 35 |
| nvidia_deepseek_v4_1_flash_free + nvidia_gemma_4_31b_it_free | 1/35 | 17/18 | 18/53 | 35 |
| nvidia_deepseek_v4_1_flash_free + nvidia_nemotron_3_5_super_vl_120b_a12b_free | 1/35 | 17/18 | 18/53 | 35 |
| nvidia_deepseek_v4_1_flash_free + sambanova_gemma_4_31b_it_primary | 1/35 | 17/18 | 18/53 | 35 |
| nvidia_gemma_4_31b_it_free + nvidia_nemotron_3_5_super_vl_120b_a12b_free | 1/35 | 17/18 | 18/53 | 35 |
| nvidia_gemma_4_31b_it_free + openrouter_nvidia_nemotron_3_5_super_vl_free | 1/35 | 17/18 | 18/53 | 35 |
| nvidia_nemotron_3_5_super_vl_120b_a12b_free + sambanova_gemma_4_31b_it_primary | 1/35 | 17/18 | 18/53 | 35 |
| openrouter_nvidia_nemotron_3_5_super_vl_free + sambanova_gemma_4_31b_it_primary | 1/35 | 17/18 | 18/53 | 35 |
| beatapi_deepseek_v4_1_flash_free + gemma_4_31b_secondary | 1/35 | 15/18 | 16/53 | 37 |
| beatapi_secondary_deepseek_v4_1_flash_free + nvidia_gemma_4_31b_it_free | 1/35 | 15/18 | 16/53 | 37 |
| beatapi_secondary_deepseek_v4_1_flash_free + nvidia_nemotron_3_5_super_vl_120b_a12b_free | 1/35 | 15/18 | 16/53 | 37 |
| beatapi_secondary_deepseek_v4_1_flash_free + openrouter_nvidia_nemotron_3_5_super_vl_free | 1/35 | 15/18 | 16/53 | 37 |
| beatapi_secondary_deepseek_v4_1_flash_free + sambanova_gemma_4_31b_it_primary | 1/35 | 15/18 | 16/53 | 37 |
| gemma_4_31b_secondary + nvidia_deepseek_v4_1_flash_free | 1/35 | 15/18 | 16/53 | 37 |
| gemma_4_31b_secondary + openrouter_nvidia_nemotron_3_5_super_vl_free | 1/35 | 15/18 | 16/53 | 37 |
| gemma_4_31b_secondary + nvidia_nemotron_3_5_super_vl_120b_a12b_free | 1/35 | 14/18 | 15/53 | 38 |
| beatapi_deepseek_v4_1_flash_free + groq_qwen_3_8_27b_primary | 1/35 | 12/18 | 13/53 | 40 |
| beatapi_deepseek_v4_1_flash_free + orcarouter_zai_glm_5_3_flash_free | 1/35 | 12/18 | 13/53 | 40 |
| beatapi_secondary_deepseek_v4_1_flash_free + gemma_4_31b_secondary | 1/35 | 12/18 | 13/53 | 40 |
| gemma_4_31b_secondary + groq_qwen_3_8_27b_primary | 1/35 | 12/18 | 13/53 | 40 |
| gemma_4_31b_secondary + orcarouter_zai_glm_5_3_flash_free | 1/35 | 12/18 | 13/53 | 40 |
| groq_qwen_3_8_27b_primary + nvidia_deepseek_v4_1_flash_free | 1/35 | 12/18 | 13/53 | 40 |
| groq_qwen_3_8_27b_primary + openrouter_nvidia_nemotron_3_5_super_vl_free | 1/35 | 12/18 | 13/53 | 40 |
| nvidia_deepseek_v4_1_flash_free + orcarouter_zai_glm_5_3_flash_free | 1/35 | 12/18 | 13/53 | 40 |
| openrouter_nvidia_nemotron_3_5_super_vl_free + orcarouter_zai_glm_5_3_flash_free | 1/35 | 12/18 | 13/53 | 40 |
| beatapi_secondary_deepseek_v4_1_flash_free + groq_qwen_3_8_27b_primary | 1/35 | 11/18 | 12/53 | 41 |
| gemma_4_31b_primary + groq_qwen_3_8_27b_primary | 1/35 | 11/18 | 12/53 | 41 |
| gemma_4_31b_primary + orcarouter_zai_glm_5_3_flash_free | 1/35 | 11/18 | 12/53 | 41 |
| groq_qwen_3_8_27b_primary + nvidia_gemma_4_31b_it_free | 1/35 | 11/18 | 12/53 | 41 |
| groq_qwen_3_8_27b_primary + nvidia_nemotron_3_5_super_vl_120b_a12b_free | 1/35 | 11/18 | 12/53 | 41 |
| groq_qwen_3_8_27b_primary + sambanova_gemma_4_31b_it_primary | 1/35 | 11/18 | 12/53 | 41 |
| nvidia_gemma_4_31b_it_free + orcarouter_zai_glm_5_3_flash_free | 1/35 | 11/18 | 12/53 | 41 |
| nvidia_nemotron_3_5_super_vl_120b_a12b_free + orcarouter_zai_glm_5_3_flash_free | 1/35 | 11/18 | 12/53 | 41 |
| orcarouter_zai_glm_5_3_flash_free + sambanova_gemma_4_31b_it_primary | 1/35 | 11/18 | 12/53 | 41 |
| beatapi_secondary_deepseek_v4_1_flash_free + orcarouter_zai_glm_5_3_flash_free | 1/35 | 9/18 | 10/53 | 43 |
| groq_qwen_3_8_27b_primary + orcarouter_zai_glm_5_3_flash_free | 1/35 | 9/18 | 10/53 | 43 |
| gemma_4_31b_primary + nvidia_deepseek_v4_1_flash_free | 2/35 | 17/18 | 19/53 | 34 |
| beatapi_secondary_deepseek_v4_1_flash_free + gemma_4_31b_primary | 2/35 | 15/18 | 17/53 | 36 |

## One observation per logical model

Canonical routes are native NVIDIA Gemma/DeepSeek/Nemotron, Groq Qwen and OrcaRouter GLM.
This avoids counting provider/account repeats or the authorized substitutions as extra models.

| Pair | False accepts / 35 negatives | True accepts / 18 positives | Accepted / 53 |
|---|---:|---:|---:|
| groq_qwen_3_8_27b_primary + nvidia_deepseek_v4_1_flash_free | 1/35 | 12/18 | 13/53 |
| groq_qwen_3_8_27b_primary + nvidia_gemma_4_31b_it_free | 1/35 | 11/18 | 12/53 |
| groq_qwen_3_8_27b_primary + nvidia_nemotron_3_5_super_vl_120b_a12b_free | 1/35 | 11/18 | 12/53 |
| groq_qwen_3_8_27b_primary + orcarouter_zai_glm_5_3_flash_free | 1/35 | 9/18 | 10/53 |
| nvidia_deepseek_v4_1_flash_free + nvidia_gemma_4_31b_it_free | 1/35 | 17/18 | 18/53 |
| nvidia_deepseek_v4_1_flash_free + nvidia_nemotron_3_5_super_vl_120b_a12b_free | 1/35 | 17/18 | 18/53 |
| nvidia_deepseek_v4_1_flash_free + orcarouter_zai_glm_5_3_flash_free | 1/35 | 12/18 | 13/53 |
| nvidia_gemma_4_31b_it_free + nvidia_nemotron_3_5_super_vl_120b_a12b_free | 1/35 | 17/18 | 18/53 |
| nvidia_gemma_4_31b_it_free + orcarouter_zai_glm_5_3_flash_free | 1/35 | 11/18 | 12/53 |
| nvidia_nemotron_3_5_super_vl_120b_a12b_free + orcarouter_zai_glm_5_3_flash_free | 1/35 | 11/18 | 12/53 |

## Recommendation and limits

Retain the agreed routine panels: JEV + Gemma for rule/non-Google producers and JEV +
Nemotron for Google producers. GLM or NVIDIA DeepSeek remains the adjudicator, independent
of the producer and sibling families. Qwen stays an eligible sibling alternative.
This chat-only comparison omits JEV and producer eligibility; it does not replace those panels.
The next matched accuracy analysis must compare JEV with each eligible sibling.
Non-accepted outcomes go to human review per the maintainer clarification; this PR does not
change production admission thresholds or implement a suppression policy.

The following alternative-pair counts are diagnostic, not a panel recommendation:
Native NVIDIA Gemma + native NVIDIA DeepSeek accepts 18/53: 17 true accepts and one false accept
(eval-i2), leaving 35/53 for review. Its false-accept rate is 1/35; Wilson 95% interval 0.5%-14.5%.
On the 13 real adjudicated items alone it accepts 8: seven true and one false; the five real
negatives are too few for a tight bound. The native pair does not establish zero false accepts.

The top zero-false-accept route result, OpenRouter Gemma + native NVIDIA DeepSeek, accepts
16/53 (16 true, zero false). It sends 37/53 to review; 14 labeled items were already unavailable
because OpenRouter Gemma omitted kind in two packets. That includes eval-i2. Compatibility-only
sensitivity results in JSON expose the underlying decisions; this is not a change to v3 policy.
Thus do not interpret the zero as evidence that this provider is intrinsically a safer judge.
Zero out of 35 negatives still has a Wilson 95% upper bound of 9.9%; this selection set cannot
justify a production guarantee. Recommend holding eval-i2 for maintainer review and validating
the chosen pair on a disjoint human-labeled holdout before adopting a numerical false-accept cap.

Recompute pair counts, false-accept IDs and intervals from observations.json with
`scripts/score_judge_pairs.py`; the external archive retains the detailed historical report.
No production policy/configuration is changed. No failed HTTP-200 reply is retried.

Label review: i2 is currently false for Parks and recreation; T1 describes the city Parks and
Recreation Special Olympics team and its first competition. All five canonical models accept
it. This is scored as a false accept against unchanged gold; maintainer review may resolve a
rubric/label disagreement. No label is changed and no post-hoc exclusion is used.
