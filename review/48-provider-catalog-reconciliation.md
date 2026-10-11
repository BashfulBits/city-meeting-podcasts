# review/48 — Provider catalog reconciliation

**Maturity: Slice 1 and PR C shipped · R10 verification shipped (#2169) · Slice 2a shipped (#2182) · shadow shutdown shipped (#2188) · Slice 2b paid decisions shipped (#2189) · Slice 3a shipped (#2191) · Slice 3b code shipped (#2193); Slice 4 shipped (#2201); removal activation retains gates**

Owner: LLM dispatch maintainers. Code: `citypods/provider_catalog/`,
`scripts/reconcile_provider_routes.py`, `.github/workflows/provider-catalog-reconcile.yml`,
`config/provider_catalog_decisions.yml`. Prerequisite shipped: the v2 dispatch pause (#1848).

This supersedes the first design in PR #1841. Its free-evidence rules, Artificial Analysis matching
and comment-preserving route edits were kept; its always-open issue, digest-branch PRs, unreachable
auto-added routes, HTML scraper and hand-kept alias tables were not.

## Implementation checkpoint — 2026-10-09

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
The implementation is in [PR #2193](https://github.com/BashfulBits/city-meeting-podcasts/pull/2193),
implemented in PR #2193, merged 2026-10-08 (America/Chicago). Live publication remains disabled.
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
`scripts/provider_catalog_commands.py::prepare_retirements()` and `publish()` fail before
live probes or publication while false, and no workflow calls the retirement preparer. The later activation
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

Offline acceptance: 5,316 Python tests pass (16 live tests deselected), including 263 targeted
retirement/apply/editor/reconcile/workflow tests. Whole-repository Ruff and both compilers pass
without generated drift. The BeatAPI regression removes one physical route while retaining its
shared DeepSeek logical pool and all lane↔route guards. No Worker code changes or live calls.
Final validation excludes definitively retired routes from safe replacements even when their removal
is held back, and retains a promoted model in backups to preserve the existing retry allowance.
CodeRabbit reviewed unchanged head a7d36703 after the 2026-10-09 02:39:17 UTC request; its sole
roadmap-date finding was withdrawn after confirming the October 8 America/Chicago commit date.
The final two planner edge cases are fixed together after that review. Retirement preparation
parses freshly read lane YAML rather than the process cache, including changed-main rebuilds.
The additions/paid snapshot-cache follow-up was implemented in PR #2197 (#2196), merged
2026-10-08 (America/Chicago). `scripts/provider_catalog_commands.py::prepare()` now parses lanes
from its freshly read site-config text, like retirement preparation. The apply regressions use
successive-main snapshots against a warmed obsolete cache: current free-primary admission and
backup opt-out win. The retirement cache-guard mock accommodates the removed loader import.
No evidence, permission, provider probes, production config or publication gates changed. Its full
CodeRabbit review found no actionable comments on unchanged head 1a106c9c; current-head CI passed.
Any further manual review must be one hour after the 2026-10-09 03:40:41 UTC request and any later
repository-wide human request.

Primary changes affect newly generated recipe identities after maintainer merge; they do not rewrite
stored inputs or invalidate completed artifacts. Old terminal handles follow 3a's fenced recovery.
Removal automation stays disabled until 3a is deployed and a bounded recovery canary verifies the
old sentinel/no-route indexes, no retry-cap increment and no repeating enqueue loop.

**Remaining delivery checkpoint (2026-10-09, after #2208):** Slice 4 shipped in #2201 and the
Slice 5 L3 design was accepted in #2208. Quota admission shipped in #2214; one offline
implementation PR remains under #2209 for final calibration with input before output and all-route
coverage.
Removal activation still needs ordinary-run recovery-canary acceptance. Live input/output canaries
and recurring calibration activation remain separately reviewed changes. The snapshot-cache
follow-up shipped in #2197 and recovery telemetry in #2199 (#2198). Review/53 PR12 (accepted in
#2195) is a separate judge-stack lane-governance extension depending on #2193; it keeps standard
lane repair unchanged and is not a blocker for this remaining core sequence.

#### Recovery-canary evidence follow-up — maintainer approved, 2026-10-09 (#2198)

**Ordinary-run evidence checkpoint — 2026-10-09, tracking #2220 (partial acceptance).**
No provider calls, synthetic jobs, storage scans, deployment or activation were performed for this
inspection. The GitHub Actions run metadata and named payload-free telemetry artifacts were read.

- Scheduled sweep [37927175388](https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/37927175388)
  succeeded on `1b6f7b11048630ed6d92e4ba795b84a383cdeb69`; artifact `11615726319`
  (`llm-submission-telemetry-37927175388`) contains two `audit_persisted` events at
  2026-10-09 12:12:33–12:12:35 UTC. Both preserve failure count `0 → 0` and schema correction
  `false → false`, terminal reason `unadmissible`. Correlated fingerprints are
  `56167e9f17a4023d946787087f5615307e17bd71699ffd490044871554c0fcee` and
  `f1713a86d2aa6bc2b23a5eb09a96c0ba9a41c0b9d107914817b758150e33e636`.
- Scheduled locator [37954384745](https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/37954384745)
  succeeded on `94049c15646de70aebaab4ddf4570d4c4e824124`, and
  [37993571610](https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/37993571610)
  succeeded on `5005879bd0626a3192b8f3890802df45a30bd2e8`. Their named
  `llm-submission-telemetry-<run_id>` artifacts record four blocked submission decisions for each
  correlated fingerprint in each run: `allowed=false`, `unchanged_or_unfitting_generation`.
  Event windows are 15:51:22–15:52:03 and 21:30:48–21:31:11 UTC, respectively. Artifact
  `11646442275` supplies the latter observations. Repeated checks are blocked decisions, not
  evidence of repeated successful enqueue. A third fingerprint has blocked decisions but lacks
  a correlated audit in this inspection; it is not counted as recovery-state preservation proof.
- Both sweep snapshots and later ordinary producer/sweep snapshots show the same catalog digest
  `93ea5092844785be2a06727b927cbde4b7652a5c52f51fd1dd2bd12ebbbe2efd`, `complete=true`,
  and identical nonempty committed cursor. Latest sweep
  [37995585913](https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/37995585913),
  head `5005879bd0626a3192b8f3890802df45a30bd2e8`, artifact `11647393429`, records this at
  21:48:34 and 22:05:48 UTC. It contains no recovery events; that absence is not guard proof.
  Ordinary moments [38004086895](https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/38004086895)
  reports the same completed checkpoint through 2026-10-10 00:09:15 UTC. Its start was before
  the successful normal Worker deployment
  [38004328681](https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/38004328681)
  of `898147413ca26d4ce87265e691d5ee0de25960e9` at 23:25 UTC. This is continuity across a
  deployment window, not proof of a particular DO instance recreation or injected outage.

**Remaining gaps:** legacy sentinel/no-route index coverage is not established by these
`unadmissible` events; nonzero retry counts or an already-used schema correction are not exercised;
explicit outage/recreation continuity is not established. Current data proves two zero-state audits,
subsequent blocked checks in two runs and a retained completed checkpoint. It does not satisfy the
entire removal gate. #2220 remains open and removals remain disabled. Continue observing ordinary
runs; missing cases must not be manufactured with synthetic production jobs or inferred from offline
tests. Any change to the acceptance gate requires a recorded maintainer decision.


The deployed coordinator run [37857444179](https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/37857444179)
succeeded with Slice 3a on main. The ordinary Python sweep
[37877209806](https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/37877209806)
ran main head 263b237f, succeeded and logged 41 distinct structural recoveries with no repeated job
references within that run. Its existing telemetry has only aggregate scheduler counts; it does
not prove retry/schema-state preservation, later unchanged-recipe admission decisions or committed
rescue progress. These observations do not satisfy the removal-activation gate by themselves.

Approved file/function plan for this narrow prerequisite:

- `workers/llm-dispatch-v2/src/coordinator.js::stats()` adds `catalog_rescue` with the stored digest,
  opaque committed cursor and completion flag to the existing scheduler-row SELECT. No additional
  queue scans, writes, columns, endpoints, deployment settings or budget changes. Existing bounded
  leased-job reads remain unchanged; the new fields survive recreation because they are persisted.
- `citypods/compute/llm_deferred.py` emits optional `llm_structural_recovery` events after a successful
  structural audit write, before deleting its handle. Record effective prior/new failure counts and
  prior/new schema-correction booleans. A persisted audit event does not assert handle deletion.
  `terminal_failure_retry_allowed()` records the existing structural-marker decision: allowed new
  fitting generation, blocked unchanged/unfitting generation, missing context, invalid count or
  exhausted retry cap. Reuse existing marker reads and generation checks; admission stays unchanged.
- `scripts/llm_submission_telemetry.py::_scheduler_summary()` retains the fixed rescue fields from
  ordinary start/end stats. Old Workers expose unknown fields, not successful completion.
- `citypods/compute/llm_submission_telemetry.py::render_markdown()` reports audited count/state changes
  and admission dispositions, showing at most ten SHA-256 recipe fingerprints. Full constant-size
  events use the existing opt-in JSONL artifact; no prompts, results, original recipe labels,
  payload keys, recovery input identities, credentials or new durable audit objects are emitted.
  Disabled or unavailable telemetry never changes recovery, and missing evidence is not success.
  The summary explicitly reports no recovery observations even when the event list is empty.
- Extend `tests/test_compute_llm_deferred.py`, `tests/test_llm_submission_telemetry.py` and Worker
  `test/row-accounting.test.js`. Verify audit-write failure emits no persisted-success event;
  telemetry I/O failure leaves recovery/admission intact; typed reasons and correlation match;
  retry/schema state survives; unchanged/new-generation decisions differ; summary sampling is
  bounded; recreated stats show committed cursor/completion without extra writes or queued scans.
  Run complete Python/Worker suites and whole-repository Ruff checks.

After maintainer merge, observe ordinary scheduled producer/sweep runs and record their exact head,
deployment/run/artifact IDs and UTC timestamps. Follow at most ten fingerprints across successful
runs for the same catalog: require equal audit before/after counts/schema state, blocked decisions
when no fitting generation changed, and committed cursor progress to completion or observed
completion for that digest. A changed digest starts a different pass; unavailable stats during a DO
outage defer acceptance until a later successful observation. Do not inject live quota outages.
An idle producer with no repeated admission check is unproven, not evidence that the guard ran.
Legacy sentinel/no-route coverage also remains required; report absent live coverage as a gap,
rather than creating synthetic production jobs, widening storage scans or relaxing the gate.

Offline acceptance: 5,322 Python tests pass (16 deselected), including 55 focused deferred/telemetry
tests; all 402 Worker tests, whole-repository Ruff lint/format and diff checks pass.

CodeRabbit completed its full review of b12bddf3 with one reporting finding: empty recovery event
lists must explicitly flag missing evidence. The fix covers both empty and snapshot-only streams.
The advisory docstring-coverage threshold is not a repository requirement; no boilerplate test
docstrings are added. All current-head checks are required before maintainer merge.

This PR gathers evidence only; `RETIREMENTS_ENABLED` stays false. No synthetic jobs, provider probes,
manual deployment, production config changes, episode invalidation or pipeline-version bump.

### 8.7 Slice 4: scoped observations, thresholds and budget

New `citypods/provider_catalog/limits.py` provides `merge_observations()`, `effective_limit()` and
`plan_limit_changes()`. `LimitObservation` contains metric, positive value, UTC time, provider,
account alias, optional route ID, scope (`route` or `provider_account`), source and trusted run ID.
Header limits, documented ceilings and observed throughput remain different evidence kinds;
throughput is a lower bound and cannot assert a hard ceiling or justify an increase by itself.
Unknown/mixed scopes, zero, malformed values and expired observations are non-actionable.

Store bounded advisory histories in v3 rolling issue state: at most six distinct successful runs per
scope/metric within 90 days. Each scheduled maintenance run on main also uploads a versioned JSON evidence
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

Early re-probe: ≥3 `own_rpm`, `own_tpm` or `unknown_429` failures for the same route in today's
UTC bucket schedules one paused check, no more often than daily; quota exhaustion still defers
until reset. Counters schedule observation only, never establish a limit. Prepare one
`automation/provider-catalog-limits` PR using the shared writer/checks; multiple automation writers
are serialized and rebuild from main, so open PR conflicts are refreshed rather than overwritten.

Maintainer decision (2026-10-09): use the existing UTC-day failure bucket for Slice 4. The Worker
does not expose a rolling 24-hour window; using its existing bucket can delay a trigger across
midnight and does not retain the prior day's failures; a run before new failures can therefore
miss an early trigger unless failures recur. This avoids a telemetry/schema extension. An exact
rolling-window trigger is a follow-up.


#### Slice 4 implementation contract (#2200; maintainer accepted increase integration)

Recovery telemetry shipped in PR #2199; its final CI and automatic deployment run 37888333212
passed on merge head 34e30a6a. Normal-run canary evidence is still pending; removal stays disabled.
Slice 4 offline implementation can proceed under §8's coding-versus-activation separation.

The maintainer confirmed that Slice 4 supplies the missing explicit rate-increase `/apply` handler,
rather than leaving increases as manual recommendations. Existing additions/paid selections retain
their gates. Rate choices use authenticated artifact observations, exact current config digests and
explicit checkboxes; throughput alone cannot offer an increase.

Maintainer-approved review correction (2026-10-09): extend existing `/v2/stats` with
`rate_failures=1`, mutually exclusive with `detail=1`. `coordinator.js::rateFailureStats`
reads today's three rate-failure cells per configured route using full primary-key lookups;
a hard ceiling of 128 routes bounds the read to 384 cells. It performs no job scans or writes.
`index.js` routes this authenticated mode; `compute/llm_dispatch_pause.py` supplies a typed
client method that rejects unavailable, malformed or truncated snapshots. Scheduled reconciliation
uses that method and defers early checks on failure, never falling back to detailed stats.
Extend Worker endpoint, row-accounting and rows-read tests and Python client tests. No endpoint,
storage schema, migration or index is added. Artifact history records attempted routes; only runs
that attempted a scope affect its consecutive-sample rule. Manual runs preserve advisory offers.
Opposite-direction limit proposals wait successfully while the other proposal is open; a merged
proposal's retained bot branch does not block later maintenance.

Implementation details within the accepted modules/workflows:

- `limits.py` keeps typed observations and internal single-scope decisions; callers rehydrate actual
  verified artifact payloads rather than trusting issue observation values or matching run IDs.
  Six distinct runs per scope/metric share the 90-day window across evidence kinds. Duplicate
  samples in one run retain the conservative maximum; different sources/scopes never manufacture
  independent-run evidence. Exact rational comparisons enforce strict 20%/50% thresholds.
- Extend `evidence.py` with bounded versioned JSON artifact encoding/verification. The payload binds
  repository, run ID, workflow path, branch/head SHA, UTC observation time, current route/provider
  digests and positive scoped observations. The envelope carries its deterministic payload digest.
  Use `provider-catalog-rate-evidence-<run_id>` and the existing reconcile workflow's 90-day upload.
  Consumers verify GitHub run/workflow identity, successful scheduled default-branch provenance,
  repository identity, ancestry on current main, exact artifact name/digest, expiry and bounded ZIP
  size/contents. Independently enumerate scheduled artifact history so edited references cannot
  omit a higher sample. Missing/malformed/expired artifacts defer; no issue-edited values supply
  a write. A successful artifact with a missing scoped sample interrupts consecutive tightening.
- The v3 issue retains bounded advisory references under `rate_evidence_refs`, and current offered
  rate choices under `rate_changes`. Successful artifacts are re-fetched for every preparation.
  References include run ID and payload digest; the current producer's reference is advisory until
  its workflow succeeds. No credentials, response bodies or arbitrary raw headers enter artifacts.
- `reconcile.py` and `llm_rate_probe.py` share maintenance allowance with existing verification:
  count existing health/candidate/method requests first, skip extra samples when three requests or
  900 seconds (including drain/spacing) are consumed, and enable only reachability/short phase-1
  samples. Existing verification is not curtailed by this new optional sampling. Reuse successful
  already-required responses for scoped header observations. Every configured request reserves
  quota before sending, renews pause and honors scarce quota and provider cooldown. Missing pause
  prevents automated evidence. Default non-maintenance callers retain their verification/selection behavior; configured health
  requests now reserve quota before sending, matching the already-fenced method checks.
- Opt-in header mappings live in existing plugins and fixtures. Groq request headers are RPD and
  token headers TPM, never inferred RPM; bind to an exact configured model/account physical route
  only where that scope is unambiguous. Unknown or shared mappings stay advisory. BeatAPI/JEV
  capacity stays shared; unknown headers cannot replace its documented ceiling or split capacity.
- Extend existing `EditPlan`/editor/proposal formatting with exact scalar rate changes only. Existing
  provider caps need compatible evidence for every configured account; never copy account capacity
  to each route or create a missing cap. Preserve comments and assert the exact semantic delta.
  Automatic tightening and explicitly selected increases share `automation/provider-catalog-limits`;
  material tightening is flagged. No context, output, concurrency, paid/free or lane changes.
- Extend `provider-catalog-commands.yml` with a successful reconcile `workflow_run` consumer using
  trusted-main checkout, authenticated artifacts and the existing writer lock/lease/main rebuild.
  Current-run artifacts cannot be trusted while their workflow is still running. This consumer
  prepares tightening only; `/apply` prepares increases only after explicit selected-choice checks.
  Automatic updates wait while a reviewed-increase proposal is open; the bot commit and PR body
  record its origin. Matching provider-account ceilings/directions are required for every account,
  and capacities are never summed without an explicit shared-scope contract. Provider RPD defers
  because the existing compiler exposes only provider RPM/TPM. Add `actions: read` only where
  history/artifacts are consumed. Never execute triggering-head code
  or artifact-supplied commands. The shared bot writer runs both compilers and catalog/limit tests.

Extend the named limit/evidence/apply/editor/reconcile/rate-probe/workflow tests. Required cases
include artifact spoof/expiry, changed-main digests, independent-run/window/boundary rules, unknown
scope and shared-account mismatch, partial quota/pause failure, exact request/time ceilings,
BeatAPI/JEV cooldown and protected policy/context fields. The early-429 trigger uses existing scoped
failure counters only to schedule observation; counters cannot become ceiling evidence. No live
provider calls or deployment are performed during implementation.

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
remaining-slice technical choices; Slice 2a shipped in PR #2182 (#2179). Slice 5's L3 implementation contract was accepted in §8.11 (merged PR #2208). The original remaining-contract docs PR #2178 enabled no automatic removal or limit
maintenance; Slice 4 implementation now supplies reviewed rate proposals. Removal stays disabled.


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

### Maintainer-directed route reopening — 2026-10-09

The maintainer explicitly approved setting `rpd: 20` on these three previously paused free routes:
`openrouter_google_gemma_4_31b_it_free`, `openrouter_google_gemma_4_26b_a4b_it_free`, and
`nvidia_gemma_4_31b_it_free`, so ordinary health monitoring and eventual Slice 5 calibration can
include them. This deliberately supersedes the OpenRouter config's prior requirement for a live
recovery probe before reopening. The trade-off is a bounded opportunity to collect fresh evidence
versus renewed upstream 429s and NVIDIA shared-concurrency pressure; recovery is not yet proven.

Twenty RPD is each route's ordinary dispatch allowance, not a probe-only reservation. RPM, TPM,
concurrency, lane order, free/paid policy and token bounds stay as configured. Existing scarce-quota
checks require known remaining quota and normally revisit healthy routes every 28 days; the daily
bounded early-failure trigger can schedule a check sooner. Reopening does not change that cadence,
activate Slice 5 probes or bypass pause/quota guards. This config PR performs no live calls or manual
deployment. No pipeline-version bump or completed-episode backfill is needed.

### 8.10 Slice 5: adaptive context calibration — accepted decisions

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

#### Maintainer priorities — 2026-10-09

The maintainer accepted input calibration first, then output calibration. Implement and validate
provider-count feedback, tail-processing evidence and input search state before adding actual-output
capacity probing. Output parameter acceptance alone remains insufficient proof of generated capacity.

Start local validation with a few useful existing free routes, then expand the implementation and
its offline acceptance matrix to **all configured free routes in the final implementation PR**.
The pilot is a development sequence, not a permanent route allowlist or a separate production-only
pilot that delays broad coverage. Local validation uses recorded/synthetic provider fixtures and fake
pause/quota controls; it does not authorize live provider calls or establish live capacity proof.

All-route coverage includes BeatAPI's configured free chat routes and their shared JEV window.
It does not reactivate paused routes, include paid routes or infer unsupported token-count bases.
A route with missing or uninterpretable evidence stays deferred with an explicit reason; each plugin
must have acceptance coverage for its supported parser behavior and its fail-closed fallback.

Run conservative weekly experiments within firm reviewed budgets. Covering all routes means all
eligible routes participate in fair bounded rotation, not that every route must be probed in one run.
Token/request/time allowances, account quota and exclusive pause/cooldown gates still bound each
scan. Separate input and output token allowances prevent output generation from exhausting the
input-calibration allowance. Spare capacity may permit an experiment within the budget; it does
not authorize extra scans, budget expansion, top-ups or production cap changes.

These priorities are implemented by the L3 contract in §8.11. The accepted numerical budgets,
resolution threshold, exact parser/file plan and separate live activation gates are specified below.
Local pilot acceptance must expand to all configured free routes before calling the implementation
complete; live activation retains its separate scoped canary gate.

#### Accepted contract decisions — 2026-10-09

This contract preserves the accepted priorities above:
the maintainer approved the token-aware Worker prerequisite and doubled the proposed new budget
allowances on 2026-10-09. The maintainer also rejected an additional blanket cap margin and
accepted a tighter refinement target of 0.5% or 128 tokens, whichever is larger.
No live calls occur while writing or validating this design. Section 8.11 supplies the exact
schemas, functions, parser matrix and activation gates; offline implementation follows design merge.

**Approved starting budgets — 2026-10-09.** Use the existing weekly reconciliation slot, with no context
experiments on daily early-rate checks or incidental manual reconciles. Start with these hard
ceilings; they are allowances, never targets to consume:

| Scope | Approved ceiling |
|---|---|
| Input experiment, one call | 524,288 reserved input tokens; 256 reserved output tokens |
| Output experiment, one call | 2,048 reserved input tokens; 32,768 reserved output tokens |
| Weekly input allowance | 2,097,152 reserved input tokens across all context calls |
| Weekly output allowance | 131,072 reserved output tokens across all context calls |
| Weekly context requests | 24 calls, including baseline, retry and confirmation calls |
| One route per weekly run | At most 6 context calls; input before output |
| Weekly context wall time | 3,600 seconds, including drain, spacing and cooldown |
| One provider pause | Existing 900-second allowance; context cannot extend it implicitly |

Input and output allowances charge **both dimensions on every call**, including the fixed opposite
dimension. Context requests also consume the existing shared reconciliation request allowance;
neither pool can be topped up by the other. Stop before a call if its full conservative reservation
cannot fit, including time for cleanup. Token allowances bound conservative reservations, not an
unobservable exact provider count before submission. Reserve the larger of the local estimate and
the supported observed mapping with an uncertainty allowance; when no defensible bound is available,
defer. If returned usage exceeds the reservation, stop that route and report the overshoot rather
than refunding or hiding it. Failed, timed-out or ambiguously completed requests retain
their charge; restarting a workflow cannot reset the weekly allowance. A durable weekly admission
record is specified in §8.11.A; an advisory issue marker cannot supply admission authority.

Doubling applies to the new context allowances, including their fixed opposite dimension, route
call count and total wall time. Existing provider pause duration and production/account quota guards
remain unchanged; the larger context allowance cannot extend them. Live activation is still gated.

These starting budgets do **not** cover every configured maximum. The current
catalog includes input caps above one million and output caps up to 384,000. A route that reaches
the experiment ceiling without a definitive size rejection is `budget_limited`, retaining its
verified lower bound and next desired target. Do not report that ceiling as the route maximum,
reduce production caps to it, or repeat an identical capped experiment every week. Continue other
routes and offer a separately reviewed budget expansion. The accepted trade-off is that bounded
initial spending delays discovery of large boundaries. All configured free routes still receive parser and offline
acceptance coverage; coverage does not promise a live maximum measurement for every route.

**Existing estimator feedback versus boundary refinement.** The v2 Worker already consumes
provider-reported `prompt_tokens` and `completion_tokens`, forwarded as observed input/output usage.
`calibration.js` keeps a 32-sample window per route/model/prompt family and enables learning after
16 usable samples. Its input ratio is the larger of the configured prior and the observed p95
provider-input/local-estimate ratio; direct Google Gemma routes additionally use 1.2x input headroom.
Its output forecast uses 1.25x p95 actual output, bounded by the request maximum, and successful
completion settles quota to actual usage. Producers can read the learned input ratio through the
existing read-only calibration endpoint. These are already shipped mechanisms, not new Slice 5 work.

Slice 5 should reuse that meaning of provider-count feedback, with separate fixture-specific
observations for prompt construction and boundary search. Ordinary production output forecasts must
not size an output-capacity experiment: that experiment deliberately requests long generation and
must reserve its full output allowance. Synthetic calibration fixtures must not populate ordinary
production prompt-family estimates. Do not treat the Worker p95 as an exact upper bound for every
prompt or substitute an estimated count when the boundary observation reports an actual count.

**Maintainer decision — 2026-10-09:** add no blanket percentage reduction to measured caps.
Existing estimate calibration, request sizing and route-selection headroom already provide safety
mechanisms. Final L3 design must identify their units and where each applies; quota headroom is
not automatically a context-window buffer. Express both cap and request in the same provider-token
basis, include output reservation for a combined window, and reuse existing production estimate
correction. If that relationship is unsupported, withhold the cap proposal instead of adding an
arbitrary percentage. A separately justified buffer requires explicit evidence and review; Slice 5
does not silently stack one on existing safeguards or change production calibration.

**Fair rotation and uncertainty.** Schedule eligible routes by oldest attempted context scan,
with a stable route-id tie-break; choose never-attempted routes first. Persist deferred reasons
without marking an unattempted route as measured. A quota failure cannot advance a context bracket.
Use provider-counted observations for bound updates and retain local estimate/count pairs to
construct the next prompt. Synthetic fixtures do not redefine production counting behavior;
unknown basis or unsupported production-estimator mapping prevents a config cap proposal.

Stop midpoint refinement when the provider-token bracket width is at most the greater of **128
tokens or 0.5% of the successful bound**. Use exact comparisons without rounding a percentage
into a looser goal. This is a multi-scan accuracy target, not a reason to add calls per run: retain
an unresolved bracket and continue in later weekly scans under the unchanged approved request,
token and elapsed-time ceilings and fair rotation. Once converged, retain subsequent revalidation
and the existing 110% expansion policy rather than repeatedly refining the same resolved interval.

Report the interval, never an exact maximum. A budget-limited or uninterpretable interval is not
convergence. Repeated counts, tokenizer granularity or inconsistent provider behavior can prevent
further progress; record that uncertainty and defer without claiming the tighter target was met.
This target generally needs about four more midpoint steps than 5% precision, spread
across runs as needed; it does not promise four calls or a fixed completion date for every route.
Tail checks must succeed at multiple positions in deterministic varied fixtures; their failure is
inconclusive, while successful checks remain supporting evidence rather than proof against every
possible truncation behavior. For output, require actual counted generation and an interpretable
length termination; early EOS or parameter acceptance cannot advance that bound.

**Required quota-accounting prerequisite.** `reserveRouteRequests` currently reserves zero tokens
and describes its out-of-band calls as only a few tokens each. `dispatchPauseStatus` exposes daily
request headroom, not token headroom. Large context probes cannot safely inherit that contract.
The maintainer approved extending the existing reservation/status APIs and typed pause client to
perform bounded, atomic context admission against known request/token limits and existing pause
ownership on 2026-10-09. Exact schemas and functions still require the L3 contract. Do not assume that
calling the existing request reservation with a larger prompt checks token quota.

The L3 contract must specify input/output reservation units, existing ledger-window semantics,
shared account scopes, idempotency and crash recovery. Reserve before provider I/O; ambiguous Worker
responses or DO row-write exhaustion must authorize **no** provider call. Replayed admission must
not create another request allowance or double-charge an already admitted attempt. Unknown quota
scope or incompatible token units defer the route. Never enlarge a production token quota so a
context experiment fits. Retain conservative charges after ambiguous provider outcomes; reconcile
known actual usage only if it preserves account pacing and bounded storage writes. No queue scans,
synthetic jobs or dependency on rescue/removal activation are part of this prerequisite.

**Parser and transport contract.** Add a pure context-observation callback to `ProviderRules`;
provider modules interpret their own configured chat endpoint envelopes. A provider advertising
OpenAI compatibility is not enough to accept undocumented error numbers or reasoning semantics.
Successful usage, output termination, size rejections and counting basis need separate fixtures.
BeatAPI chat responses must not inherit JEV `/v1/systemone` field meanings or ceilings, although
both consumers share account pacing. Likewise Gemini native API fields do not automatically
describe its configured compatibility endpoint. Unknown shapes return an explicit unsupported or
inconclusive result. No native token-count endpoint or extra generation lookup is assumed.

Keep catalog discovery's first-event `canary` unchanged. Context measurement needs a separate
bounded transport operation in `probe.py` which obtains final usage/termination without retaining
prompt or completion text in durable artifacts. Its L3 specification must cap response bytes,
request duration and output reservation, distinguish cancellation from completion, and make lease
renewal/cleanup work even on timeouts. Only parser-supported routes run live; every free route has
offline support or an explicit fail-closed unsupported case.

**Proposed file plan.** Extend `provider_catalog/evidence.py` with typed observations and verified
context artifact lineage; `rules.py` and `providers/*.py` with pure endpoint-specific parsers;
`limits.py` with search planning and proposal eligibility; `probe.py` with bounded measurement;
`reconcile.py` and `issue.py` with rotation/deferred reporting and advisory state. Integrate the
weekly-only path in `scripts/reconcile_provider_routes.py` and
`.github/workflows/provider-catalog-reconcile.yml`. Extend `compute/llm_dispatch_pause.py` and the
existing Worker reservation/status handlers for the approved quota prerequisite, with focused
client/Worker tests. Use the exact command/apply/config-edit contract in §8.11.C. No production estimator, configured rate/cap, paid policy or lane-routing change is
authorized by this proposal.

Context state separates **measurement identity** from the catalog digest observed at each run.
Provider/account/physical model/gateway/counting-basis/fixture changes invalidate comparability;
an approved numeric cap edit alone must not erase a still-comparable search bracket. Authenticate
the configuration used by each run and re-check current eligibility before replaying state. Retain
at most 16 observations per route/dimension over 90 days plus a bounded authenticated summary;
§8.11.C fixes proof expiry at 90 days without indefinite summary carry-forward. Old measurements
without valid lineage cannot authorize cap edits.

**Activation remains separate.** First validate a few free routes entirely offline, then expand
the acceptance matrix to every configured free route in the final implementation PR. After merge,
request approval for a small live canary naming route identities, reserved tokens/requests, time
budget and expected evidence. That approval does not enable recurring full-catalog experiments.
Recurring activation requires a separate recorded decision after the canary's evidence and quota
accounting are reviewed. A DO outage, missing artifact or partial run remains a deferred scan;
it cannot reset budgets, loosen a cap or enable route removals.

#### Evidence and token-count feedback (accepted direction)

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
and document existing estimate/headroom behavior before translating measured bounds into admission
caps; do not add a blanket percentage margin.

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

Separately reviewed caps are ordinary config PR decisions with rationale, evidence basis and
documented existing headroom and uncertainty. No additional blanket margin applies. Calibration may
propose increases or reductions for explicit maintainer review;
it never automatically raises or lowers context/output caps. Slice 4's rate-tightening thresholds
and six-observation maximum are not a context search algorithm. Keep context search summaries
separate from rate aggregates while reusing their provenance checks. Current `/apply` additions
remain dependent on evidenced required bounds until a later explicit contract implements reviewed
cap handling. No unchecked cap override is introduced here.

Section 8.11 supplies the separate implementation issue, exact file/function/schema changes,
provider parser fixtures, prompt construction/counting basis, resolution policy and production
admission integration. Its file plan is binding; no unchanged reuse of `run_phase_2` is authorized.
Accepted numerical budgets, weekly rotation, confirmation allowance and trusted history retention
are fixed below. Reuse pauses, reservation/renewal, quota checks, spacing and cooldown, with shared
request allowances and unknown account scopes deferred. No top-ups, paid calls, credentials or
production ceiling changes occur in this design PR. Live activation remains separately reviewed.

Required offline acceptance: underestimated/overestimated input and output counts; provider usage
versus size-error feedback; mixed/unknown token bases; reasoning counts; shared context reservations;
clamping, early EOS and truncation; malformed/contradictory parser fields; 50% exploration, midpoint
refinement and 10% revalidation; estimate correction outside brackets; transient failures preserving
bounds; expired/spoofed provenance; route/gateway changes; resume across scans; quota/pause/time/token
ceilings; and explicit reviewed config proposals with production caps unchanged until merge.

### 8.11 Slice 5 implementation contract — L3, accepted in merged PR #2208

This section resolves §8.10's implementation placeholders and is normative for Slice 5. Accepted
budgets and precision are unchanged. Code may be implemented after this design PR merges; merging
the design authorizes offline implementation, not provider calls, manual deployments or activation.
Implementation issue [#2209](https://github.com/BashfulBits/city-meeting-podcasts/issues/2209)
tracks this contract. Slice 3b removal remains independently gated.

#### A. Durable admission and failure semantics

Extend the existing authenticated `POST /v2/dispatch:reserve` with discriminated operations. Missing
`operation` retains today's `{route_id, requests}` request-only behavior. Context operations reject
unknown fields, booleans as integers, non-finite numbers, unknown/paid/paused routes and stale compiled
catalog digests. All ceilings are server-owned constants; callers cannot supply larger allowances.

- `operation: context_start`, `run_id` (positive decimal Actions run id), `catalog_digest` (SHA-256).
  Set the current UTC Monday `week_start` and a 3,600-second session deadline once per week. Return
  `{ok, week_start, run_id, deadline_ms, remaining_input, remaining_output, remaining_requests}`.
  Repeating the same start returns the original deadline without writes or extension. A different
  run in the same week is rejected, including workflow re-runs with the same id but an expired
  deadline. Fresh weeks require a run id greater than every retained session id, checked before
  pruning; this bounded high-water mark rejects expired runs across rollover and long idle periods.
  Starting occurs before drain/spacing so those consume the allowance. No request/token
  allowance is charged merely for opening the session. Start denial means no context experiment.
- `operation: context_admit`, `run_id`, `catalog_digest`, `route_id`, `dimension: input|output`,
  `attempt_id` (SHA-256 of run id, route id, dimension, monotonically increasing attempt ordinal),
  `input_tokens` (conservative provider-token reservation), `output_tokens` (full requested output),
  `request_digest` (SHA-256 of measurement identity, fixture version and complete shaped request).
  Admit one request only; require the matching unexpired session and a provider pause with zero
  in-flight jobs and at least 205 seconds left. Return `{ok:true, disposition:new, attempt_id}` only
  after committing all charges. An existing matching attempt returns
  `{ok:false, error:already_consumed}` without writes; differing contents return `attempt_conflict`.
  Neither response permits provider I/O. Denials include `disabled`, `quota_unknown`, `quota_wait`,
  `budget_exhausted`, `session_expired`, `pause_not_drained`, `daily_row_budget` and `stale_catalog`.

An admission is a single-use permission consumed by the client receiving `disposition:new`; it is
not an exactly-once provider-delivery guarantee. After transport ambiguity or process restart,
abandon that attempt. Never resend it, automatically retry the provider request, or generate a new
attempt solely to bypass an ambiguous admission. A skipped call remains charged. This deliberately
trades some experiment allowance for safety across DO outages, runner crashes and lost responses.
Later weekly scans can revalidate missing evidence under a fresh week's allowance.

Add two SQLite tables through existing readiness/init machinery, without a DO class migration:

| Table/key | Stored fields and bound |
|---|---|
| `context_probe_weeks`, `week_start TEXT PRIMARY KEY` | `run_id TEXT`, `catalog_digest TEXT`, `deadline_ms INTEGER`, `input_used INTEGER`, `output_used INTEGER`, `requests_used INTEGER`; retain current plus seven preceding UTC weeks |
| `context_probe_attempts`, `PRIMARY KEY (week_start, attempt_id) WITHOUT ROWID` | `week_start TEXT`, `run_id TEXT`, `route_id TEXT`, `dimension TEXT`, `request_digest TEXT`, `input_tokens INTEGER`, `output_tokens INTEGER`, `admitted_at INTEGER`; at most 24 rows per week |

Use no secondary indexes. The maintainer approved the clustered weekly key on 2026-10-09 after
real workerd measured 192 reads for 24 current-week attempts with an attempt-only primary key.
The clustered table returns those 24 attempts with 25 billed reads (including the range boundary),
independent of retained history. Replay and cleanup use both key fields. At admission read the
current week and at most its 24 attempts (a 25-row corruption sentinel) to enforce
the six-calls-per-route ceiling; never inspect jobs or scan historical queue state for that count.
Use existing in-flight pause status queries for drain confirmation, with the existing bounded route
selection. Expire at most one oldest week's attempts and summary on session start, only within the
optional row budget; if more backlog exists, defer admission rather than unbounded cleanup. Refuse
session start if retaining it would exceed eight weeks/192 attempts. Schema readiness must protect
old deployments; no rebuild of job indexes or unrelated state is authorized.

`_transactionSync` must atomically check admission, charge weekly totals, persist the attempt and
update route quota ledgers. Estimate the full mutation cost, including primary-key index writes
and scheduler accounting, before it starts; use `_optionalRowStop` and the existing write counter.
Row exhaustion or storage exceptions roll back the entire mutation and never return permission.
Do not add a cleanup alarm or write on polling. Read-only status failures remain failures, not zero
usage. SQLite transaction semantics are documented in the
[Cloudflare storage API](https://developers.cloudflare.com/durable-objects/api/sqlite-storage-api/).

Reuse `_applyProvisionalReservation`, `_writeRouteLedger` and `earliestSafeStart` quota arithmetic,
with `inputRatio:1` for already-converted input and no learned output forecast. Require safe start
at the current time; return `quota_wait` with its next eligible timestamp otherwise. Context
experiments deliberately bypass authored context/hard-input caps so exploration can stretch them,
but never bypass documented provider ceilings or rate/TPM/TPD gates. Isolate that choice in context
admission: do not relax the ordinary job path or fabricate a persisted job. The helper extension
may skip only measured-context guards for this operation; existing callers retain defaults.
Google charges input to its trailing-minute quota; other supported routes charge input plus full
output, preserving current ledger semantics. Both dimensions always charge the weekly experiment
budget. Unknown account-sharing mappings defer; they do not create separate allowances per route.

Probe reservations are conservative consumption, not in-flight job reservations: release their
temporary provisional accounting within the admission transaction while retaining RPM/RPD/TPM/TPD
debits and trailing-window entries. No completion/refund API is added. Successful usage becomes
search evidence, but unused generation allowance is not refunded to enable more probes. Observed
usage beyond reservation stops that route for the run and reports an accounting gap; it cannot
produce a cap decision. Unknown completion, 429 or rejection keeps the charge. No claim-time output
forecast is used for intentional long-output generation.

Extend `GET /v2/dispatch:pause-status` with opt-in `context=1`: return current session totals and
route quota scope/readiness with the existing selection, without mutation. The server remains the
admission authority; client headroom is advisory. Context probes require a known physical account
scope matching the plugin mapping. Shared or unverified scopes, including BeatAPI/JEV until their
shared-start accounting is proven, receive `quota_unknown`; all such routes still get offline
coverage. This does not alter their ordinary dispatch eligibility or health checks.

#### B. Observation, parser and transport schemas

Add frozen `ContextObservation` in `evidence.py`: `schema_version=1`, `identity_digest`, `route_id`,
`provider`, `account_id`, `upstream_model`, `gateway_digest`, `dimension`, `fixture_version`,
`estimator_version`, `local_input_estimate`, `reserved_input`, `requested_output`, nullable
`reported_input`, `reported_output`, `reported_total`, `reported_ceiling`, `count_basis`,
`reasoning_basis`, `finish_reason`, `evidence_kind`, `outcome`, `observed_at`, `run_id`, `head_sha`,
`attempt_id`, and `parser_version`. `count_basis` is `input`, `output`, `combined_reserved`,
`combined_generated` or `unknown`; reasoning basis is `included`, `excluded` or `unknown`.
Evidence kind is `processed_input`, `generated_output`, `parameter_only` or `size_rejection`.
Outcome is `verified`, `inconclusive`, `quota`, `transport` or `unsupported`. All sizes are strict
non-negative integers bounded by safe-integer range; positive evidence requires positive counts.
Reject inconsistent totals, conflicting counts or an unknown ceiling basis rather than guessing.
A fresh rejection at/below comparable success marks drift/uncertainty; a fresh verified success
above a historical rejection invalidates that active bracket and resumes exploration. No raw bodies, prompt/completion text or secrets persist.

Add `ProviderRules.context_observation: Callable[[Response, ContextRequest], ContextObservation]`;
default returns `unsupported`. Shared strict envelope helpers belong in `rules.py`. Plugins opt in
per documented endpoint shape, keeping core reconciliation free of provider-name branches.

| Configured free provider | Required initial parser/fixture contract |
|---|---|
| Groq, OpenRouter | Strict chat `usage.prompt_tokens`, `completion_tokens`, optional consistent `total_tokens`; normalized finish reason. Reasoning inclusion must be declared; unknown reasoning permits input evidence only. Explicit size-error fields/messages need separate documented fixtures; never extract arbitrary numbers. |
| Gemini | Compatibility-endpoint fixtures only; native `usageMetadata`/Interactions fields remain unsupported in this path. Input quota rejection is quota evidence, never a context upper bound. |
| NVIDIA, SambaNova, Airforce, Kilo, OrcaRouter, ZAI, BeatAPI | Each plugin requires positive/negative fixtures for its configured chat envelope before enabling its supported usage parser. Without an endpoint-specific basis, return `unsupported`; a generic compatibility claim is insufficient. BeatAPI JEV envelopes are always rejected by chat parsing. |
| Any paid-only or paused route | Offline exclusion fixture; no context admission or live measurement |

Initial Groq and OpenRouter envelope references are their
[chat API reference](https://console.groq.com/docs/api-reference) and
[response schema](https://openrouter.ai/docs/api_reference/overview). Documentation examples supply
parser fixtures, never serving proof. Unsupported size errors mean success-only exploration can
proceed within budgets; they cannot supply a rejection bracket. Unsupported successful counts
prevent live measurement. Adding endpoint support is within the named plugin/test plan, requires
primary documentation or redacted captured protocol evidence, and cannot invoke live providers
merely to obtain a fixture. All configured free routes must have positive support or an explicit
unsupported reason in the final offline matrix.

Add frozen `ContextRequest` with `route_id`, `provider`, `account_id`, `upstream_model`,
`identity_digest`, `dimension`, `fixture_version`, `estimator_version`, `local_input_estimate`,
`reserved_input`, `requested_output`, `attempt_ordinal`, and transient `messages`/`shaped_body`.
Only hashes and typed observation metadata enter artifacts; transient bodies are never serialized.
Add `measure_context(request, *, session, before_call, clock, timeout=120)` in
`probe.py`, separate from existing canaries. Reuse existing request shaping, chat URL and authentication;
require final usage and finish reason. Consume a bounded response stream with a 120-second monotonic
deadline and 4 MiB aggregate byte ceiling, no transport retries, no redirects to unvalidated URLs,
and output limited by the full reserved maximum. Parse documented SSE final usage when streaming
is required; non-streaming responses use the same bounded collector. Close responses on every exit.
Call `before_call` exactly once immediately before submission; only a new durable admission permits
I/O. Abort/cancel/byte overflow is inconclusive and retains its reservation. Renew the existing pause
before admission and require cleanup time, including the existing 65-second BeatAPI cooldown.

Input fixture `context-input-v1` uses deterministic numbered filler blocks and unpredictable run-
specific sentinels at the start, middle and tail; ask for those sentinel values in a bounded response.
Success requires their exact positions/values plus provider-counted input. This checks processing,
not universal absence of truncation. Output fixture `context-output-v1` requests a long deterministic
sequence; capacity evidence requires positive provider-counted output and documented length
termination. Refusal, malformed sequence, early EOS or reasoning-only generation is inconclusive.
Do not count visible bytes as provider tokens. Use varied text families in offline construction
tests; initial live construction follows the fixture's recorded estimate/count mapping, never a
universal cross-route tokenizer ratio. Cold start uses existing configured input ratio; no unapproved
extra multiplier applies. Budget fits use the larger of that prior and the maximum compatible
observed ratio, not a production output forecast or an unbounded cross-fixture aggregate.

#### C. Search state, trusted history and cap decisions

Add `ContextSearchState` in `limits.py`, keyed by measurement identity and dimension: nullable
success/rejection observation references, `next_target`, `status`, `last_attempted_at`, and at most
16 compatible estimate/count pairs. Status is `baseline`, `exploring`, `refining`, `converged`,
`budget_limited`, `uncertain` or `unsupported`. `next_context_probe(state, limits, budget)` chooses
baseline, 150% exploration, exact midpoint or 110% subsequent boundary revalidation per §8.10.
`advance_context_state(state, observation)` uses actual counts and rejects incompatible bases.
Compare refinement exactly: width <= 128 **or** width * 200 <= success. Do not round percentage
thresholds, repeat a count as progress or spend confirmation calls outside the budget. Two compatible
verified successes in distinct successful weekly runs are required before offering a cap; a size
rejection may tighten a search bracket but never automatically lower a production cap.

Measurement identity hashes provider, account alias, physical route/upstream model, gateway path,
fixture/parser/estimator versions, counting basis and fixed opposite-dimension reservation. Numeric
config caps and rates are recorded in each run's catalog digest, not identity; changes still trigger
fresh eligibility and quota checks. Basis/identity changes discard the active bracket. Preserve
older evidence for diagnostics only. Converged states remain eligible for later revalidation, with
fair rotation; uncertainty never monopolizes the weekly queue.

**Maintainer direction — 2026-10-09:** a route stops its current scan on conflicting or repeated
counts. The next weekly scan restarts at its configured baseline, retaining fixture-local estimate/
count correction while clearing active success/rejection references. Previous observations remain
diagnostic only. Require two compatible successes in distinct subsequent weekly runs before a cap
offer; pre-restart successes cannot satisfy that proof. Replay of authenticated history enforces the
same run boundary, so an issue summary or process restart cannot erase the uncertainty gate.

Add `context_artifact`, `discover_context_references` and `verified_context_history` alongside the
rate equivalents. Artifacts include schema version, repository, successful-main run/head, catalog
digest, typed observations, attempted scopes and per-identity summaries. Authenticate run success,
workflow id/path, main ancestry, artifact identity and current eligibility using existing rate
provenance checks; issue text supplies no authority. Retain 16 observations/dimension and 90-day
search summaries; do not carry expired success/rejection proof indefinitely via a summary. Fresh
revalidation restarts stale bounds. Raw artifacts have a 4 MiB limit and at most 128 route identities;
larger catalogs fail closed with an explicit coverage gap, requiring a reviewed bound increase.
Missing artifacts lose measurement progress, never durable budget charges. Issue state may retain
rotation/deferred timestamps, but spoofed timestamps cannot invent counts or cap offers.

`context_cap_changes(history, config)` offers only `hard_input_ceiling` and `output_context_limit`
for existing free routes. No edit to `input_context_limit`, rate limits, estimator ratios, tolerance,
concurrency, paid policy, lanes or retired routes. Input-only evidence sets a verified input bound;
combined-window evidence can support an input ceiling only when the documented reservation basis
allows subtracting the current full configured production output reservation. Unknown semantics
withhold the choice. If the resulting ceiling exceeds the retained total context window, cap it at
that window minus production output reservation; report the limiting guard. Output offers require
actual generation evidence and cannot exceed the retained total window. Respect current stricter
documented quota/input guards; do not erase a 14,400-token Google ceiling on context evidence alone.
No blanket percentage reduction is added. Existing producer/calibration safeguards remain intact.

Extend exact issue choices with
`<route_id>: set <field> from <old-or-null> to <new> (context <evidence-digest>)`. `/apply` rebuilds
choices from fresh main and authenticated history, requires an exact offered selection and current
scalar match, and offers both tightening and increases for explicit review. Adds no automatic context
proposal path. A missing input ceiling may be offered as a new optional scalar; no new YAML key is
invented. Context decisions stay separate from additions lacking evidence and Slice 4's rate rules.
Use `automation/provider-catalog-context`, marker `citypods:provider-catalog-context`, existing writer
lock/compiler validation and retained-branch ownership guards. A different open context proposal
waits; never overwrite reviewed choices with a new measurement. PR descriptions include both count
bases, observed interval, existing safeguards, budget limitation and exact semantic delta.

Extend `Report` with `context_observations`, `context_states`, `context_changes` and
`context_attempted_routes`, empty by default. Add keyword `context_run=None` to `reconcile`;
legacy/daily/manual callers supply none. Issue state adds versioned `context_v1` containing
advisory route/dimension status and rotation timestamps only; rebuilding exact offers always uses
trusted artifacts. Add `plan_context_scan`, `context_edit_plan`, `parse_context_choice` and
`prepare_context` in the corresponding planner/apply/command files. `EditPlan.context_changes`
is a tuple of route id, field, old nullable scalar, new positive scalar and evidence digest;
`proposal_kind=context` selects the isolated publisher. Existing defaults and rate/addition
paths retain behavior. Workflow creates `provider-catalog-context-evidence-<run_id>` only for the
weekly full schedule on main, with 90-day retention. No new workflow input can bypass admission.

#### D. Exact file/function plan and acceptance

| Files | Authorized implementation changes |
|---|---|
| `workers/llm-dispatch-v2/src/coordinator.js` | readiness/init for the two tables; context branches in `reserveRouteRequests`; bounded opt-in status in `dispatchPauseStatus`; existing row-budget transaction integration |
| Worker `protocol.js`, `index.js`, `pacing.js` | discriminated validation/routing; context-only measured-cap bypass in `earliestSafeStart`; ordinary callers unchanged |
| `citypods/compute/llm_dispatch_pause.py` | typed `start_context`, `reserve_context`, `context_status` methods; strict disposition validation; no automatic retries |
| `provider_catalog/evidence.py`, `rules.py`, `providers/*.py` | observation/request schemas, artifact verification, pure plugin parsers and endpoint support declarations |
| `provider_catalog/limits.py`, `probe.py` | state/search/cap planning and bounded measurement/fixture construction; no unchanged reuse of phase 2 |
| `provider_catalog/reconcile.py`, `issue.py` | context report/state fields, fair planning, exact reviewed choices and deferred reasons |
| `provider_catalog/apply.py`, `config_edit.py` | context decision parsing, scalar edit plan and exact semantic assertions |
| `scripts/reconcile_provider_routes.py`, `scripts/provider_catalog_commands.py` | weekly context orchestration, typed admission, artifact output and fresh-main reviewed context publisher |
| `.github/workflows/provider-catalog-reconcile.yml`, `provider-catalog-commands.yml` | weekly-only context evidence and apply integration; preserve writer lock, permissions, secrets and pinned Actions |
| Python catalog/pause/limits/apply/config-edit tests; `tests/test_workflows.py` | meaningful search/parser/budget/provenance/editor/weekly exclusion fixtures; new `tests/test_provider_catalog_context.py` and `tests/fixtures/provider_catalog/context/` permitted |
| Worker protocol/coordinator/pacing/row-accounting/schema-readiness/index tests | durable admission replay/rollback/recreation, compatibility and bounded rows/writes |
| `review/48`, `review/11`, `ROADMAP.md`, `CHANGELOG.md`, `ARCHITECTURE.md` | lifecycle, shipped behavior and gates recorded in each implementation PR |

Do not modify production config or compiled catalogs in the implementation PR, existing calibration
estimator/forecast behavior, job schemas/rescue indexes, `/v2/stats`, JEV transport, provider secrets,
dependencies, episode artifacts or pipeline versions. Catalog compilers may run for validation but
their output must remain unchanged. Changes beyond this table require the AGENTS clarification gate.

Acceptance must prove: start/admit schema rejection; exact weekly/per-call/per-route ceilings and
UTC rollover; same-attempt replay/conflict; lost response/crash between admission and I/O; rollback
at every write; eviction/recreation; optional/hard row-budget exhaustion; cleanup bounds; zero job
scans for budgets; Google trailing-minute/input-only and other input+output accounting; unknown
shared scope deferral; pause expiry/drain/renewal; ordinary request-only reservation compatibility;
full output reservations unaffected by production forecasts; no daily/manual context calls;
unsupported parser paths for every configured free route; count correction, malformed/mixed basis,
reasoning, tail/truncation/EOS/error cases; exact 0.5%/128 boundaries; resumed 150%/midpoint/110%
search; artifact tampering/expiry/cap-only identity preservation; explicit fresh-main cap choices,
scalar insertion/change, combined-window subtraction, no unrelated config delta; writer collisions.

Run focused Python/Worker suites, full offline Python/Worker suites, whole-repo Ruff lint/format,
both catalog compilers and diff checks. Verify real-workerd bounded read/write and transaction
rollback/recreation fixtures for the new admission tables; do not treat an in-memory fake alone as
outage acceptance. No live tests are required or authorized by offline implementation acceptance.

#### E. Delivery and activation

Sequence: (1) quota-admission prerequisite PR, (2) final Slice 5 implementation PR with input first,
then output and all-route offline coverage. Use a small Groq/OpenRouter local fixture pilot internally
before expanding the final implementation matrix; no permanent pilot allowlist. Each PR closes its
review loop and ships lifecycle docs. The parent implementation issue tracks both deliverables.

Keep server-owned `CONTEXT_PROBE_ROUTE_IDS` empty and `CONTEXT_OUTPUT_ENABLED=false` in committed
implementation code; context start/admit return `disabled` and workflow does not make context calls.
Activation is a separately reviewed narrow change naming eligible route ids. After maintainer
approval, the first live canary is input-only on one parser/quota-supported free route, at most two
calls, 8,192 reserved input and 256 output per call, within the same durable weekly allowance and
pause gates. Acceptance: correct provider-count feedback, tail result, retained charges across
status reads/recreation, normal cleanup and no unexplained quota/accounting error. No synthetic DO
jobs, paid calls, manual deploy or implicit recurring activation. Output needs its own reviewed
canary after input acceptance; recurring fair rotation requires recorded maintainer approval and a
separate activation change. This calibration gate does not substitute for the removal recovery canary.

### Slice 5 quota-admission checkpoint — implemented in PR #2214 (merged 2026-10-09)

[PR #2214](https://github.com/BashfulBits/city-meeting-podcasts/pull/2214) implements §8.11.A
through the existing reservation/status handlers and typed
pause client. The server-owned route allowlist remains empty and output activation remains false;
no context calls are wired into reconciliation or workflows. Groq and Gemini have explicit physical
model/account quota mappings; duplicate physical routes, duplicate account credential mappings,
provider-wide shared ceilings and other unverified gateways defer. Normal route policy is unchanged.
The bounded schema initialization adds only the two context tables to current deployments.

Offline acceptance: 5,389 Python tests (16 deselected), 424 Worker tests, whole-repository Ruff
lint/format, both unchanged catalog compilers and diff checks. Real workerd confirms 25 billed reads
for a 24-attempt current week amid 192 retained attempts, three writes for session start, six for
first admission and zero for replay. Five injected mutation failures roll back every durable charge;
dispose/recreation retains the spent attempt. Node fixtures additionally cover the exact budget
ceilings, expired-run rollover fence, optional row-budget rejection and bounded weekly cleanup.
This acceptance does not authorize live calibration or satisfy the separate recovery canary.

The final implementation PR still supplies adaptive search, provider-count parsers, authenticated
history, reviewed cap choices, weekly orchestration and all configured free-route offline coverage.
Input/output canaries and recurring activation remain separately reviewed changes.

### Final Slice 5 implementation — PR #2218 (unmerged, 2026-10-09)

[PR #2218](https://github.com/BashfulBits/city-meeting-podcasts/pull/2218), branch
`feat/2209-adaptive-context-calibration`, contains the observation/request schemas,
strict Groq input and OpenRouter input/output chat parsers, sentinel and counted-generation fixtures,
actual-count search, authenticated bounded history, exact scalar choices and fresh-main cap
publication. Weekly orchestration uses the shipped durable admission and existing shared provider
request/time budget. All configured routes have offline eligibility and parser coverage; unsupported
endpoints, native Gemini and BeatAPI JEV remain explicitly deferred. There is no generic inferred
size-error parser, so these initial supported endpoints explore verified lower bounds without
inventing rejection ceilings from arbitrary error numbers. Unknown/shared quota scope separately
defers admission; parser support alone does not enable calls. Before session start, the client
compares its compiled catalog with the deployed Worker's canonical digest, including JS number
formatting. Each route also requires the exact deployed provider/account/physical-model scope;
a stale deployment or changed physical mapping cannot silently charge a different route.

The provider transport uses the approved cancellation adjustment below. URL validation and key
availability precede admission; the child waits for new durable permission before submission.
Preflight failures do not mark a route measured. Admission ambiguity abandons the context run,
transport ambiguity never retries the attempt, and reservation overshoot stops both dimensions of
that route. Actual input overshoots remain fixture-local correction evidence, never cap authority.
Raw prompts/responses do not enter artifacts. Two bounded compatible weekly successes can supply an
explicit lower-bound choice; reaching the experiment ceiling cannot justify reducing an existing
larger production cap. Selecting input and output changes together must fit the final retained total
window; an incompatible pair defers both exact choices rather than inventing an unselected value.
Current cap offers retain output/window guards and unsupported combined-
window semantics withhold proposals until a documented endpoint parser establishes that basis.

The first CodeRabbit review identified two integration gaps, both corrected: context-only provider
pauses require an eligible enabled route with exact physical quota scope, and advisory choices include
this run's observations so their values/digests match the artifacts `/apply` independently authenticates
only after successful completion. Failed or unfinished runs still grant no writer authority.
The follow-up review adds rejection-required cap reductions and route-local fixture-construction
error deferral. Success-only lower bounds can insert or raise a cap but cannot lower an existing one.
The suggested removal of the shared three-request guard was rejected: regular probes may exhaust
that allowance and defer context measurement, as the accepted shared-budget contract requires.

Final offline acceptance is recorded below; updated-head CI must finish. The matrix
covers every configured route and both dimensions, including paid/paused exclusions, actual-count
feedback, reservation overshoot, uncertain weekly restart, convergence revalidation, deadline/byte
cancellation, new-admission gating, tampered/missing/expired history, retained unexpired anchors,
exact fresh-main scalar edits and context-writer collisions. No production YAML,
compiled catalog, estimator, output forecast, Worker activation flag or live admission policy changes.
Input/output canaries, recurring activation and removal recovery acceptance remain separate gates.

Offline acceptance: 5,550 Python tests passed (16 live cases deselected), 346 focused catalog/context/
apply/editor/workflow tests, 424 Worker tests, whole-repository Ruff lint/format, both unchanged
catalog compilers and diff checks. Production-spawn private-URL exclusion and forked fake transport
prove admission gating and hard cancellation without live HTTP. The client canonical digest matches
Worker `canonicalJson`/`sha256Hex` for the current compiled catalog and a Unicode/numeric fixture.
The admission table's real-workerd bounded reads/writes, rollback and recreation acceptance remains
in the merged #2214 checkpoint above; final calibration adds no Worker table or schema change.
This offline acceptance does not satisfy any live canary or enable recurring scans.

**Maintainer-approved transport adjustment — 2026-10-09:** §8.11.B requires a 120-second
monotonic total response deadline. Requests' socket-inactivity timeout does not enforce that total
for a slowly progressing stream, as its
[timeout documentation](https://requests.readthedocs.io/en/latest/user/quickstart/#timeouts) explains.
The maintainer approved isolating the existing Requests call in a short-lived standard-library
subprocess. The child validates the URL and loads the configured credential before waiting;
the parent obtains a new typed durable admission before permitting submission. The parent
terminates the child at the total deadline, never retries and retains the admission charge.
The collector also enforces four MiB and closes responses. This preserves the hard bound without
adding a dependency or authorizing live calls. This is an explicit narrow adjustment to §8.11.B,
recorded after the AGENTS.md file/function-plan clarification gate.

### Slice 4 implementation checkpoint — implemented in PR #2201 (#2200; merged 2026-10-09)

The implementation supplies pure six-run/90-day history and strict rational thresholds, independently
enumerated authenticated artifacts, exact scalar edits, reviewed increase selections and a successful
reconcile consumer for automatic tightening proposals. An attempted scope without a valid sample interrupts the
three-consecutive-run rule; unrelated runs do not. Header units are explicitly mapped only for Groq's configured physical
model/account scope; duplicate physical routes and unknown/shared mappings remain non-actionable.
Matching provider-account ceilings/directions are required across all configured accounts; no capacity
summation or compiler/schema extension is inferred.

Optional reachability and one phase-1 sample share a provider-wide allowance with required verification.
Drain/spacing/cooldown consume its 900 seconds; configured reservations precede provider calls.
Undrained/expired pauses cannot supply artifact observations. Existing required verification is not
curtailed. BeatAPI's successful-call window remains shared with JEV; unknown headers supply no new
capacity and the existing 65-second final cooldown remains. Today's UTC counters schedule one early
route observation per day; the rolling 24-hour limitation is recorded above.

Mixed `/apply` selections publish independent catalog/rate proposals under the shared writer lock.
A reviewed-increase origin is recorded in the bot commit and PR body. Open increase and tightening
proposals cannot replace each other; each direction waits successfully for the other to finish.
A retained bot branch from a merged proposal can be replaced under the existing ownership/lease guard. No changes to context/output bounds, concurrency, paid/free policy, lane routing,
provider credentials, pipeline versions or completed artifacts. Removal publication remains disabled.

Offline acceptance after the six review corrections: 5,380 Python tests pass (16 deselected),
359 focused tests and 405 Worker tests pass. Counter-only tests cover authentication, mutual
exclusion, current-day/configured-route filtering, exhausted writes, recreation, unavailable reads,
route-count truncation and indexed read-cost invariance as history grows. Whole-repository Ruff
lint/format, both compilers and diff checks pass. Compiler outputs are unchanged.
No live provider probes, synthetic jobs, manual deployment or production config edits were performed.


### Recovery-canary acceptance — #2220, maintainer decision 2026-10-10

The maintainer explicitly approved replacing indefinite waiting for rare production cases with
isolated tests of the production rescue code plus the ordinary-run evidence recorded above.
The trade-off is deterministic legacy/failure coverage without production disruption; isolated
fixtures do not demonstrate actual platform quota exhaustion or every production workload.
The approved procedure creates its own temporary Worker and SQLite DO namespace, uses synthetic
records and a test catalog, and has no production bindings, provider credentials or model calls.
The existing local-only row benchmark is not deployed.

The Cloudflare run of `bench/recovery-canary/harness.js` imported the unchanged production
`LLMSchedulerDO`. The committed `result-2026-10-10.json` records the source commit/hash and deployment
version. Twenty-four fixtures include the old sentinel, missing `queue_models`, obsolete model
indexes, historical secondary index, oversized input and retired/no-route policies. All have
attempts=2, schema_retry_count=1 and transient_retry_count=3.

Acceptance results:

- A failure injected on the third transaction mutation rolled back the exact jobs, indexes,
  scheduler and route snapshot. This simulates write failure; it does not consume actual quota.
- The first bounded page repaired two fitting jobs and failed eighteen. A real `ctx.abort()`
  reset produced a new instance UUID; the exact persisted snapshot and incomplete cursor survived.
- The resumed page failed the remaining four jobs; a repeat pass repaired/failed zero. The rescue
  checkpoint completed with two queued indexed jobs, correct `unadmissible`/`route_retired` reasons,
  and every nonzero retry counter unchanged.
- The existing Python structural-audit/admission regressions passed for both terminal reasons,
  preserving failure_count=2 and schema_correction_attempted=true, blocking unchanged/missing
  generations and allowing a new fitting generation. The focused deferred/telemetry suite passed
  55 tests and the complete Worker suite passed 437 tests.
- The test class was deleted by migration and the temporary Worker removed. Cloudflare listings
  confirmed neither remained. No production data, provider quota, config or deployment changed.

Combined with the two preserved production audits, subsequent blocked unchanged-generation checks
in two ordinary runs and completed checkpoint continuity, this satisfies the revised #2220 evidence
gate. It does not authorize removal activation: prepare and review that narrow change separately.
Live context input/output canaries and recurring calibration remain independent gates.

### Proposed input-canary activation contract — #2221, 2026-10-10

**Maintainer approval — 2026-10-10.** Permit the existing six-call per-route weekly limit
on the single verified free Groq route, with no one-week expiry. This intentionally replaces
§8.11.E's two-call input canary and the proposed expiring window. The maintainer judged the
volume small and approved reviewing evidence immediately after the first scheduled run,
without a week-long waiting period. Wider route activation remains a separately reviewed
change after that evidence passes; output remains disabled.

**Route and identity.** Select `groq_gpt_oss_120b_primary`, upstream `openai/gpt-oss-120b`,
free account `primary`. Physical quota scope is `groq:primary:openai/gpt-oss-120b`; the deployed
status endpoint confirmed that exact mapping, disabled activation and no context session on
2026-10-10. The deployed and checkout canonical catalog digests both equal
`77c6bd0280d6c5cb13ce1b5d608373be62775e1f278c8d7190a060c12300bd36`.
Groq's [rate-limit documentation](https://console.groq.com/docs/rate-limits) describes organization/
model accounting and lists this free route at 8,000 TPM, 30 RPM and 1,000 RPD. The catalog has one
credential mapping and one physical route for this model/account, with no provider-wide shared cap.
This is verified configured scope, not proof that unrelated applications never use the account.
Existing quota admission and provider pause/drain remain mandatory. Fresh checkout/deployed digest
equality is required at run time; a mismatch defers, never bypasses the check.

**Approved bounded authority.** At most six durably consumed input admissions per UTC week,
including lost responses or process restarts; no output admissions. Each call reserves at most
8,192 input and 256 output tokens. Use the existing 7,125-token input baseline and fixture-local
count correction. Quota admission remains binding; an oversized adaptive target defers rather
than shrinking/repeating the measurement. Six is a ceiling, not a promise of six observations.
One verified observation can satisfy count/tail feedback; this is not a maximum-cap proof.

Only the existing full weekly scheduled main workflow initiates measurement within its shared
request, pause, time and durable weekly budgets. No top-up if regular probes consume the allowance.
The earliest opportunity is 2026-10-12 10:17 UTC, contingent on review, merge and successful
normal Worker deployment. No manual workflow, provider call or deployment. Review first-run
evidence immediately when available; no mandatory week-long wait before proposing expansion.

**Evidence and acceptance.** Preserve the successful-main context artifact's authenticated run,
head, catalog digest, route identity, estimated/reserved/provider-reported input counts, parser
basis and exact start/middle/tail result. Record payload-free status snapshots before and after
the scheduled run and a later read/normal recreation, showing consumed request/input/output
charges never decrease. Match admissions to observations; a missing response remains charged and
deferred. Require normal provider pause release and no unexplained accounting/quota error.
Unsupported, truncated, conflicting-count, missing-tail or outage observations are not success.
Do not force a production restart to obtain recreation evidence; if it has not been observed,
keep that acceptance item open rather than claiming it happened. No cap changes from this canary.

**Disable and rollback.** Clear the server-owned allowlist in a reviewed revert through normal
deployment. Existing provider-pause cleanup still runs; retain tables, evidence and charges.
Verify status disabled and later weekly runs make no context calls. The approved single-route
input pilot may recur until disabled; #2223 broader fair rotation remains separately approved.
Input acceptance precedes #2222 output planning. No removal automation is enabled.

**Activation file plan.** Worker `src/coordinator.js` enables this one route and a server-owned
8,192 input ceiling, keeping output false and the existing durable six-call limit. No schema,
endpoint, client, workflow or production catalog changes. Coordinator tests verify the exact
allowlist, output/other-route rejection, oversized input denial, six successful admissions and
seventh denial. Existing rollback/replay/recreation and ordinary admission tests remain binding.
Lifecycle documents record the revised approval; #2221 remains open for live evidence.

### Manual calibration trigger contract — #2221 follow-up, 2026-10-10 (L3)

The maintainer requested a reusable manual trigger instead of waiting for Monday, with selectable
routes and budgets for remaining review/48 canaries. They chose a separately bounded manual
allowance and server-approved routes only. This intentionally revises §8.11's schedule-only/no-
top-up rules. Faster feedback and bounded reruns justify the additional accounting and evidence
complexity; ordinary provider quotas and approval of new route/dimension authority remain binding.
The maintainer explicitly approved the exact limits and session/evidence/schema plan on
2026-10-10. Implementation and normal deployment are authorized; actual manual canary
execution remains a separate explicit run selection.

**Approved hard allowance.** A separate manual pool allows 24 consumed admissions, 2,097,152
reserved input tokens and 131,072 reserved output tokens per UTC week. Combined scheduled plus
manual authority is therefore at most 48 calls, 4,194,304 input and 262,144 output tokens weekly.
No refund for a timeout, lost response or unavailable artifact. At most eight calls per manual
run and twelve manual calls per route per week, subject to remaining pool capacity and provider
quota. All dimensions share that route ceiling. A run cannot replenish either pool; exhaustion
requires waiting for UTC rollover or a newly reviewed server-ceiling change. The existing
provider pause/drain, row-write budget, transport cancellation and cleanup reserve still apply.

**Workflow options.** Extend existing `provider-catalog-reconcile.yml` manual dispatch with an
explicit context-canary switch; comma-separated exact route IDs (one to eight); dimension choice
`input`, `output` or `both`; call ceiling (default two, maximum eight); input/output per-call
reservation ceilings; per-run aggregate input/output budgets (bounded by manual pool); and a
required purpose/issue reference (bounded plain text, never shell-interpolated). Default input
ceiling is 8,192 and output reservation 256. Advanced per-call ceilings cannot exceed existing
reviewed transport/probe bounds (524,288 input and 32,768 output), route-specific server authority
or provider admission. Requesting higher values never overrides the deployed 8,192 Groq pilot
ceiling. For output mode, the input fixture reservation remains 2,048. No paid routes, unknown
quota scopes, unsupported endpoints or disabled dimensions. No inputs edit provider limits,
production caps, route allowlists, secrets or schema. Reject invalid/duplicate/unknown selections
before pause or provider I/O. Reject mixing manual context with due-only/catalog discovery.

**Execution and sessions.** Manual mode runs context measurement only on the selected routes;
it does not spend allowance listing catalogs, adding models, running health/rate probes or
publishing config proposals. It shares the existing writer concurrency lock. Only authenticated
`workflow_dispatch` on default-branch main may run; no local CLI credential bypass. Use explicit
manual start/admit/finish operations through existing reservation handlers and typed client.
Server persists selected routes/dimensions and run ceilings, checks them on every admission and
uses a separate bounded manual ledger. Finish releases session ownership while retaining charges.
A monotonically newer run can start after finish or expiry; old runners are fenced. A crashed
run's consumed attempts remain charged and its session expires normally. No admission retries.
Status is payload-free and includes remaining manual totals and session identity.

**Storage and outage contract.** Add bounded `context_manual_weeks` and
`context_manual_attempts` SQLite tables, keeping scheduled tables/keys/accounting unchanged.
Week summaries retain consumed totals, monotonic run watermark, active session deadline and
validated session limits/selection. Cluster attempts by `(week_start, attempt_id)` WITHOUT ROWID
with no secondary index. Retain eight weeks, prune at most one expired week per start, at most
24 attempt rows per week. Reject oversized/inconsistent rows. Account every read/write/prune
against existing row budgets. Atomic admission charges manual pool, route quota and attempt
receipt together. Replay, lost response, rollback, actual workerd recreation and UTC rollover
must prove charges cannot reset or migrate into the scheduled pool. No historical job scans.

**Evidence.** Upload named manual-context artifacts with run purpose, selected identities,
requested limits, admission/status accounting and typed payload-free observations. Authenticate
repository, exact workflow identity, successful default-branch main run/head ancestry, event
`workflow_dispatch`, artifact identity/digest/size and route/parser/quota eligibility. Discover
manual evidence separately with bounded pagination; missing/partial artifacts are deferred.
Manual observations can satisfy canary acceptance, but cannot bypass the existing two distinct
weekly-success rule for production cap offers. Scheduled cap history remains scheduled-only.
Output support/activation and broader rotation still require #2222/#2223 review.

**File/function plan for approval.**

- `.github/workflows/provider-catalog-reconcile.yml`: typed dispatch inputs, environment-only
  argument transport, main/event guards, isolated manual mode and manual artifact upload; pinned
  actions, writer lock and minimum existing permissions retained.
- `scripts/reconcile_provider_routes.py`: validate manual arguments and context-only orchestration;
  start/status/finish with cleanup in `finally`; no regular discovery work in manual mode.
- `citypods/provider_catalog/reconcile.py`, `limits.py`, `probe.py`: explicit selected-route/
  dimension/call/token planning using current count parsers and bounded transport.
- `citypods/compute/llm_dispatch_pause.py`: typed manual session/admission/status/finish methods.
- Worker `coordinator.js`, `protocol.js`, `index.js`: discriminated manual operations, new bounded
  tables/readiness, atomic accounting, scoped session fencing and optional read-only status.
- `citypods/provider_catalog/evidence.py`: separate bounded manual artifact discovery and
  authentication; keep scheduled cap-offer provenance unchanged.
- Existing context/pause/workflow/evidence and Worker protocol/coordinator/index/row-accounting
  tests, plus `bench/rows-written`: denial-before-I/O, multi-run conservation, stale-session
  fencing, pool separation, every-write rollback, replay, recreation and billed storage bounds.
- `review/48`, `review/11`, `ROADMAP.md`, `ARCHITECTURE.md`, `CHANGELOG.md`: approved deviation,
  lifecycle and actual activation/evidence status. #2221 remains open until input evidence passes.

Do not alter credentials, dependencies, provider/compiled catalogs, ordinary jobs/rescue indexes,
producer estimator margins, production cap choices or paid policy. This trigger does not itself
activate additional routes or output, perform manual deployments or authorize test execution.
After contract approval, implement and review the trigger PR, merge/deploy normally, then the
maintainer may select an approved manual canary. Failed evidence remains charged and deferred.

### Manual trigger implementation checkpoint — #2221, prepared 2026-10-10

The approved L3 manual contract is implemented through the existing reservation handler and
workflow. Dispatch exposes an explicit context-canary switch, exact route IDs, dimensions, call
ceilings, per-call input/output ceilings, aggregate token ceilings and purpose. Manual mode performs
only context measurement, authenticates the current in-progress main dispatch run and verifies
deployed catalog/scope/parser authority before pause or admission. Existing scheduled execution
and scheduled cap-offer history remain separate. Manual evidence is authenticated independently.

Manual week/attempt tables are additive, clustered and bounded; startup readiness adds only these
tables to existing production objects. A finish or expired session permits a monotonically newer
run without resetting consumed weekly charges. Old runners and consumed attempts remain fenced.
The server enforces 24 calls/2,097,152 input/131,072 output weekly, eight/run, twelve/route/week
and current route/dimension authority. Groq pilot input remains capped at 8,192, output false.
Manual inputs cannot increase provider quota or widen route authority. No live run or manual
deployment was performed as implementation acceptance. #2221 remains open for explicit canary
execution/evidence; #2222/#2223 remain separately gated.

Real local workerd acceptance: three writes at start, seven per admission, zero on replay and
recreation; five injected SQL-mutation failures plus the accounting-flush boundary restore the
exact snapshot. A newer run preserves charges. Current-week attempt reads are exactly 24 among
192 retained attempts, with zero writes. No provider I/O, production data or credentials.

**Manual diagnostic evidence follow-up — CodeRabbit CLI review, 2026-10-10.** A measurement
exception must not skip the partial artifact, and a cleanup/status failure must not replace
the original measurement error. Manual dispatch uploads diagnostic artifacts even after failure;
records contain only error stage/type, attempted routes, available observations and accounting.
Failed runs/artifacts remain excluded from accepted measurement and scheduled cap history.

### 8.12 Experimental request ceilings and TPM topping-off — L2 extension, 2026-10-10

The maintainer requested that calibration use experimental data to adjust the request sizes the
Worker will admit, including routes whose provider accepts an individual request larger than its
published tokens-per-minute (TPM) rate. The goal is to learn the largest safe per-request input
over time, while keeping the provider's refill rate and the model's context window as separate,
unchanged facts. This extends §8.10–8.11, which currently use calibration evidence to offer
reviewed catalog cap changes; it does not authorize changing catalog TPM, model window, route
allowlist, weekly budget, output authority or quota scope.

**Separate the limits.** There are three different quantities:

1. The model's input context window is a model capability. Calibration does not change it.
2. Provider TPM is the token-bucket refill rate and remains sourced from reviewed provider limits.
   A larger accepted request does not demonstrate a higher TPM.
3. The per-request hard input ceiling is a request-size constraint. Authenticated experiments may
   establish a verified-success lower bound and, only when an endpoint has a reliable typed
   context-size rejection parser, a rejection upper bound. The Worker may adjust its effective
   request ceiling from those bounds without rewriting YAML or compiled catalogs.

The current Worker already refills route and shared-provider token budgets at configured TPM and
can wait for a reservation larger than one TPM interval when the bucket has accumulated enough
tokens. It separately applies `hard_input_ceiling`, and bounds the shared provider bucket to five
TPM windows. Therefore a route may top off to a request above TPM only when its effective hard
ceiling permits that size, the route and provider buckets have enough balance, and existing
shared-scope and rolling gates pass. This does not establish that every provider permits such a
burst; `context_limit > tpm` alone must never infer it.

**Use observed provider counts and typed outcomes.** For every experiment, retain the exact
reserved amount and provider-reported input/output/total counts when available. The parser must
feed the observed count, not the local tokenizer estimate, into calibration brackets and Worker
settlement. Record only allowlisted, payload-free response evidence: outcome class, typed HTTP or
provider error class, request/response timing, documented rate-limit headers and retry timing.
Never persist prompts, completions, API keys or arbitrary response bodies. Classify at least:

- verified success, with actual provider token count;
- explicit context/request-size rejection, only for a provider-specific recognized condition;
- rate-limit/TPM rejection (for example 429), which is not a context ceiling;
- transport, authentication, quota-scope or otherwise unknown failure.

An explicit context-size rejection narrows the request-size upper bound but does not erase the
largest verified successful request. A rate-limit rejection updates rate-limit/backoff evidence
and the applicable token balance/cooldown; it must not lower the learned request ceiling or
configured TPM. Unknown outcomes move neither bound. Success above TPM proves only that the
observed request size was accepted with that experiment's bucket state; it is not by itself an
exact maximum or proof of sustained throughput.

**Learn the safe request ceiling over time.** Calibration keeps a route- and physical-quota-scope-
bound search interval: the highest request size verified successful using actual provider counts,
and the lowest reliably classified context-size rejection, if one exists. Search/refinement follows
§8.10's accepted 0.5% or 128-token stopping goal and existing weekly job budget; it adds no
blanket safety margins at each stage. If no context rejection has been observed, later approved
experiments may stretch the successful bound upward, subject to existing per-run and weekly
allowances, route/input authority, the shared bucket's five-TPM-window ceiling and normal provider
admission. After a verified context rejection, probe within the narrower bracket. The Worker raises
its effective request ceiling only to a provider-count-verified success; it lowers that ceiling
only when an unambiguous context-size rejection establishes a tighter bound. Until then, retain the
current configured ceiling/behavior. Expired, stale-catalog, mismatched physical-scope, partial or
failed evidence cannot change runtime authority.

This is automatic adjustment of the Worker's runtime request ceiling from authenticated experiment
evidence, not automatic editing of provider catalogs or provider quotas. Each result must link to
its admitted attempt, route, physical quota scope, parser version, deployed catalog digest and
actual provider count. Persist it through a typed, idempotent settlement to the existing durable
Worker authority, with bounded row/read/write cost and outage-safe rollback and recreation
behavior. A workflow artifact alone is not runtime authority. Competing runs cannot overwrite
newer evidence; accepted results are monotonic by evidence time/version and stale runners are
fenced. Provider counts exceeding a reservation are useful observed-count evidence but do not
retroactively increase admission or weekly charges.

**Evidence for topping-off behavior.** The canary tests more than a single large request. For an
approved route/scope, controlled requests should demonstrate (a) success for an actual
provider-counted request larger than one TPM interval after sufficient idle/refill and (b) a later
request submitted only after the Worker replenishes the corresponding token deficit. Capture
documented rate-limit headers and timings where available. A rate-limit response remains rate
evidence and is never labeled a context failure. Keep attempts inside existing canary/manual
budgets and shared-scope gates; do not create synthetic Worker jobs or override provider quotas.
A successful above-TPM request supports a bounded burst-capability classification for that exact
physical scope and observed size, not a blanket provider-family rule.

**Activation and failure behavior.** Live calibration remains disabled until a separately reviewed
activation specifies route/scope, dimensions, bounded experiments and rollback. Runtime ceilings
may move only within the model context limit, existing compiled/transport maximum and provider's
established shared bucket bound. No experiment changes TPM, context window, RPD/TPD, concurrency,
paid policy, output enablement or route eligibility. Missing or conflicting evidence fails closed to
the last authenticated ceiling. Rollback disables experimental ceiling updates and returns to the
last reviewed static ceiling; it does not erase evidence, weekly charges or rate-limit state.
Telemetry must expose the active ceiling, evidence version/source, last observed actual count and
rejection class without request content.

**Maturity and follow-up.** This extension is L2. Before implementation, promote it to L3 with the
exact settlement operation and bounded durable fields, evidence authentication/retention rules,
endpoint-specific rate-limit and context-error parsers, search scheduling, Worker overlay
precedence, rollback procedure and tests for stale/replayed results, rate-limit/context
classification, actual-count corrections, above-TPM refill behavior, outage/recreation and shared
provider concurrency. Update the §8.11 file/function plan and implementation issue then. This
design amendment does not implement or activate runtime ceiling adjustment.
