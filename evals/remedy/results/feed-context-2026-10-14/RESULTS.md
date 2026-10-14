# Completed feed-context diagnostic

All14 provider submissions are observed:13 valid answers and one900-second timeout.
Frozen truth requires the subcommittee, Bond Program and Transportation/Infrastructure feeds.
No original-study prompts, results, pauses or production settings changed.

| Route | Explicit policy | Retrieved title examples |
|---|---|---|
| Gemini 3.5 Flash-Lite | Correct | Incorrect: omitted both broader feeds |
| Kimi K3 | Correct | Correct |
| Full GLM 5.3 | Correct | Timed out: no answer |
| GLM 5.3 Flash | Correct | Correct |
| Sol route | Correct | Correct |
| DeepSeek V4.1 Flash route | Correct | Correct |
| GPT-OSS 120B | Correct | Correct |

Explicit policy:7/7 correct. Title examples:5 correct,1 incorrect,1 unanswered.
The timeout is not scored as an incorrect answer. No retries were submitted.

Actual prompt tokens by route:

| Route | Policy input tokens | Title input tokens | Policy provider seconds | Title provider seconds |
|---|---:|---:|---:|---:|
| Gemini 3.5 Flash-Lite | 5881 | 6262 | 1.5 | 1.3 |
| Kimi K3 | 6221 | 6517 | 38.5 | 48.0 |
| Full GLM 5.3 | 5977 | Unavailable | 136.7 | 900.1 |
| GLM 5.3 Flash | 6030 | 6271 | 31.8 | 44.9 |
| Sol route | 5941 | 6236 | 10.6 | 10.6 |
| DeepSeek V4.1 Flash route | 6187 | 6479 | 17.5 | 53.0 |
| GPT-OSS 120B | 6033 | 6328 | 4.5 | 8.0 |

Schema-shaped estimates:baseline6030, explicit policy6175 (+2.4%), examples6414 (+6.4%).
Three examples for all70 feeds would be9915 tokens; all currentRSS memberships81020.
Those larger contexts were not submitted. RSS histories are capped, not the full archive.

The explicit prompt supplies the exact recurring-body publication mapping. The examples
prompt must infer that scope from two titles in each of six retrieved feeds. This single
known-error diagnostic compares context strategies, not general model accuracy. Existing
GLM Flash identity and BeatAPI serving-family holds remain; no model qualification follows.
Provider latency excludes local admission/pacing; the score also retains end-to-end time.
For example, DeepSeek completion tokens increased from2531 with policy to9539 with titles,
including hidden reasoning. Input size alone does not capture latency or token cost.

Recommendation:explicit publication rules for known memberships; retrieved examples can
add evidence for ambiguous cases. Re-test across varied cases before replacing policy.
