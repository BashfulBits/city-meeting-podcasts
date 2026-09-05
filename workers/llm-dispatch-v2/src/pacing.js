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

/**
 * Optional, provider-specific addition to the normal RPM spacing. It is a request-start safety
 * margin for providers whose clocks can reject an exactly-on-the-minute request; it is not a
 * response-time delay.
 */
export function requestStartMarginMs(route) {
  const seconds = Number(route?.request_start_margin_seconds);
  if (!Number.isFinite(seconds) || seconds <= 0) return 0;
  return Math.ceil(seconds * 1000);
}

/** Length of the route's RPM accounting window, including any start-time safety margin. */
export function rpmWindowDurationMs(route) {
  return MS_PER_MINUTE + requestStartMarginMs(route);
}

/** ceil(60000 / rpm), plus any configured start margin: the minimum spacing between two
 * requests on the same route, evenly paced so that no sliding 60s window can ever see more than
 * `rpm` requests, regardless of how the fixed-window counter below happens to be aligned. */
export function minInterRequestGapMs(route) {
  const rpm = Number(route?.rpm);
  if (!Number.isFinite(rpm) || rpm <= 0) return 0;
  return Math.ceil(MS_PER_MINUTE / rpm) + requestStartMarginMs(route);
}

/**
 * Calendar date in a provider's own reset timezone, as `YYYY-MM-DD`.
 *
 * A daily quota resets on the provider's clock, not 24 hours after we first used it. Gemini's free
 * tier rolls at midnight America/Los_Angeles; treating it as a rolling 24h window anchored on our
 * first request holds a route exhausted for up to a further ~14 hours after the provider has
 * already refilled it. v1 (`llm-dispatch-proxy`'s zonedDateKey/routeResetTimezone) has always keyed
 * on this; v2 shipped without it, which is the divergence this restores. Duplicated rather than
 * shared, per this repo's convention of each Worker directory being self-contained.
 */
export function zonedDateKey(ms, timeZone = "UTC") {
  const date = new Date(ms);
  try {
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(date);
    const y = parts.find((p) => p.type === "year")?.value;
    const m = parts.find((p) => p.type === "month")?.value;
    const d = parts.find((p) => p.type === "day")?.value;
    if (y && m && d) return `${y}-${m}-${d}`;
  } catch {
    // Invalid timezone string -- fall through to UTC rather than throwing inside the scheduler.
  }
  return date.toISOString().slice(0, 10);
}

/** The provider's reset timezone for a route, defaulting to UTC. */
export function routeResetTimezone(route) {
  return route?.reset_timezone || "UTC";
}

/**
 * When a route's daily quota next allows a request, keyed on the provider's calendar day.
 *
 * A stale day key means the provider has already rolled over, so the route is ready now.
 *
 * `limit <= 0` returns `now`, matching the fixedWindowReadyAt behaviour this replaces. Note that
 * `_capacityFraction` reads `rpd: 0` as the repository's "paused" convention and scores it 0, so
 * the two disagree -- that predates this change and is deliberately left alone rather than
 * quietly altered while fixing timezones. In practice a paused route is dropped from the ranking
 * before pacing is consulted.
 */
export function dailyQuotaReadyAt(route, now) {
  const limit = Number(route?.rpd);
  if (!Number.isFinite(limit) || limit <= 0) return now;
  const tz = routeResetTimezone(route);
  if (route?.rpd_day_key !== zonedDateKey(now, tz)) return now; // provider already reset
  if ((Number(route?.rpd_count) || 0) < limit) return now;
  return nextZonedMidnightMs(now, tz);
}

/** Epoch ms of the next midnight in `timeZone`, found by probing forward one hour at a time. */
export function nextZonedMidnightMs(now, timeZone = "UTC") {
  const today = zonedDateKey(now, timeZone);
  // A day is at most 25 hours across a DST fall-back, so 26 probes always cross the boundary.
  for (let hour = 1; hour <= 26; hour += 1) {
    const probe = now + hour * 3_600_000;
    if (zonedDateKey(probe, timeZone) !== today) {
      // Narrow to the minute so the route is not held for up to an extra hour.
      let lo = probe - 3_600_000;
      let hi = probe;
      while (hi - lo > 60_000) {
        const mid = lo + Math.floor((hi - lo) / 2);
        if (zonedDateKey(mid, timeZone) === today) lo = mid;
        else hi = mid;
      }
      return hi;
    }
  }
  return now + MS_PER_DAY;
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
 * A route's 429 buffer, decayed for the time elapsed since it was last raised.
 *
 * `buffer_seconds` is a backoff, so it has to be able to expire on its own. Stored as a bare
 * number and compared straight against the dispatch window, it was a permanent kill switch
 * instead: _capacityFraction scores a route 0 once buffer_seconds >= DISPATCH_WINDOW_SECONDS
 * (25), and a single 429 adds MAX_429_BACKOFF_SECONDS (60) at once. The only code that ever
 * cleared it ran on a *successful* completeBatch for that route -- unreachable, because a
 * zero-capacity route is never ranked, so never claimed, so never succeeds. One 429 therefore
 * removed a route from v2's ranking forever, with no blocked_until and no error to show for it.
 * Observed 2026-08-30: 15,833 queued jobs, every route silently at zero, zero bundles claimed.
 *
 * Decaying at one second per second gives the backoff its intended meaning -- a 60-second buffer
 * stops gating the route after ~35 seconds (when it falls under the 25-second window) and is
 * fully spent after 60 -- and makes recovery unattended.
 */
export function effectiveBufferSeconds(route, now) {
  const stored = Number(route?.buffer_seconds);
  if (!Number.isFinite(stored) || stored <= 0) return 0;
  const updatedAt = Number(route?.buffer_updated_at) || 0;
  // A row written before buffer_updated_at existed has 0 here; treating that as "fully elapsed"
  // is the right migration, since those are exactly the routes stuck from the old behaviour.
  if (updatedAt <= 0) return 0;
  const elapsedSeconds = Math.max(0, (now - updatedAt) / 1000);
  return Math.max(0, stored - elapsedSeconds);
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
  // A SEPARATE, tighter, opt-in ceiling from the bucket math above. `FULL_TOKEN_BUDGET_WINDOWS`
  // assumes every route can burst up to several windows deep -- true for at least one provider
  // confirmed live (NVIDIA accepted a request ~3x its configured tpm outright), but not for
  // Gemini/Gemma: confirmed live, a single request over its real usable window is rejected
  // outright by the provider no matter how idle the account is, and that real ceiling can sit
  // well below both `tpm` and the model's own context window. `route.hard_input_ceiling` is only
  // ever set (compile_llm_limits.py) where that hard-reject behavior has actually been verified,
  // so this never over-restricts a route we haven't tested.
  const hardCeiling = Number(route?.hard_input_ceiling);
  if (Number.isFinite(hardCeiling) && hardCeiling > 0 && reservation > hardCeiling) {
    return null;
  }

  let notBeforeAt = Math.max(earliestCandidateTime, now);

  // RPM: a one-minute accounting window, with an optional provider-specific start-time margin.
  notBeforeAt = Math.max(
    notBeforeAt,
    fixedWindowReadyAt(
      route?.rpm_window_start,
      route?.rpm_count,
      route?.rpm,
      rpmWindowDurationMs(route),
      now
    )
  );

  // RPD: the provider's calendar day in its own reset timezone, not a rolling 24h window.
  notBeforeAt = Math.max(notBeforeAt, dailyQuotaReadyAt(route, now));

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

  // Provider-level TPM: shared refillable token bucket across all routes for this provider.
  const providerTpm = Number(options?.providerConfig?.tpm ?? route?.provider_tpm);
  const providerLedger = options?.providerLedger;
  if (Number.isFinite(providerTpm) && providerTpm > 0 && providerLedger) {
    if (reservation > providerTpm * FULL_TOKEN_BUDGET_WINDOWS) {
      return null;
    }
    const pWindowReserved = Number(providerLedger?.tpm_reserved) || 0;
    const pWindowStale =
      !Number.isFinite(providerLedger?.tpm_window_start) ||
      now - providerLedger.tpm_window_start >= MS_PER_MINUTE;
    const pEffectiveWindowReserved = pWindowStale ? 0 : pWindowReserved;
    if (reservation <= providerTpm && pEffectiveWindowReserved + reservation > providerTpm) {
      const pStart = pWindowStale ? now : providerLedger.tpm_window_start;
      notBeforeAt = Math.max(notBeforeAt, pStart + MS_PER_MINUTE);
    }

    const pCap = providerTpm * FULL_TOKEN_BUDGET_WINDOWS;
    const pUpdatedAt = Number(providerLedger?.token_budget_updated_at) || 0;
    const pElapsedMs = Math.max(0, now - pUpdatedAt);
    const pRefilled =
      (Number(providerLedger?.full_token_budget) || 0) + (pElapsedMs * providerTpm) / MS_PER_MINUTE;
    const pAvailableNow = Math.min(pCap, pRefilled);
    const pAvailableAtNotBefore =
      pAvailableNow + Math.max(0, notBeforeAt - now) * (providerTpm / MS_PER_MINUTE);
    if (pAvailableAtNotBefore < reservation) {
      const pDeficit = reservation - pAvailableNow;
      const pRefillMs = Math.ceil((pDeficit * MS_PER_MINUTE) / providerTpm);
      notBeforeAt = Math.max(notBeforeAt, now + Math.max(0, pRefillMs));
    }
  }

  // Route buffer: an additive delay accrued from repeated 429s. Uses what is still *owed* rather
  // than the stored figure -- the stored one never shrinks, so this pushed every future dispatch
  // a full buffer beyond `now` forever, which is the same permanent stall as the capacity check
  // and had to be fixed in both places to matter (see effectiveBufferSeconds).
  const bufferMs = effectiveBufferSeconds(route, now) * 1000;
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
  const dayOffset = (n) => Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate() + n);
  // Intended shape: a day, then a week, then "start of next month" for anything beyond. The raw
  // calendar values are NOT inherently ordered, so they are combined into a strictly increasing
  // ladder below rather than returned directly:
  //   - on a Sunday, "next Monday" IS tomorrow, so rung 2 would equal rung 1;
  //   - near a month end, "start of next month" can fall BEFORE next Monday, so rung 3 would
  //     regress below rung 2.
  // Either way a further 402 would buy no extra cooldown, which is the opposite of an escalation.
  const daysUntilNextMonday = ((8 - d.getUTCDay()) % 7) || 7;
  const rungs = [
    dayOffset(1),
    dayOffset(daysUntilNextMonday > 1 ? daysUntilNextMonday : daysUntilNextMonday + 7),
    Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 1),
  ];
  const target = Math.min(Math.max(Number(streak) || 1, 1), rungs.length);
  let value = rungs[0];
  for (let i = 1; i < target; i += 1) {
    value = Math.max(rungs[i], value + 86_400_000); // at least a day beyond the previous rung
  }
  return value;
}
