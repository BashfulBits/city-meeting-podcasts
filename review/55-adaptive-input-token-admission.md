# review/55 — Adaptive input-token admission (estimator, ceilings, size-rejection policy)

Status: **implemented** (2026-10-17) as four stacked PRs, in this order: piece 4, piece 3, piece 2,
piece 1. Merge in that order; each PR is based on the previous one.

## Why

The Worker admits a job to a route using one number, the *input ratio* (provider tokens per
chars/4 estimate), and one config number, `hard_input_ceiling`. That single ratio is asked to do
three different jobs, and does none of them well:

1. **Packing target** — Python packs packets so they fit the smallest route of a model.
2. **Admission bound** — the Worker decides whether a job is worth trying on a route, or should
   overflow to a roomier route of the same model.
3. **Recovery** — a job that a route refuses for size should be retried on another route.

What the code did on 2026-10-16/17 (verified by reading it):

- The ratio is `max(route prior, p95 x headroom)`: measured evidence can raise it, never lower it.
  The AI Studio Gemma prior is 1.4 (and headroom 1.2) although the judge prompts measure p95 ~1.08.
  A 10,000-token judge packet therefore reserved ~15,900 tokens against a 14,400-token minute
  budget and could never be admitted; ~2,600 packets waited on two idle keys.
- The window is 32 samples with a 16-sample minimum, so p95 is about the second-largest sample.
- Calibration is already keyed per (route, model, prompt family).
- A provider "input too long" answer (`route_input_limit`) stood the *route* down with the 5xx
  backoff (hurting every smaller job), spent the single shared 5xx retry, and left no record on the
  job of which route had refused it. Single-model pinned jobs simply failed to their producer.
- A 413/400 saying one request exceeded the whole tokens-per-minute quota was classified `own_tpm`
  (pacing) although waiting can never admit it.
- Packet size ceilings were hard-coded in Python (`SIBLING_CEILINGS`), disconnected from what the
  Worker would admit.

## The four pieces

### 4. Response policy for size rejections — *shipped in the first PR*

- `jobs.size_excluded_routes` (JSON array) records routes that rejected the job for size.
  `routesEligibleFor` skips them for that job only.
- A size rejection requeues the job straight onto a sibling route of the same model. It does not
  spend the transient (5xx) retry and does not stand the route down.
- When no route of the model is left, the job fails with `terminal_reason: input_too_large` and the
  producer re-packs it smaller.
- `classify.js`: "Limit L, Requested R" with R > L and no "Used" is `route_input_limit`
  (`size-status-request-exceeds-tpm`); with "Used" the bucket was merely full and stays `own_tpm`.

### 3. Learned per-route ceilings from outcomes — *shipped in the second PR*

Per route, in provider-token units, keep the largest input that succeeded (`routes.input_accepted_max`,
from `usage.prompt_tokens`; raise-only, re-seeded after half the TTL) and the smallest that was
rejected for size (`routes.input_rejected_min`: the job's scaled estimate, since the completion
result carries only the failure class, not the provider's message). Admission: attempt at or below `accepted_max`,
skip at or above `rejected_min`, and use the percentile rule in between. Entries decay (7 days) so
provider changes are re-tested; the claim's drain pass (only when nothing else is dispatchable)
waives the refusal, so a refusal can never strand a job; contradictory entries (accepted >=
rejected) resolve to the more recent observation. An accepted maximum above the configured
`hard_input_ceiling` lifts that conservative ceiling for jobs up to it. `/v2/stats?routes=1` shows
both numbers (0 = none/expired). Rejections are deliberately not used for the terminal
"structurally unroutable" decision, only for admission ordering. This is observed provider behavior and replaces guessing a ceiling
from a statistical ratio. Today only `free_prompt_cap` writes anything like it
(`routes.prompt_cap_estimate`, never decays, rejected side only).

### 2. Two percentiles for two purposes — *shipped in the third PR*

- **Packing** uses a safe tail: the p95 ratio x 1.05 (`packRatio`). `GET /v2/calibration?model=&prompt_family=`
  returns `pack_ceiling_input_estimate`: the smallest free route's tightest size limit (configured
  ceiling, a learned refusal, the context window) over that ratio, with the per-route detail. The
  judge reads it at run start and sizes sibling packets to 90% of it, up to 1.5x
  the configured ceiling (which stays the safe default when the Worker is unreadable).
- **Admission** uses a lower quantile: p75 x 1.05 (`admitRatio`), not floored by the route prior
  once 16 samples exist, never above the reservation ratio. It drives only the size gates (configured
  ceiling, learned bounds, the Gemini per-minute input quota, and the structural "exceeds every
  route" check). Reservation, the rolling input window and TPM accounting keep the conservative
  ratio. A wrong guess costs one cheap refused call (pieces 3/4 make that safe); a wrong rejection
  strands the job on an idle route.
- Correction to the 2026-10-16 diagnosis: Gemini-provider reservations count input only, so the
  AI Studio limit was the size gate (estimate x 1.4 against a 14,400 ceiling, i.e. 10,285 raw), and
  the producer's 10,000 target excluded the system prompt and framing.

### 1. Estimator — *shipped in the fourth PR*

- **Pooled** by (tokenizer family, prompt family): the `estimates` key is
  `pool:<tokenizer family>:<prompt family>` where the tokenizer family is the route's
  `tokenizer_family` if the catalog names one, else its primary model, so all five routes serving
  Gemma 4 31B share one ring per prompt family (previously one ring per route). Until a pooled ring
  has 64 samples, the route's older per-route ring is used if it has more.
- **Window 128**, ratios and output sizes. Below 16 samples only the prior; from 16 to 63 the
  learned tail is blended in with a linearly shrinking weight on the prior (`1 - n/64`); from 64
  the learned percentiles stand alone -- the prior is a seed worth ~64 pseudo-samples, **not a
  floor** any more. Reservation = the safe tail (`max(p95 x headroom, p99)`), admission = p75 x
  1.05, packing = p95 x 1.05, each blended the same way.
- **Output reserve:** from 16 to 63 outputs the largest stands in for the p95; from 64 a real p95
  x 1.25.
- `GET /v2/stats?detail=1` lists pooled cells (`route_id: null`, `model` = tokenizer family) with
  the admission and packing ratios.
- Not done: size bands within a prompt family (a later refinement if the pooled per-family tail is
  too loose for large packets).
- Watch after merge: `own_tpm` 429 rates and `route_input_limit` counts per route in the budget
  monitor; reservations on routes whose full-window learned ratio is below their prior drop, which
  is the point, but is the one behavior change that touches every route. Revert is a single PR.

## One size gate (follow-up, 2026-10-18)

Pieces 1 to 4 changed the pacing check (`earliestSafeStart`) but left the claim's coarse eligibility
filter (`routesEligibleFor` -> `routeFitsContext`) on the catalog's configured ratio. A packet over
about 10,285 estimated tokens (14,400 / 1.4) was therefore dropped from the AI Studio routes before
the learned check ever ran, although their calibrated limit was 13,605 and they had completed 10,281.
Both now call `inputSizeRefusal` (calibration.js): the admission ratio against the larger of the
configured ceiling and the largest size the route has accepted, and a learned refusal skipped only
by the strict check (the filter and the drain pass are tolerant). The claim passes the filter the
same calibrated ratios and the route merged with its ledger; callers with no live data (enqueue-time
indexing, the structural unroutable checks) keep the catalog values, and the oversize check
(`_exceedsEveryLiveCeiling`) filters routes by context window alone and applies the learned gate.

## Token-window margin (follow-up, 2026-10-19)

After the one size gate, `gemma_4_31b_*` (judge:sibling) showed ~48 `own_tpm` 429s/hour against ~22
completions/hour (about 71% of attempts refused; ~5.5 failures and ~6.5 completions/hour before).
The learned ratio is not the cause: the pooled judge ring has 128 samples, p95 1.002, so the
reservation ratio (1.2024) and admission ratio (0.9975) over-count Google's own tokens, and Gemini
reservations carry no output. What changed is the packet size: a ~10-11.5k-token packet (largest
accepted 11,514) fills the 14,400 ledger minute alone, so the ledger releases the next one exactly
60.000s after the last SCHEDULED start (claim clock + wait). Google counts at ARRIVAL, which trails
the schedule by a variable delay (claim-to-receipt, B2 payload read, gateway), so about half of
those pairs reach Google less than 60s apart and 11k + 11k exceeds 16k. The `own_tpm` cooldown
(`tpm_window_start = now`) is the same 60s and re-offers the route at the same boundary.

`tpm_window_margin_seconds` (pacing.js `tpmWindowMarginMs`) lengthens the token window for
the input-minute ledger and the fixed `tpm_window` (and thereby the `own_tpm` cooldown) by the
margin; RPM spacing is untouched. Set to 15 on the two routes; remove the key to revert. Every
Google `own_tpm` now logs `own_tpm_observed` (no DO rows) so the margin can be sized from data.
Per-attempt rows are not stored (route_failures is a per-day counter), which is why the
reconstruction above is inferred from the ledger arithmetic, not measured per call.

## Quota recovery and pending input reservations (maintainer-directed, 2026-10-19)

Implementation follow-up under validation; production verification remains pending deployment.
The maintainer approved these three fixes after investigating the exported Google failures and
confirmed the primary and secondary keys belong to distinct Google projects.

**Correction to the margin diagnosis above:** scheduled-start jitter was inferred, not measured.
The export covers 2026-10-18 21:30 through 2026-10-19 20:43 UTC: Google's Gemma 31B has 835 HTTP
429s in 1,410 attempts (59.2%), versus 6 in 414 (1.4%) for 26B. The latest partial hour still has
30 refusals and 18 successes for 31B. Successful 31B inputs reach 13,559 tokens; failed records
contain no prompt, quota error body, or project identifier. These observations do not distinguish
oversized requests, timing overlap, and quota consumption elsewhere. Do not claim jitter is proven.

- **Cooldown:** `authorizeRetry` applies `own_tpm` feedback even after the job's retry allowance is
  spent. `blocked_until` is the greater of the existing deadline, the local input-window span,
  and the provider's Retry-After/RetryInfo delay plus one second. Invalid hints use the local
  window. A Google success already in flight preserves an unexpired cooldown.
- **Size:** the 429 token-quota classifier checks explicit `Requested > Limit` before returning
  `own_tpm`, using the same numeric comparison as 400/413. A limit alone is insufficient evidence.
  `route_input_limit` does not alter rate/backoff state in `authorizeRetry`; completion uses the
  existing per-job route exclusion, sibling routing, and terminal `input_too_large` repacking path.
- **Timing:** Google pending entries in the existing `input_window_json` carry their lease token,
  scheduled start, and a release clock initially set to the lease expiry. They stay charged while
  B2 reads, gateway transit/retries and provider generation are outstanding. Completion replaces
  only that lease's entry with actual input at response end (the conservative arrival upper bound),
  retaining the estimated amount if usage is unknown. Non-consuming and unstarted work releases
  its own entry. Lost executors release after lease expiry plus the token-window span; stale or
  duplicate completions cannot settle a newer lease. Small jobs may still share the window.

This is the conservative completion-based accounting option: slow responses reduce throughput;
we are not asserting Google actually bills tokens at response end. No new endpoint, RPC, table,
dependency, production quota knob, or pipeline-version bump is needed. Settlement rides existing
route writes; the failure path adds one route-keyed read. Existing untagged entries are supported
and simply age out normally; old in-flight claims retain their original scheduled-time accounting
until settled, so assess the rollout only after pre-deploy leases drain. Stored model results and
queued payloads are unchanged; no catalog reprocessing or artifact backfill is triggered.

Regression coverage includes explicit/unknown request sizes on 429, delayed starts beyond the
15-second margin, eviction, lost-executor expiry, measured error replies, unknown usage,
non-consuming/unstarted work, duplicate/stale settlement, quota feedback after retry exhaustion,
and preservation of a provider cooldown when an in-flight sibling succeeds. Production validation
should compare per-route refusal/completion rates and `own_tpm_observed` pending-call counts after
deploy; the exported gateway summaries alone cannot establish a root cause or confirm improvement.
CLI CodeRabbit review found two valid edge cases, both fixed with regressions: success preserves
only an active `own_tpm` block (and its class across repeated successes), and comma-separated quota
numbers must begin with a digit so stray punctuation cannot hide a later valid limit.

## Additional goals

Per-key observability (n, p50/p95/p99, size rejections, near-edge admissions) in `/v2/calibration`
and the budget monitor; drift detection; winsorize outliers; an offline replay of stored
(estimate, observed) pairs before changing an admission rule.

## Relation to provider-limit characterization

[review/54](54-provider-limit-characterization.md) and the deferred issue #2264 plan recurring
*probe-based* measurement of configured routes' input limits. The learned ceilings of piece 3 are
free, continuous and measured on the real traffic and tokenizer, so any future probe design should
treat them as the primary evidence (and a probe as a way to seed or cross-check them), not the
other way round.

## Rollout order and safety

4 first (the safety net), then 3, then 2 (optimistic admission is only safe once a refused job
recovers on its own), then 1. No piece changes Python packing until piece 2.
