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
