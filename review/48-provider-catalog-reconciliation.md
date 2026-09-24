# review/48 — Provider catalog reconciliation

**Maturity: Slice 1 shipped (observe and propose) · Slices 2–4 L3 dev-ready · redesigned 2026-09-24**

Owner: LLM dispatch maintainers. Code: `citypods/provider_catalog/`,
`scripts/reconcile_provider_routes.py`, `.github/workflows/provider-catalog-reconcile.yml`,
`config/provider_catalog_decisions.yml`. Prerequisite shipped: the v2 dispatch pause (#1848).

This supersedes the first design in PR #1841. Its free-evidence rules, Artificial Analysis matching
and comment-preserving route edits were kept; its always-open issue, digest-branch PRs, unreachable
auto-added routes, HTML scraper and hand-kept alias tables were not.

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
| G4 | Humans decide additions (issue checkboxes + `/apply` → curated PR); removals and small, conservative limit changes are automatic PRs. |
| G5 | Extensible: providers and LLM lanes are added or removed without touching the reconciler core. |

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

**Slice 1 — observe and propose (shipped).**
`reconcile.py` plans a run: per provider, list the catalog, health-check one live route per
configured upstream model, and canary up to 3 free candidates, all inside the pause.
`issue.py` renders and syncs the rolling issue: candidates table (AA score, floor flag, context,
research links) with a checkbox decision block per candidate (ignore / add route only / add as
backup to each eligible lane), anomalies, collapsed observations, and a base64 state marker (canary
memory, scarce-route checks, deferrals, known anomalies, the last full report for `--due-only`
merges). Ticked boxes survive weekly rewrites. Workflow: weekly full run + daily due-only run,
`issues: write` only.

**Slice 2 — `/apply` → curated additions PR.** `provider-catalog-commands.yml` on `issue_comment`
(`author_association` prefilter + `require_repository_write`), reading `checked_decisions` and
re-verifying candidate digests. Writes route blocks (comment-preserving append; limits from catalog,
canary headers, else rpm 1 / concurrency 1), lane `backup_models`, and `ignored` decisions; runs
both compilers and the lane/limit tests in-job (GITHUB_TOKEN PRs do not trigger CI); fixed branch
`automation/provider-catalog-additions` rebuilt from main with list/edit/create.

**Slice 3 — automatic removals, lane repair, job rescue.** Remove a route only when the model is
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

**Slice 4 — bounded limit maintenance.** Record header-reported limits and a bounded rate-probe
pass under the pause. Effective value = the maximum of the last 6 observations within 90 days.
Within ±20%: nothing. Below by >20% with ≥3 consecutive low readings: automatic tightening PR.
Above by >20%: an issue checkbox. Any swing >50% or an observed 0: flagged as a material change.
The Worker's `route_failures` (sustained `own_rpm`/`own_tpm`/`unknown_429`) triggers an early
re-probe of a scarce route.

**Shadow exit (maintainer decision 2026-09-24).** A shadow evaluator lane (today
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
