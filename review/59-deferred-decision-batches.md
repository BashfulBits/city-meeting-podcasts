# review/59 — Deferred Decision API batches on Cloud Run

**Maturity: L2 · 2026-11-14 · Maintainer-selected qualification pilot.** This is a follow-up to
[review/49](49-judge-consensus-admission.md) and
[review/58 §12.8–12.9](58-llm-pipeline-throughput-balance.md#128-decisions-api-for-tagging-recommendation).
It adds a way for GitHub Actions to produce typed decisions for offline GPU inference. Promotion into
the judge stack and use as a tag prefilter each require separate evidence and a maintainer decision.

## Recommendation

Start with a finite shadow replay of tags and moments using GLiDE-9B BF16 on one NVIDIA L4 in
`us-central1`. GLiDE-9B is the sole self-hosted model in this pilot. Use an independent GitHub
Actions producer and collector, private GCS objects, and asynchronous Cloud Run Jobs. Keep local
batch accumulation independent of Google's provisioning mode. Prefer regular Cloud Run Jobs when
the expected cost is tied, subject to pilot approval. Qualify the model and measure the whole bill
before scheduling recurring GPU runs.
Reuse the typed judge question and evidence builders. Give future producers the same file contract,
with a model adapter responsible for the native request and response conversion.

The current judge producer is [judge.yml](../.github/workflows/judge.yml). It calls
`citypods.cli enrich --lane judge`, creates `Item`s, packs them for the existing routes, and
reconciles through the dispatch Worker and episode judge ledger. The Gemini and OpenRouter anchors
already reuse the typed `JevBackend` contract. As of this checkout, Gemini is first and Perplexity
Decider is second, selected per subject by model-family independence. The proposed batch producer
consumes pre-pack `Item`s or equivalent frozen eval items, so it can emit small requests without
changing the packet sizing for those routes. The first results are evaluation artifacts.

GLiDE-9B descends from Qwen3.5-9B. Citypods already classifies the OpenRouter Decider as `qwen`.
GCP hosting makes GLiDE a separate execution path and capacity source. Its model lineage makes it
ineligible to cast a second independent vote alongside a Qwen producer or judge. Use the existing
family checks and per-model calibration if the route qualifies as a future alternate anchor.
[Fastino model notice](https://huggingface.co/fastino/GLiDE-9B/blob/main/NOTICE) ·
[Current judge model families](../config/site_config.yml)

### Benchmark and model diversity

Fastino reports 68.77 on the public Decision Index 0.3.1 suite for the 9B variant. The benchmark
publisher's 0.4.1 dataset lists 62.48 as its aggregate. Hosted GLiDE and the 27B variant are
separate checkpoints. The scores justify a trial, and the pilot measures quality on civic tags,
quote support, safety, usefulness and competing clips.
[GLiDE-9B model card](https://huggingface.co/fastino/GLiDE-9B) ·
[Decision Index dataset](https://huggingface.co/datasets/multimodalart/decision-index-leaderboard) ·
[SO-Arena research](https://arxiv.org/abs/2611.15777)

For a later family-diversity comparison, Liquid AI's `d1-3B` has typed decisions and an LFM2.5
backbone. Its aggregate Decision Index score of 31.1 makes it a candidate for a task-specific
screen before anchor consideration. Gemma-derived decision checkpoints diversify relative to Qwen
and share Google's slot under the current family policy. Require later adapters to declare model
lineage, question capabilities, output normalization and cost per valid decision. Judge coverage
on eligible subjects matters alongside raw benchmark scores.
[Liquid model card](https://huggingface.co/LiquidAI/d1-3B) ·
[Decision Index leaderboard](https://multimodalart-jev-decision-index.static.hf.space/index.html)

#### Gemma-family follow-up candidates

GLiDE-9B is an alternate Qwen-family capacity source. After qualification it can back up or
replace OpenRouter's free Perplexity Decider route if that route loses capacity. Keep their
quality measurements separate and select one eligible Qwen vote per subject.

Karpo Decision 31B is a dense Gemma 4 benchmark candidate. It is a Gemma 4 31B IT fine-tune
with a typed decision readout. Its 73.86 Decision Index 0.4.1 full score supports a matched
civic holdout. Unlike GLiDE, Karpo's runner can score several questions about identical state
in one forward pass. Test that grouping only where it preserves the evidence and question meaning.
Karpo adds a separate lineage from Qwen. Apply the current `google` family exclusion when Gemini
or a Google producer already occupies that family for a subject.
[Benchmark publisher data](https://huggingface.co/datasets/multimodalart/decision-index-leaderboard) ·
[31B model card](https://huggingface.co/MachinePulseAI/karpo-decision-31B) ·
[Native runner](https://github.com/machinepulse-ai/karpo-decision)

At 31.27B parameters, idealized weights alone take about 62.5 GB in BF16 or 31.3 GB in FP8/INT8.
A 24 GB L4 would require a working low-bit path that preserves the decision readout, numeric
calibration, and runtime headroom. Karpo also publishes a Gemma 4 12B weight set. Its BF16 weights
take about 24 GB before runtime overhead, so measure or quantize that variant as well for an L4
pilot. Iowa's 96 GB RTX PRO 6000 can support a BF16 31B trial. At 120 one-minute executions it
costs about $2.63 in GPU charges, or $6.37 at regular full compute rates before storage and
shared CPU/RAM credits. Neither latency nor cost per decision is measured for Citypods. Evaluate
Karpo separately under the project budget before adding a model or enabling a vote.
[12B model card](https://huggingface.co/MachinePulseAI/karpo-decision-12B) ·
[Cloud Run GPUs](https://docs.cloud.google.com/run/docs/configuring/jobs/gpu) ·
[Cloud Run pricing](https://cloud.google.com/run/pricing)

MachinePulse distributes newly acquired Karpo 31B weights under its Community License as of
November 12, 2026, including downloads of earlier revisions. The 12B release also uses the
Community License. Karpo's 90-day language exempts a pilot with no paying customers from its
production and external commercial use definitions. External commercial use, including a free
service operated for commercial advantage, requires attribution. Commercial production by entities
over $10M in annual revenue requires a separate agreement.
[31B weight license](https://huggingface.co/MachinePulseAI/karpo-decision-31B/blob/main/LICENSE) ·
[12B weight license](https://huggingface.co/MachinePulseAI/karpo-decision-12B/blob/main/LICENSE)

For a dense Gemma comparison with permissive weight terms, shortlist Copernicus-31B first.
It merges two Gemma 4 31B IT derivatives and exposes `choice`, `noul`, and `score`. The
publisher declares Apache-2.0 and lists both upstream licenses in its attribution file. The
reviewed revision lacks the standalone LICENSE file linked by the card. Confirm and archive
the applicable notices with the selected revision before deployment. The published checkpoint
contains 62.5 GB of tensors. A single 24 GB L4 needs a tested low-bit path with room for runtime
buffers. FlexDM-LoRA-v2.1 ships a CC BY-SA 4.0 adapter for the same 31B base. The evaluated
FP8 base still needs more than 24 GB before the adapter and runtime buffers. Its published
reader supports choice and yes/no. Check attribution and share-alike terms when distributing
adaptations of the adapter.
[Copernicus card](https://huggingface.co/Poindexter-Labs/Copernicus-31B) ·
[Upstream attribution](https://huggingface.co/Poindexter-Labs/Copernicus-31B/blob/main/ATTRIBUTION.md) ·
[FlexDM card](https://huggingface.co/1000luca/FlexDM-LoRA-v2.1) ·
[CC BY-SA 4.0 terms](https://creativecommons.org/licenses/by-sa/4.0/)

In the November 12 Decision Index 0.4.1 snapshot, Karpo, Copernicus, and FlexDM-LoRA scored
73.86, 72.11, and 71.61 Full respectively. Copernicus and FlexDM were fifth and eighth by
point estimate. The board treats scores within 0.9 points as tied. Copernicus had 1.80 percentage
points of expected calibration error (ECE), Karpo had 4.77, and FlexDM-LoRA had 5.36. Karpo had
the lowest Brier score of the three (0.240 vs. 0.269 and 0.305). Measure both calibration and
decision quality on the same Citypods holdout with each candidate's chosen runtime.
[Full scores](https://huggingface.co/datasets/multimodalart/decision-index-leaderboard) ·
[Published calibration data](https://huggingface.co/spaces/multimodalart/jev-decision-index/blob/main/data/index.json)

`s1-gemma4-26b-decision` is another Gemma 4 candidate and the first to try if the follow-up
targets one L4. It fine-tunes Gemma 4 26B-A4B IT and reads option probabilities from next-token
logits for choice, yes/no, and ordinal decisions. Its roughly 4B active parameters describe
compute per token. The author measured 48.5 GiB of BF16 weights and 25.8 GiB in FP8, both above
L4 capacity. The published Ollama INT4 artifact is about 17 GB on disk, making it a plausible
fit. Confirm that the Linux NVIDIA backend preserves the option logits, fits weights and runtime
buffers within 24 GB of GPU memory and the job's host memory limit, and completes cold batches
without runner restarts. The author's NVIDIA INT4 run reported restarts and 15 failed requests
out of 1,700. Pin the artifact and use calibration for the exact backend and quantization.
[Model card and measured sizing](https://huggingface.co/j-raghavan/s1-gemma4-26b-decision) ·
[Typed decision runner](https://github.com/j-raghavan/s1-decision-model)

The author reports 0.804 calibrated for INT4 versus 0.817 for BF16 on a 1,700-case JevBench
subset. Use the same Citypods tag and moment holdout to compare S1 with Karpo, GLiDE, and the
existing routes. The subset scores and Karpo's full Decision Index score use different
evaluations. All of these Gemma candidates count as `google` family under the current
exclusion rule.

## Argon’s proposal and the findings that change it

| Proposal | Finding and design consequence |
|---|---|
| Single-question requests and local micro-batching | Supported. Native `decide_batch` accepts `state` and a map of typed questions with `type`, `instructions` and kind-specific `criteria`. Argon’s legacy converter emits different fields and flattens answers, losing the IDs and usage the collector needs. GLiDE runs one forward pass per question and length-sorts prompts into padded-token micro-batches. Shared state saves wire bytes and preserves an evidence group, while each pass still reads that state. [Model card](https://huggingface.co/fastino/GLiDE-9B) |
| Set `GLIDE_TOKEN_BUDGET` and `GLIDE_MAX_LEN` in the container | Pass `token_budget` and `max_len` explicitly to the in-process loader. The pinned loader takes keyword arguments. The published environment knobs are for the HTTP server. BF16 and native decision inference are the comparison baseline. [Runtime source](https://huggingface.co/fastino/GLiDE-9B/blob/ab54df5acba0eebf7df45560ffdaacd64e32df3b/glide/model.py) |
| 18 s boot and 70–220 decisions/s | Fastino reports on an RTX PRO 6000, including 15.90 GB of BF16 weights on disk and 18.92 GB peak over its test mix. A 24 GB L4 is plausible. Packing 5–16 prompts is a rough arithmetic example for 1–3k tokens at a 16,384 padded-token budget. L4 compatibility, kernel path, model loading, token distribution, memory, and batch throughput all need measurement. [Fastino’s measurements](https://huggingface.co/fastino/GLiDE-9B#hardware-and-memory) · [Cloud Run GPUs](https://docs.cloud.google.com/run/docs/configuring/jobs/gpu) |
| CUDA 12.4 Ubuntu 22.04 image with Python 3.12 | Use a digest-pinned Python 3.12 / CUDA 13-compatible image and the repo dependency contract. Fastino validates CUDA 13 wheels with a 580+ driver and lists `flash-linear-attention` as the fast path. Google currently documents driver 580.x for the L4. Verify CUDA and kernel availability and fail the canary if it silently drops to the slower path. Record the model revision, image digest, driver, and pins. [Fastino requirements](https://huggingface.co/fastino/GLiDE-9B/blob/ab54df5acba0eebf7df45560ffdaacd64e32df3b/requirements.txt) · [Dependency policy](22-dependency-and-reproducibility-policy.md) |
| Bake the weights into the image and keep a shared writable Triton cache on GCS FUSE | Start with small runtime image layers and a read-only regional GCS mount for the pinned weights. Google prefers GCS for models over 10 GB. Use GCS object APIs and generation conditions for manifests and results. Start Triton with an execution-local cache. Compare cold compilation against a versioned cache archive only if profiling shows a material time or cost difference. Cloud Storage FUSE lacks concurrent write locking. [GPU job guidance](https://docs.cloud.google.com/run/docs/configuring/jobs/gpu-best-practices) · [FUSE limitations](https://docs.cloud.google.com/run/docs/configuring/instances/cloud-storage-volume-mounts) |
| GitHub Actions `--wait` or a Cloudflare webhook and buffer | Launch asynchronously and reconcile from a later Actions run. GCS can hold the durable input, launch state, and results. This keeps the GPU fleet at zero between jobs and avoids runner waiting, per-question DO writes, callback credentials, and loss of an in-memory queue on eviction. [Cloud Run job execution](https://docs.cloud.google.com/run/docs/execute/jobs) · [Delayed Jobs](https://docs.cloud.google.com/run/docs/delayed-jobs) |
| About $0.63/month at four launches a day | A started task has a one-minute minimum. At four launches a day, 30 days, and one billable minute each, GPU alone costs about $1.34. With no remaining free CPU or RAM, Delayed Jobs compute costs about $1.87 before storage and builds. [Cloud Run pricing](https://cloud.google.com/run/pricing) |

## Budget and latency

On November 15 in Chicago, a CPU-only Virginia transfer probe fetched all 15,895,578,201
model bytes over Direct VPC and verified their SHA-256 hashes. It measured 75.99 seconds to
download and 79.69 seconds to hash. The subsequent L4 canary used the same pinned generations,
a 20 GiB ephemeral disk and eight parallel ranged readers. It downloaded in 43.21 seconds,
hashed in 45.02 seconds, initialized in 133.92 seconds total, and ran inference in 86.70 seconds.
It produced 158 structurally valid results from 158 questions. Peak GPU allocation was
18,933,681,152 bytes and peak host RSS was 16,867,889,152 bytes against a 16 GiB host limit.
Cold-start and memory headroom require further measurement before recurring use. At the Iowa/
Virginia regular L4 list rate, 220.64 seconds of processing implies about $0.0412 in GPU
charges, with startup/shutdown, CPU, RAM, disk, build, storage and transfer charges additional.
Actual billed cost and labeled accuracy are pending. A preceding retry failed on a Python
module-path packaging error and was settled.

A later regular Virginia L4 canary loaded directly from read-only FUSE with buffered reads and
`read-global-max-blocks=16`, without a disk copy. With the same 158 frozen decisions, 4 CPUs,
16 GiB, 300-second timeout and no retries, it loaded in 109.71 seconds, ran inference in 87.11
seconds and returned 158 structurally valid results in 196.84 seconds of processing. Peak GPU
allocation was 18,933,681,152 bytes and peak host RSS was 16,748,707,840 bytes. The host memory
margin remains narrow. All 13 model object generations matched the trusted manifest before and
after the run. FUSE does not pin object generations, and the run did not rehash the mounted bytes;
the earlier generation-pinned SHA-256 verification and pre/post checks do not rule out a transient
replacement. Actual incremental charges remain pending billing reconciliation.

On November 16, two additional regular L4 prototypes used the identical 158 inputs and settings.
The 6-CPU, 24-GiB bounded FUSE run loaded in 115.39 seconds and inferred in 88.73 seconds;
all 158 results passed structural validation. Process peak RSS was 16,952,311,808 bytes and
GPU allocator peak was 18,933,681,152 bytes. Sampled container memory was not a cgroup peak.
The 4-CPU, 16-GiB streaming prototype loaded in 115.95 seconds and inferred in 90.06 seconds;
all 158 results passed structural validation. Its kernel cgroup peak was 2,598,539,264 bytes
against a 17,179,869,184-byte limit; process peak RSS was 1,757,290,496 bytes and GPU allocator
peak was 18,936,318,976 bytes. The streaming prototype fetched generation-pinned objects in four
bounded concurrent readers, checked trusted 64-MiB slice hashes before copying weights to GPU,
and verified small files by SHA-256. Its slice manifest was derived from a previous whole-object
SHA-256 verification and baked into the immutable experimental image. The prototype was subsequently adapted into the repository worker as an experimental,
opt-in path described below. GPU allocator peaks do not include all driver allocations.

A separately authorized eight-reader streaming run used the same inputs and settings; its sealed
manifest differed only in the image digest. All 158 results, including all numerical values,
matched the four-reader streaming results exactly. Load fell from 115.954 to 90.382 seconds
(25.572 seconds, 22.1%); inference was 86.959 seconds and total local processing was 177.362
seconds. The kernel cgroup peak was 2,600,767,488 bytes and GPU allocator peak was unchanged at
18,936,318,976 bytes. This is one paired observation, not a performance distribution. The
64-MiB-slice loader allows eight parallel readers; its conservative retained payload bound is ten
slices, excluding transient copies and other memory. Local tests verified eight concurrent reads,
ordered reconstruction and rejection of nine readers; image CPU smoke tests passed.

The image digest was `sha256:387ef8f2dc4c8d2035ad7b72aa6278f679d2583733b2fc23fa9eba5c94ffe34e`.
Execution `citypods-decision-batch-xf6hf` succeeded in 3m14.53s; collection verified the output
generation and SHA-256 and created receipt
`ce25878dc25173fbfa6204b7f56aac0d74ad4f8664a9fd560ad7125ea1ad5faf`. The original Cloud Run job
spec was restored and verified. Before launch the $4 monthly gross project budget displayed $0.55
before credits, but billing can lag recent usage; actual incremental charges remain pending.

Further single-run comparisons used the same 158 frozen decisions, a Virginia L4, 4 CPUs,
16 GiB host memory, 64 MiB slices and otherwise matched inference settings. Each completed within
a 300-second timeout without task retries or a quota failure:

| Loader | Load | Inference | Kernel cgroup peak | Valid results |
|---|---:|---:|---:|---:|
| Matched eight-reader reference (`kp67r`) | 95.664 s | 88.289 s | 2,599,170,048 bytes | 158/158 |
| Sixteen readers (`99b4c`, same image) | 92.849 s | 90.538 s | 2,593,357,824 bytes | 158/158 |
| Eight readers, pinned double-buffer prototype (`vm9fn`) | 134.045 s | 87.821 s | 2,704,986,112 bytes | 158/158 |
| Instrumented repository reference (`bp2jg`) | 102.725 s | 91.841 s | 2,603,671,552 bytes | 158/158 |
| 16-slice prefetch prototype (`nlff9`) | 92.480 s | 88.988 s | 2,599,157,760 bytes | 158/158 |

The matched eight- and sixteen-reader runs used the same image; sixteen readers reduced observed
load by 2.815 seconds in this pair, while inference took 2.249 seconds longer. They had 153
bit-identical results; the five remaining outputs differed only in numerical confidence or NOUL
probability, with no answer-type change or 0.5 NOUL threshold crossing. The largest differences were
0.0077262 and 0.0038631, respectively. The pinned double-buffer prototype matched all 158 results
from the sixteen-reader run but did not improve load in this observation. The prefetch prototype
matched all 158 results from the instrumented repository reference. Its conservative raw-range
buffer bound was 25 slices (1.5625 GiB) plus overhead; this is a design bound, not measured
resident memory. All reported GPU allocator peaks were 18,936,318,976 bytes; those peaks exclude
other driver allocations. These are single samples, with startup variability and synchronous instrumentation, so none establishes a causal
speedup or justifies a production default. The prototypes are not part of the repository worker.

After final-image L4 and RTX collection, the guarded ledger recorded 21 attempts and $2.34 in gross
reservations, including two $0.30 RTX reserves, against a 23-attempt and $2.40 cap, with no
outstanding execution. Each L4 attempt reserved $0.09.
Reservations are accounting ceilings, not measured charges; actual incremental compute, network,
storage and build charges remain pending billing reconciliation. No quota increase is implied by
these experiments, and capacity or later availability is not guaranteed.

A paired prefetch comparison varied only the prototype's authenticated slice window (16 versus
32 slices), using eight range readers and the same L4 envelope above. The 16-slice run (`nlff9`)
loaded in 92.48 seconds with 2,599,157,760 bytes peak cgroup memory; the 32-slice run (`tgx47`)
loaded in 100.60 seconds with 3,633,790,976 bytes peak. Inference was 88.99 versus 88.44 seconds;
all 158 rows matched exactly. The 32-slice run showed no speedup and used about 0.96 GiB more
peak cgroup memory in this single comparison. Startup and network variability and synchronized
shard timing probes limit attribution. Both jobs were restored and verified. These prototypes are
not the repository loader and do not justify a production buffer choice.

A repository-integrated streaming canary on November 16 used the same 158 inputs, L4, 4 CPUs,
16 GiB and eight readers. The immutable image was
`sha256:56bd1a905b8fc8e34b451a088733dfb2dbd554beecbb0cc33fc924bf038e0c1f` and the execution was
`citypods-decision-batch-h65zr`. Its independently authenticated manifest pinned object generations
and slice hashes. Load was 93.15 seconds, inference 87.59 seconds and local processing 180.76
seconds. All 158 outputs passed structural validation; peak kernel cgroup memory was 2,602,553,344
bytes and GPU allocator peak was 18,936,318,976 bytes. Against the matched eight-reader prototype,
153 outputs were identical; five differed only in NOUL probabilities or confidence, without a
0.5 classification crossing. The largest absolute changes were 0.003864 and 0.007727 respectively.
The variation's cause is unknown. Collection verified the output and produced receipt
`a10cfd37ffdbf25cc4b5a6b78a69c9f1d3e19e28af8f88a125c82b6bafa7cac4`; the original job was restored
and verified. Incremental billing remains pending. This is a single L4 integration observation,
not a production or quality qualification. RTX PRO 6000 validation was subsequently run as
described below. A subsequent parser hardening change rejects non-UTF-8 headers; that source
change was not in this initial L4 canary image.

The post-review repository image
`sha256:1bcb37c637f10b61c2e2e632a592a766f845d646774ad46d6f4502fab1470624` contains the UTF-8,
odd-byte BF16 copy, failed-load cleanup and cgroup fallback fixes. Independent CPU smoke tested
module paths, both regional manifest hashes and the parser and byte-copy cases. On the same L4
inputs and envelope, execution `citypods-decision-batch-htq4r` returned 158/158 structurally valid
results, all bit-identical to the earlier repository L4 canary. Load was 104.082 seconds,
inference 88.177 seconds, local processing 192.279 seconds, and kernel cgroup peak 2,602,921,984
bytes. Collection verified the output and produced receipt
`dad3152e710ad3ebb426abf56b098fe184e30bc6309fb490ab1624da1ec84a1e`; the shared job was restored
and its full template verified. This is one successful final-image L4 observation, not a quality
or recurring-production qualification.

A subsequent Iowa RTX PRO 6000 canary used repository image
`sha256:bc6937f5687e795b08a041349e9abe82a8931830f4e9bd783281c455095f9842`, including the
post-L4 parser hardening. The image passed CPU smoke tests for its actual import paths, both
embedded regional manifest hashes and non-UTF-8 header rejection. The Iowa manifest bound each
object to independently verified generation and slice hashes; the regional manifest must remain
an authenticated trust root. With 20 CPUs, 80 GiB host memory and the same 158 decisions, execution
`citypods-decision-batch-rtx-n9qbq` loaded in 80.24 seconds, inferred in 38.68 seconds and returned
158 structurally valid results. The kernel cgroup peak was 2,741,825,536 bytes. Collection verified
the output and produced receipt
`364042926c57ca8f40d82a70497819d4d938529c3030a8ffee25deeb1d437467`. The L4 and RTX envelopes differ
in accelerator and host resources, so this is not an isolated GPU-speed comparison. Compared with
the repository L4 canary, 32 of 158 answers were bit-identical. None of the 122
NOUL decisions crossed 0.5, and none of the 12 choice or 24 integer score decisions changed.
Maximum absolute NOUL and confidence changes were 0.00781 and 0.01562, respectively; the cause
is unestablished. Quality and calibration and actual billed costs require separate analysis;
structural validity alone does not establish model accuracy. This remains an experimental path.
Subsequent source changes synchronized cgroup fallback updates, supported odd byte boundaries in
the BF16 sink, and cleared retained tensor owners on load failure. Those changes have offline tests
and were absent from the earlier GPU images. The hardened L4 and RTX canaries tested the rebuilt
source as described here.

A second Iowa RTX PRO 6000 canary tested the same post-review image
`sha256:1bcb37c637f10b61c2e2e632a592a766f845d646774ad46d6f4502fab1470624` as the hardened
L4 run, using the Iowa generation-pinned manifest and the same 20-CPU, 80-GiB envelope as the
first RTX run. Execution `citypods-decision-batch-rtx-wkpqh` returned 158 structurally valid
results, loading in 61.06 seconds and inferring in 37.96 seconds, with 99.03 seconds total local
processing. Peak kernel cgroup memory was 2,744,397,824 bytes and GPU allocator peak was
18,961,353,728 bytes. Collection produced receipt
`aff56035217d5b27c206d929a0a5b9d2964db36e697b990140463f6bffe88223`. Compared with hardened L4,
32 of 158 rows were bit-identical; no NOUL, choice or integer-score classifications changed.
Maximum absolute NOUL and confidence differences were 0.00781 and 0.01562, respectively.
The different host and GPU envelopes and single observations do not isolate performance causes
or establish quality or calibration; actual billed costs remain pending reconciliation.

The reusable `citypods/weight_streaming` package separates generation-pinned, authenticated GCS
range reads and safetensors validation from a model-specific sink. `citypods/judging/glide_stream`
is an opt-in GLiDE adapter, with independently authenticated manifests, synchronous copies, and
partial-model disposal on failure. Slice hashes can be derived offline only after validating files
against trusted whole-object hashes; the manifest itself remains a trust root. The existing local
model path remains the default. This experimental path needs further validation before recurring
or production use, including cost, failure recovery and model quality assessment.

Against the original 4-CPU FUSE results, streaming returned 153 of 158 results bit-identically.
The other five differed in NOUL probability or confidence; maximum absolute differences were
0.0038631 and 0.0077262 respectively. None changed the 0.5 NOUL classification or the 12 choice
and 24 score classes. The original generation-pinned disk-download and 4-CPU FUSE results were
identical for all 158. The 6-CPU FUSE results matched 57 of 158 original FUSE results exactly;
one of 122 NOUL outcomes crossed 0.5, with no choice or score class changes. Streaming versus
6-CPU FUSE matched 52 exactly and had the same one NOUL crossing. Maximum absolute NOUL and
confidence differences for both comparisons with 6-CPU FUSE were 0.0078119 and 0.0156238.
The cause of the numerical variation is unestablished; structural validity and agreement are not
quality or calibration qualification. FUSE generation checks before and after the 6-CPU run
matched all 13 pinned objects but do not exclude a transient replacement. Actual incremental
charges remain pending billing reconciliation.

Karpo 9B and 4B are Qwen3.5 family models with respective published BF16 weights of 16.8 GB
and 9.1 GB. Both completed a regular Virginia L4 canary over the same 158 frozen decisions. Karpo 4B
returned 158 structurally valid results, with 100.44 seconds of load, 103.06 seconds of
inference and 10.82 GiB peak GPU memory. Karpo 9B also returned 158 structurally valid results,
with 149.39 seconds of load, 109.34 seconds of inference and 18.0 GiB peak GPU memory. Its
285.53-second wall time leaves little margin under the 300-second task timeout. Structural
validation does not measure decision quality or calibration. Their native reader and
model-specific decision head require a separate experimental upstream runtime that is not
locked by this repository.
They share the `qwen` producer family with GLiDE and Perplexity Decider for the existing
independence rule. Karpo's 31B and 12B BF16 weights exceed or nearly fill L4 VRAM before
inference overhead. The maintainer limits live experiments to candidates with a credible
24 GB L4 path.
[Karpo 9B](https://huggingface.co/MachinePulseAI/karpo-decision-9B) ·
[Karpo 4B](https://huggingface.co/MachinePulseAI/karpo-decision-4B)

Google's current GPU-job guidance recommends concurrent Cloud Storage downloads for large
models. The successful canaries used a Virginia private bucket, Direct VPC egress with
`all-traffic`, Private Google Access, and concurrent ranged downloads to a 20 GiB ephemeral
disk. They verified pinned object generations and SHA-256 digests before loading. The earlier
FUSE-copy canary did not measure this configuration. Actual incremental network, disk, build,
and storage charges still need to be reconciled before recurring use.
[GPU job guidance](https://docs.cloud.google.com/run/docs/configuring/jobs/gpu-best-practices) ·
[Sliced downloads](https://docs.cloud.google.com/storage/docs/sliced-object-downloads) ·
[Direct VPC](https://docs.cloud.google.com/run/docs/configuring/vpc-direct-vpc)

For comparison, a separate image can place the pinned model weights in their own stable layers
and keep the image in a Virginia Artifact Registry repository. Cloud Run imports an image at
deployment and uses optimized container streaming. This can eliminate a repeated application
download, but Google recommends considering image-baked models primarily below 10 GB. GLiDE's
15.9 GB checkpoint requires measurement of image build, initial import, subsequent cold starts,
GPU loading and registry storage costs. Keep the runtime source and model revision pinned and
preserve integrity checks. A direct-to-GPU streaming option exists in Google's GKE/vLLM
documentation, but compatibility with GLiDE's custom typed readout and Cloud Run is unproven.
[Container and model trade-offs](https://docs.cloud.google.com/run/docs/configuring/jobs/gpu-best-practices) ·
[Registry pricing](https://cloud.google.com/artifact-registry/pricing) ·
[Run:ai streamer on GKE](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/persistent-volumes/run-ai-model-streamer)

On November 15 the approved Iowa L4 quota remained zero, while Virginia granted one L4.
Two regular Virginia canaries used the same 158 frozen decisions and a 300-second timeout.
The first reached inference approximately 289 seconds after the model bucket mounted and timed
out without a result. Its warning identified the PyTorch causal-convolution fallback. The second
used an instrumented worker and Cloud Storage FUSE buffered reads. It logged the beginning of
model loading, then timed out after 300 seconds without logging load completion. Both attempts
are settled in the admission ledger. The logs establish that model loading exceeded the second
task's limit. They do not isolate the exact operation inside the model loader. That attempt did
not complete within five minutes; the job template keeps the original mount.
The third canary copied the model from the mounted bucket to a 20 GiB ephemeral disk before
loading. Its copy did not finish before the 300-second timeout. It used buffered FUSE reads and
four file-copy workers, with a worker image derived from the second canary image. The logs show
the copy exceeded 264 seconds and do not identify a specific stall within it. The third attempt
is settled. The maintainer subsequently authorized seven additional GPU attempts, for ten total,
then one further eight-reader attempt, under the $4 gross monthly project budget. All eleven
attempts have been used. The successful GLiDE
and Karpo runs above used regular jobs only; no delayed-mode comparison or tag or moment
quality qualification is complete. Check project charges before another attempt.

On November 15, 2026, Cloud Run rejected the first job deployment at the non-zonal L4 quota
check. Cloud Quotas accepted the Iowa request for one L4 and denied it before any task was
launched. The project was eligible to submit the request and had zero allocation across the
five generally available L4 regions checked. Cloud Billing Console shows whether the linked
account is Free Trial or Paid. For a denied request, ask Cloud Customer Care or the account
team for a manual review with the request trace. If a different region receives quota first,
recheck the regional budget and keep model storage and the worker in the same region.
[Free Trial and Paid billing](https://docs.cloud.google.com/free/docs/free-cloud-features#how-to-monitor-and-manage-your-costs) ·
[Denied quota requests](https://docs.cloud.google.com/support/docs/checklists/plan-peak-capacity-event#request_a_quota_adjustment)

As checked on 2026-11-14, Iowa list rates per second are `$0.0001867` for a non-zonal L4 in both
modes. Regular Jobs list `$0.000018` per vCPU-second and `$0.000002` per GiB-second. Delayed Jobs
list `$0.0000126` and `$0.0000014` respectively. At 4 vCPU and 16 GiB those total
`$0.0002907` regular and `$0.0002595` delayed per billable second before free tier, about 11%
less gross compute for delayed. If the shared CPU/RAM free tier covers the run, the listed GPU
charge is identical. The fixed costs and model-load time are amortized by batching in either mode.
[Cloud Run pricing](https://cloud.google.com/run/pricing)

Google documents a one-minute billing minimum for both job modes, then 100 ms rounding. It
documents up to 12 hours of delayed provisioning before an instance starts. The queue time is
outside the billed container lifetime. At 120 executions of 60 seconds, usage is 28,800 vCPU-s
and 115,200 GiB-s, inside the regular free-tier equivalent of 240,000 vCPU-s and 450,000 GiB-s if
the billing account has that headroom. The free tier is pooled across projects. With this headroom,
both modes cost **$1.34 for GPU compute**. A regular execution submitted after filling a batch
gets the same GPU economics and avoids the preview's extra provisioning window. If CPU/RAM
headroom runs out, the delayed rate is cheaper for equal task lifetimes and retry counts.
[Cloud Run pricing and billable lifetime](https://cloud.google.com/run/pricing) ·
[Delayed Jobs](https://docs.cloud.google.com/run/docs/delayed-jobs)

| 30-day cadence, one task per launch | Seconds/task | GPU charge in either mode | Regular list compute | Delayed list compute |
|---|---:|---:|---:|---:|
| 1/day | 60 | $0.34 | $0.52 | $0.47 |
| 2/day | 60 | $0.67 | $1.05 | $0.93 |
| 4/day | 60 | $1.34 | $2.09 | $1.87 |
| 4/day | 120 | $2.69 | $4.19 | $3.74 |
| 4/day | 300 | $6.72 | $10.47 | $9.34 |

An Iowa G2 Spot VM in Google Cloud Batch is a lower compute-rate alternative. At the listed
`$0.402748/hour` for `g2-standard-4` (one L4, 4 vCPU, 16 GiB), it costs about $0.81 for 120
one-minute allocations before disk and other costs. It breaks even on compute at about 100 billed
VM seconds per run against a 60-second Cloud Run execution with free CPU/RAM. Startup, pulls,
preemption, retries and disks can consume that margin. Keep it as a benchmark candidate if usage
or actual Cloud Run costs justify a second deployment. The same file-based worker can run there.
[Spot VM pricing](https://cloud.google.com/spot-vms/pricing) ·
[Cloud Batch pricing](https://cloud.google.com/batch/pricing)

For scale, [review/53](53-judge-stack-tags-and-moments.md#capacity-and-reservations-at-800-meetings-a-day)
projected 7,200 tag subjects/day at 800 meetings/day. Covering that modeled volume with at most
1,000 decisions per execution would need eight daily launches, with a gross delayed-compute
floor of $3.74/month before storage, or $2.69 in GPU charges with free CPU/RAM. Fleet-wide coverage
depends on measured throughput, account-wide free-tier headroom and ancillary charges. Count
questions rather than API requests or files when sizing GPU capacity. At four
300-second launches/day, RAM alone uses 576,000 GiB-s. That exceeds the regular free-tier
equivalent even on an otherwise idle billing account.

About 15 GiB of weights in regional Standard GCS is about $0.30/month before shared free storage
versus roughly $1.45/month at Artifact Registry rates after its first 0.5 GiB. Budget separately for
the runtime image, model revisions, build minutes, GCS I/O and soft-deleted data, outbound result
bytes and logging. Google lists Cloud Run as eligible for the Google Developer Program credit for
Google AI Pro. Confirm the grant, redemption window, and linked billing account before launch, then
verify that the pilot SKUs actually draw the credit on the billing report.
[Storage pricing](https://cloud.google.com/storage/pricing) ·
[Artifact Registry pricing](https://cloud.google.com/artifact-registry/pricing) ·
[Cloud Build pricing](https://cloud.google.com/build/pricing) ·
[Google Developer Program benefits](https://developers.google.com/profile/help/benefits)

Use the maintainer's **$4/month gross envelope including ancillary GCP costs**.
Keep live runs manual. The authorized canary envelope was eleven L4 task attempts, all used, each
with a 300-second timeout and zero task retries. The CAS ledger retains $1.02 in reservations:
ten at $0.09 and the 6-CPU run at $0.12. No attempt is outstanding. Reservations are not actual
charges. Derive the
recurring attempt allowance only after estimating ancillary spend and measuring runtime. Before a
launch, reserve `max(60 s, task timeout) × gross rate × task count × max attempts`, including
ambiguous submissions until reconciled. Count manual launches through the same ledger. Reserve
across billing-window boundaries for jobs that may start in the next window. Use Cloud Billing
alerts as a secondary signal and check spend-cap eligibility for this project. Provider rates,
billing data, and ancillary charges can move, so the workflow allowance bounds its own launches;
the project budget does not automatically guarantee a spending cap.
[Billing budgets](https://docs.cloud.google.com/billing/docs/how-to/budgets) ·
[Cloud Run spend controls](https://docs.cloud.google.com/run/docs/jobs-cost-optimization)

The recurring dispatcher checks for eligible work hourly. Submit one nonempty batch when it reaches
the tested fill target (start the benchmark at 500 decisions), or when the **oldest eligible item
has waited 24 hours**, whichever comes first. Keep the oldest item's timestamp across producer runs.
An empty queue always skips launch. A quiet queue can therefore run once a day, with no fixed
quota of daily launches. Apply the measured size and token bounds and the one-outstanding and
budget gates ahead of any submit. A manual canary can explicitly use a smaller batch.

The 24-hour target measures enqueue to **submission**. Track Google provisioning, runtime and
collection separately. Prefer regular mode at equal measured cost so the oldest item does not
gain a second queueing delay. Delayed mode can add up to 12 hours after submission. Record misses
due to scheduler cadence, allowance or service capacity. Run a matched
regular/delayed canary with identical input, image, region and task settings, then compare billed
GPU time, net CPU/RAM, failed attempts and cost per 1,000 valid decisions. Review the actual GCP
SKUs and credits. Set the recurring mode explicitly after that comparison. Retain a single mode
per execution and charge retries through the same budget.

## Producer and worker contract

- **One logical item per decision.** Use a bounded T1 evidence window and the existing tag
  `SUPPORTED` rubric for tags. For moments, use T1 quote context for support, safety and usefulness.
  Keep all eligible quotes for a best-of-meeting comparison in one choice question. Group multiple
  question rows under the same state for transport only when they
  genuinely need the identical evidence chunk and grouping serves a purpose, such as preserving
  a matched benchmark comparison. Never combine unrelated chapter/meeting excerpts into shared
  state. Questions that depend on an earlier answer become a later producer pass. Cross-request
  GPU packing stays in the model runtime.
- **Stable identity.** Use versioned UTF-8 JSONL with one row per logical item. Fields are
  `schema_version: 1`, `item_id`, `task`, `episode_uid`, `subject_id`, `question_id`,
  `tier`, `prompt_version`, `evidence_digest`, `state` (exact evidence string), and
  `question` (`type`, `instructions`, and criteria for choice and score). Choice rows also carry
  `subject_ids`, the option-letter-to-candidate map for evaluation. Canonical serialization uses
  sorted keys, compact separators, UTF-8 without ASCII escaping, and finite numbers only.
  `item_id` is SHA-256 hex over the canonical row without `item_id`. This leaves a stable
  comparison key across models and changes it when the actual prompt or evidence changes. Model
  revision and runtime settings live in the batch manifest and form a separate result identity.
- **Native mapping.** Pass only `state` and `questions: {item_id: question}` to GLiDE. Keep the
  rubric in `instructions` and the exact evidence in `state`. Join answers by ID. The v1 pilot
  accepts `noul`, ordered `score`, and single-select `choice`. Preserve `noul` as P(yes). For the
  moment safety question, yes means unsafe or unfit to publish. Preserve forward and reversed
  choice orders. Add a shared-state envelope only when a producer demonstrates a transport gain.
- **Results.** One result row per requested ID, in request order. Fields are
  `schema_version: 1`, `item_id`, `status` (`ok`, `invalid_input`, `too_large`, or
  `retryable_error`), and for success a typed `answer` and optional `input_tokens`. Preserve
  `noul` probability, a zero-based ordered `score` or a selected `choice` key, with optional
  confidence, distribution and expected level where applicable. Validate bounds, exact
  distribution keys, and finite numeric fields. Unknown, duplicate, or missing IDs invalidate
  publication. Only a verified output with the complete expected ID set enters evaluation.
- **Size policy.** Treat 500–3,000 tokens per prompt as a working distribution. Measure the full
  tokenizer-rendered prompt, including rubric, choices and framing. Use a provisional 4,096-token
  hard cap for this workload and test 4k, 8k and 16k padded-token budgets on the L4 before choosing
  one. An oversized question gets `too_large`; preserve it for coverage accounting or a later
  evidence recipe. Also bound compressed and expanded bytes, rows, and estimated total input per
  batch. Start with the frozen set, then scale the representative performance replay up to
  500–1,000 questions only after smaller runs show fit and time headroom. Measure peak host memory
  as well as VRAM. A system or OOM error fails the attempt visibly and stays eligible for
  explicit retry under the allowance.
- **Evidence ownership.** Use public transcript excerpts and versioned prompt definitions.
  Keep truth labels separate from model inputs. Persist raw prompts and replies only in private,
  expiring pilot objects and report aggregate counts in Actions. For future live imports,
  recheck candidate and evidence identity before applying any row to the owning judge lane.

## Asynchronous transport

1. **Produce.** An Actions job exports validated rows, sorted by item ID. The uploader makes a
   create-only request object per bounded chunk in `pending/`. Multiple producers and Actions
   reruns converge by content identity. Each producer can exit after its upload. Initially the
   manual workflow can act as producer, dispatcher, or collector on separate runs.
2. **Seal and reserve.** A single authorized dispatcher reconciles prior executions before
   admission. It drains oldest eligible chunks for one model revision and runtime settings
   into a manifest with input object generations, SHA-256 hashes and counts, sorted unique IDs,
   model revision, runtime image digest and knobs. Manifest identity hashes that canonical
   content. Use GCS creation and generation-matched updates for a persistent dispatch state
   and allowance reservation. Seal only nonempty batches. The pilot allows one outstanding
   launch across dispatcher invocations, including a delayed execution awaiting provisioning.
3. **Launch.** Persist a unique attempt and reserve spend before calling Cloud Run. Send only
   the manifest ID and attempt ID to the fixed job via approved overrides and record the
   returned operation/execution identifiers. The pilot CLI path uses `gcloud beta run jobs execute
   --async` with `--no-delay-execution` for regular or `--delay-execution` for delayed. Both use
   the same job and only override those IDs. Record the explicit mode with each reservation.
   [CLI reference](https://docs.cloud.google.com/sdk/gcloud/reference/beta/run/jobs/execute)
   If submission succeeds but acknowledgement is
   lost, hold the reservation, look for the matching execution and worker start/result
   marker, and require operator reconciliation where acceptance is still ambiguous. Never
   treat lack of output as proof that launch did not happen. Enable one task, parallelism
   one, explicit task timeout, zero automatic task retries, and no automatic regular fallback.
4. **Run and publish.** The task accepts IDs only, reads generation-pinned input from configured
   bucket paths, and loads the reviewed model with vision disabled. Before model load, check
   for a committed output and claim the attempt with its execution ID. Validate and upload
   a local complete compressed result file at an attempt-specific key, then create a completion
   manifest containing the output generation, digest, expected count, and timings. Use generation
   preconditions and verify identity on a precondition miss. Create a single chosen result for
   a batch via the dispatcher’s state compare-and-swap. A stale attempt cannot replace it.
5. **Collect.** An independent Actions run verifies the manifest, digest, schema and one-to-one
   IDs and writes an immutable evaluation receipt keyed by batch/result identity. A crashed
   collector can repeat the import. A failed execution without a good output remains
   unresolved until reconciled, then may get a new attempt under the allowance. Capture
   request age and terminal row errors, and retry genuinely transient rows in a new child
   manifest if needed. Propose a 14-day raw retention window and retain aggregate reports
   without evidence text. Reconcile and archive receipts before their raw inputs expire.
   [GCS request preconditions](https://docs.cloud.google.com/storage/docs/request-preconditions)

Use GitHub OIDC federation tied to numeric repository and owner IDs, the trusted main ref, and the
approved workflow or environment. Scope identity by operation: producer can create input chunks,
dispatcher can seal/reserve and run the named job with overrides, Cloud Run reads inputs and weights
and creates outputs, and collector reads results and writes receipts. Verify permissions for execution
lookup and job overrides explicitly. The provisioning identity owns deployment separately. Pin Actions
and review the model license and notices before copying the weights. The wrapper loads only a reviewed
revision and accepts plain text rather than image or URI inputs. Supply credentials only to authenticated
cloud steps. [Google federation guidance](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines) ·
[Cloud Run IAM](https://docs.cloud.google.com/iam/docs/roles-permissions/run) ·
[Fastino license](https://huggingface.co/fastino/GLiDE-9B/blob/main/LICENSE)

## Implementation handoff

The first PR exercises A and the explicit-manifest canary path in B and C offline. The
[operator runbook](../workers/decision-batch/README.md) covers project setup, scoped IAM and WIF,
runtime dependency and model pins, and the two launch modes. The `decision_batch_pilot` CLI
prepares the existing frozen set, reserves a launch, and collects a generation-pinned result.
The Cloud Run gate starts off and accepts only manual dispatches on main with a protected
GitHub environment. The maintainer authorized eleven one-task L4 attempts under $4/month gross;
all have been used.
The repository launcher retains its existing nine-attempt, $0.09-per-attempt guard. The three
additional prototypes used isolated guarded launchers and a shared $1.02 ledger cap; this does
not extend the repository launcher. Reservations include ambiguous submissions. The completed
canaries used the one-task Virginia L4 job configuration.

The offline replay establishes contract and state-machine behavior. The Linux GPU lock is
separate from ASR and preserves the upstream CUDA 13.0 baseline. Image and GPU canary
evidence, measured prices and archived/holdout quality remain rollout gates. The manual canary
uses explicitly selected work and can replay a different launch mode as its own manifest.
Scheduled export, multi-producer chunk discovery, expiry and autonomous admission stay with
the recurring-shadow decision. Keep the admission ledger and receipts beyond raw-object expiry.

| Slice | Proposed files and implementation | Evidence required before advancing |
|---|---|---|
| A · Offline contract, workflow and worker · Terra | `citypods/judging/decision_batch.py` converts frozen tag and moment `Item`s into stable v1 rows. `scripts/eval_decision_batch.py` exports `frozen_units`. `workers/decision-batch/run_batch.py` consumes bounded gzip JSONL with a fake-model test seam. Manual-only `.github/workflows/decision-batch.yml` rehearses production and worker consumption and previews both launch modes. The workflow's cloud job is disabled. | Deterministic IDs, exact evidence/rubric, gold-free payload, all three question kinds, native-key round trip, malformed replies and complete output from the frozen exporter-to-worker replay. |
| B · GCS and Cloud Run transport · Terra | Add `citypods/judging/decision_batch_transport.py` and its tests. Wire independent upload, seal, launch and collect paths into the workflow using the same launch command builders for regular and delayed. Develop against fake object storage and fake Cloud Run. | Test repeated workflows, simultaneous dispatch, empty/oversized batches, lost acknowledgement, partial publish, wrong generation, replayed collection, expired work and allowance exhaustion. |
| C · Pinned L4 runtime and capped canary · Terra | Package `workers/decision-batch/` with image, job descriptor and runbook. Use review/22 to settle the GPU dependency lane (`pyproject.toml`, `scripts/compile_constraints.sh`, `scripts/python_lanes.py`, generated constraints as needed) before building. Maintainer supplies the project, WIF provisioning, quota and credit verification and approves the live envelope. | Run matched regular and delayed GLiDE-9B L4 canaries. Record queue, load, first batch, inference and end timings separately, peak GPU/host memory, real token histogram, validated decisions/s, charges and cost per 1,000 valid decisions. |
| D · Qualification and optional recurring shadow · Terra | Extend only the pilot workflow and evaluation report after canary evidence. Use frozen public controls and a meeting-disjoint labeled set. Persist matched results for GLiDE and saved Gemini/OpenRouter outputs where exact evidence and rubric align. | Compare coverage, precision, recall, Brier score and reliability, order/wording stability, and disagreements by context tier and producer family. Use conversation-grounded hard negatives such as passing mentions and near misses. Report sample sizes and meeting-clustered uncertainty. Define the acceptable error/coverage trade-off with the maintainer before any vote or recurring launch. |
| E · Live judge or tag prefilter · Future design | Revisit `citypods/judging/runner.py` and `families.py` for integration only after qualification. Live ownership, pending semantics, family order, and rollback belong in the L3 follow-up. | Demonstrate incremental coverage, acceptable end-to-end lag and cost, and compliance with the review/49 graduation rules. Disable new batch admission on rollback and retain receipts for already collected results. |

A checked-in `evals/judge/gold.json` has synthetic truth and planted controls. The live tag and
moment probe files are currently empty. Use archived human labels with matching evidence as a
separately reported baseline and use a small fresh blind holdout to corroborate it. Review/49
documents issues with old labels. Exclude mismatched rubrics, missing evidence and uncertain labels
from accuracy metrics. Use unlabeled real cases for coverage, stability and agreement. Freeze
separate calibration and holdout sets by meeting.
For model comparisons, match item IDs, evidence and rubric, and record the actual wire layout. If
the Gemini/OpenRouter comparison uses the production layout with evidence in the instructions,
run that layout on GLiDE as a paired control before attributing a quality difference to the model.
Report unavailable and oversized rows against the original sample denominator.

For future fixed-taxonomy tagging, GLiDE `choice` is single-select. Run one calibrated binary
question per plausible tag and permit zero or many accepted labels. Measure recall lost in any
candidate narrowing and count full fan-out in the GPU budget. Measure end-to-end savings against
the current tagger and use deterministic transcript alignment or the existing tagger to recover
a timestamped verbatim span before a proposed tag enters the current evidence and judge path.
Review/58 §12.8 and [#2675](https://github.com/BashfulBits/city-meeting-podcasts/issues/2675)
remain the proposal for a prefilter experiment.

### Maintainer decisions (2026-11-14)

| Decision | Chosen scope |
|---|---|
| First workload and model | Shadow validation of tags and moments using GLiDE-9B. |
| Source of truth | Archived human labels plus a small fresh blind holdout. |
| Eventual role | Qualify toward an alternate anchor under per-entry family rules. Keep later open weights possible where they add diversity or improve results. |
| Gross allowance | Up to $4/month including storage and builds. |
| Launch | Submit when the batch reaches its measured fill target or the oldest work reaches 24 hours. Empty queues skip. Compare regular and delayed modes, using regular when measured cost is tied. |
| First implementation handoff | Offline contract plus a disabled Cloud Run workflow and worker. |

Track the development-ready slices as issues when agreed, then update the design index. Record
the measured envelope and a changelog fragment when runtime slices ship.
