# review/53 — P1: judgment ledger and shadow judging (JEV + sibling)

**Maturity: L3 (development-ready) · authored 2026-10-08 · build spec for review/49 phase P1.**

Parent design: [review/49](49-judge-consensus-admission.md) (sections 2, 3a, 4b, 4c, 7). P0 is
[review/50](50-p0-llm-verb-backlog-trend.md). This document is the whole P1 contract: an agent
implementing it should not need to infer anything from review/49. If something here is ambiguous or
a live result contradicts it, stop and ask (AGENTS.md "Implementing from a breakout doc").

## Goal

Produce real judge data, at production volume, **without changing anything a resident sees**:

1. Every tag candidate (rule and LLM) gets typed **judgments** from the anchor judge (JEV) and one
   sibling judge, recorded append-only on the candidate.
2. The judging framework is **generic**: tags are the first registered task; moments and future
   verbs plug in by registering a task spec, with no change to judges, packing, transport or ledger.
3. Each judgment records its **context tier**, so the T1-then-T2 rule and the continuous all-tier
   sample (review/49 section 4b) are measured from day one.

P2 (thresholds, probes, stability scoring, adjudication) needs exactly this data and nothing else.

## Non-goals (P1 must not do these)

- No display, admission or suppression change. `display`, `admission` and every existing gate stay
  exactly as they are. Judgments are read by nothing in the publishing path.
- No consensus decisions, thresholds, adjudicator calls, probes, audit or leagues (P2 to P6).
- No moments judging in production: the moments task spec is registered and tested, not scheduled
  (P4). `MomentJudgeStage` and `r6-judge` are untouched.
- No retirement of the prelabeler, tournament or benchmark lanes (P7).
- No paid route, no BeatAPI top-up.

## Decisions this spec relies on (all recorded in review/49)

| Decision | Source |
|---|---|
| JEV is the anchor judge; Gemma 4 31B/26B is the bulk sibling; a non-Google sibling (Nemotron 3 Super 120B) judges Google-produced entries; GLM 5.3 Flash and DeepSeek V4.1 Flash are adjudicators (P2) | review/49 section 4c, approved 2026-10-08 |
| Independence is per entry: a judge or adjudicator never shares a family with the entry's producer | section 4c |
| Judge at T1; re-judge at T2 when JEV p is in [0.3, 0.7]; a 5% stratified sample is judged at every tier | section 4b, approved 2026-09-30 |
| The tag rubric (specific project, contract, program or policy; not a passing mention) is the `validate` question's instruction | section 4b, maintainer policy 2026-09-30 |
| BeatAPI's free tier is one single-flight window for the whole account, shared by JEV and the chat routes; chat routes are `tier: backup` and must yield to JEV | section 7, section 4c |
| BeatAPI calls go through the `custom-beatapi` AI Gateway provider | PR #2181 |

## Measured facts used below

- JEV: `POST /v1/systemone`, body `{model, state, questions}`; question types `noul`
  (probability), `score` (needs a non-empty `criteria` array), `choice` (named options). Limits:
  about 64k tokens total and about 32k for state plus the largest question; plan for **58,000
  total and 28,000 state-plus-largest** estimated tokens. Oversize answers `503
  {"code":"processing_failed","retryable":true}`. Latency 0.7 to 2.2 s. Response
  `{answers: {<question id>: {...}}, usage: {input_tokens, output_tokens}}`. Captured real
  responses are in `evals/judge/results/2026-09-30-*-jev*.json`.
- BeatAPI account: one successful request per minute across all free models, JEV included; a call
  in flight blocks every other model; 429 `rate_limit_exceeded` with `Retry-After: 60`.
- Gemma 4 31B/26B on AI Studio: four pools (two models x two projects) of 14,400 RPD, 30 RPM,
  16,000 TPM, `hard_input_ceiling` 14,400. Usable packet about 10,000 estimated input tokens.
- Context tiers (lane `context-ladder` results): T0 about 241 tokens/item, T1 about 506, T2 about
  2,140.
- Tag candidates today: 127,719 rule candidates (99%) and 934 legacy LLM candidates, about 8.3 per
  candidate-bearing episode.
- Ingress: `global_write_budget` 25,600 units/day, 13,186 reserved, so about 12,400 units of shared
  headroom. A pinned (`per_model`) job costs 4 units.

## Design

### 1. Task-spec registry (`citypods/judging/tasks.py`, new)

```python
QuestionKind = Literal["validate", "gate", "grade", "choose"]
ContextTier = Literal["T0", "T1", "T2", "T3"]

@dataclass(frozen=True)
class QuestionSpec:
    id: str                       # stable within the task, e.g. "supported"
    kind: QuestionKind
    instruction: str              # rubric text shown to every judge
    levels: tuple[str, ...] = ()  # grade only, lowest first
    prompt_version: str = "1"     # bump to start a new calibration cell

@dataclass(frozen=True)
class Subject:
    task: str
    subject_id: str               # judgment identity, see section 3
    episode_uid: str
    group: str | None             # e.g. meeting uid for choose; None for tags
    producer_model: str | None    # None for rule candidates
    payload: Mapping[str, Any]    # the candidate as stored

@dataclass(frozen=True)
class Evidence:
    tier: ContextTier
    text: str
    digest: str                   # sha256 of text + tier + builder version

@dataclass(frozen=True)
class TaskSpec:
    name: str
    questions: tuple[QuestionSpec, ...]
    subjects: Callable[[Episode], Iterable[Subject]]
    evidence: Callable[[Subject, ContextTier, EpisodeTexts], Evidence | None]
    first_tier: ContextTier
    escalation: tuple[ContextTier, float, float] | None   # ("T2", 0.3, 0.7)
    consensus_policy: str          # named, implemented in P3; recorded only in P1
    selection_policy: Literal["all_admitted", "top_k_per_group"]

def register(spec: TaskSpec) -> None: ...
def task(name: str) -> TaskSpec: ...
def registered() -> tuple[str, ...]: ...
```

Registration rejects duplicate names, duplicate question ids, a `grade` with no levels and a
`choose` in an `all_admitted` task. `evidence` returns None when the tier cannot be built (no
chapter, no transcript); that subject is skipped at that tier, never judged on empty evidence.

**`citypods/judging/tag_task.py`** registers `tag` (scheduled in P1):
- subjects: every tag candidate on the episode, episode- and chapter-scope: rule candidates from
  `Episode.tags` (`source_kind: "rule"`) and LLM candidates from `Episode.llm_tag_candidates`
  (`citypods/models.py`). The `judgments` list is appended to the same dict in whichever list holds
  it.
- one question `supported` (`validate`), instruction = the section 4b rubric verbatim, plus the tag
  definition from `config/taxonomy.yml`.
- evidence: T0 = the matched span(s); T1 = transcript window of plus or minus 45 s around the first
  evidence `t`, capped at 400 words, plus chapter or agenda title; T2 = the whole chapter capped at
  1,800 words (episode-scope candidates without a chapter: the 1,800 words around the first
  evidence `t`). Tag definition and title are always included.
- `first_tier="T1"`, `escalation=("T2", 0.3, 0.7)`, `selection_policy="all_admitted"`.

**`citypods/judging/moment_task.py`** registers `moment` (not scheduled until P4): questions
`supported` (validate), `publishable` (gate: PII, mocking, procedural), `usefulness` (grade, the
existing `PULL_QUOTE_CRITERIA`), `best` (choose among the meeting's survivors); `group` = meeting
uid; `selection_policy="top_k_per_group"`. It exists in P1 to prove the registry and backends handle
all four kinds; tests exercise it end to end against fixtures.

### 2. Families and independence (`config/site_config.yml`, `citypods/judging/families.py`)

New top-level `llm_families:` map, model to family, for every model in any producer or judge lane
(`google` for Gemini and Gemma; `deepseek`; `zai`; `moonshot`; `stepfun`; `nvidia` for Nemotron;
`qwen`; `tencent`; `typesafe` for JEV; `unverified-beatapi-gpt` for the BeatAPI GPT ids, whose
serving family is unknown). `family_of(model)` raises for an unknown model, and a test fails if any
lane model lacks a family. Rule candidates have producer family `rule`, which collides with nothing.

Sibling choice per subject: the first sibling slot whose family differs from the subject's producer
family. Slot A (Gemma) unless the producer is `google`, then slot B (Nemotron 3 Super). JEV is never
excluded. A subject with no eligible sibling (not possible with this config) is recorded as
`no_independent_sibling` in the run telemetry and judged by JEV only.

### 3. Judgment record and storage

Stored **on the candidate**, in a new append-only list `judgments`, next to the existing fields,
following the precedent of R6's `judge_assessments`. No new state file, no global ledger: the
episode record is already the unit that is persisted, synced and backed up.

```python
@dataclass(frozen=True)
class Judgment:
    schema: Literal[1]
    task: str
    subject_id: str
    question_id: str
    kind: QuestionKind
    judge_model: str              # logical model, e.g. "typesafe/jev-1.13"
    judge_route: str              # physical route id that answered
    judge_role: Literal["anchor", "sibling"]
    prompt_version: str
    context_tier: ContextTier
    evidence_digest: str
    value: float | int | str | bool   # validate/gate: probability (JEV) or bool (sibling)
    probabilities: Mapping[str, float] | None
    confidence: float | None
    reason: str | None            # sibling only, <= 300 chars; quoted evidence
    packet_id: str                # all judgments from one call share it
    sample: Literal["routine", "escalation", "all_tiers"]
    judged_at: str                # ISO 8601 UTC
```

- **Subject identity.** `subject_id` must not change when an unrelated field changes. The existing
  `candidate_id` includes the prelabeler's decision, reason and input digest, so it changes when the
  prelabeler runs. `subject_id` = `"subj-" + sha256(json(task, episode_uid, chapter_id, tag id,
  source_kind, provider_model, rule_version, sorted evidence (where, span, t)))[:24]`, computed by
  `citypods/judging/subjects.py:tag_subject_id`.
- **Dedup key:** `(subject_id, question_id, judge_model, prompt_version, context_tier,
  evidence_digest)`. A judgment is appended only if its key is absent. A record is never modified or
  deleted; a re-judged subject (new prompt version, new evidence) gets a new row.
- **Size:** about 350 bytes per row, two to three rows per candidate, about 8 candidates per
  episode: about 8 KB per episode. Acceptable for the episode record.
- Failed, malformed, deferred or oversize calls produce **no judgment row** (review/49 invariant:
  never admit, never suppress). They are counted in run telemetry by reason.

### 4. Backends (`citypods/judging/backends.py`)

One interface, two implementations:

```python
class JudgeBackend(Protocol):
    role: Literal["anchor", "sibling"]
    def packet_ceiling(self) -> PacketCeiling: ...   # token limits, max questions
    def build(self, packet: Packet) -> InferenceJob: ...
    def parse(self, packet: Packet, result: JobResult) -> list[Judgment]: ...
```

- **`JevBackend`.** `validate` and `gate` compile to `noul` (`criteria: {}`), `grade` to `score`
  with `criteria` = levels lowest first, `choose` to `choice` with one named option per subject.
  Evidence goes **in the question text** for non-overlapping windows (tags), and in the shared
  `state` when several questions share it (moments: the meeting window). Question ids are
  `<subject_id>:<question_id>`. Parse maps `answers[id].noul` to `value`, `score` plus
  `probabilities` plus `confidence`, `choice` plus `probabilities`. A missing answer id produces no
  row for that question and an `answer_missing` count. The job carries the payload as
  `{"systemone": {"state": ..., "questions": ...}}` and no `messages`.
- **`ChatJudgeBackend`** (siblings). One structured contract per kind, registered through
  `citypods/compute/structured.py`: `judge-validate-v1` =
  `{answers: [{id, supported: bool, reason: str<=300}]}`, plus `judge-gate-v1`, `judge-grade-v1`
  and `judge-choose-v1` (moments, tested only). The system prompt is the question instruction; each
  item is `id`, evidence and the claim. Parse validates with the Pydantic contract and maps
  `supported` to `value` (bool), `reason` to `reason`. Items absent from the reply produce no row.

### 5. Packing (`citypods/judging/packing.py`)

`pack(subjects_with_evidence, ceiling) -> list[Packet]`, greedy and deterministic:
- Order: episode (recent-first, see section 7), then subject id. Fill a packet with whole items
  until the next item would exceed any ceiling; never split an item, never truncate evidence.
- An item that alone exceeds the ceiling is skipped and counted as `payload-too-large` (the
  existing blocked outcome in `citypods/stages.py`); it is never truncated.
- JEV ceiling: 58,000 total estimated tokens, 28,000 for state plus the largest question, at most
  60 questions (above that, quality is unmeasured).
- Gemma ceiling: 10,000 estimated input tokens, at most 25 items, output reservation 2,048.
- Nemotron 3 Super ceiling: 24,000 estimated input tokens, at most 25 items.
- Token estimate: the repo's `chars / 4` builder estimate; the Worker's route `input_token_ratio`
  still applies at admission.

### 6. Worker: a second transport, and yield-to-JEV (`workers/llm-dispatch-v2/src`)

**Route field `transport`**, `"chat"` (default) or `"systemone"`, validated in
`scripts/compile_llm_limits.py` and carried in `_WORKER_ROUTE_FIELDS`. Route field `request_path`
(optional) overrides the provider's `chat_path` and `ai_gateway_chat_path` for that route. All
transport-specific behaviour moves into a new `transports.js`, keyed by transport name, never by
provider:

| Function | `chat` (unchanged behaviour, moved) | `systemone` |
|---|---|---|
| `buildRequest(payload, route, opts)` | today's `upstreamRequestForRoute` | `{model: route.upstream_model, state, questions}` from `payload.systemone`; no `max_tokens`, no `response_format` |
| `emptyCompletion(status, body)` | today's `upstreamEmptyCompletion` | 2xx without an `answers` object, or with `error` |
| `structuredInvalid(payload, body)` | today's `structured_output.js` check | answers missing for more than 10% of question ids (the remainder parse in Python) |
| `observedTokens(body)` | `usage.prompt_tokens` / `completion_tokens` | `usage.input_tokens` / `output_tokens` |
| `lengthTruncated(body)` | `finish_reason === "length"` | always false |

`gateway.js`, `index.js` and `structured_output.js` call these through `transportFor(route)`. A test
asserts the chat transport's outputs are byte-identical to today's for the existing fixtures.

**Oversize classification.** In `classify.js`, for a `systemone` route, `503` with
`code: "processing_failed"` is `request_defect` (rule id `systemone-oversize-503`) when the job's
input estimate is at least 85% of the route's `hard_input_ceiling` (58,000), otherwise
`upstream_capacity`. This stops the 5xx retry budget from resending a request that cannot fit.

**`yields_to`** (route field, list of route ids, compile-validated to exist). In the claim's route
ordering (`coordinator.js`, where `tier: backup` sorts), a route with `yields_to` is skipped when any
named route has queued work: `SELECT 1 FROM job_models WHERE model = ? LIMIT 1` with the named
route's logical model (`typesafe/jev-1.13`; `job_models` is the queued-job index keyed by model),
cached for the claim. This is a read, not a billed row write. Set on the five BeatAPI chat
routes: `yields_to: [beatapi_jev_1_13_free]`.

### 7. Configuration

`config/provider_limits.yml`, new route (BeatAPI provider already has `rpm: 1`, `concurrency: 1`,
`ai_gateway_slug: custom-beatapi`):

```yaml
  - route_id: beatapi_jev_1_13_free
    model: beatapi/jev-1.13
    model_key: typesafe/jev-1.13
    provider: beatapi
    upstream_model: jev-1.13-free
    transport: systemone
    request_path: /systemone
    input_context_limit: 64000
    output_context_limit: 4096       # reservation only; JEV output is small and not capped
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

and `yields_to: [beatapi_jev_1_13_free]` on the five BeatAPI chat routes.

`config/site_config.yml`, two lanes (purpose names are what the Worker reserves against):

| Lane | Models | Shape | `reserved_write_units` | `daily_write_units` | Jobs/day cap | Telemetry |
|---|---|---|---|---|---|---|
| `judge:anchor` | `typesafe/jev-1.13` | `per_model` | 0 | 1,200 | 300 | `{producer: judge, unit: episode, completion: consumed, scope: retained_catalog}` |
| `judge:sibling` | `google/gemma-4-31b-it`, `google/gemma-4-26b-a4b-it`, `openrouter/nvidia/nemotron-3-super-120b-a12b:free` | `per_model` | 0 | 2,400 | 600 | same |

Zero reservation: P1 shadow draws only on shared headroom (3,600 of about 12,400 units) and can
never displace a production lane, the same rule as `topic-tags:prelabeler-shadow`. The caps are
P1's, chosen to finish the backfill in about one to two weeks; P2 re-sizes them from the P0 backlog
trend.

Plus `llm_families:` (section 2) and a `judging:` block:

```yaml
judging:
  enabled: false           # flipped by the P1 acceptance PR
  tasks: [tag]
  all_tiers_sample_rate: 0.05
  backfill_order: recent_first
```

### 8. Stage, lane and workflow

- **`JudgeShadowStage`** (`citypods/stages.py`, an `LLMProducerStage`, name `judge`), registered in
  `LANE_STAGES["judge"] = frozenset({"judge"})`. It must not appear in any other lane, and it runs
  after the stages whose candidates it reads (it reads persisted records only, so in its own lane it
  needs no in-run ordering). It is not audio-affecting.
- `work_items`: per episode, per registered and enabled task, yield `ready` when any subject lacks a
  judgment for its next due `(question, judge, tier)`, otherwise `reused`. Telemetry purposes
  `judge:anchor` and `judge:sibling`.
- `process`: for each episode, recent-first by `published`, build subjects and evidence for the due
  tiers; route each subject to JEV and to its independent sibling; pack per backend; submit with
  the purpose's `LLMRequestPolicy` (pinned model); on completion parse and append judgments; then
  schedule escalation (JEV `value` in [0.3, 0.7] at the first tier) and the all-tier sample (a
  deterministic 5% of subjects by `subject_id` hash, judged at T0, T1 and T2 by both judges).
  Persist through the existing episode-record write path, after the run's other writes, under the
  normal wall-clock `stop()` budget (deferred is not failed).
- **Workflow** `.github/workflows/judge.yml`: `python -m citypods.cli enrich --lane judge`, every
  two hours, concurrency group `judge`, the same secrets and state sync as `tag.yml`. Manual
  dispatch has a `dry_run` input that builds packets and writes the packet report without
  submitting.

### 9. Telemetry and report

Per run, through the existing purpose snapshots (review/52): `ready`, `queued`, `consumed`,
`ingress_limited`, plus judging-specific counts in the run event: `packets_by_backend`,
`items_per_packet` (p50, p90), `payload_too_large`, `answer_missing`, `parse_failed`,
`no_independent_sibling`, `escalations`, `all_tier_samples`, `judgments_appended`.
`python -m citypods.judging.report` prints, from stored judgments only: judgments per day by judge
and tier, JEV-versus-sibling agreement at p = 0.5 by tier (the P2 input), escalation rate, and
backfill progress (share of candidates in the last 90 days with both judges at the first tier).

## Files

| File | Change |
|---|---|
| `citypods/judging/__init__.py`, `tasks.py`, `tag_task.py`, `moment_task.py`, `families.py`, `subjects.py`, `backends.py`, `packing.py`, `ledger.py`, `report.py` | new |
| `citypods/stages.py` | `JudgeShadowStage`; `LANE_STAGES["judge"]` |
| `citypods/compute/structured.py` | register the four `judge-*` contracts (through `judging/backends.py`) |
| `config/site_config.yml` | lanes `judge:anchor`, `judge:sibling`; `llm_families`; `judging` |
| `config/provider_limits.yml` | JEV route; `yields_to` on BeatAPI chat routes |
| `scripts/compile_llm_limits.py` | validate and carry `transport`, `request_path`, `yields_to` |
| `scripts/compile_llm_lanes.py` | nothing new beyond the lanes (existing compile) |
| `workers/llm-dispatch-v2/src/transports.js` | new |
| `workers/llm-dispatch-v2/src/gateway.js`, `index.js`, `structured_output.js`, `classify.js`, `coordinator.js` | call through `transportFor`; oversize rule; `yields_to` skip |
| `.github/workflows/judge.yml` | new |
| `tests/fixtures/jev/*.json` | real JEV responses copied from `evals/judge/results` (one `noul` packet, one `score`, one `choice`), plus the observed error bodies: `503 processing_failed`, `429 rate_limit_exceeded`, `401 invalid_api_key` |
| `ARCHITECTURE.md`, `CHANGELOG.md`, `review/11`, `review/49` | doc-update contract |

## Tests

Python (`tests/test_judging_*.py`):
1. Registry: rejects duplicates, a `grade` without levels, a `choose` in an `all_admitted` task; both
   `tag` and `moment` register.
2. `tag_subject_id` is unchanged when prelabeler fields, `display` or `admission` change, and
   changes when the evidence span or tag id changes.
3. Evidence builders: T0, T1 and T2 caps, the plus or minus 45 s window, None without a chapter or
   transcript; digest changes with text and tier.
4. Families: every model in every lane has a family; sibling choice switches to slot B for a
   `google` producer; rule candidates use slot A.
5. Packing: never splits or truncates; respects each ceiling and the JEV state-plus-largest limit;
   an oversize single item is skipped and counted; deterministic order.
6. `JevBackend` build and parse against the fixtures for all three JEV types, including a missing
   answer id; `ChatJudgeBackend` against a recorded Gemma reply and a malformed one.
7. Ledger: dedup key, append-only (a second identical append is a no-op; nothing is ever modified),
   no row for failed, malformed or deferred calls.
8. Stage: with `judging.enabled: false` it does nothing; with fixtures it appends JEV and sibling
   judgments, escalates exactly the [0.3, 0.7] band, samples 5% deterministically, never changes
   `display`, `admission` or any existing field (a whole-record before/after comparison minus the
   new `judgments` lists), and leaves `MomentJudgeStage` output untouched.
9. Moments task end to end against fixtures (validate, gate, grade, choose), not scheduled.
10. Config: the lanes compile; reservations stay zero; the JEV route compiles with `transport` and
    `request_path`; `yields_to` targets exist.

Worker (`workers/llm-dispatch-v2/test`):
11. `chat` transport: byte-identical requests and identical classifications for the existing
    fixtures (a regression guard for the move into `transports.js`).
12. `systemone`: request shape, usage mapping, empty-answers handling, the URL through
    `custom-beatapi` plus `/systemone`.
13. Oversize 503: `request_defect` at 85% or more of the ceiling, `upstream_capacity` below.
14. `yields_to`: a BeatAPI chat route is skipped while a JEV job is queued and eligible otherwise
    (mirror of the backup-tier test).
15. Each new Worker test is shown to fail with its feature removed.

## Sequencing

```text
PR1 Worker: transports.js (chat moved, systemone added), request_path, oversize rule
      |                        (no config uses systemone yet; chat behaviour byte-identical)
PR2 Config: JEV route + yields_to + lanes (judging.enabled: false) + llm_families
      |                        (Worker deploys; nothing enqueues JEV yet)
PR3 Python: citypods/judging/* + JudgeShadowStage + judge.yml (dry_run only on schedule)
      |
PR4 Acceptance: one manual dry run, then judging.enabled: true; observe 3 days; record results
```

PR1 and PR3 can be developed in parallel; PR2 needs PR1 deployed; PR4 needs all three.

## Migration and backfill

Nothing is migrated. The new `judgments` lists start empty. Backfill is the normal recent-first
traversal under the lane caps: about 128,000 existing candidates at T1 is about 1,200 JEV packets
(at about 110 items each) and about 5,100 Gemma packets (at about 25), so about 4 days for JEV and
about 9 days for the sibling at P1's caps, plus escalations. **Rollback:** set
`judging.enabled: false` (the stage stops; recorded judgments stay, harmless and unread); the JEV
route can be paused with `rpd: 0`. The Worker transport change is backward-compatible by test 11.

## Acceptance (PR4, recorded in this document)

1. A manual `dry_run` reports packet counts and item sizes within every ceiling, with zero
   `payload_too_large` among candidates that have a chapter.
2. Three consecutive days with `judging.enabled: true`:
   - judgments appended for both judges every day, at least 90% of the lane caps used once backfill
     is under way;
   - zero changes to `display` or `admission` on any episode (checked by the before/after
     comparison on a sampled set of persisted records);
   - no JEV job failed as `request_defect` except genuine oversize; BeatAPI chat routes admitted
     only while no JEV job was queued (Worker telemetry);
   - DO billed rows stay under the enqueue stop on every day.
3. `citypods.judging.report` shows JEV-versus-sibling agreement by tier, the escalation rate and
   backfill progress. These numbers are the P2 inputs and are copied into review/49.

## Open questions for the maintainer (none block PR1 to PR3)

1. **Caps.** P1 uses 300 JEV and 600 sibling jobs a day (3,600 of about 12,400 shared units). Larger
   finishes the backfill faster; it does not change correctness.
2. **Workflow cadence.** Every two hours keeps JEV's single-flight window lightly loaded (about 25
   packets per run at the cap). Hourly would spread the same caps more evenly.
3. **Nemotron 3 Super as sibling slot B** has no judging record here. If you prefer Qwen3.8 for slot
   B (measured 30/30 on the easy set, but capped at about 200k tokens a day shared with
   `r6-judge`), swap the model in `judge:sibling`; nothing else changes.
