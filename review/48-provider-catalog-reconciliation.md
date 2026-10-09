# review/48 — Provider catalog reconciliation

**Maturity: Slice 1 and PR C shipped · R10 verification shipped (#2169) · Slice 2a shipped (#2182) · shadow shutdown shipped (#2188) · Slice 2b paid decisions shipped (#2189) · Slice 3a shipped (#2191); removal activation retains gates**

Owner: LLM dispatch maintainers. Code: `citypods/provider_catalog/`,
`scripts/reconcile_provider_routes.py`, `.github/workflows/provider-catalog-reconcile.yml`,
`config/provider_catalog_decisions.yml`. Prerequisite shipped: the v2 dispatch pause (#1848).

This supersedes the first design in PR #1841. Its free-evidence rules, Artificial Analysis matching
and comment-preserving route edits were kept; its always-open issue, digest-branch PRs, unreachable
auto-added routes, HTML scraper and hand-kept alias tables were not.

## Implementation checkpoint — 2026-10-08

PR C shipped in [#1854](https://github.com/BashfulBits/city-meeting-podcasts/pull/1854). The Worker
and Python direct path already shape requests per route and reject empty structured responses. The
remaining R10 gap was the catalog: its first-event availability ping could prove service without
proving JSON support. The maintainer approved completing this verification before Slice 2 on
2026-10-08. Catalog verification shipped in [#2169](https://github.com/BashfulBits/city-meeting-podcasts/pull/2169).
This checkpoint does not mark Slices 2–4 shipped; their remaining contracts are proposed in §8.

BeatAPI registration shipped in
[#2167](https://github.com/BashfulBits/city-meeting-podcasts/pull/2167). Its DeepSeek routes pool
with NVIDIA/OrcaRouter through `model_key`; the model's other routes must remain visible in anomaly
lane-impact reporting. BeatAPI's five free chat routes share a successful request/minute account
window with JEV. Its plugin excludes JEV's `owned_by: task plugin` catalog entry from chat
discovery, and spaces probes by 65 seconds. The first reasoning event is only availability evidence,
never structured-output evidence. Advertised GPT names do not verify the serving family; catalog
JSON support does not establish judge-family independence (review/49).

The catalog now follows a successful availability ping with four schema-bound streaming checks,
using `structured_output_methods` and the same Python renderer as direct calls. Each asks for
`{"answer":"ok"}`; strict vs relaxed checks exercise a string-length bound. Visible content must
parse to that exact object after `finish_reason: stop`. Reasoning-only, malformed, extra-field,
wrong-value and truncated replies do not qualify a candidate. A completed empty/invalid reply or
HTTP 400 rejection of the resolved configured method is a `structured_output_invalid` anomaly;
transport failures, timeouts and capacity/access errors remain inconclusive. Access/capacity errors
stop the remaining method checks. A working alternative is reported without editing route config.

Checks allow 4,096 total completion tokens, including reasoning, with a 720-second read timeout and
elapsed-time checks against the Worker response ceiling, and cap streamed event data at 64 KiB. The
old four-token ping remains a cheap availability check, but cannot establish method support. This
means up to five requests per checked model, rather than one. Every request uses plugin spacing,
with pause renewal after the wait and during long streams. Configured-route method checks are
charged individually to the Worker ledger; a failed charge aborts further probing instead of
continuing with an unrecorded spend. Scarce routes require reported quota; unknown or exhausted
quota defers the check. Partial checks and a 429 are deferred until reset. A check with no method evidence leaves its
previous record unchanged; partial results merge by method, retaining unchecked methods and
latency. The prior preferred method is retained when the new attempt verifies no method. Candidate checks have no
configured route ID to reserve and remain under the provider pause and spacing; their per-model
allowance is bounded by the three-candidate run budget. The provider pause stays armed for one final
plugin interval before resuming dispatch, so the last unregistered candidate cannot leave
BeatAPI/JEV inside a spent account window.

The issue shows a candidate's preferred method (first valid method in R10 order) and verification
date. The version-2 state marker retains per-method outcomes and first-event/completion latency. Its
base64 payload is compressed to keep that evidence within GitHub's issue-body limit; the decoder
still accepts older uncompressed JSON markers, and rendering reserves the measured marker size
before bounding human-readable observations. Version-1 candidate `proven` memories without a
verified method are rechecked; retirement memories remain valid. Daily merges discard legacy
candidate proofs without a method and clear recovered route anomalies using the set of routes
actually rechecked. Configured methods are resolved using the same route/model/provider fallback as
the compiler, with a contract test covering the actual configuration. `--evidence-report` uses the
same planner, so it cannot bypass BeatAPI spacing or scarce quota handling. No production route,
recipe, pipeline version or stored episode artifact changes; no catalog backfill or deployment is
required for the offline tests. Live verification remains a separate maintainer-run step.

Acceptance coverage: both shared shaping suites; reasoning-only and malformed/full-stream response
cases; all four request shapes; scarce quota accounting and partial deferral; BeatAPI's 65-second
spacing across health and method/candidate checks; JEV exclusion; and pooled-route lane rescue
reporting. Slices 2–4 remain the next implementation work after this verification change.

## 1. Why

LLM routes churned heavily in Aug–Sep 2026: providers retired models (NVIDIA's gpt-oss-120b and
deepseek-v4-pro went 410), plans changed (Airforce's kimi-k2.7-code became paid), new free
models appeared (NVIDIA's GLM-5.3), and limits drifted. Checking all of that by hand is not
sustainable. The goal is automation that *reduces* maintenance: it finds changes, proves them, and
asks a human only for real decisions.

## 2. Goals

| | Goal |
|---|---|
| G1 | Keep LLM config current with providers: routes, context limits and rate limits. |
| G2 | Automate discovery and retirement: read `/models`, prove each model with a canary, screen quality, check limits. |
| G3 | Never strand jobs when a route is removed; affected jobs are reassigned automatically. |
| G4 | Humans decide additions (issue checkboxes + `/apply` → curated PR); removals and proven conservative tightening are automatic PRs (material-change policy: §8.1). |
| G5 | Extensible: providers and LLM lanes are added or removed without touching the reconciler core. |
| G6 | Every structured request reaches its route in a form that route answers with JSON; a route that stops doing so fails loudly, never with silent empty replies. |

Out of scope: task-specific output scoring and judge-based admission (a later design).

## 3. Requirements

**R1 Complete catalogs.** Every provider catalog is read completely (Gemini pages at 50 by default)
with the provider's first account key.

**R2 Providers are plugins.** Each provider's knowledge lives in one
`citypods/provider_catalog/providers/<name>.py` exporting `RULES = ProviderRules(...)`:
catalog endpoint and auth style, free evidence, chat filter, response `Signal`s, the free-variant
suffix, the creator for bare model IDs, and research links. The registry discovers plugins
automatically; `_template.py` is the starting point. Contract tests fail CI when a configured
provider has no plugin, a plugin names a provider that is no longer configured, or the core
branches on a provider name.

**R3 Canaries are fair evidence.**
- One 4-token streaming completion; success is decided by *parsing* the first SSE event (a 200
  whose first event is an error is not a success; a normal chunk may carry `"error": null`).
- Timeout matches the Worker's own 720 s response ceiling. A timeout or transport error is always
  `inconclusive`; so is anything no provider signal recognizes.
- Each provider's probes run inside a provider-scoped v2 dispatch pause, after its in-flight work
  drains, with the pause renewed per probe. Without the Worker, probes still run and are reported
  as possibly contended.
- **Scarce daily quotas** (configured `rpd` ≤ 50, e.g. Gemini's 20/day) are checked every 4 weeks,
  only when the Worker reports quota left (`pause-status.rpd_remaining`), and each probe is charged
  to the Worker's ledger (`/v2/dispatch:reserve`). With no quota left the check is deferred to the
  reset; a daily `--due-only` run picks it up. A spent quota is a deferral, never a verdict.

**R4 Quality is informational.** The Artificial Analysis Intelligence Index is fetched once per
run and shown next to each lane's incumbents. The floor (lower of GPT-OSS-120B and Nemotron-3
Super) is a flag, not a gate. Matching is exact identity plus generic naming rules and a
publisher-alias table — no per-model aliases: AA may prefix the creator; `-it`/`-instruct`/`-chat`/
`-preview`/`-reasoning` are optional on either side; a bare ID from a multi-publisher host matches
only a slug exactly one creator has. An exact slug always wins, and a normalized match is used only
when it is unique, so ambiguity stays unscored. Unscored models get research links.

**R5 Low noise.** One rolling issue lists only actionable items: proven candidates and
unacknowledged anomalies on configured routes. Each anomaly shows the lanes that use the route and
what else still serves each of them (or that the lane would stall). A free route that became paid
(`not_entitled`) gets a **remove route / keep as a paid route** checkbox, never an automatic PR. Everything else is in a collapsed Observations
block. The issue closes when nothing is actionable and reopens (same issue) when something is.

**R6 Bounded, useful probing.** At most 3 candidate canaries per provider per run: never-tried
first, then retries of inconclusive ones, best-scored first within each. Only definitive verdicts
are remembered (28 days); an inconclusive or spent-quota result is retried, so a provider's rate
limit cannot hide a model for a month. A plugin can space its canaries (Airforce: 1.5 s, for its
global 1 req/s limit). Skipped before any canary: aliases of a configured model (same name without
free/variant decorations) and models whose listed context is below the smallest configured free
route's (a data-driven floor, not a constant).

**R7 Decisions are durable.** `config/provider_catalog_decisions.yml` holds `ignored` candidates
(optionally until `revisit_after`) and `acknowledged` known states of configured routes (e.g. the
Mistral account-tier block), so a decision is made once.

**R8 Lanes come from the registry.** Lane placement reads `load_lanes()` only. Eligible lanes
are those whose `accepts_catalog_backups` is true (pooled, and `catalog_backup_candidates` not set
false). A lane checkbox is
offered when the candidate scores at least the lane's *weakest* scored member (backups add capacity
and are usually weaker than the primary); every other eligible lane is listed with its score gap,
because capacity backups are sometimes chosen below that bar. Promotion to a lane's primary model
stays manual (it changes recipe hashes).

**R9 Observe before acting.** Slice 1 changes no config; its only side effect is the issue.

**R10 Structured output is shaped per route, from verified configuration (G6).** Producers
(GitHub Actions, local runs, evals) send only the response *schema*; they never choose how to ask
for JSON. The v2 Worker shapes the request for the route it actually dispatches to, because only it
knows that route (a pooled job may be served by any route in the pool). A small, closed set of
methods covers every route:

| Method | Request shape |
|---|---|
| `json_schema` | `response_format: json_schema` with the full schema |
| `json_schema_relaxed` | `response_format: json_schema`, schema with size/length/range bounds stripped |
| `json_object` | `response_format: json_object`, schema appended to the prompt |
| `prompt_only` | no `response_format`; schema appended to the prompt |

- **Resolution:** a route's own *verified* method is authoritative (with its `verified_on` date).
  Support is a property of the provider *serving* a model, not of either alone, so the
  verification lives on the route. An unverified route (between being added and its first canary)
  falls back to the method verified for the same model on another route, then the provider's
  default, then `prompt_only` (the only method every chat endpoint accepts).
- **Verification:** the catalog canary (Slice 1) sends a small schema-bound request with each
  method to each configured route and to each candidate, and records which methods return valid
  JSON. It proposes the method for a new route (Slice 2 writes it), and a configured method that
  stops working is an anomaly on the rolling issue. Methods that work are preferred in table order
  (strictest first).
- **Loud failure:** the Worker treats a 200 with empty or unparseable content on a structured
  request as a route failure (`structured_output_empty`/`structured_output_invalid` in
  `route_failures`) and retries the job on another route; it never completes a job with it. Full
  schema validation stays in Python (LLM output is untrusted; the Pydantic models live there).
- **One definition:** local direct calls (tests, evals, research) use the same shaping. A shared
  fixture of `method + schema -> expected request` is asserted by both the Python and the Worker
  test suites, so the two implementations cannot drift.

## 4. What a response means (evidence, 2026-09-24)

Recorded under provider-scoped pauses and pinned in
`tests/fixtures/provider_catalog/evidence_2026_09_24.json` and
`tests/test_provider_catalog_signals.py`. Verdicts: `proven`, `retired`, `not_served`,
`not_entitled`, `account_blocked`, `quota_exhausted`, `inconclusive`. Only `proven`, `retired` and
`not_served` can ever change config.

| Provider | Free evidence | Signals seen |
|---|---|---|
| NVIDIA | Build Catalog "Free Endpoint" label (NGC JSON) **and** a canary | 404 "Function … not found for account" = not served (all 8 sampled card-less listings); 410 = end of life. A 200 proves *served*, not cost: two unlabeled non-chat models also served, and NVIDIA returns no billing signal, so the label is required. First bytes took up to 300 s. |
| Gemini | account-level (free-tier project) | 404 "no longer available" = retired; 429 with free-tier quota violations **without** a value = not in the free tier (models production never uses); 429 **with** a value (e.g. 20/day) = spent quota, defer |
| Mistral | chat-capable entries (account-level) | 429 + `x-ratelimit-limit-req-minute: 0` = account-blocked (all 3 keys); 403 `tier_not_allowed` = not entitled; 403 Labs = preference toggle |
| Groq | account-level (all-free account) | 404 "does not exist" = retired; headers carry per-day requests and per-minute tokens |
| OpenRouter, Kilo | `:free` + zero prompt/completion price | 403 "agentic harness" = not entitled; 429 upstream = inconclusive |
| OrcaRouter | `-free` + "(Free)" + zero request price | — |
| Airforce | record's own `tier: free` | 402 subscription = not entitled; 429 global 1 rps = spent quota |
| z.ai | none (observation-only) | `/models` omits its free flash models (absence is not evidence); code 1113 = paid; 1305 = overloaded |
| SambaNova | account-level (documented Free tier; routes are `free: true`) | 429 "high demand" = inconclusive |
| SiliconFlow, DeepSeek | none (observation-only) | — |

**Structured output (2026-09-24).** NVIDIA's `deepseek-ai/deepseek-v4.1-flash` returned a 200 with
**empty content** (`finish_reason: stop`, reasoning only) to *both* `response_format: json_schema`
and `json_object`, and valid JSON only with no `response_format`. NVIDIA's earlier
`deepseek-v4-pro` answered the default `json_schema` fine, and OrcaRouter's `deepseek-v4-flash`
answers both formats (5–14 s). No config recorded that difference, and the Worker forwarded the
job's format unchanged, so the failure was silent: an agenda benchmark spent hours retrying empty
replies before it was traced. Until PR C lands, v4.1 is out of the r6-moments and council-moments
pools (OrcaRouter v4 serves that overflow instead).

Follow-up (same day): with `prompt_only` (PR C, #1854) v4.1 returns valid JSON, but NVIDIA serves
it at roughly 11 output tokens/s with long reasoning -- 151 s for a 465-character agenda, and no
full-size agenda finished within two hours of a benchmark run. That is far past the Worker's 720 s
response ceiling, so v4.1 stays out of every production pool regardless of method. Lesson for the
canary (Slice 1): a method check proves the *shape* of an answer, not that a route is usable;
record first-byte and completion latency with each canary and flag routes whose typical task would
exceed the Worker ceiling.

## 5. Discovery backtest (2026-09-24)

`--backtest` replays the candidate gates for every configured model as if it were not configured
(catalog reads only). Every weekly run adds the one-line result to the issue's Observations, so a
plugin gate that drifts from how routes are actually chosen shows up without anyone looking.

- **Recall 34/39.** Misses: three retired or de-listed models (groq `qwen3.6-27b`; mistral
  `devstral-2512`, `mistral-large-2512`) — correct — and z.ai's two free flash models, which z.ai's
  `/models` does not list at all (added by hand; documented in its plugin).
- **What the backtest changed:** SambaNova moved from observation-only to account-level (it had
  made both configured SambaNova models undiscoverable); the lane rule moved from "beats every
  member" to "at least the weakest member" (12 real memberships missed → 4); AA matching gained the
  `-preview`/`-reasoning` and unique-bare-ID rules (most configured Gemini/NVIDIA/SambaNova models
  went from unscored to scored).
- **Remaining lane gap, by design:** four capacity placements score below their lane's weakest
  member (`gemini-3.5-flash-lite`, `gemini-3.1-flash-lite`, `gpt-oss-120b`); they appear in the
  issue's "other lanes" column with the gap rather than as checkboxes.

The same day's paused dry run found both real anomalies (Airforce `kimi-k2.7-code` now paid,
Groq `qwen3.6-27b` retired), treated the Mistral account-tier states as acknowledged, and surfaced
NVIDIA `z-ai/glm-5.3` (AA 44.8, above every configured model) as the top candidate. It also drove the
alias skip, the context floor, the definitive-only memory, the Airforce spacing and a 600 s drain.

## 6. Design and delivery

**PR C — per-route structured-output shaping (shipped in #1854).**
Implements R10 on today's routes, before the catalog automates them.
- *Config:* `structured_output_profiles` in `config/provider_limits.yml` become the four R10
  methods; `structured_output_method` + `structured_output_verified_on` may be set on a route,
  otherwise resolved as R10 describes. `compile_llm_limits.py` validates methods, resolves every
  route's method, and emits it into both `llm_routes.json` and the Worker's
  `dispatch_limits.json`.
- *Payload:* Python's durable and dispatch payloads carry `structured_output: {name, schema}`
  instead of a pre-shaped `response_format`, and messages without schema-in-prompt edits.
  Admission estimates keep today's conservative rule (the larger, schema-in-prompt form).
- *Worker:* `gateway.js:upstreamRequestForRoute` renders the four methods from the chosen route's
  method; the executor classifies an empty/unparseable structured 200 as a retryable route failure
  (R10). A stored payload without `structured_output` (jobs already in B2) keeps today's
  `response_format` passthrough until those drain; the passthrough is then removed.
- *Python direct path:* the same four renderers, one local parse/validate/retry path; Instructor's
  schema mode is retired from the direct path.
- *Tests:* the shared shaping fixture (both suites); a Worker test that an empty structured 200
  retries on another route and is counted; a compile test that every route resolves a method.
- *Original rollout plan (superseded by the latency evidence in §4):* mark NVIDIA `deepseek-v4.1-flash` `prompt_only`, re-run its agenda benchmark
  (`evals/chapter-agenda/`). The measured latency keeps it out of production pools. The other
  configured routes keep their current method until the Slice 1 canary verifies them.

**Slice 1 — observe and propose (shipped).**
`reconcile.py` plans a run: per provider, list the catalog, health-check one live route per
configured upstream model, and canary up to 3 free candidates, all inside the pause.
`issue.py` renders and syncs the rolling issue: candidates table (AA score, floor flag, context,
research links) with a checkbox decision block per candidate (ignore / add route only / add as
backup to each eligible lane), anomalies, collapsed observations, and a base64 state marker (canary
memory, scarce-route checks, deferrals, known anomalies, the last full report for `--due-only`
merges). Ticked boxes survive weekly rewrites. Workflow: weekly full run + daily due-only run,
`issues: write` only.

**Slice 2 — `/apply` → curated additions PR (contract proposal: §8.2–8.4).** `provider-catalog-commands.yml` on `issue_comment`
(`author_association` prefilter + `require_repository_write`), reading `checked_decisions` and
re-verifying candidate digests. Writes route blocks (comment-preserving append; limits from catalog,
canary headers, else rpm 1 / concurrency 1), lane `backup_models`, and `ignored` decisions; runs
both compilers and the lane/limit tests in-job (GITHUB_TOKEN PRs do not trigger CI); fixed branch
`automation/provider-catalog-additions` rebuilt from main with list/edit/create.

**Slice 3 — automatic removals, lane repair, job rescue (contract proposal: §8.5–8.6).** Remove a route only when the model is
absent from a complete catalog and its canary is `retired`/`not_served`. A free route that becomes
paid (`not_entitled`) is **never** removed automatically, even when no lane uses it: it stays a
notify-only anomaly for a maintainer decision (maintainer decision 2026-09-24). Same PR repairs lanes
(drop backups; promote the first backup for a removed primary with `needs:human-verification`;
escalate instead of removing when a lane would be empty). Worker: on a catalog-digest change, a
bounded per-tick pass fails queued jobs whose models all lack routes as `route_retired` -- including
jobs already indexed under the Worker's `__unroutable__` sentinel or under a model with no route
(2026-09-24: 34 and 10 such jobs, e.g. `llama-4-maverick`), which the first pass after Slice 3
deploys picks up because no catalog digest has been recorded yet -- and
also queued jobs no eligible route can ever admit (input above every route's hard ceiling -- e.g.
2026-09-24: ~2,600 prelabeler batches built before the 2026-09-23 sizing fix could only reach a
20/day route) as `unadmissible`, so the producer re-batches them; the
deferred sweep does not count `route_retired` toward the retry cap, and the producer resubmits
under the current lane. The lane↔route CI guard (#1849) blocks any removal that would strand a
lane.

**Slice 4 — bounded limit maintenance (contract proposal: §8.7).** Record header-reported limits and a bounded rate-probe
pass under the pause. Effective value = the maximum of the last 6 observations within 90 days.
Within ±20%: nothing. Below by >20% with ≥3 consecutive low readings: automatic tightening PR.
Above by >20%: an issue checkbox. Any swing >50% or an observed 0: flagged as a material change.
The Worker's `route_failures` (sustained `own_rpm`/`own_tpm`/`unknown_429`) triggers an early
re-probe of a scarce route.

**Historical shadow exit (2026-09-24; superseded for R5 on 2026-10-08 by §8.4 and review/53).** A shadow evaluator lane (today
`topic-tags:prelabeler-shadow`, gemma-4-26b-a4b-it shadowing the gemma-4-31b-it pre-labeler) runs
the production prompt on the same subjects, never affects display, and earns its own calibration
row: human reviews of the production evaluator are mirrored onto it through the subject's truth
(`llm_evaluation.mirror_shadow_prelabeler_review`), so it is scored at no extra reviewer cost.
It **qualifies to exit** shadow when, on those mirrored reviews, it meets the same bar the
production evaluator must meet before it may act on its own (`EvaluationConfig`):
- at least 50 scored reviews (`prelabeler_minimum_reviews`);
- at least 95% precision on each actionable decision (`prelabeler_required_precision`), each with
  at least 5 reviews (`prelabeler_minimum_decision_reviews`); and
- precision not lower than the production evaluator's over the same subjects.

When it qualifies, the rolling catalog issue shows the evidence (review counts, per-decision
precision, agreement with production) and offers a **promote shadow** checkbox; `/apply` (Slice 2)
turns it into a curated PR that adds the model as the production lane's backup (or as an
additional model) and retires the shadow lane. Promotion to the lane's primary stays a manual
edit, because the primary is part of the recipe hash. Until a task has a committed evaluation set
(GH#1852), these mirrored reviews are its task-level quality evidence; once it has one, both are
shown side by side.

## 7. Verification

- Offline: `pytest tests/test_provider_catalog_*.py tests/test_llm_lanes.py tests/test_workflows.py`.
- Live dry run: `citypods-env python scripts/reconcile_provider_routes.py` prints the would-be issue.
- Discovery recall: `citypods-env python scripts/reconcile_provider_routes.py --backtest`.
- New or changed provider: `--evidence-report --provider <name>` under the pause, then pin the new
  response shapes in the fixture.
- PR C: the shared shaping fixture passes in `pytest` and `npm test`; after deploy, one r6-moments
  job forced to NVIDIA `deepseek-v4.1-flash` completes with valid JSON, and a route configured with
  a failing method shows `structured_output_empty` in `/v2/stats?detail=1` and the job completes on
  another route.

## 8. Remaining-slice contract proposal — 2026-10-08

**Status: accepted proposal in #2178; Slice 2a shipped in #2182 (#2179); Slice 2b tracked in
[#2187](https://github.com/BashfulBits/city-meeting-podcasts/issues/2187).** The maintainer requested
implementation after merging the proposal on 2026-10-08. The remaining slices retain their separate
issue and activation gates. The earlier L3 label overstated readiness: candidate provenance, YAML writes, terminal
recovery and scoped limit observations lacked executable contracts. This section proposes those
contracts against current `main`; it supersedes conflicting shorthand in §6. It does not freeze the
whole breakout or claim the remaining slices shipped.

### 8.1 Maintainer decisions and delivery gates

The maintainer selected these priorities in chat on 2026-10-08:

1. Slice 2 additions/ignore first; then Slice 3 rescue/removal; then Slice 4 limits.
2. Split Slice 2: 2a handles additions/ignore; 2b completes paid-route decisions and shadow support.
   Unsupported choices remain visible but cannot be applied by 2a. The R5 shadow portion was
   subsequently superseded by decision 6 below.
3. Proven tightening may prepare a PR even for a decrease greater than 50%; flag the material
   change prominently. Increases still require a maintainer choice. Zero never becomes a limit.
4. Missing catalog output bounds may use a separately reviewed conservative cap. Automatic
   additions still defer missing required bounds; a tiny canary cannot establish an output ceiling.
5. Add adaptive input/output context calibration **after** bounded rate maintenance (Slice 5).
   Repeated probes should expand successful lower bounds and refine real size-rejection brackets,
   using provider-reported token counts rather than treating local estimates as exact.
6. Let review/53 own graduation. Defer the legacy R5 human-review shadow-promotion automation
   from Slice 2b; its pre-labelers and calibration will be retired by review/53 PR11. This narrows
   the earlier accepted paid/shadow scope deliberately: avoid building soon-retired automation and
   avoid adding storage access for obsolete evidence. Paid decisions complete Slice 2b; existing
   review history stays untouched until review/53's archival step. The #2188 shutdown fix remains
   a useful retirement prerequisite. Do not substitute judge graduation into `/apply` here.

Decision 3 extends G4's earlier “small” automatic changes. It saves a decision round when repeated
credible evidence proves a large capacity loss; the risk is a misleading measurement lowering
throughput. Three independent observations, scope validation and human PR review contain that
risk. Automation prepares PRs; it does not merge them or deploy changes.

Proposed merge order: **2a → 2b → 3a recovery → 3b removal → 4 → 5 calibration**.
The rescue deployment is a hard prerequisite for removal automation. Split 2b can proceed
separately from 3a after 2a; paid decisions remain in the delivery list. The legacy shadow portion
is explicitly superseded by decision 6, not silently dropped. No live probe is required to write
or test these slices.
Paused live evidence and post-deploy recovery validation are activation gates, not coding blockers.
Each slice gets a separate issue and scoped implementation PR after this proposal is accepted.

### 8.2 Shared evidence and identity contract

Add `citypods/provider_catalog/evidence.py` with immutable `CatalogEvidence`, `RouteEvidence` and
`LimitObservation` records, `catalog_digest()` and `candidate_digest()`. Serialize canonical JSON
(sorted keys, stable ordering, SHA-256); never include credentials, raw response bodies or prompts.

- Catalog evidence identifies provider, UTC observation time, completeness, upstream IDs and the
  normalized fields used for free eligibility, context and identity. Pagination order does not
  change the digest; changed eligibility or limits does. Missing pages are incomplete evidence.
- Route evidence identifies exact provider/upstream ID, account alias, availability verdict,
  verified structured method/date, latency and safe scoped limit observations. A candidate digest
  binds this evidence to catalog digest, proposed logical model key and eligible lane identities.
- Issue state advances from v2 to v3 with these optional records. Continue decoding v1/v2; those
  records remain advisory and require fresh evidence before config writes. Partial/deferred checks
  retain existing evidence exactly as #2169 does; deferral is never a new affirmative proof.
- Extend `ProviderRules` in `rules.py` with pure `model_identity` and `limit_observations` callbacks;
  defaults return no identity/observations. Plugins normalize provider-specific fields and headers.
  The core must not branch on provider names. Unknown scope is displayed but cannot change config.
- Quality matching in `quality.py` remains informational. Its publisher aliases/AA score cannot
  establish serving identity, shared quotas, or judge independence. Reuse an existing pool only
  for an exact configured upstream identity or a unique plugin-proven canonical identity. Otherwise
  report `identity_required` and request a manual mapping; no inferred brand-name pooling.
- An addition requires a positive catalog context bound and a verified structured method. Missing
  hard ceilings remain unset: a successful long request is only a lower bound. Missing catalog
  context requires a separately reviewed config/evidence change before `/apply`; never invent a
  context value. RPM/concurrency may fall back to 1 within existing provider caps. Do not invent
  TPM/RPD/output ceilings or copy provider/account capacity into independent route capacity.

Use `issue.py`, `reconcile.py` and `probe.py` to carry these records from the actual planner. Safe
headers must be captured before classification discards them. Availability and method verification
have separate freshness: on apply, refetch the complete catalog and rerun selected candidates under
R3/R6 budgets. Require unchanged candidate identity/free evidence/context and a fresh working method.
If quota or the run budget prevents proof, defer that selection and explain why; retain its checkbox.
A partial apply may propose other proven selections, but never silently mark deferred ones applied.

### 8.3 Slice 2a: authorized commands and precise config edits

New files: `citypods/provider_catalog/config_edit.py`, `citypods/provider_catalog/apply.py`,
`scripts/provider_catalog_commands.py`, `.github/workflows/provider-catalog-commands.yml`.

`process_event(event, permission)` accepts exactly `/apply` on the rolling non-PR issue with the
catalog marker. Reuse `require_repository_write` and the existing remedy command workflow's trusted
base checkout/permission check pattern. Fetch the current issue through GitHub before parsing
`checked_decisions`; webhook bodies, checkbox text and embedded state are untrusted data. Reject
unknown IDs, conflicting ignore/add choices, duplicate lanes and unsupported 2b choices. No issue
value supplies executable code, URLs, checkout refs, filesystem paths or workflow arguments.

`plan_apply(report, decisions, config)` returns an immutable edit plan plus deferred/rejected
selections. The run refetches and re-verifies as §8.2 specifies, then binds the plan to the current
main commit and config hashes. Recheck the base before push; rebuild and revalidate on changed main.
Only already-configured providers/accounts/endpoints can be used. Apply never adds credentials.

`apply_config_edits(texts, plan)` uses narrow line-span edits and a parsed semantic before/after
assertion, following the approach of `feed_yaml_edit.py` without changing that feed-only module.
Preserve comments/order and fail closed on duplicate keys, unsupported YAML shapes, collisions or
unexpected semantic differences. The exact write set is:

- `config/provider_limits.yml`: append the selected route to the identified pool or append a new
  logical model block; route IDs are provider plus upstream identity with a collision check.
  Write the verified method/date and evidenced limits; never overwrite existing route properties.
- `config/site_config.yml`: append selected model keys to `llm_lanes.<lane>.backup_models` only for
  registry-eligible lanes. Preserve primary, existing backup order and unrelated defaults.
- `config/provider_catalog_decisions.yml`: append ignored provider/upstream identities and optional
  revisit date. An ignore alone creates a decisions-only PR; it never modifies routing.
- Compiler outputs only: `citypods/compute/llm_routes.json`,
  `workers/llm-dispatch-v2/src/dispatch_limits.json` and
  `workers/llm-dispatch-v2/src/ingress_reservations.json`.

Run `scripts/compile_llm_limits.py` and `scripts/compile_llm_lanes.py`, then the lane/limit/catalog
contract tests in the workflow before push. Its contents/pull-requests write token is scoped to this
job; catalog observation keeps read-only contents. Use one concurrency group for catalog writers.
Rebuild `automation/provider-catalog-additions` from current main, and list/edit/create one PR;
force-with-lease only against the fetched automation branch. Never replace a human-owned branch.
Repeated apply produces no duplicate route, lane entry, ignore entry or PR. Keep decisions ticked
until main actually contains them; explain open-PR versus merged state on the issue. A token-created
PR must have these in-job checks because ordinary PR CI may not be triggered.

### 8.4 Slice 2b: paid decisions; legacy shadow exit superseded

Extend the same planner/editor/command files; do not add a second command surface. A paid anomaly's
explicit remove choice uses the Slice 3 removal planner only after rescue is deployed. “Keep paid”
sets that existing route's `free: false` and writes an acknowledged decision; it does not add paid
lane eligibility or change any lane's `allow_paid` policy. Require a fresh `not_entitled` observation
and show lanes that lose free eligibility. Reject a change that empties a lane's usable pool.

**Slice 2b preparation (#2187), 2026-10-08:** the maintainer approved a narrow shutdown
extension after a code mismatch: promotion also sets `tagging.prelabeler.shadow_enabled: false`.
Extend `citypods/run.py`'s dispatch-cap and StageContext construction to resolve the shadow lane
only when enabled; exclude disabled shadow work from ingress preflight,
and add an end-to-end regression in `tests/test_run.py` for an absent disabled shadow lane;
an enabled missing shadow lane must still fail loudly. This permits the exact shadow-lane removal
below without breaking builds. The fix shipped in #2188. Legacy promotion was subsequently
superseded by review/53; no human-calibration storage loader is needed in the catalog workflows.

**Paid-decision implementation checkpoint (#2187), implemented in PR #2189 on 2026-10-08:**
extend `Decision` with the
physical route ID and reviewed route digest, and `Anomaly` with optional observation date,
route-config digest and pause contention. Legacy anomalies remain advisory. `reconcile(...,
route_ids=...)` freshly checks only selected active physical routes, including separate accounts
for the same upstream, without using previous scarce-check dates or acknowledgements to hide the
result. Existing pause, quota, reservation and spacing behavior remains authoritative.

`parse_decisions()` rejects conflicting paid choices; `plan_apply()` requires unchanged reviewed
and live route digests plus current-day uncontended `not_entitled` proof. It changes only the
selected route's `free` scalar and appends an existing-schema acknowledgement with a literal,
glob-escaped upstream model. Because acknowledgements apply across a provider/upstream identity,
a selection that would conceal another configured free route for that identity defers to manual
review. Pool checks include all previously accepted paid changes in the same plan, require an
unpaused free route for an affected lane's primary model set, and do not rely on delayed backups or
paid eligibility to establish free admission. Report affected primary/backup lanes; preserve their
model lists, primary, backup order, `allow_paid` policies, quotas and gateway properties.

The editor preserves scalar comments and asserts the exact semantic delta. Repeated fulfilled
keep-paid choices spend no quota and write nothing. Selected paid decisions survive weekly outages
until main contains both the paid classification and acknowledgement; open PRs are not fulfillment.
Explicit remove selections remain deferred until deployed Slice 3 rescue and the removal planner.
No live provider calls or config changes occur during this implementation's offline validation.

The shadow shutdown prerequisite shipped in #2188. The maintainer chose review/53 graduation
on 2026-10-08 (decision 6). That design's PR11 retires this R5 pre-labeler and archives its human
calibration; this implementation enables no legacy promotion or judge-stack path. Supporting docs
include a cross-link in `review/53-judge-stack-tags-and-moments.md` so the scope change is durable.

**Historical shadow contract, superseded by review/53 (not Slice 2b implementation scope):**
For shadow exit, read current mirrored-review calibration through `citypods/llm_evaluation.py` and
current `EvaluationConfig`; recompute all §6 thresholds at apply time. Treat issue counts as display
only. Append the shadow model as a production backup, remove the exact shadow lane configuration
from `config/site_config.yml`, and retain calibration/history. Reject missing/changed parent or
shadow lanes and falling qualification. Never promote a primary through this command. Tests extend
`tests/test_llm_evaluation.py`, `tests/test_llm_lanes.py` and the new apply/editor tests.

### 8.5 Slice 3a: structural terminal reasons and bounded rescue

Implementation issue: [#2190](https://github.com/BashfulBits/city-meeting-podcasts/issues/2190);
Implemented in PR [#2191](https://github.com/BashfulBits/city-meeting-podcasts/pull/2191), merged
2026-10-08 after Slice 2b #2189. The automatic coordinator deployment succeeded; coordinated
client activation and a recovery canary remain prerequisites for Slice 3b activation.

**Implementation checkpoint (2026-10-08, #2190; PR #2191, merged):** bounded catalog
rescue, nullable terminal metadata, audit-before-delete structural recovery, producer guards and
retained-subject rebatching shipped in #2191. Python recognizes only the two specified
reasons with a nonempty catalog identity; generic/legacy failures retain existing semantics.
The terminal fence compares backend/ref against both snapshot and current stored record.
Structural failures preserve retry/schema-correction counts and remain blocked until an eligible
physical route generation changes or task-specific rebatching produces a different recipe.
Recovery estimates and input identities include the same structured schema as queue submission.
Offline validation passes: 5,284 Python tests (16 live tests deselected), 401 Worker tests,
whole-repository Ruff checks/format checks and diff whitespace checks. Local workerd verifies
billed-row reservations; the existing index preserves lifecycle write costs. No deployment occurred.
The review round also enforces combined reservations before dispatch for singleton prelabeler
batches, retains recovery contexts by job index and reuses the gate's marker read for generation
checks. The production catalog digest returns from an identity cache before serialization;
mutable overrides still serialize/compare so in-place changes restart rescue. Local CPU evidence
is in the existing benchmark README; it does not claim production invocation CPU savings.

**Maintainer decision (2026-10-08): reuse the existing queue index.** The proposed `(state, id)`
index measured 21 writes and 65 reads to build over 20 retained jobs, plus recurring lifecycle
writes. Reuse `(state, updated_at, id)` instead, avoiding that unbounded migration and ongoing cost.
Persist JSON `{after: [updated_at, id], through: [updated_at, id]}` in the planned TEXT cursor.
Capture the last queued key as a fixed upper endpoint at pass start; each page seeks strictly after
its committed cursor and no later than that endpoint. Model-index repair leaves `updated_at`
unchanged. New arrivals and requeues get current eligibility checks independently, so ongoing
traffic cannot extend this pass forever. Missing/legacy/malformed cursors restart a bounded pass;
a changed catalog digest also restarts it. No additional jobs index or raised budget is needed.

**Outage invariant (maintainer request 2026-10-08):** job/index mutations, queued-count changes,
catalog identity and cursor advancement commit in the existing single transaction. A write-budget
stop leaves the whole page unprocessed. Failure during mutation or checkpoint persistence rolls
back the page; in-memory staged progress is cleared, and DO recreation resumes the last committed
cursor. If the platform rejects reads/writes until its quota resets, leave progress intact and
resume when storage is available. Test both failure locations, repeated unavailability, recreation,
UTC budget rollover, equal timestamps and fixed endpoints under new traffic. Verify rollback with
the existing local workerd benchmark as well as the Node SQLite tests; never exhaust live quota
for testing. A rolled-back page may be safely replayed; a committed page remains committed even
if the caller loses its response.

**Rollout gate:** the Worker deploys automatically from main, independently of Python consumers.
Verify that the Python sweep/producers carrying structural recovery semantics are in use before
activating rescue in the deployed coordinator. Old clients retain `job_failed` compatibility but
would count structural failures as generic retries. Slice 3b remains disabled until deployment and
the recovery canary confirm bounded progress, preserved failure counts and no unchanged-recipe loop.

**Maintainer decision (2026-10-08):** when none of a job's active models has a
policy-eligible structurally fitting route, immediately unlock its already-declared backups.
Retain paid/free policy and lane model allowlists; do not add models, rewrite attempt counts or
activate backups for temporary pause/cooldown/quota exhaustion. Keep the usual attempt/schema
activation when the primary remains structurally usable. Extend the existing pure route helpers
in `workers/llm-dispatch-v2/src/routes.js` and their route/coordinator tests so indexing and actual
claim selection use the same rule. Test against the full catalog even when claim admission uses
a pause-filtered catalog. This resolves the primary-cannot-attempt deadlock without changing
ordinary fallback thresholds.

**Review pacing (maintainer instruction 2026-10-08):** request CodeRabbit when each implementation PR
is ready, with at least one hour between manual requests. Check repository-wide recent requests
before posting, including other sessions. Slice 2b #2189 was requested at 2026-10-08 14:14:23 UTC
([request](https://github.com/BashfulBits/city-meeting-podcasts/pull/2189#issuecomment-6061804491));
Slice 3a #2191 was requested at 2026-10-08 18:17:44 UTC
([request](https://github.com/BashfulBits/city-meeting-podcasts/pull/2191#issuecomment-6066258476));
That review was canceled after a bookkeeping push changed the head. A fresh full review on the
unchanged head was requested at 2026-10-08 19:18:38 UTC
([request](https://github.com/BashfulBits/city-meeting-podcasts/pull/2191#issuecomment-6067299806));
it completed at 19:27:38 UTC with three valid findings, fixed in one batch. The next manual
request cannot precede 20:18:38 UTC, and a later request elsewhere moves that floor.

The deployed coordinator already has `_modelsForQueuedJob()` and
`_reconcileUnroutableJobs()`. Extend them; do not introduce a second full-queue reconciler.
Structural eligibility uses the full dispatch catalog and the job's policy/context/output bounds.
Pause, cooldown, temporarily exhausted quota and slow providers cannot produce structural failure.

Add nullable `jobs.terminal_reason` and `jobs.terminal_catalog_digest`; expose them additively through
`pollBatch()` and `terminalFeed()` while retaining `error: job_failed` for old clients. Reasons are
`route_retired` (none of the eligible logical models has a configured route) and `unadmissible`
(routes exist but none fits structural bounds). Ordinary failures retain their current semantics.
The digest hashes sorted dispatch route identities and structural admission fields, not transient
ledger state. Old failed rows remain generic; never retroactively relabel them by guesswork.

Persist `catalog_digest`, `catalog_rescue_cursor` and `catalog_rescue_complete` on the existing
scheduler row. On first deploy or changed digest, scan queued jobs with the existing
`(state, updated_at, id)` index and bounded time/ID keyset pages through a fixed upper endpoint,
including old model indexes and `__unroutable__`. Budget both row reads and worst
case writes before processing; use the existing per-tick rescue ceiling and lifecycle accounting.
Reindex still-admissible jobs; mark only structural failures terminal and unindex them atomically.
Fold cursor changes into the existing scheduler accounting write. Restart the cursor when the digest
changes mid-pass; new enqueues get current structural checks. Never inspect/alter leased jobs during
this pass. Completion/requeue paths must recheck against current structural eligibility.

Update `workers/llm-dispatch-v2/src/coordinator.js` and its existing schema/accounting helpers only
as needed for these columns and existing indexes. Include index updates in billed-row accounting and maintain
current configured budgets; if the existing budget cannot fit the worst case, defer the page. Consume
SQL cursors synchronously inside the existing transaction, with no network await between writes
([Cloudflare SQLite storage contract](https://developers.cloudflare.com/durable-objects/api/sqlite-storage-api/)).
No new DO, alarm, polling endpoint or raised concurrency/write-budget knob is needed.

In `citypods/compute/llm.py`, extend `LLMDispatchTerminalError`, single/batch polling and terminal
feed decoding to retain these reason/digest fields. In `llm_deferred.py::discard_terminal_failure`,
add an explicit structural disposition that writes an audit before removing the matching handle,
preserves existing failure counts/schema-correction state and does not increment the failure cap.
`scripts/llm_deferred_sweep.py::recover_terminal` selects it only for these typed structural reasons.
Keep equality fencing so an old terminal response cannot delete a newer deferred handle.

A recovery audit records terminal digest, original recipe/input identity and recovery disposition.
Resubmit `route_retired` only when current lane membership offers a new eligible route generation;
do not repeatedly enqueue the same impossible recipe under the same catalog digest. For oversized
prelabeler work, `citypods/tags.py::prelabeler_batch_limits` regenerates batches from retained subjects
using current limits/learned ratio. Other callers retain a recoverable blocked disposition until an
eligible route or task-specific split plan exists; do not split agenda/moments inputs by guesswork.
No successful episode artifact is invalidated and no pipeline version is bumped. Recovery targets
structurally failed deferred work; already-completed recipes remain reusable.

### 8.6 Slice 3b: removals and lane repair

Implementation issue: [#2192](https://github.com/BashfulBits/city-meeting-podcasts/issues/2192).
The implementation is in PR #2193. Live publication remains disabled.
Retirement requires current-day complete catalog evidence and an unchanged route digest for
each configured physical account serving the upstream. Old issue markers alone cannot authorize
a removal. Both compilers and the lane↔route guard must pass before a managed proposal is pushed.

**Maintainer decisions (2026-10-08):** count only surviving free routes that are not explicitly
paused (`rpd: 0`) as safe lane replacements. Preserve all paid policies. Promote the first existing
eligible backup when a primary is removed; if none remains, hold back that route removal and report
pool alternatives. Remove `backup_after_attempts` when the last backup is removed and prune only
reasoning entries for models no longer in the lane. Do not rewrite existing queued jobs or their
stored policies. New lane primary identities affect new jobs; completed artifacts remain reusable.

Finish and merge the code with removal publication disabled. Then verify a bounded recovery canary
against deployed Worker/Python consumers and enable the writer in a separate reviewed activation
change. `retire.py::RETIREMENTS_ENABLED` remains false; dormant preparation/publication helpers
fail before live probes or publication while false, and no workflow calls them. The later activation
change must record canary evidence and wire the existing reconcile/writer workflow paths under the
shared writer lock. This PR does not dispatch a canary or change deployment settings.

New `citypods/provider_catalog/retire.py::plan_retirements()` shares the evidence/editor contracts.
Require fresh absence from a complete catalog **and** a definitive retired/not-served probe for the
exact route/account; generic 404, timeout, 429, structured-output failure and account-tier failures
are insufficient. With multiple configured accounts, require matching retirement evidence for all
accounts serving that route; a single key cannot retire a pooled service for everyone.

Prepare one removal PR on `automation/provider-catalog-removals`, rebuilt and checked like additions.
Remove only affected route blocks. Keep logical models with any surviving route. Drop a model from
lane backups only when no policy-eligible route remains; promote the first surviving backup for a
removed primary and label `needs:human-verification`. If any lane would become empty, reject that
route removal and escalate with pool alternatives; do not manufacture a replacement. Run the
lane↔route guard and both compilers before push. Paid transitions remain explicit 2b decisions.

Primary changes affect newly generated recipe identities after maintainer merge; they do not rewrite
stored inputs or invalidate completed artifacts. Old terminal handles follow 3a's fenced recovery.
Removal automation stays disabled until 3a is deployed and a bounded recovery canary verifies the
old sentinel/no-route indexes, no retry-cap increment and no repeating enqueue loop.

### 8.7 Slice 4: scoped observations, thresholds and budget

New `citypods/provider_catalog/limits.py` provides `merge_observations()`, `effective_limit()` and
`plan_limit_changes()`. `LimitObservation` contains metric, positive value, UTC time, provider,
account alias, optional route ID, scope (`route` or `provider_account`), source and trusted run ID.
Header limits, documented ceilings and observed throughput remain different evidence kinds;
throughput is a lower bound and cannot assert a hard ceiling or justify an increase by itself.
Unknown/mixed scopes, zero, malformed values and expired observations are non-actionable.

Store bounded advisory histories in v3 rolling issue state: at most six distinct successful runs per
scope/metric within 90 days. Each scheduled main-branch run also uploads a versioned JSON evidence
artifact with 90-day requested retention. Before automation, retrieve referenced artifacts and
verify workflow identity, successful conclusion, default-branch provenance and payload digests.
Edited issue markers cannot forge evidence. Missing/expired artifacts defer change; unavailable
retention never becomes a reason to weaken this check. Add `actions: read` only to evidence consumers.

For each comparable scope/metric, take the maximum valid value across the bounded window. Three
consecutive independent runs below the configured value by more than 20%, and an effective maximum
also below by more than 20%, permit a tightening PR. Within ±20% do nothing; above +20% offer an
explicit increase checkbox handled by 2b. Greater-than-50% tightening still prepares a PR, marked
material as the maintainer chose; an observed zero creates an anomaly only. Never auto-change
concurrency, context/output ceilings or paid/free classification from rate observations.
Provider/account observations may update existing provider caps only when all configured accounts
have compatible evidence; otherwise report the mismatch. Preserve aggregate caps and never assign
shared account capacity separately to each route. Compiler/schema extensions need a later design
if an observed scope cannot be represented by the existing config.

Reuse `citypods/llm_rate_probe.py::RateProbeRunner` budget checks. Maintenance enables only phase 0
and a small phase-1 sample: at most three rate requests per provider per run, at most 900 seconds
including drain/spacing, and the existing candidate/method probes consume the same total allowance
when combined. If scheduled R10 verification already spends that allowance, skip rate samples.
Disable input/output/endurance/agreement phases 2–4 in Slice 4; Slice 5 (§8.10) has a separate
activation gate and must not enable those existing phases wholesale. Reserve every configured-route request, renew
pause, and honor reset/cooldown; unknown/exhausted scarce quota defers. Never exceed a documented
provider ceiling to “discover” capacity. Without an exclusive Worker pause, observations are
contended and cannot support automatic tightening.

Early re-probe proposal: ≥3 `own_rpm`, `own_tpm` or `unknown_429` failures for the same route in the
last 24 hours schedules one paused check, no more often than daily; quota exhaustion still defers
until reset. Counters schedule observation only, never establish a limit. Prepare one
`automation/provider-catalog-limits` PR using the shared writer/checks; multiple automation writers
are serialized and rebuild from main, so open PR conflicts are refreshed rather than overwritten.

### 8.8 BeatAPI regression and boundaries

#2167 is merged; tests must use current config rather than its earlier context placeholders.
BeatAPI currently uses `prompt_only`, provider RPM/concurrency 1 and a successful-call account window
shared with JEV. Its measured large-input success is a lower bound, not a hard ceiling. Long calls
occupy that window until completion; elapsed 60 seconds alone does not permit another concurrent
call. Keep 65-second probe spacing and final cooldown before dispatch resumes. Do not top up the
account or change its free-tier policy. JEV transport belongs to review/49 and stays outside chat
addition/removal; catalog knowledge does not establish GPT serving-family independence.

Pin offline fixtures for catalog/header scope and documented BeatAPI error shapes before allowing
those signals to write config. Add coverage for shared BeatAPI/JEV capacity, reasoning-only method
failure, partial quota deferral preserving evidence, JEV exclusion and surviving NVIDIA/OrcaRouter
pool routes. A BeatAPI-only retirement must not remove a shared logical model or strand its lanes.
Unknown headers and serving identity require a human mapping/evidence follow-up.

Allowed supporting changes: existing catalog modules/plugins/template, the named new modules and
command workflow, reconcile script/workflow, the specified config/compiler outputs, Python deferred
paths, prelabeler batching and Worker coordinator/test helpers. No meeting adapters, city/feed
configs, audio/ASR stages, dependencies, credentials, review/49 transport or Worker deployment knobs
may change. An additional required file or behavior must be named in this contract before coding.

### 8.9 Acceptance matrix and promotion to L3

| Slice | Required offline acceptance |
|---|---|
| Shared/2a | New `tests/test_provider_catalog_evidence.py`, `test_provider_catalog_apply.py`, `test_provider_catalog_config_edit.py`: deterministic digests; incomplete/stale/deferred evidence; ambiguous identity; unknown limits; unauthorized/spoofed commands; YAML comments/semantic fencing; idempotent branch/PR updates; concurrent main change; mixed applied/deferred choices. Extend existing catalog contract/reconcile, compiler, lane and workflow tests. |
| 2b | Paid route policy/free-pool guard across a batch; fresh selected-account proof; shared-upstream acknowledgement deferral; literal glob matching; pending/fulfilled decisions; removal gate; primary and policies untouched. Extend apply/editor/reconcile tests. Disabled-shadow runtime regression shipped in #2188; legacy promotion is superseded by review/53. |
| 3a | Worker coordinator/protocol/row-accounting/rows-read tests: old sentinel/model indexes; bounded time/ID pages with fixed endpoints; transaction failure/quota outage/recreation/UTC rollover; digest change/restart; temporary quota/pause; leased-job fencing; fitting pooled alternative; structural reason compatibility and measured worst-case billing. Python dispatch-v2/deferred/sweep tests: preserved counts, audit-before-delete, stale handle fence, generation loop guard. Tag tests prove rebatching retains every subject; unsplittable tasks stay recoverable. |
| 3b | New `tests/test_provider_catalog_retire.py`: incomplete catalogs, multi-account disagreement, non-retirement signals, surviving pools, backup repair, primary review flag, empty-lane rejection and current-main rebuild. |
| 4 | New `tests/test_provider_catalog_limits.py` plus rate-probe/workflow tests: six/90-day window, independent-run requirement, stale maximum, 20%/50% boundaries, zero/mixed scopes, artifact spoof/expiry, shared account cap, scarce quota, pause failure and exact request/time ceilings. |

Each implementation runs whole-repo Ruff checks/format, the complete offline Python suite and Worker
`npm test` when Worker code changes. Config PR preparation also runs both compilers and targeted
catalog/lane/limit tests in-job. Test fixtures never require provider keys or quota.

Before promoting a slice to L3: accept its proposed technical choices, create its implementation
issue, resolve any code/file mismatch, and copy its activation gate into that issue. Remaining
review questions are technical acceptance of identity fallback, the trusted-artifact history and
structural recovery lineage; priority, initial command scope and material-tightening policy are
already decided above. #2178 and the subsequent implementation request accept the original
remaining-slice technical choices; Slice 2a shipped in PR #2182 (#2179). Slice 5 remains L2
under §8.10. No automatic removal or limit maintenance is enabled by this docs PR.


### Slice 2a implementation checkpoint — implemented in PR #2182, 2026-10-08 (#2179)

The additions/ignore command shipped with v3 evidence/digests, pure plugin identity callbacks,
selected-candidate fresh rechecks, exact command/current-issue authorization, additive YAML edits,
both compilers and in-job tests, and one managed additions branch/PR. Observation and apply share
`provider-catalog-writers` concurrency. Legacy issue markers/state remain readable; old proofs cannot
authorize additions. Deferred/rejected selections remain ticked until fulfilled on main. If a concurrent change adds the route without the selected backup,
the old choice and its advisory provenance survive weekly rewrites; apply defers the missing lane
placement for review instead of using that stale choice as new admission evidence. An already
fulfilled route/backup selection is a no-op. A pending selected choice keeps the rolling issue open.

The current compiler requires both input and output context bounds. Additions with either absent
are deferred without guessing a limit; other valid additions/ignores can still form a PR. The
maintainer accepted separately reviewed conservative output caps in #2186; automatic additions
continue to require evidenced bounds. No cap override is implemented. Initial identity opt-in covers
publisher-qualified NVIDIA/OpenRouter/Kilo IDs and native Gemini IDs; other plugins leave unknown
identities for a manual mapping. AA matching is never used for pooling.

#2180 temporarily paused BeatAPI chat routes with `rpd: 0`; #2181 restored them through the
registered `custom-beatapi` gateway slug. Apply preserves existing gateway config and backup tiers;
an explicitly paused physical route remains deferred and cannot be re-enabled by this command.
JEV stays outside chat addition.
There are no runtime config changes, live probes, pipeline/recipe bumps or episode artifact backfill
in this implementation. Paid/shadow, retirement, recovery and limit changes remain later slices.

### 8.10 Slice 5: adaptive context calibration — accepted direction, L2

**Maintainer decision, 2026-10-08:** accept separately reviewed conservative output caps when
catalog bounds are absent, and sequence adaptive context calibration after Slice 4. This explicitly
extends §8.7's rate-only maintenance scope; it does not enable context probes or override current
Slice 2a admission checks. Missing input bounds still need their own evidence/review. No runtime
config, deployment or stored episode artifact changes are part of this design update.

The goal is gradual refinement of usable input and output bounds, not a promise to discover a
provider's true maximum within three scans. Three successful 50% expansions reach about 3.4 times
the starting value; quota, token uncertainty, truncation and changing provider behavior may require
more scans or leave a boundary unresolved. Configured admission caps and experimental measurements
remain separate. A locally chosen cap must not permanently fence exploration, but documented
provider ceilings, shared account quotas and reviewed probe budgets remain hard guards.

#### Evidence and token-count feedback

Extend the existing evidence/limit-history path with typed context observations. Keep input,
output and combined input-plus-output window distinct; fix a small output reservation during input
calibration and a small input during output calibration, recording both. Provider-specific pure
parsers belong in the catalog plugins with offline fixtures, not generic number extraction from
arbitrary error text. The provider/gateway counts tokens; the model's generated claims do not count
as telemetry. Parse successful usage, documented size-error fields and documented error messages.

Each observation records local token estimate and estimator identity/version, requested output
reservation, provider-reported input/output/total counts with their documented counting basis,
explicit reported ceiling if present, finish reason and size-error classification. Record whether
reasoning tokens are included, excluded or unknown; do not add reasoning counts twice or equate
visible text with total output. Keep requested, reported, estimated and inferred values distinct.
Reject boolean, negative, contradictory, malformed or unscoped values; zero output is not proof of
capacity. Preserve safe parsed fields and provenance, never credentials, prompts or raw responses.

Provider-reported counts override estimates for that observation when their scope/basis is known.
They feed back into the next prompt construction: retain observed estimate/count pairs for the
same route, tokenizer/estimator and probe fixture; use a conservative observed mapping to approach
the next target, and update it after each response. This mapping is an estimate, not a universal
conversion ratio or proof for arbitrary production text. Use varied representative offline fixtures
and a documented uncertainty margin before translating measured bounds into admission caps.

A rejection can report both an actual request size and an allowed ceiling; store each separately.
A rejected request of 15,300 provider tokens does not prove a ceiling of 15,299. For a combined
window error, interpret the documented input/output reservation semantics before deriving any
input bound. Only compare brackets in a common counting basis with the same fixed reservation.
Unknown basis or irreconcilable counts make the observation advisory and prevent cap changes.
Where the provider supplies no actual count, retain the estimated observation but do not present it
as an exact provider-token boundary. A provider estimate correction must not silently reinterpret
old observations or overwrite the production estimator; that integration requires an explicit plan.

#### Search policy and proof strength

Maintain separate input/output search states with nullable highest verified success and lowest
definitive size rejection. Parameter acceptance, processed input and generated output are separate
evidence kinds. A 200 response with a large `max_tokens` but tiny actual output proves parameter
acceptance only: it cannot establish that output capacity. Silent parameter clamping, early EOS,
reasoning-only responses and silent input truncation must not masquerade as boundary proof.
Input probes require provider-counted input and a fixture checking processing near the tail;
output capacity requires actual provider-counted generation and interpretable termination.
Tail checks are supporting evidence; a failed content check alone is not a size rejection.

- With no rejection bracket, target 150% of the highest verified success in the same basis,
  bounded by the approved budget and documented ceilings. With no verified success, start from the
  reviewed conservative cap and establish a baseline first. Do not grow from parameter acceptance
  when the desired evidence is actual generated output.
- Once a definitive size rejection brackets a success, choose a midpoint within that bracket.
  Update bounds using the provider's actual counts; if estimate correction lands outside the
  intended interval or repeats a size, replan rather than claim progress. Stop at an agreed
  uncertainty/resolution threshold or when the per-run budget is exhausted; resume next scan.
- On a later boundary-revalidation scan, try roughly 110% of the last verified successful size,
  subject to documented ceilings and budget. Preserve the historical rejected bound; fresh success
  above it invalidates that active bracket and resumes exploration. Fresh failures refine it.
- Quota/rate errors, timeouts, gateway failures, policy refusal, invalid request shape and unrelated
  errors never tighten a context bracket. A size-like status alone is insufficient. Confirm a
  definitive rejection under a valid pause/quota before using it for a cap proposal; conflicting
  outcomes leave the boundary unresolved.

There is no implied exact maximum: report successful lower bounds and rejected upper bounds with
basis, age and uncertainty. Input success near a combined window with a one-token output reservation
cannot justify allowing that same input alongside a large production output reservation.

#### Durable state, config review and activation gate

Reuse §8.7's bounded rolling-issue advisory state and trusted successful-main Actions artifacts.
Bind state to provider, account alias, physical route, upstream model, gateway path, catalog digest,
probe fixture/version and counting basis. Carry search bounds, evidence kinds, estimate/count pairs,
last outcomes and trusted run references; a single `maximum_found` boolean is insufficient. Preserve
verified aggregate search state across artifact expiry only through a fresh successful-main artifact
that carries authenticated evidence lineage. Expired or missing proof without that lineage requires
revalidation. Changes to identity, gateway, basis or fixture require revalidation rather than mixing
old measurements into a new bracket. Bound both state size and lineage; do not accumulate an
unbounded per-probe log or treat edited issue markers as trusted evidence.

Separately reviewed caps are ordinary config PR decisions with rationale, evidence basis and an
uncertainty margin. Calibration may propose increases or reductions for explicit maintainer review;
it never automatically raises or lowers context/output caps. Slice 4's rate-tightening thresholds
and six-observation maximum are not a context search algorithm. Keep context search summaries
separate from rate aggregates while reusing their provenance checks. Current `/apply` additions
remain dependent on evidenced required bounds until a later explicit contract implements reviewed
cap handling. No unchecked cap override is introduced here.

Before L3, create a separate Slice 5 issue and specify exact file/function/schema changes, provider
parser fixtures, prompt construction/counting basis, uncertainty/resolution policy and integration
with production admission estimates. Expected extension points are catalog evidence/plugins,
`limits.py`, `probe.py`, issue state, reconcile/workflow and the existing rate-probe budget helpers;
this L2 direction does not authorize arbitrary new modules or reuse `run_phase_2` unchanged.

Also specify numerical per-request and per-run input/output/token-cost, request-count and elapsed
budgets, scan cadence/fair rotation, confirmation allowance and trusted history retention. Existing
Slice 4 request/time ceilings are not permission for large output generation. Reuse exclusive
pauses, reservation/renewal, quota checks, spacing and cooldown; share the allowance with discovery
and rate probes and defer when exhausted. BeatAPI/JEV share their successful-call account window;
long output calls occupy that window until completion. No top-ups, paid calls, credentials or
production ceiling changes are authorized by this design. Live activation requires the reviewed
budget and a scoped canary after offline acceptance; no provider calls occur for this docs PR.

Required offline acceptance: underestimated/overestimated input and output counts; provider usage
versus size-error feedback; mixed/unknown token bases; reasoning counts; shared context reservations;
clamping, early EOS and truncation; malformed/contradictory parser fields; 50% exploration, midpoint
refinement and 10% revalidation; estimate correction outside brackets; transient failures preserving
bounds; expired/spoofed provenance; route/gateway changes; resume across scans; quota/pause/time/token
ceilings; and explicit reviewed config proposals with production caps unchanged until merge.
