# review/53 — Judge stack: tags and moments, from shadow to authority

**Maturity: L3 (development-ready) · authored 2026-10-08 · build spec for review/49 phases P1 to
P5 and P7, plus a review/48 lane-repair extension (PR12).** P6 (route leagues) is outside this series; the configuration here is shaped so P6 can
rotate routes without schema changes.

Parent design: [review/49](49-judge-consensus-admission.md) (sections 2, 3a, 4, 4a, 4b, 4c, 7 and
the graduation rules). P0 is [review/50](50-p0-llm-verb-backlog-trend.md). An agent implementing
any PR here should not need to infer anything from review/49. If something is ambiguous or a live
result contradicts this document, stop and ask (AGENTS.md "Implementing from a breakout doc").

## End state (what "done" means for this series)

- **Tags and moments are both admitted by the judge stack.** A tag is visible if and only if the
  stack admitted it. A moment is published if and only if the stack admitted it and it is among the
  meeting's top K by the `choose` question, and it passes the existing deterministic safety and
  technical gates. `moments.mode` stays as the global kill switch.
- **No routine human approval.** The only human touch is the weekly blind audit (at most 12 items,
  about 10 minutes, review/49 section 4a). It is monitoring: a disagreement queues adjudication and,
  if repeated, a ticket; it never flips a decision and never re-approves a task.
- **The old flows are gone:** R5 pre-labeler lanes and the 12/90 human matrix, the weekly 80-item
  tag packet, the R5 benchmark, the tag tournament, the R6 judge panel, R6's human-calibrated
  admission threshold and the R6 moment review workflow. Their queued jobs are cancelled and their
  human labels archived (never read by policy).

## What "shadow" means here

Shadow is a **temporary, per-task phase with an exit condition**, not an operating mode. While a
task is in shadow, the judges score everything and the current gate still decides what is visible,
because nothing has yet shown the stack agrees with the adjudicator at least as often as the current
gate does. A task leaves shadow once, through a graduation PR (section "Graduation"), when (a) the
recent catalog has been judged and (b) the graduation comparison passes. After that the same lanes
are the authority; nothing re-enters shadow except a prompt or rubric change, which starts a new
calibration cell for that question only. The judge lanes are production lanes from the start and
are reserved accordingly.

**review/48 scope reconciliation, maintainer decision 2026-10-08:** this series owns graduation.
The legacy R5 mirrored-human-review promotion automation in
[review/48 §8.4](48-provider-catalog-reconciliation.md#84-slice-2b-paid-decisions-legacy-shadow-exit-superseded)
is superseded, avoiding new automation/storage access for the pre-labelers that PR11 retires.
Review/48 Slice 2b finishes with paid-route decisions. Its disabled-shadow build prerequisite
shipped in #2188 and supports PR11's lane retirement; it does not graduate the judge stack or
archive any review state. Graduation and archival remain this design's responsibility.

## Decisions this spec relies on (all recorded in review/49)

| Decision | Source |
|---|---|
| JEV is the anchor; Gemma 4 31B/26B is the bulk sibling; Nemotron 3 Super is the sibling for Google-produced entries; GLM 5.3 Flash and DeepSeek V4.1 Flash (NVIDIA) are adjudicators; independence is per entry | section 4c, approved 2026-10-08 |
| Judge at T1; escalate to T2 when JEV p is in [0.3, 0.7]; a 5% stratified sample at every tier | section 4b, approved 2026-09-30 |
| The tag rubric (specific project, contract, program or policy) | section 4b, maintainer 2026-09-30 |
| Relative graduation rules; nothing auto-merges; contested and unsure cases file tickets | review/49 graduation rules and governance |
| Existing human reviews are archived, never read by policy | section 4, 2026-09-29 |
| BeatAPI is one single-flight free window shared by JEV and the chat routes; chat routes are `tier: backup` and yield to JEV; calls go through `custom-beatapi` | section 7, PRs #2177 and #2181 |
| Capacity is sized for 800 meetings a day, like the other lanes | maintainer, 2026-10-08 |

## Measured facts used below

- JEV: `POST /v1/systemone`, body `{model, state, questions}`; types `noul`, `score` (non-empty
  `criteria`), `choice`. Plan for 58,000 total and 28,000 state-plus-largest estimated tokens;
  oversize answers `503 {"code":"processing_failed","retryable":true}`; 0.7 to 2.2 s; response
  `{answers: {<id>: {...}}, usage: {input_tokens, output_tokens}}`. Real responses:
  `evals/judge/results/2026-09-30-*-jev*.json`.
- BeatAPI account: about 1,440 successful calls a day at most, shared by JEV and the chat routes.
- Gemma 4 31B/26B on AI Studio: four pools of 14,400 RPD, 30 RPM, 16,000 TPM, ceiling 14,400; usable
  packet about 10,000 estimated input tokens.
- Context tiers: T0 about 241 tokens/item, T1 about 506, T2 about 2,140.
- Tags: 127,719 rule candidates and 934 legacy LLM candidates in storage; about 12 judged pairs per
  tag-eligible episode going forward (8.4 rule plus about 3 LLM).
- Ingress: `global_write_budget` 25,600 units a day, 13,186 reserved; a pinned job costs 4 units.
  `scripts/llm_capacity_plan.py --meetings 800 --consensus packed` projects 186 JEV, 370 sibling and
  192 adjudicator jobs and about 72,000 billed DO rows a day for all LLM lanes, using row costs
  measured before #1844 (today's are lower, so this is conservative).

## Capacity and reservations at 800 meetings a day

Assumptions are the calculator's: 800 meetings, 75% tag-eligible (600 episodes, 7,200 tag
subjects), 10% moments-eligible (80 meetings, 400 pull quotes), 10% contested. Two design choices
keep the sibling within budget: **only JEV escalates to T2** (the sibling stays at T1 except in the
all-tier sample), and **moment questions for one meeting share one packet**.

| Lane | Work per day at 800 meetings | Jobs/day | Reserved units (jobs x 4 x 1.2) | Daily cap (backfill headroom) |
|---|---|---|---|---|
| `judge:anchor` (JEV) | tags: 66 T1 + 72 T2 + 16 sample packets; moments: 20 packets (4 meetings each); +10% probes | about 190 | **920** | 1,400 jobs (5,600 units): the account limit, less a margin for the chat backups |
| `judge:sibling` (Gemma, Nemotron 3 Super) | tags T1: about 216 Gemma + 72 Nemotron; sample: about 93; moments: about 60 Nemotron (most moments are Gemini-produced) | about 450 to 500 | **2,400** | 2,000 jobs (8,000 units) |
| `judge:adjudicator` (GLM 5.3 Flash, DeepSeek V4.1 Flash) | contested about 760 items plus a 3% random calibration sample about 230, at up to 20 per packet; calculator figure kept as the conservative bound | 50 to 192 | **920** | 400 jobs (1,600 units) |
| **New total** | | | **4,240** | |

Released when the old flows retire (PR10 and PR11): `topic-tags:prelabeler` 1,860, `r6-judge` 650,
`tournament:tag` 310, `tournament:tag-judge` 620, total **3,440** reserved, plus large shared-only
daily allowances (pre-labeler shadow 9,600, `r6-judge` 16,000, benchmark 4,960, tournament 3,720).
Reserved total: 13,186 today, 17,426 during the series, **13,986 at the end** (of 25,600). DO rows
for the three judge lanes at the routine volume: about 25,000 a day at pre-#1844 row costs; all
lanes together about 77,000, under the 90,000 enqueue stop, and lower once the retired lanes stop.

**Measured, not trusted:** these are projections. P0's backlog trend (review/50) reports each
judge purpose's backlog from the first day, and PR3 extends `scripts/llm_capacity_plan.py` to model
judging per task (escalation share, sample, moments packing) so reservations are re-derived from
measured values. Whether to re-size one purpose or scale the whole 800-a-day plan is decided from
that data after the series, as agreed.

Backfill at the daily caps: tags about 2 days for JEV and about 3.5 for the sibling; moments
measured in the PR4 dry run.

## Judge pools: active and eligible

Each judge lane lists **active** models (dispatched) and **eligible** models (qualified to be
substituted, not dispatched). P6's league moves models between the two by config PR. Until P6, a
maintainer can swap them by hand.

| Lane | Active | Eligible, not active |
|---|---|---|
| `judge:anchor` | `typesafe/jev-1.13` | none |
| `judge:sibling` slot A | `google/gemma-4-31b-it`, `google/gemma-4-26b-a4b-it` | none |
| `judge:sibling` slot B | `openrouter/nvidia/nemotron-3-super-120b-a12b:free` | `qwen/qwen3.8-27b` |
| `judge:adjudicator` | `zai/glm-5.3-flash`, `deepseek/deepseek-v4.1-flash` (NVIDIA leg only) | none yet |

Qwen3.8 as eligible-not-active fits slot B: it is a different family from Google, it has a quality
record (30/30 on the easy set), and its 200k-token daily cap makes it a poor primary but a good
substitute. Its cap is shared with `r6-judge` until PR10 retires that lane. Eligible models are
validated like active ones (family known, route exists, independence rules hold) so a swap cannot
introduce an invalid panel.

## Cross-series order (maintainer goal, 2026-10-09)

1. review/48 Slice 5 contract (#2208, merged); its implementation (#2209) runs in parallel.
2. This series PR1 and PR2, with review/51 P2 in parallel.
3. PR3 and PR4 (shadow on).
4. review/51 P3: JEV shadow for remedy claims, through PR1 to PR3's dispatch integration.
5. PR5 to PR12 alongside review/51 P4 and P5.

Merge one PR at a time: review/48 Slice 5 and PR1 both edit `coordinator.js` and `index.js`, and
every series edits CHANGELOG, ARCHITECTURE and review/11. After each PR here the maintainer is asked
to complete any applicable parallel work in this list before the next one starts.

## The PR series

```text
PR1 Worker api_shape (systemone), request_path, oversize rule, yields_to
 |
PR2 Config: JEV route, yields_to, judge lanes + reservations, llm_families, pools
 |
PR3 Python judging package; JudgeStage for tags AND moments (shadow); judge.yml; capacity model
 |
PR4 Enable shadow for tags and moments; 3-day observation; dry-run and acceptance recorded
 |
PR5 Adjudication + calibration sample + quote check + stability probes          (review/49 P2)
 |
PR6 Probe set (gold.json) + weekly blind audit                                  (review/49 P5)
 |
PR7 Calibration report and threshold proposal (per task, per question)
 |---------------------------------------------.
PR8 Tags graduation: display = admitted          PR9 Moments graduation: admission = stack + top-K
 |                                              |
PR10 Retire R6 judge, R6 human calibration,      PR11 Retire R5 pre-labelers, tag packet,
     r6-moment-review; cancel queued jobs;            benchmark, tournament; cancel queued jobs;
     archive labels; release reservations             archive labels; release reservations
                         \                     /
                          PR12 League-governed lane repair (review/48 §8.6 extension)
```

PR1 and PR3 can be built in parallel. PR8 and PR9 are independent of each other, and each needs
PR7. PR12 needs PR8, PR9 and review/48's Slice 3b (#2193) merged. PR10 needs PR9 and PR11 needs PR8, so neither task loses its gate before the replacement holds
authority. PR8 and PR9 are merged by the maintainer; each carries its graduation evidence.

### PR1 — Worker: a second API shape

**Implemented** on `feat/review-53-pr1-systemone-transport` (2026-10-09). The field is named
`api_shape`, not `transport`: the Python route model already uses `transport` for direct versus
Worker dispatch.

Route field `api_shape` (`"chat"` default, or `"systemone"`) and optional `request_path` (replaces
the provider's `chat_path` and `ai_gateway_chat_path` for that route), validated in
`scripts/compile_llm_limits.py` and carried in `_WORKER_ROUTE_FIELDS`. A non-chat route compiles
with no `direct` transport, so Python never sends it a chat completion. New
`workers/llm-dispatch-v2/src/api_shapes.js`, keyed by shape name, never by provider; `gateway.js`
builds requests and `index.js` checks replies through `apiShapeFor(route)`:

| Function | `chat` (existing functions, unchanged) | `systemone` |
|---|---|---|
| `buildRequest` | `upstreamRequestForRoute` | `{model: route.upstream_model, state, questions}` from `payload.systemone`; no `max_tokens`, `response_format` or reasoning controls |
| `emptyCompletion` | `upstreamEmptyCompletion` | 2xx without an `answers` object, or with `error` |
| `replyProblem` | `structuredReplyProblem` for structured payloads | `structured_output_empty` when more than 10% of question ids have no answer |
| `observedTokens` | `usage.prompt_tokens` / `completion_tokens` | `usage.input_tokens` / `output_tokens` |
| `lengthTruncated` | `finish_reason === "length"` | false |

`classify.js`: on a `systemone` route, `503` with `code: "processing_failed"` is `request_defect`
(rule `systemone-oversize-503`) when the job's scaled input estimate is at least 85% of the route's
`hard_input_ceiling`, otherwise `upstream_capacity` (`systemone-processing-503`). `index.js` makes
a 5xx classified `request_defect` a `terminal_error`, so the oversize job ends and the producer
re-packs it, instead of the 5xx budget resending a request that cannot fit.

**`yields_to`** (route field, list of route ids, compile-validated to exist and not be the route
itself). In the claim loop (`coordinator.js`), a yielding route is skipped while any named route's
logical model has a row in `job_models`: `SELECT 1 FROM job_models WHERE model = ? LIMIT 1`, cached
per claim. That is one indexed single-row read per named model per claim, which fits review/47's
row-read budget. Skips are counted as `rejections.route_yield` in the claim diagnostics.

**Slice 3a interaction (review/48 §8.5, #2191).** `yields_to` and `tier: backup` are ordering
terms only. They must never enter `routesEligibleFor` or the structural fit used by the rescue pass
(`_reconcileUnroutableJobs`, terminal reasons `unadmissible` and `route_retired`): a job whose only
routes are yielding BeatAPI chat routes (today `deepseek/deepseek-v4-pro` and both GPT pools) is
temporarily held while JEV has work, exactly like a pause, and is never failed as structural.

Tests (`test/api_shapes.test.js`, a `yields_to` claim test in `test/dispatch.test.js`, compiler
tests in `tests/test_compile_llm_limits.py`): the chat shape is the existing functions; `systemone`
request shape, reply checks, usage and the 10% missing-answer threshold; `request_path` through
`custom-beatapi` and directly; the oversize rule both ways, and a terminal outcome only for the
oversize case; a yielding chat job held (still `queued`) while JEV work is queued, then claimed on
its yielding route; compile validation of all three fields and the Worker-only transport. Each new
Worker test was shown to fail with its feature removed. All 415 existing and new Worker tests pass.

### PR2 — Configuration

**Implemented** on `feat/review-53-pr2-judge-config` (2026-10-09). Two deviations from the text
below, both deliberate: (1) the judge lanes register with `reserved_write_units: 0`; the
reservations in the capacity table move to PR4, because a lane nothing dispatches to must not take
headroom from the others (`tests/test_llm_lanes.py` enforces this through
`PENDING_DISPATCH_PURPOSES`). The daily caps and run caps are as specified. (2) Slots are named
`google` and `non_google`. The weekly live gateway contract probe also skips Worker-only routes.

`config/provider_limits.yml`:

```yaml
  - route_id: beatapi_jev_1_13_free
    model: beatapi/jev-1.13
    model_key: typesafe/jev-1.13
    provider: beatapi
    upstream_model: jev-1.13-free
    api_shape: systemone
    request_path: /systemone
    input_context_limit: 64000
    output_context_limit: 4096       # reservation only; JEV output is small
    hard_input_ceiling: 58000
    account_id: primary
    rpm: 1
    rpd: 1400
    concurrency: 1
    free: true
    input_per_token: 0.0
    output_per_token: 0.0
    upstream_429_default: own_rpm
    retry_after_trustworthy: true
```

and `yields_to: [beatapi_jev_1_13_free]` on the five BeatAPI chat routes. Add the `custom-beatapi`
`/systemone` path to the recorded gateway registrations test.

**Keep chat-only tooling away from the first non-chat route** (added 2026-10-09 after reviewing
open PRs #2215 and #2218). Each of these would otherwise send JEV a chat completion, spending the
shared BeatAPI window, or report it as permanently pending:
- review/48 Slice 5's context scan (`citypods/provider_catalog/reconcile.py:plan_context_scan`):
  include only routes whose `api_shape` is `chat` (absent). Slice 5 already defers BeatAPI as an
  unknown quota scope and never counts `systemone` usage as chat capacity, so this removes a
  permanent "deferred" line, not a hazard.
- The provider-catalog reconciler's health check of configured routes (the chat canary in
  `citypods/provider_catalog/probe.py`): skip non-chat routes; JEV's health is visible from its own
  judge-lane telemetry.
- `citypods/llm_rate_probe.py`'s route catalog: skip non-chat routes.
Each gets a regression test with a `systemone` route in the fixture catalog.

`config/site_config.yml`: lanes `judge:anchor`, `judge:sibling`, `judge:adjudicator` with the
reservations and caps in the capacity table, `dispatch_shape: per_model`, telemetry
`{producer: judge, unit: episode, completion: consumed, scope: retained_catalog}`, and
`active`/`eligible` lists (a lane's `models` = active; new key `eligible_models`, compiled and
validated but never dispatched). A `slots:` sub-key on `judge:sibling` names slot A and slot B.
Judge lanes declare **no `backup_models`** (compile-time check): a judge's identity is part of
every judgment and calibration cell, so the Slice 3a rescue must not silently move a judge packet
to another model. A judge job whose route disappears fails as typed structural and is re-planned by
PR3's recovery hook instead.
Top-level `llm_families:` maps every model in every lane (active and eligible) to a family: `google`
(Gemini, Gemma), `deepseek`, `zai`, `moonshot`, `stepfun`, `nvidia` (Nemotron), `qwen`, `tencent`,
`typesafe` (JEV), `unverified-beatapi-gpt`. A `judging:` block:

```yaml
judging:
  tasks:
    tag:    {mode: shadow}       # shadow | authority; PR8 flips to authority
    moment: {mode: shadow, top_k: 3}   # PR9 flips to authority
  all_tiers_sample_rate: 0.05
  calibration_sample_rate: 0.03  # random non-contested subjects sent to the adjudicator (PR5)
  stability_sample_rate: 0.02    # re-asked shuffled and paraphrased (PR5)
  backfill_order: recent_first
  enabled: false                 # PR4 sets true
```

### PR3 — Judging package, stage and workflow

**Registry** (`citypods/judging/tasks.py`): `QuestionSpec(id, kind, instruction, levels,
prompt_version)`, `Subject(task, subject_id, episode_uid, group, producer_model, payload)`,
`Evidence(tier, text, digest)`, `TaskSpec(name, questions, subjects, evidence, first_tier,
escalation, consensus_policy, selection_policy, top_k)`; `register`, `task`, `registered`.
Registration rejects duplicate names or question ids, a `grade` without levels, and a `choose` in
an `all_admitted` task. `evidence` returns None when a tier cannot be built; that subject is not
judged at that tier.

**Tag task** (`tag_task.py`): subjects from `Episode.tags` (rule) and `Episode.llm_tag_candidates`
(LLM), episode- and chapter-scope; question `supported` (validate) with the section 4b rubric plus
the tag definition from `config/taxonomy.yml`; T0 matched spans, T1 plus or minus 45 s capped at 400
words, T2 the chapter capped at 1,800 words (episode scope: 1,800 words around the first evidence
`t`); `first_tier="T1"`, `escalation=("T2", 0.3, 0.7)` applied to JEV only;
`selection_policy="all_admitted"`.

**Moment task** (`moment_task.py`, scheduled from PR4): subjects from
`Episode.moment_pullquote_candidates` that pass the deterministic verbatim-and-timing check;
questions `supported` (validate: the quote says what its reason claims), `publishable` (gate: PII,
mocking or embarrassing a person, procedural; any judge above the flag level holds the subject),
`usefulness` (grade, levels from `PULL_QUOTE_CRITERIA`, the tie-break), `best` (choose among the
meeting's survivors, asked in two shuffled orders; an order flip is contested); evidence: the quote
plus 60 s either side at T1, plus the agenda item at T2; `group` = meeting uid;
`selection_policy="top_k_per_group"`, `top_k` from the new `judging.tasks.moment.top_k` (default
3; there is no existing per-meeting limit to reuse). The existing `quote_safety_gate` and `technical_video_gate` stay in front of any
judging and in front of publication.

**Families and independence** (`families.py`): `family_of(model)` raises for an unknown model.
Sibling per subject: slot A unless the producer family is `google`, then slot B. Adjudicator per
subject: the first active adjudicator whose family differs from the producer and from the sibling
that scored it. Rule candidates have family `rule`.

**Judgment record** (`ledger.py`), appended to the candidate's new `judgments` list, following
R6's `judge_assessments` precedent (no new state file):

```python
@dataclass(frozen=True)
class Judgment:
    schema: Literal[1]
    task: str
    subject_id: str
    question_id: str
    kind: Literal["validate", "gate", "grade", "choose"]
    judge_model: str
    judge_route: str
    judge_role: Literal["anchor", "sibling", "adjudicator", "audit"]
    prompt_version: str
    context_tier: Literal["T0", "T1", "T2", "T3"]
    evidence_digest: str
    value: float | int | str | bool
    probabilities: Mapping[str, float] | None
    confidence: float | None
    reason: str | None            # chat judges and adjudicators: quoted evidence, <= 300 chars
    quote_valid: bool | None      # PR5 deterministic check; None for JEV
    packet_id: str
    sample: Literal["routine", "escalation", "all_tiers", "calibration", "stability", "probe"]
    judged_at: str
```

`subject_id` = `"subj-" + sha256(json(task, episode_uid, chapter_id, subject key, source_kind,
producer_model, rule_version, sorted evidence))[:24]` (`subjects.py`). It must not use the existing
`candidate_id`, which changes when the pre-labeler runs. Dedup key: `(subject_id, question_id,
judge_model, prompt_version, context_tier, evidence_digest)`. Rows are never modified or deleted;
failed, malformed, deferred or oversize calls write no row.

**Backends** (`backends.py`): `JevBackend` compiles `validate`/`gate` to `noul`, `grade` to `score`,
`choose` to `choice`; tag evidence goes in the question text, a moment meeting's evidence in the
shared `state`; ids `<subject_id>:<question_id>`. `ChatJudgeBackend` uses contracts
`judge-validate-v1`, `judge-gate-v1`, `judge-grade-v1`, `judge-choose-v1` registered through
`citypods/compute/structured.py`, each with an `id` and a short `reason`. A missing answer produces
no row and an `answer_missing` count.

**Packing** (`packing.py`): greedy, deterministic, recent-first by `Episode.published`; never splits
an item or truncates evidence; an item over the ceiling alone is counted `payload-too-large` (the
existing blocked outcome) and skipped. Ceilings: JEV 58,000 total and 28,000 state-plus-largest, 60
questions; Gemma 10,000 tokens, 25 items; Nemotron 3 Super 24,000 tokens, 25 items; adjudicators
20 questions and their route ceiling. Moment questions for one meeting stay in one packet.

**Stage** `JudgeStage` (`citypods/stages.py`, an `LLMProducerStage`, name `judge`) in
`LANE_STAGES["judge"]` only. Per episode and enabled task: build subjects and due evidence, choose
judges per subject, pack, submit with the purpose's pinned policy, parse and append; schedule JEV
escalation and the all-tier sample; persist through the existing record write path under the normal
`stop()` budget. In `shadow` mode the stage writes judgments only. In `authority` mode it also
writes the task's decision (PR8, PR9).

**Structural recovery (Slice 3a).** Each judge job carries the standard recovery context
(`llm_deferred.recovery_context`, input identity = packet digest) and an output reservation within
the route's `output_context_limit` (JEV: 1,024 against 4,096), so the fit check matches what the
Worker admits. The judge recipe identity includes the judge model, tier and prompt version. On a
`structural_blocked` marker with `await_eligible_generation_or_rebatch` for a judge packet, the stage
takes the **rebatch** branch, never the same-recipe resubmission: it writes no judgment, leaves the
marker as the audit record, and lets the packet's subjects re-enter the next run, re-packed for the
current active judges (which, after a PR12 promotion, is a different model and therefore a new
recipe that does not collide with the blocked marker). A test covers a judge route retired with
queued packets: no row written, subjects re-planned once, no unchanged-recipe resubmission.

**Workflow** `.github/workflows/judge.yml`: `python -m citypods.cli enrich --lane judge` every two
hours, concurrency group `judge`, secrets and state sync as `tag.yml`, a `dry_run` dispatch input.

**Capacity model**: `scripts/llm_capacity_plan.py` gains per-task judging inputs (escalation share,
sample rates, moment packing) so the reservation table above is reproducible.

**Report**: `python -m citypods.judging.report`: judgments per day by judge, task and tier;
anchor-versus-sibling agreement by tier; escalation rate; backfill progress for the last 90 days.

### PR4 — Shadow on, observed

Set `judging.enabled: true` with both tasks in `shadow`, set the judge lanes' reservations
(anchor 920, sibling 2,400, adjudicator 920 units) and remove them from
`PENDING_DISPATCH_PURPOSES` in `tests/test_llm_lanes.py`. Acceptance, recorded in this document:
a dry run within every ceiling; then three days with judgments appended for both tasks and both
judges every day; zero change to `display`, `admission` or moment publication (before/after
comparison of sampled records); no JEV `request_defect` except genuine oversize; BeatAPI chat
routes admitted only while no JEV job was queued; DO rows under the enqueue stop each day.

### PR5 — Adjudication, calibration sample, quote check, stability (review/49 P2)

- **Adjudication.** A subject is contested when anchor and sibling disagree at p = 0.5, or JEV p is
  in the escalation band at its highest judged tier, or (moments) the `choose` winner flips with
  order. Contested subjects plus a 3% random calibration sample of uncontested ones go to the
  per-subject adjudicator with the largest tier, and a disprove-me instruction that must quote the
  evidence. Lane `judge:adjudicator`.
- **Quote check** (deterministic): the `reason` quote of a chat judge or adjudicator must occur
  verbatim (whitespace-normalised) in the evidence it was sent; otherwise `quote_valid: false`, and
  the judgment counts as unsupported in every statistic.
- **Stability**: 2% of subjects re-asked with shuffled question order and a paraphrased instruction
  (`sample: "stability"`); flips are recorded per judge.
- No decisions yet; data only.

### PR6 — Probe set and weekly blind audit (review/49 P5)

- `evals/judge/<task>/gold.json`: plain list of human-verified items, empty at start; probes are
  mixed invisibly into normal packets (`sample: "probe"`); reported per judge, non-gating until a
  task has about 20 items.
- Weekly audit issue created with `gh issue create` from a workflow: at most 12 items selected by
  expected information (judge/adjudicator disagreement, items near a threshold, the newest or
  noisiest judge) plus about 2 random; one binary claim per item framed to disprove; text first;
  moment audio as a link to the meeting page at `#t=`; verdicts hidden until answered; choices
  Supported / Not supported / Uncertain. Answers ingest through the existing `review_issues.py`
  path as `judge_role: "audit"` judgments. Clear-cut answers can be promoted to `gold.json` with one
  click. Uncertain over 30% in a week files a ticket and discards that batch.
- A human answer is one vote: a disagreement queues full-context adjudication; repeated
  audit-versus-adjudicator disagreement on a task files a ticket. Nothing flips automatically.

### PR7 — Calibration report and threshold proposal

`python -m citypods.judging.calibrate --task <task>` computes, per question kind, from the
adjudicated set (contested plus the random calibration sample, weighted to the population):
- `validate`: the pair `(t_admit, t_reject)` on JEV p, with sibling agreement required to admit,
  that maximises agreement with the adjudicator subject to a false-accept rate no higher than the
  current gate's on the same items;
- `gate`: the flag level per judge whose false-hold rate on adjudicated items is lowest at zero
  missed flags (safety errs to holding);
- `grade`: agreement within one level; `choose`: decided only when both orders agree.
It writes the proposal into `config/site_config.yml` `judging.tasks.<task>.thresholds` with the
policy fingerprint, and the evidence (sample sizes, intervals) into the PR description. The
maintainer merges. A threshold change re-projects display from stored judgments without recalling
any judge (review/42 pattern).

### Graduation (PR8 tags, PR9 moments)

The review/49 rule, applied per task on the same sample of adjudicated items:
1. the stack's decisions agree with the adjudicator at least as often as the current gate's (tags:
   the 12/90 matrix plus pre-labeler overlay; moments: the manual R6 gate), and
2. the stack's false-accept on the probe set is not worse than its best single judge's (reported,
   not gating, until the probe set has about 20 items), and
3. tags only: the recent catalog the maintainer names (default: the last 90 days of episodes) is
   fully judged, so the switch does not blank the site.

**PR8** flips `judging.tasks.tag.mode` to `authority`: the tag display projection becomes "visible
iff admitted by the stack" for rule and LLM tags alike; unjudged, contested-unresolved and rejected
candidates are hidden in the projection only, every candidate row kept.

**PR9** flips `judging.tasks.moment.mode` to `authority`: `MomentAdmissionStage` admits a moment iff
the deterministic gates pass, `supported` is admitted, no judge flags `publishable`, and it is in
the meeting's top K by `best` (tie-break `usefulness`). `moments.mode` is left for the maintainer to
set to `auto` separately, as the kill switch.

### PR10 — Retire the R6 judge and human calibration (after PR9)

- Remove lane `r6-judge`, `MomentJudgeStage`, `citypods/moment_judging.py`'s panel and its
  telemetry purpose; remove the human-calibrated policy path in `citypods/moment_evaluation.py`
  (30-day warm-up, 30 reviews, 90% precision, learned score threshold) and its use in
  `MomentAdmissionStage`; keep `quote_safety_gate` and `technical_video_gate`.
- Remove `.github/workflows/r6-moment-review.yml` and the `moments.evaluation` (`minimum_days`,
  `minimum_reviews`, `required_precision`) and `moments.judges` blocks in `config/site_config.yml`.
- **Cancel queued work** for purpose `r6-judge` through `/v2/jobs:cancel-batch` (the existing
  client in `citypods/compute/llm.py`), with a dry-run count first, **before** the lane or any of its
  routes is removed, so the Slice 3a rescue cannot first turn those jobs into structural failures
  that the sweep keeps re-evaluating. Then archive and delete that purpose's deferred handles and
  `deferred_failure` markers (`structural_blocked` included) so the sweep stops reading them.
- **Archive** the R6 human reviews, policies and `judge_observations` to
  `state/archive/r6-moment-evaluation-<date>.json`; the live state keeps only what admission still
  reads (nothing).
- Release the `r6-judge` reservation; regenerate `ingress_reservations.json`.

### PR11 — Retire the R5 pre-labelers, packet, benchmark and tournament (after PR8)

- Remove lanes `topic-tags:prelabeler`, `topic-tags:prelabeler-shadow`, `r5-benchmark:tag`,
  `r5-benchmark:judge`, `tournament:tag`, `tournament:tag-judge`; the pre-labeler overlay and the
  12/90 matrix in `citypods/llm_evaluation.py`; workflows `llm-tag-review.yml`, `r5-benchmark.yml`,
  `llm-tournament.yml`, `tournament-tag-backfill.yml`, `tournament-ticket-approve.yml`,
  `tournament-route-merged.yml`. The R5 benchmark's frozen sample moves to `evals/tag-judge/` as
  read-only history.
- Cancel queued work for those purposes first (dry-run count), then archive and delete their
  deferred handles and failure markers, as in PR10. Remove Slice 3a's pre-labeler rebatch path in
  `citypods/tags.py` (recovery from retained pre-labeler subjects) with the pre-labeler itself; the
  tagger's own recovery stays. Archive R5 reviews and pre-labeler observations to `state/archive/`;
  release reservations; regenerate.
- The tagger lane (`topic-tags:tagger`) stays: it produces candidates; the stack decides them.

### PR12 — League-governed lane repair (extends review/48 §8.6; approved 2026-10-08)

review/48 Slice 3b (#2193) repairs a lane when a retired route takes a model's last eligible
route: additional models are dropped; a removed primary is replaced by the first `backup_models`
entry; with no backup the removal is held for human review. For judge lanes that rule picks by list
position, can break independence (a Google model into the non-Google slot), gives the newcomer full
authority at once, and keeps a dead route that fails every job sent to it.

- **Lane field `governance`**: `standard` (default; review/48's rule, byte-identical) or `league`
  (`judge:sibling`, `judge:adjudicator` now; P6 producer leagues later). Compile-validated.
- **Removal evidence is unchanged.** review/48's proof of retirement, account checks and
  surviving-pool rule decide whether a model leaves the lane at all.
- **Replacement from the bench.** `retire.py` gains `_repair_league_lane`: a vacated active slot is
  filled from `eligible_models`, choosing the highest-standing candidate that passes the lane's
  invariants: the slot's family rule (slot B non-Google; adjudicators distinct from each other and
  from both sibling families), not protected, at least one free unpaused route, a ceiling that fits
  the lane's packet size. Standing is the league score's lower bound; before P6, the maintainer's
  bench order checked against PR7's calibration and stability numbers.
- **Trial before authority.** A promoted model judges everything, but its votes do not count until
  it has a minimum record (probes plus adjudicated agreement whose lower bound clears the lowest
  incumbent's point estimate). Meanwhile entries that needed its vote are contested and go to the
  adjudicator. Nothing is admitted on JEV alone.
- **No eligible candidate**: the removal still proceeds; the slot fails safe (affected entries are
  contested, bounded by the adjudicator lane's cap, the rest wait) and a `priority:high` ticket is
  filed.
- **Exceptions that keep review/48's hold:** the anchor lane (every entry depends on JEV, and the
  adjudicator cannot absorb the whole load; if JEV is truly gone, judging fails closed and a
  `priority:high` ticket explains why tags stop updating), and any producer league that would be
  left with no active model.
- **Bench refill.** When review/48's reconciler proves and adds a new free model, it is also
  proposed as `eligible`, never `active`, for every league lane whose static invariants it passes;
  it starts with no record, as a P6 challenger.
- **Governance unchanged**: the same managed writer and branch, `needs:human-verification` on any
  active-model change, the standing and invariants that decided the pick in the PR body; nothing
  auto-merges.
- **Queued work**: packets pinned to the removed judge fail as typed structural and take PR3's
  rebatch branch, so they are re-judged by the promoted model.
- Tests: a Google model is never promoted into slot B; adjudicator family constraints hold; trial
  votes do not count; an empty slot fails safe and files a ticket; the anchor lane holds; a
  `standard` lane's repair is byte-identical to review/48's (regression against the #2193 fixtures).

## Rollback

Each task has its own switch. Before graduation: `judging.enabled: false` stops judging; stored
judgments are inert. After graduation: set the task back to `shadow`, which restores the previous
gate only until PR10/PR11 remove it; after those, rollback is the stack's own thresholds plus
`moments.mode: manual`. For that reason PR10 and PR11 land only after at least two weeks of stable
authority, measured by the audit and the reliability tripwires.

## Tests (beyond each PR's own)

1. Registry rules; both tasks register; `subject_id` stable across pre-labeler and display changes.
2. Evidence caps per tier; None without chapter or transcript.
3. Families: every active and eligible model has one; sibling and adjudicator choice per subject.
4. Packing ceilings, no split or truncation, oversize skipped and counted, one packet per meeting
   for moments.
5. Backends against fixtures: JEV `noul`/`score`/`choice`; chat contracts, including malformed and
   partial replies.
6. Ledger append-only and dedup; no row on failure.
7. Shadow mode never changes `display`, `admission` or publication (whole-record comparison minus
   `judgments`).
8. Authority mode: tag projection and moment admission follow the thresholds; deterministic moment
   gates still apply; `moments.mode: manual` still blocks publication.
9. Calibration: on a synthetic adjudicated set with known optimum, the proposal recovers it; the
   false-accept constraint is respected.
10. Retirement PRs: no reference to a removed lane remains (compile fails otherwise); cancellation
    dry-run reports counts; archives are written before deletion.

## Open questions

None blocking. The capacity numbers are projections and are re-derived from PR4 data.
