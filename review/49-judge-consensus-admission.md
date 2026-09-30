# review/49 — Judge-consensus admission (JEV as anchor judge)

**Maturity: L2-partial (approach chosen, risks and capacity worked out; not L3 dev-ready) · authored 2026-09-29, revised 2026-09-30.
The gap to L3 is listed in "Path to L3" at the bottom; this doc is not yet in `review/11`.**

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
     `gemini-3.8/3.7/3.6-flash` (40 RPD each) are small overflow. Sharing a model with the locator is acceptable because adjudicators
     are rarely called (it only shares RPD).
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
- **Input limit (bisected 2026-09-30, random common words at about 1.01 tokens/word):** 30,326 and **32,925 input tokens succeeded**; 33,525 words
  (about 33.9k tokens), 34,450, 36,300, 40,000 and 64,000 words all failed. So the cap is about 33k tokens for state plus questions combined (documented as 32k), and the
  "64k" figure is not reachable. **The failure is misleading:** oversize requests return `HTTP 503 {"code":"processing_failed","retryable":true}`, not a 4xx context error.
  The Worker must enforce a client-side size ceiling (plan for 30k estimated tokens, calibrated against the returned `usage.input_tokens`) and must classify this
  503 as non-retryable when the request is near or above the ceiling, or the existing 5xx retry budget will re-send a request that can never succeed.
- Caveat: a keyword-stuffed random text still scored 0.92 for "is this about zoning", so JEV is sensitive to surface terms; the stability and
  planted-probe checks in section 4 matter.
- Errors seen: `503 processing_failed` (oversize, above), `401 invalid_api_key` (a trailing carriage return in the secret; fixed), `400 bad_request` with a clear message for the `score` criteria.
- **Worker config:** secrets are not declared in `wrangler.jsonc` (its closing comment documents them, and
  `tests/test_llm_dispatch_worker_limits.py` derives the secret list from each provider account's `api_key_env` in `provider_limits.yml`).
  There is no `wrangler.toml` in the repo. `BEATAPI_API_KEY` joins that count when the `beatapi` provider entry is added (now 39 vars + 21 secrets = 60; 61 after
  BeatAPI, against a limit of 64 with 2 headroom, leaving one spare slot). Add the name to the closing "Secrets" comment in the same change.

**Worker variable survey (2026-09-30; the Cloudflare token available here cannot list deployed secrets, so this is from config and source).**
- *Dead variable:* `UNKNOWN_ATTEMPT_POLICY` ("hold") appears only in `wrangler.jsonc` and the review/44 table; nothing in `workers/llm-dispatch-v2/src` reads it. Remove it.
- *Redundant variables (20):* these declare the same value as the in-code default: `ATTEMPT_RETENTION_DAYS`, `BUNDLE_RETENTION_DAYS`, `CLEANUP_INTERVAL_MINUTES`,
  `COMPLETED_RETENTION_DAYS`, `LEASE_DURATION_SECONDS`, `MAX_429_BACKOFF_SECONDS`, `MAX_429_RETRIES`, `MAX_5XX_BACKOFF_SECONDS`, `MAX_5XX_RETRIES`,
  `MAX_ATTEMPT_PRUNE_PER_TICK`, `MAX_BUNDLE_PRUNE_PER_TICK`, `MAX_CANDIDATE_LOOKAHEAD`, `MAX_CONCURRENT_ROUTE_LANES`, `MAX_LEASES_PER_UTC_DAY`,
  `MAX_ROUTE_BUFFER_SECONDS`, `MAX_UPSTREAM_CAPACITY_RETRIES`, `PURGE_BATCH_LIMIT`, `ROUTE_UNAVAILABLE_BLOCK_SECONDS`, `UPSTREAM_CAPACITY_COOLDOWN_SECONDS`,
  `UPSTREAM_CAPACITY_MAX_COOLDOWN_SECONDS`. The match is a pattern search; verify each default at its call site before deleting (some are documented on purpose).
  Removing them frees up to 20 variable slots, at the cost of pinning those values only in code.
- *Secrets with no current lane route:* `MISTRAL_API_KEY`, `MISTRAL_API_KEY_SECONDARY` (only Codestral routes exist, none in a lane), `AIRFORCE_API_KEY` (one route, none in a lane),
  `DEEPSEEK_API_KEY` and `SILICONFLOW_API_KEY` (no routes at all in `provider_limits.yml`). After the prelabeler, tournament and benchmark lanes retire, `SAMBANOVA_API_KEY`
  (only Gemma 4 31B) and `ZAI_API_KEY` (only `glm-4.7-flash`) follow. Lane model names differ from route names for OrcaRouter (for example `deepseek/deepseek-v4-flash` is served by
  `orcarouter/deepseek-v4-flash`), so confirm before deleting anything. The test counts every account's `api_key_env` in `provider_limits.yml`, so the slots come back only when the
  provider entry is removed from that file and the secret is deleted in Cloudflare.

## Risks

- Single-vendor dependence on a free tier (terms, availability, 1 RPM). Mitigation: consensus rule keeps
  a second judge, and fail-open means "not admitted", never "auto-admitted".
- Shared bias between judges. Mitigation: planted probes, full-context adjudication, tiny blind audit.
- Published public content is at stake for moments (embarrassment/PII). Mitigation: keep a second judge
  and a hard rule-based PII/procedural pre-filter for moments regardless of consensus.
- Backlog latency: at 1 RPM, a large catalog backfill takes days; admission for the current backlog
  should stay on existing gates until shadow data exists.

## Capacity check (maintainer figures, 2026-09-29)

About 10 new meetings/day today, 800–1,000 dispatch jobs/day budget (about 600–800 meetings, roughly
500 cities of headroom). JEV at 1 RPM allows about 1,440 calls/day, one call per chapter for tags plus one per
meeting for moments, so current volume is far below the cap and the backlog drains at whatever the cap
leaves, in the existing recent-first-then-backfill order. Probe and adjudication traffic must be budgeted
inside the same cap (target: at most 10% of calls).

## Capacity targets and Durable Object row accounting at 500 episodes/day

### Limits

Free-plan Durable Object: 100,000 billed rows/day for the whole account; ingress stops at 90,000 (`DO_ROWS_ENQUEUE_STOP`), claims at 97,000
(`DO_ROWS_CLAIM_STOP`). Cost model from `workers/llm-dispatch-v2/bench/rows-written/README.md` (measured 2026-09-24), per job, for an
N-model pool with 4 jobs per bundle: enqueue `4 + 2N`; claim 4.9; attempt start 3; completion 5.0; client retire 1; plus a 15% retry/requeue
allowance of 3.0 rows. Ingress admits `3 + N` write units per job. Both the ingress quota and the runtime row gate must hold.

### Measured inputs (episode-record scan, 2026-09-30)

Scanned the durable state for the 42 sources whose `episodes.json` exist in storage (26,537 episodes; the other configured feeds share
source keys or have no record file, so **this is a sample, not the whole catalog**; re-run when convenient):

| Quantity | Measured |
|---|---|
| Episodes with provider chapters | 17,961 (68%); **without: 8,576 (32%)** |
| Without provider chapters, last 30 days / last 7 days | 99 of 165 (60%) / 23 of 35 (66%) |
| Without provider chapters but with an agenda | 77% overall, about 99% of the last 30 days |
| Episodes with a transcript | 12,231 (46%); last 30 days 112 of 165 (68%) |
| Provider chapters per episode | mean 11.6, median 9, p90 26 |
| LLM tag candidates per tagged episode (non-historical) | mean 8.3, median 5, p90 20 |
| Candidates per chapter | mean 1.8, median 1, p90 3 |
| New episodes/day in this sample | about 5 (165 in 30 days); the maintainer's full-catalog figure is about 10 |

Provider chapters are **less common in recent episodes** than in the backlog, so `f` (share needing agenda+locator jobs) is 32% for backlog
and about 60% for new episodes; I use 0.60 as the conservative steady state. Tagging needs a transcript and chapters, so I use 0.75 of episodes as tag-eligible.
Earlier drafts assumed 25 judged pairs per episode; the data says about 12 (8.3 LLM candidates plus an assumed ~4 rule candidates), which shrinks judging by half.

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

At 500 episodes/day the adjudicator need is small enough that Qwen3.8-27B (about 6k-token packets, 1,000 RPD) plus one former locator route covers it comfortably.

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

## Governance

Route promotions/demotions are opened as config PRs for the maintainer to merge, never auto-merged.
When the system is unsure or needs direction (audit/adjudicator disagreement, a judge tripping its
reliability floor, a new route with contradictory results), it files a ticket instead of acting.

## Decisions recorded 2026-09-30

- League cadence at most once per two weeks; new challengers get about 10% trial share.
- League sizing follows capacity, not model count. The firm throughput target is **500 episodes/day** until the $5 plan.
- Backlog trend is the constraint metric, counted in episodes for every verb; the per-verb rollup is the first work item.
- Existing human reviews are archived; probes are a small human-verified list.
- JEV packing by evidence window; no paid JEV tier.
- `audit-remedy` and `city-onboarding` are outside leagues.
- Audio clips only when needed, as a tiny mp4 via `gh issue create --attach`.

## Open questions (remaining)

1. **Full-catalog scan:** the 2026-09-30 scan covered 42 of 156 sources (26.5k episodes). Re-run for the rest once the state layout for the other sources is clear (many feeds share a source key).
2. **Audit clips and authentication (canary failed for `GITHUB_TOKEN`):** accept a classic PAT secret with `repo` scope just for the audit job, render moment clips outside GitHub, or keep the audit text-only and open the clip-bearing items from the maintainer's machine.
3. **Second adjudicator route:** decided once the locator verdict is final.
4. **Qwen quality:** the probe covered limits, thinking mode and structured output, not adjudication quality; that needs a small run on real contested items.

## Path to L3 (what is missing; nothing here is decided yet)

Per `review/11`, L3 needs concrete file/function changes, a test plan, a sequencing DAG, migration/backfill and acceptance
criteria. This doc has the approach, the data on capacity and cost, and the risks. It does **not** yet have:

1. **Phase split.** One L3 for the whole design is too large for safe implementation. Proposed DAG, each phase its own breakout/issue set:
   P0 per-verb episode-backlog rollup and quota re-sizing (no behaviour change) → P1 unified judgment ledger and a judge lane with the
   JEV route (shadow only) → P2 shadow scoring, probes, stability checks → P3 consensus admission for tags → P4 moments → P5 blind audit →
   P6 route leagues → P7 retire prelabeler/tournament/benchmark.
2. **Data model.** Exact schema for the judgment record, the league/scoreboard state, the backlog rollup, and the probe set (`evals/tag-judge`,
   `evals/moment-judge`), including how it coexists with `llm_tag_candidates`, `moment_*_candidates`, `llm_evaluation.json` and
   `r6_moment_evaluation.json`, and the archive step for old human reviews.
3. **JEV integration spike (API shape, `choice`, packing and the 33k input cap are verified above; the Worker route, `structured.py` mapping and oversize-503 classification remain).** BeatAPI request/response format (shared state plus typed questions), structured-output mapping, error classes,
   how it fits `provider_limits.yml` (`rpm: 1`, `concurrency: 1`) and the Worker route; needs a live key.
4. **File/function plan and tests** per phase, written against the real modules (`llm_evaluation.py`, `moment_evaluation.py`,
   `moment_judging.py`, `tags.py`, `llm_lanes.py`, the Worker), plus acceptance criteria and migration/backfill for each phase.
5. **Open decisions:** second adjudicator route (after the locator verdict), audit clip authentication (above), a fuller catalog scan, and a
   small Qwen adjudication-quality run.
