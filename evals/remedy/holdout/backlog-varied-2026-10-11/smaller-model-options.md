# Smaller-model evaluation proposal — not production admission

The earlier perfect returned-answer scores establish no smaller-model result and cover only
four actual Denton meetings. Testing lighter proposers is worthwhile; lowering the production
admission floor before challenge evaluation is not supported by those scores.

## Context and capacity snapshot

These are repository catalog settings inspected on 2026-10-11, not fresh live quota guarantees.
Configured per-request ceilings matter more than advertised model context. Primary/secondary
Gemini credentials must not be treated as independent projects without verification.

| Candidate | Advertised input context | Configured hard input ceiling | Catalog daily calls |
|---|---:|---:|---:|
| Gemini 3.5 Flash-Lite | 1,048,576 | 218,875 | 500 |
| Gemma 4 26B | 262,144 | 14,400 | 14,400 |
| Groq Qwen 3.8 27B | 131,072 | 7,000 | 1,000 |
| Z.ai GLM 4.7 Flash | 200,000 | 199,222 | 500 |
| NVIDIA Nemotron Nano Omni 30B | 256,000 | 234,437 | unspecified |

The current draft evidence packets contain 2,597–18,446 characters (median 5,621), including
candidate feed display names/descriptions and at most 4,000 agenda characters. These are
character counts, not provider token counts. Tokenize/count the actual serialized prompt plus
response allowance before execution. Never truncate relevant contradictory evidence to fit a
small route. Full agendas remain available for targeted excerpt review and harder-case escalation.
A larger family/history packet may exceed Groq's actual allowance despite its advertised context.

Google documents Flash-Lite as a low-latency document-processing model with structured outputs
and thinking support. This supports its candidacy, not project accuracy:
https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite
Google's documented 1M input window:
https://ai.google.dev/gemini-api/docs/generate-content/whats-new-gemini-3.6

## Recommended experiment

Start with Flash-Lite as the lighter proposer candidate and GLM 4.7 Flash as a different-family
comparison candidate. Gemma is a subsequent small-packet candidate if latency/structured-output
probes and capacity sharing are acceptable. No live calls are included in this preparation.

Use exactly the same grounded difficult cases as the stronger models. Report per-category false
assignment, false exclusion, multi-feed omission, justified abstention, completion and latency.
Keep ordinary controls separate. Unknown truth is not scored as a correct abstention. A deliberately
withheld-evidence test needs its own explicit expected answer, not a borrowed full-evidence verdict.

Initially the stronger independent reviewer checks every lighter proposal. If qualification later
supports a narrow routine type, stronger review could focus on new bodies, ambiguous identity,
joints/consecutive proceedings, inconsistent evidence, exclusion, source/district mismatch and
large replay impact. Do not route solely on a model's self-reported confidence. Missing evidence
requires pending disposition or human research; a stronger model cannot manufacture proof.

The current review/51 admission floor is Gemini 3.7 High. Evaluating below-floor alternatives and
then admitting a smaller-model proposer is a plan change that needs explicit approval and a scoped
capability/evaluation contract. This proposal does not change that floor, production defaults,
route catalogs, quotas, reviewer obligations or automatic-merge eligibility.
