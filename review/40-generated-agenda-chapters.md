# Generated agenda chapters (GH#1078)

**Status: L2 designed · 2026-07-27.** This is an empirical fallback for episodes where a provider
does not supply chapter markers. It does not replace canonical provider chapters.

## Evidence and scope

The chapter-prevalence audit over the supplied production snapshot found 239 broad candidates with
canonical chapters, audio, transcript, agenda text, and word timing (231 Granicus and 8 Swagit).
The implemented strict selector additionally requires the active transcript to be marked synced;
it yields 237 candidates (229 Granicus and 8 Swagit). Gainesville's CivicPlus/CivicMedia chain has no public chapter data: its
RSS carries only the upload timestamp and link; the CivicMedia/TikiLive pages expose HLS and VTT
captions but no markers or usable chapter API; its CivicEngage companion has agenda/minutes
documents, not timings. Empty Granicus and Swagit samples were also verified upstream, rather than
assumed to be scraper gaps.

The first consumer is therefore CivicPlus episodes with a persisted agenda and transcript. The
benchmark must establish useful accuracy on provider-supplied chapters before the fallback can
materialize any public output.

### Independent title-candidate probe (2026-07-27)

The first read-only structural probe used only persisted **main** agenda-text artifacts and a
one-to-one, high-similarity (>= 0.80) comparison to hidden canonical provider titles. It did not
fetch backups or make a model call. It found that title candidates have useful lexical coverage but
are not a title-creation mechanism by themselves:

| Provider / episode | Canonical titles | Structural candidates | High-confidence matches |
| --- | ---: | ---: | ---: |
| Denton Granicus `857307f158451487` | 22 | 46 | 22 (100%) |
| Denton Granicus `cb75dc5c0546b0f7` | 3 | 9 | 3 (100%; mean 0.974) |
| Austin Swagit `e3a8875ae0aee2ec` | 7 | 34 | 6 (85.7%) |
| Austin Swagit `15f613dc69da16fd` | 5 | 24 | 4 (80.0%) |

The count gaps (roughly 2x--5x) show that a deterministic parser would create too many chapters,
even where its candidates lexically contain most provider labels. Austin also leaves one canonical
title unmatched in each sample. The extractor remains an auditable, line-evidenced candidate source
and benchmark probe only. Before any locator run, a separate title-selection/equivalence step must
select or consolidate candidates and be admitted using the same held-out canonical comparison; it
must not invent untraceable titles or treat every agenda item as discussed.

### Format-aware outline probe (2026-07-27)

The plain persisted artifact intentionally has no original PDF/HTML presentation semantics, but
the provider's original agenda URL is retained. A no-new-dependency experiment used the already
pinned `pypdf` layout mode for PDFs and converted only semantic HTML headings (plus Granicus
`Agenda`-class item links) into a small Markdown-like outline. It never infers headings merely from
bold text.

This materially improved Denton's semantic HTML **deterministic candidate baseline**: the same
2026-07-07 episode produced 22 outline headings for 22 canonical titles, all matched at the >=0.80
one-to-one threshold, versus 46 candidates from the persisted flat text. This is useful
source-format evidence, not a claim about LLM accuracy or a provider-specific production shortcut.
In the checked Arlington PDF fixture, layout extraction preserved visual line/indent spacing but
yielded the same 12 candidates from the current structural parser as plain text; that does not
show whether layout helps an LLM. Layout alone does not reliably create a heading hierarchy. PDF
font/position interpretation would need its own held-out evaluation before use.

#### Docling PDF conversion evaluation (2026-07-28)

A bounded, local-only Docling 2.115.0 trial tested the same two held-out Austin Swagit PDF
agendas and an independently located official Denton County July 7, 2026 PDF. The Denton PDF is
the County Public Notices agenda for the same meeting as held-out Granicus episode
`857307f158451487` (clip 2068); it is not the HTML `AgendaViewer` source already used by that
episode. The comparison used the existing source-backed candidate extractor and hidden canonical
provider chapter titles. No project dependency, artifact, or episode record changed.

| Episode / representation | Candidates | Canonical matches |
| --- | ---: | ---: |
| Austin `e3a8875ae0aee2ec` durable plain / `pypdf` layout | 34 / 34 | 6/7 (85.7%) / 6/7 |
| Austin `e3a8875ae0aee2ec` raw Docling Markdown | 39 | 6/7 (85.7%) |
| Austin `a768721726d45435` durable plain / `pypdf` layout | 40 / 40 | 6/6 (100%) / 6/6 |
| Austin `a768721726d45435` raw Docling Markdown | 51 | 6/6 (100%) |
| Denton `857307f158451487` official PDF `pypdf` layout | 46 | 22/22 (100%) |
| Denton `857307f158451487` raw Docling Markdown | 38 | 10/22 (45.5%) |
| Denton `857307f158451487` Docling, evaluation-only item-number reorder | 48 | 20/22 (90.9%) |
| Denton `857307f158451487` semantic HTML outline | 22 | 22/22 (100%) |

The raw Denton loss was chiefly an interchange mismatch, not absent source words: Docling emitted
many numbered items as Markdown bullets ending in their visible number (for example, title then
`6. A.`), while the existing extractor accepts a leading number. An **evaluation-only** reorder
that moved that exact visible number to the front recovered 20 titles, but still did not beat
`pypdf`; it also created more candidates. The Austin PDFs received no coverage gain and added
5--11 candidates. Docling therefore is not admitted as a production dependency or AgendaTextStage
extractor. Its first local use also required roughly 1.3 GB of environment dependencies plus OCR
and Hugging Face layout-model downloads, an operational cost not justified by these results. A
future representation evaluation could reconsider a narrow Docling adapter only if it improves
held-out PDF coverage beyond the existing extractor; it must compare raw and adapter-normalized
outputs separately.

If this representation is admitted, `AgendaTextStage` should persist it as a **second** bounded
agenda-outline artifact during normal provider fetches, alongside the current full text. Later
stages must read that immutable artifact rather than refetch a provider URL. That is a stage-version
bump with a gradual agenda-artifact backfill story, so it remains a separate approval gate from the
pure extractor and benchmark work here.

### Historical candidate-selection experiment (2026-07-27)

This candidate-gated experiment is retained as a baseline result, but is superseded by the direct,
source-anchored extraction contract below. It demonstrated why a deterministic candidate parser is
not a reliable admission gate for a capable model; it is not the selected title-extraction design.

The next empirical gate is a shadow-only, paired Mistral Large 3
(`mistral/mistral-large-latest`) title-selection run over the same held-out canonical episodes.
Both variants use the same prompt, structured response, model route,
and full main-agenda text:

1. **flat:** candidates extracted from the durable plain agenda text;
2. **format-aware:** candidates extracted from the semantic/layout outline, supplied alongside the
   same plain agenda text.

The `agenda-chapter-title-select` response may return only request-local candidate IDs. Code maps
those IDs back to their exact title, source (`flat` or `outline`), and source line; it rejects
unknown or duplicate IDs. The selector cannot write, rename, merge, or otherwise invent a title.
This makes count, canonical title coverage/similarity, invalid-output rate, and cost/latency a fair
representation comparison. It still does **not** establish that an item was discussed: timing
location remains the later transcript-based gate.

The request builder and validation are intentionally pure so the bounded cohort can run through the
configured execution path without changing a materialized episode. The paired runner must use the
paced dispatch Worker for `mistral/mistral-large-latest`; its local environment supplies the worker
URL/token, while the Worker's own `UPSTREAM_API_KEY` authenticates the Mistral API call.

#### First paired shadow result (2026-07-28)

The first live, read-only paired run used held-out Denton Granicus episode `857307f158451487`.
Both requests completed with valid candidate-ID-only structured output through the paced Mistral
Large 3 alias. The flat representation began with 46 candidates and selected 17; the format-aware
HTML outline began with 22 candidates and selected 15. Each matched 15 of 22 hidden canonical
titles (68.2%; mean matched-title similarity 1.0). Format-aware input therefore reduced candidate
noise on this example, but did **not** improve canonical-title coverage in one sample. This is an
initial calibration observation only; admission requires the planned multi-episode/provider
comparison, including the Austin/PDF-derived contrast.

The first Austin Swagit contrast, `e3a8875ae0aee2ec`, completed on 2026-07-28. Both flat and
format-aware variants began with 34 candidates, selected 19, and matched 5 of 7 hidden canonical
titles (71.4%; mean matched-title similarity 1.0). The format-aware request was materially larger
(10,646 estimated input tokens versus 4,220) without improving candidate count or coverage. This
shows that a representation must be admitted per source format: semantic HTML headings helped
reduce Denton candidate noise, while this Swagit source did not supply an outline with incremental
selection value.

The runner now sends the format-aware variant only when its ordered, normalized source-backed
candidate sequence differs from the flat sequence; a repeated sequence is cost-only duplication.
This is an evidence guard, not an accuracy admission rule: a distinct outline may still perform
worse. The second Denton Granicus contrast, `cb75dc5c0546b0f7`, demonstrates that risk: flat
selected 6 of 9 candidates and matched all 3 canonical titles (100.0%; mean similarity 0.974),
while its smaller distinct outline selected 2 of 3 and matched 2 of 3 (66.7%; mean similarity
0.949). The guard therefore decides whether a paired representation experiment is informative;
the held-out aggregate, not candidate count or prompt size, decides whether any representation is
admitted.

The second Austin Swagit contrast, `a768721726d45435`, also completed on 2026-07-28. Its outline
passed the evidence guard (it was not an exact normalized candidate-sequence duplicate), but both
variants still produced the same operational result: 40 candidates, 24 selected, and 5 of 6 hidden
canonical titles matched (83.3%; mean similarity 1.0). The format-aware request was 10,437
estimated input tokens versus 5,249 for flat. This confirms that the guard prevents only redundant
representations; it does not predict that a distinct representation improves title selection.
Benchmark reporting must retain both facts separately.

### Direct source-anchored agenda-item extraction (2026-07-28)

The selected shadow-only title path gives the model the complete main-agenda source as numbered
lines. It is not constrained by deterministic structural candidates. Each output carries a concise
**generated** title plus all of the following evidence:

1. `source_ref`: an official item ID or visible item number;
2. `line_start` / `line_end`: the exact source span;
3. `evidence_quote`: verbatim source text within that span.

Code rejects unknown fields, duplicate references, out-of-range spans, a reference absent from the
declared span, or a quote absent from it. It tolerates narrowly identified PDF layout artifacts
(spacing around punctuation, repeated page furniture, and split numeric/letter notation), but does
not accept a paraphrase. A title is deliberately not required to be verbatim: it is generated
provenance and cannot be confused with a canonical provider chapter title.

The attached 2026-07-21 Denton City Council PDF was the decisive qualitative probe. Mistral Large
returned 31 action items spanning work sessions, closed session, appointments, consent items,
public hearings, and individual consideration. Thirty had exact validated source evidence after
removing only repeated PDF page furniture. The remaining item had a cited range one line too short;
its content was not accepted merely because it looked plausible.

The first held-out Denton County canonical episode (`857307f158451487`, 22 canonical chapters)
showed the next calibration issue: its PDF-derived text displays composite references on separate
lines (for example `14.` then `B.`). The canonical provider chapters establish that `14.B` is an
actual action-item chapter while standalone `14.` is a parent/section numeral. Therefore requiring
the parent numeral inside the action evidence span would over-tighten the contract. The proposed
one-line backward recovery is **not admitted**. Future schema work must distinguish a fully
qualified display reference from the exact action evidence range, rather than treating section
context as action text.

That same known-good episode shows that lexical title similarity cannot be the quality metric for
generated concise titles: Mistral returned 16 plausible item titles, but only one clears the old
>=0.80 lexical threshold against 22 provider chapter titles, which often reproduce long agenda
prose. The benchmark must separately measure (a) action-item coverage using source evidence and
(b) generated-title quality using a semantic, held-out comparison. Austin and wider canonical
scoring are pending that benchmark revision.

The first revised run implemented those separate metrics: source-valid item count, rejected-evidence
count, canonical action denominator, and a blind one-to-one semantic title judge. On the Denton
County example it returned 17 items but only one passed the current *source-reference* invariant;
the other 16 were rejected before judging, yielding 0/17 semantic action coverage. This is a
calibration failure of the reference requirement, not an Austin/provider result: hierarchical
agendas frequently put a composite display reference in section context while the action evidence
is on the child line. No Austin request should be spent until the contract lets exact action evidence
stand on its own and treats an optional display reference as non-gating provenance.

That revision was then measured on two provider-supplied canonical episodes. Exact evidence quote
and a bounded, uniquely exact nearby-span reconciliation are the extraction gate; the reconciliation
records when it adjusts a model's declared line span. The semantic judge is post-extraction only and
therefore does not leak canonical titles to the extractor.

| Episode | Valid / rejected extracted items | Span repairs | Canonical chapters | Judge action denominator | Semantic matches |
| --- | ---: | ---: | ---: | ---: | ---: |
| Denton County Granicus `857307f158451487` | 18 / 0 | 17 | 22 | 18 | 17 (94.4%) |
| Austin Planning Commission Swagit `e3a8875ae0aee2ec` | 10 / 1 | 0 | 7 | 1 | 1 (100%) |

The Denton result is a promising item-coverage signal, but is insufficient for admission. The Austin
result reveals a new judge-calibration failure: it classified only one of seven provider chapters as
an action, making its apparent 100% denominator meaningless. The next evaluation change must not
ask the judge to infer the action denominator from provider chapter titles alone. It needs a source-
evidenced, independently auditable denominator (or a separate canonical-chapter role classification
evaluation) before provider-level title coverage can be compared or aggregated.

### Confirmed dataset-validation pivot (2026-07-28)

The maintainer approved a deviation from the small held-out LLM-calibration sequence: first build a
read-only, durable research dataset and compare alignment methods on provider chapter data before
admitting a generated-title or locator path. This is not yet a conventional supervised classifier:
canonical chapters provide titles/times but not explicit agenda-line labels, so trainable alignments
must first be derived and their provenance retained.

The prerequisite feasibility check completed against **all 237** strict-cohort episodes in the
supplied snapshot. It reused `scripts/research/agenda_chapters/audit_chapters.py`'s existing source-backed main-agenda
candidate extractor and one-to-one close-title matcher; it fetched only immutable public agenda
artifacts and made no model call or state change.

| Provider | Episodes | Provider chapter titles | Close source-agenda matches | Match rate | Episodes with any / all matches |
| --- | ---: | ---: | ---: | ---: | ---: |
| Granicus | 229 | 6,066 | 5,592 | 92.2% | 226 / 163 |
| Swagit | 8 | 49 | 43 | 87.8% | 8 / 6 |
| **Total** | **237** | **6,115** | **5,635** | **92.2%** | **234 / 169** |

This establishes that source alignment is sufficiently available to justify the research-dataset
workstream. It does **not** establish cross-provider generalization: the cohort is strongly
concentrated in Denton County/Granicus. Dataset splits must therefore hold out time and, where the
catalog grows, provider/body families; a random episode split would overstate performance. B2
credentials are not needed for this snapshot check, but a read-only canonical-state pull becomes
appropriate for refreshing and expanding the dataset.

The maintainer subsequently configured a bucket-scoped read/list B2 application key in the local
Keychain wrapper. A read-only `state/` listing succeeded on 2026-07-28; the remote catalog exposes
47 `state/sources/*/episodes.json` record stores. Dataset refresh must target those records and then
only agenda artifacts selected from canonical rows, rather than invoking a full state sync (the
overall state collection is much larger than this research slice).

### Full-corpus weak-alignment baseline (2026-07-28)

A read-only local build then restored the 42 currently configured source record stores and selected
every episode with persisted source chapters, a transcript/word pointer, and an agenda-text sidecar:
**15,576 episodes** (10,870 Granicus; 4,706 Swagit), **210,664** canonical chapter-title rows, and
**277,937** extracted structural agenda candidates. All 15,576 sidecars were retrievable (public
artifact first, B2 fallback available); none failed. At the conservative existing 0.80 title-similarity
threshold, 129,254 rows (61.36%) had a one-to-one candidate match (Granicus 63.76%; Swagit 45.63%).
The lower rate than the earlier 237-episode slice confirms that that small cohort was not
representative.

The data is split chronologically within provider/body families: 170,798 train rows and 39,866
held-out rows. The research builder retains both the selected candidate and every alternative
candidate in the same agenda, so a ranker can form real within-meeting negatives without refetching
source material.

This dataset supplies **weak**, not independent, labels: a positive is defined by the existing
deterministic title matcher. A local scikit-learn character n-gram TF-IDF / SGD logistic ranker
(50,000 train positives, 10,000 held-out positives, four same-agenda negatives per positive;
positive threshold 0.90) achieved 0.9696 pair ROC-AUC, 0.9111 average precision, and 94.25%
within-agenda Recall@1. The existing deterministic scorer achieved 100% Recall@1 on the exact same
held-out labels, as expected because it generated them. **Do not pursue this self-training ranker as
a production improvement.** Its only possible value would be as an explicitly bounded research
feature; it cannot establish generated-title accuracy.

The first *agenda-only selection* baseline is a different experiment and remains useful. It labels
each extracted agenda candidate positive when any supplied provider chapter deterministically matches
it at 0.80 or above, and labels every other candidate in that agenda negative. The classifier is never
given a provider chapter title: it receives candidate text, immediately adjacent candidate text, coarse
agenda position, and agenda-length buckets. It reserves the newest fifth of each pre-existing train
family for validation/threshold selection, retaining the existing newest-fifth test split. On 175,427
train candidates, 48,199 validation candidates, and 54,311 candidates from 2,248 held-out meetings,
the character n-gram TF-IDF / SGD logistic baseline selected at its validation F1 threshold with:

| Held-out metric | Result |
| --- | ---: |
| Candidate precision / recall / F1 | 0.672 / 0.860 / 0.755 |
| Candidate average precision | 0.746 |
| Per-meeting macro set F1 | 0.713 |
| Mean absolute selected-title count error | 3.50 |

This is the correct supervised question—"from an agenda alone, select the set whose entries look
provider-chapter-derived"—but its targets are still deterministic weak labels. Compare feature/model
variants using the held-out per-meeting set F1 and count error (not ROC alone), then independently
adjudicate the cases on which the winning model and deterministic matcher differ before considering
production use.

The first ablation found that more local agenda context is not automatically better. The same model
using only the candidate title (no adjacent candidates, position, or agenda-length tokens) improved
candidate F1 from 0.755 to **0.795**, average precision from 0.746 to **0.839**, per-meeting macro
set F1 from 0.713 to **0.736**, and mean absolute count error from 3.50 to **2.53**, over the same
54,311 candidates from 2,445 held-out meetings. Treat title-only as the current weak-label baseline;
any next model must beat its per-meeting set metrics on that exact split before it is considered for
independent adjudication.

On that title-only representation, a `LinearSVC` improved per-meeting macro set F1 again to **0.746**
and mean absolute count error to **2.32** (candidate precision / recall / F1: 0.749 / 0.838 / 0.791;
average precision: 0.824). It is the current weak-label winner because set agreement and count error
are the admission-relevant metrics, even though the logistic model has slightly higher candidate-level
average precision (0.839) and F1 (0.795). Its decision-function threshold is fitted only on the
chronological validation split; it must be re-calibrated for any future independent label set.

The next validation must be independent: stratify low-confidence and unmatched canonical-title /
agenda-candidate pairs, adjudicate them (human review preferred; bounded LLM review may assist), and
only then compare a semantic model against the deterministic matcher. This is an admission gate,
not an implementation task for the production chapter stage.

## Contract

Generated chapters are always distinct from canonical chapters. They carry generated provenance,
the locator recipe/model, agenda item index, an anchored source cue, and bounded evidence. They
never overwrite provider titles, dates, transcript text, links, or canonical source chapters.

The locator runs only when all of these are already durable: no canonical chapters, usable agenda
text, a complete transcript, and word timing. ASR timestamps are on the *served* clock. On the
following run the selected anchor is inverted through the persisted EDL with `served_to_source`;
only an unambiguous one-source mapping may become a generated source-time marker. An anchor in a
cut/gap or multi-source ambiguity is rejected, never mislabeled. Because chapter markers affect
encoded audio, this lets run N obtain audio/transcript/agenda and run N+1 generate raw markers
before `AudioStage` re-encodes. Tagging remains after chapters, so it can use generated chapters
when they pass admission.

## Locator input and output

There is no semantic speaker-turn dataset today, and diarization will not be relied upon: the same
speaker commonly closes one Robert's-Rules item and begins the next. Instead request construction
creates **ephemeral locator units** from existing time-resolved VTT cues / word-JSON segments:

```
u00420 | 01:12:03.400–01:12:18.200 | "The next item is resolution ..."
```

Each unit gets a deterministic ID for the request only. The model returns agenda-item references,
chosen unit IDs, a short transition quote, and confidence -- never arbitrary timestamps. Code maps
the chosen ID back to its stored start time and rejects unknown IDs, duplicate starts, invalid
schema, and insufficient evidence. Units are chronological; output chapters are sorted by actual
time. Agenda order is a soft prior, not a constraint: skip, reorder, and revisit are permitted and
reported as evaluation signals. Only emitted timestamps are strictly increasing.

The initial prompt supplies agenda item names/summaries and the full timed VTT/word-derived units.
It excludes backup materials: the main agenda is the cleanest candidate-title list, while packet
text adds false-positive vocabulary. A later, separately evaluated title-repair path may use an
item-specific attributed backup document.

## Shadow routing decision (2026-07-27)

The shadow locator uses `mistral/mistral-large-latest` by default. This is Mistral's current API
and deployed-worker alias for Mistral Large 3 (the dated API identifier listed by the authenticated
model catalogue is `mistral-large-2512`). `gemini/gemini-3-flash-preview`
is an explicit overflow route only when the fully assembled prompt plus reserved output would
exceed Mistral's usable 256k context. This is a throughput decision: the maintainer reports that
Gemini 3 Flash has a 20-request/day allowance, too small for backlog progress, whereas Mistral
has the useful daily-run headroom. The existing route table's provisional Gemini `rpd` value must
not be treated as an entitlement; activation updates its actual quota in the same change.

**Policy deviation confirmed 2026-07-28:** the maintainer confirmed that DeepSeek Flash has no
provider request/day allowance (it is a paid route), and that the deployed Mistral Large 3 alias
(`mistral-large-2512`) is limited to 0.07 requests/second (about four requests/minute), not one
request/second. `deepseek/deepseek-v4-flash` therefore has no artificial RPD or daily-cost ceiling;
pricing/usage telemetry remains. The deployed, one-model Mistral Worker deliberately claims only
one queued request per Cron minute, so the production scheduler records that stricter `rpm=1`
ceiling and reserves one upstream attempt for dispatch rather than the direct structured-output
retry worst case. The local agenda-title runner retains a 15-second start-to-start interval, since
a direct research caller must independently respect the upstream RPS ceiling.

The complete account-specific Mistral model registry is in
[`config/mistral_model_limits.yml`](../config/mistral_model_limits.yml). It intentionally records
both TPM and RPS, plus audio/OCR limits, rather than treating an RPM conversion as sufficient.
The reported 1B token/month figure is preserved as an *unverified shared-account assumption*, so a
future Worker route must confirm its scope/reset semantics before it becomes a hard ledger cap.

The observed strict-cohort high-water mark (72,895 estimated input tokens before prompt/output
overhead) stays on Mistral. The overflow rule retains Gemini's documented 1M input window for
reported long-meeting extremes. Neither route is enabled until the shadow stage, dispatch setup,
and calibration gate are implemented.

The request assembler uses the repository's existing conservative `characters / 4` estimate on
the complete instruction plus serialized agenda/unit payload. It reserves 16,384 tokens for the
structured response and corrective retry: Mistral is selected through 239,616 estimated input
tokens, then Gemini through 1,032,192. A larger payload fails closed without truncation. The
assembler includes only main-agenda titles and all timed units; candidate filtering and backup
materials remain outside the baseline.

## Empirical gates

Before a production locator call, compare three variants against a held-out, stratified sample of
canonical chapters:

1. full transcript units (baseline);
2. deterministic boundary candidates as optional hints, never a hard filter; and
3. the hybrid prompt with both.

Evaluate boundary error/recall at fixed tolerances, precision of admitted boundaries, agenda-title
coverage and similarity, invalid-output rate, reorder/skip rate, cost/latency, and performance by
provider and meeting length. A filter that loses recall versus the full-context baseline is removed.
Canonical markers stay hidden from the input. The same benchmark runs rarely on fresh held-out
episodes after admission to detect model/prompt drift; title extraction is measured independently
by count and similarity. Generated timing is similarly rechecked against new canonical episodes.

### Initial artifact-size probe

The read-only `--measure-samples 2` probe uses the guarded HTTP client and the same conservative
`characters / 4` estimator as the LLM scheduler. Its four stratified records measured 1,639,
11,044, 21,363, and 49,896 combined transcript-plus-agenda input tokens before prompt/schema
overhead. The largest was an Austin special Planning Commission meeting. This supports whole-
transcript as the initial evaluation baseline for this cohort; it is not yet a production model
selection or a catalog-wide size distribution.

The longest-duration selector found no 11-hour fully eligible record in the supplied snapshot.
Its longest durable locator input is a 3.06-hour Denton Commissioners Court meeting at 72,895
combined tokens; the second-longest Denton input is 65,834 tokens, and the longest Austin input
is 53,546. Those fit a 256k context with substantial headroom. The snapshot does contain a
5.49-hour chaptered Denton recording, but it lacks a synced transcript/word sidecar and cannot
be used for this benchmark. Catalog-wide route choice must still account for the reported 11-hour
extreme rather than treating this snapshot as a hard upper bound.

### Human extraction review (2026-07-29)

The paired 96-episode shadow run completed for DeepSeek Flash and Mistral Large. A blinded,
stratified 24-meeting human packet (four meetings in each Granicus/Swagit × high/medium/low
deterministic-coverage stratum) recorded 523 item decisions: **518 correct, 2 incorrect, and 3
unsure**. This is strong evidence that the extractor's immediate problem is **recall**, not
faithfulness: generated titles were concise but faithful to their cited agenda text.

The review isolated three distinct causes that must not be conflated with a same-body similarity
filter:

- A Pflugerville source artifact contained only 27 characters, so neither model had usable agenda
  evidence.
- An Arlington artifact was exactly the global 50,000-character agenda-text cap and ended mid-item;
  later agenda sections were never sent to either model.
- An Austin Board of Adjustment agenda contained a `PUBLIC HEARINGS` heading followed by individual
  cases; both models retained later routine items but omitted those hearing cases. The present prompt
  says to exclude section headings, which plausibly makes this heading/content boundary ambiguous.
  It must explicitly retain *individual hearings/cases/actions beneath* a public-hearing heading
  while excluding only the heading itself and generic participation instructions.

The extractor does **not** currently consume canonical titles, same-body similarity, or a
deterministic agenda-candidate filter. Do not add one as a production gate: it would suppress novel
or irregular agenda items. Similarity remains an evaluation/diagnostic signal only. The next
implementation decision is a targeted prompt/fixture experiment plus a separate bounded
agenda-artifact-cap/backfill design; neither may silently repurpose the generic document cap.

### Locked next evaluation design (2026-07-29)

The human review establishes high extraction precision but cannot yet measure omissions. The next
step is a complete, **agenda-derived gold set**, not a provider-chapter proxy:

1. Build a blinded candidate union from direct LLM extraction, the deterministic agenda-title
   parser, the agenda-candidate classifier, and broad numbered/case-line candidates. The reviewer
   keeps/removes candidates and can add an item absent from every candidate source.
2. Use the present 24 stratified meetings as the development set for the public-hearing and
   consent-agenda prompt changes. Freeze a fresh, similarly stratified 24-meeting holdout before
   measuring the final chosen method; it must never inform prompt/model tuning.
3. Score every method against the same gold set: item and meeting-macro precision/recall/F1, count
   error, provider/coverage stratum, public-hearing and consent-agenda recall, validated-evidence
   rate, cost, and latency. Canonical provider chapters remain a secondary benchmark only.

Extraction recall and transcript-location recall are intentionally separate. An actual agenda item
may be withdrawn, skipped, deferred, or never receive a formal vote/discussion. It is still a
correct agenda extraction, but it should legitimately yield **no generated chapter** when the
transcript locator cannot find sufficient source-grounded transition evidence. Provider chapters
will often omit such items; their absence must not retroactively label the agenda extractor wrong.
The locator records this as `not located`/coverage evidence, never fabricates a timestamp or treats
the absence as a failed extraction.

### Title-reference and consent policy (2026-07-29)

The existing structured `display_ref` field is the right reusable place for visible agenda item or
case identifiers. The next contract revision must require it whenever an exact visible reference is
present in cited source evidence and validate that it is not invented. It is locator metadata, not
a required prefix on the human-facing generated title; this preserves a concise semantic label while
making references such as “item 8.41” usable in a transcript.

The revised extractor prompt must also treat a clearly labeled `Consent Agenda`/`Consent Calendar`
as one composite action when its following entries are constituents and no separate consideration is
stated. It must return the composite rather than enumerate each child. An entry explicitly removed,
separately considered, or separately voted remains its own item. Public-hearing headings are the
opposite case: do not return the heading, but retain each individual hearing/case/action beneath it.

### Development rerun and complete-gold packet (2026-07-29)

The public-hearing and consent wording is implemented in the pure request builder with fixture
assertions. Before a completeness review, both direct extractors are re-run **only** on the existing
24-meeting development packet, preserving Mistral's 15-second direct-submission interval. This is
prompt tuning, not a final benchmark: the current 24 must not be used as the later holdout.

The complete-gold packet deduplicates proposals by cited source evidence (overlapping line spans or
the normalized verbatim evidence quote), never by generated title; model summaries of one source
item often differ word-for-word. The first unfiltered union of direct outputs and structural
candidates was 913 suggestions. Evidence deduplication reduced it to 557 (median about 14 per
meeting; one dense agenda has 170). The local reviewer records `keep`, `remove`, `unsure`, or an
explicitly added missing action with optional source lines. Candidate origins remain absent from the
review UI. The weak-label LinearSVC contributes a compact 158-candidate structural-suggestion view
for diagnosis, but never labels or limits the gold set.

The revised 24-meeting development rerun completed with **24/24 source-validated outputs for both
Mistral Large and DeepSeek Flash**. Mistral was submitted start-to-start no faster than 15 seconds.
DeepSeek initially exposed an Instructor compatibility-table failure for the custom route; the
shared direct backend now reuses the existing provider-native JSON parse/validate/one-corrective-
retry path for DeepSeek's documented JSON-object mode. A literal JSON-output instruction and then
an exact top-level-shape instruction recovered the remaining bounded validation failures. This is
implementation/compatibility evidence only; completeness is still decided by the human
agenda-derived gold set, followed by a fresh untouched holdout.

### Complete-gold development labels (2026-07-31)

The reviewer completed every proposal in the revised 24-meeting packet: 338 `keep`, 149 `remove`,
and 9 `unsure`, plus 25 manually `added` items. Until the uncertain rows are adjudicated, the
scoreable development gold set is 363 items (`keep + added`); the nine unsure rows are excluded
from both numerator and denominator. The labels are agenda-derived evidence, not provider-chapter
truth.

There are 111 substantive title/evidence comments. They overwhelmingly identify reference and
source-span fidelity: 77 mention cited evidence/lines and 54 request visible item/reference
formatting (categories overlap). The immediate packet defect is clear: 76 commented candidates had
an overlapping direct-model `display_ref`, but the local review packet stripped that separate field
and displayed only the generated semantic title. The next contract/UI revision must retain and show
that reference as locator metadata, and validation must reject a reference that is absent from the
cited source span. This is not evidence that all model-generated semantic titles are wrong.

The labels also distinguish two separate failure modes which must remain separate in scoring:

- Nine comments report lost word spacing in one durable source artifact; that is agenda-text
  extraction/normalization quality, not title selection.
- Ten manually added chapter-style entries belong to the 27-character Pflugerville artifact, so
  they have no source lines. They diagnose missing/truncated agenda evidence and cannot fairly
  count against an extractor that was never supplied that source. A later artifact-refresh fixture
  must test this separately from model recall.

The reviewer added procedural entries such as Call to Order, Opening, Consent Agenda, Regular
Agenda, Public Comment, and Adjourn. This conflicts with the original action-only wording. Before
prompt tuning or final scoring, the maintainer must decide whether the output scope is instead
`chapterable agenda segments` (with a procedural-segment subtype) while still excluding generic
participation instructions and boilerplate.

### Maintainer decisions after development review (2026-07-31)

The target is confirmed as **chapterable agenda segments**: substantive actions plus meaningful
procedural segments such as Call to Order, Consent Agenda, Public Comment, and Adjourn. Generic
speaker-registration instructions, time limits, attachments, links, and boilerplate remain
excluded. The output needs a `kind`/subtype so evaluation can separately report procedural-segment
and substantive-action behavior.

Generated `title` remains a concise human-readable summary. A visible item number or provider ID
is **not** required to prefix that title: inconsistent use would degrade the public chapter list.
Instead, future transcript locating receives the title *and* source-grounded evidence containing
the complete printed agenda reference/wording, because spoken meeting transitions commonly say the
number or source prose rather than the generated summary.

The item record therefore separates presentation from retrieval:

```text
title              concise generated chapter label (human-facing)
kind               substantive_action | procedural_segment
display_ref        optional visible item/ID, metadata only (not title decoration)
line_start/end     durable complete source span
evidence_quote     model-cited exact quote used to validate locality
evidence_text      code-derived complete text of line_start..line_end (locator input)
locator_cues       code-derived visible references and normalized source phrases
```

The model must cite a span that includes its visible item/section prefix and ID. Validation keeps
the current exact-quote rule, validates a supplied `display_ref` against the cited span, and
deterministically expands an immediately preceding identifier-only line (for example `4.1.`, `B.`,
or `ID 26-1108`) into the durable evidence span where layout extraction split it from its action
line. It does not prepend that reference to `title`. `evidence_text` and `locator_cues` are built
from immutable source lines, not trusted model prose.

#### Next implementation slices

1. **Freeze and normalize development gold.** Add a read-only compiler that turns the local review
   decisions into per-item gold rows with stable meeting/item IDs, disposition, source-backed vs
   source-unavailable provenance, and title feedback. Exclude the nine `unsure` and no-source
   Pflugerville additions from fair recall denominators; retain them as artifact-recovery findings.
   Prefill `kind` from source/title structure and use a small bounded confirmation pass for any
   ambiguous procedural/action rows.
2. **Score all methods against exactly those rows.** Match by one-to-one source-span overlap within
   a meeting, not title lexical similarity. Report micro and meeting-macro precision/recall/F1,
   count error, source-evidence validity, provider/coverage-stratum results, and separate
   public-hearing, consent, procedural, and substantive-action slices. Treat title feedback as a
   qualitative calibration set; do not turn free-text comments into invented canonical titles.
3. **Revise the item/evidence contract and rerun only development.** Preserve human titles, add
   code-derived complete evidence/cues and reference-span validation, then re-run both models on
   these 24 meetings. Compare against frozen gold. Only after selecting the contract/prompt from
   this development result, freeze a fresh 24-meeting holdout and repeat the same blinded gold
   review and scoring. No production generated chapters, stage version bump, or backfill is
   admitted before that holdout gate.

**Additional development comparator approved 2026-07-31:** run
`mistral/mistral-medium-2508` over the same frozen 24 agendas. Its account registry limit is
356,250 TPM / 0.38 RPS; the direct runner uses a conservative three-second start-to-start interval.
It is registered as an explicit direct evaluation route only, not as a change to the Mistral Large
default or Gemini overflow policy. It is scored against the same frozen gold rows before any
holdout work.

The direct benchmark runner separately controls submission cadence and in-flight concurrency:
Large retains a strict 15-second start cadence for its 0.07-RPS limit but may keep two long-running
requests in flight; Medium retains its three-second cadence and may keep four in flight. Concurrency
only hides provider response latency and never raises the upstream request rate.

### First frozen-gold method score (development only, 2026-07-31)

The common source-span scorer uses 322 source-backed required positives, 24 source-backed optional
procedural positives, 149 excluded negatives, and excludes nine uncertain plus 17
source-unavailable rows. A match is one-to-one overlapping source evidence within a meeting; it is
not a title-similarity score. Primary figures are required-action micro precision / recall / F1:

| Method | Precision | Recall | F1 | Meeting-macro F1 | Optional procedural recall |
| --- | ---: | ---: | ---: | ---: | ---: |
| Mistral Large | .764 | .494 | .600 | .669 | .042 |
| Mistral Medium 2508 | .698 | .596 | .643 | .719 | .250 |
| DeepSeek Flash | **.979** | .587 | **.734** | **.747** | .333 |
| Deterministic structural candidates | .649 | **.752** | .696 | .628 | .917 |
| Weak-label LinearSVC classifier | .942 | .407 | .568 | .445 | .708 |

This development score is useful for choosing the next contract experiment, not an admission
result: human gold was formed from a union containing Large, DeepSeek, and deterministic proposals,
so it cannot establish independent generalization. The Medium result is a useful additional signal
because its outputs were not in that original proposal union. The actionable finding is that
DeepSeek supplies the best concise high-precision titles, while deterministic candidates preserve
substantially more source coverage but cannot be published unfiltered. The classifier is too
recall-limited to be a production gate. A subsequent hybrid experiment must keep deterministic
candidates as evidence/recall hints, not substitute their verbose titles for generated ones, and
must be judged on the fresh untouched holdout.

**Classifier correction from the same human-gold score:** the original LinearSVC selection was
made on deterministic weak labels. Re-running all existing scikit variants on frozen human gold
changes that choice: title-only LinearSVC scored P=.942 / R=.407 / F1=.568 (macro F1=.445),
title-only SGD logistic P=.929 / R=.407 / F1=.566 (macro F1=.424), while **contextual SGD logistic**
scored P=.899 / R=.497 / F1=.640 (macro F1=.496; count MAE=5.78). It remains a hint source rather
than a production gate, but supersedes LinearSVC as the classifier comparator for the next hybrid
experiment. This is exactly why classifier selection must use human gold rather than the weak
alignment proxy.

**Candidate-union analysis:** every scikit prediction is a subset of the 404 deterministic
structural candidates, so adding classifiers to the full deterministic set cannot improve its
P=.649 / R=.752 score. The best compact high-priority union is LinearSVC plus contextual SGD
logistic: 222 candidates, P=.907 / R=.547 / F1=.682. Adding title-only SGD adds no recall. The
remaining 182 deterministic candidates recover 66 more required items (raising total recall to
.752), but score only P=.363 against the required items that the high-priority tier missed. The
next hybrid request should therefore receive the complete source plus two *soft* tiers: the
222-candidate high-priority hint list and the lower-priority deterministic remainder. It must be
free to reject either tier and to extract a source-grounded item outside both; neither tier is a
hard candidate gate or a public-title source.

### Soft-hint result and Gemma routing decision (development only, 2026-07-31)

The approved soft-hint experiment was completed on the same 24 source-backed development meetings.
It gave each extractor the entire agenda plus 222 source-span-only high hints (the LinearSVC plus
contextual-SGD union) and 182 remaining structural spans as low hints. It deliberately withheld
candidate titles and scores. The prompt explicitly allowed the model to reject either tier and to
extract an item absent from both. Against the frozen gold, every model was flat or worse than the
plain full-agenda prompt:

| Model | Plain P / R / F1 | Soft-hint P / R / F1 | Decision |
| --- | --- | --- | --- |
| Mistral Large | .764 / .494 / .600 | .652 / .488 / .558 | reject hints |
| Mistral Medium 2508 | .698 / .596 / .643 | .701 / .590 / .641 | reject hints |
| DeepSeek Flash | .979 / .587 / .734 | .940 / .534 / .681 | reject hints |

This is a development result rather than an admission result because the gold construction is not
independent of all candidate sources. It nevertheless falsifies the proposed two-tier hint format
as the next default: the plain full-source prompt remains the LLM comparison baseline. Keep the
deterministic and classifier methods as independent non-LLM comparators and possible future
retrieval experiments, rather than placing their output in the extraction prompt.

`gemma-4-31b-it` is available through Google AI Studio and has a 256k context window, but the
maintainer-reported free route is limited to 16k TPM despite its 30 RPM / 14.4k RPD figures. Whole
agendas can consume that entire minute budget before output, compared with Mistral Large's 250k TPM
and Medium's 356,250 TPM account limits. Do not add Gemma as an agenda-extraction production route
or consume a fourth 24-meeting benchmark now. Reserve it as a future direct small-context /
small-token option, contingent on that task's model-quality evaluation and an explicit route policy.

### Recall-oriented prompt sweep (development only, 2026-07-31)

With the maintainer's approval, five deliberately different full-agenda prompts were evaluated on
the same 24-meeting development set for DeepSeek Flash and Mistral Medium 2508. All retained the
same JSON schema, exact evidence-quote validation, source-line references, and consent composite
rule. This is prompt development, not a generalization claim: **the winner must be run exactly once
against a fresh frozen holdout before any production admission.**

| Variant | DeepSeek P / R / F1 | Medium P / R / F1 | Finding |
| --- | --- | --- | --- |
| Plain baseline | .979 / .587 / .734 | .698 / .596 / .643 | prior control |
| Coverage audit | .967 / .544 / .696 | .663 / .581 / .619 | worse |
| Transcript-anchor | .948 / .627 / .755 | .684 / .630 / .656 | modest recall gain |
| Hierarchy-first | .968 / .475 / .638 | .677 / .587 / .629 | worse; retain only as a targeted structural idea |
| Ambiguity-inclusion | .978 / .559 / .712 | .681 / .578 / .625 | worse |
| **Agenda-flow** | **.923 / .671 / .777** | **.662 / .742 / .700** | selected development prompt |

`agenda-flow` explicitly asks for the complete likely spoken meeting flow: source-grounded
call-to-order, recognition, report, presentation, hearing, case, appointment, ordinance,
resolution, discussion, vote, and adjournment segments. It continues through the complete agenda
and preserves the consent-agenda composite rule. Its expected precision tradeoff is acceptable for
the proposed later transcript-locator gate: an agenda item that was withdrawn or never reached
should fail to acquire a sufficiently supported transcript boundary and therefore must not publish.
That later gate is not yet implemented; until it is, this is only a benchmark result and cannot
broaden generated chapter publication.

The direct experiment runner records each prompt variant in an independent recipe/output directory
and paces Medium at its three-second start cadence. This makes future refinement auditable without
overwriting the baseline. The next narrow decision is whether a small prompt refinement between
plain and `agenda-flow` is worth another development sweep, or whether to freeze `agenda-flow` now
and spend the next evaluation budget on the independent holdout.

### Frozen agenda-flow holdout (in review, 2026-08-01)

`agenda-flow` was frozen after the development sweep. A new 24-meeting holdout was selected from
the same test partition with **zero episode overlap** from development, balanced four meetings in
each Granicus/Swagit × high/medium/low deterministic-coverage stratum. Medium 2508 and DeepSeek
Flash each completed one source-validated agenda-flow extraction for every holdout meeting. Their
items and the independent structural candidates were evidence-deduplicated into a 389-item,
origin-blind source-review packet; all 24 agenda-text sidecars were available. Do not inspect or
score the model identities until the human source review is complete. The holdout result decides
whether free Mistral Medium is sufficient, whether paid DeepSeek earns an upgraded route, or
whether neither is admitted.

During source review, one selected Granicus agenda proved to be an image-only/no-OCR PDF (its
stored agenda text was 27 characters), two Denton community meetings were present only in the
shared Swagit source archive and matched no published body feed, and five date-stamped Airport
Advisory Board rows were inadvertently treated as distinct bodies. These eight rows are excluded
from the denominator. The sampler now canonicalizes date-stamped body names and filters shared
archive rows against configured feed bodies; an eight-meeting stratum-matched, body-diverse
supplement is under separate origin-blind source review. The source-gold UI presents one
evidence-deduplicated union, not A/B model sets, because its job is to label agenda completeness.
A blinded paired title-quality review remains optional and separate from precision/recall scoring.

The first Granicus-low supplement exposed the same AgendaViewer placeholder failure in multiple
Pflugerville PDFs (27-character `Loading…` sidecars), and a Fort Worth AgendaViewer link yielded
only a 48-character filename/`Loading…` placeholder. Those are excluded from this agenda-text
benchmark. The targeted sampler now supports a minimum structural-candidate floor and excludes by
provider/UID rather than feed slug, because a shared-source episode can otherwise appear through
several feed projections. The verified Granicus-low substitute is Denton County Commissioners
Court, 2010-06-18: its preserved agenda text is 3,926 characters and it has 20 structural
candidates. [GH#1092](https://github.com/BashfulBits/city-meeting-podcasts/issues/1092) tracks
the separate AgendaTextStage placeholder-detection/OCR remediation required before production
generated-chapter work.

### Frozen agenda-flow holdout result (source gold, 2026-08-01)

The completed, usable holdout is **23 meetings** (not 24): 15 retained originals, seven reviewed
supplements, and the Denton County substitute. The one otherwise-retained Fort Worth Public Safety
Committee row had a publisher-placeholder agenda and no proposed items, so it carries no labels
and is correctly outside the denominator. There are 310 required, 38 optional, and 10 neutral or
uncertain source-backed agenda spans. Optional and uncertain spans are intentionally neutral for
the primary precision/recall score: including or omitting them does not make a model wrong. This
avoids treating a reviewer-marked optional section as a model-specific error.

| Method | Precision | Recall | F1 | Macro F1 | Count MAE |
| --- | ---: | ---: | ---: | ---: | ---: |
| Deterministic structural candidates | .706 | .674 | .690 | .729 | 3.57 |
| DeepSeek Flash, `agenda-flow` | **.788** | .729 | **.757** | .815 | 2.57 |
| Mistral Medium 2508, `agenda-flow` | .751 | **.758** | .754 | **.826** | **2.26** |

The 0.003 micro-F1 difference between DeepSeek and Medium is not a meaningful model-selection
result at this sample size. A paired 20,000-resample meeting bootstrap gives Medium-minus-DeepSeek
macro-F1 a 95% interval of -0.038 to +0.050. Medium has the higher recall, higher macro F1, and
lower count error; DeepSeek has the higher micro precision. Because later transcript localization
is intended to discard agenda candidates without a defensible boundary, **Mistral Medium remains
the provisionally preferred free candidate-generation route**, with no quality evidence here that
would justify making paid DeepSeek the default. This is still not a production admission: the
transcript-boundary locator and its independent canonical-chapter validation remain required.
Medium's production use is also deferred until the GitHub chapter-extraction workflow is designed.
At that point, extend the Cloudflare dispatch layer with a model-specific Medium queue/route and
explicit TPM/RPS pacing; the current one-model Large Worker should not be repurposed by changing
only its model variable.

The source-gold packet deliberately was not an A/B model comparison. It labels each unique
source-evidence span once, which is the right instrument for completeness and avoids asking a
reviewer to resolve title wording while finding missing items. A future paired, blinded A/B review
may compare title readability among matched source spans; it must report that separate preference
measure and must not replace the source-completeness score.

### Agenda artifact retention decision (2026-07-29)

Durable agenda sidecars must retain extracted main-agenda text up to the existing extreme document
safety ceiling (currently 1,000,000 characters), rather than trim to the generic 50,000-character
agenda cap. Record the stored character count, conservative truncation signal, and extraction
version. LLM route selection is made later from that recorded length/context estimate; raw source
evidence is never silently shortened to fit a model. A separately designed targeted migration will
refresh only legacy sidecars at the old 50k boundary, not invalidate/re-fetch the full catalog.

## Implementation sequence

1. Add a pure locator-unit builder and offline benchmark selector/reporting. It reads existing
   transcript/agenda/chapter artifacts and makes no model call or output mutation.
2. Add fixture-backed tests for VTT and word-sidecar unit construction, stable IDs, and canonical
   benchmark eligibility.
3. Run and inspect the baseline's real artifact sizes, cohort stratification, and independent
   title-candidate probe. Add a bounded title-selection/equivalence experiment only after its
   contract and canonical acceptance criteria are approved.
4. Select a model route only after that measurement. The existing structured-output LLM path and
   `summarize` task with `scope="agenda-chapter-locate"` will be reused rather than adding a task
   verb.
4. Design admission/provenance storage and a pre-`AudioStage` persisted-artifact stage; then run a
   shadow evaluation. No generated chapter is published until its measured gate and migration/
   backfill plan are approved.

## Open decisions

- The initial route is likely Gemini 3.5 Flash because its 1M context can hold long meetings, but
  this is not a dependency or product choice until real input-size measurements are available.
- Confidence thresholds, tolerances, held-out sampling cadence, and the exact generated-chapter
  record shape are intentionally deferred to the shadow-evaluation slice.
- Whether a generated chapter should be embedded in audio or exposed as a served-time overlay is
  decided with the storage/admission design; the current pipeline invariant requires any embedded
  marker to be known before `AudioStage`.

## Open-source landscape check (2026-07-27)

No maintained open-source library was found that operationalizes this exact contract: agenda-item
input plus timed meeting transcript, reorder/skip tolerance, timestamp-anchor selection, and
provenance-safe generated chapters. We will not add a dependency for generic topic segmentation.

- [NLTK TextTiling](https://www.nltk.org/api/nltk.tokenize.texttiling.html) is a generic lexical
  topic-boundary detector. It normalizes boundaries to paragraph breaks, which VTT does not supply
  reliably, and knows neither an agenda nor source/served time. It can be an optional evaluation-
  only hint, never a gate.
- [TalkTraces](https://senthilchandrasegaran.github.io/pages/pubs/pdfs/talktraces.pdf) is useful
  research precedent: its agenda-to-utterance similarity used word embeddings and cosine
  similarity, but its own users found generic topic modeling too abstract and preferred a
  predefined agenda. This supports our planned soft candidate hints followed by full-context
  comparison, rather than deterministic item assignment.
- [ALIGNMEET](https://github.com/ELITR/alignmeet) is a desktop human annotation/alignment tool, not an automatic locator. Its
  dialogue-act-to-summary alignment and review workflow may inform a future human-admission UI,
  but it cannot consume our durable artifacts or return production markers.
- [MeetingBank](https://meetingbank.github.io/) and Microsoft's
  [TCR project](https://github.com/microsoft/topic_conversation) are datasets/benchmark utilities,
  not runtime components. They are useful external evaluation references: MeetingBank is city-
  council data with transcript/agenda/minutes resources, while TCR supplies agenda-topic relevance
  examples and prompt-analysis utilities. Any use would be a separately licensed offline benchmark
  import, not a production dependency.

The implementation should therefore reuse this repository's existing timed artifacts, validation,
LLM routing/budgeting, and chapter/remap pipeline. The only external method retained for an
experiment is agenda-to-utterance similarity as an optional candidate-hint variant; its value is
decided only by the canonical-chapter benchmark against the full-context baseline.
