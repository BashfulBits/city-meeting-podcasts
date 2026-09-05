import test from "node:test";
import assert from "node:assert/strict";
import {
  minInterRequestGapMs,
  requestStartMarginMs,
  rpmWindowDurationMs,
  availableTokenBudget,
  reservationFor,
  earliestSafeStart,
  routeHasCapacityFor,
  computeRouteLaneWait,
  zonedDateKey,
  nextZonedMidnightMs,
  paymentRequiredBackoffUntil,
  FULL_TOKEN_BUDGET_WINDOWS,
  effectiveBufferSeconds,
} from "../src/pacing.js";

const NOW = 1_700_000_000_000;

function freshRoute(overrides = {}) {
  return {
    route_id: "r1",
    rpm: 30,
    rpd: 500,
    tpm: 10000,
    rpm_window_start: 0,
    rpm_count: 0,
    rpd_window_start: 0,
    rpd_count: 0,
    tpm_window_start: 0,
    tpm_reserved: 0,
    full_token_budget: 10000 * FULL_TOKEN_BUDGET_WINDOWS,
    token_budget_updated_at: NOW,
    throttle_streak: 0,
    blocked_until: null,
    buffer_seconds: 0,
    ...overrides,
  };
}

function job(overrides = {}) {
  return { input_token_estimate: 500, max_output_token_estimate: 200, ...overrides };
}

test("minInterRequestGapMs paces evenly to the RPM limit", () => {
  assert.equal(minInterRequestGapMs({ rpm: 30 }), 2000); // 60000/30
  assert.equal(minInterRequestGapMs({ rpm: 60 }), 1000);
  assert.equal(minInterRequestGapMs({ rpm: 0 }), 0);
  assert.equal(minInterRequestGapMs({}), 0);
});

test("minInterRequestGapMs adds a request-start safety margin without using response time", () => {
  const route = { rpm: 1, request_start_margin_seconds: 2 };
  assert.equal(requestStartMarginMs(route), 2000);
  assert.equal(minInterRequestGapMs(route), 62_000);
  assert.equal(rpmWindowDurationMs(route), 62_000);

  const exhausted = freshRoute({
    rpm: 1,
    request_start_margin_seconds: 2,
    rpm_window_start: NOW,
    rpm_count: 1,
  });
  assert.equal(earliestSafeStart(exhausted, job(), NOW, NOW).notBeforeAt, NOW + 62_000);
});

test("availableTokenBudget refills linearly up to the FULL_TOKEN_BUDGET_WINDOWS cap", () => {
  const route = freshRoute({ full_token_budget: 0, token_budget_updated_at: NOW, tpm: 6000 });
  assert.equal(availableTokenBudget(route, NOW), 0);
  assert.equal(availableTokenBudget(route, NOW + 30_000), 3000); // half a minute -> half of tpm
  const cap = 6000 * FULL_TOKEN_BUDGET_WINDOWS;
  assert.equal(availableTokenBudget(route, NOW + 999_000_000), cap); // long idle caps, doesn't grow unbounded
});

test("reservationFor takes the max of client estimate, floor, and calibrated margin", () => {
  const j = job({ input_token_estimate: 500, max_output_token_estimate: 200 }); // 700
  assert.equal(reservationFor(j), 700);
  assert.equal(reservationFor(j, { estimateFloor: 1000 }), 1000);
  assert.equal(reservationFor(j, { calibratedMargin: 1500 }), 1500);
  assert.equal(reservationFor(j, { estimateFloor: 800, calibratedMargin: 600 }), 800);
});

test("earliestSafeStart admits immediately when a fresh route has full headroom", () => {
  const route = freshRoute();
  const result = earliestSafeStart(route, job(), NOW, NOW);
  assert.equal(result.notBeforeAt, NOW);
  assert.equal(result.reservation, 700);
});

test("earliestSafeStart waits for the next RPM window once the window is full", () => {
  const route = freshRoute({ rpm: 30, rpm_window_start: NOW, rpm_count: 30 });
  const result = earliestSafeStart(route, job(), NOW, NOW);
  assert.equal(result.notBeforeAt, NOW + 60_000);
});

test("earliestSafeStart does not wait once the RPM window has rolled over", () => {
  const route = freshRoute({ rpm: 30, rpm_window_start: NOW - 61_000, rpm_count: 30 });
  const result = earliestSafeStart(route, job(), NOW, NOW);
  assert.equal(result.notBeforeAt, NOW);
});

test("earliestSafeStart waits for the provider's next calendar day once the daily cap is full", () => {
  // Daily quotas roll on the provider's clock, not 24h after our first request of the day. The
  // route is exhausted only while its recorded day key is still today in that timezone.
  const tz = "America/Los_Angeles";
  const today = zonedDateKey(NOW, tz);
  const route = freshRoute({ rpd: 500, rpd_day_key: today, rpd_count: 500, reset_timezone: tz });
  const result = earliestSafeStart(route, job(), NOW, NOW);
  assert.equal(result.notBeforeAt, nextZonedMidnightMs(NOW, tz));
  assert.ok(
    result.notBeforeAt - NOW < 86_400_000,
    "next local midnight must be sooner than a full rolling 24h from now",
  );
});

test("a stale day key means the provider already reset, so the route is ready now", () => {
  // The bug this covers: v2 anchored the daily window on first use, so a route exhausted at (say)
  // 14:00 local stayed exhausted until 14:00 the NEXT day -- ~14 hours past Gemini's actual
  // midnight-Pacific rollover. Because an exhausted route scores 0 in _capacityFraction, its whole
  // model then drops out of the ranking and its jobs go unclaimed.
  const tz = "America/Los_Angeles";
  const route = freshRoute({
    rpd: 500,
    rpd_day_key: zonedDateKey(NOW - 86_400_000, tz), // yesterday, in the provider's timezone
    rpd_count: 500,
    reset_timezone: tz,
  });
  const result = earliestSafeStart(route, job(), NOW, NOW);
  assert.equal(result.notBeforeAt, NOW, "a rolled-over daily quota must not hold the route");
});

test("rpd <= 0 keeps its pre-existing pacing meaning (unconstrained here)", () => {
  // Pins the behaviour rather than endorsing it: fixedWindowReadyAt treated `limit <= 0` as
  // unlimited while _capacityFraction reads `rpd: 0` as paused. That disagreement predates the
  // timezone work and is left as-is; a paused route is dropped from the ranking before pacing
  // is reached, so the inconsistency is not reachable in practice.
  const route = freshRoute({ rpd: 0, rpd_count: 0 });
  assert.equal(earliestSafeStart(route, job(), NOW, NOW).notBeforeAt, NOW);
});

test("earliestSafeStart waits for token budget to refill for an oversized job", () => {
  // A job needing 5000 tokens on a route with only 1000 currently available (tpm=6000, budget
  // drained to near-zero) must wait for enough to accumulate, not be rejected outright.
  const route = freshRoute({ tpm: 6000, full_token_budget: 1000, token_budget_updated_at: NOW });
  const bigJob = job({ input_token_estimate: 4000, max_output_token_estimate: 1000 }); // 5000
  const result = earliestSafeStart(route, bigJob, NOW, NOW);
  assert.ok(result.notBeforeAt > NOW);
  // Refill rate is tpm/60000 tokens/ms; deficit is 5000-1000=4000 tokens.
  const expectedWaitMs = Math.ceil((4000 * 60_000) / 6000);
  assert.equal(result.notBeforeAt, NOW + expectedWaitMs);
});

test("earliestSafeStart returns null when a reservation exceeds the route's burst ceiling outright", () => {
  const route = freshRoute({ tpm: 1000 }); // ceiling = 1000 * FULL_TOKEN_BUDGET_WINDOWS = 5000
  const hugeJob = job({ input_token_estimate: 4000, max_output_token_estimate: 2000 }); // 6000 > 5000
  const result = earliestSafeStart(route, hugeJob, NOW, NOW);
  assert.equal(result, null);
});

test("earliestSafeStart returns null for a hard_input_ceiling route, well inside the normal burst window", () => {
  // Gemini-shaped route: tpm=250000 puts the ordinary burst ceiling at 1.25M (comfortably above
  // this reservation), but a verified hard_input_ceiling below tpm itself must still reject it --
  // confirmed live: Gemini's free tier rejects a single oversized request outright with no burst
  // room, unlike the ordinary FULL_TOKEN_BUDGET_WINDOWS bucket assumed above.
  const route = freshRoute({
    tpm: 250_000,
    hard_input_ceiling: 120_000,
    full_token_budget: 250_000 * FULL_TOKEN_BUDGET_WINDOWS,
  });
  const oversizedJob = job({ input_token_estimate: 150_000, max_output_token_estimate: 1000 });
  const result = earliestSafeStart(route, oversizedJob, NOW, NOW);
  assert.equal(result, null);
});

test("earliestSafeStart ignores hard_input_ceiling for a route that never set it (NVIDIA-shaped)", () => {
  // NVIDIA-shaped route: tpm=40000 configured, but confirmed live to accept a real request nearly
  // 3x that outright. No hard_input_ceiling set here, so the ordinary bucket math alone governs --
  // this is the regression test for NOT over-restricting a provider we haven't verified.
  const route = freshRoute({ tpm: 40_000, full_token_budget: 40_000 * FULL_TOKEN_BUDGET_WINDOWS });
  const largeJob = job({ input_token_estimate: 115_000, max_output_token_estimate: 500 });
  const result = earliestSafeStart(route, largeJob, NOW, NOW);
  assert.ok(result !== null);
});

test("earliestSafeStart honors an additive route buffer from repeated 429s", () => {
  // The buffer now carries the moment it was raised, so it can be spent down; a buffer raised
  // right now is still owed in full.
  const route = freshRoute({ buffer_seconds: 30, buffer_updated_at: NOW });
  const result = earliestSafeStart(route, job(), NOW, NOW);
  assert.equal(result.notBeforeAt, NOW + 30_000);
});

test("earliestSafeStart spends the route buffer down as time passes", () => {
  // The other half of the 2026-08-30 stall. This delay is applied relative to `now`, so reading
  // the stored figure pushed every future dispatch a full buffer into the future forever -- the
  // capacity fix alone did not free the route, because this kept it unschedulable.
  const route = freshRoute({ buffer_seconds: 30, buffer_updated_at: NOW });
  assert.equal(earliestSafeStart(route, job(), NOW + 10_000, NOW + 10_000).notBeforeAt,
    NOW + 10_000 + 20_000, "only the remaining 20s is still owed");
  assert.equal(earliestSafeStart(route, job(), NOW + 30_000, NOW + 30_000).notBeforeAt,
    NOW + 30_000, "fully spent, so no delay at all");
});

test("earliestSafeStart ignores a buffer with no timestamp, as written before the migration", () => {
  const route = freshRoute({ buffer_seconds: 30 });
  assert.equal(earliestSafeStart(route, job(), NOW, NOW).notBeforeAt, NOW);
});

test("earliestSafeStart honors an explicit blocked_until floor", () => {
  const route = freshRoute({ blocked_until: NOW + 45_000 });
  const result = earliestSafeStart(route, job(), NOW, NOW);
  assert.equal(result.notBeforeAt, NOW + 45_000);
});

test("earliestSafeStart never returns a time before the lane's own earliestCandidateTime", () => {
  const route = freshRoute();
  const laneTime = NOW + 10_000; // a predecessor in this lane already occupies until here
  const result = earliestSafeStart(route, job(), laneTime, NOW);
  assert.equal(result.notBeforeAt, laneTime);
});

test("routeHasCapacityFor is false when the safe start plus call duration exceeds the window", () => {
  const route = freshRoute({ blocked_until: NOW + 20_000 });
  const withinWindow = routeHasCapacityFor(route, job(), NOW, 25, { callDurationCeilingMs: 1000 });
  assert.equal(withinWindow, true); // 20s block + 1s ceiling < 25s window

  const tooLate = routeHasCapacityFor(route, job(), NOW, 25, { callDurationCeilingMs: 6000 });
  assert.equal(tooLate, false); // 20s block + 6s ceiling > 25s window
});

test("routeHasCapacityFor is false for a permanently-oversized reservation", () => {
  const route = freshRoute({ tpm: 100 });
  const hugeJob = job({ input_token_estimate: 10_000, max_output_token_estimate: 10_000 });
  assert.equal(routeHasCapacityFor(route, hugeJob, NOW, 25), false);
});

test("computeRouteLaneWait returns wait_ms relative to now, plus the inter-request gap", () => {
  const route = freshRoute({ rpm: 30 });
  const laneTime = NOW + 5000;
  const result = computeRouteLaneWait(route, job(), laneTime, NOW);
  assert.equal(result.wait_ms, 5000);
  assert.equal(result.not_before_at, laneTime);
  assert.equal(result.min_inter_request_gap_ms, 2000);
  assert.equal(result.reservation, 700);
});

test("computeRouteLaneWait returns null for a permanently-oversized reservation", () => {
  const route = freshRoute({ tpm: 100 });
  const hugeJob = job({ input_token_estimate: 10_000, max_output_token_estimate: 10_000 });
  assert.equal(computeRouteLaneWait(route, hugeJob, NOW, NOW), null);
});

test("paymentRequiredBackoffUntil blocks until the start of the next UTC day on the first 402", () => {
  const now = Date.UTC(2024, 0, 3, 15, 0, 0); // Wed 2024-01-03 15:00 UTC
  assert.equal(paymentRequiredBackoffUntil(1, now), Date.UTC(2024, 0, 4));
});

test("paymentRequiredBackoffUntil blocks until next UTC Monday on the second consecutive 402", () => {
  const wednesday = Date.UTC(2024, 0, 3); // 2024-01-03 is a Wednesday
  assert.equal(paymentRequiredBackoffUntil(2, wednesday), Date.UTC(2024, 0, 8)); // next Monday

  // A streak reached exactly on a Monday still gets a full week out, not zero.
  const monday = Date.UTC(2024, 0, 1); // 2024-01-01 is a Monday
  assert.equal(paymentRequiredBackoffUntil(2, monday), Date.UTC(2024, 0, 8));
});

test("paymentRequiredBackoffUntil blocks until the start of next UTC month on the third+ consecutive 402", () => {
  const now = Date.UTC(2024, 0, 20);
  assert.equal(paymentRequiredBackoffUntil(3, now), Date.UTC(2024, 1, 1));
  // Stays pinned to "next month," not escalating indefinitely past this point.
  assert.equal(paymentRequiredBackoffUntil(5, now), Date.UTC(2024, 1, 1));
});

test("paymentRequiredBackoffUntil rolls the year over correctly for a December streak", () => {
  const december = Date.UTC(2024, 11, 15);
  assert.equal(paymentRequiredBackoffUntil(3, december), Date.UTC(2025, 0, 1));
});

test("effectiveBufferSeconds decays a 429 buffer to nothing over its own duration", () => {
  // The 2026-08-30 deadlock in miniature. A stored buffer of 60s is >= the 25s dispatch window,
  // so _capacityFraction scores the route 0; with no decay the only escape was a success that
  // scoring 0 makes impossible. It must instead run down on its own.
  const now = Date.parse("2026-08-30T12:00:00Z");
  const route = { buffer_seconds: 60, buffer_updated_at: now };

  assert.equal(effectiveBufferSeconds(route, now), 60);
  assert.equal(effectiveBufferSeconds(route, now + 20_000), 40);
  // Below the 25s window here, so the route starts being ranked again.
  assert.ok(effectiveBufferSeconds(route, now + 40_000) < 25);
  assert.equal(effectiveBufferSeconds(route, now + 60_000), 0);
  assert.equal(effectiveBufferSeconds(route, now + 600_000), 0, "never goes negative");
});

test("effectiveBufferSeconds treats a pre-migration row as already elapsed", () => {
  // Rows written before buffer_updated_at existed carry 0. Those are precisely the routes stuck
  // under the old behaviour, so they must read as recovered rather than as blocked at t=0.
  const now = Date.parse("2026-08-30T12:00:00Z");
  assert.equal(effectiveBufferSeconds({ buffer_seconds: 120, buffer_updated_at: 0 }, now), 0);
  assert.equal(effectiveBufferSeconds({ buffer_seconds: 120 }, now), 0);
});

test("effectiveBufferSeconds ignores absent or nonsensical buffers", () => {
  const now = Date.now();
  assert.equal(effectiveBufferSeconds({}, now), 0);
  assert.equal(effectiveBufferSeconds(null, now), 0);
  assert.equal(effectiveBufferSeconds({ buffer_seconds: 0, buffer_updated_at: now }, now), 0);
  assert.equal(effectiveBufferSeconds({ buffer_seconds: -5, buffer_updated_at: now }, now), 0);
});
