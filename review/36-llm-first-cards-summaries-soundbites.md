# review/36 — Cards, Summaries, Soundbites & Decisions: LLM-First, Scaffolding Deferred

## R6 implementation addendum — calibrated quotes and shareable video clips

This addendum supersedes the earlier “no standalone clip files” boundary. An admitted pull quote
now has a content-addressed MP4 projection when the source-media, grounding, caption, URL, and
resolution gates pass. The default output is a 9:16 social canvas with a native-resolution square
video pane, caption bands, and a `citymeetings.fyi` watermark; a quote remains text-admitted when
video rendering is unavailable.

Council feeds explicitly marked `meeting_family: council` use only `gemini/gemini-3.6-flash` followed
by `gemini/gemini-3.5-flash`; other families use the existing Lite routes. Every R6 call sets
`allow_paid=False`; quota exhaustion is deferred. The rollout is limited to council meetings and a
bounded shared generation/judge dispatch budget per run, so the two free Gemini account pools drain
gradually rather than selecting a paid route.

The R6 gate has three layers: exact transcript grounding and served-time derivation; deterministic
media/caption/duration/resolution safety; and a durable human/calibration policy. `Good` admits
immediately, `Borderline` retains a learning example, and `Reject` always suppresses. Automatic
admission is globally off initially and can qualify a calibration cell only after 30 days and 30
ranked examples, with at least 90% Good among score-threshold admissions plus positive and negative
support. Prompt/model/duration/framing changes create a new human-score cell, and manual decisions win.

Each configured independent judge (initially Llama, GLM-4.7, Gemini Flash, and GPT-OSS) scores the same
grounded candidate in the background; it never creates, repairs, or rewrites one. A weekly authenticated
GitHub Issue review packages candidates after a judge result is present and records exactly one immutable
`Good`, `Borderline`, or `Reject` decision plus any timing/title/caption/crop controls. The human decision
is then used to calibrate each judge's own model/prompt/schema cell independently. Once an individual
judge cell meets the same time, sample, support, and 90% precision rules, it may admit a technically safe
future candidate at its learned score threshold. This is deliberately not a Llama-versus-GLM tournament:
multiple judges may qualify, each may independently allow strong candidates, and a regression, model,
prompt, schema, caption, or framing revision resets only its affected cell. The global manual-only switch
remains an immediate kill switch.

The implementation lives in `citypods/moments.py`, `citypods/moment_evaluation.py`,
`citypods/moment_judging.py`, and `citypods/video_clips.py`, with extraction, judging, admission, and
video stages after transcript/timeline enrichment. Their stage fingerprints are separated so a manual
review or asynchronous judge result can change admission without re-running extraction. It records the
R6 candidate ledger separately from official text and keeps video keys in the orphan-GC live set.
The dedicated `moments.yml` producer sends the bounded shared extraction/judge allowance through the
v2 dispatch collector as one Worker ingress batch per run; durable handles/results remain finalized on
later lane passes or by the deferred sweep, so batching changes request shape without changing R6
candidate recipes, schemas, or calibration behavior.

**Producer failure correction (2026-09-04):** [run #13](https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/33922960526)
accepted all 14 batched jobs (nine replays), then polled five completed, five pending, and four
errors. The collector mislabeled those four result errors as submission failures and exited 1.
The log does not distinguish terminal execution failure from result-validation failure; it does
separately report four extraction recipes already blocked by the existing terminal-retry cap.
The fix keeps collector flushes admission-only, using the same bounded enqueue recovery and
chunking. Accepted handles stay durable for later readers/the deferred sweep, which already owns
schema correction and terminal-failure retirement. Actual enqueue exceptions still fail the run.
This shared collector contract applies to the other producer lanes too; direct
`dispatch_job_batch` callers retain their immediate reconciliation. No limits, recipes, retry caps,
or stored artifacts change, and no backfill or failure-marker reset is needed.

The OpenCV face/mouth-motion analyzer is pinned and versioned behind the framing recipe; it tracks a
confident active speaker, otherwise uses a stable group crop, honors a manual anchor, and never upscales
below the 720-pixel square-pane policy. Redirected media is first resolved through the SSRF gate before
ffprobe/ffmpeg is permitted to fetch it; HLS remains safely text-only until manifest segment validation
is implemented. `MOMENTS_PIPELINE_VERSION` and the video recipe are now `2`: existing records are
reprocessed gradually by the normal scheduled lanes, never bulk-invalidated.

**Maturity: L3 (dev-ready) · successor to [`review/30`](30-cards-summaries-soundbites.md) (now deprecated) ·
ROADMAP R6 (bundles #3/GH#155 cards, #2 auto-summaries, #15/GH#156 soundbites, plus a new decision/direction
target) · issues not yet cut**

> **Matured to L3, 2026-07-19.** Supersedes `review/30` after a research pass surveying prior art across
> journalism, meeting-summarization NLP, court/hearing transcript analysis, and social reading platforms
> (full source list in Part 2). That research produced two findings that change the shape of this item:
> (1) `review/30`'s non-LLM heuristics (a raw literal excerpt; "pick the longest chapter" for soundbites)
> aren't grounded in anything — real prior art exists and is much better, but building all of it
> (three trained classifiers, a dozen feature-extraction libraries, an acoustic pipeline) is a real
> engineering investment that shouldn't be paid speculatively; (2) a single long-context LLM call, asked
> directly for the same three things, is cheap to stand up and composes for free with the calibration
> matrix R5 already shipped. **This document therefore leads with the LLM-only implementation as the
> actual near-term build (Part 1), and keeps the researched scaffolding as a fully-designed but explicitly
> deferred fallback (Part 2)** — built later, per target, only if real calibration-review data says the
> LLM-only path isn't good enough for that target.

---

# Part 1 — Ship first: the LLM-only implementation

## §1. What this is

One combined, structured-output LLM call per episode, given the full transcript plus chapters and agenda
text, returning three things in one response: a per-chapter summary point, candidate pull quotes, and
candidate decision/direction snippets. No feature engineering, no trained classifiers, no acoustic
pipeline. It reuses R2's existing LLM dispatch infra and R5's existing calibration matrix exactly as built
— the only new work is a new structured contract, a small stage to call it, and grounding/rendering code
that already has direct precedent elsewhere in this codebase.

**Why this ships before anything in Part 2**: it's the cheapest possible way to find out whether the
elaborate scaffolding is even necessary. Every claim in Part 2 about what a trained classifier would add
over "just ask the LLM" is a hypothesis until there's real calibration-review data comparing the two. This
gets that data collected starting now instead of after months of scaffolding work.

## §2. Data model

New structured-output contract, registered the same way `tags.py::ensure_llm_contract()` registers
`"topic-tags"` — lazily, idempotently, via `register_response_model`:

```python
class SummaryPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chapter_id: str
    text: str = Field(min_length=1, max_length=400)

class PullQuoteCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quote: str = Field(min_length=3, max_length=400)
    chapter_id: str | None = None
    why: str = Field(min_length=1, max_length=300)

class DecisionCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chapter_id: str
    decision_type: Literal["approved", "denied", "deferred", "tabled", "no_decision", "unclear"]
    quote: str = Field(min_length=3, max_length=400)
    explanation: str = Field(min_length=1, max_length=300)

class MomentExtractionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary_points: list[SummaryPoint]
    pull_quotes: list[PullQuoteCandidate] = Field(default_factory=list, max_length=10)
    decisions: list[DecisionCandidate] = Field(default_factory=list, max_length=20)
```

**`summary_points` requires one entry per chapter** — the prompt supplies the full list of `chapter_id`s
and the schema is validated against that list post-response (missing or extra chapter IDs are rejected,
not silently accepted). This is a deliberate schema-level fix for a real failure mode: an unconstrained
freeform summary call skews toward the most "interesting" agenda items and can silently drop routine-but-
real ones. Enforcing one point per chapter in the contract itself gets whole-meeting coverage for free,
without needing a separate ranking step to enforce it (Part 2's scaffolded version enforces the same
constraint differently, via per-chapter classifier selection, §11).

**New `Episode` fields** (shadow-until-calibrated, mirroring `llm_tag_candidates`/`tags_llm_recipe_hash`
exactly):

- `moment_summary_candidates: list[dict] = field(default_factory=list)`
- `moment_pullquote_candidates: list[dict] = field(default_factory=list)`
- `moment_decision_candidates: list[dict] = field(default_factory=list)`
- `moments_llm_recipe_hash: str | None = None`

**New pipeline version constant**: `MOMENTS_PROMPT_VERSION = "1"`, independent of `TAG_PROMPT_VERSION` —
a prompt change here shouldn't force retagging, and vice versa.

**`ARTIFACT_BLOCKS` gains `"moments"`** — same reasoning as `"tags"`/`"agenda_text"` (`review/29` §5,
`review/30` §A.2 as originally written): without it, a scoped `transcribe`/`align`/`diarize` lane's
whole-record push would silently regress a freshly-extracted moment set.

## §3. Pipeline

New `MomentsStage` (or an addition to `TagsStage` — worth deciding at implementation time based on how
much dispatch-mode/stale-cache logic can be shared; `TagsStage`'s existing pattern, `citypods/stages.py`,
is the template either way):

1. Compute `moments_recipe = tag_recipe_hash`-style hash over `(taxonomy-independent: chapters, agenda
   text, transcript text, llm_route, MOMENTS_PROMPT_VERSION)` — same recipe-hash discipline as everywhere
   else in this codebase (§9), so a prompt version bump or route change correctly invalidates stale
   candidates.
2. If stale, dispatch one `InferenceJob(task="summarize", inputs={"scope": "moments", "chapters": [...],
   "agenda_text": ..., "transcript_text": ...}, recipe_hash=moments_recipe)` — reusing the `summarize`
   verb with a new mode, following the exact mode-aware-verb precedent R12's `classify-civic-platforms`
   and `review/30`'s own original card design already established, not inventing a fourth verb (the `Task`
   enum is a pre-1.0 lock per `citypods/compute/base.py`).
3. **Grounding check, non-negotiable**: every `PullQuoteCandidate.quote` and `DecisionCandidate.quote` is
   checked against the real transcript text via `_contains_text`/`_transcript_region` (promoted per §9)
   before being stored as a candidate at all. A quote that isn't a verbatim transcript substring is
   **dropped from the response entirely**, not down-ranked or flagged — there is no independent classifier
   cross-check in this phase to catch a hallucinated quote the way Part 2's redundant signals would, so
   this check is the whole safety net and has to be strict.
4. Store the (grounded) results in the three shadow fields; record `provider_model` using the **existing**
   convention already used for tag candidates — the real backend route string
   (`f"{backend.name}:{resolved_model}"`, e.g. `"litellm:gemini/gemini-3-flash-preview"`), not a synthetic
   label. `prompt_version` is `MOMENTS_PROMPT_VERSION`.
5. Run `visible_candidates()` (R5's existing admission check) per feature to determine what's currently
   calibrated-visible; everything else stays shadow data exactly like unqualified tag candidates do today.

## §4. R5 modifications needed now (a subset of `review/30`'s original scaffolding plan)

Only the generalization work is needed for Phase 1 — the ingestion-adapter work for external training
datasets (`review/30`'s original §4e) is entirely Part 2 scope, since there's no classifier to train yet.

- **Generalize R5's calibration matrix wiring** (`review/35` §9's already-flagged gap): `summary`,
  `pull-quote`, and `decision` each need their own row in a per-feature registry — `StageContext
  .llm_evaluation_state_path`/`llm_evaluation_config` become keyed by feature instead of singular, and
  `.github/workflows/llm-tag-review*.yml` take a feature parameter instead of being hardcoded to
  `"topic-tags"`. This was always going to be needed for a second LLM-assisted feature; it's needed now,
  for three.
- **Promote grounding helpers**: `tags.py`'s `_contains_text`/`_transcript_region`, alongside
  `_parse_words_payload`/`words_in_range` (`review/30` §0's already-flagged promotion), into a shared
  `citypods/transcript_words.py` — §3 step 3 needs these directly, don't reimplement them.
- **New parallel Episode fields** (§2) follow the exact shadow pattern `llm_tag_candidates` established.
- **Recipe-hash discipline** (§3 step 1) follows `TAGGER_VERSION`/`TAG_PROMPT_VERSION`'s exact precedent.

None of Part 2's tool matrix, feature dictionary, dataset landscape, or trained-classifier machinery is
needed to ship Phase 1.

## §5. Rendering into existing surfaces

- **Admitted `moment_summary_candidates`** render into the per-chapter card summary and the whole-episode
  `summary` field (`review/30` Part A/B's original rendering slots on the meeting page and in search) —
  those UI slots don't change, only what feeds them does.
- **Admitted `moment_pullquote_candidates`** render as callouts on the episode page and feed
  `extract_clip`/`<podcast:soundbite>` (`citypods/clips.py`, confirmed zero callers today — this is its
  first real consumer, same as `review/30`'s original soundbite plan intended).
- **Admitted `moment_decision_candidates`** render as the per-chapter "what was decided" text, replacing
  `review/30`'s original ungrounded `changed_summary` one-liner with something anchored to a real quote
  and an explicit decision-type label.

## §6. Tests

- Structured-contract registration is lazy and idempotent (mirrors `ensure_llm_contract`'s existing test).
- A response missing or adding a `chapter_id` in `summary_points` is rejected, not silently accepted.
- A quote that does not appear verbatim in the transcript is dropped before it ever reaches a shadow field
  — the grounding check actually discriminates (test both a real quote and a fabricated one).
- New shadow fields are populated but not calibration-visible until `visible_candidates()` admits them —
  same shape as the existing `llm_tag_candidates` shadow-until-admitted test.
- `ARTIFACT_BLOCKS`/`protected_blocks_for_lane`: `moments`-derived fields survive a scoped `transcribe`-
  lane whole-record push untouched — same test shape as `agenda_text`/`tags`.
- Stale-recipe invalidation: a `MOMENTS_PROMPT_VERSION` bump (or route change) invalidates cached
  candidates rather than silently reusing them — same bug class already found and fixed once for tags.

## §7. Proposed GitHub issues (Phase 1 — not filed, batch review pending)

1. Register the `"moment-extraction"` structured-output contract + `MOMENTS_PROMPT_VERSION` + the three new
   `Episode` shadow fields + `moments_llm_recipe_hash` + `ARTIFACT_BLOCKS["moments"]`.
2. Generalize R5's calibration wiring (`StageContext`, `citypods llm-evaluation` CLI, the two GitHub
   Actions workflows) to a per-feature registry, covering `summary`/`pull-quote`/`decision`.
3. Promote `_contains_text`/`_transcript_region` alongside `words_in_range` into
   `citypods/transcript_words.py`.
4. New `MomentsStage` (or `TagsStage` extension) implementing §3's dispatch/grounding/storage flow.
5. Wire admitted candidates into the three rendering surfaces (§5): meeting-page cards/summary, pull-quote
   callouts + `extract_clip`/`<podcast:soundbite>`, and per-chapter decision text.

## §8. What would actually trigger Part 2 — concretely, not vaguely

Per target, independently, using the calibration mechanism that already exists rather than a subjective
call: if, after a real batch of human-reviewed episodes, a given label (`summary`, `pull-quote`, or
`decision`) **fails to reach a qualified, precision-cleared threshold** in `review/35`'s matrix — or a
specific, recurring failure mode shows up in review (e.g., pull quotes that are technically grounded but
never actually compelling; systematically missed decisions the rule-based marker in Part 2 would have
caught) — build the corresponding piece of Part 2 **for that target only**. A weak pull-quote result
doesn't imply building the acoustic pipeline or the decision classifier too. This mirrors how the
calibration matrix already treats every `(feature, label)` cell independently.

---

# Part 2 — Deferred: the researched scaffolding, built only if Part 1's real data says so

Everything below is the fully-researched alternative/fallback design from this item's earlier drafts. It
stays in this document because the research is real and the tool/dataset choices are still the right ones
*if* they end up needed — but none of it should be built speculatively. Treat this as a designed-ahead
backlog gated on §8, not a second phase with a fixed start date.

## §9. Tool matrix — a named, real library or model for every shared signal

| Signal | Tool | What it actually is | Cost | Maturity |
|---|---|---|---|---|
| Extractive salience (which sentence is central) | **Sumy** (LexRank/LSA/Luhn/KL-Sum) | Pure-Python graph/statistical summarizer, no GPU | Free, instant | 10+ yr old, stable |
| Extractive salience (semantic) | **sentence-transformers + LexRank** | Embed sentences, cosine-similarity graph, PageRank-style centrality — official reference implementation ships in the sentence-transformers repo | Small CPU model (~100 MB) | Actively maintained (UKPLab) |
| Keyphrase salience | **KeyBERT** | BERT-embedding cosine similarity to the whole document; CPU-friendly | Small model | Actively maintained |
| Keyphrase salience (cheapest) | **YAKE** | Pure statistical, no model download, no training | Free, instant | Stable |
| Redundancy-aware final ranking | **MMR** (Maximal Marginal Relevance, Carbonell & Goldstein 1998) | Iteratively pick the candidate most relevant to the topic *and* least similar to what's already been picked | ~20 lines over existing embeddings; LangChain ships a usable reference implementation if you don't want to hand-roll it | Classic, well-understood algorithm |
| Political/civic contentiousness | **Political_DEBATE** (`mlburnham/Political_DEBATE_base_v1.0`, Hugging Face) | Zero-shot NLI model trained on the PolNLI corpus (congressional bills, court summaries, news, social media) — does stance/topic/event classification with no fine-tuning needed | Mid CPU model | Published, documented |
| Political sentiment (better fit than generic sentiment) | **ParlaSent** | Sentiment model tuned specifically for parliamentary/political discourse, trained on the **ParlaMint** corpus (1.2B words, 29 countries' parliamentary debates) | Mid model | Published |
| Cheap text-emotion cross-check | **NRCLex / LeXmo** | Python wrappers around the NRC Emotion Lexicon (27k words → 8 emotions + pos/neg) | Free, instant, no model | Stable, widely used |
| Reading difficulty / surface features | **textstat** (readability scores) + simple regex (quotation-mark presence, sentence length) | Cheap surface features — found to matter for pull-quote selection specifically, §11b/§11c | Free, instant | Stable |
| **Interaction / turn-taking features (library)** | **ConvoKit** (Cornell Conversational Analysis Toolkit) | Real, actively maintained Python toolkit for conversational-feature extraction; ships a ready-made **Supreme Court Oral Arguments Corpus** (1955–2023, 66M+ words, speaker/turn-aligned) for validation, plus built-in extractors like "Redirection" (does an utterance redirect conversation flow) | Free, small | Actively maintained (Cornell NLP) |
| **Contentiousness / derailment (upgrade over static sentiment)** | ConvoKit's **"Conversations Gone Awry"** features + **Google Perspective API** | 13 politeness + 6 impoliteness/rhetorical-prompt lexical/parse features, plus a toxicity/severe-toxicity score — purpose-built to predict conversational *escalation*, a better-fitting proxy for "this is about to get heated" than static sentiment classification | Free (Perspective API free tier) | Mature |
| Decision/vote markers | *(rule-based, not a library — see §10)* | Robert's Rules phrasing + roll-call sequences | Free, instant | N/A |
| Vocal energy (cheapest acoustic proxy) | **Parselmouth** (Python binding to Praat) | Pitch, intensity, jitter, shimmer, HNR — pure signal processing, no model | Free, instant | Actively maintained |
| Vocal energy (even cheaper) | **librosa** | RMS energy, pitch tracking, zero-crossing rate | Free, instant | Ubiquitous, stable |
| Acoustic feature extraction (SER-tuned) | **openSMILE** (`audeering/opensmile` + `opensmile-python`) | 88-feature eGeMAPS set purpose-built for speech emotion recognition; pure C++ core, runs even on a Raspberry Pi | Small, fast | Mature, actively maintained by audEERING |
| Acoustic arousal/valence/dominance (richest) | **`audeering/wav2vec2-large-robust-12-ft-emotion-msp-dim`** (Hugging Face) | Continuous **arousal**, valence, dominance regression directly from 16 kHz mono audio — no manual feature engineering | Full wav2vec2 forward pass | Published, documented, Apache-2.0-derived |
| Acoustic emotion (categorical alternative) | **SpeechBrain** `emotion-recognition-wav2vec2-IEMOCAP` | anger / happiness / sadness / neutral, 78.7% accuracy | Full wav2vec2 forward pass | Actively maintained |
| Cross-talk / heated-exchange proxy | **pyannote.audio** overlapped-speech-detection | Detects when two speakers talk over each other — a strong, cheap, independent signal for "this got contentious," no emotion model needed | Small model | De facto standard, MIT-licensed; also the presumptive choice for this project's own not-yet-built R7 diarization, so adopting it now is forward-compatible |
| Public-comment span | *(no dedicated tool — see §10)* | Chapter-title keyword match ("public comment," "citizen comment") | Free | N/A |
| Topic/segment boundary synthesis (when a training corpus lacks one) | **NLTK `TextTilingTokenizer`** or **DeepTiling** | Unsupervised topic-boundary detection, used only to backfill chapter-equivalent structure where a dataset doesn't already have it (§11d) | Free–small model | Stable / research-maintained |
| **Learned candidate ranking (×3)** | **LightGBM/XGBoost, LambdaMART ranking objective** — one model per target | Gradient-boosted learning-to-rank over the shared feature vector above; a separate model for summary-worthiness, pull-quote-worthiness, and decision-worthiness — see §11 | Training: one-time/periodic; inference: instant | Industry-standard for exactly this shape of problem |

### The one honest gap: decision/action-item detection has no plug-and-play package

Everything above is either a real pip-installable library or a documented Hugging Face model. Decision
detection is the exception — there's no mature, general-purpose "detect the decision in this meeting"
package. The two most relevant things found:

- A published methodology (BERT fine-tuned on a 2,750-utterance dialogue-act dataset) reaching 95%+
  accuracy on *action-item* classification — real, but it's a fine-tuning recipe, not a library to import.
- **MeetingBank** (Hugging Face dataset `lytang/MeetingBank-transcript`) — 1,366 real **city council
  meetings**, 3,579+ hours, with video, transcripts, and professionally written minutes. Crucially,
  MeetingBank's own construction already divides each meeting into **6,892 segment-level instances**, each
  tagged with an **`item_type`** parsed straight from the council agenda — `Ordinance`, `Clerk File`,
  `Agenda Item`, `Motion`, `Resolution` — which is close to direct supervision for a decision classifier,
  not just an oracle-extraction proxy (§11b). Its "Boundary Similarity" evaluation metric is also the
  right tool for validating segment-alignment claims.
- **Council Data Project** (`github.com/CouncilDataProject`, `cdp-backend`) — an existing open-source
  pipeline built for exactly this domain (municipal meeting ingestion, transcription, indexing). Worth a
  direct look for reusable components even though it doesn't appear to include a decision-detection step.

Given that, §10 proposes a fused rule + proximity approach instead of waiting on a library that doesn't
exist yet — now with MeetingBank's `item_type` field, and LaCour's judgment-linkage (§11b), as real
validation signal.

## §10. Decision detection: adjacency pairs, proximity to the next chapter boundary, and commitment language

No existing tool fuses these signals — but each part is real and simple, and combining them is scoring
logic, not research:

1. **Adjacency-pair detector** — regex/keyword match for propose→agree/second sequences: "so moved," "I
   move to," "second," "motion carries," "all in favor," roll-call name+aye/no sequences. This is the
   exact same technique this codebase's own rule-based tagger already uses for taxonomy phrase matching
   (`tags.py::_literal_pattern`) — same shape, not a new paradigm.
2. **Boundary-proximity weighting** — chapter boundaries already exist for free (R3's agenda text + this
   project's own served-chapter timestamps). Score every candidate decision-marker by its time-distance to
   the *next* chapter's start; a marker in the last ~10% of a chapter's span (or last N seconds) gets a
   strong multiplicative boost. Structurally, votes close out an agenda item right before the meeting
   moves on.
3. **Commitment-vs-deflection language** — parole-hearing research on predicting board outcomes uses
   remorse/evasiveness and hedging-vs-certainty lexical markers as a real, transferable feature category
   (§11c): distinguishing "we will fund this by Q3" from "we'll look into it" is a genuinely useful signal
   for whether a real decision/direction was set, independent of whether Robert's Rules phrasing was used.
4. **Validation, from two independent domains** — MeetingBank's `item_type` field gives direct
   city-council ground truth; **LaCour!** (§11b) gives a second, more structurally direct validation path
   from a completely different jurisdiction (ECHR): it links spoken hearing arguments to which ones the
   final judgment actually relied on, rather than only a coarse type tag.

## §11. Three learned classifiers, not one — summaries, pull-quotes, and decisions are different targets

### 11a. Direct precedent that fused-feature classifiers work for "importance," corroborated from two independent directions

**"Combining Acoustics, Content and Interaction Features to Find Hot Spots in Meetings"** trains exactly
this shape of classifier: openSMILE acoustic-prosodic features + BERT lexical embeddings +
speech-activity/turn-taking statistics, fused to predict human-judged "hot spots" on the ICSI meeting
corpus. Its key finding: **the lexical/content model was the most informative on its own, with
interaction and acoustic-prosodic components adding real but smaller incremental contributions.**

A completely independent research line corroborates the acoustic half of that finding with a cleaner
ground truth: **TED Talk engagement research** predicts real **audience laughter and applause** (captured
from actual audio, not inferred) using acoustic-prosodic and linguistic features fused together, and finds
the combination genuinely predictive (Interspeech 2017 and related work). Two independent studies, in
different domains, both using real audio, both finding acoustic signal adds real value on top of text —
this matters directly for §14's framing on arousal.

### 11b. Three targets, three dataset landscapes — the actionable ones, in depth

**1. Summary classifier — "is this sentence worth including in a digest of what happened"**

| | |
|---|---|
| **Datasets** | MeetingBank (exact domain) + QMSum, all three domains pooled for volume, Committee split weighted higher for register match |
| **Label technique** | **Greedy oracle extraction**: greedily select the sentence set that maximizes ROUGE overlap against the reference minutes/summary text (reference implementations exist, e.g. `pltrdy/extoracle_summarization`) — a real, standard technique, budgeted as its own small labeling pipeline, not a one-line lookup |
| **What it's for** | Feeds a summary-track LLM job (§13), applied **per chapter** — same coverage requirement Part 1 enforces via schema (§2), enforced here via per-chapter classifier selection instead |

**2. Pull-quote classifier — "is this sentence the one worth pulling out to catch attention," a genuinely different target from "summary-worthy"**

| Source | What it offers | Status |
|---|---|---|
| **Goodreads Quotes** | Publicly displayed, **like-count-ranked**, reader-submitted quotes — a graded, crowd-validated "this resonated" signal. Multiple **already-scraped datasets on Kaggle** (`dwsstudio/scrapped-quotes`, `abhishekvermasg1/goodreads-quotes`, `sanjeetsinghnaik/quotes-from-goodread`) plus an active scraper (`soniajoseph/goodreads-quotes`) if a fresher pull is needed | **Recommended lead bootstrap.** Positive examples are immediately usable at scale; needs pairing with full source text (Project Gutenberg, public-domain subset, §11e) to construct negatives — a real, bounded engineering step, not a blocker |
| **Bohn & Ling** (COLING 2020, `AutomaticPullQuoteSelection` on GitHub) | Real editor-selected pull quotes from national news outlets, sentence-classification framing, code+data available | Usable secondary source, but has a structural confound: quotation-mark presence is a top feature likely because editors favor pulling *attributed source quotes* over narration — doesn't discriminate anything in a transcript where everything is already spoken |
| **Glasp / "Cold-Start Prediction of Crowd Highlight Salience"** (2026) + **"Personal Salience"** | The best-matched *methodology* — whole-document, crowd-behavioral (many independent co-readers), no narration/quote split at all, logistic ranker over sentence embeddings + positional/contextual features | **Confirmed not publicly released** (proprietary Glasp user data) — replicate the modeling approach, don't expect to reuse the data |
| **Kindle Popular Highlights** | Real phenomenon at real scale (34,044 highlights, 1M+ words, public-domain classics, Rowberry 2016, UCL) | No confirmed clean dataset release; Amazon doesn't expose this via API; scraping it targets an unsupported feature — real ToS risk, not recommended as a first move given Goodreads' cleaner path |

**What published feature ablations say to trust (§11c has the full list)**: quotation-mark presence and
reading difficulty were strong in Bohn & Ling's *news* study, but the quotation-mark signal specifically
shouldn't transfer (see confound above). Sentiment/arousal as *text lexicon* features were weak in that
same study — that finding is revisited and narrowed in §14, since it says nothing about real acoustic
arousal.

**3. Decision/direction classifier — "did something get decided or directed here"**

| Source | What it offers |
|---|---|
| **MeetingBank `item_type`** | Segments tagged Motion/Resolution — close to direct positive-class supervision, exact domain |
| **LaCour!** (ECHR hearings, `arxiv:2312.05061`) | 154 full hearings, 2.1M tokens, sentence-level timestamps, explicitly linked to which arguments the final judgment relied on — a more direct "did this argument matter to the outcome" signal than a coarse item-type tag, from a formal oral-hearing register (though international human-rights law, not US municipal government) |
| **Parole-hearing linguistic markers** | Not a labeled dataset, but a real feature category (§11c): remorse/evasiveness and hedging-vs-certainty language, worth adding to this classifier's feature vector regardless of dataset source |

### 11c. A feature dictionary — computed metrics drawn from published classifiers, not invented from scratch

| Category | Metric | Source | Note |
|---|---|---|---|
| Surface/lexical | Sentence length, quotation-mark presence, reading-difficulty score | Bohn & Ling | Strongest hand-crafted signals in their pull-quote model — minus the quotation-mark confound (§11b) |
| Surface/lexical | Sentiment score | ClaimBuster, Bohn & Ling | Weak-to-moderate alone in both; one input among many, not a primary signal |
| Syntactic | Counts across 43 Penn Treebank POS tags | ClaimBuster | Cheap, established; feature-selected via Random Forest/GINI importance in the original work |
| Entity | Frequency across 26 named-entity types (Person, Organization, Money, Date, etc.) | ClaimBuster | Directly useful for all three classifiers |
| Semantic | TF-IDF bag-of-words; Sentence-BERT embedding similarity to the full document | ClaimBuster; Bohn & Ling | Sentence-BERT won overall in Bohn & Ling but only marginally over n-grams |
| Social/tone | 13 politeness + 6 impoliteness/rhetorical-prompt categories | ConvoKit | Ships as a ConvoKit feature extractor, not a bespoke build |
| Social/tone | Toxicity / severe-toxicity score | Google Perspective API (used inside ConvoKit's "Conversations Gone Awry") | Free-tier API, zero training required |
| Interaction | Word-count/speaking-time asymmetry directed at a specific party (the "disagreement gap") | SCOTUS oral-argument outcome research | Novel, cheap, genuinely transferable — council analog: which position on an item drew more council follow-up/pushback |
| Interaction | Interruption count; question-vs-comment ratio per utterance | SCOTUS oral-argument outcome research | |
| Interaction | Semantic similarity of pre-/post-interruption speech; sentiment shift around an interruption | "Heard or Halted" (SCOTUS interruption study) | Distinguishes an interruption that changed the substance from one that didn't |
| Register-specific | Remorse/evasiveness lexical markers; hedging vs. certainty language | Parole-hearing research | Feeds the decision classifier specifically — real commitment vs. deflection |
| Acoustic | 88-feature eGeMAPS set | openSMILE | Already in §9's tool matrix; listed here for completeness |
| Acoustic | Real audience laughter/applause as ground truth, fused with acoustic-prosodic + linguistic features | TED Talk engagement research | Corroborates Hot Spots in Meetings' own finding — see §14's framing on arousal |

### 11d. Chapter/segment-boundary presence — verified per dataset, not assumed

MeetingBank, QMSum, and LaCour all carry native segment/timestamp structure usable as chapter-equivalents;
Goodreads quotes and the Bohn & Ling dataset have no such structure and don't need one for their targets,
since boundary-proximity is a decision-classifier-specific feature. Where a future corpus lacks native
structure, synthesize it via automatic topic segmentation (§9).

### 11e. Training requires an ingestion adapter per external dataset

**Each external dataset (MeetingBank, QMSum, LaCour, Goodreads+Gutenberg, Bohn & Ling) needs its own small
ingestion adapter** converting that dataset's native shape into the minimum internal representation this
project's feature-extraction code already expects — rather than writing a second, parallel implementation
of every feature function per dataset. The feature code stays written against this project's own internal
shape; only the adapter is dataset-specific and disposable (training-time-only tooling, not something that
ships). Concretely: MeetingBank's 6,892 `item_type`-tagged segments map directly to chapter-equivalents;
QMSum's manual topic segmentation and relevant-span layers do the same independently; LaCour's sentence-
level timestamps plus judgment-linkage adapt into decision labels; Goodreads quotes need pairing with full
Project Gutenberg texts (public-domain subset) to construct a negative pool, the heaviest adapter of the
five; Bohn & Ling's news articles have no chapter concept and don't need one, since boundary-proximity is
decision-specific, not general.

### 11f. Evaluation methodology, stated properly

- **Model family, per classifier**: gradient-boosted learning-to-rank (LightGBM `lambdarank` objective or
  XGBoost `rank:ndcg`/`rank:pairwise`), not a generic binary classifier — each target is "rank candidates
  within one document/meeting," not "classify this sentence in isolation." Small, cheap to retrain, feature
  importances double as an audit trail.
- **Split discipline**: train/validation/test split, or k-fold cross-validation across train+validation
  for hyperparameter tuning, with a **final test set held out and untouched until the end**. A single
  70/30 split is a simplified special case, useful for a first pass, not a substitute once tuning
  parameters.
- **Grouping matters and is easy to get wrong**: candidate sentences from the same meeting/article/book
  are correlated, not independent — use **group-aware splitting** (`GroupKFold`/`GroupShuffleSplit`,
  grouped by source document ID) for all three classifiers.
- **Metric**: **NDCG@k** — accounts for both relevance and rank position, handles graded relevance
  naturally, unlike plain accuracy/F1. MAP/MRR are reasonable secondary metrics.

### 11g. Bootstrap, then replace with real data over time — per classifier, independently

1. **Bootstrap** each classifier on its own dataset landscape (§11b).
2. **Validate transfer before trusting any of them** — every external register differs from this
   project's own real ASR-transcribed civic meetings; check, don't assume.
3. **Retrain each classifier independently on this project's own accumulated calibration-review history**
   once it exists (from Part 1's real usage, §8) — the strongest data for each target, collected at zero
   extra annotation cost.
4. **Verify before depending on any external source**: license/ToS terms for MeetingBank, QMSum, LaCour,
   Bohn & Ling, the Goodreads-derived Kaggle datasets, and the Perspective API's usage terms — none of
   this has been checked yet.

## §12. The scaffolded pipeline, stage by stage (if built)

| Stage | What runs | Output | Needs an LLM? |
|---|---|---|---|
| 1. Rule-based decision markers | Robert's Rules regex + boundary-proximity weighting + commitment/deflection language (§10) | High-precision decision-point candidates | No |
| 2. Extractive salience | Sumy/sentence-transformers+LexRank per chapter window | Per-sentence salience score | No |
| 3. Keyphrase density | KeyBERT/YAKE | Secondary salience signal; doubles as searchable keyphrases | No |
| 4. Tone, contentiousness, and social features | Political_DEBATE + ParlaSent + NRCLex + ConvoKit politeness/derailment features + Perspective API + quotation-mark/readability/POS/entity features (§11c) | Contentiousness/category/surface features | No |
| 5. Acoustic + interaction features | Parselmouth → openSMILE → wav2vec2-dim (only on shortlisted spans) + pyannote overlap detection + ConvoKit turn-taking stats | Timestamped excitement/cross-talk/turn-taking evidence | No |
| 6a. Summary ranker | LightGBM/XGBoost trained per §11b.1, applied **per chapter** | Top-1 "summary-worthy" sentence per chapter | No |
| 6b. Pull-quote ranker | LightGBM/XGBoost trained per §11b.2 | Per-sentence "pull-quote-worthy" score | No |
| 6c. Decision ranker | LightGBM/XGBoost trained per §11b.3 | Per-span "decision-worthy" score | No |
| 7. Redundancy-aware final selection | MMR, applied mainly to 6b's output | Diverse top-N per track | No |
| 8. LLM labeling/tie-break | `summarize` verb, three modes, over each track's shortlist | Summary text · pull-quote gloss · decision text | Yes — narrow and cheap |

## §13. Where the LLM still earns its keep in the scaffolded design

Once Stage 7 has produced small, diverse, evidence-tagged shortlists per track, three separate LLM passes
are cheap and low-risk: `scope: "summary"` (mode-aware-verb precedent from R12/Part 1), `scope:
"pull-quote"` (freeform gloss, kept separate because the target — attention — differs from a summary's
target — coverage), and `scope: "decision"` (grounded "what was decided" text anchored to an
already-detected decision-type label and real quote span). All three reuse R5's dispatch infra and the
generalized calibration matrix (§4).

## §14. Honest remaining gaps (scaffolded design)

- **No plug-and-play decision-detection package** — the fused rule + proximity + commitment-language
  approach in §10, backed by MeetingBank's `item_type` and LaCour's judgment-linkage, is the practical
  answer, not a library import.
- **No single perfectly-matched labeled dataset for any of the three targets** — each has a real,
  defensible bootstrap source, but every one needs domain-transfer validation (§11b).
- **Careful framing on sentiment/arousal**: a **text-derived** arousal lexicon was found uninformative for
  pull-quote selection in one *written-news* study (Bohn & Ling) — that is a finding about a text proxy in
  a non-audio task, and it does not bear on real **acoustic** arousal in spoken content. Two independent,
  audio-grounded research lines point the other way (TED Talk engagement research; "Hot Spots in
  Meetings"). Treat acoustic arousal as a legitimate, evidence-backed signal to validate on this project's
  own data, not something to deprioritize based on a text-only negative result in an unrelated task.
- **Glasp's dataset is confirmed proprietary and not released** — usable only as a modeling approach to
  replicate, not as training data.
- **Kindle Popular Highlights has no confirmed clean public dataset** and isn't officially exposed via
  API — scraping it targets an unsupported feature; not recommended given Goodreads' cleaner path.
- **Goodreads quotes are positive-only** — building a real ranking training set needs pairing with full
  public-domain source text (Project Gutenberg) to construct negatives — a real, bounded, non-trivial
  data-engineering step, not a blocker.
- **Dataset/API license and ToS terms are unverified** for MeetingBank, QMSum, LaCour, Bohn & Ling, the
  Goodreads-derived Kaggle datasets, and the Perspective API's usage terms at this project's expected
  volume.
- **Acoustic arousal is a proxy, not ground truth** regardless of which track uses it — same
  human-calibration treatment as everything else in `review/35`, not an unreviewed automatic signal.
- **`review/34`'s tournament is still the only mechanism for genuinely freeform LLM judgment** — its job
  shrinks under this design, arbitrating within already-good per-track shortlists rather than ranking
  whole freeform documents.
- **Part 1's LLM-only approach might just keep winning.** That's the intended outcome to be open to, not
  a failure mode to explain away (§8).

### Future directions noted, not adopted

- **VerbCL's citation-graph principle** (a legal passage's importance measured by whether *later* opinions
  verbatim-quote it) suggests a long-term distant-supervision idea unique to this project: does local news
  coverage of a meeting quote a specific transcript passage? Not a ready dataset — would require pairing
  this project's own meetings against local news coverage, which may not exist as clean paired data.
- **Cross-statement contradiction/inconsistency detection**, the standout capability of deposition-
  analysis legal-tech tools (Skribe, Filevine, NexLaw), is a genuinely different, adversarial-context
  signal. A civic analog — checking whether a councilmember's stated public position is consistent with an
  earlier meeting — is an interesting future accountability-journalism angle leaning on R5's existing
  search infra, but out of scope here.

## §15. If Part 2 is ever built — suggested order

1. Rule-based decision markers + boundary-proximity + commitment-language scorer — zero-to-small new
   dependencies, immediate value on its own even before any classifier exists.
2. Shared feature computation (Stages 2–4) — build the ingestion adapters (§11e) for MeetingBank, QMSum,
   and LaCour alongside this.
3. Bootstrap the summary and decision classifiers first (§11b.1, §11b.3) — both trainable immediately
   without waiting on the pull-quote dataset or acoustic work.
4. Bootstrap the pull-quote classifier (§11b.2) — pair Goodreads quotes with Project Gutenberg full texts;
   add Bohn & Ling as a secondary comparison source; validate real transfer before trusting either.
5. MMR on top of the pull-quote ranker's output.
6. Political_DEBATE/ParlaSent + ConvoKit/Perspective contentiousness scoring, added per-classifier after
   real ablation, not assumed uniformly.
7. Acoustic arousal (Parselmouth → openSMILE → wav2vec2-dim on shortlisted spans only) — heaviest new
   infra, sequenced last; validate its contribution per-track rather than assuming it either way.
8. Three LLM labeling passes, reusing whatever of Part 1's infra still applies.
9. Ongoing: retrain each classifier as real calibration-review history accumulates, and keep comparing
   against Part 1's LLM-only approach as a standing competitor in the same calibration rows, not a
   one-time baseline that's retired once Part 2 ships.

---

## Sources

- [Sumy overview and comparison](https://machinelearningplus.com/nlp/text-summarization-approaches-nlp-example/)
- [sentence-transformers LexRank reference implementation](https://github.com/UKPLab/sentence-transformers/blob/master/examples/applications/text-summarization/LexRank.py)
- [LexRank: Graph-based Lexical Centrality as Salience in Text Summarization](http://www.cs.cmu.edu/afs/cs/project/jair/pub/volume22/erkan04a-html/erkan04a.html)
- [KeyBERT — keyword extraction with BERT](https://www.maartengrootendorst.com/blog/keybert/)
- [YAKE keyword extraction](https://www.geeksforgeeks.org/nlp/keyword-extraction-methods-in-nlp/)
- [The Use of MMR, Diversity-Based Reranking (Carbonell & Goldstein, 1998)](https://www.cs.cmu.edu/~jgc/publication/The_Use_MMR_Diversity_Based_LTMIR_1998.pdf)
- [Political_DEBATE model card](https://huggingface.co/mlburnham/Political_DEBATE_base_v1.0)
- [ParlaMint corpora of parliamentary proceedings](https://link.springer.com/article/10.1007/s10579-021-09574-0)
- [Multilingual Sentiment Analysis: Measuring Conflict in Legislative Speeches (ParlaSent lineage)](https://www.researchgate.net/publication/328022898_Multilingual_Sentiment_Analysis_A_New_Approach_to_Measuring_Conflict_in_Legislative_Speeches)
- [NRC Emotion Lexicon in Python](https://www.tutorialspoint.com/article/emotion-classification-using-nrc-lexicon-in-python)
- [Detecting Action Items in Multi-party Meetings](https://www.researchgate.net/publication/221040314_Detecting_Action_Items_in_Multi-party_Meetings_Annotation_and_Initial_Experiments)
- [MeetingBank: A Benchmark Dataset for Meeting Summarization (paper)](https://arxiv.org/abs/2305.17529)
- [MeetingBank dataset site](https://meetingbank.github.io/dataset/)
- [QMSum: A New Benchmark for Query-based Multi-domain Meeting Summarization](https://arxiv.org/abs/2104.05938)
- [QMSum (GitHub, Yale-LILY)](https://github.com/Yale-LILY/QMSum)
- [LaCour!: Enabling Research on Argumentation in Hearings of the European Court of Human Rights](https://arxiv.org/abs/2312.05061)
- [ClaimBuster / check-worthiness feature set](https://www.researchgate.net/publication/320885106_TATHYA_A_Multi-Classifier_System_for_Detecting_Check-Worthy_Statements_in_Political_Debates)
- [Catching Attention with Automatic Pull Quote Selection (Bohn & Ling, COLING 2020)](https://arxiv.org/abs/2005.13263)
- [AutomaticPullQuoteSelection (GitHub)](https://github.com/tannerbohn/AutomaticPullQuoteSelection)
- [The Long Tail, Not the Front Page: Cold-Start Prediction of Crowd Highlight Salience](https://arxiv.org/abs/2606.11654)
- [Personal Salience: Highlighting Is Social, but Individuality Lives in Selection (GitHub, Glasp)](https://github.com/glasp-co/personal-salience)
- [Commonplacing the public domain: reading the classics socially on the Kindle (Rowberry, 2016)](https://discovery.ucl.ac.uk/id/eprint/10132468/)
- [Goodreads quotes scraper (GitHub)](https://github.com/soniajoseph/goodreads-quotes)
- [Goodreads Quotes dataset (Kaggle)](https://www.kaggle.com/datasets/abhishekvermasg1/goodreads-quotes)
- [How the Guardian approaches quote extraction with NLP](https://explosion.ai/blog/guardian)
- [VerbCL: A Dataset of Verbatim Quotes for Highlight Extraction in Case Law](https://arxiv.org/abs/2108.10120)
- [Reduct.Video — highlight reels from long interview footage](https://reduct.video/)
- [Trint for Newsrooms](https://trint.com/trint-for-newsrooms)
- [Council Data Project (GitHub)](https://github.com/CouncilDataProject)
- [Parselmouth: A Python interface to Praat](https://www.researchgate.net/publication/327023550_Introducing_Parselmouth_A_Python_interface_to_Praat)
- [librosa: Audio and Music Signal Analysis in Python](https://proceedings.scipy.org/articles/Majora-7b98e3ed-003.pdf)
- [openSMILE (audeering, GitHub)](https://github.com/audeering/opensmile)
- [audeering/wav2vec2-large-robust-12-ft-emotion-msp-dim](https://huggingface.co/audeering/wav2vec2-large-robust-12-ft-emotion-msp-dim)
- [SpeechBrain emotion-recognition-wav2vec2-IEMOCAP](https://huggingface.co/speechbrain/emotion-recognition-wav2vec2-IEMOCAP)
- [pyannote.audio (GitHub)](https://github.com/pyannote/pyannote-audio)
- [Combining Acoustics, Content and Interaction Features to Find Hot Spots in Meetings](https://arxiv.org/abs/1910.10869)
- [Spotting "Hot Spots" in Meetings: Human Judgments and Prosodic Cues (SRI)](https://www.sri.com/wp-content/uploads/2021/12/spotting_hot_spots_in_meetings.pdf)
- [Visual, Laughter, Applause and Spoken Expression Features for Predicting Engagement Within TED Talks](https://www.isca-archive.org/interspeech_2017/haider17_interspeech.html)
- [Fostering User Engagement: Rhetorical Devices for Applause Generation Learnt from TED Talks](https://arxiv.org/abs/1704.02362)
- [ConvoKit (Cornell Conversational Analysis Toolkit, GitHub)](https://github.com/CornellNLP/ConvoKit)
- [Supreme Court Oral Arguments Corpus (ConvoKit documentation)](https://convokit.cornell.edu/documentation/supreme.html)
- [Conversations Gone Awry: Detecting Early Signs of Conversational Failure](https://www.cs.cornell.edu/~cristian/pdfs/conversations_gone_awry.pdf)
- [Heard or Halted? Gender, Interruptions, and Emotional Tone in U.S. Supreme Court Oral Arguments](https://arxiv.org/abs/2512.05832)
- [A Computational Analysis of Oral Argument in the Supreme Court](https://arxiv.org/abs/2306.05373)
- [Using Machine Learning to Scrutinize Parole Release Hearings](https://btlj.org/wp-content/uploads/2025/03/40-1_Bell.pdf)
- [Learning to Rank — XGBoost documentation](https://xgboost.readthedocs.io/en/latest/tutorials/learning_to_rank.html)
- [How to Use LightGBM for Learning to Rank in Python](https://forecastegy.com/posts/lightgbm-learning-to-rank-python/)
- [Group K-Fold Cross-Validation explained](https://schneppat.com/group-k-fold-cv.html)
- [Normalized Discounted Cumulative Gain (NDCG) explained](https://www.evidentlyai.com/ranking-metrics/ndcg-metric)
