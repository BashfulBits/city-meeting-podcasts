/**
 * Route capacity and pacing math for LLM Dispatch v2 (review/44 Unit 4).
 *
 * Pure functions over a `routes` ledger row (routes table) plus a route's static catalog limits
 * (rpm/rpd/tpm from dispatch_limits.json) -- unit-testable without a DO transaction around them,
 * per Unit 4's explicit instruction. Nothing here reads or writes SQLite; callers
 * (coordinator.js) pass in the current ledger row and persist whatever these functions decide.
 *
 * Rate model, one fixed-window counter per limit (RPM, RPD), reset when the window has fully
 * elapsed since it started -- not a true sliding log, which would need one row per request; a
 * fixed window is the standard cheap approximation and is only ever conservative in the direction
 * of blocking slightly early at a window boundary, never in the direction of over-admitting.
 *
 * TPM is a token *bucket*, not a third fixed window: `full_token_budget` refills continuously at
 * `tpm` tokens per 60s, capped at `tpm * FULL_TOKEN_BUDGET_WINDOWS` (a several-window ceiling,
 * not just one window's worth) specifically so a job whose conservative estimate exceeds one
 * window's entire tpm allowance is not automatically unserviceable -- it waits for enough budget
 * to accumulate across several windows, up to that ceiling. A job whose estimate exceeds the
 * ceiling itself can never be admitted on this route at all; routeHasCapacityFor returns false for
 * it permanently, not just "not yet."
 */

const MS_PER_MINUTE = 60_000;
const MS_PER_DAY = 24 * 60 * 60_000;
export const FULL_TOKEN_BUDGET_WINDOWS = 5;

/** ceil(60000 / rpm): the minimum spacing between two requests on the same route, evenly paced
 * so that no sliding 60s window can ever see more than `rpm` requests, regardless of how the
 * fixed-window counter below happens to be aligned. */
export function minInterRequestGapMs(route) {
  const rpm = Number(route?.rpm);
  if (!Number.isFinite(rpm) || rpm <= 0) return 0;
  return Math.ceil(MS_PER_MINUTE / rpm);
}

function fixedWindowReadyAt(windowStart, count, limit, windowMs, now) {
  if (!Number.isFinite(limit) || limit <= 0) return now; // unlimited / unconfigured
  if (!Number.isFinite(windowStart) || now - windowStart >= windowMs) return now; // stale/fresh window
  if (count < limit) return now; // headroom in the current window
  return windowStart + windowMs; // window full; wait for the next one
}

/** Available tokens in the refillable bucket right now, without mutating anything. */
export function availableTokenBudget(route, now) {
  const tpm = Number(route?.tpm);
  if (!Number.isFinite(tpm) || tpm <= 0) return Number.POSITIVE_INFINITY; // unlimited / unconfigured
  const cap = tpm * FULL_TOKEN_BUDGET_WINDOWS;
  const updatedAt = Number(route?.token_budget_updated_at) || 0;
  const elapsedMs = Math.max(0, now - updatedAt);
  const refilled = (Number(route?.full_token_budget) || 0) + (elapsedMs * tpm) / MS_PER_MINUTE;
  return Math.min(cap, refilled);
}

/**
 * The token reservation this job must carry on this route: never lower than the client's own
 * conservative estimate, the configured floor, or the calibrated per-route/model/prompt-family
 * margin (looked up by the caller, since that's a DB read) -- see Unit 4's exact wording.
 */
export function reservationFor(job, { estimateFloor = 0, calibratedMargin = 0 } = {}) {
  const clientEstimate = (job.input_token_estimate || 0) + (job.max_output_token_estimate || 0);
  return Math.max(clientEstimate, estimateFloor, calibratedMargin);
}

/**
 * The earliest time (ms epoch) this job could safely start on this route, honoring every
 * ledger constraint, not before `earliestCandidateTime` (the lane's own cumulative time, or `now`
 * for a first-pass eligibility check). Returns null if the job's reservation permanently exceeds
 * what this route's token bucket could ever hold (terminally unserviceable here, not just "not
 * yet" -- see the module docstring).
 */
export function earliestSafeStart(route, job, earliestCandidateTime, now, options = {}) {
  const reservation = reservationFor(job, options);
  const tpm = Number(route?.tpm);
  if (Number.isFinite(tpm) && tpm > 0 && reservation > tpm * FULL_TOKEN_BUDGET_WINDOWS) {
    return null; // exceeds the route's burst/request capacity outright -- not a timing problem
  }

  let notBeforeAt = Math.max(earliestCandidateTime, now);

  // RPM: fixed 60s window.
  notBeforeAt = Math.max(
    notBeforeAt,
    fixedWindowReadyAt(route?.rpm_window_start, route?.rpm_count, route?.rpm, MS_PER_MINUTE, now)
  );

  // RPD: fixed 24h window.
  notBeforeAt = Math.max(
    notBeforeAt,
    fixedWindowReadyAt(route?.rpd_window_start, route?.rpd_count, route?.rpd, MS_PER_DAY, now)
  );

  // TPM: refillable token bucket. Reservations at or under one window's tpm allowance are also
  // gated by the ordinary rolling reservation (tpm_reserved/tpm_window_start) so a burst of
  // small jobs can't collectively exceed one window even while the bucket itself has headroom
  // from a quiet stretch beforehand.
  if (Number.isFinite(tpm) && tpm > 0) {
    const windowReserved = Number(route?.tpm_reserved) || 0;
    const windowStale =
      !Number.isFinite(route?.tpm_window_start) || now - route.tpm_window_start >= MS_PER_MINUTE;
    const effectiveWindowReserved = windowStale ? 0 : windowReserved;
    if (reservation <= tpm && effectiveWindowReserved + reservation > tpm) {
      notBeforeAt = Math.max(notBeforeAt, (windowStale ? now : route.tpm_window_start) + MS_PER_MINUTE);
    }

    const availableAtNotBefore =
      availableTokenBudget(route, now) + Math.max(0, notBeforeAt - now) * (tpm / MS_PER_MINUTE);
    if (availableAtNotBefore < reservation) {
      const deficit = reservation - availableTokenBudget(route, now);
      const refillMs = Math.ceil((deficit * MS_PER_MINUTE) / tpm);
      notBeforeAt = Math.max(notBeforeAt, now + Math.max(0, refillMs));
    }
  }

  // Route buffer: an additive delay accrued from repeated 429s (decays on success elsewhere).
  const bufferMs = Math.max(0, Number(route?.buffer_seconds) || 0) * 1000;
  if (bufferMs > 0) notBeforeAt = Math.max(notBeforeAt, now + bufferMs);

  // Explicit block from a severe/repeated throttle.
  if (Number.isFinite(route?.blocked_until) && route.blocked_until > notBeforeAt) {
    notBeforeAt = route.blocked_until;
  }

  return { notBeforeAt, reservation };
}

/**
 * Eligibility check used while filling admission passes 1/2 -- does this job have ANY safe start
 * inside the dispatch window on this route? `callDurationCeilingMs` is a conservative upper bound
 * on how long the provider call itself might take, so a job whose safe start is technically
 * before the window's end but would still plausibly be running past it is excluded up front
 * rather than discovered as deferred_late only after being chosen.
 */
export function routeHasCapacityFor(route, job, now, windowSeconds, options = {}) {
  const result = earliestSafeStart(route, job, now, now, options);
  if (result === null) return false;
  const callDurationCeilingMs = Math.max(0, options.callDurationCeilingMs || 0);
  return result.notBeforeAt + callDurationCeilingMs <= now + windowSeconds * 1000;
}

/**
 * The full per-job lane-sequencing result used once a job has actually been chosen (Unit 4 step
 * 6): wait_ms is relative to `now` at claim time -- the executor (Unit 6) re-applies it relative
 * to its own RPC-response-receipt instant, never assuming claim time and receipt are the same
 * moment.
 */
export function computeRouteLaneWait(route, job, laneTime, now, options = {}) {
  const result = earliestSafeStart(route, job, laneTime, now, options);
  if (result === null) return null;
  return {
    wait_ms: Math.max(0, result.notBeforeAt - now),
    not_before_at: result.notBeforeAt,
    reservation: result.reservation,
    min_inter_request_gap_ms: minInterRequestGapMs(route),
  };
}

/**
 * Escalating cooldown for a route whose most recent attempt came back HTTP 402 (payment
 * required / provider-side quota exhausted at the billing layer) -- a distinct signal from 429:
 * 429 means "too fast," handled above by buffer_seconds/throttle_streak; 402 means "no budget
 * left until some future reset," which no amount of pacing fixes. Forces the route fully
 * unavailable via `blocked_until` (the same "explicit block from a severe/repeated throttle"
 * mechanism earliestSafeStart already applies above) until whichever calendar boundary the
 * current streak has earned: first occurrence assumes a same-day quota reset might simply fix
 * it, escalating through a week and then a month for a streak that keeps recurring after each
 * cooldown expires -- i.e., a real billing problem, not a transient blip -- and stays pinned to
 * "start of next month" for any further streak past that, rather than escalating indefinitely.
 *
 * `streak` is the count AFTER this occurrence (the caller increments before calling this); a
 * success resets it back to 0 elsewhere (coordinator.js), so streak only ever grows across
 * consecutive 402s with no successful call between them.
 */
export function paymentRequiredBackoffUntil(streak, now) {
  const d = new Date(now);
  if (streak <= 1) {
    return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate() + 1);
  }
  if (streak === 2) {
    // Next UTC Monday, at least 1 and at most 7 days out (today itself included, so a streak
    // reached exactly on a Monday still gets a full week, not zero).
    const daysUntilNextMonday = ((8 - d.getUTCDay()) % 7) || 7;
    return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate() + daysUntilNextMonday);
  }
  return Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 1);
}
