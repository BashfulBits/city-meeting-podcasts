# review/55 — Adaptive input-token admission (estimator, ceilings, size-rejection policy)

Status: **in progress** (2026-10-17). Four stacked PRs, shipped in this order: piece 4, piece 3,
piece 2, piece 1. Each PR updates the status line of its piece below.

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

### 3. Learned per-route ceilings from outcomes — *planned (next PR)*

Per route, in provider-token units, keep the largest input that succeeded (`accepted_max`, from
`usage.prompt_tokens`) and the smallest that was rejected for size (`rejected_min`, from the error's
own numbers when present, else the scaled estimate). Admission: attempt at or below `accepted_max`,
skip at or above `rejected_min`, and use the percentile rule in between. Entries decay (~7 days) so
provider changes are re-tested. This is observed provider behavior and replaces guessing a ceiling
from a statistical ratio. Today only `free_prompt_cap` writes anything like it
(`routes.prompt_cap_estimate`, never decays, rejected side only).

### 2. Two percentiles for two purposes — *planned*

- **Packing** uses a safe tail (p95-p99). The Worker publishes a per-model packing ceiling
  (smallest route limit minus output reserve, divided by the ratio) through `/v2/calibration`;
  Python reads it at run start instead of using hard-coded packet ceilings.
- **Admission** uses a lower quantile (about p75): a wrong guess costs one cheap rejected call
  (pieces 3/4 make that safe), a wrong rejection strands the job on an idle route.

### 1. Estimator — *planned*

Key by (tokenizer family, prompt family) and pool across routes serving the same model. Window 128,
percentiles only from 64 samples; below that the prior counts as pseudo-samples that measured data
replaces (the prior becomes a seed, not a floor). Same treatment for the output reserve. Optional
size bands.

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
