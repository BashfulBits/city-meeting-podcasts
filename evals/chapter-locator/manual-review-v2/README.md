# Chapter locator manual boundary review

This local review cohort contains every provider chapter boundary in 39 meetings:

- Eight development meetings from the already-reviewed locator comparison.
- Eight additional development meetings used for DeepSeek V4.1.
- All 23 meetings in the frozen locator holdout.

The packet contains 381 boundary cases. A median response time is prefilled for 252 cases;
the remaining cases have no confidently matched response to use as a suggestion. Existing human
labels were carried forward, including the composite marker for `P-1BB8ED7`.

## Run the review page

From the repository root:

```bash
python evals/chapter-locator/manual-review-v2/server.py
```

Open <http://127.0.0.1:8768>. The page writes confirmed times, agenda-item associations, concurrent
chapter groups, transcript-evidence status, and importance labels to `scores.json`. Model and route
details are kept in `answer-key.json`, which the server does not expose to the scoring page.
Each meeting entry links directly to its provider-supplied agenda document when available.

Do not open `index.html` directly as a `file://` URL; the page needs the local server APIs to load
the packet and saved scores. If data loading fails, it now displays a message with the server link
and startup command instead of leaving the review panel blank.

Click **Save selected time** to accept the median suggestion, or click a transcript word to set and
save a different time. Use the always-open **Transition contents** section to mark the agenda items
at this transition and select any other published chapter rows that share its time. Agenda items
identify meeting topics; chapter rows identify provider timestamps. Linked chapters share the saved
transition time and combined agenda-item selection. Keyboard shortcuts:
left/right arrows move between boundaries; `c`, `o`, and `u` set core, optional, and unsure.

The separate flow-importance field can mark a boundary as core to understanding, optional to omit,
or unsure with a single radio-button click. Unassessed and unsure labels remain distinct in the
exported scores. If a provider chapter has no corresponding spoken content, use **No spoken
segment · exclude**. If the transcript has no coverage near its timestamp, use **Transcript gap ·
unscorable**. Both clear any transition time and are excluded from timing and recall scoring; a
transcript gap does not imply that the discussion never happened. These differ from an optional
spoken transition, which remains scoreable.

## Rebuild the packet

`prepare_cohort.py` reconstructs the packet from the frozen locator manifests and existing private
run artifacts under `/private/tmp/chapter-locator-telemetry`. It caches downloaded word transcripts
in that telemetry directory rather than adding them to this cohort directory. Existing saved labels
are carried forward by stable case ID.

The median suggestion is calculated in two steps: median the matching anchors across a model's
available prompt variants, then take the median across model medians. This prevents models with more
experimental prompt variants from receiving extra weight. The answer key retains the response
validity, route, prompt, elapsed time, and returned anchors needed to score each route after the
manual labels are complete.

The eight V4.1 development meetings use the existing automated chapter-to-agenda crosswalk. The
earlier eight development meetings have a human-reviewed crosswalk; only the audited target chapter
in each holdout meeting has a human-reviewed crosswalk. Those 23 holdout packets were already sent
to locator routes during exploratory evaluation, so they are not an untouched final holdout. Review
bundle selections where a published chapter combines agenda items or its automatic mapping is
unclear.

## Rescore saved model responses

`score_model_arms.py` scores each saved routed response against the manually selected times and
agenda-item bundles. It folds explicitly linked concurrent chapter cases into one target, applies
a one-to-one 60-second match, and reports boundary-only timing separately from item-plus-boundary
recall. Invalid and missing responses are **unknown for answer quality**: the scorer reports
conditional quality on valid responses, its meeting-cluster interval, and an optimistic bound for
the full cohort. The full-cohort lower bound treats invalid or missing outputs as no hits; use that
only as an end-to-end route-yield view. Targets without a manual agenda association are omitted
only from item-plus-boundary denominators. Recall intervals resample whole meetings, preserving
correlation among chapters from the same meeting.
The score file also reports paired prompt comparisons on the same attempted meetings and the
subset valid under both prompts. The current route/prompt snapshot, including measured request-size
distribution and live route failures, is the
[matrix snapshot](../results/2026-09-29-manual-review-v2-matrix.json).

For example, rescore Kimi K3's transition-sweep arm:

```bash
python evals/chapter-locator/manual-review-v2/score_model_arms.py \
  --model moonshotai/kimi-k3 \
  --prompt transition_sweep \
  --gap-fill-run \
  /private/tmp/chapter-locator-telemetry/locator-kimi-k3-manual-v2-gapfill-20260929T061813Z \
  --write evals/chapter-locator/results/2026-09-29-kimi-k3-manual-review-v2.json
```

The Kimi result JSON includes the complete-cohort score and split breakdown. Raw requests and
responses, which can include model reasoning, remain in the private telemetry directory recorded
in that result file.

## Run another model arm

`run_model_arm.py` sends full, untruncated packets from this cohort directly to a configured model
route, with one schema-repair attempt. Before sending requests, check the latest live context/size
probe and select only meetings eligible for that route. The script pauses the provider's v2 claims,
waits for in-flight jobs to drain, reserves each direct request, captures raw requests/responses in
private telemetry, and resumes the provider on exit. Raw responses can contain private reasoning
text and are not written into the repository.

```bash
source /usr/local/bin/citypods-env
python evals/chapter-locator/manual-review-v2/run_model_arm.py \
  --model gemini/gemini-3.5-flash-lite \
  --prompt transition_sweep \
  --uid <meeting-uid> --uid <meeting-uid>
```

Each successful run prints its private telemetry directory. After reviewing its metadata and pause
outcome, merge saved `results.json` files into the answer key and rescore:

```bash
python evals/chapter-locator/manual-review-v2/merge_model_arms.py \
  /private/tmp/chapter-locator-telemetry/<run-dir>/results.json
python evals/chapter-locator/manual-review-v2/score_model_arms.py
```

## Final model comparison and routing recommendation (2026-10-02)

The current complete rescore is documented in the
[Kimi retry rescore](../results/2026-10-02-manual-review-v2-kimi-retry-rescore.json).
It covers 39 meetings and 381 boundary cases. Quality is item-plus-timing recall at a 60-second
tolerance. The conditional score uses
valid responses only; the full-cohort score counts invalid, missing, and context-too-large meetings
as misses. Every arm now has an outcome for all 39 meetings: valid or explicitly classified as too
large. The two columns keep answer quality separate from route coverage.

| Route/model | AA | Valid | Too large | Conditional | Full cohort | Sweep mean / median s |
|---|---:|---:|---:|---:|---:|---:|
| Gemini 3.5 Lite / Google | 22 | 39/39 | 0 | 84.1% / 84.4% | 84.1% / 84.4% | 28.7 / 8.5 |
| DeepSeek V4 Flash / Orca | 34 | 27/39 | 12 | 93.0% / 91.4% | 59.0% / 58.0% | 114.3 / 106.0 |
| DeepSeek V4.1 Flash / NVIDIA | 39 | 39/39 | 0 | 91.2% / 89.5% | 91.2% / 89.5% | 944.4 / 540.0 |
| GLM 5.3 Flash / Orca | 42 | 27/39 | 12 | 92.0% / 87.7% | 58.3% / 55.6% | 251.3 / 162.9 |
| Kimi K3 / NVIDIA | 44 | 39/39 | 0 | 91.2% / 88.8% | 91.2% / 88.8% | 87.6 / 78.4 |

Paired entries in the two quality columns are baseline / transition-sweep. The same ordering
applies to full-cohort scores and transition-sweep latency is shown.

The 12 DeepSeek V4 Flash and GLM 5.3 Flash cases per arm are confirmed too-large outcomes after
repeated explicit context-limit failures. They are not successful model answers; the conditional
quality score describes only the 27 meetings the route could process. The full-cohort score is the
route's end-to-end result on all 39. Gemini results from saved baseline and transition retry runs,
and 12 saved DeepSeek V4 Flash baseline results, were merged before this rescore. Existing valid
answer-key rows were preserved.

The AA column is the current Artificial Analysis Intelligence Index, a general model benchmark,
not a locator score. Lower AA means lower general benchmark capability; use it only as a tie-breaker
after the model meets the locator quality and reliability bar. Current references:
[Gemini 3.5 Flash Lite](https://artificialanalysis.ai/models/gemini-3-5-flash-lite),
[DeepSeek V4 Flash 0731](https://artificialanalysis.ai/models/deepseek-v4-flash),
[DeepSeek V4.1 Flash (Max)](https://artificialanalysis.ai/models/deepseek-v4-1-flash),
[GLM 5.3 Flash](https://artificialanalysis.ai/models/glm-5-3-flash),
and [Kimi K3 (Max)](https://artificialanalysis.ai/models/kimi-k3).

### Approximate daily capacity

These are planning estimates for validated answers per day, not observed 24-hour throughput or
provider guarantees. They apply the repository's configured request/day cap where present, the
configured route concurrency, the transition-sweep mean latency, and the observed valid fraction
among attempted meetings. They assume a similar packet mix and no additional retry traffic. Context
rejections, shared provider load, and repeated repairs reduce the usable rate.

| Route | Configured cap | Estimated attempts/day | Approx. valid/day | Constraint |
|---|---:|---:|---:|---|
| Gemini 3.5 Lite | 1,000/day | >5,500 | ~795 | 39/39 recovered; retries consume the cap |
| DeepSeek V4 Flash | 800/day | ~1,510 | ~635 | 12/39 too large; use only within the fit band |
| GLM 5.3 Flash | 800/day | ~687 | ~476 | 12/39 too large; slower than Gemini |
| DeepSeek V4.1 Flash | No RPD; 4 RPM | ~91 | ~90 | Slow; shared NVIDIA capacity and timeouts |
| Kimi K3 | No RPD; 4 RPM | ~855–986 | ~850–950 | TPM setting uncertain; retry cost |

The V4.1 baseline result was recovered from the September 30 telemetry run and merged before this
rescore. That run has 38 valid meeting results from 92 attempts; the remaining meeting already had
a valid saved answer. The baseline scores 39/39 valid at 91.2% item-plus-timing recall. Its
multi-retry outlier makes the baseline's mean elapsed time unsuitable as a per-call latency
estimate; the table reports the transition-sweep latency instead.

### Matched 27-meeting comparison

To compare answer quality without a context-coverage penalty, this second score uses the exact 27
meetings where both DeepSeek V4 Flash and GLM 5.3 Flash returned valid answers under both prompts.
All five models have valid answers for these same meetings. This excludes the too-large meetings
from every model's denominator; it measures quality on the shared fit cohort, not end-to-end route
yield. The reproducible scores are in the
[matched-cohort score file](../results/2026-10-02-manual-review-v2-small-context-cohort.json).

The rows below separate chapter coverage from timing accuracy. “Assigned” counts chapters matched
to the correct manual agenda item, regardless of time, out of 187 gold chapters. “Within 60s” is
timing accuracy among those assigned chapters. “Joint” is the combined correct-item-and-time recall
across all 187 chapters.

| Baseline model | AA | Assigned | Within 60s | Joint |
|---|---:|---:|---:|---:|
| Gemini 3.5 Lite | 22 | 181/187 (96.8%) | 169/181 (93.4%) | 90.4% |
| DeepSeek V4 Flash | 34 | 177/187 (94.7%) | 174/177 (98.3%) | 93.0% |
| DeepSeek V4.1 Flash | 39 | 175/187 (93.6%) | 172/175 (98.3%) | 92.0% |
| GLM 5.3 Flash | 42 | 176/187 (94.1%) | 172/176 (97.7%) | 92.0% |
| Kimi K3 | 44 | 175/187 (93.6%) | 171/175 (97.7%) | 91.4% |

| Transition-sweep model | AA | Assigned | Within 60s | Joint |
|---|---:|---:|---:|---:|
| Gemini 3.5 Lite | 22 | 175/187 (93.6%) | 159/175 (90.9%) | 85.0% |
| DeepSeek V4 Flash | 34 | 176/187 (94.1%) | 171/176 (97.2%) | 91.4% |
| DeepSeek V4.1 Flash | 39 | 175/187 (93.6%) | 172/175 (98.3%) | 92.0% |
| GLM 5.3 Flash | 42 | 171/187 (91.4%) | 164/171 (95.9%) | 87.7% |
| Kimi K3 | 44 | 172/187 (92.0%) | 167/172 (97.1%) | 89.3% |

On this shared cohort, baseline has higher chapter-assignment coverage for Gemini, DeepSeek V4
Flash, GLM, and Kimi; V4.1 is tied. Timing accuracy among chapters assigned is high for every
model. Gemini's main weakness on transition-sweep is lower coverage and lower timing accuracy; its
baseline recovers more chapters and scores better on both measures.

### Long-context comparison on 12 meetings

This is the complementary set of 12 meetings that neither DeepSeek V4 Flash nor GLM 5.3 Flash
could process under either prompt. Gemini, V4.1, and Kimi all returned valid responses for all 12.
The 108 gold chapters below have manual agenda associations.

| Model | Prompt | Assigned | Within 60s | Joint | Mean / median s |
|---|---|---:|---:|---:|---:|
| Gemini 3.5 Lite | Baseline | 95/108 (88.0%) | 79/95 (83.2%) | 73.1% | 90.9 / 77.0 |
| Gemini 3.5 Lite | Transition-sweep | 98/108 (90.7%) | 90/98 (91.8%) | 83.3% | 50.9 / 51.6 |
| DeepSeek V4.1 Flash | Baseline | 99/108 (91.7%) | 97/99 (98.0%) | 89.8% | 569.1 / 502.2 |
| DeepSeek V4.1 Flash | Transition-sweep | 95/108 (88.0%) | 92/95 (96.8%) | 85.2% | 911.1 / 949.3 |
| Kimi K3 | Baseline | 99/108 (91.7%) | 98/99 (99.0%) | 90.7% | 91.1 / 90.2 |
| Kimi K3 | Transition-sweep | 98/108 (90.7%) | 95/98 (96.9%) | 88.0% | 101.0 / 98.2 |

Kimi baseline has the highest joint recall on this 12-meeting slice (90.7%), narrowly ahead of
V4.1 baseline (89.8%) and with far lower latency. Gemini transition-sweep improves on its baseline
here (83.3% versus 73.1%) and is much faster, though its quality remains lower. Treat this result
as directional: it is based on 12 meetings and should not replace the larger-cohort comparison.
The detailed scores are in the
[long-context score file](../results/2026-10-02-manual-review-v2-large-context-cohort.json).

For the NVIDIA routes, “attempts/day” is the one-slot latency estimate, not an RPD quota. Their
configured 4 RPM is only a ceiling; observed latency is much slower. Kimi's 39/39 valid result
includes retry recovery. Its estimate combines a 986/day latency ceiling with a 60.6k mean
provider prompt-token count from seven recent transition-sweep responses. The compiled route says
36k TPM while `provider_limits.yml` says 40k, and neither value is a measured provider quota, so
the resulting ~850–950/day range is especially rough. V4.1's 39/39 valid result does not remove the
operational risk from its 944-second mean response time. The context-gated V4 Flash estimate must
not be read as 635 jobs available for arbitrary packet sizes.

### Recommendation

Use the baseline prompt as the default candidate. On the 27-meeting shared cohort, baseline
matches or beats transition-sweep for every model. For small and medium packets within the measured
fit band, use DeepSeek V4 Flash with baseline when quality is the priority: it scored 93.0% on the
shared cohort and has AA 34. Route packets at or below 76,000 internal estimated input tokens to V4 Flash; route larger packets to Kimi K3. Saved admission evidence shows accepted V4 Flash packets through 76,546 and verified context rejections beginning at 81,301, leaving a measured uncertainty band that the 76,000 cutoff avoids.

Gemini 3.5 Flash Lite is a fast, lower-AA alternative or fallback for smaller packets: it returned
valid responses for all 39 meetings, scored 90.4% on the shared fit cohort, and has AA 22. Gemini
also processed the long meetings, but its joint recall there was lower (73.1% baseline and 83.3%
transition-sweep), so it is not the quality-first overflow choice. Its configured hard estimate
ceiling is 218,875; the safer fit estimate is about 101,800 because Google charges actual prompt
tokens against TPM. Do not treat either estimate as a universal context window.

Use Kimi K3 with baseline for overflow: it returned valid responses for all 39 meetings, scored
90.7% on the 12 long meetings, and had about 90 seconds median latency on that slice. Its whole-set
baseline score was 91.2%. DeepSeek V4.1 Flash remains a viable alternative (89.8% on the long slice,
91.2% on the full set), but its median latency on the long slice was about 502 seconds. GLM 5.3
Flash is not a default-path candidate: it shares the 12/39 too-large outcomes with V4 Flash, has
slightly lower fit-cohort baseline quality (92.0% versus 93.0%), and is slower. Keep it only as an
optional fallback if operational experience shows a route-availability benefit.

This ordering applies the “best locator quality at the lowest AA index that meets the lane bar”
strategy, but the medium-route quality bar is not yet formally approved. The 39-meeting set has also
been used for prompt and routing selection, so confirm the cutoffs on a new unseen cohort before
calling them production-calibrated.

### Findings for the production prompt and retry policy

Carry these observations into the production prompt-policy review after the baseline-versus-
transition-sweep decision:

1. **Keep the structured answer in the assistant content field.** Two Kimi meetings repeatedly
   returned HTTP-success responses with `content: null` and only 32 punctuation tokens in
   `reasoning_content`. A temperature/seed change alone did not recover them. A short system-level
   reminder to put the required JSON in the answer content, with a new seed, produced valid JSON.
   Treat this as a response-format reminder, not evidence of a refusal or context limit; retain the
   bounded retry and rotate to the next meeting after it fails again.
2. **Repair duplicate chapter anchors from the collision evidence.** When validation finds two
   distinct chapter IDs at the same start, retry only the conflicting anchors. Keep separate
   anchors only when the transcript supports distinct starts; otherwise prefer the strongest,
   most specific chapter or omit the general section heading. Never invent a time offset. This
   remains a focused retry rule rather than a hard-coded rule for the full prompt.

The first behavior was tested on two affected meetings only, and the second is based on rare
validation failures. Preserve both as targeted recovery guidance and verify them on future cases
before making them broad prompt instructions.
