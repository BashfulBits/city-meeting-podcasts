# Chapter-locator evaluation set

This is the task-level home for locator manifests, scoring-only targets, sanitized model results,
and probe summaries, following [`chapter-agenda`](../chapter-agenda/README.md). Reusable builders
and runners remain in [`scripts/research/agenda_chapters/`](../../scripts/research/agenda_chapters/);
the design and production record remains in [`review/40`](../../review/40-generated-agenda-chapters.md).

## Frozen dataset v3

Frozen on 2026-09-26 from canonical meeting artifacts. The split is 96 development and 96 test
episodes, body-disjoint and UID-deduplicated, with 48 Granicus and 48 Swagit episodes in each split.
Provider chapters and timestamps are stored in scoring-only `dataset-v3/gold.json`. At freeze, the
test split was unscored; it has since been spent on the authorized exploratory route matrix below.

**Test-split status update (2026-09-26):** At the maintainer's explicit request, the eligible test
packets were sent to candidate routes for a first paired route comparison before the locator
admission bar was frozen. This spends the blind holdout to answer the current quality, context-fit,
and capacity questions. Treat the resulting scores as exploratory and do not present this split as
an independent final holdout after route or prompt tuning; a new unseen cohort will be needed for
that claim. Provider targets remained hidden from requests, and route comparisons used the same
frozen agenda/transcript inputs per meeting.

All 192 selected episodes have a provider chapter source and complete timed transcripts. Chapter
agenda generation is available for 29: 13 development and 16 test. The other 163 are excluded from
locator calls because generated agenda items are missing; they are dataset coverage gaps, not
locator failures. Among the 13 development rows, eight share the same frozen Mistral Medium 2508
agenda inputs and form the model-test pool. Five development rows with other agenda-model outputs
are excluded from this comparison so every candidate sees identical agenda evidence.

The eight common-input packets span 1,370–167,686 estimated tokens using the packet builder's
`characters / 4` estimate. Their median is 81,951. Six packets, up to 93,666 estimated tokens, are
the common core. The remaining two are 134,988 and 167,686 estimated tokens. With Gemini's current
`hard_input_ceiling=218,875` and measured `input_token_ratio=2.15`, the route's conservative
admission ceiling is about 101,802 builder-estimated tokens: all six common-core packets fit, while
the two largest do not. These are estimates for route admission, not provider tokenizer counts.
This explains the older 218,875 figure: before route-specific tokenizer ratios were applied, the
worker compared the raw `characters / 4` estimate directly to that provider-unit ceiling. The later
2.15 ratio correction reduced the admitted raw estimate; it did not reduce Gemini's context window.
The older setting therefore must not be read as proof that a real 218,875-token transcript was
accepted. It did mean the old scheduler could admit larger raw estimates than it does now, with a
risk of underestimating Gemini's provider-token use.

`dataset-v3/manifest.json` records the inputs and the frozen manifest hash. `gold.json` keeps
provider targets out of requests. `crosswalk.json` links provider chapters to the generated agenda
items for diagnostic scoring. Its automated audit found 92 strong, 8 possible, 3 ambiguous, and
2,131 unmatched provider chapters across the full cohort; generated-item matches were 99 mapped,
8 conflicted, and 71 unmapped. I spot-checked the title/reference pairs in the eight Mistral
development rows. The six-packet development comparison below therefore remains an automated
crosswalk proxy. For the later 16-meeting test-route matrix, all 100 provider chapters were manually
reviewed against the actual frozen agenda candidates; that separate human re-score is reported
below.

## Scoring contract

The scorer uses a fixed 60-second tolerance and keeps these measurements separate:

1. **Agenda coverage:** whether the supplied generated agenda evidence plausibly covers a provider
   chapter.
2. **Locator timing:** one-to-one provider-start recall and precision against provider timestamps.
3. **Item assignment:** correct agenda item plus valid boundary. The six-packet development result
   uses the algorithmic crosswalk proxy; the later 16-meeting test-route matrix has a separate
   manual crosswalk re-score.
4. **Operations:** completed/failed packets, provider calls, estimated input, latency, retries, and
   route limits.

Provider labels are excluded from every model request. Suspected wrong-item and unmatched anchors
are diagnostics, not confirmed errors. The prior research target of under 5% wrong publication is
not a frozen admission bar; no candidate is admitted by this development run.

For the six common-core packets, the automated crosswalk contains 35 strong item targets and 2
possible chapter candidates across 48 provider starts. The 74 generated agenda items are classified
as 37 mapped, 35 unmapped, and 2 conflicted. A model anchor for an unmapped agenda item cannot earn
a correct-item match here even if the item was discussed and the provider simply did not publish a
chapter for it. This is why the v3 item proxy cannot be compared to the earlier human-adjudicated
115/116 Gemini score or interpreted as a measured regression.

## Development comparison: 2026-09-26

All candidates received the same six common-core packets, the same Mistral Medium 2508 agenda
inputs, and the same locator runner with up to two structured-output attempts. The table's
`correct + valid` column is an **algorithmic crosswalk proxy**, not a human-reviewed accuracy
estimate. Timing recall and precision are measured directly against provider chapter starts.

| Model and route | Completed | Anchors | Correct + valid proxy | Provider-start recall / precision | Suspected wrong-item rate | Failed packets |
|---|---:|---:|---:|---:|---:|---:|
| Gemini 3.5 Flash Lite — Google primary | 3/6 | 16 | 10/16 (62.5%) | 88.2% / 93.8% | 25.0% | 3/6 |
| GLM 5.3 Flash — OrcaRouter free | 3/6 | 18 | 11/18 (61.1%) | 88.2% / 83.3% | 33.3% | 3/6 |
| DeepSeek V4 Flash — OrcaRouter free | 5/6 | 40 | 22/40 (55.0%) | 81.6% / 77.5% | 22.5% | 1/6 |
| Nemotron 3 Super 120B A12B — OpenRouter free | 5/6 | 30 | 21/30 (70.0%) | 78.1% / 83.3% | 26.7% | 1/6 |
| Step 3.7 Flash — Kilo free | 0/6 | — | Not scored | Not scored | Not scored | 6/6 |

Mean boundary error among timing hits was 4.86 seconds for Gemini, 4.90 for GLM, 2.51 for
DeepSeek V4, and 8.69 for Nemotron Super. The six-packet result is too small to establish quality,
and the item proxy is not human-adjudicated.

Nemotron Super also received the two packets beyond Gemini's conservative dispatch ceiling. One
completed and one failed schema validation. On the single completed packet it produced 2 anchors,
1 correct-item + valid-boundary proxy match, and 1 of 3 provider starts within tolerance. This
sample is too small to score an overflow route.

Failures were material to these results:

- Gemini: two Google input-TPM 429s from unpaced direct calls, plus one duplicate-unit validation
  error.
- GLM 5.3: one free-tier prompt-size rejection and two invalid JSON/schema results after repair.
- DeepSeek V4: the largest common-core packet (93,666 estimated tokens) exceeded OrcaRouter's
  practical free-tier prompt cap; the other five completed.
- Nemotron Super: one common-core response failed schema validation after repair.
- Step 3.7: all six packets exhausted schema validation retries. This measures the current route
  and structured-output contract, not the model's ability under a different JSON prompt.

At the time of this development comparison, the test split was untouched. It is now being used
for the authorized exploratory route matrix described above. Kimi K3 was not quality-scored in the
development comparison. An earlier SDK-mediated
DeepSeek V4.1 Flash try returned a 502 after 1,103.76 seconds. A later raw-stream diagnostic on
the same first development packet returned HTTP 200 from NVIDIA through the Cloudflare AI Gateway,
but took 14m13s for the first answer; that answer failed schema validation. The standard one-shot
schema repair then took 15m32s and returned valid JSON, with 3/3 provider-start hits on this one
short packet (1,370 builder-estimate units). Those three hits are a single-packet diagnostic, not a
quality estimate. Both responses spent most completion tokens on reasoning. The v2 NVIDIA pause
started with three Nemotron Ultra calls in flight, waited 322 seconds for them to drain, and resumed
cleanly after the diagnostic. Full raw SSE, requests, response IDs, and timestamped chunk logs stay
under /private/tmp/chapter-locator-telemetry/nvidia-v41-debug-20260926/ and its attempt2
companion; sanitized per-attempt metrics are in
[results/2026-09-26-deepseek-v41-debug.json](results/2026-09-26-deepseek-v41-debug.json).

### DeepSeek V4.1 JSON-mode diagnostic: 2026-09-27

The official [DeepSeek JSON Output guide](https://api-docs.deepseek.com/guides/json_mode/) asks
callers to set `response_format: {"type":"json_object"}`, include the word “json” and a concrete
output example in the prompt, and use a reasonable `max_tokens`. It also documents that empty
content can occur. DeepSeek's [Chat Completions reference](https://api-docs.deepseek.com/api/create-chat-completion/)
documents `json_object`; `json_schema` is documented under its separate [Responses API](https://api-docs.deepseek.com/api/create-response/)
using a different request envelope. The NVIDIA [V4.1 endpoint reference](https://docs.api.nvidia.com/nim/reference/nvidia-deepseek-v4_1_flash-infer)
describes an OpenAI-compatible Chat Completions request but does not detail JSON mode support. The
[V4.1 reference encoder](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash/blob/main/encoding/README.md)
supports mid-conversation system messages. To remove ordering as a variable, this rerun combined
all locator and JSON instructions in the first system message and left the packet as the final user
message. Earlier diagnostics appended extra system messages after the user packet; this is a tested
formatting difference, not evidence that the previous order was invalid for V4.1.

I repeated the test on the same 1,370-estimate-unit development packet (`d9ed782861f549dc`), with
one system message first, the packet as the final user message, an explicit schema, and the
documented example shape. The three variants ran sequentially after pausing and draining the NVIDIA
provider. Each finished with `finish_reason=stop`:

| Variant | HTTP / total / first byte | Completion / reasoning tokens | Output and locator validation |
|---|---:|---:|---|
| `json_object`, thinking off | 200 / 268.3s / 249.4s | 251 / 0 | 958 bytes; schema-valid, 5 anchors; 3/3 provider starts hit |
| No response format, thinking off | 200 / 313.4s / 221.6s | 679 / 0 | 2,783 bytes; JSON parsed but duplicate unit `u00016` made the locator response invalid |
| `json_object`, default thinking | 200 / 226.0s / 221.3s | 391 / 390 | 0 content bytes; 1,461 reasoning bytes; empty output |

This isolated a failure mode on one small packet: `json_object` with default thinking stopped
normally with reasoning tokens but no answer content. Disabling thinking made `json_object` valid;
prompt-only with thinking off emitted JSON but repeated a locator unit. The full development
comparison below tests whether those behaviors hold across packet sizes. No production route
setting changed; the route remains `prompt_only`.

Sanitized measurements are in
[`results/2026-09-27-deepseek-v41-json-format.json`](results/2026-09-27-deepseek-v41-json-format.json).
Raw requests, complete SSE bodies, and timestamped events remain private under
`/private/tmp/chapter-locator-telemetry/nvidia-v41-json-format-20260927T024601Z/`; the stream files
can include private reasoning text.

### Full development JSON and thinking-mode comparison: 2026-09-27

All eight common-input development packets were sent in three variants (24 calls total), with
concurrency capped at three and no repairs. NVIDIA was paused and drained before dispatch; all
requests completed, then the pause was cleared (`in_flight=0`). The model saw the same prompt
messages and packet in each variant. `json_object` plus thinking off and prompt-only plus thinking
off differed only in `response_format`. The default-thinking arm used prompt-only mode and omitted
`chat_template_kwargs`, leaving reasoning at the provider default. The same packet-0 message hash
matches the earlier one-packet JSON-mode diagnostic.

Quality below is the automated development crosswalk proxy with a 60-second start tolerance.
Invalid responses count as zero hits against all 39 provider starts. Precision proxy is hits divided
by emitted anchors from valid responses. This is not a manual item adjudication or a final admission
score.

| DeepSeek V4.1 mode | Valid packets | Provider-start recall proxy | Precision proxy | Mean / median / max seconds | Mean first byte | Reasoning tokens |
|---|---:|---:|---:|---:|---:|---:|
| `json_object`, thinking off | 4/8 | 12/39 (30.8%) | 12/20 (60.0%) | 191 / 148 / 472 | 155 s | 0 |
| Prompt-only, thinking off | 5/8 | 19/39 (48.7%) | 19/29 (65.5%) | 184 / 196 / 293 | 152 s | 0 |
| Prompt-only, default thinking | 8/8 | 37/39 (94.9%) | 37/58 (63.8%) | 718 / 591 / 1,210 | 118 s | 65,716 |

Per-packet timings and scores show where the default-thinking lift came from. Every call finished
with `finish_reason=stop`; the thinking-off failures below were duplicate-unit locator validation
errors, not empty content or truncated JSON.

| Packet (builder estimate) | `json_object`, off (seconds; hits) | Prompt-only, off (seconds; hits) | Prompt-only, default (seconds; hits) |
|---|---:|---:|---:|
| `d9ed782861f549dc` (1,370) | 117; 3/3 | 216; 3/3 | 590; 3/3 |
| `bb56b844c811808e` (93,666) | 236; invalid duplicate `u00040` (0/9) | 202; 7/9 | 591; 8/9 |
| `3edef9145e6fc089` (23,324) | 148; 5/5 | 118; 5/5 | 552; 5/5 |
| `ab1cfb601c5c94f0` (167,686) | 148; 2/2 | 200; 2/2 | 303; 2/2 |
| `0514f06cb748dbed` (20,634) | 69; 2/3 | 92; 2/3 | 484; 3/3 |
| `193ef567da722a3b` (78,671) | 96; invalid duplicate `u00001` (0/4) | 192; invalid duplicate `u00001` (0/4) | 930; 4/4 |
| `b713de3cac686c35` (134,988) | 472; invalid duplicate `u00001` (0/2) | 293; invalid duplicate `u00001` (0/2) | 1,087; 2/2 |
| `25902d828bbdc60a` (85,231) | 243; invalid duplicate `u00001` (0/11) | 163; invalid duplicate `u00001` (0/11) | 1,210; 10/11 |

On packet 0, the earlier sequential same-prompt control took 268.3 seconds for valid `json_object`
and 313.4 seconds for invalid prompt-only output (duplicate `u00016`). The current concurrency-three
pair took 117.2 and 215.8 seconds, respectively, and both returned 3/3 hits. Earlier same-packet
default-thinking `json_object` returned empty content at 226.0 seconds; current prompt-only default
thinking returned valid content and 3/3 hits at 590.5 seconds. The message bytes match, but
elapsed time varies between runs and the previous calls were sequential, so these duration changes
should not be attributed to `response_format` alone.

There is also a useful directional check against the earlier three-request overlap run, which used
prompt-only with default thinking but a different compatibility-prompt placement. On the shared
packets, it scored `bb56b844c811808e` at 8/9 in 556.5 seconds, `b713de3cac686c35` at 2/2 in
837.8 seconds, and the largest `ab1cfb601c5c94f0` at 2/2 in 299.0 seconds. The current same-prompt
default-thinking arm matched those hit counts at 590.9, 1,087.2, and 302.9 seconds respectively.
The repeated quality is encouraging; prompt construction differs, so timing is not an exact
replication. See [`results/2026-09-27-deepseek-v41-overlap.json`](results/2026-09-27-deepseek-v41-overlap.json)
for the earlier raw metrics.

The full matrix suggests that default thinking substantially improves first-pass usability and
recall on this development set, especially for packets where both thinking-off variants repeated a
unit. It also increases mean latency about 3.9× and emits many more anchors; precision remains about
64%, so the extra recall comes with overprediction. `json_object` did not improve on prompt-only
with thinking off: it had one fewer valid packet and seven fewer hits. The largest packet (167,686
builder-estimate tokens) was accepted and valid in all modes; that JSON-object request reported
273,070 provider prompt tokens. These are development crosswalk results, not a route-policy change.

Sanitized per-request results are in
[`results/2026-09-27-deepseek-v41-development-modes.json`](results/2026-09-27-deepseek-v41-development-modes.json).
Raw requests, complete response streams, and event logs remain private under
`/private/tmp/chapter-locator-telemetry/nvidia-v41-paired-development-20260927T035749Z/`; default
thinking streams include private reasoning text.

## Live context probes

Context probes ran directly against providers after pausing and draining conflicting v2 work; every
probe request was reserved against its route. The measured synthetic size (`characters / 4`) and
provider-reported prompt tokens are distinct. A successful size is a lower bound; TPM failures and
server errors do not establish the model's true context maximum.

| Model and route | Largest successful synthetic size (`chars / 4`) | Provider prompt tokens | Probe limit or dispatch note |
|---|---:|---:|---|
| Gemini 3.5 Flash Lite — Google | 244,354 | 217,202 | First larger attempt hit 250k/min input TPM. Dispatch ratio 2.15 makes the effective configured ceiling ~101,802 estimate units. |
| Nemotron 3 Super — OpenRouter | 261,123 | 232,117 | No rejection at this size; configured hard ceiling 249,027 estimate units. |
| Step 3.7 Flash — Kilo | 261,123 | 232,113 | No rejection at this size; configured hard ceiling 249,027 estimate units. |
| DeepSeek V4.1 Flash — NVIDIA | 562,750 | 500,251 | Server errors above this; context ceiling remains inconclusive. No dispatch hard ceiling is configured. |
| Kimi K3 — NVIDIA | 747,074 | 664,147 | No rejection through probe maximum; configured dispatch ceiling 234,437 estimate units. |
| DeepSeek V4 Flash — OrcaRouter | 106,328 | 94,594 | Larger free-tier prompt rejected at 109,253 estimate units; a 93,666-token real packet also failed. |
| GLM 5.3 Flash — OrcaRouter | 106,328 | 94,523 | Larger free-tier prompt rejected at 109,253 estimate units. |
| Gemini 3.7 Flash — Google | 125,500 | 111,552 | Larger request hit provider TPM, not a context-window error. |

Gemini 3.5 therefore has a live provider-confirmed floor of 217,202 prompt tokens, but the
production dispatch policy admits only about 101,802 packet-builder estimate units under its
conservative token-ratio multiplier. It covers the P50 packet and all six common-core packets; it
does not currently admit the two largest packets. The full probe summary, including rejection
classes, is in [`results/2026-09-26-context-probes.json`](results/2026-09-26-context-probes.json).

## Exploratory test-split route matrix: 2026-09-26

Six routes received the same 16 frozen test packets (96 paired route calls) with provider chapter
labels hidden. The test split has 96 episodes, but 80 have no generated agenda input and were not
sent. The 16 packets use three agenda sources: 13 Mistral Medium 2508, one Gemini 3.1 Flash Lite,
and two Nemotron 3 Ultra. Each route saw the same agenda and transcript for a given meeting.

The checked-in automated crosswalk was Mistral-only and did not cover the three other-source
packets. For the matrix, I rebuilt automated matches from each packet's actual frozen agenda
source. That found 54 strong item-to-chapter targets among 100 provider chapter starts; the other
46 were 37 unmatched, 6 possible, and 3 ambiguous. The 120 generated agenda items classify as 59
mapped, 55 unmapped, and 6 conflicted. The initial route table below uses this automated crosswalk;
a complete manual adjudication and re-score follows it.

The columns below measure different things. **Broad timing hits** count predicted starts within
60 seconds of any of the 100 provider starts, count failed packets as misses, and do not check
agenda-item identity. The parenthesized recall uses only starts from meetings with a valid output.
Timing precision is timing hits divided by emitted anchors. **Strong links** count predictions with
the right agenda-item index and a boundary within 60 seconds, against the 54 automated strong
targets. **Anchor lower bound** divides those confirmed-by-crosswalk hits by all emitted anchors.
It is not true precision: anchors that the crosswalk cannot confidently classify remain unresolved,
and suspected wrong-item anchors are not confirmed errors. Boundary errors use timing hits only.

| Model and route | AA | Valid | Timing recall: all (valid only) | Timing precision | Strong links / 54 | Anchor lower bound | Mean / p95 boundary error | Final failures |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| [Gemini 3.5 Flash Lite](https://artificialanalysis.ai/models/gemini-3-5-flash-lite) — Google | 22 | 13/16 | 68% (80%) | 68/88 (77.3%) | 45 (83.3%) | 45/88 (51.1%) | 4.46 / 17.32 s | 3 validation errors |
| [Nemotron 3 Super](https://artificialanalysis.ai/models/nvidia-nemotron-3-super-120b-a12b) — OpenRouter | 13 | 11/16 | 44% (67.7%) | 44/60 (73.3%) | 36 (66.7%) | 36/60 (60.0%) | 8.51 / 42.04 s | 2 invalid JSON, 1 schema, 2 upstream unavailable |
| [Kimi K3](https://artificialanalysis.ai/models/kimi-k3) — NVIDIA | 44 max | 14/16 | 62% (73.8%) | 62/80 (77.5%) | 34 (63.0%) | 34/80 (42.5%) | 5.29 / 22.15 s | 2 schema errors |
| [DeepSeek V4.1 Flash](https://artificialanalysis.ai/models/releases/deepseek-v4-1-flash) — NVIDIA | 39 max | 16/16 | 79% (79%) | 79/98 (80.6%) | 47 (87.0%) | 47/98 (48.0%) | 5.88 / 25.45 s | 0; all 16 needed repair |
| [Step 3.7 Flash](https://artificialanalysis.ai/models/step-3-7-flash) — Kilo | 19 est. | 0/16 | 0% (no valid packet) | — | — | — | — | 15 schema errors, 1 context reject |
| [GLM 4.7 Flash](https://artificialanalysis.ai/models/glm-4-7-flash) — Z.AI | 15 | 11/16 | 47% (64.4%) | 47/53 (88.7%) | 24 (44.4%) | 24/53 (45.3%) | 5.01 / 19.71 s | 4 invalid/duplicate, 1 context reject |

The table above preserves the original automated-crosswalk scores. The human re-score below
supersedes its strong-link and anchor-lower-bound columns; timing-only metrics and route failures
remain unchanged. Historical Gemini 3.5's 115/116 result used a different human-adjudicated slice
and scoring contract, so these v3 results do not establish a regression. The v3 test split has been
used and is not an untouched holdout for future route or prompt selection.

### Human-adjudicated crosswalk re-score: 2026-09-26

All 100 provider chapters in the 16 route-tested meetings were reviewed against each meeting's
actual frozen agenda candidates, with chapter timings and locator outputs hidden. Review produced
83 direct item links and 3 consent/composite links (86 eligible targets), plus 5 procedural/section
chapters, 3 missing generated candidates, and 6 source or extraction problems. The latter 14 cases
are excluded from item-and-boundary denominators. The 16 meetings still contain the only independent
clusters in this comparison.

Manual review confirmed all 54/54 automated strong crosswalk links against the exact candidate
index. It also recovered 32 additional eligible links that the automated crosswalk had classified
as 5 possible, 24 unmatched, and 3 ambiguous. Thus the automated strong-only proxy was accurate
for the links it accepted in this sample, while covering 54/86 of the manually eligible targets.

| Locator route | Valid packets | Timing hits / 100 | Human item identity / 86 | Human item + boundary / 86 | Item correct among timing-matched eligible targets | Confirmed linked anchors / all anchors |
|---|---:|---:|---:|---:|---:|---:|
| Gemini 3.5 Flash Lite — Google | 13/16 | 68% | 71/86 (82.6%) | 65/86 (75.6%); 95% interval 52.2–91.7% | 59/65 (90.8%) | 65/88 (73.9%) |
| Nemotron 3 Super — OpenRouter | 11/16 | 44% | 48/86 (55.8%) | 42/86 (48.8%); 95% interval 21.7–71.8% | 31/44 (70.5%) | 42/60 (70.0%) |
| Kimi K3 — NVIDIA | 14/16 | 62% | 63/86 (73.3%) | 59/86 (68.6%); 95% interval 41.7–89.6% | 56/60 (93.3%) | 59/80 (73.8%) |
| DeepSeek V4.1 Flash — NVIDIA | 16/16 | 79% | 78/86 (90.7%) | 75/86 (87.2%); 95% interval 75.3–95.2% | 70/77 (90.9%) | 75/98 (76.5%) |
| GLM 5.3 Flash — OrcaRouter, prompt-only | 9/16 | 48% | 49/86 (57.0%) | 47/86 (54.7%); 95% interval 27.1–81.2% | 45/48 (93.8%) | 47/57 (82.5%) |
| GLM 5.3 Flash — OrcaRouter, configured JSON object | 10/16 | 48% | 48/86 (55.8%) | 46/86 (53.5%); 95% interval 27.3–83.1% | 44/48 (91.7%) | 46/58 (79.3%) |
| Step 3.7 Flash — Kilo | 0/16 | 0% | 0/86 | 0/86; no valid outputs | — | — |
| GLM 4.7 Flash — Z.AI | 11/16 | 47% | 50/86 (58.1%) | 47/86 (54.7%); 95% interval 32.4–74.1% | 43/47 (91.5%) | 47/53 (88.7%) |

**Metric definitions.** Item identity counts one-to-one assignments to any human-linked candidate,
regardless of boundary. Item + boundary additionally requires the predicted start to be within 60
seconds of that chapter's provider start; failed packets count as misses. “Item correct among timing-
matched eligible targets” checks the agenda-item index only among one-to-one timing matches to the 86
eligible chapters. “Confirmed linked anchors” divides item-and-boundary hits by every emitted
anchor; it is a conservative lower bound because candidates without a reviewed provider-chapter
link may still represent valid chapters. The intervals use 20,000 meeting-cluster bootstrap draws;
they are descriptive and overlap substantially for Gemini, Kimi, and DeepSeek.

DeepSeek has the highest observed item-and-boundary recall in this sample, while Kimi and Gemini
are close on item correctness conditional on a timing match. The meeting-cluster intervals and
16-meeting sample do not establish a decisive population ranking or an admission bar. DeepSeek's
16/16 valid packets also required schema repair, and its one-slot throughput remains latency-bound.
The historical 115/116 Gemini result remains separate. GLM 5.3 Flash ran the same frozen packets
and crosswalk twice on 2026-09-27. Configured JSON-object mode produced 10/16 schema-valid packets
and 46/86 human item-and-boundary hits (53.5%); prompt-only mode produced 9/16 valid packets and
47/86 hits (54.7%). Their wide meeting-cluster intervals overlap substantially, so JSON-object
mode improved output validity slightly but did not improve measured locator quality. Its separate
six-packet NVIDIA screen did not produce valid locators and is not a quality score.

**GLM 5.3 route follow-up (2026-09-27).** OrcaRouter Flash used the same 16 packets, locator
prompt, and local validation in two arms: prompt-only, then the actual configured `json_object`
format with the route schema included in the prompt. Prompt-only used 29 serial provider requests;
configured JSON-object mode used 20. Thirteen packets received provider responses in each arm.
Prompt-only returned 9 valid packets and 4 malformed outputs; JSON-object returned 10 valid
packets and 3 schema failures after repair. Both modes had the same three explicit per-request
free-tier prompt-size rejections. The JSON-object arm used the route-aware direct LiteLLM backend,
which allows one provider-side Pydantic correction attempt, plus the locator runner's one repair.
That request shape matches the compiled route, but its retry flow is not the v2 Worker's retry
schedule; request counts here are not a sustained dispatch-capacity benchmark.

The largest admitted packet estimate was 63,127 tokens (84,128 provider-reported prompt tokens)
in prompt-only mode and 63,127 (84,490 provider tokens) in JSON-object mode. The next larger
packet, estimated at 90,865 tokens (91,312 with the JSON-object schema prompt), was rejected; the
120,826- and 188,657-token packets were also rejected. This observed 13/16 admission rate is
specific to these packets and the OrcaRouter free tier; the configured one-million context length
is not the effective free-tier request cap. The errors identify a single-request size limit, not
a TPM limit. The route is configured for 800 requests/day, 10 RPM, and per-model concurrency 2;
chapter-agenda does not use this exact model or its per-model RPD, but its Tencent HY3 route can
also run through OrcaRouter and shares provider-wide concurrency. The test's 20–29 requests for
16 packets do not establish a sustained jobs-per-day rate.

The full GLM 5.3 model was screened directly on NVIDIA for six packets, using serial calls. Three
HTTP 200 responses contained no valid locator: the small packet invented a unit ID, while the
90,900-token estimate was admitted at 134,149 prompt tokens but exhausted the 8,192-token output
budget on reasoning with an empty content field. Four calls returned HTTP 504 after about 302
seconds, including estimates from 17,259 to 188,693 tokens. The local client read timeout was
1,200 seconds, so these were remote gateway timeouts, not the 720-second harness setting and not
evidence of an input-token cap. This screen was stopped at six packets because NVIDIA capacity is
shared with Nemotron 3 Ultra in chapter-agenda; no quality score or context ceiling is inferred.
The detailed sanitized artifacts are
[`2026-09-27-glm-53-flash-prompt-only-human-crosswalk.json`](results/2026-09-27-glm-53-flash-prompt-only-human-crosswalk.json),
[`2026-09-27-glm-53-flash-json-object-human-crosswalk.json`](results/2026-09-27-glm-53-flash-json-object-human-crosswalk.json),
[`2026-09-27-glm-53-flash-mode-comparison.json`](results/2026-09-27-glm-53-flash-mode-comparison.json),
and [`2026-09-27-glm-53-nvidia-screen.json`](results/2026-09-27-glm-53-nvidia-screen.json).
Raw provider telemetry is retained under `/private/tmp/chapter-locator-telemetry/`.

### Prompt-contract ablation: 2026-09-27

I compared the current production prompt with one extra system instruction on the same three
common development packets (1,370, 23,324, and 20,634 builder-estimate tokens). Each request used
the compiled route's actual structured-output method and registered locator schema. The added
instruction named the exact output keys, prohibited invented unit IDs and duplicate agenda/unit
assignments, and said how to resolve competing items. The test split was not used.

| Route | First-pass schema-valid | First-pass locator-valid | Pydantic retries, baseline → added | Crosswalk hits after retry, baseline → added |
|---|---:|---:|---:|---:|
| Gemini 3.5 Flash Lite — Google `json_schema` | 3/3 → 3/3 | 3/3 → 2/3 | 0 → 0 | 8/11 → 5/11 |
| GLM 5.3 Flash — OrcaRouter `json_object` | 3/3 → 3/3 | 3/3 → 3/3 | 0 → 0 | 9/11 → 10/11 |
| DeepSeek V4 Flash — OrcaRouter `json_object` | 2/3 → 2/3 | 1/3 → 2/3 | 1 → 1 | 5/11 → 9/11 |

Invalid responses count as zero against the same 11 provider starts. On DeepSeek V4 Flash, the
added prompt removed a duplicate-unit failure but produced a malformed six-digit unit ID on
another packet; the route's one Pydantic correction then returned a valid locator. The total
Pydantic retry count stayed at one in both arms. GLM's three baseline replies were already valid,
so the added prompt did not reduce retries; the one extra crosswalk hit is only a three-meeting
automated proxy. Gemini's baseline was valid on all three packets, while the added prompt caused
one unknown-unit-ID failure. These results do not support applying the new prompt globally.

DeepSeek V4.1 already had a more detailed prompt in its eight-packet development comparison,
including an explicit schema/example and instructions not to repeat unit IDs or agenda indices.
Its default-thinking prompt-only arm was locator-valid on 8/8 packets with no repairs; the
thinking-off arm was valid on 5/8 and still had duplicate-unit failures. That earlier comparison
did not isolate prompt wording from a control prompt, but it shows the selected default-thinking
mode has no observed schema retries to remove and that repeating the uniqueness instruction alone
did not fix the thinking-off duplicates.

The retry counts above are from the direct LiteLLM structured-output path, which permits one
Pydantic correction call. They are **not** the v2 Dispatch Worker's retry schedule, and a
schema-valid JSON object can still fail locator checks for an unknown or repeated unit ID. Kimi K3
was not included in this prompt ablation: its calls share NVIDIA provider capacity with the
Nemotron 3 Ultra agenda route, and a Nemotron request was in flight when the scope was set. No
production prompt or route changed. The exact Orca target routes were paused, but unrelated
Tencent HY3 work was active on the provider; compare measured latency cautiously. An initial local
harness error also recorded six unused route reservations on each Orca route before any provider
call. Those twelve local ledger reservations reduce dispatch headroom until the daily reset; no
upstream quota was consumed by them. Per-packet metrics are in
[`2026-09-27-prompt-schema-ablation.json`](results/2026-09-27-prompt-schema-ablation.json); raw
requests and responses, which can include model reasoning, remain private under
`/private/tmp/chapter-locator-telemetry/`.

| Model and route | AA | Configured packet gate | Observed context fit in test set | RPD after agenda subtraction | Serial job/day ceiling |
|---|---:|---:|---|---:|---:|
| Gemini 3.5 Flash Lite — Google | 22 | 101,802 units; 14/16 (87.5%) under gate | 16/16 replies (100%), including largest | 1,000 requests/day (500/key) | ~1,034 with two keys* |
| Nemotron 3 Super — OpenRouter | 13 | 249,027; 16/16 (100%) | 14/16 replied (87.5%); 2 unavailable; none rejected for context | 200 requests/day | ~177 one slot |
| Kimi K3 — NVIDIA | 44 max | 234,437; 16/16 (100%) | 16/16 replied (100%), including largest | Not published | ~582 one slot |
| DeepSeek V4.1 Flash — NVIDIA | 39 max | 1,000,000 configured; 16/16 (100%) | 16/16 valid (100%), including largest | Not published | ~44 one slot |
| Step 3.7 Flash — Kilo | 19 est. | 249,027; 16/16 (100%) | 15/16 replied (93.8%); largest rejected | 200 requests/day | ~553 one slot; 0 valid outputs |
| GLM 4.7 Flash — Z.AI | 15 | 199,222; 16/16 (100%) | 15/16 replied (93.8%); largest rejected | 500 requests/day | ~106 one slot; ~212 at two slots* |

The context gate is an estimate from `characters / 4`, adjusted only where a measured token ratio
is configured. It predicts dispatch admission, not the provider's real ceiling: Gemini's gate
filters two packets even though its live probe accepted 244,354 estimate units (217,202 provider
tokens) and the direct matrix received replies for all 16. Conversely, Step and GLM config gates
predicted all 16, but each explicitly rejected the largest packet. DeepSeek's configured one-million
limit also lacks a measured tokenizer ratio or hard dispatch ceiling. The per-route context probes
are lower bounds; see the probe table above.

RPD is the configured provider-request limit; none of these exact models consumes chapter-agenda
model-specific RPD. Serial job/day ceilings divide the slowest observed full-job duration by a day;
they include schema repairs and do not account for TPM pacing, provider queueing, or the number of
provider requests per job. Gemini's matrix used the primary key; its ceiling assumes both keys are
available. The GLM two-slot estimate assumes linear scaling. These are not sustained-throughput
tests. NVIDIA, OpenRouter, and Kilo provider concurrency is still shared with chapter-agenda
routes. Kimi's first call spent 603 seconds waiting for an existing NVIDIA request to drain; its
latency estimate subtracts that wait. No production route assignment or limit changed for this test.

**DeepSeek concurrency has one controlled three-call overlap result.** Three full development
packets started within 0.2ms after NVIDIA drained and returned schema-valid results on the first
attempt, with no repair. Their estimated sizes were 93,666, 134,988, and 167,686; provider-reported
prompt sizes were 123,780, 189,229, and 273,043 tokens. This establishes that three overlapping
requests completed once; it does not establish sustained capacity or a safe production cap. The
per-route `concurrency: 1` remains a conservative scheduler setting to protect NVIDIA's shared
Nemotron 3 Ultra capacity. The route's estimated ~44 jobs/day is based on one slot and serial
latency. NVIDIA's provider-wide `concurrency: 3` is also a shared scheduler setting, not a
DeepSeek-specific capacity measurement. Full raw telemetry is under
`/private/tmp/chapter-locator-telemetry/nvidia-v41-concurrency-20260927T020551Z/`; sanitized
results are in
[`results/2026-09-27-deepseek-v41-overlap.json`](results/2026-09-27-deepseek-v41-overlap.json).

AA values are general benchmark indices, not chapter-locator scores. Current checked values are
Gemini 22, Nemotron 13, Kimi 44 at max effort, DeepSeek V4.1 39 at max effort, Step 19 estimated,
and GLM 4.7 Flash 15 reasoning (11 non-reasoning). They help choose candidates to test; they do not
establish task quality.

### Coverage prompt experiment: 2026-09-27

The earlier DeepSeek V4 Flash result quoted in this thread had 54/61 correct agenda-item
assignments among eligible chapters where it found a start (88.5%), while its 95% meeting-cluster
interval for the overall score was wide (43.0–88.4%). Kimi K3 showed the same pattern in the
16-meeting human crosswalk: 56/60 correct item assignments among timing matches (93.3%), but
59/86 item-and-boundary hits (68.6%; 95% meeting-cluster interval 41.7–89.6%). This points to a
coverage gap; item assignment is usually correct when a start is found.

I tested two prompt ideas against the current prompt: (1) check agenda items one by one and scan
the full transcript for each; (2) sweep the transcript chronologically for clear topic transitions,
then reverse-check the agenda list. The comparison used the same eight packets and current prompt
as a same-run control for DeepSeek V4 Flash on OrcaRouter, GLM 5.3 Flash on OrcaRouter, and Kimi K3
on NVIDIA. The packets contain 56 manually adjudicated eligible chapters. Arm order was rotated
between packets. Each call allowed the existing one-repair path; any response still invalid after
repair counts as zero hits.

| Model and prompt | Valid packets | Item + boundary hits / 56 | Conditional item accuracy |
|---|---:|---:|---:|
| DeepSeek V4 Flash — current | 8/8 | 45/56 (80.4%) | 43/48 (89.6%) |
| DeepSeek V4 Flash — agenda checklist | 8/8 | 46/56 (82.1%) | 39/49 (79.6%) |
| DeepSeek V4 Flash — transition sweep | 8/8 | 48/56 (85.7%) | 46/50 (92.0%) |
| GLM 5.3 Flash — current | 6/8 | 26/56 (46.4%) | 24/28 (85.7%) |
| GLM 5.3 Flash — agenda checklist | 4/8 | 23/56 (41.1%) | 21/25 (84.0%) |
| GLM 5.3 Flash — transition sweep | 6/8 | 35/56 (62.5%) | 33/37 (89.2%) |
| Kimi K3 — current | 7/8 | 41/56 (73.2%) | 41/42 (97.6%) |
| Kimi K3 — agenda checklist | 4/8 | 21/56 (37.5%) | 21/22 (95.5%) |
| Kimi K3 — transition sweep | 5/8 | 42/56 (75.0%) | 40/43 (93.0%) |

The **transition sweep** met the exploratory coverage-and-accuracy criterion for DeepSeek and GLM.
DeepSeek gained three correct item-and-boundary hits, with conditional item accuracy up 2.4
percentage points and valid packets unchanged. GLM gained nine hits, conditional accuracy rose
3.5 points, and the number of valid packets stayed at six; it recovered eight hits on one packet
where both its same-run baseline and checklist failed validation. Its schema failures still
prevented two of eight packets from scoring.

The agenda checklist did not meet the criterion: it gained one DeepSeek hit but reduced conditional
item accuracy by 10 points, while GLM lost three hits and two valid packets. For Kimi, the transition
sweep gained only one hit but lost two valid packets and reduced conditional item accuracy by 4.6
points; its checklist performed substantially worse. Kimi therefore shows the same underlying
high-conditional-accuracy, lower-coverage pattern as the earlier evaluation, but these prompt edits
are not a good fix for it.

This screen used only eight meeting clusters from the already-spent, unblinded v3 test split. It is
exploratory, not a fresh validation or an admission decision. The requests were sequential on each
exact paused route, but other routes on the same providers remained active, so elapsed times are
descriptive rather than latency benchmarks. Prompt wording also cannot overcome hard context
rejections. After the run, all three exact routes reported zero in-flight calls and no active
pauses. No production prompt or route changed.

The sanitized per-packet matrix, prompt text, and pause verification are in
[`results/2026-09-27-coverage-prompt-test.json`](results/2026-09-27-coverage-prompt-test.json).
Raw requests and provider replies remain private under
`/private/tmp/chapter-locator-telemetry/locator-coverage-prompt-test-20260927T171142Z/`; they can
include model reasoning and are not copied into the repository.

### What remains before treating the matrix as an admission test

- **Freeze the admission bar.** The earlier under-5% wrong-publication target was not approved.
  Define acceptable item and boundary error, minimum recall, abstention behavior, and schema-repair
  thresholds against the human-reviewed labels. Report uncertainty by meeting cluster; the 86
  eligible chapters come from only 16 meetings.
- **Add eligible meetings and a fresh holdout.** Only 16/96 test episodes had agenda inputs.
  Include enough long meetings and both providers, then keep a new cohort untouched after choosing
  prompts and routes.
- **Reconcile with the historical human set.** Re-score or rerun its packets under the current
  prompt, schema, and scoring contract if those exact inputs can be recovered. This is the clean
  bridge to Gemini's remembered 115/116 result.
- **Measure sustained capacity and context admission.** The max-duration projections above are
  not load tests. Probe tokenizer use on actual packets, fix the Step/GLM under-admission estimates,
  and test concurrency/TPM pacing while the competing worker is paused.
- **Harden the runner before simplifying it.** Save every request and provider attempt, including
  malformed first replies and full streaming bodies, along with request IDs, usage tokens, retries,
  and per-attempt timing. Then make the runner manifest-driven so model lists, packet hashes,
  scoring artifacts, and route caps are explicit and reproducible.

The sanitized aggregate is
[`results/2026-09-26-test-route-matrix.json`](results/2026-09-26-test-route-matrix.json).
Full request/response telemetry remains under `/private/tmp/chapter-locator-telemetry/`; it is not
copied into the repository. The DeepSeek V4.1 diagnostic SSE logs and raw replies are preserved in
`nvidia-v41-debug-20260926/` and `nvidia-v41-debug-20260926-attempt2/` there. The matrix runner
kept final successful replies but did not retain every malformed first-attempt body; preserving
those is part of the runner work above.

## Full-packet feasibility and chunking

Packets retain the full timed transcript; no item locations are assumed in advance. In the paired
test matrix, Gemini 3.5 Flash Lite, Nemotron Super, Kimi K3, and DeepSeek V4.1 all returned a
structured result on the largest 188,657-estimate packet. DeepSeek V4.1 also returned valid results
on every other packet, though all 16 required repair. Step 3.7 and GLM 4.7 explicitly rejected the
largest packet. Gemini's conservative scheduler gate still excludes two packets even though direct
requests accepted them. These 16 calls show acceptance for this cohort, not reliability at larger
sizes or sustained load.

The earlier DeepSeek V4.1 diagnostic on one small development packet returned 502 after 1,103.76
seconds on its first try; the later 16-packet matrix succeeded after the route pause and repair
behavior were applied. Keep the diagnostic and matrix as separate evidence: the former preserves
raw streaming details, while the latter gives paired quality and latency measurements.

If full-packet routes remain unreliable, chunking can be tested without knowing transition
locations: send overlapping windows of timed transcript units, include the relevant agenda
candidates in each window, map proposed anchors back to global unit IDs, then deduplicate and
reconcile them in a second pass. Overlap helps preserve boundaries at window edges, but repeated
agenda items and discussion revisits require global reconciliation. Compare chunked and full-packet
variants on the same development meetings for retrieval recall, locator quality, and extra calls;
do not use the test split to tune window size or merge rules.

## Earlier candidate screen (before the full test matrix)

The screen below records the state before the 16-packet matrix above. Its quality and capacity
judgments for Gemini 3.5, Nemotron Super, Step 3.7, DeepSeek V4.1, Kimi K3, and GLM 4.7 are
superseded by the paired results. Rows for DeepSeek V4 Flash and GLM 5.3 remain useful as older,
separate route evidence.

Artificial Analysis (AA) is a general benchmark index, not a locator score. Lower-index candidates
are preferred only after they meet the task's quality and reliability bar. Capacity below is the
configured or observed per-model allowance after subtracting exact chapter-agenda use. Chapter
agenda currently uses Nemotron 3 Ultra, Tencent Hy3, and Gemini 3.1 Flash Lite; none of the
candidates below is that exact model, so the exact-model subtraction is zero. Shared provider
concurrency can still slow agenda and locator work at the same time.

| Candidate route | AA | Per-model capacity after chapter-agenda | Locator evidence | Assessment |
|---|---:|---:|---|---|
| Nemotron 3 Super 120B A12B — OpenRouter | 13 | 200/day | 70.0% item proxy on 5/6 core; 1/2 long packets completed | Best low-AA overflow candidate measured here, but not close enough or reliable enough to admit. OpenRouter concurrency is shared with agenda Ultra. |
| Step 3.7 Flash — Kilo | 19 | 200/day | 0/6 schema-valid | Do not route under the current structured-output contract. Kilo also carries agenda Ultra. |
| Gemini 3.5 Flash Lite — Google | 22 | 1,000/day (500/day per key) | Historical human slice: 115/116 correct + valid; v3 run 3/6 completed | Keep as primary. Smooth TPM pacing and repair the duplicate-unit failure before interpreting this v3 run. |
| DeepSeek V4 Flash — OrcaRouter | 34 | 800/day | 55.0% item proxy on 5/6; largest core packet rejected | Poorer proxy than Gemini and the free route does not cover this common packet set. |
| DeepSeek V4.1 Flash — NVIDIA | 39.5 | 4 rpm, concurrency 1; daily capacity unmeasured | Context probe; first quality call returned 502 after 1,104 seconds | Long-context candidate with prompt-only JSON support, but this generation route is currently too slow and unreliable to recommend. NVIDIA concurrency is shared. |
| GLM 5.3 Flash — OrcaRouter | 42 | 800/day | 61.1% item proxy on 3/6; 3/6 failed | Not a reliable overflow choice on this route; slow and prompt-size constrained. |
| Kimi K3 — NVIDIA | 44 (max effort) | ~146/day observed at one slot | Context probe only; not quality-scored | Large measured context, but observed latency is about 590 seconds on a large packet and capacity is below a 1:1 high-volume target. |

Capacity is not yet balanced between the two lanes: the current worker budget allows up to 1,550
chapter-agenda jobs/day (9,300 daily write units / 6 per job), versus 465 chapter-locator jobs/day
(3,255 / 7). These are dispatch ingress ceilings, not measured demand. No provider quota change
would raise the locator lane cap by itself.

The previous candidate report remains useful for older models: Gemini 3.1 Flash Lite is part of
the chapter-agenda pool, so its actual remaining capacity cannot be isolated from agenda use. GLM
4.7 results do not predict GLM 5.3 quality. Other task lanes share some provider/model pools; their
usage was not deducted because this comparison is specifically against chapter-agenda.

## Earlier locator scores

### 16-meeting human-adjudicated slice

This is a separate historical slice, not dataset v3. The same 16 meetings went to each model;
proposals overlap, so the samples are not independent. `Correct + valid` requires both the agenda
item and boundary to pass human review.

| Model | Proposals | Correct + valid | Non-admitted |
|---|---:|---:|---:|
| DeepSeek V4 Flash | 119 | 111 | 8 (6.7%) |
| Gemini 3.1 Flash Lite | 81 | 80 | 1 (1.2%) |
| Gemini 3.5 Flash Lite | 116 | 115 | 1 (0.9%) |
| Z.AI GLM-4.7 Flash | 51 | 47 | 4 (7.8%) |

The Gemini 3.5 invalid boundary had the same high self-reported confidence signature as valid
proposals, so confidence is not a safe publication cutoff. The Austin one-meeting checkpoint also
found DeepSeek V4 Flash at 5/7 full context and 6/7 with pooled hints; Gemini 3.5 and Gemini 3.6
each scored 6/7 with hints. Those seven-marker results use different prompts and packets and are
not comparable with the human slice or v3.

## Production routing today

The `chapter-locator` lane uses the baseline prompt with size-based routing: DeepSeek V4 Flash
for packets up to 76,000 internally estimated input tokens, and Kimi K3 above that threshold.
GLM 5.3 Flash is documented as additional small-packet capacity and DeepSeek V4.1 Flash as
additional large-packet capacity; neither is wired into this lane. Nemotron Super and Step 3.7
were tested here but are not assigned to the lane. The `chapter-agenda` pool is Nemotron 3 Ultra, Tencent Hy3, and Gemini 3.1
Flash Lite, with Hy3 and Gemini 3.1 as backups after 12 attempts.

## Artifact layout

- `dataset-v3/manifest.json`: input-only frozen cohort and split.
- `dataset-v3/gold.json`: provider chapters and timestamps, scoring-only.
- `dataset-v3/crosswalk.json`: automated generated-agenda/provider-chapter mapping diagnostics.
- `dataset-v3/human-crosswalk-v1.json`: timing-blinded, manually adjudicated test mappings.
- `results/2026-09-26-development.json`: sanitized development scores; no raw replies, quotes, or
  meeting identifiers.
- `results/2026-09-26-context-probes.json`: sanitized live context probe measurements.
- `results/2026-09-26-test-route-matrix.json`: sanitized paired scores, context fit, AA indices,
  and route-capacity evidence for the six candidate routes.
- `results/2026-09-26-human-crosswalk-rescore.json`: route outputs rescored against the human
  crosswalk, with meeting-cluster uncertainty intervals.
- `results/2026-09-27-prompt-schema-ablation.json`: sanitized development-only paired prompt
  results for Gemini 3.5 Flash Lite, GLM 5.3 Flash, and DeepSeek V4 Flash.
- `results/2026-09-27-coverage-prompt-test.json`: sanitized, human-crosswalk-scored prompt
  comparison for DeepSeek V4 Flash, GLM 5.3 Flash, and Kimi K3 on eight previously used test
  meetings.

Keep raw requests, model replies, credentials, and reviewer identities in private storage. Store
manual adjudications in versioned scoring artifacts. Since this test split is now unblinded,
validation after route or prompt tuning requires a new untouched cohort.

## Large-context overflow holdout: 2026-09-27

Holdout-v1 was frozen before any locator calls. It contains 23 eligible packets from 24 selected
episodes; one episode failed upstream agenda validation. The frozen agendas came from the
production Gemini 3.1 Flash Lite extractor. The packet-size estimates are P50 72,089, P90 124,134,
P95 177,306, and max 191,294. Selection deliberately enriched large packets, so these are not
fleet context-fit percentages. Provider chapters were held out from every model request.

The automated score is one-to-one provider-start recall within 60 seconds. Failed or invalid
responses count as zero against the fixed target denominator. The separate manual check reviewed
one timing-blinded provider chapter per episode against generated agenda candidates: 18 matched,
one ambiguous, and four unmatched. It is a crosswalk diagnostic, not full manual locator gold.

| Model / scope | Valid | Start hits | Manual timing; exact | Note |
|---|---:|---:|---:|---|
| Gemini 3.5 / all 23 | 14/23 | 72/176 (40.9%) | 14/18; 8/18 | Schema errors; one TPM rejection. |
| Kimi K3 / five large | 5/5 | 37/49 (75.5%) | 3/4; 0/4 | Manual check conflicts with prior data. |
| DeepSeek V4.1 / 191k | 1/1 repaired | 0/1 | No matched target | One miss; 177k call hung. |

Do not use Gemini's conditional score among successful packets as its overall quality: the table
keeps failed packets in the denominator. Its exact item-and-boundary score on the 18 audited
crosswalk-matched cases was 8/18, or 8/14 conditional on a timing hit. On the four matched audit
cases above Gemini's conservative gate, it reached 2/4 timing and 1/4 exact item plus boundary.
That small subset should not be generalized to all large meetings.

### Keep DeepSeek V4.1's paired evidence in view

The relevant quality comparison is still the paired eight-packet development set, where the model
saw identical frozen agendas, transcripts, and prompt. With provider-default thinking, V4.1 was
valid on 8/8 packets and found 37/39 provider starts. Kimi's development-selected `transition_sweep`
prompt was valid on 6/8 and found 32/39 on those same packets. V4.1 was also much slower (718s
mean versus Kimi's 70s mean in that prompt screen). This supports V4.1 as the stronger quality
candidate on development; the one fresh 191k holdout packet that V4.1 missed is a warning to
investigate, not grounds to reverse that result. The fresh 177k request produced no response after
about 40 minutes. The client was still waiting for HTTP response headers despite a configured
1,200-second timeout, so it was interrupted and is not quality-scored. The route pause was
resumed and live status confirmed no paused route and zero in-flight calls. This transport hang
needs debugging independently of the model's quality.

Historical human adjudication and this automated crosswalk must remain separate. The earlier
16-meeting manual slice measured 59/86 Kimi exact item-and-boundary proposals and 56/60 correct
items conditional on finding a start. The current holdout audit is only one sampled chapter per
episode and shows a discrepancy for Kimi at large context, not a replacement estimate. A larger
paired, manually adjudicated long-context sample is needed before ranking V4.1 and Kimi for the
overflow band.

### Context bands and capacity

Live probes, not advertised context windows, set the current evidence bounds. DeepSeek V4 Flash on
Orca succeeded through about 106k estimated units and first rejected around 109k. Gemini 3.5 has
a conservative dispatch gate near 101.8k estimated units, but one 124k real packet succeeded with
223,676 provider prompt tokens; another 177k packet hit Google's 250k input-token-per-minute
limit. This is a TPM/pacing issue, not confirmation of Gemini's model context ceiling. DeepSeek
V4.1 succeeded on a synthetic probe at 562,750 estimated units; the first errors above 656,375
leave the exact maximum unknown. Kimi succeeded on synthetic input at 747,074 estimated units,
and the holdout includes valid real packets through 191,294. Its scheduler cap is 234,437, below
that synthetic success, and has not been changed.

The user's candidate bands remain hypotheses, not production cutoffs:

- DeepSeek V4 can provide extra capacity for small packets, up to its measured free-route band.
- Gemini 3.5 can overlap that band and may handle packets above its conservative gate when
  provider tokenization and TPM allow; it should not be treated as guaranteed for every packet of
  that estimate size.
- DeepSeek V4.1 remains the best-supported long-context quality overflow candidate from the
  paired development data.
- Kimi K3 is a possible higher-context overflow or capacity alternate; its large-context quality
  needs a larger adjudicated comparison against V4.1.

Gemini's configured quota is 1,000 requests/day across two 500/day keys. V4.1 and Kimi are
configured at 4 RPM with route concurrency 1; neither has a published daily quota in the current
records. Their NVIDIA concurrency is shared with Nemotron Ultra agenda work and other lanes, so
remaining daily capacity is unmeasured. AA is a general benchmark index and does not establish
locator quality. No route assignment changed from this exploratory evaluation.

### Chunking screen

The development-only chunking test repeated the complete agenda in chronological transcript
windows. On two large packets, earliest-start merging recovered 1/2 and 0/2 provider starts. A
confidence-based offline merge recovered 2/2 on one packet only, with no validated confidence
threshold. Chunking avoided the measured Google TPM rejection on those calls but did not
reliably improve locator coverage. Do not make it a production fallback until a larger development
set establishes a robust merge rule.

See the sanitized [overflow route holdout results](results/2026-09-27-overflow-route-holdout.json)
for scores, capacity evidence, and limitations; raw requests and responses, including
reasoning text, remain in private telemetry. See
[`results/2026-09-27-chunking-development.json`](results/2026-09-27-chunking-development.json)
for the packet-level chunking results.
