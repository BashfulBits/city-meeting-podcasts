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

The model must cite an exact quote from its action evidence, but it need not repeat a visibly
separate item/section prefix or ID in that quote. Post-processing first expands the cited span by
one immediately preceding identifier-only line (for example `4.1.`, `B.`, or `ID 26-1108`) when
layout extraction split it from the action line. Only after that expansion does validation check a
supplied `display_ref`; when the model omits it, code derives the first visible reference from the
expanded immutable source span. It does not prepend that reference to `title`. `evidence_text` and
`locator_cues` are built from immutable source lines, not trusted model prose.

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

### Transcript-boundary locator Phase 0 (2026-08-01)

The first locator slice is now implemented as the read-only research helper
`scripts/research/agenda_chapters/build_locator_benchmark.py`, with fixture coverage in
`tests/test_locator_benchmark.py`. It reuses `collect_benchmark_cohort`, the existing
`build_locator_units` VTT/word-sidecar contract, `build_locator_request`, the shared token
estimate, and the structural agenda-title matcher. It does not call a model, mutate episode state,
or publish chapters. It UID-deduplicates shared feed projections before selection, normalizes body
keys for diversity, then round-robins `under-2h`, `2-to-4h`, `4-to-8h`, and `8h-plus` meetings
before filling remaining slots by recency. A row with no usable agenda candidates is retained as
an explicit non-admission row with its transcript/agenda sizes and timing-unit count.

Using the preserved local research snapshot (`chapter-alignment-records`) gives the following
eligibility baseline before model calls:

| Provider | Eligible feed rows | UID-deduplicated episodes | Canonicalized bodies | Under 2h | 2–4h | 4–8h | 8h+ |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Granicus | 10,870 | 1,191 | 88 | 1,012 | 140 | 38 | 1 |
| Swagit | 4,706 | 1,070 | 176 | 760 | 244 | 25 | 10 |

A bounded 12-per-provider public-artifact measurement was run without a model. All 24 selected
rows had a usable word sidecar, so this pass measured the preferred `words` path; VTT fallback is
covered by unit tests but still needs a deliberately selected sidecar-missing cohort. Granicus
had seven rows with structural agenda candidates and five rows with none (including two very short
sidecars and one zero-byte fetch); Swagit had candidates for all 12. Among rows with candidates,
the deterministic canonical-title join was 104/180 chapters for Granicus and 73/133 for Swagit in
this intentionally mixed, body-diverse sample. This is an eligibility/evidence signal, not a
locator accuracy score: a chapter can be spoken without its printed summary, and the later
source-evidence contract must still use complete agenda lines and IDs.

The existing full-context request builder selected the route from measured input size, with no
truncation:

| Provider | Rows with a complete locator packet | Mistral route | Gemini overflow | Input-token range (median) |
| --- | ---: | ---: | ---: | ---: |
| Granicus | 7 | 6 | 1 | 41,880–254,861 (90,725) |
| Swagit | 12 | 8 | 4 | 4,960–638,403 (117,396) |

The largest observed packets were 254,861 tokens for Granicus and 638,403 for Swagit; the latter
requires the Gemini overflow route under the current 256k Mistral budget. The five Granicus rows
without structural candidates still had timed transcript units, so they are an agenda-extraction
eligibility problem rather than a transcript-boundary problem. Before any locator model sweep,
inspect those artifacts (placeholder/empty versus genuinely unnumbered agendas) and add a small
sidecar-missing/VTT-fallback stratum. The next measurement should then freeze a larger
provider × duration × agenda-eligibility cohort, record the exact request manifests, and only
after that compare full-transcript baseline versus optional deterministic hints. No generated
chapter is admitted from this Phase 0 report.

### Transcript-boundary fallback and agenda-artifact diagnosis (2026-08-01)

The next slice added an explicit research-only `allow_vtt_fallback` eligibility flag and
`--include-vtt-fallback` CLI option. The default word-sidecar cohort is unchanged. With the flag,
the preserved snapshot contains 6 CivicClerk, 57 Granicus, and 28 Swagit UID-deduplicated VTT-only
rows. The selector forces one VTT-only row per provider when available, while retaining the same
duration/body stratification.

The bounded 12-per-provider fallback measurement selected 30 rows (CivicClerk has only six
eligible rows):

| Provider | Selected | VTT units | Complete packets | Mistral | Gemini overflow | Context range (median) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| CivicClerk | 6 | 6 | 6 | 3 | 3 | 79,270–286,709 (230,590) |
| Granicus | 12 | 1 | 6 | 5 | 1 | 41,880–254,861 (98,520) |
| Swagit | 12 | 1 | 12 | 8 | 4 | 4,960–638,403 (95,471) |

All eight selected VTT rows parsed into timed units. The forced Granicus VTT row had no usable
agenda candidates because it was a `DocumentViewer.php`/`Loading…` placeholder; the CivicClerk and
Swagit VTT rows produced complete packets. This validates the VTT parser as a fallback input path,
while confirming that agenda eligibility must be measured independently from transcript timing.

The five original Granicus no-candidate artifacts were fetched read-only and classified as two
`unpublished-placeholder` responses, two empty artifacts, and one `viewer-placeholder`. None was a
genuine agenda with unnumbered action items. This evidence belongs with the existing OCR/placeholder
remediation issue rather than weakening the locator benchmark's agenda-candidate gate.

The next gate is now a larger frozen cohort with explicit strata for provider, duration, agenda
artifact class, and transcript timing artifact (word sidecar versus VTT fallback). It should include both complete packets and recorded
non-admissions, then compare the full transcript packet against optional deterministic hints. No
LLM call is warranted until that packet is frozen and its source/evidence manifest is reviewable.

### Provider-chapter retrieval benchmark (approved next slice, 2026-08-01)

The next benchmark will use episodes that already have provider-supplied chapters as the primary
ground-truth cohort. The generated agenda-item records for an episode (concise title, complete
`evidence_text`, visible references, and source span) become the only agenda-side input to the
chapter-finding process. Provider chapter records remain hidden from retrieval and verification;
they are joined only by the scoring harness. This prevents canonical chapter text from leaking
into either deterministic retrieval or an LLM prompt.

The initial frozen design is **96 development episodes plus 96 held-out test episodes** (192
unique episode UIDs total), with a larger cohort allowed only if it preserves the same split and
artifact-quality rules. The split is stratified by provider, normalized body, meeting duration,
agenda-artifact representation, and transcript timing artifact (word sidecar versus VTT fallback).
Where the catalog permits, recurring bodies
are kept entirely on one side of the split, and test episodes are later in time or body-disjoint;
random episode splits are not acceptable because agenda templates and item phrases otherwise leak
across the boundary. Shared feed projections are UID-deduplicated before sampling.

Each row stores an immutable research manifest containing:

1. episode UID, provider, normalized body, date, duration, and source chapter provenance;
2. the exact agenda artifact hash/class and complete generated agenda-item records;
3. the timed transcript/word-sidecar or VTT source and its artifact hash;
4. provider chapter start markers, any explicit ends, and titles in a hidden scoring section; and
5. exclusion/non-admission reasons for rows that cannot be scored.

Known-bad agenda artifacts are excluded from the scored set rather than silently treated as recall
failures: empty or viewer/loading placeholders, OCR-minimal PDFs already identified by the agenda
OCR issue, failed fetches, incomplete transcript timing, duplicate UIDs, and malformed provider
chapter records. A valid agenda with a genuinely skipped, withdrawn, or consent-subsumed item is
retained; the process must be allowed to return `not_found` for that item. Such rows are important
for measuring false chapter creation. Excluded rows remain in a separate artifact-diagnosis
manifest so they are not forgotten or counted as successes.

The first restored-state inventory found that the provider chapter records used by this catalog are
normally **start-only**: titles and `start` offsets are present, while provider `end` offsets are
absent. The dataset builder therefore treats monotonic provider starts as the canonical timing
labels and derives an end only for local diagnostics (the next provider start, or the served audio
duration for the final chapter). A future scorer must use the provider start as the primary
boundary label and must not mistake absent provider ends for malformed chapters. Rows with missing
titles, missing starts, non-monotonic starts, or invalid explicit ends remain excluded.

The initial 192-row manifest requires a synced word sidecar for every episode, so its transcript
timing-artifact stratum is currently constant (`words`). The field remains in the manifest because
the later VTT-fallback cohort will need the same split and scoring distinction; it is not a second
provider-chapter ground-truth source.

The benchmark reports three separate layers, with provider-chapter inclusion as the chief
end-to-end metric:

- **Agenda extraction coverage:** whether a provider chapter has an independently source-grounded
  generated agenda item whose evidence plausibly covers it. This is scored before transcript
  retrieval and prevents a locator from being blamed for a missing agenda item.
- **Retrieval coverage:** for each covered provider chapter, whether the deterministic candidate
  union contains a timed transcript window within the boundary tolerance. This is measured at
  several top-k values and at the final compact packet size.
- **Verification/boundary quality:** whether the compact LLM verifier chooses the correct anchor,
  rejects a skipped item, and avoids duplicate or out-of-order chapters. Precision, recall, F1,
  boundary error, candidate token count, and full-context escalation rate are all retained.

The first comparison is deliberately empirical and paired on the same episodes:

1. full-transcript locator baseline;
2. sparse lexical retrieval using normalized rare terms, IDs, phrases, and transition cues;
3. agenda-to-transcript embedding similarity, following the agenda-vector/sentence-vector
   approach in [TalkTraces](https://vis.cs.ucdavis.edu/papers/TalkTraces_CHI2019.pdf); and
4. the high-recall union of lexical, embedding, neighboring-window, and transition-cue hits.

The agenda-aware attention design described in [Dynamic agenda-aware real-time meeting
summarization](https://link.springer.com/article/10.1007/s44443-025-00304-y) is an architectural
reference for the per-item verifier, not a dependency or an assumed production model. The first
sparse implementation should reuse the existing optional scikit-learn research dependency and
timed-artifact builders. Dense embeddings, rerankers, or a new runtime library are admitted only
if the held-out development results show a material recall/cost benefit and their operational
burden is acceptable.

Development episodes may tune token windows, top-k, neighbor expansion, confidence thresholds,
and prompt wording. The test manifest, provider-chapter section, and all thresholds are frozen
before the test run. No provider chapter title, timestamp, or canonical count may be supplied to
the retrieval or verifier process. The full-context baseline remains a required canary and
fallback; a cheaper retrieval path is admitted only when its provider-chapter recall is not
materially worse and its false-publication safeguards remain intact.

### Frozen final agenda extraction for the locator cohort (2026-08-01)

The 192-row locator cohort now has one authoritative agenda extraction pass. It used the frozen
`agenda-flow` prompt and `mistral-medium-2508`; the earlier Large/DeepSeek shadow outputs are not
substituted into this dataset because they predate the final prompt decision. The run submitted
all 192 episodes, completed the two transient failures sequentially, and produced 192 terminal
responses. The 192 public agenda sidecars were then fetched into a temporary read-only cache and
each raw response was re-run through the current source validator. This explicitly verifies that
evidence expansion to a preceding item-number/ID line happens before `display_ref` validation.

The result was 192/192 revalidated responses, 2,969 accepted generated agenda items, and 190 rows
with at least one accepted item. Two rows have zero accepted items: one response returned an empty
list for a source whose extracted text is not useful for action-item discovery; the other contained
hierarchical Fort Worth references whose declared evidence spans did not
contain the cited reference or quote. Those rows remain in the research manifest and are not
counted as provider-chapter recall failures. The joined temporary artifacts are:

`/private/tmp/locator-dataset-final-medium-agenda-flow/manifest.json`,
`/private/tmp/locator-dataset-final-medium-agenda-flow/gold.json`, and
`/private/tmp/locator-dataset-final-medium-agenda-flow/diagnostics.json`.

The runner and dataset loader preserve the prompt variant and support the nested
`<prompt-variant>/<model>/` output layout. No transcript boundary locator or provider chapter
title/timing was supplied to the agenda extractor.

### Initial provider-chapter crosswalk audit (2026-08-02)

Before building transcript retrieval, a scoring-only crosswalk compared the hidden provider chapter
titles with the final Medium-generated agenda titles/evidence. When available, it also compared
each provider title with the structural candidates in the original agenda sidecar. This is a
diagnostic, not a gold label and none of its relationships are passed to retrieval.

Across 2,564 provider chapters, the source-level comparison found 2,117 strong matches, 23 possible
matches, 407 unmatched titles, and 17 episodes whose sidecars produced no structural candidates.
The generated-agenda comparison found 1,758 strong matches, 72 possible matches, 113 ambiguous
matches, and 621 unmatched titles. There were 1,589 chapters that were strong in both comparisons.
Another 541 chapters had a strong/possible source match but no strong generated match; these are
agenda-extraction or crosswalk gaps, not locator failures. Ninety-eight chapters had multiple
plausible generated-item candidates, concentrated in repeated procedural language, consent-style
entries, and hierarchical references.

This confirms that retrieval scoring needs an explicit scoring-only crosswalk with role and
ambiguity states. Provider section headings, consent composites, skipped items, and genuinely
unmapped chapters must not be silently converted into ordinary action-item retrieval negatives.
The crosswalk implementation is `audit_locator_crosswalk.py`; its output remains outside the
repository with the other benchmark artifacts.

### Development crosswalk review packet (2026-08-02)

The heuristic crosswalk is useful for finding edge cases, but it is not a gold label.  The next
gate is a small human adjudication packet built only from the **development** half of the frozen
192-episode cohort.  `prepare_locator_crosswalk_review.py` selects 48 chapter-level cases from 48
unique development episodes (24 Granicus and 24 Swagit), with deterministic strata:

| Stratum | Cases |
| --- | ---: |
| ambiguous candidate relationships | 12 |
| source-strong/generated-gap | 12 |
| procedural, section, or consent-shaped | 8 |
| unmatched, no-structural-source, or hierarchical-reference | 8 |
| clear strong controls | 8 |

The packet carries the provider chapter title and index, episode/body metadata, the complete
generated agenda-candidate list (title, display reference, evidence text, and agenda line range),
and the extracted agenda source lines.  It deliberately carries no provider chapter start/end,
transcript, word-sidecar, or timing-source fields.  The reviewer labels each case as a matched
candidate, consent/composite chapter (with two or more selected candidates), section/procedural,
missing generated candidate, source/extraction problem, or unsure.  A missing-candidate reason and
free-text note are optional/required as appropriate.  These labels are for scoring-only crosswalk
calibration; they must not enter retrieval prompts or production decisions.

Build and serve it with:

```bash
PYTHONPATH=.:scripts/research/agenda_chapters \
python scripts/research/agenda_chapters/prepare_locator_crosswalk_review.py \
  --manifest /private/tmp/locator-dataset-final-medium-agenda-flow/manifest.json \
  --crosswalk /private/tmp/locator-crosswalk-audit.json \
  --agenda-cache /private/tmp/locator-agenda-medium-192-final-cache \
  --write /private/tmp/locator-crosswalk-review-packet.json

PYTHONPATH=.:scripts/research/agenda_chapters \
python scripts/research/agenda_chapters/serve_locator_crosswalk_review.py \
  --packet /private/tmp/locator-crosswalk-review-packet.json \
  --decisions /private/tmp/locator-crosswalk-review-decisions.json
```

The UI is localhost-only.  It shows all generated candidates for the meeting so a heuristic
miss does not become an apparent retrieval miss; the held-out test split remains untouched.

### Human crosswalk adjudication result (2026-08-03)

The 48-case development packet is now labeled.  Its raw label counts are:

| Human label | Cases | Interpretation for extraction quality |
| --- | ---: | --- |
| matched candidate | 30 | usable one-to-one agenda/candidate relationship |
| consent/composite | 1 | usable relationship, but not one provider chapter per child item |
| section/procedural | 5 | not ordinary action-item extraction negatives |
| unsure | 2 | provider title/source does not establish a reliable relationship |
| missing generated candidate | 10 | requires reason-level adjudication; not all are extractor failures |

The packet must not be read as a prevalence sample.  Forty of its 48 cases were deliberately
selected from ambiguity/gap/unmatched strata; only eight were clear controls.  In the full
development half of the cohort, the selector's diagnostic strata are 251 clear controls, 182
source-strong/generated-gap, 332 ambiguous, 231 procedural/consent, and 511 unmatched/
structural/hierarchical chapters.  Thus the packet intentionally over-samples source gaps and
edge cases, and under-samples unmatched chapters.  The 10/48 missing-candidate label rate is not a
catalog-wide recall estimate.

The reason labels separate the apparent misses further.  Of the ten missing-candidate cases, seven
were marked `extraction_missed_item`, one was an agenda-absent item, one was a provider-only section,
and one was a broad/composite mismatch.  The comments identify a smaller set of genuine problems:
multiline/hierarchical agenda entries can be omitted, a nested child action can be hidden under a
parent evidence span, and one Fort Worth Building Standards response appeared to contain only two
accepted candidates for an agenda with roughly 35 raw items.  Other cases are expected section
headings, a provider-only future-agenda entry, or a feed/body metadata mismatch rather than a model
recall failure.

There is an important measurement trap here: the packet displayed **post-validated accepted items**,
not the raw LLM list.  Across the 192 final Medium responses, the raw responses contained 3,476
items; 2,969 survived source revalidation and 507 were rejected (288 quote-span failures and 219
display-reference-span failures).  The Fort Worth Building Standards row contained all of its
case entries in the raw response, but 32 were rejected because the current validator could not find
the hierarchical display reference in the expanded evidence span.  Another long Fort Worth row had
17 raw items and zero accepted items.  Therefore the packet demonstrates a real evidence-repair /
postprocessor weakness in addition to genuine extraction misses; it does not demonstrate that the
Medium model simply failed to read the agendas.

Before using this crosswalk as the retrieval gold gate, preserve the raw/rejected distinction and
run a recovery audit for hierarchical references, multiple preceding identifier lines, and
multiline evidence spans.  Then re-adjudicate only the recovered/changed cases (or rebuild the
development packet) before estimating agenda-item recall.  The current evidence supports: “the
agenda path has a meaningful long-tail evidence/validation failure mode that must be fixed,” not
“the average agenda extraction is only 79% complete.”

### Rejected-item recovery audit (2026-08-03)

The first read-only recovery audit is implemented as `audit_agenda_recovery.py` and ran against all
507 rejected raw items.  It does not accept or rewrite any item.  Its diagnostic classes were:

| Audit class | Items | Meaning |
| --- | ---: | --- |
| exact quote, source reference resolved | 107 | safe candidate for conservative span repair |
| exact quote, descriptive display label | 43 | source evidence is exact; the label is not a formal reference and must not be a hard gate |
| token-subsequence source window | 206 | quote omits layout/parenthetical text but source tokens remain ordered; store the complete source window if repaired |
| token-subsequence, formal reference unresolved | 18 | source window is plausible but hierarchical/reference repair still needs review |
| exact quote, formal reference unresolved | 52 | reference resolver needs a broader/typed hierarchy pass |
| quote ambiguous | 11 | repeated source text; do not auto-accept |
| quote not found | 70 | remains a genuine recovery or source/LLM problem |

Thus 356 of 507 rejected items have an exact or source-token-ordered recovery path, but the
token-subsequence class is only an audit signal—not permission to accept a discontinuous model
quote.  A repair must retain the complete immutable source span and record its recovery method.
The shadow implementation adds typed formal-reference parsing, multi-line hierarchy prefix
expansion, and complete-source-window recovery while retaining ambiguous/not-found items in
diagnostics.  Only after reviewing that shadow should we rerun the crosswalk review or spend LLM
calls on the remaining irrecoverable/ambiguous items.

### Shadow recovery implementation result (2026-08-03)

The reusable shadow function `recover_agenda_item_extractor_response` now lives in
`citypods/chapter_titles.py`; `build_agenda_recovery_shadow.py` applies it without model calls or
durable writes.  Across the 192 raw responses it produced 350 separately marked recovered items
across 54 rows, leaving 157 raw rejections unresolved.  Recovery methods preserve complete source
line windows and record whether the repair was exact, token-subsequence, identifier-prefix, or
hierarchical-prefix recovery.  Strict accepted items remain unchanged.

For a temporary recovered-manifest comparison only, the scoring-only crosswalk changed as follows:

| Measure | Strict candidates | Strict + recovered shadow |
| --- | ---: | ---: |
| provider chapters with strong generated match | 1,758 | 1,885 |
| provider chapters with source-strong/possible but no strong generated match | 541 | 432 |
| generated items | 2,969 | 3,319 |

The human packet examples behaved as expected: Arlington SUP14-6 and Denton DCA26-0002 moved to
strong recovered matches, while the Fort Worth “Changes in Membership” item (absent from the raw
LLM list) and several Austin case/discussion items remained unresolved.  The recovered manifest is
diagnostic only; it is not yet the frozen agenda input, production output, or locator gold.

### Fixed-case recovery review packet (2026-08-03)

Because recovery changes the crosswalk strata, regenerating the original 48-case packet would
confound recovery quality with a new sample.  `prepare_locator_recovery_review.py` therefore keeps
the original packet's selected episodes and provider chapters fixed, and emits a fresh `RXR-*`
packet containing only its 15 cases with shadow-recovered items.  It contains 128 recovered
candidates (including the long Fort Worth and Denton rows), marks each recovery method in the UI,
and uses a separate decision namespace so the original labels and comments remain untouched.  It
omits provider timings and makes no model calls.  This packet is the next human gate before any
recovered item is admitted to the agenda input or locator gold.

### Fixed-case recovery adjudication (2026-08-03)

The recovery packet was completed.  The 15 cases were labeled as 12 direct matches, one
consent/composite relationship, one section/provider-only case, and one unsure case.  Four
recovered candidates were selected: three as direct matches (Arlington SUP14-6, Fort Worth
HS-23-134, and Denton DCA26-0002) and one as part of a consent/composite relationship.  The other
selected matches were already present in the strict candidate set, so recovery was unnecessary
for those provider chapters.  This is evidence that the reviewed recovery additions are useful,
but it is not a precision estimate for all 350 recovered items because the packet contains one
provider chapter per episode and does not adjudicate every recovered candidate.

The DCA26-0002 review also exposed a span-completeness defect: the matched agenda paragraph's
formal ID (`DCA26-0002B.`) appears on a line after the descriptive paragraph, while the recovered
source span stopped at the paragraph's last descriptive line.  The candidate is semantically
correct, but the recovery layer must expand forward to a trailing formal-reference line before
it can be used for downstream transcript matching.  The next bounded repair is therefore a
forward identifier/reference expansion pass, followed by a focused re-review of affected cases;
the current shadow manifest remains non-authoritative.

### Forward formal-reference repair result (2026-08-03)

The recovery layer now performs one conservative forward pass after locating a source span.  It
may include a nearby standalone formal ID line when the intervening source lines are contiguous;
blank lines stop the search, and bare section markers such as `A.` or `3.` are not treated as
trailing IDs.  This addresses IDs printed after a long wrapped paragraph without absorbing the
next agenda item.

The v2 shadow run recovered 377 items across 60 rows and left 130 unresolved, compared with 350
and 157 in the prior shadow run.  The scoring-only crosswalk changed to 1,907 strong provider
matches, 79 possible, 155 ambiguous, and 423 unmatched chapters; the source-strong/possible gap
fell to 431 (from 432 in the prior shadow and 541 under strict validation).  The generated-item
count is 3,346.  In the reported DCA26-0002 case, the recovered span now extends through the
trailing `DCA26-0002B.` line.  These figures remain diagnostic: the v2 recovered manifest is not
the frozen agenda input, production output, or locator gold.

The five-row forward-expansion spot-check is complete.  The two previously validated recovered
matches (SUP14-6 and DCA26-0002) retained correct relationships and complete trailing-reference
spans; no expansion crossing into a later item was reported.  The remaining rows likewise had no
span defect; the City Council Español row was correctly treated as a provider-only/procedural
chapter rather than forced into an agenda candidate.  The forward rule is therefore accepted for
the shadow layer, while the broader recovered candidate set remains diagnostic until its admission
policy is decided.

### First deterministic locator retrieval slice (2026-08-03)

The first read-only evaluator is `evaluate_locator_retrieval.py`.  Its “full-context” path only
measures the request that would be sent to the locator; it does not call a model.  The lexical and
TF-IDF paths rank timed transcript units, and the union adds neighboring units.  Provider chapter
starts and the agenda/provider crosswalk are used only after ranking to score candidate recall.
The TF-IDF path is explicitly a lightweight scikit-learn proxy for an embedding path, not a
semantic-model result.

A four-episode development smoke slice (two Granicus and two Swagit) completed with public
transcript/word-sidecar fetches.  Under strict candidates there were 46 scoreable strong chapters;
strict-plus-recovered had 48.  At top-10, lexical/union recall was 42/46 (0.913) strict and 44/48
(0.917) recovered.  Swagit top-1 union recall was 6/9 in both variants; the recovered improvement
came from the long Fort Worth row, whose full-context request grew from 84,699 to 89,840 tokens
and whose top-1 union hits rose from 2/3 to 4/5.  This is a smoke result only, not a model-quality
estimate; the full development run is recorded below.

### Full deterministic locator development run (2026-08-03)

The paired development run used all 96 frozen episodes (48 Granicus and 48 Swagit), the current
v2 recovered agenda manifest, and public transcript/word-sidecar artifacts.  The retrieval
evaluator is `evaluate_locator_retrieval.py`; it makes no model call.  It ranks timed transcript
units from each agenda candidate, while provider chapter starts remain hidden until scoring.
`union` is the lexical-plus-TF-IDF top-k set with one neighboring unit on each side.  The TF-IDF
path is a lightweight scikit-learn proxy for an embedding retriever, not a semantic embedding
model.

The strict crosswalk supplied 1,165 strong provider chapters.  The recovered crosswalk supplied
1,225 strong chapters: 1,165 from the strict set plus 60 newly scoreable recovered relationships.
The paired result below is the fair comparison because it holds the 1,165 strict-covered targets
constant:

| Candidate path | Top-1 | Top-3 | Top-5 | Top-10 |
| --- | ---: | ---: | ---: | ---: |
| Lexical (strict-covered targets) | 655/1,165 (0.562) | 795/1,165 (0.682) | 843/1,165 (0.724) | 918/1,165 (0.788) |
| TF-IDF proxy (strict-covered targets) | 663/1,165 (0.569) | 796/1,165 (0.683) | 842/1,165 (0.723) | 907/1,165 (0.779) |
| Union (strict-covered targets) | **752/1,165 (0.646)** | **866/1,165 (0.743)** | **908/1,165 (0.779)** | **977/1,165 (0.839)** |

These percentages are **not** calculated over every generated agenda candidate.  They are
provider-chapter recall: the denominator is a provider chapter whose hidden title was linked to a
generated agenda item with `status=strong`.  To answer the candidate-side question separately, the
same run grouped strong provider starts by unique generated item.  In the recovered development
manifest there were 1,957 generated candidates; 1,185 (60.6%) were linked to at least one strong
provider chapter, leaving 772 with no strong provider-chapter relationship.  The latter group is
not automatically false: it includes legitimate skipped/withdrawn items, consent children,
procedural or section candidates, and crosswalk failures.

On only those 1,185 strong-linked generated candidates, candidate-side recall was:

| Candidate path | Top-1 | Top-3 | Top-5 | Top-10 |
| --- | ---: | ---: | ---: | ---: |
| Lexical | 55.1% | 66.8% | 70.9% | 77.4% |
| TF-IDF proxy | 55.5% | 66.8% | 71.0% | 76.3% |
| Union | **63.4%** | **73.1%** | **76.8%** | **82.7%** |

The paired strict candidate set was 1,127 unique candidates; union top-10 recall was 944/1,127
(0.838), essentially the same as the provider-chapter view.  Therefore the current result is not
primarily an artifact of counting many unmatched agenda candidates in the denominator: among
agenda candidates that do have a strong provider-chapter relationship, the deterministic union
still misses roughly 17% at top-10.  Conversely, the 39.4% of recovered candidates without a
strong crosswalk relationship is a separate agenda/crosswalk coverage problem and cannot be
called either a retrieval success or a retrieval failure without adjudication.

This candidate-side metric is still a heuristic diagnostic because the strong crosswalk itself is
not the final human gold set.  The next review slice should sample both kinds of cases: retrieval
misses among strong-linked candidates, and unlinked candidates to determine how many are expected
skips/consent/procedural entries versus agenda-extraction or crosswalk misses.

#### Sensitivity to the local retrieval window

The 82.7% headline uses the union's top ten lexical/TF-IDF units plus one adjacent timed unit on
each side, with a 60-second provider-start tolerance.  A development sweep on the same 1,185
strong-linked candidates shows that widening the **candidate packet** helps, but only modestly:

| Union neighbor radius | Candidate-side recall | Paired strict candidate recall |
| ---: | ---: | ---: |
| 0 units | 967/1,185 (0.816) | 933/1,127 (0.828) |
| 1 unit (current) | 980/1,185 (0.827) | 944/1,127 (0.838) |
| 2 units | 989/1,185 (0.835) | 952/1,127 (0.845) |
| 4 units | 1,005/1,185 (0.848) | 968/1,127 (0.859) |

These are adjacent ASR/word-sidecar units, not fixed seconds; their durations vary.  The gain from
one to four neighbors is about two percentage points, so simply sending a wider local packet will
not close the entire gap to 100% and will increase verifier input size.

For comparison, changing only the **scoring tolerance** (still one neighbor) produced 71.8% at
30 seconds, 82.7% at 60 seconds, and 89.7% at 120 seconds on the candidate side.  The 120-second
number is not a better locator—it permits a selected clue to be two minutes from the canonical
start.  We should keep tolerance fixed for the benchmark and treat local-window width as the
actual packet-design variable.

This confirms the concern that deterministic retrieval alone is not sufficiently complete for
publication.  The next experiment should compare the full-context locator with compact union
packets (likely radius 2 or 4) on development; the LLM can use the wider evidence to resolve
transition language that lexical similarity misses, while provider chapters remain scoring-only.

### Learned transition scoring and escalation policy (research phase, 2026-08-04)

The deterministic score should not be treated as the final transition detector.  The literature
supports a learned, agenda-aware reranker, but not an end-to-end deep model trained directly on our
current 96-episode development half:

- Georgescul, Clarck, and Armstrong's meeting-segmentation study used a supervised SVM with
  lexical, acoustic, and syntactic/conversational features for boundary classification.  This is
  directly relevant to a small scikit-learn prototype, especially because our timed artifacts
  expose pause/gap and unit-duration features even before diarization:
  [ACL paper](https://aclanthology.org/2007.jeptalnrecital-long.1/).
- TalkTraces used agenda-vector/utterance-vector cosine similarity and an “unknown topic”
  probability threshold.  That supports both a semantic candidate scorer and an explicit
  `not_found` state rather than forcing every agenda item onto a transcript window:
  [CHI 2019 paper](https://vis.cs.ucdavis.edu/papers/TalkTraces_CHI2019.pdf).
- Solbiati et al. report a 15.5% error reduction from BERT-based unsupervised meeting topic
  segmentation over earlier unsupervised methods, while noting that meeting ground truth is hard
  to collect.  This is evidence for testing a dense-similarity/change-point feature, not for
  importing a large runtime model:
  [paper](https://arxiv.org/abs/2106.12978).
- A recent agenda-aware meeting summarization design tracks the current agenda item by comparing
  each utterance with agenda items, allows forward skips, and treats revisits separately.  Its
  stateful tracking pattern is useful for our reranker, but its training/evaluation assumptions do
  not replace our provider-chapter benchmark:
  [Springer paper](https://link.springer.com/article/10.1007/s44443-025-00304-y).

The bounded experiment should proceed in four layers:

1. **Feature rows.** For each agenda item and timed transcript unit/window, retain the current
   lexical and TF-IDF scores, identifier/phrase overlap, dense-similarity score if tested,
   similarity to the previous and next local windows, local maxima/score slope, transition cue
   phrases (“next item”, “move to”, “item number”, “motion”, “public hearing”), timestamp gap and
   unit duration, meeting-relative position, and a soft agenda-order distance.  Agenda order must
   remain a prior: skipped and revisited items are legal.
2. **Small learned rerankers.** Train a calibrated LogisticRegression baseline, then a small
   HistGradientBoostingClassifier; optionally compare a calibrated LinearSVC because it already
   exists in the research tools.  Fit only on development episodes with grouped folds by episode
   and body family.  Evaluate candidate-side recall, provider-chapter recall, boundary error,
   packet token count, and precision of the `not_found` decision.  Do not use provider titles or
   provider starts as runtime features.
3. **Temporal reconciliation.** Re-rank local maxima rather than independently selecting every
   agenda item's highest-scoring unit.  A small dynamic-programming/HMM-style pass may prefer
   staying on the current item, allow a forward skip, and allow a revisit at a penalty.  It must
   not hard-code monotonic agenda order or collapse a consent composite into child items.
4. **Gold-label discipline.** Strong crosswalk relationships provide positive boundary labels.
   Provider-only/section/procedural chapters, consent children, skipped/withdrawn items, ambiguous
   relationships, and unlinked agenda candidates are not all interchangeable negatives.  Keep
   them as separate roles or unlabeled cases; add targeted human labels before estimating
   candidate precision.  The held-out 96 episodes remain untouched until the feature/model family
   and thresholds are frozen.

#### Escalation policy for compact versus full-context locator calls

The compact call should return a structured result per agenda item: `found`, `not_found`, or
`ambiguous`; a supplied unit ID; a copied transition quote; and a confidence/rationale.  The
validator rejects invented unit IDs and timestamps.  The confidence is not trusted blindly; it is
calibrated against the provider-chapter benchmark and combined with retrieval signals.

The development run should call both routes on the same episodes (provider data still hidden from
the requests) and construct a risk/coverage curve:

- **Coverage:** fraction of meetings handled by the compact route.
- **Conditional quality:** provider-chapter recall, candidate precision, F1, boundary error, and
  false `found`/`not_found` rates for those compact meetings.
- **Escalation risk features:** best and second-best score, lexical/TF-IDF disagreement, score
  margin, local transition-cue strength, number of agenda items without a plausible candidate,
  artifact/timing quality, agenda size, and measured full-context token budget.
- **Policy:** escalate the entire meeting when calibrated compact-failure risk exceeds the chosen
  threshold, when the compact packet has too many unresolved items, or when the full-context route
  is unavailable for a packet that exceeds Mistral's budget.  No packet is truncated to avoid an
  escalation.

The threshold should be selected from development to meet an explicit quality target (for example,
compact recall within a small, predeclared margin of full-context recall with a bounded upper
confidence limit on failure risk), then confirmed once on the held-out set.  Periodic full-context
canaries from meetings that would otherwise use compact retrieval are required to detect provider,
ASR, prompt, or agenda-template drift.  Until this curve exists, a deterministic “low score means
full context” rule would be guesswork and should remain research-only.  This is the standard
selective-prediction/reject-option framing: report quality as a risk-versus-coverage curve and
calibrate the abstention/escalation threshold rather than treating a raw model score as a universal
confidence value.  A recent context-adaptive abstention study is useful methodological background,
but its guarantees assume exchangeable calibration data and must not be claimed automatically for
our changing provider catalog:
[CAP](https://proceedings.mlr.press/v304/tayebati26a.html).

#### First supervised all-unit development result (2026-08-04)

The first bounded implementation is now in
[`scripts/research/agenda_chapters/train_transition_scorer.py`](../scripts/research/agenda_chapters/train_transition_scorer.py).
This run uses strong provider-chapter crosswalk relationships only as development labels: for a
linked agenda item, timed units within 30 seconds of the provider-supplied chapter start are
positive examples, and sampled units are negatives. Provider starts, provider titles, and the
crosswalk status are never model features. At evaluation time the model scores **all** timed
transcript units, not only the lexical/TF-IDF candidate union. That is the required test of whether
a learned scorer can recover transitions that deterministic retrieval did not propose. The
deterministic union is retained only as a separately measured baseline and as an optional combined
source of candidates.

The run used the 90 usable development episodes, with grouped validation stratified by provider
and body family (15 validation episodes, 75 training episodes), 104 strong provider chapters, and
102 unique strongly linked agenda candidates in validation. The validation slice is intentionally
hard and is not the held-out estimate. At top-10 selection, with a 60-second scoring tolerance:

| Selection | Provider chapters | Linked candidates | Chapters found outside deterministic union |
| --- | ---: | ---: | ---: |
| Existing lexical/TF-IDF union (radius 2) | 69/104 (66.4%) | 68/102 (66.7%) | — |
| LogisticRegression, all timed units | 57/104 (54.8%) | 56/102 (54.9%) | 9 |
| HistGradientBoosting, all timed units | 60/104 (57.7%) | 59/102 (57.8%) | 9 |
| Existing union **plus** HistGradientBoosting | 78/104 (75.0%) | 77/102 (75.5%) | 9 |

The learned models are not a replacement for the deterministic union on this first slice, but the
combined result demonstrates the intended recovery shape: the all-unit scorer found nine chapter
transitions outside the deterministic top-10 union, and adding those selections raised validation
recall from 66.4% to 75.0%. This is preliminary evidence only; it is one grouped validation slice,
uses crosswalk-derived labels rather than a fully adjudicated gold set, and has not touched the
96-episode held-out test.

#### Ranking and local-change follow-up (2026-08-04)

The next comparison was run on the same 15-episode provider-stratified validation slice, with the
same 104 provider chapters and 102 linked candidates. It added adjacent-unit token novelty and
local-change features (`previous_unit_novelty`, `next_unit_novelty`, and their peak/mean), and
trained a pairwise LogisticRegression ranker from positive-versus-negative unit comparisons for
each agenda item. The pairwise experiment used at most ten sampled comparisons per item so it
would remain a bounded research run.

| Selection | Provider chapters | Chapters found outside deterministic union |
| --- | ---: | ---: |
| Existing lexical/TF-IDF union (radius 2) | 69/104 (66.4%) | — |
| Original-feature HistGradientBoosting | 60/104 (57.7%); union 78/104 (75.0%) | 9 |
| + adjacent-unit novelty features, HistGradientBoosting | 59/104 (56.7%); union 77/104 (74.0%) | 8 |
| Pairwise LogisticRegression + novelty features | 52/104 (50.0%); union 70/104 (67.3%) | 1 |

This is a negative result for the added complexity on the current cohort: local token novelty did
not improve the learned scorer, and the simple pairwise objective was not competitive with the
pointwise HistGradientBoosting model. Keep the pairwise implementation as a reproducible research
option, but do not include either variant in the candidate admission path yet. The current best
research combination remains the original-feature HistGradientBoosting scorer unioned with the
deterministic candidates. No held-out episode was read.

#### First development risk/coverage diagnostic (2026-08-04)

`analyze_transition_risk.py` now consumes the scorer's per-item diagnostics instead of reducing the
validation slice to one aggregate. On the same 15 validation episodes, the novelty-feature
HistGradientBoosting run had 102 linked candidates and 58 top-10 candidate hits. Margin is a useful
ordering signal but not a production confidence value:

| Minimum top-vs-second margin | Items retained | Item coverage | Conditional hit rate | Whole episodes with every item retained |
| ---: | ---: | ---: | ---: | ---: |
| 0.000 | 102/102 | 100.0% | 56.9% | 15/15 |
| 0.001 | 91/102 | 89.2% | 61.5% | 8/15 |
| 0.005 | 72/102 | 70.6% | 66.7% | 4/15 |
| 0.010 | 53/102 | 52.0% | 71.7% | 3/15 |
| 0.050 | 19/102 | 18.6% | 94.7% | 2/15 |

Using top probability instead, a threshold of 0.7 retained 95/102 items, all 58 hits in this slice,
and all items in 9/15 episodes; that apparent stability is only one development split and is not
calibration evidence. The diagnostic supports a reject/escalate design, but it also exposes a
separate problem: the all-unit scorer sometimes assigns the same late transcript unit to many
agenda items. Thresholding alone cannot repair that temporal collapse. We therefore need a temporal
reconciliation/change-point pass and per-meeting unresolved-item policy before selecting a compact
versus full-context route threshold. No held-out episode was read.

#### Distinct-unit reconciliation result (2026-08-04)

To test whether the repeated-late-unit failure could be repaired cheaply, the scorer now has an
order-neutral greedy assignment diagnostic. It considers each item's top 50 learned units and gives
each unit to at most one item; it does **not** force agenda order, forbid skips, or assume revisits
are impossible. On the same validation slice, HistGradientBoosting's distinct assignment found
51/104 provider chapters (49.0%) and 50/102 linked candidates (49.0%). This is effectively its
top-1 result and is below the independent top-10 learned result (59/104 chapters, 58/102
candidates) and the deterministic-plus-learned union (77/104 chapters, 76/102 candidates).

The diversity constraint can prevent duplicate anchors, but it does not recover the missing
transitions. Keep it as a diagnostic for future temporal decoders, not as the current locator
policy. A useful temporal model will need local transition evidence or a stateful agenda tracker,
not only one-to-one assignment. No held-out episode was read.

#### Word-timed speech-rate vector extension (implemented research-only, 2026-08-04)

The scorer now has an opt-in speech-rate feature family in
[`train_transition_scorer.py`](../scripts/research/agenda_chapters/train_transition_scorer.py). It
uses the existing ASR word sidecar only; VTT-only rows receive an explicit unavailable mask and
zero-valued vectors. For every candidate unit it samples words-per-second in one-second bins over
the fixed `-30..+30` second window, applies a light five-bin moving-average smoother, and robustly
normalizes the vector using the episode's positive-bin median and MAD-derived scale. The optional
finite-difference vector is computed after smoothing. The serialized feature family also retains
`speech_rate_available`, the reference median, and the reference scale so normalization does not
hide whether a meeting is intrinsically fast or slow. The CLI modes are `none` (the unchanged
default), `vector`, `derivative`, and `both`; no provider timestamp or title is a runtime feature.

This representation deliberately avoids hand-selected pre/post intervals. The fixed window and
binning are experimental representation choices, while the classifier learns which rate shapes
and derivatives are useful. Provider starts remain development-only labels: the feature is never
given the marker around which the vector was measured at runtime. The implementation precomputes
one vector per timed unit so the all-unit research sweep remains practical.

The first ablation used the corrected served-v3 development manifest: 83 usable episodes (68
training, 15 grouped validation), 95 strong provider chapters, and 94 linked agenda candidates in
validation. The held-out 96 episodes were not read. HistGradientBoosting results below are
provider-chapter recall at a 60-second scoring tolerance; the deterministic union is the existing
top-k baseline, and the learned rows score all timed units.

| Selection | Top-1 | Top-3 | Top-5 | Top-10 |
| --- | ---: | ---: | ---: | ---: |
| Deterministic lexical/TF-IDF union | .674 | .779 | .811 | .842 |
| Existing-feature HistGradientBoosting | .621 | .695 | .705 | .737 |
| + normalized rate vector | .611 | .695 | .715 | .726 |
| + rate derivative | .632 | .684 | .695 | .737 |
| + vector and derivative | **.642** | **.716** | **.726** | .737 |

The vector-plus-derivative family improves the existing learned scorer by roughly two percentage
points at top-1, top-3, and top-5, while tying it at top-10. It still does not replace the
deterministic union, and this is one grouped development split rather than a held-out estimate.
The deterministic-plus-learned union scores higher because it increases the candidate set; those
figures are retained in the artifacts but are not treated as an equal-budget improvement. The
current result supports keeping the feature family for further development, especially in a
future temporal/change-point model, but does not justify production admission.

Research outputs:

- `/private/tmp/transition-scorer-speech-vector-v1.json`
- `/private/tmp/transition-scorer-speech-derivative-v1.json`
- `/private/tmp/transition-scorer-speech-both-v1.json`

Next evaluation should freeze the representation and compare its candidate-side recall and
false-positive behavior against the same development baseline before any final retraining. The
held-out cohort remains untouched until the feature family and compact/full escalation policy are
frozen.

#### Learned transition-word/phrase map extension (research-only, 2026-08-04)

The scorer now has a separate `--transition-phrase-mode learned` feature family. It learns
1--3-gram terms from the timed transcript around strong provider chapter starts in the training
fold only; agenda titles and hidden provider labels are not runtime inputs. To avoid the obvious
failure mode where the text of the newly opened item overwhelms reusable transition language, a
timed unit's positive contribution decays exponentially with distance from the nearest known
start (default decay constant: 8 seconds), and evidence after the start is weighted at 0.35. Each
term's positive and background rates are then aggregated per episode before log-odds fitting. A
minimum number of training episodes containing a term (`--transition-phrase-min-positive-episodes`)
and a bounded vocabulary prevent a single verbose or topic-specific meeting from defining the map.
The per-unit features are compact statistics (mean/max log odds, positive/negative mass, matched
term count, and availability), not the provider timestamp itself. This is the word/phrase
optimizer discussed after the unweighted ±30-second prototype; the prototype's content-heavy
phrases confirmed why distance and per-episode weighting are necessary.

The matched ablation below used the corrected served-v3 **development** manifest with the Austin
Electric Utility Commission checkpoint episode `e5afbf9795c9f4b2` excluded before artifact loading,
folding, and validation. It therefore has 82 usable episodes (68 training, 14 grouped validation),
88 strong provider chapters, and 87 linked generated candidates in validation. The held-out 96
episodes and the Austin checkpoint were not read by the learner. HistGradientBoosting values are
provider-chapter recall at the existing 60-second scoring tolerance; learned rows score all timed
units, while the deterministic row is the existing lexical/TF-IDF union at the same top-k.

| Selection | Top-1 | Top-3 | Top-5 | Top-10 |
| --- | ---: | ---: | ---: | ---: |
| Deterministic lexical/TF-IDF union | .705 | .807 | .818 | .852 |
| Existing-feature HistGradientBoosting | .659 | .727 | .727 | .750 |
| + normalized speech-rate vector | .659 | .716 | .739 | .739 |
| + vector and derivative | .693 | .739 | .739 | .750 |
| + weighted phrase map | .659 | .705 | .727 | .739 |
| + weighted phrase map and vector+derivative | .670 | .727 | **.761** | **.773** |
| + same, phrase support ≥5 training episodes | **.705** | **.750** | **.761** | **.773** |

The phrase map by itself does not beat the existing learned scorer. When combined with the speech
vector and derivative, it improves top-5/top-10 learned recall by about 2.3 points over the exact
speech-vector-plus-derivative control; requiring a term to appear near a boundary in at least five
training episodes improves top-1/top-3 as well. At the larger deterministic-plus-learned union
budget, the support-5 combination reaches .841/.932/.932/.955 at top-1/3/5/10 versus
.818/.932/.932/.955 for speech vector+derivative without phrase features. These union figures are
candidate-set coverage, not equal-budget classifier improvements.

The learned map's strongest terms include reusable cues such as `next item`, `agenda`, `go item`,
`approval`, and `consent agenda`, but also a few source/procedural or identifier-like terms (for
example recording notices and numbered subitems). Those are retained as diagnostics rather than
accepted production vocabulary; a later map cleanup can require body/provider diversity or remove
numeric terms without changing the frozen test. The result supports carrying the weighted phrase
family into the next development ablation, but does not justify production admission or a blind
held-out run yet.

Research outputs (all Austin-excluded):

- `/private/tmp/transition-scorer-excl-baseline-v1.json`
- `/private/tmp/transition-scorer-excl-speech-vector-v1.json`
- `/private/tmp/transition-scorer-excl-speech-both-v1.json`
- `/private/tmp/transition-scorer-excl-phrase-weighted-v1.json`
- `/private/tmp/transition-scorer-excl-phrase-weighted-speech-both-v1.json`
- `/private/tmp/transition-scorer-excl-phrase-weighted-min5-speech-both-v1.json`

The Austin UID exclusion is recorded in each artifact's `excluded_uids` field. The next checkpoint
can therefore reuse the Austin episode with a selected model/prompt without training leakage.

#### Development compact/full packet construction (2026-08-04)

`build_locator_packets.py` now constructs paired requests for the 15 provider-stratified validation
episodes. Each route receives the same source-grounded agenda items and the same locator contract;
only the supplied timed transcript units differ:

| Route | Median input tokens | P90 input tokens | Maximum input tokens | Hidden provider-chapter recall |
| --- | ---: | ---: | ---: | ---: |
| Full transcript | 50,121 | 185,087 | 224,527 | 98/104 (94.2%) |
| Deterministic compact union | 12,608 | 18,379 | 30,558 | 68/104 (65.4%) |
| Deterministic + learned compact union | 13,356 | 19,038 | 31,707 | 76/104 (73.1%) |

The learned supplement raises hidden retrieval recall by eight chapters over the deterministic
compact route while reducing median input tokens by about 73% versus full context. This is still
only retrieval coverage; it does not predict what an LLM will recover from the packets. The packet
manifest explicitly records `provider_labels_in_requests: false`, and the hidden score section is
not serialized into any request message. One long meeting remains a deliberate escalation case:
both compact routes have zero hidden retrieval hits while its full packet has non-zero coverage.

`run_locator_packet_shadow.py` is ready to submit paired full/learned-compact requests and validate
the returned unit IDs. Held-out episodes remain untouched.

#### First paired locator shadow call (2026-08-04; clock-corrected rerun)

After elevated Keychain access was restored, one representative validation episode was run through
both routes. Both requests used `mistral/mistral-large-latest`; the agenda items had been extracted
earlier with Mistral Medium 2508, and the learned compact supplement came from the development-only
HistGradientBoosting scorer. Neither route used Gemini because both packets fit the Mistral budget.

The meeting had seven strongly linked provider chapters. On the clock-corrected packet, the full
route returned six anchors and matched one of those seven starts within the 60-second scoring
tolerance. The compact retry returned a duplicate unit ID (`u00302`) and was rejected by the
structured-output validator, so it has no valid score in this rerun; the earlier 2/7 compact score
was from the source/served-mixed version-2 packet and is retired. Additional returned anchors in
the full route referred to agenda items with no strong provider crosswalk, so they are **unmatched**,
not confirmed false positives, until a separate human adjudication establishes whether they
represent legitimate provider omissions.

This is an important first warning, not a route verdict: full context did not automatically win, and
both routes sometimes followed a repeated spoken item number to the wrong occurrence. The sample is
one meeting, so it is insufficient for prompt or threshold decisions. The next run should expand
the paired calls across the validation strata and retain the same distinction between confirmed
timing misses and unmatched/unlabeled agenda items. Held-out episodes remain untouched.

#### Single-meeting compact repair and DeepSeek comparison (2026-08-03)

The first Mistral compact response was rejected because it assigned unit `u00302` to more than one
agenda item. The packet itself was valid. The research runner now makes one explicit corrective
request when this occurs: use each supplied unit at most once, retain the strongest-supported item,
and omit an item rather than inventing a timestamp or silently deduplicating the answer. The
repaired Mistral response returned seven validated anchors and matched 2/7 hidden served-clock
provider starts within 60 seconds. Its 18,799-token compact request remains directly comparable to
the original 18,825-token compact packet; the score is now valid rather than a failed run.

The same full and learned-compact packets were then sent to `deepseek/deepseek-v4-flash`. DeepSeek's
normal structured-output path failed its provider/Pydantic retry because the model used alternate
field names (`agenda_index`, `display_ref`) and omitted required fields. For this research-only
comparison, the runner therefore used plain JSON text plus the unchanged local strict validator,
with one exact-schema repair instruction. This does not relax the contract or change production
routing.

| Model / packet | Input tokens | Valid anchors | Provider-start hits (7) | Result |
| --- | ---: | ---: | ---: | --- |
| Mistral Large / full | 50,121 | 6 | 1/7 | baseline full route |
| Mistral Large / learned compact | 18,799 | 7 | 2/7 | one duplicate-unit repair |
| DeepSeek V4 Flash / full | 50,233 | 9 | 6/7 | one schema repair |
| DeepSeek V4 Flash / learned compact | 18,797 | 6 | 4/7 | one schema repair |

DeepSeek was materially slower on both packet sizes, but its corrected outputs were substantially
better on this one Austin meeting. The full route's six hits were the opening/public-comment,
contract, battery, overhead-study, and budget-related transitions plus one additional served-clock
boundary; the remaining miss was the late budget/adjournment mismatch. This is one meeting only and
does not justify replacing Mistral for production, but it is strong enough to warrant a larger
paired comparison before closing the model question. The DeepSeek shadow artifacts remain under
`/private/tmp` and are not production episode data.

As a separate reasoning ceiling, an independent Codex agent was given the same agenda and timed
transcript without the hidden scoring section and asked to select distinct starts manually. It
identified all seven strong target boundaries (7/7 within 60 seconds): call to order, public
communication, NewGen contract, battery agreement, annual review, overhead-resilience briefing,
and FY25/26 budget briefing. It intentionally omitted the minutes, HDR contract, easement, Mastec,
and adjournment as chapter starts. This is not an automated API route—the agent had time to inspect
the transcript and produce a reasoned adjudication—but it demonstrates that the ambiguity is not
intrinsic to the source. The production-shaped question remains whether a bounded model prompt and
validator can approach this reasoning quality without the agent's unrestricted review time.

#### Pooled versus retrieval-provenance compact packet A/B (2026-08-03)

The Austin meeting was rerun with the same 454 learned-compact transcript units, but the second
packet retained the item-to-unit retrieval associations that are lost when deterministic windows
are pooled. The first provenance encoding was needlessly verbose: 844 unit/item associations
serialized to 113,060 bytes because every association repeated long field names, null ranks, and
verbose reason strings. The research packet was corrected to compact entries of the form
`[agenda_item_index, {L/T/H: rank}, signals]`, omitting null and out-of-window ranks. The final
annotated request was 26,487 tokens versus 18,799 for the pooled Mistral control.

| Model / packet | Input tokens | Valid anchors | Provider-start hits (7) | Repair |
| --- | ---: | ---: | ---: | --- |
| Mistral Large / pooled compact control | 18,799 | 7 | 2/7 | duplicate-unit repair |
| Mistral Large / provenance compact | 26,487 | 7 | 1/7 | none |
| DeepSeek V4 Flash / pooled compact control | 18,797 | 6 | 4/7 | schema repair |
| DeepSeek V4 Flash / provenance compact | 26,599 | 7 | 5/7 | schema repair |

The deterministic compact pool itself covered 5/7 provider starts item-by-item, and the learned
plus deterministic pool covered 6/7. All seven target neighborhoods were present somewhere in the
pooled learned packet, so this A/B tests selection and item-to-unit association rather than merely
retrieval admission. Provenance helped DeepSeek by one hit but hurt Mistral by one; therefore the
metadata is not a universal fix and does not yet justify production prompt changes. The result does
confirm that the pooled packet is a harder selector task than the deterministic scorer: the model
does not receive the deterministic per-item score ordering unless we explicitly preserve it, and
even then it may over-trust noisy associations. The provenance representation and calls remain
research-only.

#### Direct top-k compact packet sweep (2026-08-03)

To test whether the packet was carrying unnecessary low-ranked alternatives, the same Austin
meeting was rerun with no neighbor expansion and a single direct-candidate cap applied consistently
to lexical, TF-IDF, and learned candidates. The earlier top-5 artifact had accidentally capped only
the lexical/TF-IDF routes while retaining ten learned candidates; that artifact is superseded and is
not included below.

| Model / packet | Units | Input tokens | Valid anchors | Provider-start hits (7) | Repair |
| --- | ---: | ---: | ---: | ---: | --- |
| Deterministic learned compact / top-5 | 82 | 5,337 | — | 6/7 hidden coverage | — |
| Deterministic learned compact / top-10 | 155 | 7,964 | — | 6/7 hidden coverage | — |
| Mistral Large / top-5 | 82 | 5,337 | 6 | 3/7 | none |
| Mistral Large / top-10 | 155 | 7,964 | 7 | 2/7 | none |
| DeepSeek V4 Flash / top-5 | 82 | 5,598 | 8 | 4/7 | schema repair |
| DeepSeek V4 Flash / top-10 | 155 | 8,226 | 8 | 4/7 | schema repair |

On this meeting, top-5 retained the same deterministic learned coverage as top-10 while cutting the
bounded packet from 155 to 82 transcript units. It also did not reduce DeepSeek's result and improved
Mistral by one hit, although the latter is ordinary single-meeting variance rather than evidence that
top-5 is generally more accurate. The result supports top-5 as the cheaper default candidate packet
for the next paired cohort, with top-10 retained as a held-out comparison until broader coverage and
false-positive measurements are available. These are research-only shadow calls; neither the packet
cap nor the model scores change production behavior.

#### Full-context Gemini Flash trial (2026-08-03)

The compact experiments establish a cost-saving retrieval hint, not a reliable semantic locator:
the hidden learned packet retained 6/7 target neighborhoods on the Austin meeting, while the
bounded models still assigned several neighborhoods to the wrong agenda item or omitted them. The
remaining non-full-context options—larger neighbor windows, more lexical/TF-IDF variants, and a
second local verifier—can improve admission or abstention, but none has yet shown that it resolves
the item-to-transition ambiguity. They remain useful only as soft hints until the full-context
benchmark establishes a safe precision gate.

Two explicitly experimental direct routes were added for that benchmark:
`gemini/gemini-3.5-flash` and `gemini/gemini-3.6-flash`. The local policy caps each at the currently
available 20 requests/day and marks them `experimental`, so ordinary pipeline scheduling cannot
consume the scarce pools. Google documents both model IDs with 1M-token input limits, so the
existing untruncated full-transcript-plus-agenda request is the intended comparison route.

The first Austin full-context smoke call was attempted before the AI Studio credential was
corrected, and Google's API rejected it with HTTP 401 (`ACCESS_TOKEN_TYPE_UNSUPPORTED`). The
corrected-key rerun and its confidence/error analysis are recorded below. The benchmark reports
both provider-start recall and the false-positive/abstention curve from returned confidence values,
with publication gated to the threshold whose confirmed wrong-match rate is below 5%.

#### Corrected-key Austin full-context rerun (2026-08-03)

The corrected local credentials were used to rerun the identical Austin Electric Utility
Commission packet (`e5afbf9795c9f4b2`) through all four requested full-context routes. The packet
contains the same 1,433 served-clock word units, the same Medium-generated agenda items, and the
same hidden scoring-only provider markers. No provider titles, starts, or hidden scores were sent
to any model.

| Model | Input tokens | Valid anchors | Strict strong-item hits (7) | Explicit item-reference errors | Provider-start timing disagreements |
| --- | ---: | ---: | ---: | ---: | ---: |
| Mistral Large | 50,121 | 7 | 1/7 | **at least 3** | 2 strong-item selections |
| DeepSeek V4 Flash | 50,233 | 7 | 5/7 | 0 observed | 1 strong-item selection |
| Gemini 3.5 Flash | 50,121 | 9 | 6/7 | 0 observed | 1 strong-item selection |
| Gemini 3.6 Flash | 50,121 | 9 | 6/7 | 0 observed | 1 strong-item selection |

The strict strong-item score is not a false-positive rate. It counts only the seven agenda items
that the source crosswalk independently labeled `strong`; it excludes the provider's separate
minutes and `Items 2, 4, & 6` markers because those relationships remain unmatched/composite. A
model selecting the minutes marker can therefore be a valid chapter even though it is absent from
the 7-item denominator.

Mistral produced three unambiguous item-reference errors: it selected the agenda item for number
four while quoting “number five,” selected the annual-review item while quoting number eight/the
overhead briefing, and selected the overhead item while quoting number nine/the budget briefing.
Its confidence values for those errors were 0.98, 0.98, and 0.98. At a `confidence >= 0.98`
threshold, four Mistral anchors remain and at least three are wrong by explicit reference (at
least 75% error among retained anchors); lowering the threshold retains more errors. This single
case provides no defensible Mistral publication threshold below 5%.

DeepSeek and both Gemini routes did not make an explicit item-reference error in this rerun. All
three selected the budget item at approximately 3,546 seconds, while the provider marker is at
3,831 seconds after a short technical recess. The transcript itself announces “number nine” at
3,546 seconds, so this is a provider-timing disagreement, not yet a confirmed wrong chapter. It
must be adjudicated as either an earlier legitimate transition or a timing error before it enters
the false-publication denominator. Gemini 3.5 also returned plausible call-to-order and adjournment
anchors that have no corresponding strong provider marker; these are likewise unconfirmed, not
automatic false positives.

The confidence outputs are not calibrated on this evidence: Mistral assigned 0.98 to explicit
reference errors, DeepSeek used 0.95/1.00 for both correct and timing-disputed anchors, Gemini 3.5
reported 0.95 for every anchor, and Gemini 3.6 reported 0.95/0.98. The next threshold experiment
therefore needs independently adjudicated `wrong`, `valid provider-omitted`, `composite`, and
`timing-disputed` labels. Until that set exists, raw model confidence and strict provider-start
misses must not be used to claim the under-5% publication-error requirement is met.

#### Corrected-key Z.AI GLM-4.7-Flash Austin rerun (2026-08-04)

The identical full-context Austin packet was submitted to Z.AI's OpenAI-compatible
`zai/glm-4.7-flash` route using the locally stored `ZAI_API_KEY`. The route is research-only,
free, and declares `concurrency=1`; it uses JSON-object mode plus the same local locator-schema
validation and one corrective retry. The request contained 1,433 transcript units and 50,237
estimated input tokens.

GLM-4.7-Flash returned 8 valid anchors after one schema repair and had a strict strong-item score
of **2/7**. It selected the valid call-to-order and annual-review neighborhoods, and also selected
the provider's minutes neighborhood (which is outside the seven-item strong denominator). The
remaining output showed several clear reference/timing problems: a public-communication quote was
paired with a unit at the end of public comment rather than its transition start; `number five`
was assigned to the easement agenda record instead of the battery item; `number three` was assigned
to the zero-based record for display item 2 instead of display item 3; `number seven` was assigned
to display item 6 instead of display item 7; and `number eight` was assigned to display item 7
instead of display item 8. The budget anchor again landed at the transcript's `number nine`
announcement around 3,546 seconds while the provider marker is at 3,831 seconds, so that remains a
timing disagreement rather than a confirmed semantic false positive.

Every returned confidence value was `1.0`, including the explicit reference errors. This single
case therefore provides no usable confidence threshold and is materially weaker than the
corrected-key DeepSeek and Gemini full-context runs on this meeting. The temporary artifact is
`/private/tmp/locator-packet-shadow-one-served-v4-zai-glm47flash-full.json`.

Temporary rerun artifacts:

- `/private/tmp/locator-packet-shadow-one-served-v4-mistral-full.json`
- `/private/tmp/locator-packet-shadow-one-served-v4-deepseek-full.json`
- `/private/tmp/locator-packet-shadow-one-served-v4-gemini35-full.json`
- `/private/tmp/locator-packet-shadow-one-served-v4-gemini36-full.json`

#### Additional Gemini route Austin reruns (2026-08-04)

The same full-context packet was submitted sequentially to the two existing lower-tier Gemini
routes so their results could be compared without concurrent account traffic.

`gemini/gemini-3-flash-preview` returned 9 valid anchors from 1,433 units and 50,270 estimated
input tokens after one duplicate-unit repair (`u00269`). It scored **6/7** on strict strong-item
provider starts. Its item assignments matched the transcript's display-number references for the
strong items; it also returned plausible call-order and adjournment anchors that are not in the
strong provider denominator. As with the other Gemini full-context runs, every returned confidence
was `1.0`, so confidence is not calibrated by this case.

`gemini/gemini-3.1-flash-lite` returned 7 valid anchors from 1,433 units and 50,121 estimated
input tokens with no repair, but scored only **1/7**. It selected the valid minutes neighborhood
(outside the strict denominator) and the overhead-distribution neighborhood. It assigned the
transcript's display item 3/5/7 announcements to the zero-based agenda records for display items
2/4/6, respectively—three explicit item-reference errors. Its budget selection again landed at
the transcript announcement near 3,546 seconds instead of the provider marker at 3,831 seconds,
which remains a timing disagreement. All seven confidences were `1.0`, including the explicit
reference errors.

Temporary artifacts:

- `/private/tmp/locator-packet-shadow-one-served-v4-gemini3-full.json`
- `/private/tmp/locator-packet-shadow-one-served-v4-gemini31lite-full.json`

#### OpenRouter Qwen Flash Austin rerun (2026-08-04)

The OpenRouter API catalog was queried before submission. It exposes `qwen/qwen3.7-flash` with a
1M-token context, but it does not expose either `qwen/qwen-flash` or `qwen-flash`; those are
QwenCloud/DashScope model names rather than OpenRouter routes. The exact `qwen-flash` request was
therefore not submitted through this key. A separate DashScope credential would be required to
test that model directly.

The identical Austin full-context packet was submitted to `openrouter/qwen/qwen3.7-flash` using
the new `OPENROUTER_API_KEY`. OpenRouter's Alibaba provider required the prompt to contain the
word `JSON` when using JSON-object response mode, so the research runner adds that provider-local
instruction and still validates the returned object against the existing locator contract.

Qwen3.7 Flash returned one valid anchor from 1,433 units and 50,165 estimated input tokens, with
no repair, scoring **0/7** on strict strong-item provider starts. The sole anchor was assigned to
the zero-based agenda record for display item 4 while quoting `number five`, another item-number
alignment error. Its confidence was `0.95`; this single case provides no usable confidence gate.

The temporary artifact is
`/private/tmp/locator-packet-shadow-one-served-v4-openrouter-qwen37flash-full.json`.

#### Gemini 2.5 Flash availability check (2026-08-03)

The same Austin full-context packet was also attempted with `gemini/gemini-2.5-flash`, because
its pricing is materially lower than the newer full-Flash routes. The local research runner first
rejected the route because it was not in the repository's supported-model table; that temporary
evaluation-only entry was removed after the live check. With the direct Gemini credential, the
request reached Google's API but returned HTTP 404:

`This model models/gemini-2.5-flash is no longer available to new users.`

The account's read-only model catalogue still lists `models/gemini-2.5-flash` and its generation
methods, so catalogue visibility does not imply that this account may generate with it. No locator
anchors or quality score were produced, and there is no Gemini 2.5 result to compare with the four
completed routes. The failed response is retained at
`/private/tmp/locator-packet-shadow-one-served-v4-gemini25-full.json`; this route should be retried
only if AI Studio changes the account's eligibility or exposes a versioned replacement.

#### Provider timestamp duration audit (2026-08-04)

The apparent out-of-duration starts were traced to a **research benchmark clock mismatch**, not a
provider timestamp defect. The dataset builder selected raw `source_chapters` (provider/source
clock) while comparing them with word-sidecar transcript units and `audio.duration_served` (served
clock). For example, Arlington UID `5661e9f07650d353` has a provider/source chapter at `9035s`,
but its persisted served chapter is `4656.31s` after the silence/consolidation timeline. The same
pattern maps Arlington `8d51…` source starts `9827/9836s` to served starts `9263.42/9272.42s`.
Those timestamps are valid on their respective clocks; they are not chapters after the meeting.

Therefore the six apparent full-packet misses, the 12-start out-of-duration count in the 104-start
slice, the broader 206-start audit, and the resulting 94.2% representation figure are **invalid
benchmark measurements** and must not be used as provider-quality or locator-quality findings.
`audit_chapters.py` now rebuilds from raw `source_chapters` plus the persisted timeline when both
are available, applying the same snap-aware policy; it falls back to the persisted served
`chapters` view for legacy records without enough timeline data. The confirmed chapter-specific
policy snaps a provider chapter start inside a removed source gap to the served start of the next
kept source span. A marker with no later kept audio is still dropped. Generic transcript/cue
remapping retains its existing drop-by-default behavior; only provider chapters opt into the
forward snap. The remap preserves the source chapter index for chapter identity and agenda
evidence, even when multiple source markers collapse onto one served boundary. Held-out data
remains untouched.

The policy-corrected research artifacts are version 3 of the frozen 192-episode cohort (96
development / 96 test). Rebuilding the served labels from raw source chapters plus each record's
timeline changed 45 rows and increased the provider-marker denominator from 2,499 to 2,554. The
corrected crosswalk has 1,752 strong, 113 ambiguous, 72 possible, and 617 unmatched provider
markers. The prior version-2 retrieval numbers must not be compared as if the denominator were
fixed: the new snap policy intentionally restores markers that the old drop policy discarded.

On the corrected 96-episode development split (1,161 strong provider markers), the high-recall
lexical/TF-IDF union reaches 81.9% at top-1, 89.1% at top-3, 90.9% at top-5, and 93.8% at top-10
(60-second tolerance). Lexical alone reaches 73.9% / 84.1% / 87.3% / 91.6%; TF-IDF reaches
76.3% / 85.9% / 87.6% / 90.3%. These are candidate-window recall figures before any LLM call,
not final chapter accuracy and not held-out results.

The phrase “near the known provider boundary” continues to mean an absolute difference of at most
**60 seconds between the served provider chapter start and the nearest timed transcript unit start**.
That is the evaluation scoring tolerance only; it is not a 60-second packet width or an assertion
that the locator is accurate to one minute. Training positive labels use a narrower 30-second
window, while packet representation and evaluation metrics use 60 seconds.

The earlier recovered-candidate comparison (24/60, 33/60, 36/60, and 37/60 union hits at top-1,
top-3, top-5, and top-10) was also run against the pre-snap labels and is retired with the other
version-2 metrics. The corrected crosswalk and retrieval output above are the only current
development measurements. Any newly recovered relationship still requires human adjudication
before it can be treated as a gold locator target.

The run also measured the untruncated full-context request.  Of 95 rows with at least one agenda
item, the recovered request had a median of 54,536 input tokens, a 90th percentile of 185,119,
and a maximum of 627,010.  Four rows exceeded the Mistral 256k input-plus-16,384-output reserve
and therefore select the existing Gemini overflow route; no text was cut to fit.  One valid
zero-yield episode had no full-context request.  These measurements validate the route-selection
mechanism but do not yet establish that full-context LLM anchors outperform deterministic
retrieval.

This is still development evidence, not a production threshold or gold set.  The next gate is to
inspect the per-episode misses, choose the candidate packet size/neighbor policy on development,
then freeze those choices and run the 96-episode held-out test without changing the retrieval
rules.

#### Full-context retrieval-hint experiment (2026-08-03)

To test whether deterministic/scikit-learn work can improve a capable full-context locator without
hard-gating it, the identical Austin Electric Utility Commission packet was rerun with an optional
retrieval index appended to the request. The packet still contained all 1,433 served transcript
units and the complete agenda. For each agenda item, the index supplied up to three candidates from
each independent method: lexical, TF-IDF, logistic, HistGradientBoosting, and pairwise-logistic.
Each candidate contained only its supplied unit ID, served start, rank, and transcript text; it
contained no provider chapter labels, hidden scores, or claim that it was correct. The prompt told
the model to inspect these as optional checks, independently search the full transcript, reject
false candidates, select unsuggested units when appropriate, and omit undiscussed items.

This follows the useful part of retrieval-augmented-generation practice without turning retrieval
into a hard gate. [Corrective RAG](https://arxiv.org/abs/2401.15884) recommends evaluating and
filtering retrieved material before generation, while [Yoran et al.](https://arxiv.org/abs/2310.01558)
show that irrelevant retrieved passages can reduce accuracy unless the model is trained or prompted
to treat them as distractors. The [Lost in the Middle](https://arxiv.org/abs/2307.03172) results
also caution that a long transcript is not uniformly accessible; placing a small, clearly labeled
retrieval index at the end may exploit recency, but is not evidence that the model can reliably
search the middle of a meeting. We therefore retain the full transcript as the source of truth and
measure hint runs only as an A/B research variant.

The strict score below is provider-start recall for the seven independently `strong` provider
chapters in this one episode. It is not precision or a production error rate. `off-window` counts
anchors assigned to one of those seven agenda items whose selected unit is more than 60 seconds
from the provider marker. `verified wrong` is narrower: a manual check found an explicit wrong
item/section assignment or an unmistakable continuation rather than a transition. The recurring
budget anchor at 3,546 seconds is **timing-disputed**, not counted as wrong: the transcript clearly
says “number nine” there, while the provider marker is at 3,831 seconds after a technical recess.

| Model | Control hits | Hint hits | Hint off-window | Hint verified wrong | Result |
| --- | ---: | ---: | ---: | ---: | --- |
| Mistral Large | 1/7 | 1/7 | 2 | 3 | no gain; composite item-six, overhead/annual, and number-nine assignment errors |
| Mistral Medium 2508 | 3/7 | 3/7 | 2 | 2 | no gain; overhead/annual and number-nine assignment errors |
| Ministral 8B | 1/7 | 2/7 | 2 | 2 | small recall gain, but overhead/annual and number-nine errors |
| Mistral Small 4 (OpenRouter fallback) | 0/7 | 0/7 | 2 | 2 | unusable on this packet; direct Mistral API hit its rate limit |
| DeepSeek V4 Flash | 5/7 | **6/7** | 1 | 0 | best hint gain; budget remains timing-disputed |
| Gemini 3 Flash Preview | 6/7 | 6/7 | 1 | 0 | unchanged |
| Gemini 3.1 Flash-Lite | 1/7 | 4/7 | 2 | 0 | substantial recall gain, no confirmed wrong item in this run |
| Gemini 3.5 Flash | 6/7 | 6/7 | 1 | 0 | unchanged after one transient 503 retry |
| Gemini 3.6 Flash | 6/7 | 6/7 | 1 | 0 | unchanged after transient 503 retries |
| Z.AI GLM-4.7 Flash | 2/7 | 4/7 | 1 | 1 | recall gain; duplicated overhead evidence assigned to annual item |
| OpenRouter Qwen3.7 Flash | 0/7 | 4/7 | 1 | 0 | large recall gain; one local JSON/schema repair |

The additional routes used these research-only artifacts:

- Mistral Medium control: `/private/tmp/locator-austin-mistral-medium-full-control.json`
- Ministral 8B control/hint: `/private/tmp/locator-austin-ministral8-full-control.json` and
  `/private/tmp/locator-austin-ministral8-full-hints.json`
- Small 4 fallback control/hint: `/private/tmp/locator-austin-mistralsmall4-openrouter-full-control.json`
  and `/private/tmp/locator-austin-mistralsmall4-openrouter-full-hints.json`
- Hint reruns for the existing routes: `/private/tmp/locator-austin-*-full-hints*.json`

The result supports retaining retrieval hints as an optional full-context variant, especially for
DeepSeek and lower-tier Gemini/Qwen routes. It does **not** justify replacing full-context search
with the hints, exposing classifier scores to the model, or lowering the independent wrong-match
gate. The next model decision must be made on the frozen held-out cohort, with verified semantic
wrong matches and timing-disputed cases adjudicated separately.

#### Updated phrase-plus-speech classifier Austin checkpoint (2026-08-04)

To test whether the new transition features improve the LLM path, the Austin checkpoint was scored
after fitting on the other 82 usable development episodes. The checkpoint UID
`e5afbf9795c9f4b2` was excluded from fitting and validation, then scored only after the classifier
was frozen. The hint map contained the existing lexical and TF-IDF top-three candidates plus the
top-three candidates from the support-5 HistGradientBoosting scorer using both the weighted
transition-word/phrase map and the normalized speech-rate vector/derivative. The full transcript
remained in every request; hints were optional checks, not a gate. The packet had 1,433 units and
458 pooled hint units, with hidden retrieval coverage of 6/7 strong provider starts.

This is a targeted comparison against the previous hint experiment, not a new cohort estimate. It
uses only the updated HGB hint source rather than all of the earlier classifier variants, so it
isolates whether the new phrase-plus-speech scorer adds useful suggestions. The same seven strong
provider starts and 60-second scoring tolerance were used.

| Model | Valid anchors | Updated-hint hits | Repair | Notes |
| --- | ---: | ---: | --- | --- |
| DeepSeek V4 Flash | 10 | 5/7 | schema repair | below prior 6/7 hint result; no clear recall gain |
| Gemini 3.1 Flash Lite | 10 | 4/7 | none | same 4/7 as prior hint result |
| Z.AI GLM-4.7 Flash | 10 | 4/7 | schema repair | same 4/7 as prior hint result |
| OpenRouter GPT-OSS-120B | 6 | 1/7 | JSON/schema repair | first run; weak on this meeting |

The updated classifier therefore did not improve this single-meeting LLM checkpoint. DeepSeek's
output still found five strong starts, Gemini and GLM found four, and GPT-OSS found only one. The
responses continued to show the known ambiguity: repeated item-number announcements, composite
items, and the budget transition at approximately 3,546 seconds versus the provider marker at
3,831 seconds. Several Gemini/GLM assignments also paired a correct spoken number with the wrong
agenda record, so confidence remains uncalibrated and this must not be treated as a false-positive
rate. The result is useful as an anecdotal checkpoint only; it does not justify discarding the
new features or changing the production route.

Research artifacts:

- `/private/tmp/transition-scorer-austin-checkpoint-phrase-speech-v1.json`
- `/private/tmp/locator-packets-austin-phrase-speech-v1.json`
- `/private/tmp/locator-austin-phrase-speech-deepseek-full-hints.json`
- `/private/tmp/locator-austin-phrase-speech-gemini31lite-full-hints.json`
- `/private/tmp/locator-austin-phrase-speech-glm47flash-full-hints.json`
- `/private/tmp/locator-austin-phrase-speech-gptoss120b-full-hints.json`

The OpenRouter catalog verified `openai/gpt-oss-120b` with a 131,072-token context window; its
request fit without truncation. No provider labels or hidden scores were sent to any route.

#### Multi-episode hint-encoding comparison (2026-08-04)

The Austin-only hint result was too sparse to decide whether the three method-specific candidate
lists were helping or confusing the locator. A read-only follow-up therefore used eight additional
validation episodes from the same frozen provider-chapter cohort (three Granicus/Arlington, four
Swagit/Austin bodies, and one Swagit/Waco body; 38 strong provider starts total). Every request
contained the complete transcript and agenda. Provider starts remained hidden and were used only
after the response for scoring. The four full-context variants were:

1. **Control:** full transcript with no retrieval hints.
2. **Current buckets:** independent lexical, TF-IDF, and HGB top-three lists, each with a local
   rank. This is the prior experimental presentation.
3. **HGB-only:** one candidate list containing the classifier's top ten units.
4. **Pooled:** one deduplicated candidate list per agenda item containing direct lexical top ten,
   TF-IDF top ten, and HGB top ten units. Each entry carried only provenance (`learned`, `lexical`,
   or `tfidf`); it exposed no score or incomparable method rank. Neighbor expansion was set to
   zero for this full-transcript test because the transcript itself supplies surrounding context;
   the compact packet route continues to use neighbor expansion.

The pooled list was intentionally not a hard gate. Its prompt explicitly said that provenance is
not a vote or confidence score, that a unit can be selected at most once globally, and that the
complete transcript remains authoritative. The aggregate results below include successful retries
of duplicate-unit/schema responses.

The earlier draft of this table called one column **Verified wrong**. That label was too strong:
these were generated by an automated comparison to hidden provider starts, not by manual semantic
adjudication. The old count combined two different signals, so it is split here:

* **Same-item off-window** — the model assigned an anchor to an agenda item that has a known
  provider start, but the returned time was more than 60 seconds away. This is evidence of a
  timing disagreement, not proof that the chapter is semantically wrong; provider markers may
  use a different boundary convention, and served/source timing or recesses can matter.
* **Wrong-item near provider** — the returned anchor was assigned to a different agenda item but
  landed within 60 seconds of a known provider start. This is the stronger automated signal of a
  bad item assignment, but it still needs review when adjacent items or agenda indexing are
  ambiguous. Where both conditions applied, the anchor is counted in this stronger category.
* **Unverified** — the assigned item has no known strong provider start nearby. It is not counted
  as wrong: it may be a valid provider chapter outside the strong subset, a procedural item, or
  an item that was skipped/withdrawn.

| Variant | Provider-start hits | Candidate hits | Returned anchors | Same-item off-window | Wrong-item near provider | Unverified |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Control | 29/38 (76.3%) | 28/37 (75.7%) | 76 | 4 | 16 | 28 |
| Current method buckets | 32/38 (84.2%) | 31/37 (83.8%) | 72 | 3 | 10 | 28 |
| HGB-only | 27/38 (71.1%) | 26/37 (70.3%) | 59 | 4 | 18 | 11 |
| **Pooled deduplicated** | **34/38 (89.5%)** | **33/37 (89.2%)** | 77 | 2 | 13 | 29 |

For continuity, the former “verified wrong” totals were simply the sum of the two middle
categories: 20, 13, 22, and 15 respectively. They should be read as **provider-discordant
proxy counts**, not publication false-positive counts. No confidence threshold was applied, and
the provider's strong-chapter subset is not a complete gold set. A final publication error rate
still requires manual adjudication of the suspected assignments plus a held-out confidence
threshold evaluation.

The locator does return a numeric `confidence` in the validated anchor contract, and the research
artifact preserves it. It is a self-reported model score, not a calibrated probability. In the
pooled slice the median was 0.98 and 36 of 77 anchors were exactly 1.0; seven of the 13
wrong-item-near-provider anchors scored at least 0.90 (six scored at least 0.98), and both
same-item off-window anchors scored at least 0.93. The other full-context probes show the same
saturation: Mistral gave 0.98 to explicit item-reference errors, GLM gave 1.0 to its explicit
errors, and Gemini routes commonly gave 0.95 or 1.0 to every returned anchor. Confidence can be
used as an input to a later, model/prompt-specific calibration fitted on adjudicated development
episodes, but a raw cutoff is not currently a safe publication filter.

This slice favors the pooled representation for recall and rejects HGB-only hints as too narrow.
The current buckets did improve over control, but the pooled list found two more provider starts
without a material verified-precision advantage. The result supports removing incomparable
method-specific rank lists from the prompt and using a deduplicated provenance-only shortlist.
It remains development evidence: the sample is provider/body concentrated, the provider-discordant
proxy rate is far above the eventual publication target before downstream confidence filtering,
and the 96-episode held-out test is still required.

The implementation is research-only in `run_locator_hint_ab.py`; `run_locator_packet_shadow.py`
now supports the grouped and merged hint instruction styles and passes an opt-in DeepSeek V4
non-thinking request body ([DeepSeek thinking-mode API documentation](https://api-docs.deepseek.com/guides/thinking_mode)). DeepSeek V4 Flash defaults to thinking mode; without the documented
toggle, its output budget was consumed by `reasoning_content` and no locator JSON was emitted.
The direct API probe also showed that `mistral-medium-2508` currently rejects prompts above
131,072 tokens, despite the earlier 256k planning assumption. The Mistral multi-episode attempt
was therefore stopped after a context-safe request remained in the synchronous transport for
more than six minutes; it contributes no quality measurements here and must be rerun only with a
provider-verified context budget/latency plan.

Research artifacts:

- `/private/tmp/locator-hint-ab-8-deepseek-v4-flash-combined-v1.json`
- `/private/tmp/locator-hint-ab-8-deepseek-v4-flash-v2.json`
- `/private/tmp/locator-hint-ab-retry-1e-hgb-v1.json`
- `/private/tmp/locator-hint-ab-retry-3edef-v1.json`
- `/private/tmp/locator-hint-ab-retry-e5-pooled-v1.json`
- `/private/tmp/locator-hint-ab-retry-f272-pooled-v1.json`

## Implementation sequence

### 2026-08-04 confidence-calibration and manual-adjudication slice

The next evaluation slice uses the frozen 96-episode test split, not the development rows used to
fit retrieval features. We selected 16 episodes (eight Granicus and eight Swagit), one distinct
body per provider, stratified across the available under-2-hour, 2-to-4-hour, and 4-to-8-hour
duration buckets. Every selected row has provider-supplied chapter starts in the hidden crosswalk,
the current Mistral Medium 2508 agenda extraction, and a usable transcript unit artifact. The
provider starts remain scoring-only labels and are never placed in model requests.

The locator prompt now asks for a decomposed, research-only confidence record: overall confidence,
item confidence, boundary confidence, evidence type, strongest alternative agenda item/unit, and a
short uncertainty reason. These are self-reports rather than probabilities; missing fields are
recorded explicitly and do not get silently filled. The calibration prompt does not request hidden
chain-of-thought. The four-model comparison is:

* DeepSeek V4 Flash;
* Gemini 3.1 Flash Lite;
* Gemini 3.5 Flash Lite; and
* Z.AI GLM-4.7 Flash (one-at-a-time route).

The Gemini Lite models have the maintainer's 500-request/day AI Studio allowance. That daily quota
is separate from the live free-tier input-token-per-minute ceiling observed during this run: the
route returned a 250,000-input-token/minute limit for both models. Requests were therefore started
about 65 seconds apart and automatic schema retries were disabled for this comparison. This is a
throughput guard, not a claim that the daily request quota is 20; Google documents that project
limits are model- and tier-specific at [Gemini API rate limits](https://ai.google.dev/gemini-api/docs/rate-limits).

For adjudication, proposals are deduplicated by the source evidence reference (episode, generated
agenda item index, and timed transcript unit ID), not by the model's summarized title. The final
four-model packet contains 201 such evidence-reference cases. A single row can therefore show
several model proposals while the reviewer labels the evidence once. The
reviewer must first choose `supported`, `no_evidence`, or `ambiguous`. For `supported` evidence,
the two independent fields are required:

* `item_correctness`: `correct` or `incorrect`;
* `boundary_validity`: `valid`, `invalid`, or `no_boundary`.

This preserves all four useful combinations (correct/valid, correct/invalid, incorrect/valid,
incorrect/invalid or no-boundary) and does not force a false item/boundary judgment when there is no
usable evidence. Comments are retained per evidence reference. The packet intentionally hides
provider targets, model identities, and confidence scores in the review UI to avoid anchoring; the
raw proposal diagnostics remain in the packet for later model-specific calibration analysis.

Research artifacts for this slice are kept under `/private/tmp`:

* `locator-calibration-cohort-16.json` — selection and strata;
* `locator-calibration-results-deepseek-v1.json`;
* `locator-calibration-results-gemini31-flash-lite-v2.json`;
* `locator-calibration-results-gemini35-flash-lite-v1.json`;
* `locator-calibration-glm-probe.json` — initial bounded route-availability probe;
* `locator-calibration-results-glm47-flash-patient.json` — completed slow serial GLM cohort; and
* `locator-calibration-review-packet-v2.json` — final four-model review packet.

The localhost review tool is `serve_locator_calibration_review.py`. It writes only a separate
decision JSON file and has no production or episode-record side effects. After manual review, the
adjudicated rows are retained as a held-out benchmark for model/prompt-specific confidence
calibration and publication false-positive estimation. Fit any calibration mapping on a separate
development adjudication set; raw self-reported confidence will not be used as a cutoff.

Run status at this checkpoint: DeepSeek returned 119 anchors, Gemini 3.1 Flash Lite returned 81,
Gemini 3.5 Flash Lite returned 116, and GLM-4.7 Flash returned 51, with all 16 episodes completed
for each. Gemini 3.1 omitted the optional boundary field on all returned anchors and the evidence
type on 46; Gemini 3.5 omitted the boundary field on 52 and evidence type on 19. Those omissions
are recorded as missing rather than inferred. GLM required the patient one-at-a-time runner, with
some full-context requests taking several minutes; no parallel requests or retries were used.

#### Initial adjudication result

The 201-case packet was fully adjudicated. All rows were marked `supported`; 188 were `correct`
item + `valid` boundary, seven were `correct` item + `invalid` boundary, four were `incorrect`
item + `valid` boundary, and two were `incorrect` item + `invalid` boundary. Thus, using the
strict provisional admission rule of publishing only `correct` + `valid`, 188/201 (93.5%) of the
deduplicated evidence references were publishable in this slice. The 13 rejected rows are not all
the same failure: reviewer comments identify late/early boundaries, a boundary in the middle of
an item, and adjacent-item or pre-item assignments.

Because rows are deduplicated by evidence reference, model counts overlap and are not independent
samples. Per-model proposal outcomes were:

| Model | Proposals | Correct + valid | Non-admitted | Non-admitted rate |
| --- | ---: | ---: | ---: | ---: |
| DeepSeek V4 Flash | 119 | 111 | 8 | 6.7% |
| Gemini 3.1 Flash Lite | 81 | 80 | 1 | 1.2% |
| Gemini 3.5 Flash Lite | 116 | 115 | 1 | 0.9% |
| GLM-4.7 Flash | 51 | 47 | 4 | 7.8% |

These are human-adjudicated evidence/item/boundary outcomes, not provider-chapter recall. They
also are not a production precision estimate: the cohort has only 16 meetings, model proposals
overlap, and the packet was selected for a locator stress slice. The comments reinforce that
timing placement—not title faithfulness—is the dominant remaining error mode. Several notes also
flag body/title metadata mismatches in the source rows; those are dataset/provider diagnostics and
not locator judgments.

Exploratory confidence thresholding is informative but not yet a gate. On this small slice, a
joint-confidence cutoff of 0.90 left 4.0% non-admitted DeepSeek proposals and 0% non-admitted
Gemini 3.1 proposals, while Gemini 3.5's one bad row was still scored 0.98 and GLM retained one bad
row at 1.0. This confirms that raw self-reported confidence is model-specific and can remain
overconfident; fit calibration on a separate development adjudication set and validate it on a
new blinded cohort before using it to target the under-5% publication-error requirement. This
16-meeting held-out result should remain an evaluation checkpoint, not a tuning set.

1. **Completed (Phase 0).** Add a pure locator-unit builder and offline benchmark
   selector/reporting. It reads existing transcript/agenda/chapter artifacts and makes no model
   call or output mutation.
2. **Completed (Phase 0).** Add fixture-backed tests for VTT and word-sidecar unit construction,
   stable IDs, and canonical benchmark eligibility.
3. **Completed (Phase 0).** Run and inspect the baseline's real artifact sizes, cohort
   stratification, VTT fallback, and no-candidate artifact diagnosis.
4. **Completed (Phase 1 dataset freeze).** `build_locator_dataset.py` freezes the immutable
   96-development/96-test provider-chapter manifest as separate input, hidden-gold, and
   diagnostics artifacts. The first public-URL inventory found 157 complete artifacts and 35
   non-admissions in the initial 192-row pool. A larger read-only pool was then used to replace
   those rows, producing a clean 192-row candidate manifest: 48 Granicus + 48 Swagit episodes on
   each side, with normalized body families kept on one side and 192 agenda hashes recorded. The
   larger pool excluded 39 viewer/loading placeholders, 17 empty artifacts, 6 unpublished
   placeholders, and 5 cap-suspected artifacts; it had no fetch failures. The earlier 20-row
   Mistral Large and 19-row DeepSeek outputs were from the pre-freeze shadow run and are retained
   only as diagnostic history; they are not the agenda input for this benchmark. The frozen
   extraction pass then ran **Mistral Medium 2508 with the final `agenda-flow` prompt on all 192
   rows**. All 192 raw requests completed; current-code source revalidation isolated rejected
   items after the post-processing step that expands evidence to a preceding identifier line
   before validating `display_ref`. 190 rows produced at least one accepted agenda item. Two rows
   produced no accepted items (one returned an empty item list; one returned hierarchical
   references whose evidence spans failed the source contract); both remain in the manifest as
   valid zero-yield diagnostics rather than being silently dropped. No locator model has been
   called.
5. **Completed (Phase 1 research).** Implemented and ran the read-only paired benchmark for the
   full-context request sizing, sparse lexical retrieval, episode-level TF-IDF similarity, and
   their high-recall union. The 96-episode development result is above; it scores candidate
   recall before any compact packet is sent to an LLM.
5a. **In progress (Phase 1).** Added `train_transition_scorer.py` and completed its first
   provider-stratified all-unit validation slice plus a bounded pairwise/local-change comparison.
   Provider chapter starts are labels only; the learned scorer ranks every timed unit and is
   measured both alone and as a union with deterministic candidates. The added variants did not
   improve the original-feature HistGradientBoosting baseline, so they remain research-only.
5b. **Completed (Phase 1 clock contract).** Provider chapter starts inside removed source spans
   now snap to the next kept served boundary; generic transcript/cue remapping still drops such
   starts by default. The change preserves source-index identity and has focused regression tests
   in `tests/test_timeline.py`, `tests/test_remap_stage.py`, `tests/test_tags.py`, and
   `tests/test_audit_chapters.py`. The benchmark gold builder is versioned with this policy so
   source-clock labels cannot be paired with served transcript units again.
5c. **Completed (Phase 1 research extension).** Added the opt-in word-timed speech-rate vector
   and derivative features described above. The default scorer mode remains unchanged; the
   corrected development ablation is recorded in the speech-rate section, and no held-out row or
   production artifact was changed.
5d. **Completed (Phase 1 research extension).** Added the opt-in training-fold transition
   word/phrase map with distance decay, post-boundary downweighting, and per-episode aggregation.
   Re-ran the speech-only, phrase-only, and combined ablations after excluding the Austin
   checkpoint UID `e5afbf9795c9f4b2` from both fitting and validation. The support-5 combined result
   is the current development candidate; no held-out row, Austin checkpoint result, or production
   artifact was changed.
5e. **Completed (Phase 1 checkpoint tooling).** Added scorer checkpoint diagnostics and packet
   selection so an excluded episode can be scored only after training. Re-ran the Austin full-
   transcript hint pass through DeepSeek V4 Flash, Gemini 3.1 Flash Lite, Z.AI GLM-4.7 Flash, and
   OpenRouter GPT-OSS-120B. This was a single anecdotal model check; no held-out row or production
   artifact was changed.
5f. **Completed (Phase 1 evaluation).** The 16-episode held-out confidence slice was run through
   DeepSeek V4 Flash, Gemini 3.1/3.5 Flash Lite, and GLM-4.7 Flash with the decomposed confidence
   prompt. The manual packet was deduplicated by source evidence reference and used independent
   evidence-status, item-correctness, and boundary-validity labels. Gemini 3.5 Flash Lite produced
   115 correct-item + valid-boundary proposals out of 116 (99.1% in this slice). Its one invalid
   boundary had confidence 0.98/0.98/0.95, and an adjacent valid proposal had the same complete
   feature signature. Raw self-reported confidence and the optional fields therefore do not show
   enough separation to justify fitting a calibration classifier from this one-error sample. No
   provider target was shown to the reviewer and no production artifact was changed.
### Approved production implementation plan (2026-08-05)

The 16-episode Gemini 3.5 Flash Lite result is sufficient to move from confidence research to
implementation. The one observed failure has the same complete diagnostic signature as a valid
neighboring proposal, so we will not fit a confidence classifier to this slice. Confidence remains
useful for diagnostics and routing, but structural validation and explicit provenance are the
publication safeguards.

The current mixed worktree will be preserved locally on `wip/1078-research-full`. That branch is a
research archive and source of selectively reviewed changes; it is not a production dependency and
will not be pushed or used as the base of the four public PRs. Each implementation step below must
port only the relevant, tested pieces onto a clean branch. Temporary packets, model responses,
adjudication JSON, logs, and credentials remain outside Git.

1. **Define the generated-chapter record and provenance contract.** Add a versioned, generated-only
   record containing the stable episode UID; generated agenda-item index; concise title; optional
   display reference; immutable agenda source hash/line span and expanded `evidence_text`; selected
   transcript unit ID and start; served/source time basis; model/route and prompt versions; raw
   confidence diagnostics; and an admission status/reason. Generated records must be visibly
   distinct from provider chapters and must never overwrite canonical titles, dates, links, or
   provider timings. Use the locator contracts and tests in `wip/1078-research-full` as design
   references, then port only the stable schema to the production branch. Add schema, round-trip,
   missing-evidence, and provenance tests before wiring the pipeline.

2. **Implement an idempotent pre-`AudioStage` chapter stage.** The stage runs only after agenda
   extraction and transcription are complete, consumes their content-addressed artifacts, and
   persists a generated-chapter artifact before any audio bytes are rendered. It must preserve the
   served/source timeline basis, use the existing stage hashing/version conventions, be restartable
   under the wall-clock stop budget, and leave canonical/provider chapter records untouched. A
   re-run with identical inputs must produce the same request manifest and stable generated record
   identity. The WIP branch may contain packet runners and stage-shaped experiments, but the
   production stage must be implemented and reviewed independently on a clean branch.

3. **Implement structural admission, deduplication, and fallback behavior.** Reject or abstain when
   the model cites unavailable evidence, an invalid/expanded agenda span, a missing transcript unit,
   an invalid time basis, a non-monotonic or duplicate start, or an unusable title. Preserve valid
   `not_found` outcomes for skipped, withdrawn, or consent-subsumed agenda items rather than
   inventing chapters. Run the post-processing evidence expansion before `display_ref` validation.
   Treat raw confidence, missing optional fields, evidence type, alternatives, and retrieval scores
   as diagnostic/routing signals only; they are not a standalone publication gate. Route malformed,
   over-limit, or provider-failed requests to the documented fallback/abstention path and record the
   reason. Add focused tests for public hearings, consent agendas, hierarchical references,
   duplicate anchors, and source/served timestamp conversion. Reuse source-validation and recovery
   experiments from WIP only after each behavior is promoted explicitly; research shadow recovery
   must not silently become production acceptance.

4. **Run a no-publish shadow and blind validation.** Start with the full-context Gemini 3.5 Flash
   Lite route and the frozen provider-chapter test manifest; keep compact retrieval/hint variants
   research-only until they demonstrate comparable provider-chapter recall. Compare generated
   starts against hidden provider starts using separate agenda coverage, locator recall, strict
   correct-item + valid-boundary precision, boundary error, skipped-item false-creation, duplicate
   rate, abstention rate, token/cost, latency, and route-failure metrics. Run the benchmark tools
   from WIP against the clean production branches, with provider labels remaining scoring-only. Do
   not expose provider targets to the locator. The shadow report is the go/no-go gate for publication,
   not a confidence
   score selected after looking at the test results.

5. **Roll out gradually with provenance, monitoring, and backfill controls.** After the shadow gate,
   enable generated chapters for a bounded stream, retain the full request/response and admission
   audit trail, and periodically sample results across providers, bodies, duration, agenda quality,
   and meeting types. Track model/prompt drift, invalid-boundary rate, abstentions, cost, and route
   quotas; trigger review or fallback on regressions. Decide the embedded-marker versus served-time
   overlay representation before enabling any audio materialization, and document the pipeline
   version/backfill story. Backfill only after the rollout is reversible and canonical chapters are
   protected. Tagging remains a later stage that may consume admitted generated chapters. After
   rollout, WIP remains the place for backfill experiments and prompt/model comparisons; only a
   separately reviewed production PR may move a result into the pipeline.

After these five steps, the remaining decisions are limited to the shadow results and operational
choices below; none requires another one-error confidence-calibration experiment.

After the four cleanup PRs land on `main`, the repository will contain only: (a) the vetted timeline
and chapter-remapping correctness fix, (b) source-grounded agenda evidence validation/expansion,
(c) clearly labeled reusable research benchmark tooling and its tests, and (d) this durable plan and
README documentation. `main` will not yet contain the generated-chapter stage, Gemini 3.5 production
routing, compact-route admission policy, generated-chapter materialization, backfill, or research
outputs. Those are later implementation PRs that may use `wip/1078-research-full` for experiments but
must start from the cleaned `main` history.

## Open decisions

- **Routing intent:** Gemini 3.5 Flash Lite is the preferred chapter-timing selection model. It had
  the strongest result in the 16-meeting adjudicated slice (115/116 proposals were correct-item +
  valid-boundary). We will proceed with it as the preferred chapter-timing route. A normal fresh
  validation/shadow run is still required before publication, but a separate confidence-calibration
  classifier is not a prerequisite: the one observed failure has no distinct confidence/evidence
  signature and further fitting would overfit this slice. Mistral Large, DeepSeek V4 Flash, and
  GLM-4.7 Flash remain comparison/fallback routes.
- Tagging should not permanently consume Gemini 3.5 Flash Lite's free-tier pool merely as a
  throughput spillover. The current configuration still uses Gemini 3.1 Flash Lite as primary and
  Gemini 3.5 Flash Lite as spillover for compatibility; run a bounded tagging-quality comparison
  of GLM-4.7 Flash and smaller candidates before removing 3.5 from that lane.
- Confidence thresholds, tolerances, held-out sampling cadence, and the exact generated-chapter
  record shape are intentionally deferred to the shadow-evaluation slice. Confidence is a
  diagnostic/routing hint, not a calibrated probability or a standalone publication gate; the
  first implementation should rely on structural validation, evidence preservation, and a
  conservative fallback/abstention path.
- Whether a generated chapter should be embedded in audio or exposed as a served-time overlay is
  decided with the storage/admission design; the current pipeline invariant requires any embedded
  marker to be known before `AudioStage`.

### Remaining issues after the implementation plan

These are the remaining issues after the five implementation steps above; they are sequencing and
operational decisions, not reasons to reopen the one-error confidence study:

1. **Shadow go/no-go:** run the frozen provider-chapter test and set the boundary tolerance,
   admission rule, and publication-error target from its predeclared metrics. This is the final
   quality gate before generated chapters are exposed.
2. **Full versus compact routing:** full-context Gemini 3.5 is the initial production-shaped path.
   The compact retrieval/escalation curve remains a cost-reduction project; it must demonstrate
   comparable provider-chapter recall before replacing or screening the full route.
3. **Dispatch and quota wiring:** add or verify the production dispatch route for the pinned Gemini
   3.5 Flash Lite model, including model-specific rate limits, retries, request accounting, and a
   documented fallback when the route or context budget is unavailable.
4. **Tagging route:** complete the bounded GLM-4.7/smaller-model tagging comparison before moving
   tagging traffic off Gemini 3.1/3.5 Lite. This is independent of chapter-timing admission.
5. **Materialization and backfill:** choose embedded audio markers versus served-time chapter
   overlays, define the pipeline-version/backfill story, and make rollout reversible without
   touching canonical provider chapters.
6. **Source-quality dependency:** resolve the known AgendaViewer/OCR placeholder problem tracked
   in [GH#1092](https://github.com/BashfulBits/city-meeting-podcasts/issues/1092). It is not a locator
   model failure, but production agenda extraction must classify those artifacts before chaptering.
7. **Ongoing monitoring:** define the periodic human-QA sample, drift/invalid-boundary alert, quota
   budget, and rollback trigger for model or prompt changes. These can be finalized alongside the
   shadow rollout rather than through another calibration experiment.

The local WIP branch is intentionally the handoff mechanism for these remaining issues: it retains
the full benchmark runners, adjudication UIs, scorer variants, and exploratory route adapters, while
the public history receives only the pieces that have a clear reusable contract or a production
correctness justification.

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
