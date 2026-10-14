# Pair agreement on the shared answered set

All seven models completed these same 40 recordings. Qwen is excluded.
Agreement means the same publication action and exact unordered feed set.
Free-text names, rationale wording and citation wording are not equality tests.
True positive = agreement with frozen truth; false positive = shared wrong answer;
negative = different recommendations. All rates use the common set as denominator.

| Pair | True positive | False positive | Negative |
|---|---:|---:|---:|
| Flash-Lite + Kimi | 33 (82.5%) | 1 (2.5%) | 6 (15.0%) |
| Flash-Lite + Full GLM | 33 (82.5%) | 1 (2.5%) | 6 (15.0%) |
| Flash-Lite + GLM Flash | 33 (82.5%) | 1 (2.5%) | 6 (15.0%) |
| Flash-Lite + Sol | 33 (82.5%) | 0 (0.0%) | 7 (17.5%) |
| Flash-Lite + DeepSeek | 33 (82.5%) | 1 (2.5%) | 6 (15.0%) |
| Flash-Lite + GPT-OSS | 32 (80.0%) | 1 (2.5%) | 7 (17.5%) |
| Kimi + Full GLM | 39 (97.5%) | 1 (2.5%) | 0 (0.0%) |
| Kimi + GLM Flash | 39 (97.5%) | 1 (2.5%) | 0 (0.0%) |
| Kimi + Sol | 39 (97.5%) | 0 (0.0%) | 1 (2.5%) |
| Kimi + DeepSeek | 39 (97.5%) | 1 (2.5%) | 0 (0.0%) |
| Kimi + GPT-OSS | 38 (95.0%) | 1 (2.5%) | 1 (2.5%) |
| Full GLM + GLM Flash | 39 (97.5%) | 1 (2.5%) | 0 (0.0%) |
| Full GLM + Sol | 39 (97.5%) | 0 (0.0%) | 1 (2.5%) |
| Full GLM + DeepSeek | 39 (97.5%) | 1 (2.5%) | 0 (0.0%) |
| Full GLM + GPT-OSS | 38 (95.0%) | 1 (2.5%) | 1 (2.5%) |
| GLM Flash + Sol | 39 (97.5%) | 0 (0.0%) | 1 (2.5%) |
| GLM Flash + DeepSeek | 39 (97.5%) | 1 (2.5%) | 0 (0.0%) |
| GLM Flash + GPT-OSS | 38 (95.0%) | 1 (2.5%) | 1 (2.5%) |
| Sol + DeepSeek | 39 (97.5%) | 0 (0.0%) | 1 (2.5%) |
| Sol + GPT-OSS | 38 (95.0%) | 0 (0.0%) | 2 (5.0%) |
| DeepSeek + GPT-OSS | 38 (95.0%) | 1 (2.5%) | 1 (2.5%) |

Common-set categories: {"existing_body_assignment": 32, "publication_policy_control": 8}

Partial completion can exclude hard cases and inflate agreement. No uncertain-owner
guardrail case is in the current common set. No production qualification follows.
The one shared wrong case identifies the correct body but omits broader feed policy.
See report.json for exact pairs, titles, source hashes and unchanged frozen truth.
