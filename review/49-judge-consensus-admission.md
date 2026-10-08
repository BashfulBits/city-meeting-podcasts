# review/49 — Judge-consensus admission (JEV as anchor judge)

**Maturity: L2-partial (approach chosen, risks and capacity worked out; not L3 dev-ready) · authored 2026-09-29, revised 2026-10-05.
The gap to L3 is listed in "Path to L3" at the bottom; this doc is not yet in `review/11`.**

**Document map.** This document is the umbrella design: why, what, decisions, capacity and evidence. Each implementation phase gets its own build spec
(one agent can be handed one short, exact document): [review/50](50-p0-llm-verb-backlog-trend.md) is P0 (L3); P1 onward are specified when their predecessors have produced data. The pilots live in the eval lane
[`evals/judge`](../evals/judge/README.md) and are re-run, not re-described here.

## Goal (from the maintainer)

1. Take the human maintainer out of the approve/rate loop for LLM tags and moments (or shrink it to a
   small, higher-context residual).
2. Admit a tag or moment only when a panel of judges agrees strongly.
3. Learn which judges and which tagger/moment routes are reliable, promote the good ones, retire the
   bad ones, and try new routes automatically, at zero maintainer cost.
4. Simplify the tags and moments lanes and drop training/holdout machinery that is no longer needed.

## What exists today (verified in this checkout)

| Concern | Today | Where |
|---|---|---|
| R5 tag admission | Per (tag, route, scope, prompt, taxonomy) row: 12 human reviews and 90% precision | `citypods/llm_evaluation.py`, review/42 |
| R5 "pre-labeler" | Gemma 4 31B overlay per row: 50 reviews, 95% precision, then it suppresses or keeps candidates | same; lane `topic-tags:prelabeler`, `:prelabeler-shadow` |
| R6 moment admission | Per (family, route, prompt, duration, framing) cell: 30 days warm-up, 30 reviews, 90% precision, learned score threshold | `citypods/moment_evaluation.py` |
| R6 judge panel | 3 routes (`r6-judge`), append-only `judge_observations`, per-judge calibration cells | `citypods/moment_judging.py` |
| Human touchpoints | Weekly ~80-item issue packet (R5), `r6-moment-review` workflow, R5 benchmark labeling (200–300 chapters), tournament ticket approval | workflows `llm-tag-review`, `r6-moment-review`, `r5-benchmark`, `tournament-ticket-approve` |
| Route choice | Static `llm_lanes` in `config/site_config.yml`; pairwise tournament records evidence only, a human approves changes | review/34, review/46 |

Two things follow. (a) There are already **four** places where a human is ground truth, and
**two** separate calibration ledgers with different rules. (b) The judge-panel shape already exists for
R6 (`judge_observations`), so this is a generalisation and consolidation, not a greenfield build.

## Is JEV a good candidate?

Qualified yes. Reported, not independently verified by me: strong on RewardBench/HaluEval-style
bounded judgments, low variance, returns probabilities, weak on reasoning-heavy judging (68.4% JudgeBench).
That profile matches "is this tag supported by this chapter" and "is this quote supported and usable",
and does not match "check a chain of reasoning". The `moment-judge` rubric includes safety-flavoured
items (embarrassing to a person, member-of-public PII), where a single judge should not be the only gate.

Hard constraints to design around: one job at a time, 1 RPM (free tier), 32k tokens of state plus the
longest question (64k total window). About 1,440 calls/day at most, so **JEV is a scarce, slow anchor,
not the bulk scorer**. Packing helps only if one call answers many questions about one shared state.

## Proposed design

### 1. One judgment contract for every task

A candidate (tag, moment, later anything else) gets a set of typed **judgments**, each
`{judge_route, prompt_version, question_type, verdict, p, reason, evidence_digest}`, appended to one
ledger. `question_type` is `supported` (bool), `usable` (score), or `prefer` (pairwise). This replaces the
R5 pre-labeler output shape and R6 `judge_observations` with a single append-only record; raw output is
never overwritten (same rule as today).

### 2. JEV packing: one state per chapter/meeting, many questions

State = the chapter (or the moment's surrounding window) transcript plus mapped agenda evidence, capped
under ~28k tokens. Questions = every candidate tag on that chapter, or every moment candidate on that
meeting. One call therefore judges a whole subject. Throughput is bounded by subjects/day, not candidates/day.
Subjects that do not fit get the normal `payload-too-large` deferral, never truncation.

### 3. Consensus admission rule

- **Admit** when JEV `p >= t_admit` and at least one independent-family judge (existing Gemma or an
  `r6-judge` route) also says supported.
- **Reject/suppress** when both are below `t_reject`.
- **Contested** (everything else) goes to the residual human queue, shown with all judges' reasons and
  the source context. This is the only human volume and it should be a small fraction.
- Thresholds are calibrated on gold (below), per task, not hard-coded, and are stored with the policy
  fingerprint so a change re-projects display without recalling vendors (same as review/42).
- Failed, deferred, or malformed judge calls never admit and never suppress (existing invariant).
- The tagger/moment route and the judges must be different families; enforce it at policy load.

### 3a. One framework for every additive producer task (tags, moments, and what comes next)

The judge stack is not a tag feature. Any producer LLM task whose output is either **valid or invalid** or **one of several to pick from** plugs in by registering a task spec; nothing in the judges, the consensus code, the probe set, the audit or the leagues is tag-specific.

**Vocabulary.**
- **Subject:** one producer output (a tag candidate, a pull quote, a decision candidate, a summary, a chapter title) with an id, the task name, an optional **group** (for example the meeting) and its evidence.
- **Question kinds** (logical, backend-neutral):
  - `validate`: is the subject supported and correct? Answer is a probability.
  - `gate`: is it safe to publish (no PII, no mocking, not procedural filler)? Answer is a probability; **any** judge that flags it holds the subject.
  - `grade`: how good is it on an ordered scale? Answer is a level.
  - `choose`: which of these N subjects from the same group is best? Answer is a winner with probabilities.
- **Backend mapping:** JEV: `validate` and `gate` are `noul`, `grade` is `score` with a `criteria` array, `choose` is `choice` with an options object (all verified live). LLM judges (Qwen via Groq, Gemma) get the same question compiled to a JSON schema (`{supported: boolean, quote}`, `{level: integer, reason}`, `{best: enum, reason}`). Both return the same normalized **judgment**: `{judge, task, subject_id, question_id, kind, value, confidence or probabilities, context_tier, prompt_version, evidence_digest}`.
- **Task spec** (a registry entry, one per task): subject kinds, group key, the ordered list of questions, how evidence is built at each context tier, the **consensus policy** per question kind, the **selection policy**, the probe set location, and the audit-item renderer.
- **Consensus per kind:** `validate`: admit when the JEV and sibling probabilities are both high enough, reject when both are low enough, otherwise contested; `gate`: any judge above the flag level holds the subject; `grade`: admit on agreement of levels within one step, contested when they differ by more; `choose`: winners agree is decided, otherwise contested, and order-flips are treated as contested. **Contested goes to the adjudicator, then the weekly audit.** Thresholds are calibrated from probes and adjudicator agreement (section 4), never from human labels.
- **Selection policy:** `all_admitted` (tags: every admitted tag is visible) or `top_k_per_group` (moments: at most K admitted subjects per meeting, chosen by the `choose` result with `grade` as the tie-break).

**Evidence (committed lane [`evals/judge`](../evals/judge/README.md), `question-types`, run 2026-09-30 on six real council meetings with four real pull quotes each; all 390 real candidates are currently shadow and manual-only).**
- **`choose` (best of 4) on JEV** picked the same winner under two shuffled option orders in **6 of 6** meetings (5 of 6 in an earlier exploratory run on a different sample).
- **The question kinds are not interchangeable:** the `choose` winner equalled the top `grade` in 2 of 6 and the top publish probability in 2 of 6 meetings. They measure different things, so a moments
  gate must not substitute one for another; `choose` selects among survivors of the `gate` and `validate` questions.
- **Cross-family agreement is sample-dependent:** Qwen3.8-27B picked the same winner as JEV in **3 of 6** meetings in the committed run and in 5 to 6 of 6 in the earlier exploratory run. At six meetings this cannot
  set a threshold; it says agreement on `choose` is somewhere between half and most, which is exactly the contested band the adjudicator exists for. Growing this sample is listed under "Path to L3".
- **Stability and spread:** `validate`(publishable) and `grade` moved at most 0.06 and 0.16 between orders; publish probabilities ranged 0.40 to 0.835 across real candidates, so a gate can discriminate.
  `grade` returns low confidence on JEV (0.09 to 0.3), so it is a tie-break, not a gate.
- **The producer's own `quality_score` is an unreliable signal:** it matched the JEV `choose` winner in 4 of 6 meetings in the committed run and 1 of 6 in the exploratory run; it is not used for admission.
- **Rate limits to design around:** Groq's Qwen route has an **output-token limit of 1,000 per minute**, which reasoning answers of 450 to 730 tokens hit after two requests; the harness spaces and packs sibling calls for this reason.

**How this maps to each task.**

| Task | Questions | Consensus and selection |
|---|---|---|
| Tag candidate (rule or LLM) | `validate` (evidence: matched span, window or chapter per the context study); deterministic checks first | admit when validated; `all_admitted` |
| Pull quote | deterministic verbatim and timing check, then `validate` (does the quote say what the reason claims), `gate` (PII, mocking, procedural), `grade` (usefulness, readiness), then `choose` among the meeting's survivors | admitted = passes gate and validate; selected = best `top_k_per_group` by `choose`; replaces the human-calibrated threshold in `moment-admission` and reuses the existing `moment_judging.py` rubric text as a sibling-judge prompt |
| Decision candidate | `validate` (is the stated outcome supported by the quote; the "AI interpretation" label stays) | `all_admitted` |
| Summaries, titles, other generated text (future) | `validate` (faithful to the source), `choose` between producer models' outputs for the same agenda item | replaces `tournament:tag` pairwise comparison; `choose` both orders |
| Anything later with valid/invalid or pick-best | declare its questions and evidence builder in a task spec | no change to judges, leagues or audit |

Tasks with external ground truth (chapter-agenda, chapter-locator) stay out, as decided.

### 4. Judge reliability without trusting human labels

**Decision (maintainer, 2026-09-29): existing human reviews are not trusted and are archived.** Rule-match
reviews had no context, tag reviews varied by tag, and pre-labeler reviews mostly graded how convincing a
one-sentence argument sounded. They move out of the active ledgers into an archive (kept for history, never
read by policy). A good LLM that reads the whole context is trusted over a fast human skim, so the reference
signal is built from evidence that does not depend on a hurried human. "Chapter" here only means the unit
tags attach to; this design does not judge chapter discovery (that stays with the chapter-locator lane).

1. **Deterministic checks (free, exact).** Quote appears verbatim in the transcript at the claimed
   time; chapter id and timing valid; cited link on the allowlist; a rule tag's include phrase literally
   present and no exclude phrase. Failing candidates never reach a judge.
2. **Human-verified probe set (small, grows slowly).** No generators or scaffolding. The set is a plain list in
   `evals/<task>/gold.json` of items a human has verified with the full context: known-right and known-wrong
   (tag, chapter) pairs, and known-good and known-bad moments. It is seeded from whatever the maintainer verifies
   during the weekly audit. The audit cohort (10–12 items) is mostly contested candidates; of those, the 2–3 per week that
   are clear-cut good or clear-cut bad (never Uncertain) can be promoted with one click, so the set is about two dozen items after
   a couple of months. Probes are
   mixed invisibly into normal judge batches and give each judge's false-accept / false-reject rate and a drift signal.
   Until the set has enough items (about 20) it is reported but does not gate anything; judges are then ranked
   on stability (3) and adjudication (4) only.
3. **Stability probes.** Re-ask a sample with shuffled question order and a paraphrase; a judge whose
   verdict flips is unreliable on that item. This directly tests JEV's advertised low variance.
4. **Adjudication (rare, automated).** Not a second sibling judge. A *judge* is the routine
   cheap vote every candidate gets (JEV plus one sibling). An *adjudicator* is a third, stronger
   long-context model consulted only when the two judges disagree or are near threshold, and for a small
   sample of probes and audit items. It reads the whole chapter/meeting and answers one disprove-me
   question with a quoted rationale, so it is the tiebreaker and the yardstick for scoring judges.
   - *Route choice (2026-09-30).* Excluded: every `chapter-locator` target (`gemini-3.5-flash-lite`, `gemini-3.5-flash`,
     `deepseek-v4-flash`, `deepseek-v4.1-flash`, `glm-5.3-flash`, `kimi-k3`; verdict pending) and all `chapter-agenda` routes.
     Requirements: general purpose, AA above 35, more than 100 RPD. **`qwen/qwen3.8-27b`** (Groq, 1,000 RPD) qualifies on AA (34 for xhigh on the
     current AA page; one source says 52, so the index version needs checking). **Live probe 2026-09-30** (free tier,
     `on_demand`): model reports 131,072 context and 16,384 max completion tokens; requests above **7,000 input tokens fail with HTTP 413** (ITPM
     limit 7,000; 9.7k, 12.9k and 16.4k-token prompts all rejected) while the response headers show 8,000 tokens/minute, 1,000 requests/day;
     `response_format: json_schema` works and returned valid JSON. **Thinking is opt-in, not default:** by default there is no
     `reasoning` field and the model reasons inline in `content` (a bat-and-ball question used about 390 output tokens);
     `reasoning_effort: "high"` with `reasoning_format: "parsed"` returns a `reasoning` field (134 reasoning tokens on an easy item);
     `reasoning_effort: "none"` was accepted. So the adjudicator job must set `reasoning_effort` explicitly. Qwen is limited to
     packets of about 6k input tokens (roughly 4–6 questions with evidence windows); the ceiling is an account tier limit, so the
     Groq Dev tier (paid) would lift it, which is not planned. The second adjudicator route should be whichever
     locator-target model falls out of contention once the locator verdict is final, using its long context for large packets;
     `gemini-3.8/3.7/3.6-flash` (40 RPD each) are small overflow. **Quality check (2026-09-30):** on the same 30 known-truth items packed 5 per request (about 760 prompt tokens, 400–800 reasoning tokens, about 2 s each, `reasoning_effort: high`, `json_schema`) Qwen3.8-27B was 30/30 correct with quoted evidence; the set is easy, so this confirms it can do the job within its 7k-token packets, not its accuracy on hard cases. Sharing a model with the locator is acceptable because adjudicators
     are rarely called; shared RPD, TPM and TPD still need a combined demand budget (see below).
   - *Volume and packing.* Judging is per (tag, chapter) pair, not per call: at about 25 pairs per episode, 1,000 episodes
     is about 25,000 pairs/day, and 10% contested is 2,500 pairs/day (not 10% of the roughly 1,750 tagger and moment calls). Adjudication
     is packed, not per pair: greedy packets of contested items across chapters and episodes, up to the route's input ceiling and at most
     about 20 questions per call (more degrades quality), grouped per episode when possible so state is shared. At 10% contested
     that is about 125 to 500 calls/day depending on route ceiling (25k-token packets down to Qwen's 6k), not 930. The earlier 930
     assumed one packet per episode with any contested pair.
5. **Blind human audit (section 4a)** is a check on all of the above, not the source of truth.

Reliability per judge is a windowed score from probes 2–4 (false-accept/false-reject rate, flip rate,
agreement with adjudication), stored per (judge, task, prompt version). JEV becomes sole judge for a
task only when it beats the panel on probes and adjudication over a minimum sample and duration.
Consensus among judges alone is never used as truth, since they share biases.

Any train/holdout material this needs (probe sets, adjudicated samples) lives under `evals/<task>/`
(`evals/tag-judge/`, `evals/moment-judge/`) following the `evals/chapter-agenda` layout: `manifest.json`
inputs, `gold.json` provider-independent truth, frozen selection, `results/`. Existing `training`/holdout
constructs elsewhere are deleted or moved there once superseded. `evals/chapter-locator` is being built
in an unpushed Codex worktree and is out of scope here.

### 4a. The blind human audit (capped, ~10 minutes, information-dense)

Purpose: detect whether the automated stack has drifted from what a person would call right, using the
human only where a skim is genuinely informative.

- **Hard cap 12 items, 10 minutes total (~45 s each); smaller when there is less uncertainty.** The
  weekly count is `min(12, n)` where `n` is the number of items needed to keep the estimated error rate
  of auto-decisions under target with 90% confidence given current probe results. If probes are clean
  and stable, `n` shrinks toward 0–4. A quiet week is a valid outcome.
- **Selection by expected information, not random.** Items where the judges and adjudicator disagree,
  items just inside the admit threshold, and items from the judge/route with the newest or noisiest
  probe record. A few (about 2) purely random items keep the sample honest.
- **Mobile first, text by default.** Everything needed to answer is inside the item: highlighted text span and
  agenda line. Tag items never need audio. Only moment items where the question is delivery/clarity/shareability
  ("would you publish this clip?") include a clip, and only when the text alone cannot answer it; most
  moment items should be answerable from text plus the caption.
- **Clip attachments (dry run 2026-09-30, `gh` 2.100).** Raw `<audio>`/`<video>` tags in an issue body are stripped and a bare
  link to a repo `.mp3`/`.mp4` renders only as a link. `gh issue create --attach clip.mp4` (images and videos only, not
  audio-only files) uploaded a 20 s clip (320x180 black frame, 24 kbps mono AAC, 68.7 KB) and the issue rendered an
  embedded `<video controls>` player, collapsed under a filename and muted until tapped. So moment audio ships as a tiny
  mp4 (static frame plus audio, well under 200 KB), and the audit issue must be created through `gh issue create`, not the
  REST issue API. **Not yet verified:** that this works with the Actions `GITHUB_TOKEN` (the test used the maintainer's own
  login) and mobile playback in the GitHub app. Use clips only for moment items that need them. The two test issues were deleted.
- **One binary claim per screen, framed to disprove.** "Does this chapter discuss *Short-term rentals*?" or
  for moments "Would you publish this 20 s clip on its own?". The evidence is the exact span highlighted
  in about 30 seconds of surrounding transcript, the chapter title/agenda line, and for moments an
  inline audio/clip player. Judge verdicts and reasoning are hidden until after you answer, so you are
  not anchored to a persuasive sentence, which was the failure of the earlier pre-labeler reviews.
- **Choices: Supported / Not supported / Uncertain.** Uncertain is first-class. Uncertain answers
  are excluded from all statistics and never treated as a label.
- **A human answer is one vote, not an override.** A disagreement with the automated decision does not
  flip it; it queues full-context adjudication. Repeated human/adjudicator disagreement on a task raises a
  ticket ("audit and adjudicator disagree on X"), not a silent policy change.
- **Self-limiting.** If the Uncertain rate in a week exceeds 30%, the format is judged broken for
  that task: the audit auto-shrinks, opens a ticket describing which item type fails, and no labels from
  that batch are used.
- Delivered as one item stream per week (existing `review_issues.py` ingestion), rendered as a mobile-friendly
page with embedded media; the exact vehicle (issue vs artifact/page) is an implementation choice.

### 4b. Context ladder: how much evidence should a judge see?

More context should raise accuracy and lower speed: a larger prompt carries fewer judgments per call, costs more tokens and, for the sibling judge, more Durable Object rows. The plan measures this instead of guessing.

**Tiers (per task, built by the task spec's evidence builder).** Always included (they are cheap): the tag definition and the chapter or agenda title.
- **T0:** the matched span or the quote itself only.
- **T1:** a window around it (about 90 seconds, capped at 400 words).
- **T2:** the whole chapter or surrounding section (capped at 1,800 words).
- **T3 (where it exists):** T2 plus the agenda or document text.

**Method.** (a) **The lane** ([`evals/judge`](../evals/judge/README.md), `scripts/eval_judge.py run context-ladder`) judges real rule-matched tag candidates at each tier, with planted wrong-tag controls with
known truth and, as labels accumulate, adjudicated labels. (b) **Continuous:** from P1 on every judgment records its `context_tier`, and a stratified 5% sample is judged at **all** tiers and by the adjudicator at the largest, which keeps
measuring the trade-off as tasks, routes and prompts change. The free-tier JEV cap (about 1,440 calls a day) and Durable Object rows make this sample small on purpose.

**Results (committed, 2026-09-30; 34 real candidates plus 10 controls; two prompt versions).**

| Tier | Tokens per item | Items per JEV call | JEV calls to backfill 127,719 candidates | Accuracy on 13 adjudicated items, prompt 1 | prompt 2 (maintainer rubric) |
|---|---|---|---|---|---|
| T0 matched span | 241 | 232 | 551 | 10/13 | 12/13 |
| T1 +-45 s window | 506 | 110 | 1,162 | 9/13 | 10/13 |
| T2 whole chapter | 2,140 | 26 | 4,913 | 12/13 | 13/13 |

No tier accepted any control. Under prompt 2, **judging at T1 and re-judging at T2 when the T1 probability is between 0.3 and 0.7 escalated 9 of 34 items (26%) and scored 13/13**, about 1,162 + 0.26 x 4,913, roughly **2,450 calls instead of 4,913**
(prompt 1: 29% escalated, 12/13). Cautions: the labels are Claude's as corrected by the maintainer's rulings (two items are Uncertain and excluded), 6 of the 13 were chosen because tiers disagreed, and **prompt 2 was written after seeing these items,
so its gain here is optimistic and must be confirmed on a fresh holdout (lane set version 2)**. At 13 labels the tiers cannot be separated statistically.

**The tag rubric (maintainer policy, 2026-09-30), part of the tag task spec and the `validate` question's instructions.** A tag is correct when the chapter involves a **specific project, contract, program or policy** on the topic, even when it is approved routinely (for example on a
consent agenda); it is not correct for a generic mention, a passing reference in a list or summary, a read-back of past items, or a general-purpose services contract not tied to a specific project or policy on the topic. Surfacing specific projects and policies, not only contested debate, is the point of the
tags. Marginal cases are Uncertain, which the lane and the audit both exclude from statistics. A rubric change bumps the prompt version and starts a new judge calibration cell.

**Initial rule for P1/P2 (confirmed 2026-09-30): judge at T1, escalate to T2 when p is between 0.3 and 0.7, and let the continuous 5% sample confirm or move both the tier and the band.** T0 is a candidate first tier (12/13 under the rubric) but is not yet shown to be as good as T1 or T2 at this sample size.

### 4c. Judge and adjudicator routes (added 2026-10-08)

**Status.** The "second adjudicator route" was left to the locator verdict, which is now in ([review/40](40-generated-agenda-chapters.md), 2026-10-02): the production locator is DeepSeek V4 Flash (packets up to 76,000 estimated tokens) and Kimi K3 (larger); `chapter-agenda` is Nemotron 3 Ultra, tencent/hy3 and Gemini 3.1 Flash Lite. Those stay protected. The verdict released GLM 5.3 Flash, DeepSeek V4.1 Flash, Gemini 3.5 Flash and Gemini 3.5 Flash Lite. No adjudicator had been chosen before this; the split below is a recommendation for the maintainer.

**Independence is per entry, not per lane.** Every LLM output records the model that produced it. When JEV and the sibling disagree on an entry, the adjudicator is picked from the adjudicator slots whose family differs from (a) that entry's producer and (b) the sibling judge that scored it. JEV is TypeSafe's own model, so it never collides. Two adjudicators from two different families therefore always leave at least one eligible adjudicator for any entry from any verb, provided neither sibling judge shares a family with them. Rule candidates have no producing model, so either adjudicator is eligible. This works for `r6-moments`, tags and future verbs without a per-lane list.

**Where the load is.** At 500 episodes/day the capacity table above has the sibling judge at about 470 calls/day packed per episode (about 230 packed across episodes) against a 600 RPD target, and the adjudicator at 60 to 120 packets/day. Throughput is a judge problem; the adjudicator only sees the contested residual.

| Role | Route | Why | To verify before relying on it |
|---|---|---|---|
| Anchor judge | **JEV** (`jev-1.13-free`, BeatAPI) | Pinned (section 2) | Single-flight account shared with BeatAPI's chat routes (below) |
| Sibling slot A, bulk | **Gemma 4 31B / 26B** (AI Studio, both projects) | Four 14,400 RPD pools at 30 RPM; the binding limit is about 16k tokens/minute and a 14,400-token ceiling, which fits T1 packets (about 25 items) and small T2 packets. Takes every rule candidate (99% of candidates today) and every entry not produced by a Google model | Daily token cap unrecorded; shares quota with the R5 pre-labeler lanes until P7 retires them; recorded AA is low (16.7 for 26B), which the probes and stability checks measure |
| Sibling slot B, independent of Google | **Nemotron 3 Super 120B** (OpenRouter 200 RPD + NVIDIA leg) or **Qwen3.8-27B** (Groq) | Only needed for Gemini-produced LLM entries (tagger primary, moments primaries): a few dozen packets a day. Qwen's 200k TPD (about 25 six-thousand-token packets) makes Nemotron Super the steadier default; the league can trial both | Nemotron Super has no AA or judging record here; Qwen's TPD is shared with the existing `r6-judge` lane during migration |
| Adjudicator 1 | **GLM 5.3 Flash** (OrcaRouter) | Highest recorded AA of the released routes (41.8), 1M context, 800 RPD, about 5 s, `json_object` verified | Orca's per-request free prompt cap (`err_free_prompt_cap`) at the adjudicator packet size; whether its 800 RPD is shared with Orca's other free models |
| Adjudicator 2 | **DeepSeek V4.1 Flash**, NVIDIA leg | AA 39.5, 1M context, no RPD cap at 4 RPM; read long locator packets well (89.8% joint recall on the long slice) | Slow on long packets (about 502 s median); needs the `prompt_only` method on NVIDIA; the BeatAPI V4.1 leg must not serve adjudication (below) |

Qwen and the Gemini 3.8/3.7 Flash pools are not needed as adjudicators at this volume; they remain league challengers.

**Qualification follows the spec, not a bench.** Section 4 deliberately has no frozen per-task test set: the reference is a small `gold.json` grown from clear-cut weekly audit items, reported but non-gating until it has about 20 items, with stability probes and audit agreement doing the ranking until then. Adjudicators are scored the same way, plus two additions that fit that model: (1) a deterministic **quote check**, in the section 4.1 family: the adjudicator's quoted rationale must appear verbatim in the evidence it was given, or the answer counts as unsupported; (2) the adjudicator league's score is agreement with the audited contested items, and **an adjudicator that does not beat the better of JEV and the sibling on those items adds nothing** and is relegated. No new benchmark is built; locator and agenda gold stay with their own lanes.

**BeatAPI chat routes: supplemental, never ahead of JEV.** PR #2167 put BeatAPI legs in the `deepseek/deepseek-v4-flash` and `v4.1-flash` pools that chapter-locator, tagger spill and moments use. BeatAPI's free tier is one single-flight window for the whole account (section 7): any call blocks every other model, JEV included, for its whole duration and about 60 s after. Today the Worker has no route priority. Routes are ranked free before paid, then by remaining capacity, and an idle BeatAPI leg scores as fully available, so it is preferred over a partly used OrcaRouter or NVIDIA leg about once a minute. Before JEV runs (P1), add three things so the chat routes become backups that use only time JEV is not using:

1. **A backup tier on routes** (`tier: backup` in `provider_limits.yml`, compiled into the Worker catalog): one extra sort term after free-before-paid, so a backup route is chosen only when no primary route in the job's pool has capacity. This keeps the unified model pools and needs no per-provider branch.
2. **Yield to a named route** (`yields_to: [<JEV route id>]` on the BeatAPI chat routes): at claim time a yielding route is skipped while the named route has queued or due work. JEV's own route therefore always gets the account first, and the chat routes take the idle minutes.
3. **A short output cap on the BeatAPI chat routes** (`output_context_limit` of a few thousand tokens): at the measured 140 to 235 tokens/s a borrowed slot then holds the account for well under a minute. Long locator and moments generations stay on OrcaRouter and NVIDIA.

Item 1 shipped 2026-10-08 (the BeatAPI chat routes are `tier: backup`). Item 3 was dropped: tag and moments jobs reserve 12,288 and 16,384 output tokens and the Worker only considers a route whose output limit covers the reservation, so a cap of a few thousand would have removed the routes from every current lane. With yield-to-JEV (item 2, ships with P1) JEV is never queued behind a chat call it could have taken; the remaining exposure is JEV work arriving mid-call, which waits for that call plus about 60 s. The BeatAPI chat routes are not adjudicators or sibling judges; JEV's window is too scarce to share with a per-entry role.

### 5. Route league (European-cup model)

Every verb (tagger, moment finder, judge, adjudicator, future additive verbs) has its own **league**:
a fixed number of slots and a table of competing routes. The table is scored, not voted on, so the maintainer
sets no numeric thresholds up front.

- **Score:** the route's judged admitted-precision on its own outputs (from the judge stack, probes and
  adjudication), plus structured validity, evidence fidelity, and cost/latency as tie-breakers. For the
  judge and adjudicator leagues the score is the probe/stability/adjudicator-agreement record from section 4.
  Scores are relative within the league and carry an uncertainty interval; a route with little data has a wide one.
- **Relegation and promotion:** on a fixed cadence the lowest-scoring slot is dropped and replaced by a
  challenger that either has little data (new route, gets a trial) or previously scored above the lowest
  incumbent. A route is only dropped when its interval is clearly below the field, not on noise.
- **Eligibility:** general-purpose routes only (not specialised or flagged models, e.g. a finance model
  or one with an open issue), and never a route that carries a crucial dedicated workflow (`chapter-agenda`,
  `chapter-locator` `models[0]`, the recipe/calibration-key routes of review/46). Eligibility comes from the
  provider catalog (`citypods/provider_catalog`, review/48) plus an explicit deny list in config.
- **Capacity expansion:** if a verb is *capacity constrained*, a promotion admits two routes for the one
  bumped, or one route without bumping anyone (league grows, up to a cap). The single metric is **backlog
  trend per verb**: episodes not yet processed by that verb (one unit for every verb, so they compare), growing or shrinking over a trailing window,
  after excluding shortfalls explained by ingress limits. Every other signal only moves the backlog. Because admission
  is rate-limited, backlog length is not visible today, so producers publish it:
  - Do not scan all episodes each run. Keep two cheap counters per verb per day in durable state:
    `eligible_created` (an episode reaches the verb's eligible state, incremented where the pipeline already
    records that transition) and `completed` (ledger append). `backlog = previous + created - completed`, plus a
    weekly full-scan reconciliation to correct drift. Precision matters little because backlog is large.
  - Also record `ingress_rejected` and `deferred_by_reason` from the existing `llm_submission_telemetry.py` events, so
    a growing backlog is split into "capacity" vs "ingress-limited". Worker queue depth (its pending-only index) gives
    in-flight work.
  - **To verify:** how each producer marks eligibility (the `tags` stage shows `completion-cache` skips, so
    completion is already recorded per episode) and whether a per-verb `created` count can be emitted without a scan.
- **Governance:** the league emits a config PR (`llm_lanes` edit) for the maintainer to merge, or a ticket when
  the evidence conflicts. Nothing auto-merges. The `models[0]` recipe-key rule from review/46 still applies.
- **Free rider on shadow:** a challenger runs in a shadow slot, judged like everyone else, and touches no
  published output until it holds a slot.

### 6. Simplification (candidate deletions once shadow proves out)

- Lanes `topic-tags:prelabeler`, `topic-tags:prelabeler-shadow`, `r6-judge` fold into one `judge` lane.
- `tournament:tag`, `tournament:tag-judge`, `r5-benchmark:*` and `tournament-ticket-approve` become
  the route scoreboard; pairwise (`prefer`) stays as a question type only if it earns its keep.
- The 12/90% R5 and 30-day/30-review R6 human gates and the weekly 80-item packet are retired for tasks
  that graduate; the residual queue and audit replace them.
- Two evaluators (`llm_evaluation.py`, `moment_evaluation.py`) converge on one engine.

### 7. Where JEV plugs in

Provider keys and queueing live in the dispatch Worker (`workers/llm-dispatch-v2`). Add JEV as a Worker route with `rpm: 1`, `concurrency: 1`
in `config/provider_limits.yml` (same mechanism as other slow routes), queued like other `queue_only` lanes.

**JEV API spike (2026-09-30, `jev-1.13-free`, key from `BEATAPI_API_KEY`).** `POST https://api.beatapi.io/v1/systemone`, bearer auth,
body `{model, state, questions}`. Verified live (3 successful calls, about a minute apart; no rate-limit headers are returned):
- Question types: `noul` (probability 0–1, `criteria` may be `{}`), `score` (requires a non-empty `criteria` array, lowest first, returns
  `score`, a per-level `probabilities` map, and `confidence`), `choice` (verified: named options with a description each; returns `choice`, a `probabilities` map and `confidence`, 0.7 s).
- Discrimination: a true tag scored 0.97 and a false tag 0.01 in one call; a score question returned 2.05 with a spread distribution and confidence 0.09 (low).
- **Packing works:** 14 evidence windows with one `noul` question each in one call (11,579 input tokens, 330 output tokens, 2.2 s): the window
  containing the planted fact scored 0.96, the other thirteen scored 0.02–0.03.
- Latency 0.7–2.2 s, so one call per minute is the real limit, not response time.
- **The free limit is shared with BeatAPI's free chat models (measured 2026-10-07).** `jev-1.13-free` and the five `-free` chat models (`deepseek-v4-flash-0731`, `deepseek-v4-pro`, `deepseek-v4.1-flash`, `gpt-6-astra`, `gpt-6.1-sol`) sit in one account-wide window of 1 successful request per minute: a success on any of them (JEV or chat, either order) makes every other one return `429 rate_limit_exceeded` with `Retry-After: 60`. So the roughly 1,440 calls a day are one budget, not 1,440 for JEV plus 1,440 per chat model; any BeatAPI chat route spends JEV capacity. The chat routes are registered in `provider_limits.yml` at the provider level (`rpm: 1`, `concurrency: 1`) for exactly this reason. **The account is single-flight (measured 2026-10-08, three runs):** while any BeatAPI call is running, a call to another model gets 429, so a long chat generation holds JEV out for its whole duration plus about 60 s afterwards. Output speed was 140-235 tokens/s. All five chat models accepted 234,437 input tokens with no rejection, so input size is not the limit; output length is. Keep BeatAPI chat calls short, or leave BeatAPI to JEV once P1 is live.
- **Input limits (maintainer rule, verified 2026-09-30 with random common words at about 1.01 tokens/word):** the total input is capped near
  64k tokens, and the **state plus the single largest question** must stay under about 32k. Tested: state 28k + 10 questions of about 2.5k each
  (53,576 input tokens) succeeded with 10 answers; state 28k + 14 questions (62,296 tokens) succeeded with 14 answers in 1.8 s; state 28k + 16
  questions (about 66k) failed; state 29k + one 2.5k question (31,815) succeeded; state 29k + one 5k question (about 34k) failed. Earlier
  tests with a large state and one tiny question confirm the same state-side ceiling (32,925 ok, 33.9k fail). **Design consequence:** a call can carry
  about 30k tokens of shared state plus about 30k more in the questions themselves, so evidence windows can live in the questions (each under a few thousand
  tokens), roughly doubling what one call judges. At about 25 to 60 evidence windows per call, 500 episodes/day of judging fits in tens of JEV calls, not hundreds.
  **The failure is misleading:** oversize requests return `HTTP 503 {"code":"processing_failed","retryable":true}`, not a 4xx. The Worker must enforce
  both ceilings client-side (plan for 28k state-plus-largest-question and 58k total estimated tokens, calibrated against the returned `usage.input_tokens`) and
  classify that 503 as non-retryable when the request is near either limit, or the existing 5xx retry budget will re-send a request that can never succeed.
- **Evidence in the questions versus in the state (2026-09-30, 30 items with ground truth: 10 supported, 10 near-miss "tabled/denied", 10 off-topic; about 0.6 KB of evidence each; two shuffled orders each):**
  bundling each item's evidence into its own question with a 100-byte state (5,203 input tokens) and putting all evidence in the state with 30 small questions (5,439 tokens) both scored
  accuracy 1.00 and AUC 1.000 (true items 0.96–0.99, false items at most 0.05), with no verdict flips between orders (max probability change 0.01 within a mode, 0.04 between modes). So JEV **does** answer
  questions from evidence carried in the question text, not only from the state. Limits of this test: the items were easy and synthetic (the near-misses were clearly worded), so it shows the mode is valid and order-stable, not that
  the two are equally accurate on hard real text. Cost consequence: question-bundled evidence is repeated per question, so it suits **small, non-overlapping evidence** (a rule candidate's matched phrase plus a
  window); the state is better for **shared or overlapping evidence** (one chapter judged against many tags). A call can mix both inside the 32k (state plus largest question) and 64k (total) limits.
- Caveat: a keyword-stuffed random text still scored 0.92 for "is this about zoning", so JEV is sensitive to surface terms; the stability and
  planted-probe checks in section 4 matter.
- Errors seen: `503 processing_failed` (oversize, above), `401 invalid_api_key` (a trailing carriage return in the secret; fixed), `400 bad_request` with a clear message for the `score` criteria.
- **Worker config:** secrets are not declared in `wrangler.jsonc` (its closing comment documents them, and
  `tests/test_llm_dispatch_worker_limits.py` derives the secret list from each provider account's `api_key_env` in `provider_limits.yml`).
  There is no `wrangler.toml` in the repo. `BEATAPI_API_KEY` joins that count when the `beatapi` provider entry is added (now 39 vars + 21 secrets = 60; 61 after
  BeatAPI, against a limit of 64 with 2 headroom, leaving one spare slot). Add the name to the closing "Secrets" comment in the same change.

**Superseded (2026-10-07):** PR #1965 compiled the numeric tunables out of Cloudflare vars and removed the orphan and inert accounts. With `beatapi` registered the Worker carries 1 var + 20 derived secrets (21 of 64), so the 63-of-64 pressure below no longer applies and the survey is kept as history only.

**Worker variable survey (2026-09-30, deployed list supplied by the maintainer).** The deployed Worker has **39 variables + 24 secrets = 63 of the 64-item
free-plan limit**, not the 61 the test predicts: the 39 variables match `wrangler.jsonc` exactly, but three deployed secrets are invisible to
`tests/test_llm_dispatch_worker_limits.py` because no `provider_limits.yml` account names them (`BEATAPI_API_KEY` today, plus two orphans below).
- *Delete orphan secrets (inert today):* `DISPATCH_AUTH_TOKEN` (zero references anywhere in the repo; a leftover of the retired v1 proxy naming) and `OPENCODE_API_KEY`
  (no `opencode` provider block or routes; OpenCode Zen's free models are listed in `LLM_SETUP.md` but not wired; re-add with a route).
- *Mistral (maintainer is fine removing):* `MISTRAL_API_KEY` and `MISTRAL_API_KEY_SECONDARY`; only Codestral routes exist and no lane uses them. Remove the two accounts from
  `provider_limits.yml` in the same change, or the test keeps counting them.
- *Inert direct-API secrets with no routes:* `DEEPSEEK_API_KEY` (the DeepSeek models in lanes are served by OrcaRouter/NVIDIA, not api.deepseek.com) and `SILICONFLOW_API_KEY` (the free
  models need identity verification and paid routes are deliberately absent). No free capacity is lost by removing them from the Worker; the GitHub secrets for the probe workflows can stay.
- *Keep (active free capacity):* `GEMINI_API_KEY` and `_SECONDARY`, `GROQ_API_KEY` (Qwen3.8), `KILO_API_KEY`, `NVIDIA_API_KEY`, `OPENROUTER_API_KEY`, `ORCAROUTER_API_KEY`,
  `ZAI_API_KEY` (`glm-4.7-flash`/`4.5-flash`, 500 RPD), `SAMBANOVA_API_KEY` (Gemma 4 31B), `AIRFORCE_API_KEY` (one route), the fixed infrastructure secrets, and `BEATAPI_API_KEY`.
- *Variables:* remove the dead `UNKNOWN_ATTEMPT_POLICY`; the empty `AI_GATEWAY_BASE_URL` is removable if the code treats unset and empty alike (verify); 20 variables
  equal their in-code defaults (`ATTEMPT_RETENTION_DAYS`, `BUNDLE_RETENTION_DAYS`, `CLEANUP_INTERVAL_MINUTES`, `COMPLETED_RETENTION_DAYS`, `LEASE_DURATION_SECONDS`,
  `MAX_429_BACKOFF_SECONDS`, `MAX_429_RETRIES`, `MAX_5XX_BACKOFF_SECONDS`, `MAX_5XX_RETRIES`, `MAX_ATTEMPT_PRUNE_PER_TICK`, `MAX_BUNDLE_PRUNE_PER_TICK`, `MAX_CANDIDATE_LOOKAHEAD`,
  `MAX_CONCURRENT_ROUTE_LANES`, `MAX_LEASES_PER_UTC_DAY`, `MAX_ROUTE_BUFFER_SECONDS`, `MAX_UPSTREAM_CAPACITY_RETRIES`, `PURGE_BATCH_LIMIT`, `ROUTE_UNAVAILABLE_BLOCK_SECONDS`,
  `UPSTREAM_CAPACITY_COOLDOWN_SECONDS`, `UPSTREAM_CAPACITY_MAX_COOLDOWN_SECONDS`), verify each at its call site first.
- *Structural option (separate chore):* the remaining numeric tunables could move from dashboard variables into a compiled `src/dispatch_tuning.json`, following the existing
  `dispatch_limits.json` / `ingress_reservations.json` pattern (drift-checked in `llm-dispatch-v2-worker-deploy.yml`), which frees about 25 more slots and makes changes reviewable PRs.
- Net effect: deleting the orphans, Mistral, the inert direct keys and the dead variable takes 63 to 56; the redundant 20 would take it to 36. Fix the test to count deployed secrets
  by listing them in a documented constant, including `BEATAPI_API_KEY`, so the next provider cannot silently hit 64.

## Risks

- Single-vendor dependence on a free tier (terms, availability, 1 RPM). Mitigation: consensus rule keeps
  a second judge, and fail-open means "not admitted", never "auto-admitted".
- Shared bias between judges. Mitigation: planted probes, full-context adjudication, tiny blind audit.
- Published public content is at stake for moments (embarrassment/PII). Mitigation: keep a second judge
  and a hard rule-based PII/procedural pre-filter for moments regardless of consensus.
- Backlog latency: at 1 RPM, a large catalog backfill takes days; admission for the current backlog
  should stay on existing gates until shadow data exists.

## Capacity check (maintainer figures, 2026-09-29)

Historical shorthand only: the job-to-meeting equivalence in this paragraph is superseded by
the detailed 500-meeting table and the 800-meeting reconciliation below. It does not account
for every pipeline task, batching, panel judging or full billed-row lifecycle.

About 10 new meetings/day today, 800–1,000 dispatch jobs/day budget (about 600–800 meetings, roughly
500 cities of headroom). JEV at 1 RPM allows about 1,440 calls/day, one call per chapter for tags plus one per
meeting for moments, so current volume is far below the cap and the backlog drains at whatever the cap
leaves, in the existing recent-first-then-backfill order. Probe and adjudication traffic must be budgeted
inside the same cap (target: at most 10% of calls).

## 800-meeting reconciliation (2026-10-02)

The [capacity evidence](evidence/2026-10-02-llm-800-meeting-capacity.md) scales the tables below
to 800 meetings/day,
updates locator accounting to one pinned DS4/Kimi model, includes claim-time deletion of
extra model indexes omitted from the table below, and distinguishes the current unbatched
panel from this future packed consensus design. Across-episode packing projects about 71.9k rows,
or 75.9k-79.4k after a rough correction for Gemma's actual 10k/8k usable packet sizes. Per-episode
packing projects about 95.1k, exceeding the 90k safe budget.
These are design projections, not completed production throughput; P1-P7 stay gated on shadow
results. The offline calculator is `scripts/llm_capacity_plan.py --consensus packed`.

## Capacity targets and Durable Object row accounting at 500 episodes/day

### Limits

Free-plan Durable Object: 100,000 billed rows/day for the whole account; ingress stops at 90,000 (`DO_ROWS_ENQUEUE_STOP`), claims at 97,000
(`DO_ROWS_CLAIM_STOP`). Cost model from `workers/llm-dispatch-v2/bench/rows-written/README.md` (measured 2026-09-24), per job, for an
N-model pool with 4 jobs per bundle: enqueue `4 + 2N`; claim 4.9; attempt start 3; completion 5.0; client retire 1; plus a 15% retry/requeue
allowance of 3.0 rows. Ingress admits `3 + N` write units per job. Both the ingress quota and the runtime row gate must hold.

### Measured inputs (episode-record scan, 2026-09-30)

Scanned the durable state for all 42 unique source keys (the 156 configured feeds share them; one `episodes.json` each; 26,537 episodes), so
**this is the whole catalog**, not a sample:

| Quantity | Measured |
|---|---|
| Episodes with provider chapters | 17,961 (68%); **without: 8,576 (32%)** |
| Without provider chapters, last 30 days / last 7 days | 99 of 165 (60%) / 23 of 35 (66%) |
| Without provider chapters but with an agenda | 77% overall, about 99% of the last 30 days |
| Episodes with a transcript | 12,231 (46%); last 30 days 112 of 165 (68%) |
| Provider chapters per episode | mean 11.6, median 9, p90 26 |
| Tag candidates per candidate-bearing episode (non-historical) | mean 8.3, median 5, p90 20, **99% deterministic rule candidates**: 127,719 rule (62,456 episode-scope, 65,263 chapter-scope, all displayed) vs 934 LLM (all legacy episode-scope, shadow, hidden, on 458 episodes) |
| Candidates per chapter | mean 1.8, median 1, p90 3 |
| New episodes/day (by `published` date, last 30 days) | about 5.5 (165 in 30 days); the maintainer's figure is about 10 |

Provider chapters are **less common in recent episodes** than in the backlog, so `f` (share needing agenda+locator jobs) is 32% for backlog
and about 60% for new episodes; I use 0.60 as the conservative steady state. Tagging needs a transcript and chapters, so I use 0.75 of episodes as tag-eligible.
Earlier drafts assumed 25 judged pairs per episode. The measured 8.3 per episode is almost entirely **rule** candidates, and chapter-scoped LLM candidates do not exist in storage yet, so judged pairs are about 8.4 (rule) plus the future LLM tagger's output, which I assume at about 3 per episode: **about 12 pairs per tagged episode**, unchanged from the accounting below.

### Accounting at 500 episodes/day (f = 0.60, 12 pairs per tagged episode, 10% contested)

Mix A = judge calls packed per episode; mix B = packed across episodes by token budget (JEV 28k state, Gemma ~14k).

| Lane (pool N) | Jobs/day | Enqueue | Claim | Attempt | Complete | Retire | Retry allow. | Rows/job | **Rows/day** | Units/job | **Units/day** | Recommended `daily_write_units` (1.2x) | Today's |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| chapter-agenda (3) | 300 | 10 | 4.9 | 3 | 5.0 | 1 | 3.0 | 27.0 | 8,089 | 6 | 1,800 | 2,200 | 9,300 |
| chapter-locator (6, four now, six with current targets) | 300 | 16 | 4.9 | 3 | 5.0 | 1 | 3.0 | 33.0 | 9,889 | 9 | 2,700 | 3,250 | 3,255 |
| topic-tags:tagger (3) | 413 | 10 | 4.9 | 3 | 5.0 | 1 | 3.0 | 27.0 | 11,123 | 6 | 2,475 | 3,000 | 5,580 |
| r6-moments (4, trimmed from 9) | 65 | 12 | 4.9 | 3 | 5.0 | 1 | 3.0 | 29.0 | 1,883 | 7 | 455 | 550 | 2,232 |
| judge: JEV (1, mix A) | 468 | 6 | 4.9 | 3 | 5.0 | 1 | 3.0 | 23.0 | 10,736 | 4 | 1,870 | 2,250 | none |
| judge: sibling league (2, mix A) | 468 | 8 | 4.9 | 3 | 5.0 | 1 | 3.0 | 25.0 | 11,671 | 5 | 2,338 | 2,800 | none |
| adjudicator (2, packed) | 120 | 8 | 4.9 | 3 | 5.0 | 1 | 3.0 | 25.0 | 2,996 | 5 | 600 | 720 | none |
| **Total** | **2,134** | | | | | | | | **56,385** | | **12,238** | **14,770** | |

Adding idle cron ticks (1,440) and a 1,000-row reserve for `city-onboarding`, `audit-remedy` and scheduled cleanup gives about **58,800 rows/day
(65% of the 90,000 enqueue stop)** in mix A and about **45,000 (50%)** in mix B (JEV about 116 jobs, sibling about 231). Mix A uses about 2,130 jobs/day, well under the 4–5k dispatch figure.

Sensitivity: `f` = 0.32 (backlog-only mix) gives about 50,400 rows; the agenda and locator lines are the swing factor. Retry rate,
pairs/episode, and transcript eligibility each move the total 10–15%. Headroom scaling: the same mix reaches the 85k usable rows at about **750 episodes/day
(mix A, f 0.60)** or about 1,000 (mix B), so **500 is a safe target with a real margin**, 750 is the ceiling without packing, and the `$5` plan is needed for more.
Today's quotas total about 34.8k units/day across all lanes (69.6k rows if fully used at enqueue alone), most of it on lanes this design retires
(prelabelers, tournament, benchmark), so the new quotas above free a large share of the budget.

### Per-lane capacity targets at 500 episodes/day (route RPD)

| Lane | Calls/day | Target sustained RPD |
|---|---|---|
| Tagger | about 410 (+15% retry) | 500 |
| Moments (50 meetings) | about 65 | 120 |
| JEV, evidence window per episode (mix A) | about 470 incl. probes | about 550 of the 1,440 free cap (mix B: about 116) |
| Sibling judge | about 470 (mix B: about 230) | 600 |
| Adjudicator (packed, ≤ 20 questions or route ceiling) | about 60 to 120 | 200 |
| Agenda / locator | about 300 jobs each | existing route capacity |

The 60–120 adjudicator calls/day target cannot be justified from Qwen's 1,000 RPD alone.
The observed TPD cap below makes Qwen supplemental capacity at 6k-token packet sizes; coverage
by Qwen plus a former locator route remains unproven until the combined token budget is measured.

### Token-cap evidence and sibling/adjudicator selection (2026-10-05)

Snapshot after [PR #2028](https://github.com/BashfulBits/city-meeting-podcasts/pull/2028).
Configured values below come from [`config/provider_limits.yml`](../config/provider_limits.yml);
compiled admission limits come from
[`dispatch_limits.json`](../workers/llm-dispatch-v2/src/dispatch_limits.json).
**An absent `tpd` is unknown/unconfigured, not evidence of unlimited daily tokens.**
The observations are maintainer-supplied Gateway failures and the earlier probes recorded above;
no fresh live quota probes were run for this note.

| Candidate route(s) | Configured raw TPD | Compiled TPD | Observed TPD evidence |
|---|---|---|---|
| Groq `groq_qwen_3_8_27b_primary` | 200,000 | 180,000 (10% safety margin) | HTTP 429: `Limit 200000, Used 199659, Requested 1089`; service tier `on_demand` |
| Groq Qwen 3.6 (catalog entry, not a current lane) | Absent | Absent | No numerical TPD observation recorded here; do not copy Qwen 3.8's cap |
| AI Studio Gemma 4 26B / 31B, primary and secondary projects | Absent | Absent | No numerical TPD observation recorded here |
| Gemini 3.8 / 3.7 / 3.6 / 3.5 Flash and 3.5 / 3.1 Flash Lite, both projects | Absent | Absent | Google quota 429 reported; actual quota metric and daily token ceiling not supplied |
| OrcaRouter GLM 5.3 Flash / DeepSeek v4 Flash (potential former locator routes) | Absent | Absent | HTTP 400 `err_free_prompt_cap`: per-request free prompt cap, **not TPD**; numerical cap unknown |
| NVIDIA Kimi K3 / DeepSeek v4.1 Flash and other catalog Gemma routes (NVIDIA, OpenRouter, SambaNova) | Absent | Absent | No numerical TPD observation recorded here |
| BeatAPI JEV (planned anchor; not yet in the route catalog) | Not configured | Not configured | No numerical TPD observation recorded here; the documented 1 RPM / concurrency 1 is a separate constraint |

Qwen's sample needs 748 more tokens than the remaining 341. Its advertised retry delay,
323.136 seconds, matches replenishment at `200000 / 86400` tokens/second. PR #2028 therefore
accounts for continuous token refill rather than assuming a midnight daily-token reset.
At steady state the raw allowance is about 139 tokens/minute, or 125 after the safety margin.
Budget prompt, schema, completion and reasoning tokens, with retries and probes, across **all**
consumers of this organization/model quota, including the existing pinned `r6-judge` lane during
migration. Configuring another route or key against the same quota does not multiply capacity.

At 6,000 input tokens plus 1,000 output/reasoning tokens per packet, Qwen supplies only about
**25 packets/day** from the compiled 180k allowance, before other consumers. The proposed
60–120 adjudicator packets would need 420k–840k tokens/day at that size. Smaller packets improve
this, but the 1,000 RPD figure does not establish sibling or adjudicator throughput. Packing
saves repeated evidence and per-call overhead; it does not eliminate token consumption.

**Model-selection follow-up for P1/P2:** measure actual total tokens per subject/packet at each
context tier, including thinking mode, probes, retries and contested-rate sensitivity. Record
verified TPD values, quota scope and refill/reset behavior for candidate siblings and adjudicators
before relying on them; retain unknown caps as explicit risks. Select independent families with
sufficient combined token headroom for the routine sibling load and the adjudication residual,
sharing budgets with their producer lanes. Keep Qwen supplemental unless measured packet demand
fits its remaining TPD; choose additional adjudicator capacity for the rest without assuming
paid upgrades. Google input-only TPM and per-project RPD remain separate constraints; Orca's
unpublished free prompt cap must be checked at the selected packet size even when RPD remains.
Do not graduate shadow throughput or repeat the earlier “covers it comfortably” claim until
these token budgets and admissible packet sizes have been verified.

## Lane inventory (current `llm_lanes` and non-lane purposes)

| Lane | Role | Proposed status |
|---|---|---|
| `chapter-agenda` | crucial production | protected: models never enter a league |
| `chapter-locator` | crucial production | protected (the unpushed `chapter-locator` eval work is out of scope) |
| `topic-tags:tagger` | additive verb | tagger league |
| `r6-moments` | additive verb | moment-finder league |
| `r6-judge` | judge | replaced by the shared `judge` lane, itself a league with JEV pinned plus two sibling slots (and a separate adjudicator league); today's models feed the sibling slots if eligible |
| `topic-tags:prelabeler`, `topic-tags:prelabeler-shadow` | R5 evaluator | retire once judge lane is live; shadow first |
| `tournament:tag`, `tournament:tag-judge` | route comparison | retire; replaced by league scoring |
| `r5-benchmark:tag`, `r5-benchmark:judge` | R5 benchmark | retire; its frozen sample folds into `evals/tag-judge/` |
| `city-onboarding` (purpose, `require_direct`) | discovery research, low RPD, high reasoning | protected special-purpose capacity, outside leagues |
| `audit-remedy` (purpose) | suggests board-config changes; low RPD, needs one of the highest-reasoning routes | protected like `city-onboarding`, outside leagues |
| `topic-tags:rules` (no LLM) | deterministic rule candidates | not a lane; candidates go to the judges with the tagger's, see rule audit below |

**Rule audit.** Deterministic `topic-tags:rules` candidates are judged like tagger candidates. The consensus reject rate of each rule
(per tag and matched phrase) is compared with the reject rate of LLM candidates for the same tag; a rule rejected far more often than
the baseline, with enough samples, files a GitHub issue to remove that suggestion or tighten its include/exclude phrases. It never edits
`config/taxonomy.yml` (consistent with review/42).

**Why leagues for the judge but not the locator/agenda.** Leagues fit verbs where quality is relative ("best" and "valid"): tags, moments,
judging, adjudication. `chapter-agenda` and `chapter-locator` have external ground truth (provider chapters plus the maintainer's
holdout evaluation under `evals/`), so route choice stays a measured, reviewed evaluation against that truth. The same promotion machinery
could later be reused by swapping its scoring input from judge consensus to holdout score, but the current decision is to keep them out.

Deny list for league eligibility = the protected rows above (agenda, locator targets, onboarding, audit-remedy) plus any model flagged
special-purpose in the provider catalog.

### Dispatch requirements from the row-write reductions (2026-10-08)

The #1844 Durable Object row-write stack (PRs #2155–#2160) deployed on 2026-10-08. Its first
production logs surfaced two things the judge refactor should settle, rather than patching today's
`r6-judge` lane, which this plan replaces.

1. **Single-model lanes must not retry an output-limit cut-off on the same model.**
   - **Observed:** in the first minutes after deploy, `r6-judge` jobs pinned to `gemma_4_26b` were
     requeued every minute with `output_budget_exhausted` (reply stopped at its output limit) and
     `structured_output_empty`.
   - **Why it repeats:** both classes draw on the upstream retry budget, `MAX_UPSTREAM_CAPACITY_RETRIES`
     (8) plus `backup_after_attempts`. Each cycle is a claim plus a requeueing completion, about 13
     billed rows and one provider call. That budget exists so a job can move to another route or
     model. A job pinned to one model (the JEV anchor and every `per_model` judge lane) has nowhere to
     move, so the same cut-off recurs.
   - **Requirement (P1 lane definition):** for each single-model judge lane, either:
     - size the output budget to the model's reasoning plus the answer, measured from P1 shadow
       replies; or
     - give `output_budget_exhausted` on a job with one eligible model a small retry ceiling (1–2,
       a coordinator change in `completeBatch`) and fail it to the producer, which re-packs or
       re-sizes.

   The same applies to `structured_output_empty` when it is persistent for a model, not transient.
2. **Retiring a lane cancels its queued backlog.**
   - **The problem:** P7 retires lanes (`topic-tags:prelabeler`, `-shadow`, `tournament:*`,
     `r5-benchmark:*`, the legacy `r6-judge` panel). Their queued jobs would otherwise sit in the
     coordinator indefinitely, unclaimable once the lane's routes or reservations are gone.
   - **What it blocks:** jobs queued before #2155 also hold the legacy `(job_id, model)` index in
     place. It is dropped only when no such job remains queued, and until then enqueue reserves 2
     billed rows per ingress unit instead of 1.25.
   - **Requirement (P7 checklist):** before removing a lane's `llm_lanes` entry, cancel its queued
     jobs (`/v2/jobs:cancel-batch`, `LiteLLMBackend.cancel_batch`), then confirm in
     `/v2/stats?detail=1` that its queued count is 0.
3. **Reassess the pooled queue index on the final lanes.** #2162 parks a one-queue-row-per-job
   design until these lanes are live. Once P7 leaves the final pools and volumes, run #2162's
   `/pooled` bench with that mix and apply its thresholds. Multi-model pools (sibling, adjudicator,
   moments) are what it saves on; pinned lanes gain nothing.

## Governance

Route promotions/demotions are opened as config PRs for the maintainer to merge, never auto-merged.
When the system is unsure or needs direction (audit/adjudicator disagreement, a judge tripping its
reliability floor, a new route with contradictory results), it files a ticket instead of acting.

## Shadow and switch-over (decided 2026-09-30: a tag is visible only after it is judged good)

What a person can see today: deterministic **rule** tags are displayed immediately (all 127,719 rule candidates have `display: true`); LLM tags are hidden shadow candidates until a tag/route row earns the 12/90%
human-calibrated admission (711 hidden today); R6 moments never auto-publish (`moments.mode: manual`).

**The rule after switch-over (P3), for rule and LLM tags alike: a tag is visible if and only if the judge stack has admitted it.** Unjudged, contested-and-unresolved and rejected candidates are hidden
(in the display projection only; every candidate row is kept, as review/42 already does for suppression). There are no per-source exceptions.
**Moments (P4)** use the same stack through the generalized framework (section 3a): consensus admission, with per-meeting top-K selection by `choose`, replaces the human-calibrated threshold in the `moment-admission` stage. `moments.mode` (`manual` or `auto`) stays as the global kill switch; P4 is shadow-scored first, exactly like tags, before `moments.mode` is set to `auto`.

**Shadow phase (P2) is everything before the switch.** The judges score every candidate as it is produced and every existing candidate in a recent-first backfill, and the current gates keep deciding what is visible, so nothing
the public sees changes. It exists to measure the stack (probes, stability, adjudicator agreement) before it has authority, and to do the backfill so the switch does not leave the site empty.

### Worked example

One episode (Planning Commission, a 40-minute hearing) has three candidates:

| Candidate | Source | Evidence |
|---|---|---|
| "Short-term rentals" on the chapter "Amend Chapter 12, short-term rentals" | rule (matched phrase "short-term rental") | the agenda title and 30 s of discussion |
| "Zoning" on the chapter "Approve minutes" | rule (matched the word "variance" in a read-back of last month's items) | one sentence of minutes text |
| "Housing affordability" on the same STR chapter | LLM tagger | the commissioners' discussion of rents |

| Stage | What happens | What a resident sees |
|---|---|---|
| **Today** | the two rule tags are shown immediately; the LLM tag is hidden until its route earns 12/90% admission | "Short-term rentals", "Zoning" (wrong, but shown) |
| **P2, shadow** | JEV scores the three (for example 0.97, 0.06, 0.91) and the sibling judge agrees on the first two; the stack records "admit, reject, admit" but has no authority; old gates still decide | unchanged: the same two rule tags |
| **Switch-over, day 0** | visibility becomes "judged good"; this episode was already judged in the backfill (recent episodes first), so it flips correctly | "Short-term rentals" and "Housing affordability" appear; "Zoning" disappears |
| **Switch-over, an older episode not yet judged** | its rule tags are hidden until its backfill turn comes | no tags for that episode for a few days |
| **A contested candidate** (JEV 0.62, sibling disagrees) | goes to the adjudicator with the evidence window; if still unclear it stays hidden and is a candidate for the weekly audit | hidden until decided |

**What the switch costs:** about 127,700 existing rule candidates become hidden until judged. At 12 candidates per episode and recent-first ordering, the backfill is bounded by the spare Durable Object row budget rather than JEV:
JEV packs roughly 100 to 300 candidates per call (evidence in the questions, 58k-token ceiling) so it clears the backlog in about a day of its spare free-tier calls, while the sibling judge (about 14k-token packets,
roughly 35 candidates per call, about 25 rows per job) needs about 3,600 jobs or roughly 90k rows, which at about 30k spare rows a day is **3 to 5 days**, tag browse pages thin out for that period and refill recent-first. This is an estimate; P2 measures the real
rate and P3 is not switched until the backlog is below an agreed level (see the graduation rules).

## Graduation rules (restated; the earlier "stable for two league cycles" was the wrong shape)

Two separate decisions, each stated relative to a measured baseline rather than a fixed number:

1. **Task graduation (shadow to enforced) measures the judge stack, not any tagger route.** Graduate a task when, on the same sample of judged items, the stack's decisions agree with the adjudicator
   **at least as often as the current gate's decisions do** (the 12/90% matrix plus pre-labeler overlay for tags; the manual gate for moments), and the stack's false-accept on the human-verified probe set is not worse than that of its best single judge. Routes that are not in the mix do not affect this. Because unjudged tags are hidden, P3 is also not switched on until the recent-first backfill of rule candidates has judged the most recent portion of the catalog (the maintainer sets how much, for example the last 90 days of episodes), so the site does not go blank.
2. **League promotion (a route that is not currently in the mix) uses its trial slot, not time.** A challenger with no record runs at the trial share (about 10%, capped by its own quota) until it has a minimum number of judged outputs (sized by the league, for example 100); it is promoted when its score interval's **lower bound exceeds the lowest incumbent's point estimate**, and a formerly demoted route is treated as a fresh challenger with its old record as a prior. "Stable" applies only to incumbents: an incumbent is relegated when its **upper bound** falls below the challenger's point estimate in two consecutive evaluations, never on one noisy evaluation.

## Decisions recorded 2026-09-30

- League cadence at most once per two weeks; new challengers get about 10% trial share.
- League sizing follows capacity, not model count. The firm throughput target is **500 episodes/day** until the $5 plan.
- Backlog trend (in episodes, same unit for every verb) is the constraint metric; P0 is specified at L3 in [review/50](50-p0-llm-verb-backlog-trend.md), reading the existing per-run `run_events`, with no producer changes.
- Existing human reviews are archived; probes are a small human-verified list.
- JEV packing by evidence window; no paid JEV tier. JEV answers from evidence in the questions as well as the state (above).
- `audit-remedy` and `city-onboarding` are outside leagues.
- Audit audio is a link to the meeting page at `#t=`; no clip uploads, no PAT.
- The mobile check of the `#t=` link is deferred to the website redesign.
- Approved 2026-09-30: the worked shadow and switch-over example (a tag is visible only once judged good), the two relative graduation rules, and the initial context rule (judge at T1, escalate to T2 when p is 0.3 to 0.7).
- The 15 adjudicated context labels are Claude's and were shown to the maintainer for a sanity check; they are replaced over time by audit-verified labels, not by a separate labeling exercise.

## Open questions (remaining)

1. **Judge and adjudicator routes:** recommendation in section 4c (JEV anchor; Gemma bulk sibling plus a non-Google sibling; GLM 5.3 Flash and DeepSeek V4.1 Flash adjudicators with per-entry independence), pending the maintainer's approval.
2. **BeatAPI chat routes as backups:** backup tier shipped; yield-to-JEV ships with P1 (section 4c).

## Path to L3

Status against `review/11` L3 (concrete file/function changes, test plan, sequencing DAG, migration/backfill, acceptance criteria):

| Item | Status |
|---|---|
| **P0 per-verb backlog trend** | **L3 done: [review/50](50-p0-llm-verb-backlog-trend.md)** (file list, exact algorithm, golden results from real data, tests, workflow, acceptance) |
| Phase DAG | P0 → P1 judgment ledger and JEV route in shadow → P2 scoring, probes, stability → P3 tags consensus → P4 moments → P5 blind audit → P6 leagues → P7 retire old lanes |
| Eval lane | [`evals/judge`](../evals/judge/README.md) with `scripts/eval_judge.py` (PR #1966): bundling, question types, context ladder, adjudicator; re-runnable |
| Facts gathered | catalog scan complete; rule vs LLM candidate counts measured; JEV limits, `choice`, packing and question-bundled evidence verified; Qwen limits, thinking mode and a 30/30 quality check; `gh` attach and CI token behaviour; Worker variable and secret audit (PR 1965 implements the cleanup) |
| BeatAPI provider registration | [#1967](https://github.com/BashfulBits/city-meeting-podcasts/issues/1967): secret, discovery plugin, contract tests; prerequisite for P1 |
| P1 breakout | next: adds the generalized task-spec registry of section 3a and the `context_tier` field to the judgment record; needs the judgment-record schema, the `beatapi` provider entry (`rpm: 1`, `concurrency: 1`), the `structured.py` mapping and oversize-503 classification, and the JEV real-response fixtures (captured in the spike) |
| Dispatch requirements (2026-10-08) | P1: single-model lanes get a measured output budget or a 1–2 retry ceiling for `output_budget_exhausted`; P7: cancel a retired lane's queued jobs before removing it; after P7: #2162 pooled-index reassessment. See "Dispatch requirements from the row-write reductions" |
| P2-P7 | stay L2 until P1 shadow data exists: thresholds, league scoring and the graduation comparison need measured judge behaviour; specifying them now would invent numbers |
| Judge and adjudicator routes | recommendation in section 4c; qualified the section 4 way (audit-grown probes, stability, audit agreement, quote check), no separate benchmark |
| Open decisions | shadow and switch-over shape, graduation wording (above) |
