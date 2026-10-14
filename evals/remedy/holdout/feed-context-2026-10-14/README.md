# Feed membership context diagnostic

Separate14-job comparison for Dallas Swagit270975, August22,2023, across the seven
models excluding Qwen. Original60-recording evidence and scoring truth are unchanged.
VariantA states a recurring-body publication policy explicitly; variantB adds two
representative existing episode titles for six feeds retrieved by title similarity.
It tests policy compliance vs learning scope from examples, not independent discovery.
No expected answer file is read by the live runner. One submission per pair, no retries.

Public RSS was retrieved for all70 Dallas feeds through the source-URL security gate.
All returned200. The inventories retain5,372 current RSS title memberships (not the
entire historical archive); target UID/date/title entries are excluded. Three distinct
title patterns per feed yield173 samples. Selection uses title-token Jaccard similarity
without consulting gold: choose six closest feeds, then two distinct patterns per feed.
Examples are real published memberships and can inherit earlier categorization errors.
Future production retrieval must retain that limitation and preserve evidence precedence.

Schema-shaped estimates: baseline6,030tokens; explicit policy6,175 (+2.4%); retrieved
titles6,414 (+6.4%). Three samples for every feed would cost9,915; all current RSS
titles81,020. The latter two are measured only and never sent. Actual provider token
counts are retained per response; chars/4 estimates are not exact provider tokenization.
Both sent variants remain below GPT-OSS's configured7,125-token input ceiling.

Use shared call locking and CAS accounting with the existing approved coordination
bypass. Normal Groq daily gates/reserve apply here. This request authorizes at most
two GPT-OSS diagnostic calls; its original-study429 pause remains intact. Patient
900s/950s settings and Flash-Lite120s/150s remain. Never launch a duplicate controller.

The diagnostic scripts are under evals/remedy/results/feed-context-2026-10-14.
Original-study pair_audit.py retains the common-question21-pair comparison for final
re-scoring; output filenames must be new and immutable. The interim audit is retained
separately. Neither diagnostic nor reused historical comparison qualifies production
models or changes feeds, records, audio, catalogs or Worker settings.
