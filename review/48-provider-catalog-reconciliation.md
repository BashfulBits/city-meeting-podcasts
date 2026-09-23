# review/48 — Provider catalog reconciliation and free-route evidence

**Maturity: L3 dev-ready · authorized 2026-09-23**

Owner: LLM dispatch maintainers. Scope: `scripts/reconcile_provider_routes.py`,
`config/provider_limits.yml`, `tests/test_reconcile_provider_routes.py`, and the scheduled GitHub
Actions workflow. This supersedes review/41 §3.4's append-only maintainer discovery restriction for
the explicitly reviewed reconciliation path; the deploy compiler remains a deterministic,
network-free YAML-to-JSON transformation.

## Goal

Keep the committed free-route catalog aligned with each provider's authenticated model catalog and
authoritative free-tier evidence. A scheduled run may create a review PR and may maintain an
operational GitHub issue, but it never merges, deploys, or invents a substitute model.

## Evidence and decision rules

The reconciler records a compact, redacted report for every provider. A model-list response only
establishes availability. It does not establish either pricing or the active account's entitlement.
Every automatic addition therefore needs provider-specific free evidence, an exact independent
Artificial Analysis comparison at least equal to the lower score of GPT-OSS-120B and Nemotron-3
Super, and a small
non-sensitive completion canary that returns HTTP 2xx. Missing or incomparable quality evidence is
an inconclusive issue finding, not a candidate for a separate quality workflow.

Artificial Analysis is the primary quality source: its documented LLM API publishes its independent
Intelligence Index and individual benchmark scores under stable creator/model IDs. One catalog fetch
is shared by the entire reconciliation run; a candidate must match both the normalized publisher and
model slug exactly, and its non-negative, numeric `artificial_analysis_intelligence_index` must meet
the lower score of `openai/gpt-oss-120b` and `nvidia/nvidia-nemotron-3-super-120b-a12b`. No score is
inferred from an earlier model version, model name, vendor claim, parameter count, or rank. This
direct rule admits newly evaluated releases (such as GLM-5.3) without assuming that a predecessor's
score carries forward.

The Intelligence Index is a versioned, evolving composite. A methodology or evaluation refresh can
change numeric scores, so a reconciler-added route records its AA score only as a dated YAML comment:
it is explanatory and never changes dispatch behavior. Each reconciliation re-fetches AA and applies
the current score and current floor; the comment must not be treated as a permanent model ranking.

Hugging Face is a strict fallback, not a model-card source: the reconciler requests `evalResults` and
accepts only explicitly `verified` results for a narrow, direction-safe task allow-list shared with
Gemma-4. It does not read unstructured publisher claims or unverified results. Small reviewed
organization aliases (currently NVIDIA's `z-ai` identifier → Hub's `zai-org`) resolve publisher
naming differences without fuzzy repository matching.

| Provider | Availability source | Free evidence | Automatic action |
|---|---|---|---|
| Airforce | authenticated `/v1/models` | authoritative `tier: free` (not broad `access_tiers`) | add only after independent quality evidence and canary, when the response supplies the needed route limits |
| NVIDIA | authenticated `/v1/models` | matching Build Catalog `Free Endpoint` label | add only after independent quality evidence and canary; no matching label is not free, while scraper failure is inconclusive |
| Groq | authenticated `/models` | maintainer-confirmed free account: every exposed model is free-route eligible | add only after independent quality evidence and canary with conservative catalog-derived limits |
| Mistral | authenticated `/v1/models` | Free Mode is an account/quota policy; only `completion_chat` entries are eligible | deduplicate provider billing aliases, then add only after independent quality evidence and a bounded canary; a zero-provisioned quota header halts admission |
| OpenRouter, Kilo | authenticated `/models` | literal `:free` identifier plus explicit zero prompt and completion pricing | add only after independent quality evidence and canary |
| OrcaRouter | authenticated `/models` | `-free` identifier, `(Free)` display label, and zero request pricing | add only after independent quality evidence and canary |
| Gemini | authenticated model list | Google documents Free Tier access for only certain models; the list has no tier marker | inspect a newly discovered route with the bounded rate-limit probe; do not auto-add from catalog visibility alone |
| SambaNova | authenticated `/models` | account-level Free plan is distinct from its nonzero model pricing | no automatic additions without an account-credit/free-entitlement signal |
| z.ai | authenticated `/models` | account-wide free-plan assertion has no per-model marker | no automatic additions; retain existing routes unless definitely missing and ticket any entitlement ambiguity |
| SiliconFlow, DeepSeek | authenticated `/models` | no GLOBAL SiliconFlow free marker; DeepSeek is paid | no additions; report any configured inconsistency |

The NVIDIA metadata scraper is deliberately treated as a scraper, not an API contract: NVIDIA does
not publish a stable documented Build Catalog JSON API with the `Free Endpoint` property. Candidates
come exclusively from authenticated `/models`; once per run the reconciler reads Build Catalog's
public NGC `search/catalog/resources/ENDPOINT` response for its `Free Endpoint` label and matches
the publisher/model identifier exactly. After a model passes that quality gate, its matching NGC
endpoint detail may supply a published context limit when `/models` did not. Its transport, HTTP,
or schema failure therefore creates an issue rather than removing or adding a route.

For an existing route absent from its catalog, the reconciler sends one fixed completion canary.
Only an explicit invalid-model/not-found response removes that route. A payment, subscription,
quota, timeout, or server response is inconclusive and goes to the rolling issue. This prevents an
empty credit balance from being mistaken for a model retirement. NVIDIA's one-off admission canary
has a 90-second bound rather than the normal 30 seconds, because a slow but successful first
completion is evidence of availability, not model retirement.

## PR-safe mutation and fallback

The script changes `config/provider_limits.yml` only in `--apply` mode. It makes a targeted YAML
route-block edit rather than round-tripping the whole document through PyYAML, preserving the
curated explanatory comments. It then runs the existing compiler, so the PR contains all three
generated dispatch artifacts as well as a Markdown evidence report.

The successful catalog plan is content-addressed. A digest-named branch is reused only for the
same planned changes; a new plan gets a new branch and never force-pushes an unrelated review.
The script uses `gh` to create/reuse a PR only when `--open-pr` and `GH_TOKEN` are present.

If a removal leaves a logical model with another verified, free physical route, that route remains
the exact-model backfill automatically. If no exact logical route remains, the reconciler only
uses an already reviewed `model_routing` target (or lane `backup_models` for failure fallback).
It never selects a different model by fuzzy name, family, context size, or price. Missing explicit
fallback is included in the rolling issue for a maintainer decision.

## GitHub issue lifecycle

One issue titled **Provider catalog reconciliation — inconclusive free-route evidence** is deduped
by a hidden marker. The body is replaced with the current outstanding provider/model findings; it
is updated rather than duplicated and closed only when no inconclusive finding remains. It has
`type:operations`, `area:provider`, `signal:endpoint-contract`, and
`needs:human-verification` labels. Dry runs render the would-be issue action without calling
GitHub.

## Workflow and secrets

`provider-catalog-reconcile.yml` runs weekly and on manual dispatch. It has only `contents: write`,
`pull-requests: write`, and `issues: write`, checks out without persisted credentials, and passes
provider keys plus `ARTIFICIAL_ANALYSIS_API_KEY` only as step environment variables. The latter is
the free API key, subject to its 1,000-request-per-day limit and attribution requirement; the script
makes one request per run and attributes the source in its report. The report excludes Authorization
headers, keys, and response bodies longer than a short sanitized error summary. A manual provider
filter allows focused recovery without widening the scheduled scope.

## Tests and acceptance criteria

Offline tests fake catalog and canary clients and cover:

1. provider free evidence, an exact independent Artificial Analysis comparison (or verified Hub
   fallback), and canary all require success before an addition; missing evidence becomes an issue
   finding;
2. explicit missing-model response removes exactly that route block, while payment/quota preserves
   it and creates a finding;
3. a last-route removal reports a missing fallback, while an exact sibling route is retained;
4. targeted source edits preserve unrelated comments and compile generated catalogs;
5. issue/PR commands are skipped in dry-run mode and digest branches reuse only matching work.

The workflow is accepted when a manual dry run publishes a redacted report; an apply run with a
safe fixture plan opens a review PR and updates the rolling issue without merge/deploy authority.
