# Patient alternative comparison

Maintainer requested full NVIDIA GLM 5.3 and existing free BeatAPI GPT 6 Astra,
GPT 6.1 Sol and DeepSeek V4.1 Flash. The plan in review/51 was committed before
implementation. These are default-reasoning baseline comparisons, not admission.

Each model receives ten approved Denton cases in two modes: checking proposed
assignments and choosing the owner without seeing the proposed answer. Expected
answers are read only by score.py after calls. Each request allows 600 seconds
and 16,384 output tokens, with one provider attempt. Overall elapsed time includes
queue/drain waits and is reported separately from provider response time.

Provider production claims are briefly paused and drained around each call, then
resumed. Existing routes reserve an attempted call in the Worker ledger as well
as local CAS accounting. Full GLM has only an evaluation-local physical route;
its calls use CAS accounting and NVIDIA provider isolation, without a new production
route. Configured conservative GLM bounds are evaluation limits, not provider claims.
BeatAPI batches run sequentially with at least 65 seconds between attempts.

The first GLM setup failed before any provider call because a concurrency field
belonged on QuotaPolicy. The first Astra setup failed before HTTP because the
generated direct-model prefix is unknown to LiteLLM. These failures are not model
quality failures; astra-v1-01.json retains the observation. The evaluation-local
BeatAPI transport uses its documented OpenAI-compatible endpoint, preserving the
exact free model ID and physical route identity. Production catalogs are unchanged.

BeatAPI GPT-labelled serving-family metadata is unresolved in the catalog. Retain
all response metadata and do not assume independent OpenAI family identity.
